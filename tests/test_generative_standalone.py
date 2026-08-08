"""Standalone test of the generative compositing service.

Run this after adding REPLICATE_API_TOKEN to .env to verify the
Replicate FLUX integration works before running the full pipeline.
"""

import os
from pathlib import Path

from planter_app.config import Settings
from planter_app.services.generative_compositing_service import GenerativeCompositingService

FRONTAGE_PATH = Path("planter_app/data/images/ChIJR6kvFRELdkgR7deUVt8nLws/streetview_primary_299.jpg")
PLANTER_PATH = Path("sample_plants/plant1.png")
OUTPUT_DIR = Path("test_generative_output")


def main():
    settings = Settings.from_env()
    if not settings.replicate_api_token:
        print("ERROR: REPLICATE_API_TOKEN is not set in .env")
        print("Sign up at https://replicate.com/account/api-tokens and add your token.")
        return

    print(f"Replicate token found (model={settings.replicate_model})")
    print(f"Frontage: {FRONTAGE_PATH}")
    print(f"Planter:  {PLANTER_PATH}")
    print("-" * 50)

    service = GenerativeCompositingService(
        api_token=settings.replicate_api_token,
        model=settings.replicate_model,
        output_dir=OUTPUT_DIR,
    )

    results = service.compose(
        venue_id="test_venue",
        frontage_path=FRONTAGE_PATH,
        planter_path=PLANTER_PATH,
        positions=["left", "center", "right"],
    )

    print("\nDone! Outputs:")
    for r in results:
        print(f"  {r.position}: {r.path}")


if __name__ == "__main__":
    main()
