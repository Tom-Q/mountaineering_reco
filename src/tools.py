"""
Anthropic tool definitions and dispatcher for the tool-use architecture.

Each tool is defined as an Anthropic-format dict (name / description / input_schema)
and has a corresponding handler function. The dispatcher routes tool_use blocks
returned by the API to the right handler and returns a JSON-serializable result.

Usage:
    from src.tools import ALL_TOOLS, dispatch_tool

    # Pass ALL_TOOLS to the Anthropic messages API:
    #   client.messages.create(..., tools=ALL_TOOLS)

    # When the response contains tool_use blocks:
    #   for block in response.content:
    #       if block.type == "tool_use":
    #           result = dispatch_tool(block.name, block.input)
"""

from __future__ import annotations

from datetime import date
from typing import Any

from src.avalanche import fetch_avalanche_bulletin
from src.camptocamp import (
    fetch_outing_full,
    fetch_outing_stubs,
    fetch_route,
    latlon_bbox_to_mercator,
    search_routes,
    search_routes_by_name,
)
from src.weather import fetch_weather_for_coords, route_coords

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

WEATHER_TOOL: dict = {
    "name": "get_weather_forecast",
    "description": (
        "Fetch a 7-day weather forecast and a snowfall history for a location. "
        "Returns daily snowfall, wind speed, wind gusts, 0°C isotherm (refreeze and melt), "
        "night cloud cover, and min/max temperature. "
        "Snowfall history includes recent loading events (past 15 days) and, when in-season, "
        "total accumulation since the season start — windows are range-aware. "
        "Use this to assess whether a route is in safe condition weather-wise."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "latitude": {
                "type": "number",
                "description": "WGS84 latitude of the route location.",
            },
            "longitude": {
                "type": "number",
                "description": "WGS84 longitude of the route location.",
            },
            "elevation_m": {
                "type": "integer",
                "description": (
                    "Altitude of the route summit in metres. "
                    "Used for Open-Meteo altitude correction and isotherm warning threshold. "
                    "Omit if unknown."
                ),
            },
        },
        "required": ["latitude", "longitude"],
    },
}

AVALANCHE_TOOL: dict = {
    "name": "get_avalanche_bulletin",
    "description": (
        "Fetch the current avalanche danger bulletin for a location. "
        "Returns danger level (1–5), aspects at risk, elevation split if present, "
        "and a summary of snowpack conditions. "
        "Sources: Météo-France BRA for French massifs, EAWS CAAMLv6 feeds for "
        "Switzerland, Italy, and Austria. "
        "Use this when assessing avalanche risk for a route."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "latitude": {
                "type": "number",
                "description": "WGS84 latitude of the route location.",
            },
            "longitude": {
                "type": "number",
                "description": "WGS84 longitude of the route location.",
            },
        },
        "required": ["latitude", "longitude"],
    },
}

SEARCH_ROUTES_BY_NAME_TOOL: dict = {
    "name": "search_routes_by_name",
    "description": (
        "Search Camptocamp routes by name or keyword. "
        "Use when the user mentions a specific route by name (e.g. 'Frendo Spur', 'Gervasutti pillar'). "
        "Returns a list of matching routes with grades, elevation, and a short summary."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Route name or keyword to search for.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default 10, max 30).",
            },
        },
        "required": ["query"],
    },
}

SEARCH_ROUTES_BY_AREA_TOOL: dict = {
    "name": "search_routes_by_area",
    "description": (
        "Search Camptocamp routes within a geographic bounding box. "
        "Use when the user asks about routes in a particular area or massif "
        "(e.g. 'routes around Chamonix', 'ski tours in the Écrins'). "
        "Returns routes sorted by documentation quality, with grades and elevation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "lat_min": {"type": "number", "description": "Southern boundary (WGS84 latitude)."},
            "lat_max": {"type": "number", "description": "Northern boundary (WGS84 latitude)."},
            "lon_min": {"type": "number", "description": "Western boundary (WGS84 longitude)."},
            "lon_max": {"type": "number", "description": "Eastern boundary (WGS84 longitude)."},
            "activities": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional activity filter. Valid values: skitouring, ice_climbing, "
                    "rock_climbing, mountain_climbing, snow_ice_mixed, hiking, via_ferrata."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default 15, max 30).",
            },
        },
        "required": ["lat_min", "lat_max", "lon_min", "lon_max"],
    },
}

FETCH_ROUTE_TOOL: dict = {
    "name": "fetch_route",
    "description": (
        "Fetch full details for a Camptocamp route by its numeric ID. "
        "Returns the route description, approach notes, gear list, all grade fields, "
        "elevation, coordinates, and a link to the Camptocamp page. "
        "Use after search to get the full topo for a specific route."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "route_id": {
                "type": "integer",
                "description": "Camptocamp numeric route ID (visible in the URL: camptocamp.org/routes/XXXXXXX).",
            },
        },
        "required": ["route_id"],
    },
}

GET_OUTING_LIST_TOOL: dict = {
    "name": "get_outing_list",
    "description": (
        "Fetch the list of trip reports (outings) for a Camptocamp route. "
        "Returns each report's ID, date, and condition rating. "
        "Use this first to see the date distribution and choose which reports "
        "to read in full with get_outing_detail."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "route_id": {
                "type": "integer",
                "description": "Camptocamp numeric route ID.",
            },
        },
        "required": ["route_id"],
    },
}

GET_OUTING_DETAIL_TOOL: dict = {
    "name": "get_outing_detail",
    "description": (
        "Fetch the full text of a single Camptocamp trip report (outing). "
        "Returns conditions description, weather notes, and date. "
        "Call get_outing_list first to find relevant outing IDs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "outing_id": {
                "type": "integer",
                "description": "Camptocamp numeric outing ID (from get_outing_list).",
            },
        },
        "required": ["outing_id"],
    },
}

MAKE_ROUTE_TOOL: dict = {
    "name": "make_route",
    "description": (
        "Construct a route object for a route not found on Camptocamp — e.g. from a "
        "guidebook, user description, or web search. Returns a route dict in the same "
        "shape as fetch_route, usable with get_weather_forecast and get_avalanche_bulletin. "
        "Provide lat/lon only if you have high confidence (e.g. from a web search result). "
        "If omitted, the tool geocodes from `location` automatically via Nominatim — "
        "prefer letting the tool do this rather than estimating coordinates yourself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Route name.",
            },
            "location": {
                "type": "string",
                "description": "Area or massif name, e.g. 'Patagonia / Fitzroy' or 'Karakoram / Baltoro'.",
            },
            "lat": {
                "type": "number",
                "description": "Latitude (WGS84). Omit to let the tool geocode from location.",
            },
            "lon": {
                "type": "number",
                "description": "Longitude (WGS84). Omit to let the tool geocode from location.",
            },
            "grades": {
                "type": "object",
                "description": "Grade fields using the same keys as fetch_route (alpine_grade, rock_grade, ice_grade, mixed_grade, engagement, etc.).",
            },
            "elevation_max_m": {
                "type": "integer",
                "description": "Summit elevation in metres.",
            },
            "description": {
                "type": "string",
                "description": "Route description or notes from the source.",
            },
            "source": {
                "type": "string",
                "description": "Where this information comes from, e.g. 'Piola guidebook', 'user', 'Mountain Project'.",
            },
        },
        "required": ["name", "location"],
    },
}

ALL_TOOLS: list[dict] = [
    WEATHER_TOOL,
    AVALANCHE_TOOL,
    SEARCH_ROUTES_BY_NAME_TOOL,
    SEARCH_ROUTES_BY_AREA_TOOL,
    FETCH_ROUTE_TOOL,
    GET_OUTING_LIST_TOOL,
    GET_OUTING_DETAIL_TOOL,
    MAKE_ROUTE_TOOL,
]

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_get_weather_forecast(tool_input: dict) -> dict:
    lat = tool_input["latitude"]
    lon = tool_input["longitude"]
    elevation_m = tool_input.get("elevation_m")
    today = date.today()

    summary = fetch_weather_for_coords(lat, lon, today, elevation_m=elevation_m)

    # Serialise _DayForecast dataclasses from forecast_text source data.
    # WeatherSummary already has the pre-formatted strings; we expose the
    # structured fields so Claude can reason over them directly.
    return {
        "fetch_date": summary.fetch_date,
        "coords": {"lat": lat, "lon": lon},
        "elevation_m": elevation_m,
        "forecast_text": summary.forecast_text,
        "historical_summary": summary.historical_text,
        "errors": summary.fetch_errors,
    }


def route_summary(route: dict) -> dict:
    """Lean route dict for search results — enough for Claude to assess relevance."""
    return {
        "id": route.get("document_id"),
        "title": route.get("title"),
        "area": route.get("title_prefix"),
        "activities": route.get("activities", []),
        "quality": route.get("quality"),
        "alpine_grade": route.get("global_rating"),
        "rock_grade": route.get("rock_free_rating"),
        "ice_grade": route.get("ice_rating"),
        "mixed_grade": route.get("mixed_rating"),
        "engagement": route.get("engagement_rating"),
        "elevation_max_m": route.get("elevation_max"),
        "height_diff_up_m": route.get("height_diff_up"),
        "summary": (route.get("summary") or "")[:300],
    }


def _handle_search_routes_by_name(tool_input: dict) -> dict:
    query = tool_input["query"]
    limit = min(int(tool_input.get("limit") or 10), 30)
    routes = search_routes_by_name(query, limit=limit)
    return {
        "query": query,
        "count": len(routes),
        "routes": [route_summary(r) for r in routes],
    }


def _handle_search_routes_by_area(tool_input: dict) -> dict:
    lat_min = tool_input["lat_min"]
    lat_max = tool_input["lat_max"]
    lon_min = tool_input["lon_min"]
    lon_max = tool_input["lon_max"]
    activities = tool_input.get("activities") or None
    limit = min(int(tool_input.get("limit") or 15), 30)

    bbox = latlon_bbox_to_mercator(lon_min, lat_min, lon_max, lat_max)
    routes, total = search_routes(bbox, activities=activities, page_size=limit)
    return {
        "area": {"lat_min": lat_min, "lat_max": lat_max, "lon_min": lon_min, "lon_max": lon_max},
        "total_available": total,
        "returned": len(routes),
        "routes": [route_summary(r) for r in routes],
    }


def _handle_fetch_route(tool_input: dict) -> dict:
    route_id = int(tool_input["route_id"])
    route = fetch_route(route_id)
    locale = route.get("_locale") or {}
    coords = route_coords(route)
    return {
        "id": route_id,
        "title": route.get("title"),
        "area": route.get("title_prefix"),
        "activities": route.get("activities", []),
        "alpine_grade": route.get("global_rating"),
        "rock_grade": route.get("rock_free_rating"),
        "ice_grade": route.get("ice_rating"),
        "mixed_grade": route.get("mixed_rating"),
        "engagement": route.get("engagement_rating"),
        "risk": route.get("risk_rating"),
        "elevation_max_m": route.get("elevation_max"),
        "height_diff_up_m": route.get("height_diff_up"),
        "coords": {"lat": coords[0], "lon": coords[1]} if coords else None,
        "description": (locale.get("description") or "")[:2000],
        "remarks": (locale.get("remarks") or "")[:500],
        "gear": (locale.get("gear") or "")[:500],
        "external_resources": (locale.get("external_resources") or "")[:300],
        "camptocamp_url": f"https://www.camptocamp.org/routes/{route_id}",
    }


def _handle_get_outing_list(tool_input: dict) -> dict:
    route_id = int(tool_input["route_id"])
    stubs = fetch_outing_stubs(route_id)
    return {
        "route_id": route_id,
        "total": len(stubs),
        "outings": [
            {
                "outing_id": s.get("document_id"),
                "date": s.get("date_start"),
                "condition_rating": s.get("condition_rating"),
                "activities": s.get("activities", []),
            }
            for s in stubs
        ],
    }


def _handle_get_outing_detail(tool_input: dict) -> dict:
    outing_id = int(tool_input["outing_id"])
    outing = fetch_outing_full(outing_id)
    locale = outing.get("_locale") or {}
    return {
        "outing_id": outing_id,
        "date": outing.get("date_start"),
        "condition_rating": outing.get("condition_rating"),
        "elevation_max_m": outing.get("elevation_max"),
        "conditions": (locale.get("conditions") or "")[:1500],
        "weather": (locale.get("weather") or "")[:400],
        "camptocamp_url": f"https://www.camptocamp.org/outings/{outing_id}",
    }


def _handle_make_route(tool_input: dict) -> dict:
    from src.geo import geocode_location
    lat = tool_input.get("lat")
    lon = tool_input.get("lon")
    if lat is None or lon is None:
        coords = geocode_location(tool_input["location"])
        if coords:
            lat, lon = coords
    grades = tool_input.get("grades") or {}
    return {
        "id": None,
        "camptocamp": False,
        "source": tool_input.get("source", "user"),
        "title": tool_input["name"],
        "area": tool_input["location"],
        "activities": [],
        "alpine_grade": grades.get("alpine_grade"),
        "rock_grade": grades.get("rock_grade"),
        "ice_grade": grades.get("ice_grade"),
        "mixed_grade": grades.get("mixed_grade"),
        "engagement": grades.get("engagement"),
        "risk": grades.get("risk"),
        "elevation_max_m": tool_input.get("elevation_max_m"),
        "height_diff_up_m": None,
        "coords": {"lat": lat, "lon": lon} if lat is not None else None,
        "description": tool_input.get("description", ""),
        "remarks": "",
        "gear": "",
        "external_resources": "",
        "camptocamp_url": None,
    }


def _handle_get_avalanche_bulletin(tool_input: dict) -> dict:
    lat = tool_input["latitude"]
    lon = tool_input["longitude"]

    bulletins = fetch_avalanche_bulletin(lat, lon)

    if not bulletins:
        return {
            "coords": {"lat": lat, "lon": lon},
            "bulletins": [],
            "note": "No avalanche bulletin available for this location.",
        }

    def _serialise(b) -> dict:
        return {
            "provider": b.provider_name,
            "massif": b.massif_name,
            "danger_level": b.danger_level,
            "danger_level_lo": b.danger_level_lo,
            "danger_level_hi": b.danger_level_hi,
            "danger_split_altitude_m": b.danger_split_altitude,
            "valid_until": b.valid_until,
            "aspects_at_risk": b.aspects_at_risk,
            "summary": b.summary,
            "full_text": b.full_text[:1500] if b.full_text else "",
            "fetch_error": b.fetch_error,
        }

    return {
        "coords": {"lat": lat, "lon": lon},
        "bulletins": [_serialise(b) for b in bulletins],
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, Any] = {
    "get_weather_forecast": _handle_get_weather_forecast,
    "get_avalanche_bulletin": _handle_get_avalanche_bulletin,
    "search_routes_by_name": _handle_search_routes_by_name,
    "search_routes_by_area": _handle_search_routes_by_area,
    "fetch_route": _handle_fetch_route,
    "get_outing_list": _handle_get_outing_list,
    "get_outing_detail": _handle_get_outing_detail,
    "make_route": _handle_make_route,
}


def dispatch_tool(name: str, tool_input: dict) -> dict:
    """
    Route a tool_use block to the appropriate handler.

    Returns a JSON-serializable dict to pass back to the API as a
    tool_result content block.

    Raises KeyError if the tool name is not registered.
    """
    handler = _HANDLERS[name]
    return handler(tool_input)
