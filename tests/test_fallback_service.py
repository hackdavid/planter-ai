"""Quick test of the unified FallbackImageService.

Demonstrates the single-call API: fetch_images() does BP + website + filtering + union.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from planter_app.config import Settings
from planter_app.services import FallbackImageService

load_dotenv()


def main():
    settings = Settings.from_env()
    service = FallbackImageService(api_key=settings.google_places_api_key)

    # 5 test venues: 2 BP-rejected + 3 BP-success
    test_ids = [
        ("ChIJK8Bb9WcLdkgRrUZC97T0XWQ", "Deer Cafe"),
        ("ChIJA3NOoqsHdkgReHOikJIlCXg", "Chaachi's"),
        ("ChIJAdrq478LdkgRbIQdZ-jtDAs", "Fortunella"),
        ("ChIJJ3AGi7YLdkgRYrRCCnTmMlk", "The French Tarte"),
        ("ChIJu46vWr8LdkgR1bOGMBoMl50", "Surbeanton"),
    ]

    print(f"{'='*70}")
    print("UNIFIED FALLBACK SERVICE — single call per venue")
    print(f"{'='*70}\n")

    for pid, name in test_ids:
        print(f"Venue: {name}")
        result = service.fetch_images(place_id=pid, force_refresh=False)

        src = result.get("sources", {})
        print(f"  BP candidates: {src.get('business_photos_count', 0)}")
        print(f"  Web candidates: {src.get('website_count', 0)}")
        print(f"  Total union: {result['candidate_count']}")
        print(f"  Status: {result['status']}")

        best = result.get("best_candidate")
        if best:
            print(f"  BEST: source={best['source']} | tier2={best['tier2_score']}")
        else:
            print(f"  BEST: NONE")

        wu = result.get("websiteUri") or "N/A"
        print(f"  Website: {wu[:60]}...")
        print()

    # Show cache path
    cache = Path(__file__).parent / "planter_app" / "data" / "fallback_images"
    print(f"Cache stored at: {cache}")
    print(f"Venues cached: {len(list(cache.glob('*/metadata.json')))}")


if __name__ == "__main__":
    main()
