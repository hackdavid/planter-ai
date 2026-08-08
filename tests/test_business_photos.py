"""Test runner for BusinessPhotosService.

Fetches Google Business Photos + websiteUri for the same 20 candidates
that already have Street View images. Runs twice to verify caching.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from planter_app.config import Settings
from planter_app.utils.cache_db import CacheDB
from planter_app.services import BusinessPhotosService

load_dotenv()


def main():
    settings = Settings.from_env()
    db = CacheDB(settings.cache_db_path)

    # Load the same 20 candidates we used for Street View
    with db._connection() as conn:
        row = conn.execute(
            "SELECT id FROM scan_sessions WHERE query = ? AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
            ("London, UK",),
        ).fetchone()
        scan_session_id = row["id"] if row else None

        candidates = conn.execute(
            """
            SELECT place_id, name, address FROM candidate_venues
            WHERE scan_session_id = ? AND is_candidate = 1 AND street_view_score = 100
            ORDER BY road_proximity_meters ASC, user_ratings_total DESC, name ASC
            LIMIT 20
            """,
            (scan_session_id,),
        ).fetchall()

    print(f"Loaded {len(candidates)} candidates from Phase 1 cache")

    service = BusinessPhotosService(api_key=settings.google_places_api_key)

    # ------------------------------------------------------------------
    # Run 1: Cold cache — hits the live API
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("RUN 1: COLD CACHE — calling Google Business Photos API")
    print(f"{'='*60}")
    start = time.time()
    results = []
    for c in candidates:
        result = service.fetch(place_id=c["place_id"], force_refresh=False)
        result["name"] = c["name"]
        result["address"] = c["address"]
        results.append(result)
    elapsed_cold = time.time() - start

    # ------------------------------------------------------------------
    # Run 2: Warm cache — reads from disk
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("RUN 2: WARM CACHE — reading from local disk")
    print(f"{'='*60}")
    start = time.time()
    results_warm = []
    for c in candidates:
        result = service.fetch(place_id=c["place_id"], force_refresh=False)
        result["name"] = c["name"]
        result["address"] = c["address"]
        results_warm.append(result)
    elapsed_warm = time.time() - start

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    success_count = sum(1 for r in results if r["status"] == "success")
    has_website = sum(1 for r in results if r.get("websiteUri"))
    no_photos = sum(1 for r in results if r["status"] == "no_photos")
    tier1_rejected = sum(1 for r in results if r["status"] == "tier1_rejected")
    api_errors = sum(1 for r in results if r["status"] == "api_error")

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Venues processed: {len(results)}")
    print(f"Business Photos success: {success_count}")
    print(f"No photos available: {no_photos}")
    print(f"Tier 1 rejected (all interior): {tier1_rejected}")
    print(f"API errors: {api_errors}")
    print(f"Venues with website URL: {has_website}")
    print(f"Cold cache time: {elapsed_cold:.2f}s")
    print(f"Warm cache time: {elapsed_warm:.2f}s")

    print(f"\nTop 10 results:")
    for idx, r in enumerate(results[:10], 1):
        best = r.get("best_candidate")
        print(f"  {idx}. {r['name']}")
        wu = r.get('websiteUri') or 'N/A'
        print(f"      status={r['status']} | website={wu[:50]}...")
        if best:
            print(f"      best_photo={best['path']} | tier2={best.get('tier2_score')} | size={best.get('size_bytes')} bytes")
        else:
            print(f"      best_photo=NONE")

    # Save website URIs to a simple lookup file for later use
    website_lookup = {
        r["place_id"]: {
            "name": r["name"],
            "websiteUri": r.get("websiteUri"),
            "business_status": r["status"],
        }
        for r in results
    }
    lookup_path = Path(__file__).parent / "planter_app" / "data" / "website_lookup.json"
    lookup_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lookup_path, "w", encoding="utf-8") as f:
        import json as _json
        _json.dump(website_lookup, f, indent=2)
    print(f"\nWebsite lookup saved to: {lookup_path}")


if __name__ == "__main__":
    main()
