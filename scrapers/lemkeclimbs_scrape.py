"""Scrape topos from lemkeclimbs.com into data/lemkeclimbs.db.

Two page types:
  trip_report   — individual climb reports (the bulk of the site)
  regional_info — area/sub-area index pages with access notes and peak tables

Classification is done entirely from the homepage nav using two hardcoded sets;
no regional pages need to be fetched to discover trip reports.

Safe to Ctrl-C and resume: already-scraped URLs are skipped on restart.

Usage:
    python scrapers/lemkeclimbs_scrape.py
    python scrapers/lemkeclimbs_scrape.py --limit 10 --shuffle
    python scrapers/lemkeclimbs_scrape.py --delay 5 --db data/lemkeclimbs.db
"""

import argparse
import random
import re
import sqlite3
import sys
import time
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.lemkeclimbs.com"
HOME_URL = "https://www.lemkeclimbs.com"
ROBOTS_URL = "https://www.lemkeclimbs.com/robots.txt"

HEADERS = {
    "User-Agent": "mountaineering-reco/1.0 (personal research project)",
}

# Slugs whose pages are regional/sub-regional overviews worth scraping as regional_info.
# Keys are URL slugs (without leading slash or .html); values are (area, region) tuples.
# area is the sub-region label; region is the top-level label.
AREA_INDEX_SLUGS: dict[str, tuple[str, str]] = {
    # Top-level regions (no sub-region)
    "washington":       ("", "Washington"),
    "oregon":           ("", "Oregon"),
    "colorado":         ("", "Colorado"),
    "california":       ("", "California"),
    "wyoming":          ("", "Wyoming"),
    "utah":             ("", "Utah"),
    "alaska":           ("", "Alaska"),
    "international":    ("", "International"),
    # Washington sub-regions
    "north-cascades":           ("North Cascades", "Washington"),
    "alpine-lakes-wilderness":  ("Alpine Lakes Wilderness", "Washington"),
    "south-cascades":           ("South Cascades", "Washington"),
    "olympic-peninsula":        ("Olympic Peninsula", "Washington"),
    # Oregon sub-regions
    "oregon-cascades":          ("Oregon Cascades", "Oregon"),
    # Colorado sub-regions
    "san-juan-mountains":       ("San Juan Mountains", "Colorado"),
    "elk-mountains":            ("Elk Mountains", "Colorado"),
    "sawatch-range":            ("Sawatch Range", "Colorado"),
    "front-range":              ("Front Range", "Colorado"),
    # California sub-regions
    "sierra-nevada":            ("Sierra Nevada", "California"),
    "yosemite":                 ("Yosemite", "California"),
    # Wyoming sub-regions
    "wind-river-range":         ("Wind River Range", "Wyoming"),
    "tetons":                   ("Tetons", "Wyoming"),
    # International sub-regions
    "canadian-rockies":         ("Canadian Rockies", "International"),
    "alps":                     ("Alps", "International"),
}

# Slugs to skip entirely — no useful content for our purposes.
SKIP_SLUGS: set[str] = {
    # Year-by-year link lists (no content)
    "2008-trip-reports", "2009-trip-reports", "2010-trip-reports",
    "2011-trip-reports", "2012-trip-reports", "2013-trip-reports",
    "2014-trip-reports", "2015-trip-reports", "2016-trip-reports",
    "2017-trip-reports", "2018-trip-reports", "2019-trip-reports",
    "2020-trip-reports", "2021-trip-reports", "2022-trip-reports",
    "2023-trip-reports",
    # Vanlife section
    "vanlife",
    # Admin / meta pages
    "whats-new", "contact", "about", "2019-goalswishlist",
    # Van conversion pages (top-level slugs, not under /vanlife/)
    "composting-toilet", "heating-system", "tire-upgrade", "electrical-system",
    "floor-installation", "folding-bed", "refrigerator", "thinsulate-installation",
    "water-system", "the-couch", "the-pantry", "the-wardrobe", "conversion-journal",
    "bookshelf",
    # Non-mountaineering activity
    "white-rim-trail-biking",
}

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS topos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT UNIQUE NOT NULL,
    page_type     TEXT NOT NULL,
    title         TEXT,
    area          TEXT,
    region        TEXT,
    grade         TEXT,
    elevation     TEXT,
    date_of_climb TEXT,
    full_text     TEXT,
    scraped_at    TEXT
);
"""

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(DB_SCHEMA)
    conn.commit()
    return conn


def already_scraped(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT url FROM topos").fetchall()}


def insert_topo(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO topos
           (url, page_type, title, area, region, grade, elevation, date_of_climb, full_text, scraped_at)
           VALUES (:url, :page_type, :title, :area, :region, :grade, :elevation, :date_of_climb,
                   :full_text, :scraped_at)""",
        data,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# robots.txt check
# ---------------------------------------------------------------------------

def check_robots(session: requests.Session) -> None:
    try:
        resp = session.get(ROBOTS_URL, timeout=10)
        resp.raise_for_status()
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(resp.text.splitlines())
        if not rp.can_fetch(HEADERS["User-Agent"], HOME_URL):
            print("WARNING: robots.txt disallows crawling for our user-agent. Proceeding anyway "
                  "since this is a personal research project — but check the file manually.")
    except Exception as exc:
        print(f"WARNING: could not fetch robots.txt: {exc}")


# ---------------------------------------------------------------------------
# URL collection
# ---------------------------------------------------------------------------

def _slug_from_url(url: str) -> str:
    path = urlparse(url).path.lstrip("/")
    return path.removesuffix(".html")


def collect_urls(session: requests.Session) -> list[dict]:
    """Fetch homepage, classify every nav URL, return list of {url, page_type, area, region}."""
    resp = session.get(HOME_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    nav = soup.find("nav") or soup.find("div", id="nav") or soup.find("div", class_=re.compile("nav"))
    link_soup = nav if nav else soup

    seen: set[str] = set()
    entries: list[dict] = []

    for a in link_soup.find_all("a", href=True):
        href = a["href"].strip()
        full_url = urljoin(BASE_URL, href)
        # Only internal .html pages
        if not full_url.startswith(BASE_URL):
            continue
        if full_url in seen:
            continue
        seen.add(full_url)

        slug = _slug_from_url(full_url)
        if not slug or slug == "index":
            continue

        # Skip vanlife sub-pages (slug starts with "vanlife/")
        if slug.startswith("vanlife"):
            continue

        if slug in SKIP_SLUGS:
            continue

        if slug in AREA_INDEX_SLUGS:
            area, region = AREA_INDEX_SLUGS[slug]
            entries.append({
                "url": full_url,
                "page_type": "regional_info",
                "area": area,
                "region": region,
            })
        else:
            entries.append({
                "url": full_url,
                "page_type": "trip_report",
                "area": "",
                "region": "",
            })

    return entries


# ---------------------------------------------------------------------------
# Page scrape
# ---------------------------------------------------------------------------

def _extract_text(soup: BeautifulSoup) -> str:
    body = soup.find("div", id=re.compile("content|main|entry", re.I)) or soup.find("article") or soup.body
    parts = []
    for el in body.find_all(["p", "h2", "h3", "h4", "li"]):
        # Skip elements that contain only an image with no text
        if el.find("img") and not el.get_text(strip=True):
            continue
        text = el.get_text(separator=" ", strip=True)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def scrape_page(session: requests.Session, entry: dict) -> dict:
    resp = session.get(entry["url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    title = re.sub(r"\s*[-|]\s*LEMKE CLIMBS\s*$", "", title, flags=re.IGNORECASE).strip()

    full_text = _extract_text(soup)

    data: dict = {
        "url": entry["url"],
        "page_type": entry["page_type"],
        "title": title,
        "area": entry.get("area", ""),
        "region": entry.get("region", ""),
        "grade": None,
        "elevation": None,
        "date_of_climb": None,
        "full_text": full_text,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }

    return data


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(db_path: str, limit: int | None, delay: float, shuffle: bool) -> None:
    session = requests.Session()
    conn = init_db(db_path)
    done = already_scraped(conn)

    check_robots(session)

    print("Collecting URLs from homepage...")
    entries = collect_urls(session)
    trip_reports = [e for e in entries if e["page_type"] == "trip_report"]
    regional = [e for e in entries if e["page_type"] == "regional_info"]
    print(f"  Found {len(trip_reports)} trip reports, {len(regional)} regional info pages.")

    pending = [e for e in entries if e["url"] not in done]
    if shuffle:
        random.shuffle(pending)
    if limit:
        pending = pending[:limit]

    print(f"  Already scraped: {len(done)}  To scrape: {len(pending)}")
    if not pending:
        print("Nothing to do.")
        conn.close()
        return

    print("\nScraping pages...")
    ok = fail = 0
    for i, entry in enumerate(pending, 1):
        label = f"[{i}/{len(pending)}]"
        try:
            data = scrape_page(session, entry)
            insert_topo(conn, data)
            print(f"{label} [{data['page_type']}] {data['title'] or entry['url']}")
            ok += 1
        except KeyboardInterrupt:
            print(f"\nInterrupted. Scraped: {ok}  Failed: {fail}. Resume with same command.")
            conn.close()
            sys.exit(0)
        except Exception as exc:
            print(f"{label} ERROR {entry['url']}: {exc}")
            fail += 1

        if i < len(pending):
            time.sleep(delay)

    conn.close()
    print(f"\nDone. Scraped: {ok}  Failed: {fail}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape lemkeclimbs.com trip reports and area pages")
    parser.add_argument("--db", default="data/lemkeclimbs.db",
                        help="SQLite output path (default: data/lemkeclimbs.db)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max pages to scrape in this run (default: all)")
    parser.add_argument("--delay", type=float, default=3.0,
                        help="Seconds between requests (default: 3)")
    parser.add_argument("--shuffle", action="store_true",
                        help="Randomise URL order before applying --limit")
    args = parser.parse_args()
    run(args.db, args.limit, args.delay, args.shuffle)


if __name__ == "__main__":
    main()
