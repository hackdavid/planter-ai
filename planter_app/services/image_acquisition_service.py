"""Phase 2 Step 1: Acquire frontage images from Street View Static API."""

import json
import math
import time
import logging
from pathlib import Path
from typing import Optional

import requests

from planter_app.config import Settings
from planter_app.utils.cache_db import CacheDB

logger = logging.getLogger(__name__)


class ImageAcquisitionService:
    """
    Service class for acquiring real frontage images from Street View Static API.

    For each candidate venue with a nearby Street View panorama:
    1. Compute the bearing from panorama to venue
    2. Request a primary image (heading = bearing)
    3. Request a validation image (heading = bearing ± 20°)
    4. Store both images locally on disk
    5. Cache metadata alongside the images

    If images already exist on disk for a venue, the API is skipped.
    """

    STREET_VIEW_STATIC_URL = "https://maps.googleapis.com/maps/api/streetview"

    def __init__(self, settings: Settings, db: CacheDB):
        self.settings = settings
        self.db = db
        self._session = requests.Session()
        self._api_calls = 0
        self._image_dir = Path(__file__).parent.parent / "data" / "images"
        self._image_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(
        self,
        scan_session_id: int,
        max_venues: int = 20,
        force_refresh: bool = False,
    ) -> list[dict]:
        """
        Fetch Street View images for the top N candidates with street_view_score = 100.

        Args:
            scan_session_id: The Phase 1 session ID to read candidates from.
            max_venues: Maximum number of venues to process (default 20).
            force_refresh: If True, re-download images even if they exist on disk.

        Returns:
            List of dicts containing venue info, image paths, and metadata.
        """
        logger.info("=" * 60)
        logger.info(
            "ACQUIRE START | session_id=%s | max_venues=%s | force=%s",
            scan_session_id, max_venues, force_refresh,
        )
        logger.info("=" * 60)

        candidates = self._fetch_candidates(scan_session_id, max_venues)
        logger.info("[DB] Loaded %s candidates with street_view_score=100", len(candidates))

        results: list[dict] = []
        for c in candidates:
            venue_id = c["place_id"]
            venue_dir = self._image_dir / venue_id
            venue_dir.mkdir(parents=True, exist_ok=True)

            # Check if we already have images cached on disk
            if not force_refresh and self._images_exist(venue_dir):
                logger.info("[CACHE] Images already on disk for %s (%s)", venue_id, c.get("name"))
                result = self._load_existing_result(venue_dir, c)
                results.append(result)
                continue

            # Need to download images
            lat = c.get("lat")
            lng = c.get("lng")
            pano_lat = c.get("panorama_lat")
            pano_lng = c.get("panorama_lng")
            road_dist = c.get("road_proximity_meters")

            if lat is None or lng is None or pano_lat is None or pano_lng is None:
                logger.warning("[SKIP] Missing coordinates for %s", venue_id)
                continue

            bearing = self._compute_bearing(pano_lat, pano_lng, lat, lng)
            primary_heading = round(bearing)
            validation_heading = round((bearing + 20) % 360)

            logger.info(
                "[FETCH] %s | bearing=%.1f° | primary=%s° | validation=%s° | road_dist=%sm",
                c.get("name"), bearing, primary_heading, validation_heading, road_dist,
            )

            # Download primary image
            primary_path = venue_dir / f"streetview_primary_{primary_heading}.jpg"
            primary_ok = self._download_image(
                lat=lat, lng=lng, heading=primary_heading, pitch=0, fov=60, save_path=primary_path
            )

            # Download validation image
            validation_path = venue_dir / f"streetview_validation_{validation_heading}.jpg"
            validation_ok = self._download_image(
                lat=lat, lng=lng, heading=validation_heading, pitch=0, fov=60, save_path=validation_path
            )

            # Write metadata
            meta = {
                "venue_id": venue_id,
                "place_id": venue_id,
                "name": c.get("name"),
                "address": c.get("address"),
                "lat": lat,
                "lng": lng,
                "panorama_lat": pano_lat,
                "panorama_lng": pano_lng,
                "computed_bearing": round(bearing, 2),
                "primary_heading": primary_heading,
                "validation_heading": validation_heading,
                "pitch": 0,
                "fov": 60,
                "image_size": "640x480",
                "road_proximity_meters": road_dist,
                "source": "google_street_view_static",
                "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "primary_image_exists": primary_ok,
                "validation_image_exists": validation_ok,
            }
            meta_path = venue_dir / "metadata.json"
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)

            results.append(
                {
                    "venue_id": venue_id,
                    "name": c.get("name"),
                    "address": c.get("address"),
                    "primary_image": str(primary_path) if primary_ok else None,
                    "validation_image": str(validation_path) if validation_ok else None,
                    "metadata": str(meta_path),
                    "computed_bearing": bearing,
                    "road_proximity_meters": road_dist,
                }
            )

            logger.info(
                "[DONE] %s | primary=%s | validation=%s",
                venue_id, "OK" if primary_ok else "FAIL", "OK" if validation_ok else "FAIL",
            )

        logger.info("[COMPLETE] Processed %s venues | API calls=%s", len(results), self._api_calls)
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_candidates(self, scan_session_id: int, limit: int) -> list[dict]:
        """Load top N candidates with street_view_score=100 from SQLite."""
        with self.db._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM candidate_venues
                WHERE scan_session_id = ? AND is_candidate = 1 AND street_view_score = 100
                ORDER BY road_proximity_meters ASC, user_ratings_total DESC, name ASC
                LIMIT ?
                """,
                (scan_session_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def _images_exist(self, venue_dir: Path) -> bool:
        """Check if both primary and validation images already exist."""
        primary = list(venue_dir.glob("streetview_primary_*.jpg"))
        validation = list(venue_dir.glob("streetview_validation_*.jpg"))
        return len(primary) > 0 and len(validation) > 0

    def _load_existing_result(self, venue_dir: Path, candidate: dict) -> dict:
        """Load cached image paths from disk without calling the API."""
        primary = list(venue_dir.glob("streetview_primary_*.jpg"))
        validation = list(venue_dir.glob("streetview_validation_*.jpg"))
        meta_path = venue_dir / "metadata.json"

        meta = {}
        if meta_path.exists():
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

        return {
            "venue_id": candidate["place_id"],
            "name": candidate.get("name"),
            "address": candidate.get("address"),
            "primary_image": str(primary[0]) if primary else None,
            "validation_image": str(validation[0]) if validation else None,
            "metadata": str(meta_path) if meta_path.exists() else None,
            "computed_bearing": meta.get("computed_bearing"),
            "road_proximity_meters": candidate.get("road_proximity_meters"),
            "cached": True,
        }

    def _download_image(
        self, lat: float, lng: float, heading: int, pitch: int, fov: int, save_path: Path
    ) -> bool:
        """Download a single Street View Static image. Returns True on success."""
        params = {
            "size": "640x480",
            "location": f"{lat},{lng}",
            "heading": heading,
            "pitch": pitch,
            "fov": fov,
            "key": self.settings.google_places_api_key,
        }
        try:
            self._api_calls += 1
            time.sleep(0.3)  # rate limit
            resp = self._session.get(
                self.STREET_VIEW_STATIC_URL, params=params, timeout=self.settings.request_timeout
            )
            if resp.status_code == 200 and len(resp.content) > 1000:
                with open(save_path, "wb") as f:
                    f.write(resp.content)
                return True
            else:
                logger.warning(
                    "[API] Image download failed | status=%s | size=%s | path=%s",
                    resp.status_code, len(resp.content), save_path.name,
                )
                return False
        except Exception as exc:
            logger.error("[API] Exception downloading image: %s", exc)
            return False

    @staticmethod
    def _compute_bearing(
        lat1: float, lng1: float, lat2: float, lng2: float
    ) -> float:
        """Compute initial bearing from (lat1, lng1) to (lat2, lng2) in degrees."""
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        d_lambda = math.radians(lng2 - lng1)

        y = math.sin(d_lambda) * math.cos(phi2)
        x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
            d_lambda
        )
        bearing = math.degrees(math.atan2(y, x))
        return (bearing + 360) % 360
