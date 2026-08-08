"""Vision QA service using Gemini to validate frontage images."""

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = """
You are a quality-control agent for a planter-sales company.
An image has been proposed as the "street-facing frontage" of an independent café or restaurant.

Your job: decide if this image is suitable.

Checklist (all must be true for YES):
1. The image clearly shows the exterior front of the venue (not the interior, not a side alley, not a satellite map).
2. The photo is taken from street level / pedestrian perspective.
3. The entrance or shop-front is visible and unobscured.
4. The image is a photograph, not a sketch, logo, or menu graphic.
5. There is no large planter, flower box, or heavy greenery already blocking the frontage.

Respond ONLY with a JSON object in this exact format (no markdown, no extra text):
{
  "pass": true or false,
  "confidence": 0.0 to 1.0,
  "reason": "One-sentence explanation"
}
""".strip()


@dataclass(frozen=True)
class VisionQAVerdict:
    """Structured result from a single image QA call."""

    pass_: bool
    confidence: float
    reason: str
    model: str
    cached: bool = False


class VisionQAService:
    """
    Uses Gemini vision model to validate whether an image is a usable
    street-facing frontage photograph.

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
        self.cache_dir = cache_dir or (Path(__file__).parent.parent / "data" / "vision_qa_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Lazy import so the module can be loaded without the SDK installed
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

    def evaluate_image(
        self,
        image_path: Path,
        prompt: str | None = None,
        force_refresh: bool = False,
    ) -> VisionQAVerdict:
        """
        Run Gemini vision QA on a single image.

        Args:
            image_path: Path to the image file.
            prompt: Optional custom prompt (default is frontage checklist).
            force_refresh: If True, bypass cache and re-call the API.

        Returns:
            VisionQAVerdict with pass/fail, confidence, and reasoning.
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
            logger.info("[VISION_QA] CACHE PURGED | %s", image_path.name)

        # ---- cache hit ------------------------------------------------
        if cache_file.exists():
            logger.info("[VISION_QA] CACHE HIT | %s", image_path.name)
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return VisionQAVerdict(
                pass_=data["pass"],
                confidence=data["confidence"],
                reason=data["reason"],
                model=data.get("model", self.model_name),
                cached=True,
            )

        # ---- API call -------------------------------------------------
        logger.info("[VISION_QA] API CALL | %s | model=%s", image_path.name, self.model_name)

        pil_image = Image.open(image_path)
        try:
            response = self._client.models.generate_content(
                model=self.model_name,
                contents=[prompt, pil_image],
            )
            raw_text = response.text or "{}"
        except Exception as exc:
            # Log the full error but surface a clean exception
            logger.error("[VISION_QA] Gemini API error: %s", exc)
            # If it's a quota/rate-limit error, raise a specific message
            err_str = str(exc)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                raise RuntimeError(
                    "Gemini API quota exceeded. Check your plan and billing at "
                    "https://ai.google.dev/gemini-api/docs/rate-limits"
                ) from exc
            raise

        verdict = self._parse(raw_text)

        # Persist cache
        cache_file.write_text(
            json.dumps(
                {
                    "pass": verdict.pass_,
                    "confidence": verdict.confidence,
                    "reason": verdict.reason,
                    "model": self.model_name,
                    "image_path": str(image_path),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        logger.info(
            "[VISION_QA] RESULT | pass=%s | confidence=%.2f | %s",
            verdict.pass_,
            verdict.confidence,
            verdict.reason,
        )
        return verdict

    def evaluate_candidates(
        self,
        candidates: list[dict],
        stop_on_first_pass: bool = True,
        force_refresh: bool = False,
    ) -> dict:
        """
        Evaluate a list of image candidates sequentially.

        Args:
            candidates: List of dicts with keys "path" (str) and optional "source" (str).
            stop_on_first_pass: If True, return immediately on first passing image.
            force_refresh: If True, bypass cache for all candidates.

        Returns:
            Dict with:
              - "best_image": path of the best passing image, or None
              - "results": list of VisionQAVerdict dicts for all tried images
              - "tried_count": how many images were evaluated
        """
        results: list[dict] = []
        best_image: Optional[str] = None

        for cand in candidates:
            path = Path(cand["path"])
            source = cand.get("source", "unknown")

            try:
                verdict = self.evaluate_image(path, force_refresh=force_refresh)
            except Exception as exc:
                logger.warning("[VISION_QA] ERROR evaluating %s: %s", path, exc)
                results.append(
                    {
                        "path": str(path),
                        "source": source,
                        "pass": False,
                        "confidence": 0.0,
                        "reason": f"Error: {exc}",
                        "cached": False,
                    }
                )
                continue

            entry = {
                "path": str(path),
                "source": source,
                "pass": verdict.pass_,
                "confidence": verdict.confidence,
                "reason": verdict.reason,
                "cached": verdict.cached,
            }
            results.append(entry)

            if verdict.pass_ and best_image is None:
                best_image = str(path)
                logger.info("[VISION_QA] FIRST PASS | %s | source=%s", path.name, source)
                if stop_on_first_pass:
                    break

        return {
            "best_image": best_image,
            "results": results,
            "tried_count": len(results),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hash(image_path: Path, prompt: str) -> str:
        """Deterministic cache key from file content + prompt."""
        content = image_path.read_bytes()
        hasher = hashlib.md5()
        hasher.update(content)
        hasher.update(prompt.encode("utf-8"))
        return hasher.hexdigest()[:16]

    @staticmethod
    def _parse(raw_text: str) -> VisionQAVerdict:
        """Parse Gemini response into a structured verdict."""
        # Strip markdown fences if present
        text = raw_text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove first and last fence lines
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Fallback heuristic if JSON parse fails
            lower = text.lower()
            passed = "yes" in lower or "true" in lower or "pass" in lower
            return VisionQAVerdict(
                pass_=passed,
                confidence=0.5 if passed else 0.0,
                reason=text[:200],
                model="gemini-2.0-flash-lite",
            )

    def _parse(self, raw_text: str) -> VisionQAVerdict:
        """Parse Gemini response into a structured verdict."""
        # Strip markdown fences if present
        text = raw_text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove first and last fence lines
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Fallback heuristic if JSON parse fails
            lower = text.lower()
            passed = "yes" in lower or "true" in lower or "pass" in lower
            return VisionQAVerdict(
                pass_=passed,
                confidence=0.5 if passed else 0.0,
                reason=text[:200],
                model=self.model_name,
            )

        return VisionQAVerdict(
            pass_=bool(data.get("pass", False)),
            confidence=float(data.get("confidence", 0.0)),
            reason=str(data.get("reason", "No reason provided.")),
            model=self.model_name,
        )
