"""Bulk-scrape SummitPost routes from a URL list into data/summitpost.db.

Reads URLs from a file (one per line), skips already-scraped ones, and writes
structured metadata + section text + cover image URL into SQLite.

Safe to Ctrl-C and resume: already-scraped URLs are skipped on restart.
Failed URLs are appended to data/failed_urls.txt for later retry.

Usage:
    python scrapers/summitpost_scrape.py
    python scrapers/summitpost_scrape.py --limit 10 --delay 3.0
    python scrapers/summitpost_scrape.py --urls data/route_urls.txt --db data/summitpost.db
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.summitpost import fetch_route, PageUnderConstruction

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
    sp_id         INTEGER PRIMARY KEY,
    url           TEXT UNIQUE NOT NULL,
    name          TEXT,
    lat           REAL,
    lon           REAL,
    location      TEXT,
    route_type    TEXT,
    difficulty    TEXT,
    time_required TEXT,
    views         INTEGER,
    score         REAL,
    votes         INTEGER,
    properties    TEXT,  -- JSON: Season, Grade, Rock Difficulty, and any other table fields
    scraped_at    TEXT
);

CREATE TABLE IF NOT EXISTS sections (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id INTEGER REFERENCES routes(sp_id),
    heading  TEXT,
    body     TEXT,
    position INTEGER
);

CREATE TABLE IF NOT EXISTS images (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id   INTEGER REFERENCES routes(sp_id),
    remote_url TEXT,
    local_path TEXT
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(DB_SCHEMA)
    conn.commit()
    return conn


def already_scraped_urls(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT url FROM routes").fetchall()
    return {r[0] for r in rows}


def insert_route(conn: sqlite3.Connection, data: dict) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO routes
           (sp_id, url, name, lat, lon, location, route_type, difficulty,
            time_required, views, score, votes, properties, scraped_at)
           VALUES (:sp_id, :url, :name, :lat, :lon, :location, :route_type,
                   :difficulty, :time_required, :views, :score, :votes, :properties, :scraped_at)""",
        data,
    )
    if data.get("sp_id") is not None:
        conn.execute("DELETE FROM sections WHERE route_id = ?", (data["sp_id"],))
        for section in data.get("sections", []):
            conn.execute(
                "INSERT INTO sections (route_id, heading, body, position) VALUES (?, ?, ?, ?)",
                (data["sp_id"], section["heading"], section["body"], section["position"]),
            )
        if data.get("cover_image_url"):
            conn.execute(
                "INSERT OR IGNORE INTO images (route_id, remote_url) VALUES (?, ?)",
                (data["sp_id"], data["cover_image_url"]),
            )
    conn.commit()


def scrape(urls_path: str, db_path: str, limit: int | None, delay: float) -> None:
    urls = Path(urls_path).read_text().splitlines()
    urls = [u.strip() for u in urls if u.strip()]

    conn = init_db(db_path)
    done = already_scraped_urls(conn)
    pending = [u for u in urls if u not in done]
    failed_path = Path(urls_path).parent / "failed_urls.txt"

    total = len(pending)
    if limit:
        pending = pending[:limit]
        total = len(pending)

    print(f"URLs total: {len(urls)}  already scraped: {len(done)}  to scrape: {total}")
    if not pending:
        print("Nothing to do.")
        return

    ok = skip = fail = 0
    for i, url in enumerate(pending, 1):
        label = f"[{i}/{total}]"
        try:
            data = fetch_route(url)
            insert_route(conn, data)
            name = data.get("name") or url
            print(f"{label} {name} — OK")
            ok += 1
        except KeyboardInterrupt:
            print(f"\nInterrupted after {ok} scraped, {skip} skipped, {fail} failed. Resume with same command.")
            conn.close()
            sys.exit(0)
        except PageUnderConstruction:
            print(f"{label} SKIP (under construction) {url}")
            skip += 1
        except Exception as e:
            print(f"{label} ERROR {url}: {e}")
            with open(failed_path, "a") as f:
                f.write(url + "\n")
            fail += 1
            time.sleep(delay)  # still wait after errors to avoid hammering

    conn.close()
    print(f"\nDone. Scraped: {ok}  Skipped: {skip}  Failed: {fail}")
    if fail:
        print(f"Failed URLs written to {failed_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk-scrape SummitPost routes")
    parser.add_argument("--urls", default="data/route_urls.txt",
                        help="URL list file (default: data/route_urls.txt)")
    parser.add_argument("--db", default="data/summitpost.db",
                        help="SQLite output path (default: data/summitpost.db)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max routes to scrape in this run (default: all)")
    parser.add_argument("--delay", type=float, default=3.0,
                        help="Seconds between requests (default: 3.0)")
    args = parser.parse_args()
    scrape(args.urls, args.db, args.limit, args.delay)


if __name__ == "__main__":
    main()
