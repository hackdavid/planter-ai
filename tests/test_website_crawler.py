"""Test runner for WebsiteCrawlerService.

Crawls a real venue website to demonstrate fuzzy page detection,
image extraction, and tiered filtering.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from planter_app.services import WebsiteCrawlerService


def main():
    # Real independent London cafe websites to test
    test_urls = [
        "https://www.monmouthcoffee.co.uk",      # Monmouth Coffee
        "https://www.departmentofcoffee.com",    # Department of Coffee
    ]

    crawler = WebsiteCrawlerService(request_timeout=15)

    for url in test_urls:
        print(f"\n{'='*70}")
        print(f"CRAWLING: {url}")
        print(f"{'='*70}")

        result = crawler.crawl(url, max_pages=3, max_images_per_page=20)

        print(f"\nStatus: {result['status']}")
        if result.get('reason'):
            print(f"Reason: {result['reason']}")

        print(f"\nPages crawled:")
        for page in result.get('pages_crawled', []):
            print(f"  - {page}")

        print(f"\nTotal images ranked: {len(result.get('all_ranked', []))}")

        best = result.get('best_candidate')
        if best:
            print(f"\nBEST CANDIDATE:")
            print(f"  URL: {best['url']}")
            print(f"  Page: {best.get('source_page')}")
            print(f"  Tier1 score: {best.get('tier1_score')}")
            print(f"  Tier2 score: {best.get('tier2_score')}")
            print(f"  Color ext. score: {best.get('color_exterior_score')}")
            print(f"  Edge vert. score: {best.get('edge_vertical_score')}")
            print(f"  Keyword score: {best.get('keyword_score')}")
            print(f"  Dimensions: {best.get('analyzed_width')}x{best.get('analyzed_height')}")
            if best.get('aspect_ratio'):
                print(f"  Aspect ratio: {best['aspect_ratio']}")

        print(f"\nTop 5 ranked images:")
        for idx, img in enumerate(result.get('all_ranked', [])[:5], 1):
            print(f"  {idx}. score={img.get('tier2_score')} | page={img.get('page_type')} | {img['url'][:80]}...")


if __name__ == "__main__":
    main()
