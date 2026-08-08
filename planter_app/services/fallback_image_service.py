"""Unified fallback service: Business Photos + Website Crawler + filtering + union."""

import json
import logging
from pathlib import Path
from typing import Optional

from planter_app.services.business_photos_service import BusinessPhotosService
from planter_app.services.website_crawler_service import WebsiteCrawlerService

logger = logging.getLogger(__name__)


class FallbackImageService:
    """
    One-call fallback service that:
      1. Fetches Google Business Photos
      2. Crawls the venue website (if URL known)
      3. Applies Tier 1 + Tier 2 filtering on both sources
      4. Unions and ranks all candidates
      5. Stores metadata locally
      6. Returns the best candidate

    Usage:
        service = FallbackImageService(api_key=settings.google_places_api_key)
        result = service.fetch_images(
            place_id="ChIJ...",
            website_url="https://example.com",  # optional
            force_refresh=False,
        )
        # result["best_candidate"] has the top-ranked image from any source
    """

    def __init__(
        self,
        api_key: str,
        cache_dir: Optional[Path] = None,
    ):
        self.api_key = api_key
        self.cache_dir = cache_dir or (
            Path(__file__).parent.parent / "data" / "fallback_images"
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._bp = BusinessPhotosService(api_key=api_key)
        self._crawler = WebsiteCrawlerService()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_images(
        self,
        place_id: str,
        website_url: Optional[str] = None,
        force_refresh: bool = False,
        max_bp_photos: int = 3,
        max_web_pages: int = 3,
        max_web_images_per_page: int = 20,
    ) -> dict:
        """
        Fetch and rank fallback images from all available sources.

        Args:
            place_id: Google Place ID.
            website_url: Venue website URL (optional; if None, only Business Photos are tried).
            force_refresh: Bypass local cache and re-call all APIs.
            max_bp_photos: Max Business Photos to download.
            max_web_pages: Max website pages to crawl.
            max_web_images_per_page: Max images to extract per crawled page.

        Returns:
            Dict with status, candidates (union), best_candidate, websiteUri.
        """
        logger.info("[FALLBACK] place_id=%s | website=%s | force=%s", place_id, website_url, force_refresh)
        venue_dir = self.cache_dir / place_id
        venue_dir.mkdir(parents=True, exist_ok=True)
        meta_path = venue_dir / "metadata.json"

        # 1. Cache hit
        if not force_refresh and meta_path.exists():
            with open(meta_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            logger.info("[FALLBACK] Cache hit for %s", place_id)
            return cached

        # 2. Source A: Google Business Photos
        bp_result = self._bp.fetch(
            place_id=place_id,
            max_photos_to_download=max_bp_photos,
        )
        discovered_website = bp_result.get("websiteUri")
        # If caller didn't provide a website, use the one from Place Details
        if not website_url and discovered_website:
            website_url = discovered_website

        bp_candidates = []
        if bp_result.get("status") == "success" and bp_result.get("best_candidate"):
            for ph in bp_result.get("all_photos", []):
                bp_candidates.append({
                    "source": "business_photos",
                    "path": ph.get("path"),
                    "tier2_score": ph.get("tier2_score", 0),
                    "tier1_score": ph.get("tier1_score", 0),
                    "aspect_ratio": ph.get("aspect_ratio"),
                })

        # 3. Source B: Website Crawler
        web_candidates = []
        if website_url and not website_url.startswith("https://www.instagram.com"):
            try:
                web_result = self._crawler.crawl(
                    website_url,
                    max_pages=max_web_pages,
                    max_images_per_page=max_web_images_per_page,
                )
                for img in web_result.get("all_ranked", []):
                    web_candidates.append({
                        "source": "website",
                        "path": img.get("url"),  # website images are remote URLs
                        "tier2_score": img.get("tier2_score", 0),
                        "tier1_score": img.get("tier1_score", 0),
                        "aspect_ratio": img.get("aspect_ratio"),
                    })
                logger.info(
                    "[FALLBACK] Website crawl | url=%s | candidates=%s",
                    website_url, len(web_candidates),
                )
            except Exception as exc:
                logger.warning("[FALLBACK] Website crawl failed for %s: %s", place_id, exc)
        else:
            logger.info("[FALLBACK] No website URL available for %s", place_id)

        # 4. Union: combine and rank by tier2_score
        all_candidates = bp_candidates + web_candidates
        all_candidates.sort(key=lambda x: x["tier2_score"], reverse=True)

        best = all_candidates[0] if all_candidates else None
        status = "success" if best else "no_candidates"
        reason = None if best else "Neither Business Photos nor website yielded usable images"

        result = {
            "place_id": place_id,
            "status": status,
            "reason": reason,
            "websiteUri": website_url or discovered_website,
            "candidate_count": len(all_candidates),
            "candidates": all_candidates,
            "best_candidate": best,
            "sources": {
                "business_photos_count": len(bp_candidates),
                "website_count": len(web_candidates),
            },
        }

        # 5. Persist
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        logger.info("[FALLBACK] Saved metadata for %s | candidates=%s", place_id, len(all_candidates))

        return result
