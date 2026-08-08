"""Deterministic planter compositing service (Approach 1: CV-based)."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger(__name__)

# Lazy-load rembg only when needed (downloads ~170 MB model on first use)
_rembg_available: bool | None = None


def _has_rembg() -> bool:
    global _rembg_available
    if _rembg_available is None:
        try:
            import rembg  # noqa: F401
            _rembg_available = True
        except Exception:
            _rembg_available = False
    return _rembg_available


@dataclass(frozen=True)
class CompositeResult:
    """Single composite output."""

    position: str
    path: Path
    scale_ratio: float
    anchor_x: int
    anchor_y: int


class CompositingService:
    """
    Places a user-uploaded planter image onto a validated frontage photograph.

    Uses deterministic computer vision (OpenCV + PIL) — no generative AI.
    """

    def __init__(self, output_dir: Path | None = None):
        self.output_dir = output_dir or (Path(__file__).parent.parent / "data" / "composites")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compose(
        self,
        venue_id: str,
        frontage_path: Path,
        planter_path: Path,
        positions: list[str] | None = None,
        scene_analysis: dict | None = None,
        planter_width_cm: float = 35.0,
        planter_height_cm: float = 45.0,
        door_gap_cm: float = 30.0,
    ) -> list[CompositeResult]:
        """
        Compose the planter onto the frontage at multiple positions.

        Args:
            venue_id: Place ID of the venue.
            frontage_path: Path to the validated frontage JPG.
            planter_path: Path to the planter product image (PNG with alpha, or JPG).
            positions: List of positions to render (default: left, center, right).
            scene_analysis: Optional dict from SceneAnalysisService with keys:
                pixels_per_meter, ground_plane_y, shadow_angle_deg, shadow_softness,
                placement_candidates, door_bbox, image_width, image_height.
            planter_width_cm: Real-world width of the planter in centimeters.
            planter_height_cm: Real-world height of the planter in centimeters.
            door_gap_cm: Distance from door frame to planter in centimeters.

        Returns:
            List of CompositeResult objects.
        """
        positions = positions or ["left", "center", "right"]
        frontage = Image.open(frontage_path).convert("RGBA")
        planter_raw = Image.open(planter_path)
        fw, fh = frontage.size

        # Handle planter background removal if needed
        planter = self._prepare_planter(planter_raw)

        # --- Scale using real-world dimensions ---
        if scene_analysis:
            ppm = float(scene_analysis.get("pixels_per_meter", 100.0))
            planter_real_width_m = planter_width_cm / 100.0
            target_planter_width = int(planter_real_width_m * ppm)
            # Enforce visual minimum so the planter is clearly visible (at least 60px)
            target_planter_width = max(60, target_planter_width)
            # Clamp to sensible upper bound (max 25% of image width)
            target_planter_width = min(int(fw * 0.25), target_planter_width)
            logger.info("[COMPOSE] Real-world scale | ppm=%.1f | planter_w=%.2fm | target_px=%d",
                        ppm, planter_real_width_m, target_planter_width)
        else:
            target_planter_width = int(fw * 0.15)
            logger.info("[COMPOSE] Heuristic scale | target_px=%d", target_planter_width)

        planter = self._resize_planter(planter, target_planter_width)

        # Venue output directory
        venue_dir = self.output_dir / venue_id
        venue_dir.mkdir(parents=True, exist_ok=True)

        results: list[CompositeResult] = []

        for pos in positions:
            # --- Placement: compute relative to door for accuracy ---
            if scene_analysis:
                anchor_x, anchor_y = self._calculate_anchor_from_door(
                    fw, fh, pos, planter.size[0], scene_analysis, door_gap_cm / 100.0
                )
            else:
                anchor_x, anchor_y = self._calculate_anchor(fw, fh, pos, planter.size[0])

            # Apply a very subtle perspective skew
            skewed_planter = self._apply_perspective_skew(planter, intensity=0.06)

            # Build shadow with scene-matched direction if available
            pw, ph = skewed_planter.size
            shadow_angle = float(scene_analysis.get("shadow_angle_deg", 90.0)) if scene_analysis else 90.0
            shadow_softness = scene_analysis.get("shadow_softness", "soft") if scene_analysis else "soft"
            shadow = self._build_shadow((pw, ph), angle_deg=shadow_angle, softness=shadow_softness)

            # Composite — ground the planter visually
            composed = frontage.copy()
            composed = self._paste_shadow(composed, shadow, pw, anchor_x, anchor_y)
            composed = self._paste_contact_shadow(composed, pw, anchor_x, anchor_y)
            composed = self._paste_planter(composed, skewed_planter, anchor_x, anchor_y)

            # Convert to RGB and save
            output_path = venue_dir / f"planter_{pos}.jpg"
            composed.convert("RGB").save(output_path, quality=92)

            results.append(
                CompositeResult(
                    position=pos,
                    path=output_path,
                    scale_ratio=round(target_planter_width / fw, 3),
                    anchor_x=anchor_x,
                    anchor_y=anchor_y,
                )
            )
            logger.info("[COMPOSE] %s | %s | saved=%s", venue_id, pos, output_path.name)

        # Save metadata
        meta = {
            "venue_id": venue_id,
            "frontage_source": str(frontage_path),
            "planter_source": str(planter_path),
            "composites": [
                {
                    "position": r.position,
                    "path": str(r.path),
                    "scale_ratio": r.scale_ratio,
                    "anchor_x": r.anchor_x,
                    "anchor_y": r.anchor_y,
                }
                for r in results
            ],
        }
        (venue_dir / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _prepare_planter(img: Image.Image) -> Image.Image:
        """
        Remove background from the planter product image so only the object remains.

        Priority:
        1. rembg (U²-Net deep-learning segmentation) — best quality, handles
           complex backgrounds, shadows, and transparent props.
        2. If rembg is unavailable, fall back to chroma-key for near-white
           backgrounds and naive brightness threshold for everything else.
        """
        # Fast path: already has meaningful alpha channel
        if img.mode == "RGBA":
            # Check whether alpha is actually used (not all opaque)
            alpha = img.getchannel("A")
            if alpha.getextrema() != (255, 255):
                return img  # trust the existing alpha

        # Try rembg first for proper segmentation
        if _has_rembg():
            try:
                from rembg import remove as rembg_remove
                rgb = img.convert("RGB")
                result = rembg_remove(rgb)
                logger.info("[COMPOSE] rembg segmentation applied")
                return result.convert("RGBA")
            except Exception as exc:
                logger.warning("[COMPOSE] rembg failed (%s), falling back to chroma-key", exc)

        # Fallback: chroma-key + brightness threshold
        img = img.convert("RGBA")
        datas = img.getdata()
        new_data: list[tuple[int, int, int, int]] = []
        for item in datas:
            r, g, b, a = item
            # Chroma-key: near-white backgrounds
            if r > 240 and g > 240 and b > 240:
                new_data.append((255, 255, 255, 0))
            else:
                # Also catch light-gray studio backdrops
                brightness = int(0.299 * r + 0.587 * g + 0.114 * b)
                if brightness > 230:
                    new_data.append((255, 255, 255, 0))
                else:
                    new_data.append((r, g, b, a))
        img.putdata(new_data)
        return img

    @staticmethod
    def _calculate_anchor_from_door(
        fw: int, fh: int, position: str, planter_width: int, scene_analysis: dict, door_gap_m: float
    ) -> tuple[int, int]:
        """
        Calculate anchor point relative to the detected door bounding box.

        Positions:
        - left:  planter placed `door_gap_m` to the left of door left edge
        - center: planter centered on door center (on pavement directly in front)
        - right: planter placed `door_gap_m` to the right of door right edge

        Args:
            fw, fh: frontage image dimensions
            position: "left", "center", or "right"
            planter_width: pixel width of the resized planter
            scene_analysis: dict containing door_bbox and pixels_per_meter
            door_gap_m: real-world gap from door frame in meters

        Returns:
            (anchor_x, anchor_y) tuple
        """
        door_bbox = scene_analysis.get("door_bbox", {})
        ppm = float(scene_analysis.get("pixels_per_meter", 100.0))
        ground_y = int(scene_analysis.get("ground_plane_y", int(fh * 0.88)))

        # Parse door bbox
        door_x = int(door_bbox.get("x", fw * 0.35))
        door_w = int(door_bbox.get("width", fw * 0.3))
        door_left = door_x
        door_right = door_x + door_w
        door_center = door_left + door_w // 2

        gap_px = int(door_gap_m * ppm)

        if position == "left":
            # Place planter so its right edge is at door_left - gap
            x = door_left - gap_px - planter_width // 2
        elif position == "center":
            # Centered on door, directly in front
            x = door_center - planter_width // 2
        elif position == "right":
            # Place planter so its left edge is at door_right + gap
            x = door_right + gap_px - planter_width // 2
        else:
            x = fw // 2 - planter_width // 2

        # Clamp to image bounds with 5px margin
        x = max(5, min(fw - planter_width - 5, x))

        return x, ground_y

    @staticmethod
    def _resize_planter(planter: Image.Image, target_width: int) -> Image.Image:
        """Resize planter maintaining aspect ratio."""
        w, h = planter.size
        ratio = target_width / w
        new_size = (target_width, int(h * ratio))
        return planter.resize(new_size, Image.Resampling.LANCZOS)

    @staticmethod
    def _calculate_anchor(fw: int, fh: int, position: str, planter_width: int) -> tuple[int, int]:
        """
        Calculate the anchor point (bottom-center of planter) on the frontage.

        Street-view photos point slightly downward; the actual sidewalk/ground
        is typically in the bottom 10-15 % of the frame, so we place the
        planter base near 0.88 of the image height.
        """
        ground_y = int(fh * 0.88)

        if position == "left":
            x = int(fw * 0.22)
        elif position == "center":
            x = int(fw * 0.50)
        elif position == "right":
            x = int(fw * 0.78)
        else:
            x = int(fw * 0.50)

        # Center the planter horizontally on the anchor
        x = x - planter_width // 2
        return max(0, x), ground_y

    @staticmethod
    def _apply_perspective_skew(planter: Image.Image, intensity: float = 0.15) -> Image.Image:
        """
        Apply a trapezoid perspective skew so the planter looks like it's sitting on the ground.

        The bottom edge stays full width; the top edge narrows slightly.
        """
        w, h = planter.size
        # Define source and destination points for perspective transform
        src_pts = np.float32([[0, 0], [w, 0], [w, h], [0, h]])

        # Shrink the top edge by `intensity` proportion
        shrink = int(w * intensity)
        dst_pts = np.float32([
            [shrink, 0],          # top-left moved inward
            [w - shrink, 0],      # top-right moved inward
            [w, h],               # bottom-right stays
            [0, h],               # bottom-left stays
        ])

        matrix = cv2.getPerspectiveTransform(src_pts, dst_pts)
        planter_np = np.array(planter)
        warped = cv2.warpPerspective(planter_np, matrix, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))

        return Image.fromarray(warped, "RGBA")

    @staticmethod
    def _build_shadow(size: tuple[int, int], angle_deg: float = 90.0, softness: str = "soft") -> Image.Image:
        """
        Create a directional elliptical shadow beneath the planter.

        Args:
            size: (width, height) of the planter.
            angle_deg: Direction the shadow is cast. 90° = straight down (noon),
                       45° = down-right, 135° = down-left, etc.
            softness: "hard" for sharp shadows (bright sun), "soft" for diffuse,
                      "none" for minimal shadow.
        """
        w, h = size
        if softness == "none":
            return Image.new("RGBA", (1, 1), (0, 0, 0, 0))

        shadow_w = int(w * 1.25)
        shadow_h = int(h * 0.18)

        # Convert angle to an offset vector (how far the shadow center shifts)
        import math
        rad = math.radians(angle_deg)
        offset_x = int(shadow_h * 0.6 * math.cos(rad))
        offset_y = int(shadow_h * 0.3 * math.sin(rad))

        # Build a larger canvas so the shifted shadow doesn't get clipped
        canvas_w = shadow_w + abs(offset_x) + 20
        canvas_h = shadow_h + abs(offset_y) + 20
        draw = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(draw)

        opacity = 120 if softness == "soft" else 160
        d.ellipse([0, 0, shadow_w, shadow_h], fill=(0, 0, 0, opacity))

        blur_radius = max(shadow_w, shadow_h) // 3 if softness == "soft" else max(shadow_w, shadow_h) // 6
        shadow = draw.filter(ImageFilter.GaussianBlur(radius=blur_radius))

        # Crop back to usable size with offset embedded
        left = max(0, (canvas_w - shadow_w) // 2 + offset_x)
        top = max(0, (canvas_h - shadow_h) // 2 + offset_y)
        right = min(canvas_w, left + shadow_w)
        bottom = min(canvas_h, top + shadow_h)

        return shadow.crop((left, top, right, bottom))

    @staticmethod
    def _paste_shadow(frontage: Image.Image, shadow: Image.Image, planter_width: int, anchor_x: int, anchor_y: int) -> Image.Image:
        """
        Paste the soft shadow so it spreads out from under the planter.
        The shadow top sits at the ground line so it peeks out below.
        """
        sw, sh = shadow.size
        # Wider than planter so edges are visible
        x = anchor_x + (planter_width - sw) // 2
        # Top of shadow at ground line, extending downward
        y = anchor_y
        frontage.paste(shadow, (x, y), shadow)
        return frontage

    @staticmethod
    def _paste_contact_shadow(frontage: Image.Image, planter_width: int, anchor_x: int, anchor_y: int) -> Image.Image:
        """
        Add a dark contact shadow around the base of the planter.
        Slightly wider than the planter so it creates a visible dark rim.
        """
        cw = int(planter_width * 1.10)
        ch = max(3, int(planter_width * 0.05))

        contact = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        d = ImageDraw.Draw(contact)
        d.ellipse([0, 0, cw, ch], fill=(0, 0, 0, 160))
        contact = contact.filter(ImageFilter.GaussianBlur(radius=3))

        x = anchor_x + (planter_width - cw) // 2
        y = anchor_y - ch // 3
        frontage.paste(contact, (x, y), contact)
        return frontage

    @staticmethod
    def _paste_planter(frontage: Image.Image, planter: Image.Image, anchor_x: int, anchor_y: int) -> Image.Image:
        """
        Paste the planter so its bottom edge sits exactly on the ground line.
        anchor_y is the ground line; the planter base touches it.
        """
        pw, ph = planter.size
        x = anchor_x
        y = anchor_y - ph  # bottom of planter == anchor_y
        frontage.paste(planter, (x, y), planter)
        return frontage
