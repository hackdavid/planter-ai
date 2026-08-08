"""Scene Analysis service using Gemini Vision to extract geometry, lighting, and placement data from frontage images."""

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = """
You are a scene-analysis agent for a visual-compositing pipeline.
An image shows the street-facing frontage of an independent café or restaurant.

Your job: extract physical and lighting information so a planter can be placed realistically in the scene.

Analyze the image carefully and respond ONLY with a JSON object in this exact format (no markdown, no extra text):

{
  "pixels_per_meter": 120,
  "reference_object": "door",
  "reference_width_meters": 0.9,
  "shadow_angle_deg": 45,
  "shadow_softness": "soft",
  "ground_plane_y": 410,
  "door_bbox": {"x": 200, "y": 150, "width": 120, "height": 220},
  "placement_candidates": [
    {"x": 120, "y": 410, "label": "left", "score": 0.9},
    {"x": 320, "y": 410, "label": "center", "score": 0.7},
    {"x": 520, "y": 410, "label": "right", "score": 0.8}
  ],
  "lighting_direction": "left_to_right",
  "image_height": 480,
  "image_width": 640
}

Field definitions:
- pixels_per_meter: Estimate based on a known reference object (standard door = 0.9m, pavement slab = 0.6m, A-board = 0.5m). Divide the object's pixel width by its real-world width.
- reference_object: Name of the object you used for scale (e.g., "door", "window", "pavement_slab", "a_board").
- reference_width_meters: Real-world width of the reference object in meters.
- shadow_angle_deg: Direction of existing shadows in the scene, measured in degrees where 0° = right, 90° = down, 180° = left, 270° = up. If no clear shadows, estimate from lighting direction.
- shadow_softness: "hard" for sharp-edged shadows (bright direct sun), "soft" for diffuse shadows (cloudy/overcast), "none" if no shadows visible.
- ground_plane_y: The y-coordinate (in pixels from top) where the sidewalk/ground meets the building base. This is where the planter should sit.
- door_bbox: Bounding box of the main entrance door in pixels. Use this to avoid blocking the entrance.
- placement_candidates: Ranked list of bare-ground spots near the entrance where a planter could go without blocking access. Each should have x, y coordinates and a quality score 0.0–1.0. Include at least 3 candidates.
- lighting_direction: General direction light is coming from (e.g., "left_to_right", "right_to_left", "front", "back", "top").
- image_height, image_width: Dimensions of the image in pixels.

Important:
- If you cannot identify a clear reference object, use the average pavement-slab size (0.6m) or estimate from the door frame.
- ground_plane_y should be realistic: in street-view photos the sidewalk is typically in the bottom 10–20% of the frame (y ≈ 0.85–0.90 * image_height).
- placement_candidates must be on the ground plane (y ≈ ground_plane_y) and not overlap the door_bbox.
""".strip()


@dataclass(frozen=True)
class SceneAnalysisResult:
    """Structured result from a scene analysis call."""

    pixels_per_meter: float
    reference_object: str
    reference_width_meters: float
    shadow_angle_deg: float
    shadow_softness: str
    ground_plane_y: int
    door_bbox: dict
    placement_candidates: list[dict]
    lighting_direction: str
    image_height: int
    image_width: int
    model: str
    cached: bool = False

    def get_placement(self, label: str = "center") -> dict | None:
        """Return the placement candidate matching the given label."""
        for c in self.placement_candidates:
            if c.get("label") == label:
                return c
        # Fallback to first candidate if label not found
        return self.placement_candidates[0] if self.placement_candidates else None


class SceneAnalysisService:
    """
    Uses Gemini vision model to extract scene geometry, lighting, and placement
    information from a validated frontage photograph.

    Results are cached to disk by image hash to avoid repeat API calls.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.5-flash-lite",
        cache_dir: Path | None = None,
    ):
        self.api_key = api_key
        self.model_name = model
        self.cache_dir = cache_dir or (Path(__file__).parent.parent / "data" / "scene_analysis_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        try:
            from google import genai
            self._client = genai.Client(api_key=api_key)
        except ImportError as exc:
            raise ImportError(
                "google-genai package is required. Install with: pip install google-genai"
            ) from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(
        self,
        image_path: Path,
        prompt: str | None = None,
        force_refresh: bool = False,
    ) -> SceneAnalysisResult | None:
        """
        Run Gemini scene analysis on a single frontage image.

        Args:
            image_path: Path to the validated frontage image.
            prompt: Optional custom prompt.
            force_refresh: If True, bypass cache and re-call the API.

        Returns:
            SceneAnalysisResult with geometry and lighting data, or None if the API call fails.
        """
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(image_path)

        prompt = prompt or DEFAULT_PROMPT
        cache_key = self._hash(image_path, prompt)
        cache_file = self.cache_dir / f"{cache_key}.json"

        # ---- cache purge (force refresh) -----------------------------
        if force_refresh and cache_file.exists():
            cache_file.unlink()
            logger.info("[SCENE] CACHE PURGED | %s", image_path.name)

        # ---- cache hit ------------------------------------------------
        if cache_file.exists():
            logger.info("[SCENE] CACHE HIT | %s", image_path.name)
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return self._from_dict(data)

        # ---- API call -------------------------------------------------
        logger.info("[SCENE] API CALL | %s | model=%s", image_path.name, self.model_name)

        pil_image = Image.open(image_path)
        actual_w, actual_h = pil_image.size
        try:
            response = self._client.models.generate_content(
                model=self.model_name,
                contents=[prompt, pil_image],
            )
            raw_text = response.text or "{}"
        except Exception as exc:
            logger.error("[SCENE] Gemini API error: %s", exc)
            err_str = str(exc)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                raise RuntimeError(
                    "Gemini API quota exceeded. Check your plan and billing at "
                    "https://ai.google.dev/gemini-api/docs/rate-limits"
                ) from exc
            return None

        result = self._parse(raw_text, actual_w, actual_h)
        if result is None:
            logger.warning("[SCENE] Failed to parse response for %s", image_path.name)
            return None

        # Persist cache
        cache_data = {
            "pixels_per_meter": result.pixels_per_meter,
            "reference_object": result.reference_object,
            "reference_width_meters": result.reference_width_meters,
            "shadow_angle_deg": result.shadow_angle_deg,
            "shadow_softness": result.shadow_softness,
            "ground_plane_y": result.ground_plane_y,
            "door_bbox": result.door_bbox,
            "placement_candidates": result.placement_candidates,
            "lighting_direction": result.lighting_direction,
            "image_height": result.image_height,
            "image_width": result.image_width,
            "model": self.model_name,
            "image_path": str(image_path),
        }
        cache_file.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")

        logger.info(
            "[SCENE] RESULT | ppm=%.1f | ground_y=%d | shadow=%s | candidates=%d",
            result.pixels_per_meter,
            result.ground_plane_y,
            result.shadow_softness,
            len(result.placement_candidates),
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hash(image_path: Path, prompt: str) -> str:
        hasher = hashlib.sha256()
        hasher.update(image_path.read_bytes())
        hasher.update(prompt.encode("utf-8"))
        return hasher.hexdigest()[:16]

    @staticmethod
    def _parse(raw_text: str, actual_w: int = 640, actual_h: int = 480) -> SceneAnalysisResult | None:
        """Parse Gemini response text into SceneAnalysisResult.

        All coordinates are normalized to the actual image dimensions because
        Gemini may estimate coordinates at a different internal resolution.
        """
        # Strip markdown code fences if present
        text = raw_text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("[SCENE] JSON parse failed. Raw text:\n%s", raw_text[:500])
            return None

        # Validate required fields and apply sensible defaults
        ppm = float(data.get("pixels_per_meter", 100.0))
        if ppm <= 0:
            ppm = 100.0

        reported_h = int(data.get("image_height", actual_h))
        reported_w = int(data.get("image_width", actual_w))
        if reported_h <= 0:
            reported_h = actual_h
        if reported_w <= 0:
            reported_w = actual_w

        # Compute normalization scales
        scale_x = actual_w / reported_w
        scale_y = actual_h / reported_h

        ground_y = int(int(data.get("ground_plane_y", 0)) * scale_y)
        if ground_y <= 0 or ground_y >= actual_h:
            ground_y = int(actual_h * 0.88)

        # Normalize placement candidates
        raw_candidates = data.get("placement_candidates", [])
        candidates: list[dict] = []
        if raw_candidates and isinstance(raw_candidates, list):
            for c in raw_candidates:
                if isinstance(c, dict):
                    candidates.append({
                        "x": int(c.get("x", 0) * scale_x),
                        "y": int(c.get("y", 0) * scale_y),
                        "label": c.get("label", "unknown"),
                        "score": float(c.get("score", 0.5)),
                    })
        if not candidates:
            candidates = [
                {"x": int(actual_w * 0.22), "y": ground_y, "label": "left", "score": 0.8},
                {"x": int(actual_w * 0.50), "y": ground_y, "label": "center", "score": 0.6},
                {"x": int(actual_w * 0.78), "y": ground_y, "label": "right", "score": 0.8},
            ]

        # Normalize door bbox
        raw_door = data.get("door_bbox", {})
        door_bbox: dict = {}
        if raw_door and isinstance(raw_door, dict):
            door_bbox = {
                "x": int(raw_door.get("x", 0) * scale_x),
                "y": int(raw_door.get("y", 0) * scale_y),
                "width": int(raw_door.get("width", reported_w * 0.3) * scale_x),
                "height": int(raw_door.get("height", reported_h * 0.5) * scale_y),
            }
        else:
            door_bbox = {
                "x": int(actual_w * 0.35),
                "y": int(actual_h * 0.3),
                "width": int(actual_w * 0.3),
                "height": int(actual_h * 0.5),
            }

        return SceneAnalysisResult(
            pixels_per_meter=ppm,
            reference_object=data.get("reference_object", "door"),
            reference_width_meters=float(data.get("reference_width_meters", 0.9)),
            shadow_angle_deg=float(data.get("shadow_angle_deg", 90.0)),
            shadow_softness=data.get("shadow_softness", "soft"),
            ground_plane_y=ground_y,
            door_bbox=door_bbox,
            placement_candidates=candidates,
            lighting_direction=data.get("lighting_direction", "left_to_right"),
            image_height=actual_h,
            image_width=actual_w,
            model=data.get("model", "gemini-3.5-flash-lite"),
        )

    @staticmethod
    def _from_dict(data: dict) -> SceneAnalysisResult:
        """Reconstruct SceneAnalysisResult from cached dict."""
        return SceneAnalysisResult(
            pixels_per_meter=float(data.get("pixels_per_meter", 100.0)),
            reference_object=data.get("reference_object", "door"),
            reference_width_meters=float(data.get("reference_width_meters", 0.9)),
            shadow_angle_deg=float(data.get("shadow_angle_deg", 90.0)),
            shadow_softness=data.get("shadow_softness", "soft"),
            ground_plane_y=int(data.get("ground_plane_y", 410)),
            door_bbox=data.get("door_bbox", {}),
            placement_candidates=data.get("placement_candidates", []),
            lighting_direction=data.get("lighting_direction", "left_to_right"),
            image_height=int(data.get("image_height", 480)),
            image_width=int(data.get("image_width", 640)),
            model=data.get("model", "gemini-3.5-flash-lite"),
            cached=True,
        )
