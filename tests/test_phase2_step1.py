"""Test runner for Phase 2 Step 1: Image Acquisition.

Runs twice:
  1. Cold cache — downloads Street View images via API
  2. Warm cache — reads images from local disk (0 API calls)
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from planter_app.config import Settings
from planter_app.utils.cache_db import CacheDB
from planter_app.services import VenueDiscoveryService, ImageAcquisitionService

load_dotenv()


def main():
    # Use higher max_api_calls for this test so Phase 1 can complete both
    # places search and Street View scoring for London
    settings = Settings(
        google_places_api_key=os.environ["GOOGLE_PLACES_API_KEY"],
        cache_db_path=Path(os.getenv("CACHE_DB_PATH", "./planter_app/data/venue_cache.db")),
        cache_ttl_days=int(os.getenv("CACHE_TTL_DAYS", "7")),
        default_quantity_target=50,
        default_grid_radius_meters=int(os.getenv("DEFAULT_GRID_RADIUS_METERS", "2000")),
        street_view_search_radius_meters=int(os.getenv("STREET_VIEW_SEARCH_RADIUS_METERS", "30")),
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "30")),
        max_api_calls=250,
        rate_limit_delay_seconds=0.3,
    )
    db = CacheDB(settings.cache_db_path)

    # ------------------------------------------------------------------
    # Phase 1: ensure we have candidates (cached or fresh)
    # ------------------------------------------------------------------
    discovery = VenueDiscoveryService(settings=settings, db=db)
    candidates = discovery.discover(
        query="London, UK",
        categories=["cafe", "restaurant"],
        quantity=50,
        force_refresh=False,
    )
    print(f"\nPhase 1: {len(candidates)} candidates available")

    # Get the scan_session_id from the most recent London session
    with db._connection() as conn:
        row = conn.execute(
            "SELECT id FROM scan_sessions WHERE query = ? AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
            ("London, UK",),
        ).fetchone()
        scan_session_id = row["id"] if row else None

    if not scan_session_id:
        raise RuntimeError("No completed scan session found for London")

    print(f"Using scan_session_id={scan_session_id}")

    # ------------------------------------------------------------------
    # Phase 2 Step 1: Image Acquisition — COLD CACHE
    # ------------------------------------------------------------------
    acquisition = ImageAcquisitionService(settings=settings, db=db)

    print("\n" + "=" * 60)
    print("RUN 1: COLD CACHE — downloading images from API")
    print("=" * 60)
    start = time.time()
    results_cold = acquisition.acquire(scan_session_id=scan_session_id, max_venues=20, force_refresh=False)
    elapsed_cold = time.time() - start

    print(f"\nCold cache: {len(results_cold)} venues processed in {elapsed_cold:.2f}s")
    for r in results_cold[:5]:
        print(f"  {r['name']} | primary={r['primary_image']} | validation={r['validation_image']} | cached={r.get('cached', False)}")

    # ------------------------------------------------------------------
    # Phase 2 Step 1: Image Acquisition — WARM CACHE
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("RUN 2: WARM CACHE — reading images from disk")
    print("=" * 60)
    start = time.time()
    results_warm = acquisition.acquire(scan_session_id=scan_session_id, max_venues=20, force_refresh=False)
    elapsed_warm = time.time() - start

    print(f"\nWarm cache: {len(results_warm)} venues processed in {elapsed_warm:.2f}s")
    for r in results_warm[:5]:
        print(f"  {r['name']} | primary={r['primary_image']} | validation={r['validation_image']} | cached={r.get('cached', False)}")

    # Summary
    api_calls_cold = getattr(acquisition, "_api_calls", 0)
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Cold cache time: {elapsed_cold:.2f}s")
    print(f"Warm cache time: {elapsed_warm:.2f}s")
    print(f"Images cached on disk: {len(results_warm)}")
    print(f"All venues returned from disk on second run: {all(r.get('cached') for r in results_warm)}")


if __name__ == "__main__":
    main()
