"""Dump a scraped route from the DB in readable form for manual comparison.

Usage:
    python scrapers/summitpost_inspect.py <url-or-sp-id>

Examples:
    python scrapers/summitpost_inspect.py 155420
    python scrapers/summitpost_inspect.py https://www.summitpost.org/coleman-deming-glaciers/155420
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path


def find_route(conn, query: str):
    # Try numeric ID first
    m = re.search(r"(\d+)$", query.rstrip("/"))
    if m:
        row = conn.execute("SELECT * FROM routes WHERE sp_id = ?", (int(m.group(1)),)).fetchone()
        if row:
            return row
    # Fall back to URL substring match
    row = conn.execute("SELECT * FROM routes WHERE url LIKE ?", (f"%{query}%",)).fetchone()
    return row


def main():
    parser = argparse.ArgumentParser(description="Inspect a scraped SummitPost route")
    parser.add_argument("query", help="Route URL or numeric sp_id")
    parser.add_argument("--db", default="data/summitpost.db")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    route = find_route(conn, args.query)
    if not route:
        print(f"Route not found: {args.query}")
        sys.exit(1)

    sp_id = route["sp_id"]

    print("=" * 70)
    print(f"  {route['name']}")
    print(f"  {route['url']}")
    print("=" * 70)
    print(f"  sp_id        : {sp_id}")
    print(f"  lat/lon      : {route['lat']}, {route['lon']}")
    print(f"  location     : {route['location']}")
    print(f"  route_type   : {route['route_type']}")
    print(f"  difficulty   : {route['difficulty']}")
    print(f"  time_required: {route['time_required']}")
    print(f"  views        : {route['views']}")
    print(f"  score        : {route['score']}  votes: {route['votes']}")
    print(f"  scraped_at   : {route['scraped_at']}")

    props = json.loads(route["properties"] or "{}")
    if props:
        print("\n  --- Extra properties ---")
        for k, v in props.items():
            print(f"  {k:<20} {v}")

    sections = conn.execute(
        "SELECT heading, body, position FROM sections WHERE route_id = ? ORDER BY position",
        (sp_id,),
    ).fetchall()

    print(f"\n  --- Sections ({len(sections)}) ---")
    if not sections:
        print("  (none)")
    for s in sections:
        print(f"\n  [{s['position']}] {s['heading']}")
        print("  " + "-" * 60)
        for line in s["body"].splitlines():
            print(f"  {line}")

    images = conn.execute(
        "SELECT remote_url FROM images WHERE route_id = ?", (sp_id,)
    ).fetchall()
    print(f"\n  --- Cover image ---")
    if images:
        for img in images:
            print(f"  {img['remote_url']}")
    else:
        print("  (none)")

    conn.close()


if __name__ == "__main__":
    main()
