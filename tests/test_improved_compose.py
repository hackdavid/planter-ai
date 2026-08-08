"""Standalone test of improved compositing with rembg + CV ground detection."""

import json
import logging
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageFilter, ImageDraw
from rembg import remove

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

FRONTAGE_PATH = Path("planter_app/data/images/ChIJR6kvFRELdkgR7deUVt8nLws/streetview_primary_299.jpg")
PLANTER_PATH = Path("sample_plants/plant1.png")
OUTPUT_DIR = Path("test_composite_output")


def remove_background_rembg(img: Image.Image) -> Image.Image:
    """Use rembg to strip background, return RGBA."""
    logger.info("Removing background with rembg...")
    result = remove(img)
    return result.convert("RGBA")


def find_ground_line(frontage_np: np.ndarray) -> int:
    """Detect strongest horizontal line in the BOTTOM zone as ground contact."""
    h, w = frontage_np.shape[:2]
    gray = cv2.cvtColor(frontage_np, cv2.COLOR_RGB2GRAY)

    # Focus ONLY on bottom 25% of image (75% to 100%) — that's where ground lives
    bottom_start = int(h * 0.72)
    roi_gray = gray[bottom_start:, :]
    edges = cv2.Canny(roi_gray, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=60, minLineLength=w // 5, maxLineGap=15)

    if lines is None:
        logger.info("No Hough lines found in bottom zone, falling back to bottom 15% anchor")
        return int(h * 0.88)

    # Keep nearly-horizontal lines
    horiz = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        if abs(y2 - y1) < h * 0.03:
            y_global = y1 + bottom_start
            length = abs(x2 - x1)
            # Weight: longer is better, and lower (closer to bottom edge) is better
            closeness_to_bottom = (y_global - bottom_start) / (h - bottom_start)
            score = length * (1 + closeness_to_bottom)
            horiz.append((y_global, score, length))

    if not horiz:
        return int(h * 0.88)

    # Pick highest-scoring line
    horiz.sort(key=lambda x: x[1], reverse=True)
    best_y = horiz[0][0]

    # Sanity check: if it's suspiciously high up (above 85%), force to bottom
    if best_y < h * 0.80:
        logger.info(f"Detected line at y={best_y} too high, forcing to bottom zone")
        best_y = int(h * 0.88)
    else:
        logger.info(f"Detected ground line at y={best_y}")

    return best_y


def find_vanishing_point(frontage_np: np.ndarray) -> tuple[int, int] | None:
    """Estimate vanishing point from vertical building edges."""
    h, w = frontage_np.shape[:2]
    gray = cv2.cvtColor(frontage_np, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=60, minLineLength=h // 5, maxLineGap=10)

    if lines is None:
        return None

    # Collect near-vertical lines
    verts = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        dx, dy = x2 - x1, y2 - y1
        if abs(dx) < w * 0.05 and abs(dy) > h * 0.15:  # vertical-ish
            verts.append((x1, y1, x2, y2))

    if len(verts) < 2:
        return None

    # Find intersections of vertical lines extended upward
    intersections = []
    for i in range(len(verts)):
        for j in range(i + 1, len(verts)):
            x1, y1, x2, y2 = verts[i]
            x3, y3, x4, y4 = verts[j]
            # Line 1: (x1,y1)-(x2,y2), Line 2: (x3,y3)-(x4,y4)
            denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
            if abs(denom) < 1e-6:
                continue
            px = ((x1 * y2 - y1 * x2) * (x3 - x4) - (x1 - x2) * (x3 * y4 - y3 * x4)) / denom
            py = ((x1 * y2 - y1 * x2) * (y3 - y4) - (y1 - y2) * (x3 * y4 - y3 * x4)) / denom
            if 0 <= px <= w and 0 <= py <= h * 0.6:  # vanishing point should be above ground
                intersections.append((int(px), int(py)))

    if not intersections:
        return None

    # Median intersection
    vx = int(np.median([p[0] for p in intersections]))
    vy = int(np.median([p[1] for p in intersections]))
    logger.info(f"Estimated vanishing point at ({vx}, {vy})")
    return vx, vy


def estimate_light_direction(frontage_np: np.ndarray) -> str:
    """Rough guess: brighter side = light source."""
    h, w = frontage_np.shape[:2]
    left_brightness = np.mean(frontage_np[:, : w // 3])
    right_brightness = np.mean(frontage_np[:, 2 * w // 3:])
    if left_brightness > right_brightness * 1.1:
        return "left"
    elif right_brightness > left_brightness * 1.1:
        return "right"
    return "top"


def perspective_skew_from_vanishing(
    planter: Image.Image, vanishing_pt: tuple[int, int] | None, anchor_x: int, anchor_y: int, frontage_w: int
) -> Image.Image:
    """Warp planter so its verticals point toward vanishing point."""
    w, h = planter.size
    if vanishing_pt is None:
        # Fallback: simple trapezoid
        return _simple_trapezoid(planter)

    vx, vy = vanishing_pt
    # Compute how much to shrink top based on vanishing point distance
    # The further the vanishing point, the less perspective
    dist_to_vp = abs(anchor_y - vy)
    if dist_to_vp < 1:
        dist_to_vp = h * 3

    # Shrink top edge proportionally to distance
    shrink = int(w * 0.12 * (h / dist_to_vp))
    shrink = max(2, min(shrink, w // 4))

    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([
        [shrink, 0],
        [w - shrink, 0],
        [w, h],
        [0, h],
    ])
    matrix = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(np.array(planter), matrix, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    return Image.fromarray(warped, "RGBA")


def _simple_trapezoid(planter: Image.Image) -> Image.Image:
    w, h = planter.size
    shrink = int(w * 0.12)
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[shrink, 0], [w - shrink, 0], [w, h], [0, h]])
    matrix = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(np.array(planter), matrix, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    return Image.fromarray(warped, "RGBA")


def build_directional_shadow(size: tuple[int, int], light_dir: str, vanishing_pt: tuple[int, int] | None) -> Image.Image:
    """Create a shadow that stretches away from the light source."""
    w, h = size
    shadow_w = int(w * 1.0)
    shadow_h = int(h * 0.25)

    canvas = Image.new("RGBA", (shadow_w, shadow_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # Ellipse base
    draw.ellipse([0, 0, shadow_w, shadow_h], fill=(0, 0, 0, 90))

    # Stretch shadow opposite to light
    if light_dir == "left":
        canvas = canvas.transform((shadow_w, shadow_h), Image.Transform.AFFINE, (1, 0.3, 0, 0, 1, 0))
    elif light_dir == "right":
        canvas = canvas.transform((shadow_w, shadow_h), Image.Transform.AFFINE, (1, -0.3, 0, 0, 1, 0))

    blurred = canvas.filter(ImageFilter.GaussianBlur(radius=max(shadow_w, shadow_h) // 5))
    return blurred


def find_safe_positions(frontage_np: np.ndarray, ground_y: int, planter_width: int) -> list[tuple[int, int, str]]:
    """Return (x, y, label) positions that avoid center-blocking."""
    h, w = frontage_np.shape[:2]
    # Use left and right of entrance, skip dead center
    positions = []

    # Left position: ~25% from left
    x_left = int(w * 0.25) - planter_width // 2
    positions.append((max(0, x_left), ground_y, "left"))

    # Right position: ~75% from left
    x_right = int(w * 0.75) - planter_width // 2
    positions.append((min(w - planter_width, x_right), ground_y, "right"))

    # Near-center but offset: ~40% or ~60%
    x_offset = int(w * 0.40) - planter_width // 2
    positions.append((max(0, x_offset), ground_y, "near_center"))

    return positions


def composite_single(
    frontage_path: Path,
    planter_path: Path,
    output_dir: Path,
) -> list[Path]:
    """Run improved compositing and return output paths."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load images
    frontage = Image.open(frontage_path).convert("RGBA")
    planter_raw = Image.open(planter_path)
    frontage_np = np.array(frontage.convert("RGB"))
    fw, fh = frontage.size

    # 1. Extract planter with rembg
    planter = remove(planter_raw)
    planter = planter.convert("RGBA")

    # 2. Resize planter to ~15% of frontage width
    target_w = int(fw * 0.15)
    ratio = target_w / planter.width
    target_h = int(planter.height * ratio)
    planter = planter.resize((target_w, target_h), Image.Resampling.LANCZOS)

    # 3. Find ground line and vanishing point
    ground_y = find_ground_line(frontage_np)
    vanishing_pt = find_vanishing_point(frontage_np)
    light_dir = estimate_light_direction(frontage_np)
    logger.info(f"Light direction estimate: {light_dir}")

    # 4. Find safe positions
    positions = find_safe_positions(frontage_np, ground_y, planter.width)

    outputs = []
    for x, y, label in positions:
        # 5. Perspective skew
        skewed = perspective_skew_from_vanishing(planter, vanishing_pt, x, y, fw)

        # 6. Shadow
        shadow = build_directional_shadow(skewed.size, light_dir, vanishing_pt)

        # 7. Composite
        composed = frontage.copy()
        # Paste shadow slightly below anchor
        sx = x + (skewed.width - shadow.width) // 2
        sy = y + 3
        composed.paste(shadow, (sx, sy), shadow)

        # Paste planter so its bottom edge sits at anchor
        py = y - skewed.height
        composed.paste(skewed, (x, py), skewed)

        # Save
        out_path = output_dir / f"planter_{label}.jpg"
        composed.convert("RGB").save(out_path, quality=92)
        outputs.append(out_path)
        logger.info(f"Saved: {out_path.name}")

    # Save debug overlay showing ground line and vanishing point
    debug = frontage_np.copy()
    cv2.line(debug, (0, ground_y), (fw, ground_y), (0, 255, 0), 2)
    if vanishing_pt:
        cv2.circle(debug, vanishing_pt, 8, (255, 0, 0), -1)
        for x, y, label in positions:
            cv2.rectangle(debug, (x, y - target_h), (x + target_w, y), (0, 0, 255), 2)
    debug_img = Image.fromarray(debug)
    debug_path = output_dir / "debug_overlay.jpg"
    debug_img.save(debug_path, quality=92)
    logger.info(f"Saved debug overlay: {debug_path.name}")

    return outputs


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("IMPROVED COMPOSITING TEST")
    logger.info(f"Frontage: {FRONTAGE_PATH}")
    logger.info(f"Planter:  {PLANTER_PATH}")
    logger.info("=" * 60)
    paths = composite_single(FRONTAGE_PATH, PLANTER_PATH, OUTPUT_DIR)
    logger.info(f"\nDone. Outputs in {OUTPUT_DIR}:")
    for p in paths:
        logger.info(f"  - {p}")
