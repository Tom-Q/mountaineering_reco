"""
Camptocamp API client.

Read-only access to https://api.camptocamp.org — no authentication required.
All geographic coordinates use EPSG:3857 (Web Mercator) for bbox queries.
"""

import math
import requests_cache

BASE_URL = "https://api.camptocamp.org"
_DEFAULT_LANG = "fr"  # Covers the most Alpine routes; used in all API requests

_session = requests_cache.CachedSession("c2c_cache", expire_after=3600)
_session.headers["User-Agent"] = "mountaineering-reco-dev/0.1"


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
    Responses are cached for 1 hour so repeated calls during development don't
    hit the network.
    """
    params = params or {}
    params.setdefault("pl", _DEFAULT_LANG)  # pl = preferred language for localized fields
    response = _session.get(f"{BASE_URL}{path}", params=params)
    response.raise_for_status()
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

def search_routes(bbox_mercator: str, activities: list[str] | None = None, limit: int = 50) -> list[dict]:
    """
    Search for routes within a geographic area and return lightweight stubs.

    Returns route stubs — just enough to triage candidates. Use fetch_route()
    for full details and fetch_outings() for conditions reports.

    Args:
        bbox_mercator: Geographic bounds in EPSG:3857 — build with latlon_bbox_to_mercator().
        activities: Optional activity filter. Valid values: skitouring, ice_climbing,
                    rock_climbing, mountain_climbing, snow_ice_mixed, hiking, via_ferrata.
                    If None, all activities are returned.
        limit: Max routes per page (API cap is ~100).

    Returns:
        List of route dicts. Each has: document_id, title, title_prefix, summary,
        activities, quality, global_rating, and discipline-specific grade fields
        (rock_free_rating, ice_rating, mixed_rating, ski_rating, engagement_rating, etc.).
    """
    params: dict = {"bbox": bbox_mercator, "limit": limit}
    if activities:
        params["act"] = ",".join(activities)  # "act" is Camptocamp's parameter name for activity type

    data = _fetch_json("/routes", params)
    routes = data.get("documents", [])

    # Flatten localized text fields into the top-level dict for easier downstream access
    for route in routes:
        locale = _pick_locale(route.get("locales", []))
        route["title"] = locale.get("title")
        route["title_prefix"] = locale.get("title_prefix")
        route["summary"] = locale.get("summary")

    return routes


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


def fetch_outings(route_id: int, limit: int = 10) -> list[dict]:
    """
    Fetch recent trip reports (outings) for a route, newest first.

    Outings are the primary source of conditions information. Each outing
    has structured fields (condition_rating, snow_quality, avalanche_signs, etc.)
    and a "_locale" key with free-text fields (conditions, weather, timing).

    Note: makes N+1 API calls — one to list stubs, then one per outing to get the
    full detail including condition text (the list endpoint omits it). Responses
    are cached, so this is cheap during development.

    Args:
        route_id: Camptocamp route document_id (from search_routes or fetch_route).
        limit: Max number of outings to fetch.
    """
    stubs = _fetch_json("/outings", {"r": route_id, "limit": limit}).get("documents", [])

    outings = []
    for stub in stubs:
        try:
            outing = _fetch_json(f"/outings/{stub['document_id']}")
            outing["_locale"] = _pick_locale(outing.get("locales", []))
            outings.append(outing)
        except Exception:
            # Skip outings that fail individually (deleted document, permission issue, etc.)
            continue

    return outings
