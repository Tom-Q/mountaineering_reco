"""
Geographic utilities: range classification and location geocoding.

classify_range(lat, lon)     → range key used by weather.py for season windows.
geocode_location(text)       → (lat, lon) via Nominatim, with persistent cache.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Alps/Pyrenees: point-in-polygon against the French massif GeoJSON
# (same file used by src/avalanche.py for bulletin lookup)
# ---------------------------------------------------------------------------

_MASSIF_GEOJSON = Path(__file__).parent.parent / "domain_knowledge" / "liste-massifs.geojson"
_massif_features: list | None = None


def _load_massif_features() -> list:
    global _massif_features
    if _massif_features is None:
        try:
            with open(_MASSIF_GEOJSON, encoding="utf-8") as f:
                _massif_features = json.load(f)["features"]
        except (OSError, KeyError, json.JSONDecodeError):
            _massif_features = []
    return _massif_features


def _ray_cast(lat: float, lon: float, ring: list) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]   # GeoJSON stores [lon, lat]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def _point_in_polygon(lat: float, lon: float, rings: list) -> bool:
    if not rings:
        return False
    if not _ray_cast(lat, lon, rings[0]):
        return False
    for hole in rings[1:]:
        if _ray_cast(lat, lon, hole):
            return False
    return True


def _point_in_multipolygon(lat: float, lon: float, geometry: dict) -> bool:
    gtype = geometry.get("type", "")
    coords = geometry.get("coordinates", [])
    if gtype == "Polygon":
        return _point_in_polygon(lat, lon, coords)
    if gtype == "MultiPolygon":
        return any(_point_in_polygon(lat, lon, poly) for poly in coords)
    return False


def _classify_alps_pyrenees(lat: float, lon: float) -> str | None:
    """Return 'alps', 'pyrenees', or None if the point isn't in the French massif GeoJSON."""
    for feature in _load_massif_features():
        if _point_in_multipolygon(lat, lon, feature["geometry"]):
            mountain = feature["properties"].get("mountain", "")
            if mountain == "Pyrenees":
                return "pyrenees"
            return "alps"
    return None


# ---------------------------------------------------------------------------
# Bounding-box table for all other ranges
# (lat_min, lon_min, lat_max, lon_max) — first match wins
# ---------------------------------------------------------------------------

_RANGE_BBOXES: list[tuple[str, float, float, float, float]] = [
    # Southern Hemisphere first — prevents NH boxes from claiming SH coords
    ("patagonia",        -56, -76, -39, -66),
    ("central_andes",    -35, -73, -18, -64),
    ("northern_andes",    -2, -80,  12, -66),
    ("new_zealand",      -47, 166, -34, 174),
    # Northern Hemisphere
    ("alaska",            54,-170,  72,-130),
    ("pacific_nw",        44,-125,  51,-117),
    ("sierra_nevada",     36,-122,  39,-118),
    ("colorado_rockies",  37,-109,  41,-104),
    ("appalachians",      35, -84,  48,  -66),
    ("mexico_volcanic",   18,-100,  21,  -96),
    ("scandinavia",       57,   4,  71,   31),
    ("caucasus",          41,  38,  44,   48),
    ("tian_shan",         40,  68,  45,   80),
    ("altai",             48,  82,  56,   90),
    ("hindu_kush",        34,  69,  38,   75),
    ("karakoram",         34,  72,  38,   78),
    ("himalaya",          26,  78,  36,   98),
    ("japan",             35, 136,  37,  138),  # Japanese Alps specifically
    # Broad Alps catch-all for Swiss, Austrian, Italian Alpine coords not covered
    # by the French massif GeoJSON (which handles only French territory via polygon).
    ("alps",              43,   5,  48,   17),
    ("pyrenees",          42,  -2,  44,    4),
]


def classify_range(lat: float, lon: float) -> str:
    """
    Return a range key for the given coordinates.

    Checks Alps/Pyrenees first via point-in-polygon against the French massif GeoJSON
    (most precise for those ranges), then walks the bounding-box table.
    Returns "unknown" if nothing matches.
    """
    alps_or_pyr = _classify_alps_pyrenees(lat, lon)
    if alps_or_pyr:
        return alps_or_pyr
    for name, lat_min, lon_min, lat_max, lon_max in _RANGE_BBOXES:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return name
    return "unknown"


# ---------------------------------------------------------------------------
# Nominatim geocoding with persistent cache
# ---------------------------------------------------------------------------

_GEOCODE_CACHE_PATH = Path(".cache/geocoding_cache.json")
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_HEADERS = {"User-Agent": "mountaineering-reco/1.0"}
_geocode_cache: dict[str, list[float]] | None = None
_last_nominatim_request: float = 0.0


def _load_geocode_cache() -> dict[str, list[float]]:
    global _geocode_cache
    if _geocode_cache is None:
        try:
            with open(_GEOCODE_CACHE_PATH, encoding="utf-8") as f:
                _geocode_cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _geocode_cache = {}
    return _geocode_cache


def _save_geocode_cache() -> None:
    _GEOCODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_GEOCODE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(_geocode_cache, f, indent=2)


def _nominatim_query(query: str) -> tuple[float, float] | None:
    """Single Nominatim request, rate-limited to 1 req/sec as required by ToS."""
    global _last_nominatim_request
    elapsed = time.time() - _last_nominatim_request
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    try:
        r = requests.get(
            _NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1},
            headers=_NOMINATIM_HEADERS,
            timeout=10,
        )
        _last_nominatim_request = time.time()
        r.raise_for_status()
        results = r.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    return None


def geocode_location(location_text: str) -> tuple[float, float] | None:
    """
    Translate a place name to (lat, lon) via Nominatim.

    For slash-separated strings like "Massif du Mont-Blanc / Aiguilles de Chamonix",
    tries the rightmost segment first (most specific), then falls back to the leftmost.
    Results are cached permanently in .cache/geocoding_cache.json — geography doesn't change.
    """
    cache = _load_geocode_cache()
    if location_text in cache:
        cached = cache[location_text]
        return float(cached[0]), float(cached[1])

    segments = [s.strip() for s in location_text.split("/") if s.strip()]
    if len(segments) >= 2:
        # Rightmost (most specific) first, leftmost (broader) as fallback
        queries = [segments[-1], segments[0]]
    elif segments:
        queries = [segments[0]]
    else:
        return None

    result = None
    for query in queries:
        result = _nominatim_query(query)
        if result:
            break

    if result:
        cache[location_text] = [result[0], result[1]]
        _save_geocode_cache()

    return result
