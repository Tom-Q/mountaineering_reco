"""Discover all SummitPost route URLs for a given route type.

Paginates the SummitPost object list and writes one URL per line to an output file.
Safe to re-run: existing output file is overwritten.

Usage:
    python scrapers/summitpost_discover.py
    python scrapers/summitpost_discover.py --route-type Mountaineering --out data/route_urls.txt
"""

import argparse
import sys
import time

import cloudscraper
from bs4 import BeautifulSoup

BASE_URL = "https://www.summitpost.org"
LIST_URL = f"{BASE_URL}/object_list.php"
PAGE_SIZE = 24   # results per page (confirmed from HTML)
PAGE_DELAY = 3.0  # seconds between list-page requests


def fetch_page(scraper, route_type: str, page: int) -> str:
    # Sorted by hits descending so most-documented routes come first.
    # No location params — purely type + sort filter.
    params = {
        "object_type": "2",
        "route_type_2": route_type,
        "map_2": "1",
        "order_type": "DESC",
        "orderby": "object_scores.hits",
        "page": str(page),
    }
    resp = scraper.get(LIST_URL, params=params, timeout=30,
                       headers={"Referer": BASE_URL + "/"})
    resp.raise_for_status()
    return resp.text


def parse_route_links(html: str) -> list[str]:
    """Extract route URLs from p.cci-title > a links on a listing page."""
    soup = BeautifulSoup(html, "lxml")
    urls = []
    for a in soup.select("p.cci-title > a"):
        href = a.get("href", "")
        if href and not href.startswith("http"):
            href = BASE_URL + href
        if href and href not in urls:
            urls.append(href)
    return urls


def parse_total(html: str) -> int | None:
    """Extract total result count from 'Viewing: 1-24 of 2328' span."""
    import re
    m = re.search(r"Viewing:\s*\d+-\d+\s+of\s+([\d,]+)", html)
    return int(m.group(1).replace(",", "")) if m else None


def discover(route_type: str, out_path: str) -> None:
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "desktop": True}
    )

    all_urls: list[str] = []
    seen: set[str] = set()
    page = 1
    expected_total = None

    print(f"Discovering {route_type} routes on SummitPost...")

    while True:
        print(f"  Page {page}...", end=" ", flush=True)
        try:
            html = fetch_page(scraper, route_type, page)
        except Exception as e:
            print(f"ERROR: {e}")
            break

        if expected_total is None:
            expected_total = parse_total(html)
            if expected_total:
                print(f"(total: {expected_total}) ", end="", flush=True)

        links = parse_route_links(html)
        if not links:
            print("no routes found — stopping.")
            break

        new_links = [u for u in links if u not in seen]
        for u in new_links:
            seen.add(u)
            all_urls.append(u)
        print(f"{len(new_links)} new (running total: {len(all_urls)})")

        if len(links) < PAGE_SIZE:
            # Last page — fewer results than a full page
            break

        if not new_links:
            print("  No new URLs on this page — stopping.")
            break

        page += 1
        time.sleep(PAGE_DELAY)

    print(f"\nDiscovered: {len(all_urls)} URLs")
    if expected_total and abs(len(all_urls) - expected_total) > expected_total * 0.05:
        print(f"WARNING: expected ~{expected_total}, got {len(all_urls)} — "
              f"pagination may have failed, check before scraping.")
    elif expected_total:
        print(f"OK: within 5% of expected {expected_total}")

    with open(out_path, "w") as f:
        for url in all_urls:
            f.write(url + "\n")
    print(f"Written to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover SummitPost route URLs")
    parser.add_argument("--route-type", default="Mountaineering",
                        help="SummitPost route_type_2 filter (default: Mountaineering)")
    parser.add_argument("--out", default="data/route_urls.txt",
                        help="Output file path (default: data/route_urls.txt)")
    args = parser.parse_args()
    discover(args.route_type, args.out)


if __name__ == "__main__":
    main()
