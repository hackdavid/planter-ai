"""Phase 1 venue discovery service: find and filter candidate venues for planter prospecting."""

import json
import math
import re
import time
import logging
import requests
from typing import Optional
from datetime import datetime
from pathlib import Path

from planter_app.config import Settings
from planter_app.utils.cache_db import CacheDB

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class VenueDiscoveryService:
    """
    Service class for discovering independent, street-facing venues in a given city.

    Usage:
        settings = Settings.from_env()
        db = CacheDB(settings.cache_db_path)
        service = VenueDiscoveryService(settings=settings, db=db)
        candidates = service.discover(query="London", categories=["cafe", "restaurant"])
    """

    # Whitelisted Google Places types for planter prospecting
    CATEGORY_WHITELIST = {
        "cafe",
        "restaurant",
        "meal_takeaway",
        "bakery",
        "beauty_salon",
        "hair_care",
        "spa",
        "bar",
        "meal_delivery",
    }

    # Non-street-facing location indicators (address substrings)
    NON_STREET_INDICATORS = {
        "mall", "arcade", "shopping centre", "terminal", "airport",
        "station", "kiosk", "stadium", "arena", "hospital",
        "university", "basement", "underground", "mezzanine",
        "level ", "floor ", "unit ", "wing ", "block ",
    }

    # Name heuristics for non-street-facing or irrelevant venues
    NAME_DROP_PATTERNS = re.compile(
        r"\b(hotel|inn|resort|hostel|bed and breakfast|guest house|b&b)\b",
        re.IGNORECASE,
    )

    # Google Places API (New) endpoint
    PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
    GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
    STREET_VIEW_METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"

    def __init__(self, settings: Settings, db: CacheDB):
        self.settings = settings
        self.db = db
        self._blacklist = self._load_chain_blacklist()
        self._session = requests.Session()
        self._api_calls = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover(
        self,
        query: str,
        categories: Optional[list[str]] = None,
        quantity: Optional[int] = None,
        force_refresh: bool = False,
    ) -> list[dict]:
        """
        Main entry point. Returns a list of candidate venue dicts.

        Args:
            query: City name (e.g., "London").
            categories: Google Places types to include. Defaults to full whitelist.
            quantity: Target number of raw venues before filtering.
            force_refresh: If True, bypass the SQLite cache.
        """
        categories = categories or list(self.CATEGORY_WHITELIST)
        quantity = quantity or self.settings.default_quantity_target

        logger.info("=" * 60)
        logger.info("DISCOVER START | query=%s | categories=%s | quantity=%s | force=%s",
                    query, categories, quantity, force_refresh)
        logger.info("Safety limit: max_api_calls=%s | rate_delay=%ss",
                    self.settings.max_api_calls, self.settings.rate_limit_delay_seconds)
        logger.info("=" * 60)

        cache_hash = self.db.compute_cache_hash(query, categories, quantity)
        logger.info("[CACHE] computed hash=%s", cache_hash)

        # 1. Cache lookup
        if not force_refresh:
            cached_session = self.db.get_session(cache_hash)
            if cached_session and cached_session["status"] == "completed":
                logger.info("[CACHE] HIT | session_id=%s | raw_found=%s | filtered=%s",
                            cached_session["id"], cached_session["raw_found"], cached_session["filtered_count"])
                candidates = self.db.get_candidates(
                    cached_session["id"], limit=quantity
                )
                if candidates:
                    logger.info("[CACHE] Returning %s candidates from cache (0 API calls)", len(candidates))
                    return candidates
                logger.warning("[CACHE] Session exists but no candidates found; re-running.")
            else:
                logger.info("[CACHE] MISS or expired")

        # 2. Geocode city
        logger.info("[API #%s] Geocoding city: %s", self._api_calls + 1, query)
        bounds, center = self._geocode_city(query)
        if bounds is None:
            raise ValueError(f"Could not geocode city: {query}")
        logger.info("[GEO] bounds acquired | center=%s | ne=%s | sw=%s",
                    center, bounds.get("northeast"), bounds.get("southwest"))

        # 3. Create / reset scan session
        scan_session_id = self.db.create_session(cache_hash, query, categories, quantity)
        logger.info("[DB] scan_session_id=%s created/reset", scan_session_id)

        # 4. Generate search grid
        grid_points = self._generate_grid(bounds, radius_m=self.settings.default_grid_radius_meters)
        logger.info("[GRID] Generated %s search points", len(grid_points))

        # 5. Fetch raw venues from Google Places
        raw_venues = self._fetch_raw_venues(grid_points, categories)
        logger.info("[FETCH] Retrieved %s unique raw venues from API", len(raw_venues))
        self.db.store_raw_venues(scan_session_id, raw_venues)
        logger.info("[DB] Stored %s raw venues", len(raw_venues))

        # 6. Apply deterministic filters
        candidates, dropped = self._apply_filters(raw_venues)
        logger.info("[FILTER] candidates=%s | dropped=%s", len(candidates), len(dropped))
        for reason, count in self._summarize_drops(dropped).items():
            logger.info("[FILTER] drop_reason=%s | count=%s", reason, count)

        # 7. Street View metadata scoring (cheap ranking signal)
        logger.info("[SV] Scoring %s candidates for Street View coverage...", len(candidates))
        candidates = self._score_street_view(candidates)
        sv_summary = self._summarize_sv_scores(candidates)
        for score, count in sv_summary.items():
            logger.info("[SV] score=%s | count=%s", score, count)

        # 8. Persist and update session
        all_records = candidates + dropped
        self.db.store_candidates(scan_session_id, all_records)
        self.db.update_session_counts(
            scan_session_id,
            raw_found=len(raw_venues),
            filtered_count=len(candidates),
            status="completed",
        )
        logger.info("[DB] Finalized session | raw=%s | filtered=%s | total_api_calls=%s",
                    len(raw_venues), len(candidates), self._api_calls)

        # 9. Return top N
        result = candidates[:quantity]
        logger.info("[DONE] Returning %s candidates", len(result))
        return result

    # ------------------------------------------------------------------
    # Step 2: Geocoding
    # ------------------------------------------------------------------

    def _geocode_city(self, query: str) -> tuple[Optional[dict], Optional[tuple[float, float]]]:
        """Return (bounds dict, center tuple) or (None, None) on failure."""
        self._bump_api_call()
        params = {"address": query, "key": self.settings.google_places_api_key}
        self._rate_limit()
        resp = self._session.get(
            self.GEOCODE_URL, params=params, timeout=self.settings.request_timeout
        )
        resp.raise_for_status()
        data = resp.json()

        if not data.get("results"):
            return None, None

        result = data["results"][0]
        bounds = result.get("geometry", {}).get("bounds") or result.get("geometry", {}).get("viewport")
        center = (
            result["geometry"]["location"]["lat"],
            result["geometry"]["location"]["lng"],
        )
        return bounds, center

    # ------------------------------------------------------------------
    # Step 4: Grid Generation
    # ------------------------------------------------------------------

    def _generate_grid(
        self, bounds: dict, radius_m: int
    ) -> list[tuple[float, float]]:
        """
        Generate lat/lng search points covering the bounding box with ~20% overlap.
        """
        ne = bounds["northeast"]
        sw = bounds["southwest"]

        # Approximate degrees per meter
        lat_deg_per_m = 1.0 / 111_000.0
        lng_deg_per_m = 1.0 / (111_000.0 * math.cos(math.radians((ne["lat"] + sw["lat"]) / 2)))

        step_m = int(radius_m * 1.6)  # 20% overlap → 1.6× radius step
        d_lat = step_m * lat_deg_per_m
        d_lng = step_m * lng_deg_per_m

        points = []
        lat = sw["lat"] + d_lat / 2
        while lat < ne["lat"]:
            lng = sw["lng"] + d_lng / 2
            while lng < ne["lng"]:
                points.append((lat, lng))
                lng += d_lng
            lat += d_lat

        return points

    # ------------------------------------------------------------------
    # Step 5: Fetch Raw Venues
    # ------------------------------------------------------------------

    def _fetch_raw_venues(
        self, grid_points: list[tuple[float, float]], categories: list[str]
    ) -> list[dict]:
        """Query Google Places Nearby Search for all grid points and categories."""
        seen_place_ids = set()
        venues: list[dict] = []

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.settings.google_places_api_key,
            "X-Goog-FieldMask": "places.id,places.displayName,places.location,places.types,places.primaryType,places.formattedAddress,places.businessStatus,places.userRatingCount",
        }

        for cat in categories:
            for lat, lng in grid_points:
                if self._api_calls >= self.settings.max_api_calls:
                    logger.warning("[SAFETY] max_api_calls (%s) reached. Stopping fetch.",
                                   self.settings.max_api_calls)
                    return venues

                page_token: Optional[str] = None
                pages = 0
                while pages < 3:
                    if self._api_calls >= self.settings.max_api_calls:
                        logger.warning("[SAFETY] max_api_calls reached mid-pagination.")
                        return venues

                    body: dict = {
                        "locationRestriction": {
                            "circle": {
                                "center": {"latitude": lat, "longitude": lng},
                                "radius": self.settings.default_grid_radius_meters,
                            }
                        },
                        "includedTypes": [cat],
                        "maxResultCount": 20,
                    }
                    if page_token:
                        body["pageToken"] = page_token

                    self._bump_api_call()
                    self._rate_limit()
                    logger.info("[API #%s] places.searchNearby | cat=%s | grid=%.4f,%.4f | page=%s",
                                self._api_calls, cat, lat, lng, pages)

                    resp = self._session.post(
                        self.PLACES_NEARBY_URL,
                        headers=headers,
                        json=body,
                        timeout=self.settings.request_timeout,
                    )
                    if resp.status_code != 200:
                        logger.error("[API] places.searchNearby FAILED | status=%s | body=%s",
                                     resp.status_code, resp.text[:200])
                        break

                    data = resp.json()
                    places = data.get("places", [])
                    if not places:
                        break

                    for p in places:
                        pid = p.get("id")
                        if not pid or pid in seen_place_ids:
                            continue
                        seen_place_ids.add(pid)

                        loc = p.get("location", {})
                        venues.append(
                            {
                                "place_id": pid,
                                "name": p.get("displayName", {}).get("text"),
                                "lat": loc.get("latitude"),
                                "lng": loc.get("longitude"),
                                "address": p.get("formattedAddress"),
                                "types": p.get("types", []),
                                "primary_type": p.get("primaryType"),
                                "user_ratings_total": p.get("userRatingCount", 0),
                                "business_status": p.get("businessStatus", "OPERATIONAL"),
                                "discovery_source": f"grid:{lat:.4f},{lng:.4f}",
                                "grid_point_lat": lat,
                                "grid_point_lng": lng,
                            }
                        )

                    page_token = data.get("nextPageToken")
                    if not page_token:
                        break
                    pages += 1

        logger.info("[FETCH] Total unique venues after dedup: %s", len(venues))
        return venues

    # ------------------------------------------------------------------
    # Step 6: Deterministic Filters
    # ------------------------------------------------------------------

    def _apply_filters(self, raw_venues: list[dict]) -> tuple[list[dict], list[dict]]:
        """Return (candidates, dropped). Each venue gets filter metadata attached."""
        candidates: list[dict] = []
        dropped: list[dict] = []

        for v in raw_venues:
            reasons: list[str] = []
            drop_reason: Optional[str] = None

            # 1. Primary Type Gate (strictest — the venue must primarily BE a whitelisted business)
            primary = v.get("primary_type")
            if primary and primary not in self.CATEGORY_WHITELIST:
                drop_reason = "primary_type_gate"
                v["is_candidate"] = False
                v["filter_dropped_reason"] = drop_reason
                dropped.append(v)
                continue
            # Fallback if primary_type is missing: accept if ANY type matches
            types = set(v.get("types", []))
            if not (types & self.CATEGORY_WHITELIST):
                drop_reason = "category_gate"
                v["is_candidate"] = False
                v["filter_dropped_reason"] = drop_reason
                dropped.append(v)
                continue
            reasons.append("category")

            # 2. Operational Gate
            if v.get("business_status") != "OPERATIONAL":
                drop_reason = "operational_gate"
                v["is_candidate"] = False
                v["filter_dropped_reason"] = drop_reason
                dropped.append(v)
                continue
            reasons.append("operational")

            # 3. Street-Facing Gate
            addr = (v.get("address") or "").lower()
            if any(ind in addr for ind in self.NON_STREET_INDICATORS):
                drop_reason = "street_facing_gate"
                v["is_candidate"] = False
                v["filter_dropped_reason"] = drop_reason
                dropped.append(v)
                continue
            reasons.append("street_facing")

            # 4. Chain Gate (Hard + Soft)
            name = (v.get("name") or "").lower()
            if self._is_chain(name):
                drop_reason = "chain_gate"
                v["is_candidate"] = False
                v["filter_dropped_reason"] = drop_reason
                dropped.append(v)
                continue
            reasons.append("independent")

            # 5. Review Existence Gate
            rating_count = v.get("user_ratings_total") or 0
            review_floor = self._blacklist.get("review_floor", 3)
            review_cap = self._blacklist.get("review_cap", 1000)
            if rating_count < review_floor or rating_count > review_cap:
                drop_reason = "review_gate"
                v["is_candidate"] = False
                v["filter_dropped_reason"] = drop_reason
                dropped.append(v)
                continue
            reasons.append("review_range")

            # 6. Name Heuristic Gate
            if self.NAME_DROP_PATTERNS.search(v.get("name", "")) and not (types & {"restaurant", "cafe", "bar"}):
                drop_reason = "name_heuristic_gate"
                v["is_candidate"] = False
                v["filter_dropped_reason"] = drop_reason
                dropped.append(v)
                continue
            reasons.append("name_heuristic")

            # Passed all filters
            v["is_candidate"] = True
            v["filter_passed_reasons"] = reasons
            v["filter_dropped_reason"] = None
            candidates.append(v)

        return candidates, dropped

    # ------------------------------------------------------------------
    # Step 7: Street View Metadata Scoring
    # ------------------------------------------------------------------

    def _score_street_view(self, candidates: list[dict]) -> list[dict]:
        """
        Add a 0/50/100 street_view_score to each candidate based on panorama proximity.
        Does NOT drop anyone — ranking only.

        Scoring logic (distance-based — the Metadata API does not expose camera heading):
          100 = panorama exists within 20m (high confidence the entrance is visible)
           50 = panorama exists within the search radius but >20m away
            0 = no panorama found within search radius
        """
        total = len(candidates)
        for idx, c in enumerate(candidates, 1):
            if idx % 50 == 0:
                logger.info("[SV] Scoring progress: %s/%s", idx, total)

            lat = c.get("lat")
            lng = c.get("lng")
            if lat is None or lng is None:
                c["street_view_score"] = 0
                continue

            if self._api_calls >= self.settings.max_api_calls:
                logger.warning(
                    "[SAFETY] max_api_calls reached during SV scoring. "
                    "Remaining candidates get score=0."
                )
                c["street_view_score"] = 0
                continue

            params = {
                "location": f"{lat},{lng}",
                "radius": self.settings.street_view_search_radius_meters,
                "key": self.settings.google_places_api_key,
            }
            try:
                self._bump_api_call()
                self._rate_limit()
                resp = self._session.get(
                    self.STREET_VIEW_METADATA_URL,
                    params=params,
                    timeout=self.settings.request_timeout,
                )
                data = resp.json()
                status = data.get("status", "UNKNOWN_ERROR")

                if status == "OK":
                    pano_lat = data.get("location", {}).get("lat")
                    pano_lng = data.get("location", {}).get("lng")
                    c["panorama_lat"] = pano_lat
                    c["panorama_lng"] = pano_lng
                    if pano_lat is not None and pano_lng is not None:
                        distance = self._haversine(lat, lng, pano_lat, pano_lng)
                        c["road_proximity_meters"] = round(distance, 1)
                        if distance <= 20:
                            c["street_view_score"] = 100
                        else:
                            c["street_view_score"] = 50
                    else:
                        c["street_view_score"] = 100
                        c["road_proximity_meters"] = None
                elif status == "ZERO_RESULTS":
                    c["street_view_score"] = 0
                    c["road_proximity_meters"] = None
                else:
                    c["street_view_score"] = 0
                    c["road_proximity_meters"] = None
            except Exception as exc:
                logger.debug("[SV] Exception for %s: %s", c.get("name"), exc)
                c["street_view_score"] = 0

        return candidates

    @staticmethod
    def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        """Return distance in meters between two lat/lng points."""
        R = 6_371_000  # Earth radius in meters
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        d_phi = math.radians(lat2 - lat1)
        d_lambda = math.radians(lng2 - lng1)

        a = (
            math.sin(d_phi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    # ------------------------------------------------------------------
    # Safety helpers
    # ------------------------------------------------------------------

    def _bump_api_call(self) -> None:
        self._api_calls += 1

    def _rate_limit(self) -> None:
        delay = self.settings.rate_limit_delay_seconds
        if delay > 0:
            time.sleep(delay)

    @staticmethod
    def _summarize_drops(dropped: list[dict]) -> dict[str, int]:
        summary: dict[str, int] = {}
        for d in dropped:
            reason = d.get("filter_dropped_reason", "unknown")
            summary[reason] = summary.get(reason, 0) + 1
        return summary

    @staticmethod
    def _summarize_sv_scores(candidates: list[dict]) -> dict[int, int]:
        summary: dict[int, int] = {}
        for c in candidates:
            score = c.get("street_view_score", 0)
            summary[score] = summary.get(score, 0) + 1
        return summary

    # ------------------------------------------------------------------
    # Other Helpers
    # ------------------------------------------------------------------

    def _load_chain_blacklist(self) -> dict:
        path = self.settings.chain_blacklist_path
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"chains": [], "review_cap": 1000, "review_floor": 3}

    def _is_chain(self, name: str) -> bool:
        """Fuzzy check against chain blacklist."""
        chains = self._blacklist.get("chains", [])
        name_lower = name.lower()
        for chain in chains:
            if chain.lower() in name_lower:
                return True
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
