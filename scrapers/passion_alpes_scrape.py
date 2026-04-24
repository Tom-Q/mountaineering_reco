"""Scrape topos from passion-alpes.com/topos into data/passion_alpes.db.

Two-pass scrape:
  Pass 1: fetch the index page, extract all topo URLs with category + region.
  Pass 2: fetch each topo page, extract title, grade, full text, and images.

Safe to Ctrl-C and resume: already-scraped URLs are skipped on restart.

Usage:
    python scrapers/passion_alpes_scrape.py
    python scrapers/passion_alpes_scrape.py --limit 5 --delay 1.5
    python scrapers/passion_alpes_scrape.py --db data/passion_alpes.db
"""

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import cloudscraper
from bs4 import BeautifulSoup, NavigableString, Tag

INDEX_URL = "https://www.passion-alpes.com/topos"
BASE_URL = "https://www.passion-alpes.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS topos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT UNIQUE NOT NULL,
    title       TEXT,
    category    TEXT,
    region      TEXT,
    grade       TEXT,
    departure   TEXT,
    timing      TEXT,
    full_text   TEXT,
    scraped_at  TEXT
);

CREATE TABLE IF NOT EXISTS topo_images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topo_id     INTEGER REFERENCES topos(id),
    image_url   TEXT,
    caption     TEXT,
    is_diagram  INTEGER DEFAULT 0  -- 1 if caption/filename suggests a topo diagram
);
"""

DIAGRAM_KEYWORDS = {"topo", "schéma", "schema", "itinéraire", "trace", "tracé", "croquis"}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(DB_SCHEMA)
    conn.commit()
    return conn


def already_scraped(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT url FROM topos").fetchall()}


def insert_topo(conn: sqlite3.Connection, data: dict, images: list[dict]) -> None:
    cur = conn.execute(
        """INSERT OR REPLACE INTO topos
           (url, title, category, region, grade, departure, timing, full_text, scraped_at)
           VALUES (:url, :title, :category, :region, :grade, :departure, :timing, :full_text, :scraped_at)""",
        data,
    )
    topo_id = cur.lastrowid
    for img in images:
        conn.execute(
            "INSERT INTO topo_images (topo_id, image_url, caption, is_diagram) VALUES (?, ?, ?, ?)",
            (topo_id, img["url"], img["caption"], int(img["is_diagram"])),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Pass 1: index scrape
# ---------------------------------------------------------------------------

def _make_scraper():
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "linux", "desktop": True}
    )


def _get_with_retry(scraper, url: str, retries: int = 3, backoff: float = 10.0):
    for attempt in range(retries):
        resp = scraper.get(url, headers=HEADERS, timeout=30)
        if resp.status_code == 429:
            wait = backoff * (attempt + 1)
            print(f"  429 rate limit — waiting {wait:.0f}s before retry {attempt + 1}/{retries}...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def fetch_index(scraper) -> list[dict]:
    """Return list of {url, title, category, region} from the index page."""
    resp = _get_with_retry(scraper, INDEX_URL)
    soup = BeautifulSoup(resp.text, "lxml")

    # The topo list lives in the main article/entry-content area.
    content = soup.find("div", class_="entry-content") or soup.find("article")
    if not content:
        # Fallback: search entire body
        content = soup.body

    entries = []
    current_category = ""
    current_region = ""

    # Walk all elements looking for <strong> (category), <em> (region), <a> (route)
    for el in content.descendants:
        if not isinstance(el, Tag):
            continue

        if el.name == "strong":
            text = el.get_text(strip=True).rstrip(":")
            if text:
                current_category = text
                current_region = ""  # reset region on new category

        elif el.name == "em":
            text = el.get_text(strip=True).rstrip(":")
            if text:
                current_region = text

        elif el.name == "a":
            href = el.get("href", "")
            title = el.get_text(strip=True)
            # Only include links that look like topo posts (contain a year in path)
            if href and re.search(r"/20\d\d/", href) and title:
                full_url = urljoin(BASE_URL, href)
                entries.append({
                    "url": full_url,
                    "title": title,
                    "category": current_category,
                    "region": current_region,
                })

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for e in entries:
        if e["url"] not in seen:
            seen.add(e["url"])
            unique.append(e)
    return unique


# ---------------------------------------------------------------------------
# Pass 2: individual topo scrape
# ---------------------------------------------------------------------------

def _is_diagram(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in DIAGRAM_KEYWORDS)


def _clean_url(url: str) -> str:
    """Strip WordPress resize query params to get the original image URL."""
    return url.split("?")[0]


def scrape_topo(scraper, url: str) -> tuple[dict, list[dict]]:
    """Fetch a single topo page and return (data_dict, images_list)."""
    resp = _get_with_retry(scraper, url)
    soup = BeautifulSoup(resp.text, "lxml")

    # Title
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""

    # Article body
    body = soup.find("div", class_="entry-content") or soup.find("article")

    grade = ""
    departure = ""
    timing = ""

    # Extract structured stats from <strong> label → following text pattern
    if body:
        for strong in body.find_all("strong"):
            label = strong.get_text(strip=True).lower()
            # The value is in the next sibling text node or the parent's remaining text
            following = ""
            sib = strong.next_sibling
            while sib and not (isinstance(sib, Tag) and sib.name in ("strong", "br", "p")):
                if isinstance(sib, NavigableString):
                    following += str(sib)
                elif isinstance(sib, Tag):
                    following += sib.get_text()
                sib = sib.next_sibling
            value = following.strip().lstrip(":").strip()

            if not grade and any(k in label for k in ("niveau", "difficulté", "grade", "cotation")):
                grade = value
            elif not departure and any(k in label for k in ("départ", "point de départ", "accès")):
                departure = value
            elif not timing and any(k in label for k in ("horaire", "temps", "durée")):
                timing = value

    # Full text: all paragraph text from the article body
    full_text = ""
    if body:
        paragraphs = []
        for el in body.find_all(["p", "h2", "h3", "h4", "li"]):
            t = el.get_text(separator=" ", strip=True)
            if t:
                paragraphs.append(t)
        full_text = "\n\n".join(paragraphs)

    # Images
    images = []
    seen_srcs: set[str] = set()
    if body:
        for figure in body.find_all("figure"):
            img = figure.find("img")
            figcap = figure.find("figcaption")
            if img:
                src = _clean_url(img.get("src", "") or img.get("data-src", ""))
                caption = figcap.get_text(strip=True) if figcap else img.get("alt", "")
                if src and src not in seen_srcs:
                    seen_srcs.add(src)
                    images.append({
                        "url": src,
                        "caption": caption,
                        "is_diagram": _is_diagram(caption + " " + src),
                    })

        # Images not inside <figure> (older posts)
        for img in body.find_all("img"):
            src = _clean_url(img.get("src", "") or img.get("data-src", ""))
            if src and src not in seen_srcs and "passion-alpes.com/wp-content" in src:
                alt = img.get("alt", "")
                images.append({
                    "url": src,
                    "caption": alt,
                    "is_diagram": _is_diagram(alt + " " + src),
                })
                seen_srcs.add(src)

    from datetime import datetime, timezone
    data = {
        "url": url,
        "title": title,
        "category": "",   # filled in by caller from index
        "region": "",     # filled in by caller from index
        "grade": grade,
        "departure": departure,
        "timing": timing,
        "full_text": full_text,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }
    return data, images


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(db_path: str, limit: int | None, delay: float) -> None:
    conn = init_db(db_path)
    done = already_scraped(conn)
    scraper = _make_scraper()

    print("Pass 1: fetching index...")
    entries = fetch_index(scraper)
    print(f"  Found {len(entries)} topos on index page.")

    pending = [e for e in entries if e["url"] not in done]
    if limit:
        pending = pending[:limit]

    print(f"  Already scraped: {len(done)}  To scrape: {len(pending)}")
    if not pending:
        print("Nothing to do.")
        conn.close()
        return

    print("\nPass 2: scraping topos...")
    ok = fail = 0
    for i, entry in enumerate(pending, 1):
        label = f"[{i}/{len(pending)}]"
        try:
            data, images = scrape_topo(scraper, entry["url"])
            data["category"] = entry["category"]
            data["region"] = entry["region"]
            if not data["title"]:
                data["title"] = entry["title"]
            insert_topo(conn, data, images)
            print(f"{label} {data['title'] or entry['url']} — OK ({len(images)} images)")
            ok += 1
        except KeyboardInterrupt:
            print(f"\nInterrupted. Scraped: {ok}  Failed: {fail}. Resume with same command.")
            conn.close()
            sys.exit(0)
        except Exception as e:
            print(f"{label} ERROR {entry['url']}: {e}")
            fail += 1

        if i < len(pending):
            time.sleep(delay)

    conn.close()
    print(f"\nDone. Scraped: {ok}  Failed: {fail}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape passion-alpes.com topos")
    parser.add_argument("--db", default="data/passion_alpes.db",
                        help="SQLite output path (default: data/passion_alpes.db)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max topos to scrape in this run (default: all)")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="Seconds between requests (default: 1.5)")
    args = parser.parse_args()
    run(args.db, args.limit, args.delay)


if __name__ == "__main__":
    main()
