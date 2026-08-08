"""Test runner: pull website images, union with Business Photos, then Vision QA one-by-one."""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from planter_app.config import Settings
from planter_app.utils.cache_db import CacheDB
from planter_app.services import WebsiteCrawlerService

load_dotenv()


def main():
    settings = Settings.from_env()
    db = CacheDB(settings.cache_db_path)

    # Load the 20 candidates
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

    # Load website lookup from Business Photos run
    lookup_path = Path(__file__).parent / "planter_app" / "data" / "website_lookup.json"
    with open(lookup_path, "r", encoding="utf-8") as f:
        website_lookup = json.load(f)

    crawler = WebsiteCrawlerService(request_timeout=15)

    # For the prototype, we run website crawl on:
    # 1. The 2 venues where Business Photos Tier 1 rejected ( Deer Cafe, Chaachi's )
    # 2. 3 more venues where Business Photos succeeded, to demonstrate the union
    target_place_ids = {
        "ChIJK8Bb9WcLdkgRrUZC97T0XWQ",   # Deer Cafe — BP rejected
        "ChIJA3NOoqsHdkgReHOikJIlCXg",   # Chaachi's — BP rejected
        "ChIJAdrq478LdkgRbIQdZ-jtDAs",   # Fortunella — BP success (demo union)
        "ChIJJ3AGi7YLdkgRYrRCCnTmMlk",   # The French Tarte — BP success (demo union)
        "ChIJu46vWr8LdkgR1bOGMBoMl50",   # Surbeanton — BP success (demo union)
    }

    union_results = []
    for c in candidates:
        pid = c["place_id"]
        name = c["name"]

        # Load Business Photos metadata
        bp_dir = Path(__file__).parent / "planter_app" / "data" / "business_photos" / pid
        bp_meta_path = bp_dir / "metadata.json"
        bp_candidates = []
        if bp_meta_path.exists():
            with open(bp_meta_path, "r", encoding="utf-8") as f:
                bp_meta = json.load(f)
            for ph in bp_meta.get("all_photos", []):
                bp_candidates.append({
                    "source": "business_photos",
                    "path": ph.get("path"),
                    "tier2_score": ph.get("tier2_score", 0),
                    "tier1_score": ph.get("tier1_score", 0),
                })

        # Load Website candidates (only for target_place_ids to limit crawl time)
        web_candidates = []
        if pid in target_place_ids:
            wu = website_lookup.get(pid, {}).get("websiteUri")
            if wu and not wu.startswith("https://www.instagram.com"):
                print(f"\n[Crawling website for {name}] {wu}")
                try:
                    web_result = crawler.crawl(wu, max_pages=3, max_images_per_page=20)
                    for img in web_result.get("all_ranked", []):
                        web_candidates.append({
                            "source": "website",
                            "path": img.get("url"),  # website images are URLs, not local paths
                            "tier2_score": img.get("tier2_score", 0),
                            "tier1_score": img.get("tier1_score", 0),
                        })
                    print(f"  -> Found {len(web_candidates)} website candidates")
                except Exception as exc:
                    print(f"  -> Website crawl failed: {exc}")
            else:
                print(f"\n[Skipping website for {name}] No valid website URL")

        # UNION: combine all candidates and rank by tier2_score
        all_candidates = bp_candidates + web_candidates
        all_candidates.sort(key=lambda x: x["tier2_score"], reverse=True)

        union_results.append({
            "place_id": pid,
            "name": name,
            "address": c["address"],
            "websiteUri": website_lookup.get(pid, {}).get("websiteUri"),
            "candidates": all_candidates,
            "candidate_count": len(all_candidates),
        })

    # Save union metadata
    union_dir = Path(__file__).parent / "planter_app" / "data" / "union_candidates"
    union_dir.mkdir(parents=True, exist_ok=True)
    union_path = union_dir / "metadata.json"
    with open(union_path, "w", encoding="utf-8") as f:
        json.dump(union_results, f, indent=2)

    # Print summary
    print(f"\n{'='*60}")
    print("FALLBACK UNION SUMMARY")
    print(f"{'='*60}")
    print(f"Total venues: {len(union_results)}")
    for r in union_results:
        srcs = [c["source"] for c in r["candidates"]]
        bp_count = srcs.count("business_photos")
        web_count = srcs.count("website")
        best = r["candidates"][0] if r["candidates"] else None
        print(f"\n  {r['name']}")
        print(f"    BP candidates: {bp_count} | Website candidates: {web_count}")
        if best:
            print(f"    BEST: source={best['source']} | tier2={best['tier2_score']} | {best['path'][:60]}...")
        else:
            print(f"    BEST: NONE")

    print(f"\nUnion metadata saved to: {union_path}")


if __name__ == "__main__":
    main()
