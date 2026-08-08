"""Generative compositing service using Replicate FLUX Kontext Pro for scene editing."""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# Model choices available on the user's Replicate free tier
FLUX_KONTEXT_PRO = "black-forest-labs/flux-kontext-pro"
FLUX_1_1_PRO = "black-forest-labs/flux-1.1-pro"
FLUX_DEV = "black-forest-labs/flux-dev"
IMAGEN_4 = "google/imagen-4"
IDEOGRAM_V3 = "ideogram-ai/ideogram-v3-turbo"

DEFAULT_MODEL = FLUX_KONTEXT_PRO

# Prompts tuned for spatially-grounded product insertion
DEFAULT_PROMPT_TEMPLATE = (
    "Add a photorealistic outdoor potted plant with {description} "
    "placed on the ground {position} the storefront entrance. "
    "The plant sits naturally on the sidewalk with correct scale, "
    "soft shadow on the pavement, and daylight matching the scene. "
    "Keep the building facade, signage, windows, street, and all existing "
    "objects completely unchanged. Do not add or remove anything else."
)

DEFAULT_NEGATIVE_PROMPT = (
    "multiple plants, floating object, oversized plant, tiny plant, "
    "cartoon, illustration, painting, watermark, text, logo, distorted, blurry"
)

PLANTER_DESCRIPTIONS = {
    "plant1.png": "lush green leaves in a smooth light-mint ceramic pot",
    "plant2.png": "compact green foliage in a modern white cylindrical planter",
    "plant3.jpg": "tall snake plant in a minimalist white pot with wooden stand",
}


@dataclass(frozen=True)
class CompositeResult:
    """Single composite output."""

    position: str
    path: Path
    scale_ratio: float
    anchor_x: int
    anchor_y: int


class GenerativeCompositingService:
    """
    Uses Replicate FLUX Kontext Pro to edit an existing frontage photo,
    inserting the client's planter via natural-language spatial instruction.

    Unlike CV-based cut-and-paste, this asks the generative model to
    natively render the planter into the scene with correct lighting,
    shadows, and scale — no mask or alpha channel required.
    """

    def __init__(
        self,
        api_token: str,
        output_dir: Path | None = None,
        model: str = DEFAULT_MODEL,
        prompt_template: str = DEFAULT_PROMPT_TEMPLATE,
        negative_prompt: str = DEFAULT_NEGATIVE_PROMPT,
    ):
        self.api_token = api_token
        self.model = model
        self.prompt_template = prompt_template
        self.negative_prompt = negative_prompt
        self.output_dir = output_dir or (Path(__file__).parent.parent / "data" / "composites_generative")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        try:
            import replicate
            self._client = replicate.Client(api_token=api_token)
        except ImportError as exc:
            raise ImportError("replicate package is required. Install: pip install replicate") from exc

        # Global rate-limit tracking (free tier = ~6 req/min)
        self._last_api_call: float = 0.0
        self._rate_limit_seconds: float = 15.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compose(
        self,
        venue_id: str,
        frontage_path: Path,
        planter_path: Path,
        positions: list[str] | None = None,
    ) -> list[CompositeResult]:
        """
        Edit the frontage image to insert the planter at requested positions.

        Args:
            venue_id: Place ID of the venue.
            frontage_path: Path to the validated frontage JPG.
            planter_path: Path to the planter product image (used for description only).
            positions: List of positions (default: left, center, right).

        Returns:
            List of CompositeResult objects.
        """
        positions = positions or ["left", "center", "right"]
        frontage = Image.open(frontage_path).convert("RGB")
        fw, fh = frontage.size

        # Derive a product description from the filename
        planter_name = Path(planter_path).name
        description = PLANTER_DESCRIPTIONS.get(planter_name, "a potted plant")

        # Venue output directory
        venue_dir = self.output_dir / venue_id
        venue_dir.mkdir(parents=True, exist_ok=True)

        results: list[CompositeResult] = []

        for pos in positions:
            # Calculate anchor point (same logic as CV compositor for consistency)
            anchor_x, anchor_y = self._calculate_anchor(fw, fh, pos)

            # Build the spatial editing prompt
            position_phrase = self._position_to_phrase(pos)
            prompt = self.prompt_template.format(
                description=description,
                position=position_phrase,
            )

            # Call Replicate
            output_path = venue_dir / f"planter_{pos}.jpg"
            try:
                self._edit_image(frontage_path, prompt, output_path)
            except Exception as exc:
                logger.error(f"[GENERATIVE] Editing failed for {venue_id} {pos}: {exc}")
                raise

            results.append(
                CompositeResult(
                    position=pos,
                    path=output_path,
                    scale_ratio=round(0.15, 3),
                    anchor_x=anchor_x,
                    anchor_y=anchor_y,
                )
            )
            logger.info("[GENERATIVE] %s | %s | saved=%s", venue_id, pos, output_path.name)

        # Save metadata
        meta = {
            "venue_id": venue_id,
            "frontage_source": str(frontage_path),
            "planter_source": str(planter_path),
            "model": self.model,
            "prompt_template": self.prompt_template,
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
    def _calculate_anchor(fw: int, fh: int, position: str) -> tuple[int, int]:
        """Anchor point for consistency with CV compositor."""
        ground_y = int(fh * 0.85)
        if position == "left":
            x = int(fw * 0.22)
        elif position == "center":
            x = int(fw * 0.50)
        elif position == "right":
            x = int(fw * 0.78)
        else:
            x = int(fw * 0.50)
        return x, ground_y

    @staticmethod
    def _position_to_phrase(position: str) -> str:
        """Convert position label to a natural spatial phrase for the prompt."""
        mapping = {
            "left": "to the left of",
            "center": "directly in front of",
            "right": "to the right of",
        }
        return mapping.get(position, "near")

    def _edit_image(self, image_path: Path, prompt: str, output_path: Path) -> None:
        """Call Replicate model and save the edited image."""
        # Global rate-limit guard (free tier = ~6 req/min)
        elapsed = time.time() - self._last_api_call
        if elapsed < self._rate_limit_seconds:
            sleep_for = self._rate_limit_seconds - elapsed
            logger.info("[REPLICATE] Rate-limit sleep %.1fs before next call...", sleep_for)
            time.sleep(sleep_for)

        logger.info(
            "[REPLICATE] Editing | model=%s | image=%s | prompt=%s",
            self.model,
            image_path.name,
            prompt[:80],
        )

        with open(image_path, "rb") as img_file:
            output = self._client.run(
                self.model,
                input={
                    "input_image": img_file,
                    "prompt": prompt,
                    "aspect_ratio": "match_input_image",
                    "output_format": "jpg",
                    "output_quality": 92,
                    "safety_tolerance": 2,
                },
            )

        # Replicate returns a file-like object, URL string, or list
        if hasattr(output, "read"):
            result_bytes = output.read()
            output_path.write_bytes(result_bytes)
        elif isinstance(output, str):
            import requests
            r = requests.get(output)
            r.raise_for_status()
            output_path.write_bytes(r.content)
        elif isinstance(output, list):
            first = output[0]
            if hasattr(first, "read"):
                output_path.write_bytes(first.read())
            elif isinstance(first, str):
                import requests
                r = requests.get(first)
                r.raise_for_status()
                output_path.write_bytes(r.content)
            else:
                raise RuntimeError(f"Unexpected Replicate output type: {type(first)}")
        else:
            raise RuntimeError(f"Unexpected Replicate output type: {type(output)}")

        self._last_api_call = time.time()
        logger.info("[REPLICATE] Saved edited image to %s", output_path)
