"""Scrape SAC alpine tour routes from the suissealpine API into data/sac.db.

Uses the public suissealpine.sac-cas.ch JSON API — no HTML scraping needed.
One DB record per route (a summit can have multiple routes at different grades).
Coordinates are exact (Swiss LV95 → WGS84) — no geocoding needed.

Usage:
    python scrapers/sac_scrape.py
    python scrapers/sac_scrape.py --limit 50
    python scrapers/sac_scrape.py --db data/sac.db
"""

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

API_BASE = "https://www.suissealpine.sac-cas.ch/api/1/poi/search"
PORTAL_BASE = "https://www.sac-cas.ch/de/huetten-und-touren/sac-tourenportal"
LANG = "de"
PAGE_SIZE = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS topos (
    id          INTEGER PRIMARY KEY,  -- SAC route ID (routes[].id from API)
    summit_id   INTEGER,              -- SAC summit/POI ID
    url         TEXT NOT NULL,        -- sac-cas.ch summit portal page
    title       TEXT,                 -- "Summit Name: Route Title"
    category    TEXT,                 -- always "alpine_tour"
    region      TEXT,                 -- SAC region name (German)
    grade       TEXT,                 -- SAC difficulty: L/WS/ZS/S/SS/AS/ES (+ modifiers)
    departure   TEXT,                 -- NULL (not in API)
    timing      TEXT,                 -- "↑ Xh  ↓ Xh"
    altitude    INTEGER,              -- summit altitude (m)
    latitude    REAL,                 -- WGS84, exact from API
    longitude   REAL,
    full_text   TEXT,
    scraped_at  TEXT
);

CREATE TABLE IF NOT EXISTS topo_images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topo_id     INTEGER REFERENCES topos(id),
    image_url   TEXT,
    caption     TEXT,
    is_diagram  INTEGER DEFAULT 0,
    UNIQUE (topo_id, image_url)
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(DB_SCHEMA)
    conn.commit()
    return conn


def already_scraped(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute("SELECT id FROM topos").fetchall()}


def lv95_to_wgs84(e: float, n: float) -> tuple[float, float]:
    """Convert Swiss LV95 (EPSG:2056) to WGS84 lat/lon.
    Swisstopo approximate formula, accurate to ~1m.
    https://www.swisstopo.admin.ch/content/swisstopo-internet/en/topics/survey/
    """
    e_ = (e - 2_600_000) / 1_000_000
    n_ = (n - 1_200_000) / 1_000_000
    lon = (2.6779094
           + 4.728982 * e_
           + 0.791484 * e_ * n_
           + 0.1306 * e_ * n_**2
           - 0.0436 * e_**3) * 100 / 36
    lat = (16.9023892
           + 3.238272 * n_
           - 0.270978 * e_**2
           - 0.002528 * n_**2
           - 0.0447 * e_**2 * n_
           - 0.0140 * n_**3) * 100 / 36
    return lat, lon


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    return BeautifulSoup(text, "lxml").get_text(separator="\n", strip=True)


def format_time(t_min: int | None, t_max: int | None) -> str:
    if t_min is None and t_max is None:
        return ""

    def hm(mins: int) -> str:
        h, m = divmod(mins, 60)
        return f"{h}h{m:02d}" if m else f"{h}h"

    if t_min is None:
        return hm(t_max)
    if t_max is None or t_min == t_max:
        return hm(t_min)
    return f"{hm(t_min)}–{hm(t_max)}"


def build_full_text(summit: dict, route: dict) -> str:
    parts = []

    name = summit.get("display_name", "")
    alt = summit.get("altitude")
    region = summit.get("regions_denormalization", "")
    header = name
    if alt:
        header += f" ({int(alt)}m)"
    if region:
        header += f" — {region}"
    if header:
        parts.append(header)

    for field in ("description_summer", "description_winter"):
        text = strip_html(summit.get(field))
        if text:
            parts.append(text)

    route_lines = []
    if route.get("title"):
        route_lines.append(f"Route: {route['title']}")
    if route.get("main_difficulty"):
        route_lines.append(f"Schwierigkeit: {route['main_difficulty']}")
    if route.get("ascent_altitude"):
        route_lines.append(f"Höhenmeter Aufstieg: {route['ascent_altitude']}m")
    at = format_time(route.get("ascent_time_min"), route.get("ascent_time_max"))
    if at:
        route_lines.append(f"Aufstiegszeit: {at}")
    dt = format_time(route.get("descent_time_min"), route.get("descent_time_max"))
    if dt:
        route_lines.append(f"Abstiegszeit: {dt}")
    if route_lines:
        parts.append("\n".join(route_lines))

    return "\n\n".join(parts)


def fetch_page(session: requests.Session, cursor: int) -> list[dict]:
    resp = session.get(
        API_BASE,
        params={
            "lang": LANG,
            "output_lang": LANG,
            "cursor": cursor,
            "order_by": "-first_time_published",
            "disciplines": "alpine_tour",
            "hut_type": "all",
            "mode": "per_discipline",
            "limit": PAGE_SIZE,
        },
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def process_summit(summit: dict, done: set[int]) -> list[tuple[dict, list[dict]]]:
    """Return (route_data, images) for each new route on this summit."""
    routes = summit.get("routes") or []
    if not routes:
        return []

    lat = lon = None
    geom = summit.get("geom")
    if geom and geom.get("type") == "Point":
        coords = geom.get("coordinates", [])
        if len(coords) >= 2:
            lat, lon = lv95_to_wgs84(coords[0], coords[1])

    images = []
    for photo in summit.get("photos") or []:
        p = photo.get("photo") or {}
        img_url = p.get("url") or p.get("filename")
        caption = photo.get("caption") or p.get("caption") or ""
        if img_url:
            images.append({"url": img_url, "caption": caption})

    results = []
    for route in routes:
        route_id = route.get("id")
        if not route_id or route_id in done:
            continue

        summit_name = summit.get("display_name", "")
        route_title = route.get("title", "")
        title = f"{summit_name}: {route_title}" if route_title else summit_name

        _timing_parts = []
        at = format_time(route.get("ascent_time_min"), route.get("ascent_time_max"))
        dt = format_time(route.get("descent_time_min"), route.get("descent_time_max"))
        if at:
            _timing_parts.append(f"↑ {at}")
        if dt:
            _timing_parts.append(f"↓ {dt}")
        timing = "  ".join(_timing_parts)

        data = {
            "id": route_id,
            "summit_id": summit["id"],
            "url": f"{PORTAL_BASE}/{summit['id']}/alpine_tour",
            "title": title,
            "category": "alpine_tour",
            "region": summit.get("regions_denormalization") or "",
            "grade": route.get("main_difficulty") or "",
            "departure": None,
            "timing": timing,
            "altitude": int(summit["altitude"]) if summit.get("altitude") else None,
            "latitude": lat,
            "longitude": lon,
            "full_text": build_full_text(summit, route),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        results.append((data, images))

    return results


def insert_route(conn: sqlite3.Connection, data: dict, images: list[dict]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO topos
           (id, summit_id, url, title, category, region, grade, departure, timing,
            altitude, latitude, longitude, full_text, scraped_at)
           VALUES (:id, :summit_id, :url, :title, :category, :region, :grade, :departure,
                   :timing, :altitude, :latitude, :longitude, :full_text, :scraped_at)""",
        data,
    )
    for img in images:
        conn.execute(
            "INSERT OR IGNORE INTO topo_images (topo_id, image_url, caption) VALUES (?, ?, ?)",
            (data["id"], img["url"], img["caption"]),
        )
    conn.commit()


def run(db_path: str, limit: int | None, delay: float) -> None:
    conn = init_db(db_path)
    done = already_scraped(conn)
    session = requests.Session()

    ok = 0
    cursor = 0
    print("Fetching SAC alpine tours from API...")

    while True:
        summits = fetch_page(session, cursor)
        if not summits:
            break

        for summit in summits:
            for data, images in process_summit(summit, done):
                insert_route(conn, data, images)
                grade = data["grade"] or "?"
                print(f"  [{data['id']}] {data['title']} — {grade} ({data['timing']})")
                done.add(data["id"])
                ok += 1
                if limit and ok >= limit:
                    break
            if limit and ok >= limit:
                break

        print(f"  cursor={cursor}: {len(summits)} summits fetched (routes total: {ok})")
        cursor += len(summits)

        if len(summits) < PAGE_SIZE or (limit and ok >= limit):
            break

        time.sleep(delay)

    conn.close()
    print(f"\nDone. Routes stored: {ok}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape SAC alpine tours via API")
    parser.add_argument("--db", default="data/sac.db")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max routes to store in this run (default: all)")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Seconds between API pages (default: 0.5)")
    args = parser.parse_args()
    run(args.db, args.limit, args.delay)


if __name__ == "__main__":
    main()
