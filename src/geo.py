"""
Geographic utilities: range classification and location geocoding.

classify_range(lat, lon)     → range key used by weather.py for season windows.
geocode_location(text)       → GeocodingResult dict or None, with persistent cache.

GeocodingResult shape:
    {
        "lat":          float,
        "lon":          float,
        "display_name": str,    # full Nominatim location string
        "osm_class":    str,    # e.g. "natural", "place", "tourism"
        "osm_type":     str,    # e.g. "peak", "village", "alpine_hut"
        "importance":   float | None,  # Nominatim 0–1 prominence score
        "query_used":   str,    # which query string produced this result
    }
"""

from __future__ import annotations

import json
import math
import time
from functools import lru_cache
from pathlib import Path

import requests
import requests_cache
import yaml

from src.spatial import point_in_geometry

_SNOW_SEASONS_PATH = Path(__file__).parent.parent / "domain_knowledge" / "snow_seasons.yaml"

# GMBA ancestor IDs for ranges whose yaml entry is null (no season window),
# but which we still want to classify for downstream use.
_GMBA_NULL_RANGE_IDS: dict[int, str] = {
    11400: "himalaya",
    12123: "northern_andes",
}

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


def _classify_alps_pyrenees(lat: float, lon: float) -> str | None:
    """Return 'alps', 'pyrenees', or None if the point isn't in the French massif GeoJSON."""
    for feature in _load_massif_features():
        if point_in_geometry(lat, lon, feature["geometry"]):
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
# GMBA ancestry → snow season key
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _build_gmba_season_lookup() -> dict[int, str]:
    """Build a {gmba_ancestor_id: season_key} dict from snow_seasons.yaml."""
    seasons = yaml.safe_load(_SNOW_SEASONS_PATH.read_text())["ranges"]
    lookup: dict[int, str] = {}
    for key, entry in seasons.items():
        if entry and isinstance(entry, dict):
            for gid in entry.get("gmba_ancestor_ids") or []:
                lookup[int(gid)] = key
    lookup.update(_GMBA_NULL_RANGE_IDS)
    return lookup


def gmba_ancestry_to_season_key(ancestry: str) -> str:
    """
    Map a GMBA ancestry string to a snow_season key.

    Accepts single-chain format ("12155 > 10001 > 10005 > 10012") or
    pipe-separated multi-massif format (as stored in gmba_ancestry for
    text-region enriched documents: "12155 > ... > 10012 | 12155 > ... > 10061").
    Returns "unknown" if no ancestor ID matches any known range.
    """
    if not ancestry:
        return "unknown"
    lookup = _build_gmba_season_lookup()
    for chain in ancestry.split("|"):
        for part in chain.split(">"):
            part = part.strip()
            if part.isdigit():
                key = lookup.get(int(part))
                if key:
                    return key
    return "unknown"


# ---------------------------------------------------------------------------
# Nominatim geocoding with persistent cache
# ---------------------------------------------------------------------------

_GEOCODE_CACHE_PATH = Path(".cache/geocoding_cache.json")
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_HEADERS = {"User-Agent": "mountaineering-reco/1.0"}
_geocode_cache: dict | None = None
_last_nominatim_request: float = 0.0
_nominatim_session = requests_cache.CachedSession(".cache/nominatim_cache", expire_after=86400 * 30)


def _load_geocode_cache() -> dict:
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


# Classes that are never relevant to mountaineering — always skip.
# Intentionally minimal: altitude restaurants, mountain huts, cable-car stations,
# villages as trailheads are all legitimate and must not be filtered out.
_COMMERCIAL_CLASSES = frozenset({"shop", "office", "industrial"})


def _passes_permissive(hit: dict) -> bool:
    """Minimal filter for full-context queries: reject only clearly commercial results."""
    return hit.get("class", "") not in _COMMERCIAL_CLASSES


def _passes_strict(hit: dict) -> bool:
    """
    Tighter filter for isolated single-word queries where ambiguity is high.

    Accepts natural features unconditionally. Accepts geographic boundaries
    (national parks, protected areas) but NOT administrative boundaries, which
    are typically urban/political divisions (e.g. Melbourne's Fitzroy suburb is
    tagged boundary/administrative in OSM). Everything else requires importance > 0.4.
    """
    cls = hit.get("class", "")
    typ = hit.get("type", "")
    if cls in _COMMERCIAL_CLASSES:
        return False
    if cls == "natural":
        return True  # peaks, mountain ranges, valleys — always accept
    if cls == "boundary":
        # Administrative boundaries are urban/political (e.g. Melbourne's Fitzroy suburb
        # is tagged boundary/administrative). National parks and protected areas are fine.
        return typ != "administrative"
    try:
        return float(hit.get("importance") or 0) > 0.4
    except (TypeError, ValueError):
        return False


def _nominatim_query(query: str, strict: bool = False) -> dict | None:
    """
    Single Nominatim request, rate-limited to 1 req/sec (ToS requirement).

    Fetches up to 5 candidates and returns full metadata for the first hit that
    passes the filter. `strict=True` applies the tighter isolated-segment filter
    for single-word queries where name collisions are most dangerous.

    Returns a GeocodingResult dict or None.
    """
    global _last_nominatim_request
    elapsed = time.time() - _last_nominatim_request
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    try:
        r = _nominatim_session.get(
            _NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 5},
            headers=_NOMINATIM_HEADERS,
            timeout=10,
        )
        _last_nominatim_request = time.time()
        r.raise_for_status()
        filter_fn = _passes_strict if strict else _passes_permissive
        for hit in r.json():
            if filter_fn(hit):
                importance = hit.get("importance")
                return {
                    "lat":          float(hit["lat"]),
                    "lon":          float(hit["lon"]),
                    "display_name": hit.get("display_name", ""),
                    "osm_class":    hit.get("class", ""),
                    "osm_type":     hit.get("type", ""),
                    "importance":   float(importance) if importance is not None else None,
                    "query_used":   query,
                }
    except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
        print(f"[geo] Nominatim query failed for {query!r}: {exc}")
    return None


def bbox_around(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """Return (lat_min, lat_max, lon_min, lon_max) bounding a circle of radius_km."""
    delta_lat = radius_km / 111.0
    delta_lon = radius_km / (111.0 * math.cos(math.radians(lat)))
    return lat - delta_lat, lat + delta_lat, lon - delta_lon, lon + delta_lon


def geocode_location(location_text: str) -> dict | None:
    """
    Translate a place name to a GeocodingResult dict via Nominatim.

    Query strategy for slash-separated strings like
    "Patagonia / Fitzroy" or "Massif du Mont-Blanc / Aiguilles de Chamonix":

    1. Full context string (segments joined with spaces) — permissive filter.
       Most context; avoids single-word collisions like "Fitzroy" → Melbourne.
    2. Rightmost segment alone — strict filter.
       Fallback when combined string finds nothing.
    3. Leftmost segment alone — strict filter.
       Broadest fallback.

    Results (including metadata) are cached permanently so confidence signals
    are preserved across sessions. Legacy cache entries in the old [lat, lon]
    list format are read back with osm_class="unknown" without crashing.
    """
    cache = _load_geocode_cache()
    if location_text in cache:
        cached = cache[location_text]
        # Backward compatibility: old cache stored bare [lat, lon] lists
        if isinstance(cached, list):
            return {
                "lat":          float(cached[0]),
                "lon":          float(cached[1]),
                "display_name": "unknown (legacy cache entry — re-geocode for full metadata)",
                "osm_class":    "unknown",
                "osm_type":     "unknown",
                "importance":   None,
                "query_used":   "unknown",
            }
        return cached

    segments = [s.strip() for s in location_text.split("/") if s.strip()]
    if not segments:
        return None

    if len(segments) >= 2:
        query_attempts = [
            (" ".join(segments), False),  # full context, permissive
            (segments[-1],       True),   # rightmost segment, strict
            (segments[0],        True),   # leftmost segment, strict
        ]
    else:
        query_attempts = [(segments[0], False)]  # single segment, permissive

    result = None
    for query, strict in query_attempts:
        result = _nominatim_query(query, strict=strict)
        if result:
            break

    if result:
        cache[location_text] = result
        _save_geocode_cache()

    return result
