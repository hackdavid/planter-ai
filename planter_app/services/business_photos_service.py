"""Fallback service: Google Business Photos acquisition with live API calls."""

import hashlib
import io
import json
import logging
import requests
from pathlib import Path
from typing import Optional

from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

logger = logging.getLogger(__name__)


class BusinessPhotosService:
    """
    Fetches Google Business Photos via Places API (New) Place Details,
    applies Tier 1/2 filtering, and caches images locally.

    Also retrieves the venue's websiteUri from the same Place Details call
    so the website crawler can use it later if needed.
    """

    PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
    EXTERIOR_KEYWORDS = {
        "exterior", "outside", "front", "street", "storefront",
        "building", "facade", "shop", "venue", "store", "outdoor",
        "entrance", "door", "sidewalk", "pavement", "road",
    }
    INTERIOR_KEYWORDS = {
        "interior", "inside", "menu", "food", "dish", "plate",
        "staff", "team", "chef", "logo", "icon", "banner",
        "event", "party", "wedding", "booking", "table",
        "kitchen", "bar", "drink", "cocktail", "dessert",
    }

    def __init__(self, api_key: str, cache_dir: Optional[Path] = None):
        self.api_key = api_key
        self.session = requests.Session()
        self.cache_dir = cache_dir or (Path(__file__).parent.parent / "data" / "business_photos")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(
        self,
        place_id: str,
        force_refresh: bool = False,
        max_photos_to_download: int = 3,
    ) -> dict:
        """
        Fetch Place Details (photos + websiteUri), download best candidate,
        and cache locally.

        Args:
            place_id: Google Place ID.
            force_refresh: If True, bypass local cache and re-call API.
            max_photos_to_download: Number of Tier-1 survivors to download.

        Returns:
            Dict with status, websiteUri, best_candidate, and all_photos.
        """
        logger.info("[BUSINESS_PHOTOS] place_id=%s | force=%s", place_id, force_refresh)
        venue_dir = self.cache_dir / place_id
        venue_dir.mkdir(parents=True, exist_ok=True)

        meta_path = venue_dir / "metadata.json"

        # Cache hit
        if not force_refresh and meta_path.exists():
            with open(meta_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            logger.info("[BUSINESS_PHOTOS] Cache hit for %s", place_id)
            return cached

        # ------------------------------------------------------------------
        # 1. Call Place Details API
        # ------------------------------------------------------------------
        try:
            resp = self.session.get(
                self.PLACE_DETAILS_URL.format(place_id=place_id),
                headers={
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": "photos,websiteUri",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("[BUSINESS_PHOTOS] Place Details failed for %s: %s", place_id, exc)
            result = {
                "place_id": place_id,
                "status": "api_error",
                "websiteUri": None,
                "reason": str(exc),
                "best_candidate": None,
                "all_photos": [],
            }
            self._save_meta(venue_dir, result)
            return result

        website_uri = data.get("websiteUri")
        raw_photos = data.get("photos", [])
        logger.info("[BUSINESS_PHOTOS] websiteUri=%s | raw_photos=%s", website_uri, len(raw_photos))

        if not raw_photos:
            result = {
                "place_id": place_id,
                "status": "no_photos",
                "websiteUri": website_uri,
                "reason": "Place Details returned zero photos",
                "best_candidate": None,
                "all_photos": [],
            }
            self._save_meta(venue_dir, result)
            return result

        # ------------------------------------------------------------------
        # 2. Tier 1: metadata filtering (no download)
        # ------------------------------------------------------------------
        tier1 = self._tier1_filter(raw_photos)
        logger.info("[BUSINESS_PHOTOS] Tier 1 survivors: %s", len(tier1))

        if not tier1:
            result = {
                "place_id": place_id,
                "status": "tier1_rejected",
                "websiteUri": website_uri,
                "reason": "No photos passed aspect ratio / size / keyword filters",
                "best_candidate": None,
                "all_photos": [],
            }
            self._save_meta(venue_dir, result)
            return result

        # ------------------------------------------------------------------
        # 3. Download top N survivors
        # ------------------------------------------------------------------
        downloaded: list[dict] = []
        for p in tier1[:max_photos_to_download]:
            safe_name = hashlib.md5(p["name"].encode()).hexdigest()[:12]
            photo_path = venue_dir / f"{safe_name}.jpg"
            media_url = (
                f"https://places.googleapis.com/v1/{p['name']}/media"
                f"?maxHeightPx=480&maxWidthPx=640&key={self.api_key}"
            )
            try:
                img_resp = self.session.get(media_url, timeout=30, allow_redirects=True)
                if img_resp.status_code == 200 and len(img_resp.content) > 1000:
                    with open(photo_path, "wb") as f:
                        f.write(img_resp.content)
                    downloaded.append(
                        {
                            "path": str(photo_path),
                            "size_bytes": len(img_resp.content),
                            "name": p["name"],
                            "tier1_score": p["tier1_score"],
                            "aspect_ratio": p.get("aspect_ratio"),
                            "keyword_score": p.get("keyword_score"),
                        }
                    )
                    logger.info("[BUSINESS_PHOTOS] Downloaded %s (%s bytes)", photo_path.name, len(img_resp.content))
                else:
                    logger.warning("[BUSINESS_PHOTOS] Media request failed | status=%s | size=%s", img_resp.status_code, len(img_resp.content))
            except Exception as exc:
                logger.warning("[BUSINESS_PHOTOS] Download failed for %s: %s", p["name"], exc)

        logger.info("[BUSINESS_PHOTOS] Successfully downloaded: %s", len(downloaded))

        # ------------------------------------------------------------------
        # 4. Tier 2: visual scoring on downloaded images
        # ------------------------------------------------------------------
        tier2 = self._tier2_score(downloaded)
        logger.info("[BUSINESS_PHOTOS] Tier 2 ranked: %s", len(tier2))

        best = tier2[0] if tier2 else None

        result = {
            "place_id": place_id,
            "status": "success" if best else "tier2_rejected",
            "websiteUri": website_uri,
            "reason": None if best else "No photo scored high enough on visual heuristics",
            "best_candidate": best,
            "all_photos": tier2,
            "downloaded_count": len(downloaded),
            "fetched_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
        }
        self._save_meta(venue_dir, result)
        return result

    # ------------------------------------------------------------------
    # Tier 1: deterministic metadata filters
    # ------------------------------------------------------------------

    def _tier1_filter(self, photos: list[dict]) -> list[dict]:
        survivors = []
        for p in photos:
            w = p.get("widthPx", 0)
            h = p.get("heightPx", 0)
            if h == 0:
                continue
            ratio = w / h
            if not (1.3 <= ratio <= 3.0):
                continue
            if w < 400 or h < 300:
                continue
            score = self._keyword_score(p)
            survivors.append(
                {
                    "name": p.get("name"),
                    "widthPx": w,
                    "heightPx": h,
                    "aspect_ratio": round(ratio, 2),
                    "keyword_score": score,
                    "tier1_score": round(ratio * 10 + score * 5, 2),
                }
            )
        survivors.sort(key=lambda x: x["tier1_score"], reverse=True)
        return survivors

    # ------------------------------------------------------------------
    # Tier 2: lightweight visual scoring
    # ------------------------------------------------------------------

    def _tier2_score(self, photos: list[dict]) -> list[dict]:
        ranked = []
        for p in photos:
            try:
                visual = self._analyze_image(p["path"])
                p.update(visual)
                p["tier2_score"] = round(
                    p["tier1_score"] * 0.6
                    + p.get("color_exterior_score", 0) * 20
                    + p.get("edge_vertical_score", 0) * 20,
                    2,
                )
                ranked.append(p)
            except Exception as exc:
                logger.debug("[BUSINESS_PHOTOS] Tier 2 failed for %s: %s", p.get("path"), exc)
        ranked.sort(key=lambda x: x["tier2_score"], reverse=True)
        return ranked

    def _analyze_image(self, image_path: str) -> dict:
        im = Image.open(image_path)
        im = im.convert("RGB")
        thumb = im.resize((64, 64))
        pixels = list(thumb.getdata())
        total = len(pixels)

        blue = sum(1 for r, g, b in pixels if b > 150 and b > r + 20 and b > g + 20)
        warm = sum(1 for r, g, b in pixels if r > 150 and g > 100 and b < 100)
        gray = sum(1 for r, g, b in pixels if max(r, g, b) - min(r, g, b) < 30)

        blue_ratio = blue / total
        warm_ratio = warm / total
        gray_ratio = gray / total

        brightness = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in pixels]
        mean_b = sum(brightness) / total
        variance_b = sum((b - mean_b) ** 2 for b in brightness) / total

        color_score = min(1.0, (blue_ratio + gray_ratio) * variance_b * 10)
        if warm_ratio > 0.4:
            color_score *= 0.5

        edges = 0
        for y in range(64):
            for x in range(63):
                left = pixels[y * 64 + x]
                right = pixels[y * 64 + x + 1]
                diff = sum(abs(left[i] - right[i]) for i in range(3))
                if diff > 60:
                    edges += 1

        edge_score = min(1.0, edges / (64 * 63))

        return {
            "color_exterior_score": round(color_score, 3),
            "edge_vertical_score": round(edge_score, 3),
            "analyzed_width": im.width,
            "analyzed_height": im.height,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _save_meta(venue_dir: Path, data: dict) -> None:
        meta_path = venue_dir / "metadata.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _keyword_score(self, photo: dict) -> float:
        text = ""
        atts = photo.get("authorAttributions", [])
        if atts and atts[0].get("photoUri"):
            text += atts[0]["photoUri"].lower()
        if atts and atts[0].get("uri"):
            text += " " + atts[0]["uri"].lower()
        name = photo.get("name", "")
        if name:
            text += " " + name.lower()

        score = 0.0
        for kw in self.EXTERIOR_KEYWORDS:
            if kw in text:
                score += 1.0
        for kw in self.INTERIOR_KEYWORDS:
            if kw in text:
                score -= 1.0
        return score
