"""
Camptocamp API client.

Read-only access to https://api.camptocamp.org — no authentication required.
All geographic coordinates use EPSG:3857 (Web Mercator) for bbox queries.
"""

import math
import time
import requests_cache

BASE_URL = "https://api.camptocamp.org"
_DEFAULT_LANG = "fr"  # Covers the most Alpine routes; used in all API requests

_session = requests_cache.CachedSession("c2c_cache", expire_after=3600)
_session.headers["User-Agent"] = "mountaineering-reco-dev/0.1"

_last_request_time: float = 0.0
_MIN_REQUEST_INTERVAL = 0.25  # seconds between real (non-cached) API calls


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _pick_locale(locales: list[dict]) -> dict:
    """Return the French locale from a locales list, falling back to the first available."""
    return next((l for l in locales if l.get("lang") == _DEFAULT_LANG), locales[0] if locales else {})


def _fetch_json(path: str, params: dict | None = None) -> dict:
    """
    Send a GET request to the Camptocamp API and return the parsed JSON response.

    Adds the preferred-language parameter (pl=fr) automatically so that localized
    text fields — titles, descriptions, conditions — come back in French.
    Responses are cached for 1 hour. Real network calls are rate-limited to at
    most one per _MIN_REQUEST_INTERVAL seconds; cache hits skip the wait.
    """
    global _last_request_time
    params = {**(params or {})}
    params.setdefault("pl", _DEFAULT_LANG)

    elapsed = time.time() - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)

    response = _session.get(f"{BASE_URL}{path}", params=params)
    response.raise_for_status()

    if not getattr(response, "from_cache", True):
        _last_request_time = time.time()

    return response.json()


def latlon_bbox_to_mercator(lon_min: float, lat_min: float, lon_max: float, lat_max: float) -> str:
    """
    Convert a WGS84 (lat/lon) bounding box to an EPSG:3857 (Web Mercator) bbox string.

    The Camptocamp API requires geographic filters in Web Mercator coordinates,
    not plain lat/lon. Returns a comma-separated string "x_min,y_min,x_max,y_max".
    """
    def _to_mercator(lon: float, lat: float) -> tuple[float, float]:
        x = lon * 20037508.34 / 180
        y = math.log(math.tan((90 + lat) * math.pi / 360)) / (math.pi / 180)
        return x, y * 20037508.34 / 180

    x1, y1 = _to_mercator(lon_min, lat_min)
    x2, y2 = _to_mercator(lon_max, lat_max)
    return f"{int(x1)},{int(y1)},{int(x2)},{int(y2)}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_routes(
    bbox_mercator: str,
    activities: list[str] | None = None,
    offset: int = 0,
    page_size: int = 100,
) -> tuple[list[dict], int]:
    """
    Fetch one page of routes sorted by quality (best-documented first).

    Args:
        bbox_mercator: Geographic bounds in EPSG:3857 — build with latlon_bbox_to_mercator().
        activities: Optional activity filter. Valid values: skitouring, ice_climbing,
                    rock_climbing, mountain_climbing, snow_ice_mixed, hiking, via_ferrata.
        offset: Starting index for pagination (0 = first page).
        page_size: Number of routes to fetch (API cap is 100).

    Returns:
        (routes, total) where routes is a list of route dicts and total is the
        API-reported total matching the query (use to detect when pages are exhausted).
        Each route dict has: document_id, title, title_prefix, summary, activities,
        quality, global_rating, and discipline-specific grade fields.
    """
    params: dict = {
        "bbox": bbox_mercator,
        "sort": "-quality",
        "limit": page_size,
        "offset": offset,
    }
    if activities:
        params["act"] = ",".join(activities)

    data = _fetch_json("/routes", params)
    routes = data.get("documents", [])
    total = data.get("total", 0)

    for route in routes:
        locale = _pick_locale(route.get("locales", []))
        route["title"] = locale.get("title")
        route["title_prefix"] = locale.get("title_prefix")
        route["summary"] = locale.get("summary")

    return routes, total


def fetch_route(route_id: int) -> dict:
    """
    Fetch full details for a single route.

    Returns a dict with all grade fields, elevation data, geometry, and a
    "_locale" key containing the French locale (description, approach notes,
    gear list, etc.). Use search_routes() first to get candidate route IDs.
    """
    data = _fetch_json(f"/routes/{route_id}")
    data["_locale"] = _pick_locale(data.get("locales", []))
    return data


def fetch_outing_stubs(route_id: int, limit: int = 200) -> list[dict]:
    """
    Fetch outing stubs for a route without loading conditions text.

    Each stub includes document_id, date_start, date_end, condition_rating,
    activities, global_rating, elevation_max, and author. Used for building
    the date distribution (seasonality) and selecting which outings to read
    in full before making an LLM call.

    Makes 1–2 API calls (pages of 100). Responses are cached.
    """
    stubs: list[dict] = []
    page_size = 100
    while len(stubs) < limit:
        page = _fetch_json("/outings", {
            "r": route_id,
            "limit": min(page_size, limit - len(stubs)),
            "offset": len(stubs),
        }).get("documents", [])
        stubs.extend(page)
        if len(page) < page_size:
            break
    return stubs


def fetch_outing_full(outing_id: int) -> dict:
    """
    Fetch full detail for a single outing, including conditions text.

    Returns all fields plus a "_locale" key with the French locale
    (conditions, weather, timing free-text fields).
    """
    outing = _fetch_json(f"/outings/{outing_id}")
    outing["_locale"] = _pick_locale(outing.get("locales", []))
    return outing
