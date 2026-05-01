"""
Build the mountain ranges lookup table from GMBA and enrich source DBs.

Outputs:
  data/ranges_lookup.json  — compact lookup for runtime use
  gmba_id + gmba_ancestry columns added to summitpost, sac, refuges, hikr,
  passion_alpes, lemkeclimbs DBs.

Usage:
  python scripts/precompute_ranges.py
"""

import json
import math
import re
import sqlite3
import time
from pathlib import Path

import geopandas as gpd
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
from shapely.geometry import Point

ROOT        = Path(__file__).parent.parent
SHAPEFILE   = ROOT / "_external" / "GMBA_Inventory_v2.0_standard" / "GMBA_Inventory_v2.0_standard.shp"
LOOKUP_OUT  = ROOT / "data" / "ranges_lookup.json"
DATA_DIR    = ROOT / "data"

GEOCODE_DELAY = 1.1   # seconds between Nominatim requests
BUFFER_DEG    = 0.15  # ~15 km buffer for geocoded points


# ---------------------------------------------------------------------------
# Load shapefile
# ---------------------------------------------------------------------------

def load_basic_polygons() -> gpd.GeoDataFrame:
    print("Loading GMBA shapefile…")
    gdf = gpd.read_file(SHAPEFILE)
    basic = gdf[gdf["MapUnit"] == "Basic"].copy()
    basic = basic.set_index("GMBA_V2_ID")
    print(f"  {len(basic)} Basic polygons loaded")
    return basic


# ---------------------------------------------------------------------------
# Export ranges_lookup.json
# ---------------------------------------------------------------------------

def _nan_to_none(val):
    """Convert float NaN (from shapefile) to None; pass other values through."""
    if isinstance(val, float) and math.isnan(val):
        return None
    return val or None


def _ancestry_fallback(ancestry_en: str | None) -> str | None:
    """Extract the last named segment from a GMBA ancestry path as a fallback name.

    e.g. "Alps > Western Alps > Mont Blanc Massif" -> "Mont Blanc Massif"
         "Alps > Glarus Alps > Glarus Alps (nn)"   -> "Glarus Alps"
    """
    if not ancestry_en or not isinstance(ancestry_en, str):
        return None
    last = ancestry_en.strip().rsplit(" > ", 1)[-1].strip()
    last = re.sub(r"\s*\([a-z]{2,3}\)\s*$", "", last).strip()  # strip language codes like (nn)
    last = last.rstrip("*").strip()                              # strip GMBA uncertainty marker
    return last or None


def export_lookup(basic: gpd.GeoDataFrame) -> None:
    print("Exporting ranges_lookup.json…")
    lookup = {}
    for gmba_id, row in basic.iterrows():
        # Parse LocalNames: "Name (Language); Name2 (Language2)"
        local = []
        if row.get("LocalNames"):
            for part in str(row["LocalNames"]).split(";"):
                part = part.strip()
                m = re.match(r"^(.+?)\s*\([^)]+\)$", part)
                name = m.group(1).strip() if m else part
                if name and name.lower() != "nan":
                    local.append(name)

        name_en = _nan_to_none(row.get("Name_EN"))
        name_fr = _nan_to_none(row.get("Name_FR"))
        name_de = _nan_to_none(row.get("Name_DE"))
        ancestry_en = _nan_to_none(row.get("Path"))

        # Fall back to ancestry name when no official name exists
        if not name_en and not name_fr and not name_de and not local:
            name_en = _ancestry_fallback(ancestry_en)

        centroid = row["geometry"].centroid
        lookup[int(gmba_id)] = {
            "name_en":      name_en,
            "name_fr":      name_fr,
            "name_de":      name_de,
            "local_names":  local,
            "centroid_lon": round(centroid.x, 5),
            "centroid_lat": round(centroid.y, 5),
            "ancestry_ids": _nan_to_none(row.get("Path_ID")),
            "ancestry_en":  ancestry_en,
        }

    LOOKUP_OUT.write_text(json.dumps(lookup, ensure_ascii=False, indent=2))
    print(f"  Written {len(lookup)} entries → {LOOKUP_OUT}")


# ---------------------------------------------------------------------------
# Point-in-polygon lookup
# ---------------------------------------------------------------------------

def lookup_point(lat: float, lon: float, basic: gpd.GeoDataFrame) -> tuple[int | None, str | None]:
    pt = Point(lon, lat)
    hits = basic[basic.geometry.contains(pt)]
    if hits.empty:
        return None, None
    row = hits.iloc[0]
    return int(row.name), row.get("Path_ID")


# ---------------------------------------------------------------------------
# Geocode + buffer lookup (for text regions)
# ---------------------------------------------------------------------------

def make_geocoder():
    geolocator = Nominatim(user_agent="mountaineering_reco_precompute")
    return RateLimiter(geolocator.geocode, min_delay_seconds=GEOCODE_DELAY)


def _region_query(text: str) -> str:
    """Extract the leaf segment from a hikr-style breadcrumb path."""
    if "»" in text:
        return text.strip().split("»")[-1].strip().replace("\n", "").strip()
    return text.strip()


def lookup_text_region(text: str, geocode, basic: gpd.GeoDataFrame) -> tuple[str | None, str | None]:
    """
    Geocode a region name, buffer, find all intersecting Basic polygons.
    Returns comma-separated gmba_ids and a joined ancestry string.
    """
    query = _region_query(text)
    try:
        loc = geocode(query, language="en", timeout=10)
    except Exception:
        return None, None
    if not loc:
        return None, None

    pt = Point(loc.longitude, loc.latitude)
    buf = pt.buffer(BUFFER_DEG)
    hits = basic[basic.geometry.intersects(buf)]
    if hits.empty:
        return None, None

    ids = [str(int(i)) for i in hits.index]
    paths = [str(hits.loc[i, "Path_ID"]) for i in hits.index if hits.loc[i, "Path_ID"]]
    return ",".join(ids), " | ".join(paths)


# ---------------------------------------------------------------------------
# Per-DB enrichment
# ---------------------------------------------------------------------------

def migrate(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(huts)"
                                                if _has_table(conn, "huts")
                                                else "PRAGMA table_info(routes)")}
    # handled per table below


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def add_columns(conn: sqlite3.Connection, table: str) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, typ in [("gmba_id", "TEXT"), ("gmba_ancestry", "TEXT")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()


def _pk(conn: sqlite3.Connection, table: str) -> str:
    """Return the primary key column name for a table."""
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if row[5] == 1:   # pk flag
            return row[1]
    return "id"


def enrich_coords(db_path: Path, table: str, basic: gpd.GeoDataFrame,
                  lat_col: str = "lat", lon_col: str = "lon") -> None:
    """Enrich documents that already have lat/lon coordinates."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    add_columns(conn, table)
    pk = _pk(conn, table)

    rows = conn.execute(
        f"SELECT {pk}, {lat_col}, {lon_col} FROM {table}"
        f" WHERE {lat_col} IS NOT NULL AND {lon_col} IS NOT NULL AND gmba_id IS NULL"
    ).fetchall()
    print(f"  {db_path.name}/{table}: {len(rows)} rows to enrich by coordinates")

    for row in rows:
        gmba_id, ancestry = lookup_point(row[lat_col], row[lon_col], basic)
        conn.execute(
            f"UPDATE {table} SET gmba_id=?, gmba_ancestry=? WHERE {pk}=?",
            (str(gmba_id) if gmba_id else None, ancestry, row[pk])
        )

    conn.commit()
    conn.close()


def enrich_text_region(db_path: Path, table: str, region_col: str,
                        basic: gpd.GeoDataFrame, geocode) -> None:
    """Enrich documents using a text region field via geocoding."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    add_columns(conn, table)
    pk = _pk(conn, table)

    rows = conn.execute(
        f"SELECT {pk}, {region_col} FROM {table} WHERE {region_col} IS NOT NULL AND gmba_id IS NULL"
    ).fetchall()
    # Deduplicate region values to minimise geocoding calls
    region_cache: dict[str, tuple] = {}
    unique_regions = {r[region_col] for r in rows}
    print(f"  {db_path.name}/{table}: {len(rows)} rows, {len(unique_regions)} unique regions to geocode")

    for region in unique_regions:
        if region not in region_cache:
            ids, ancestry = lookup_text_region(region, geocode, basic)
            region_cache[region] = (ids, ancestry)

    for row in rows:
        ids, ancestry = region_cache.get(row[region_col], (None, None))
        conn.execute(
            f"UPDATE {table} SET gmba_id=?, gmba_ancestry=? WHERE {pk}=?",
            (ids, ancestry, row[pk])
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    basic = load_basic_polygons()
    export_lookup(basic)

    geocode = make_geocoder()

    sources = [
        # (db_path, table, method, extra_arg)
        # method="coords"  → extra_arg=(lat_col, lon_col) or None for defaults
        # method="region"  → extra_arg=region_col_name
        (DATA_DIR / "summitpost.db",    "routes",  "coords", None),
        (DATA_DIR / "sac.db",           "topos",   "coords", ("latitude", "longitude")),
        (DATA_DIR / "refuges.db",       "huts",    "coords", None),
        (DATA_DIR / "hikr.db",          "reports", "region", "region"),
        (DATA_DIR / "passion_alpes.db", "topos",   "region", "region"),
        (DATA_DIR / "lemkeclimbs.db",   "topos",   "region", "area"),
    ]

    for db_path, table, method, extra in sources:
        if not db_path.exists():
            print(f"  Skipping {db_path.name} (not found)")
            continue
        print(f"\n{db_path.name}")
        if method == "coords":
            if extra:
                enrich_coords(db_path, table, basic, lat_col=extra[0], lon_col=extra[1])
            else:
                enrich_coords(db_path, table, basic)
        else:
            enrich_text_region(db_path, table, extra, basic, geocode)

    print("\nDone.")


if __name__ == "__main__":
    main()
