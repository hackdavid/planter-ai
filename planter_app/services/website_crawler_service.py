"""Fallback service: Website image crawler with fuzzy page detection and tiered filtering."""

import re
import io
import time
import logging
import requests
from urllib.parse import urljoin, urlparse
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

logger = logging.getLogger(__name__)


class WebsiteCrawlerService:
    """
    Crawls a venue's website to find the most likely exterior/frontage image.

    Pipeline:
    1. Fetch homepage, extract all internal links
    2. Score each linked page by exterior-relevance (fuzzy URL matching)
    3. Crawl top 3 scoring pages
    4. Extract all <img> tags
    5. Tier 1: filter by aspect ratio, size, URL keywords
    6. Tier 2: lightweight visual scoring (color histogram + edge detection)
    7. Return top-ranked candidate(s)
    """

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

    PAGE_TYPE_PATTERNS = {
        "contact": ["contact", "find-us", "location", "directions", "visit", "reach-us"],
        "about": ["about", "our-story", "who-we-are", "meet-us", "story"],
        "gallery": ["gallery", "photos", "images", "media", "look"],
        "home": ["home", "index", "welcome"],
        "menu": ["menu", "food", "drinks", "cuisine", "eat"],
        "events": ["events", "weddings", "parties", "bookings", "private-hire"],
        "team": ["team", "staff", "chefs", "people", "crew"],
    }

    def __init__(self, request_timeout: int = 15):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
        })
        self.request_timeout = request_timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def crawl(self, base_url: str, max_pages: int = 3, max_images_per_page: int = 30) -> dict:
        """
        Crawl a venue website and return the best exterior image candidate.

        Args:
            base_url: The venue's website URL (e.g., "https://example-cafe.co.uk")
            max_pages: Maximum number of sub-pages to crawl for images
            max_images_per_page: Maximum <img> tags to process per page

        Returns:
            Dict with status, best_candidate, all_ranked, and crawl log.
        """
        logger.info("[CRAWL] Starting crawl for %s", base_url)
        parsed = urlparse(base_url)
        base_domain = parsed.netloc

        if not base_domain:
            return {"status": "invalid_url", "reason": "Could not parse domain", "best_candidate": None}

        # Step 1: Fetch homepage and discover links
        links = self._discover_links(base_url, base_domain)
        logger.info("[CRAWL] Discovered %s internal links", len(links))

        # Step 2: Score pages by exterior-relevance
        scored_pages = self._score_pages(links, base_url)
        top_pages = scored_pages[:max_pages]
        logger.info("[CRAWL] Top pages to crawl: %s", [p["url"] for p in top_pages])

        # Step 3: Crawl top pages and extract images
        all_images: list[dict] = []
        for page in top_pages:
            imgs = self._extract_images_from_page(page["url"], max_images_per_page)
            logger.info("[CRAWL] %s | found %s images", page["url"], len(imgs))
            for img in imgs:
                img["page_score"] = page["score"]
                img["page_type"] = page["page_type"]
            all_images.extend(imgs)

        logger.info("[CRAWL] Total images before Tier 1: %s", len(all_images))

        # Step 4: Tier 1 filtering
        tier1 = self._tier1_filter(all_images)
        logger.info("[CRAWL] Tier 1 survivors: %s", len(tier1))

        if not tier1:
            return {
                "status": "no_candidates",
                "reason": "No images passed aspect ratio / size / keyword filters",
                "best_candidate": None,
                "all_ranked": [],
            }

        # Step 5: Tier 2 visual scoring
        tier2 = self._tier2_score(tier1)
        logger.info("[CRAWL] Tier 2 ranked: %s", len(tier2))

        best = tier2[0] if tier2 else None

        return {
            "status": "success" if best else "no_high_score",
            "reason": None if best else "No image scored high enough on visual heuristics",
            "best_candidate": best,
            "pages_crawled": [p["url"] for p in top_pages],
            "all_ranked": tier2,
        }

    # ------------------------------------------------------------------
    # Step 1 & 2: Link discovery and page scoring
    # ------------------------------------------------------------------

    def _discover_links(self, base_url: str, base_domain: str) -> list[str]:
        """Fetch homepage and extract all internal links."""
        try:
            resp = self.session.get(base_url, timeout=self.request_timeout)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("[CRAWL] Failed to fetch homepage: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        links = set()

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            absolute = urljoin(base_url, href)
            parsed = urlparse(absolute)

            # Only internal links
            if parsed.netloc == base_domain or parsed.netloc == "":
                # Normalize: strip fragment, trailing slash
                clean = f"{parsed.scheme or 'https'}://{parsed.netloc or base_domain}{parsed.path.rstrip('/')}"
                links.add(clean)

        return list(links)

    def _score_pages(self, links: list[str], base_url: str) -> list[dict]:
        """Score each page URL by how likely it is to contain an exterior photo."""
        scored = []
        for url in links:
            path = urlparse(url).path.lower()
            score = 0
            detected_type = "other"

            for page_type, patterns in self.PAGE_TYPE_PATTERNS.items():
                for pat in patterns:
                    if pat in path:
                        detected_type = page_type
                        break

            # Score by page type
            if detected_type == "contact":
                score += 40
            elif detected_type == "about":
                score += 30
            elif detected_type == "gallery":
                score += 30
            elif detected_type == "home":
                score += 20
            elif detected_type == "menu":
                score -= 30
            elif detected_type == "events":
                score -= 40
            elif detected_type == "team":
                score -= 40

            # Boost homepage
            if path in ("", "/", "/index", "/index.html", "/home"):
                score += 25
                detected_type = "home"

            scored.append({"url": url, "score": score, "page_type": detected_type})

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored

    # ------------------------------------------------------------------
    # Step 3: Image extraction from a page
    # ------------------------------------------------------------------

    def _extract_images_from_page(self, page_url: str, max_images: int) -> list[dict]:
        """Fetch a page and extract all <img> tags with metadata."""
        try:
            resp = self.session.get(page_url, timeout=self.request_timeout)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("[CRAWL] Failed to fetch page %s: %s", page_url, exc)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        images = []

        for idx, img in enumerate(soup.find_all("img")):
            if idx >= max_images:
                break

            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if not src:
                continue

            absolute = urljoin(page_url, src)
            # Skip data URIs and obvious non-images
            if absolute.startswith("data:") or not absolute.startswith(("http://", "https://")):
                continue

            w = self._parse_dimension(img.get("width"))
            h = self._parse_dimension(img.get("height"))
            alt = (img.get("alt") or "").lower()

            images.append(
                {
                    "url": absolute,
                    "html_width": w,
                    "html_height": h,
                    "alt_text": alt,
                    "source_page": page_url,
                }
            )

        return images

    # ------------------------------------------------------------------
    # Step 4: Tier 1 filtering
    # ------------------------------------------------------------------

    def _tier1_filter(self, images: list[dict]) -> list[dict]:
        """Filter by aspect ratio, size, and URL keywords."""
        survivors = []

        for img in images:
            w = img.get("html_width")
            h = img.get("html_height")

            # If HTML dimensions are missing, try to fetch image header
            if not w or not h:
                dims = self._probe_image_dimensions(img["url"])
                if dims:
                    w, h = dims
                    img["probed_width"] = w
                    img["probed_height"] = h

            # If still no dimensions, skip or rely on URL keywords only
            if w and h:
                if h == 0:
                    continue
                ratio = w / h
                if not (1.3 <= ratio <= 3.0):
                    continue
                if w < 400 or h < 300:
                    continue
                img["aspect_ratio"] = round(ratio, 2)
            else:
                img["aspect_ratio"] = None

            # URL keyword scoring
            score = self._keyword_score(img["url"]) + self._keyword_score(img.get("alt_text", ""))
            img["keyword_score"] = score

            # Combined tier1 score
            ar_score = img.get("aspect_ratio", 1.5) * 10 if img.get("aspect_ratio") else 5
            img["tier1_score"] = round(ar_score + score * 5, 2)
            survivors.append(img)

        survivors.sort(key=lambda x: x["tier1_score"], reverse=True)
        return survivors

    # ------------------------------------------------------------------
    # Step 5: Tier 2 visual scoring
    # ------------------------------------------------------------------

    def _tier2_score(self, images: list[dict]) -> list[dict]:
        """Download top images and score by color + edge heuristics."""
        ranked = []
        for img in images[:10]:  # Only score top 10 to limit bandwidth
            try:
                visual = self._analyze_image(img["url"])
                img.update(visual)
                img["tier2_score"] = round(
                    img["tier1_score"] * 0.6
                    + img.get("color_exterior_score", 0) * 20
                    + img.get("edge_vertical_score", 0) * 20,
                    2,
                )
                ranked.append(img)
            except Exception as exc:
                logger.debug("[CRAWL] Tier 2 failed for %s: %s", img["url"], exc)
                continue

        ranked.sort(key=lambda x: x["tier2_score"], reverse=True)
        return ranked

    def _analyze_image(self, image_url: str) -> dict:
        """Download image and compute lightweight CV signals."""
        # Download first 50KB to keep bandwidth low; most web images are under this
        resp = self.session.get(image_url, timeout=self.request_timeout, stream=True)
        resp.raise_for_status()
        chunk = resp.raw.read(50_000)
        resp.close()

        im = Image.open(io.BytesIO(chunk))
        im = im.convert("RGB")

        # Resize for speed
        thumb = im.resize((64, 64))
        pixels = list(thumb.getdata())

        # Color analysis
        total = len(pixels)
        blue = sum(1 for r, g, b in pixels if b > 150 and b > r + 20 and b > g + 20)
        warm = sum(1 for r, g, b in pixels if r > 150 and g > 100 and b < 100)
        gray = sum(1 for r, g, b in pixels if max(r, g, b) - min(r, g, b) < 30)

        blue_ratio = blue / total
        warm_ratio = warm / total
        gray_ratio = gray / total

        # Brightness variance
        brightness = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in pixels]
        mean_b = sum(brightness) / total
        variance_b = sum((b - mean_b) ** 2 for b in brightness) / total

        # Heuristic exterior score
        color_score = min(1.0, (blue_ratio + gray_ratio) * variance_b * 10)
        if warm_ratio > 0.4:
            color_score *= 0.5  # Penalize warm interiors

        # Simple edge proxy: contrast between adjacent pixels (horizontal scan)
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
    def _parse_dimension(value) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    def _probe_image_dimensions(self, image_url: str) -> Optional[tuple[int, int]]:
        """Download first 8KB and parse dimensions via PIL without full download."""
        try:
            resp = self.session.get(image_url, timeout=self.request_timeout, stream=True)
            resp.raise_for_status()
            chunk = resp.raw.read(8_000)
            resp.close()
            im = Image.open(io.BytesIO(chunk))
            return im.width, im.height
        except Exception:
            return None

    def _keyword_score(self, text: str) -> float:
        text = text.lower()
        score = 0.0
        for kw in self.EXTERIOR_KEYWORDS:
            if kw in text:
                score += 1.0
        for kw in self.INTERIOR_KEYWORDS:
            if kw in text:
                score -= 1.0
        return score
