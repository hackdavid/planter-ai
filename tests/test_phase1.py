"""Quick test runner for Phase 1 venue discovery with logging and caching verification."""

import os
import sys
import time
from pathlib import Path

# Ensure repo root is on path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from planter_app.config import Settings
from planter_app.utils.cache_db import CacheDB
from planter_app.services import VenueDiscoveryService

load_dotenv()

# Use London as requested; max_api_calls safety cap limits spend
TEST_QUERY = "London, UK"
TEST_CATEGORIES = ["cafe", "restaurant"]
TEST_QUANTITY = 50


def run_once(label: str, force_refresh: bool = False):
    print(f"\n{'='*60}")
    print(f"RUN: {label}")
    print(f"{'='*60}\n")

    # Override settings for safety during testing
    settings = Settings(
        google_places_api_key=os.environ["GOOGLE_PLACES_API_KEY"],
        cache_db_path=Path(os.getenv("CACHE_DB_PATH", "./planter_app/data/venue_cache.db")),
        cache_ttl_days=int(os.getenv("CACHE_TTL_DAYS", "7")),
        default_quantity_target=TEST_QUANTITY,
        default_grid_radius_meters=int(os.getenv("DEFAULT_GRID_RADIUS_METERS", "2000")),
        street_view_search_radius_meters=int(os.getenv("STREET_VIEW_SEARCH_RADIUS_METERS", "30")),
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "30")),
        max_api_calls=300,          # INCREASED for full Street View scoring
        rate_limit_delay_seconds=0.3,
    )

    db = CacheDB(settings.cache_db_path)
    service = VenueDiscoveryService(settings=settings, db=db)

    start = time.time()
    candidates = service.discover(
        query=TEST_QUERY,
        categories=TEST_CATEGORIES,
        quantity=TEST_QUANTITY,
        force_refresh=force_refresh,
    )
    elapsed = time.time() - start

    print(f"\n{'='*60}")
    print(f"RESULT: {label}")
    print(f"{'='*60}")
    print(f"Candidates returned: {len(candidates)}")
    print(f"Time elapsed: {elapsed:.2f}s")

    for i, c in enumerate(candidates[:15], 1):
        print(f"  {i}. {c['name']} | {c['address']} | SV={c['street_view_score']} | reviews={c['user_ratings_total']} | types={c['types']}")

    if candidates:
        sv_scores = [c.get("street_view_score", 0) for c in candidates]
        print(f"\nStreet View score distribution: 100={sv_scores.count(100)}, 50={sv_scores.count(50)}, 0={sv_scores.count(0)}")

    return candidates


if __name__ == "__main__":
    # Run 1: cold cache (should hit API)
    run_once("COLD CACHE — first call (API requests expected)", force_refresh=False)

    # Run 2: warm cache (should read SQLite, zero API calls)
    run_once("WARM CACHE — second call (cache hit expected)", force_refresh=False)
