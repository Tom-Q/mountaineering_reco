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

import re
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
        "If omitted, the tool geocodes from `location` automatically via Nominatim. "
        "The result always includes a `geocoding_note` field. When coordinates were "
        "geocoded (not supplied explicitly), you MUST report the geocoding_note to the "
        "user before calling weather or avalanche tools. If importance < 0.4 or the "
        "display_name looks geographically wrong, ask the user to confirm or supply "
        "explicit coordinates before proceeding with weather data."
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

SHOW_IMAGES_TOOL: dict = {
    "name": "show_images",
    "description": (
        "Queue one or more images for the user to view in the gallery panel. "
        "Call this whenever you have image URLs worth showing — route photos from fetch_route, "
        "topo diagrams, conditions shots from trip reports, or any other relevant visuals. "
        "Images appear in a side panel with prev/next navigation. "
        "Each image needs a caption and optionally a source URL. "
        "The tool returns immediately; images appear in the panel as soon as you call it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "images": {
                "type": "array",
                "description": "List of images to queue for display.",
                "items": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "Public https:// URL of the image.",
                        },
                        "caption": {
                            "type": "string",
                            "description": "Short description of what this image shows.",
                        },
                        "source_url": {
                            "type": "string",
                            "description": "URL of the page where the image was found (for attribution).",
                        },
                    },
                    "required": ["url", "caption"],
                },
            },
        },
        "required": ["images"],
    },
}

SEARCH_DOCUMENTS_TOOL: dict = {
    "name": "search_documents",
    "description": (
        "Search the local mountaineering document corpus (~16,400 documents across 8 sources: "
        "SummitPost, hikr.org, SAC, passion-alpes, lemkeclimbs, refuges.info, Freedom of the Hills, "
        "Mémento FFCAM) for route descriptions, trip reports, hut info, and reference material. "
        "Use for factual route beta (approach, gear, grades), trip reports, hut access, and technique guides. "
        "Queries can be in any language. "
        "Not for conditions or weather — use get_outing_detail and get_weather_forecast for those.\n\n"
        "Geographic filtering — use at most one of:\n"
        "  area: a named mountain range (e.g. 'Mont Blanc massif', 'Karakoram', 'Patagonia'). "
        "Resolves to a bounding box from the ranges database.\n"
        "  near: a place name (peak, village, hut, pass). Geocoded via Nominatim; combine with "
        "radius_km to set the search radius (default 50 km).\n\n"
        "To retrieve the full text of a specific document, use retrieve_document."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you are looking for, in any language.",
            },
            "n_results": {
                "type": "integer",
                "description": "Number of results to return (default 5, max 15).",
            },
            "doc_type": {
                "type": "string",
                "description": (
                    "Optional: filter by document type. "
                    "Values: route_description, personal_trip_report, hut, manual, other."
                ),
            },
            "language": {
                "type": "string",
                "description": "Optional: filter by language ISO code (en, fr, de, it, …).",
            },
            "area": {
                "type": "string",
                "description": (
                    "Named mountain range to restrict results to. "
                    "Examples: 'Alps', 'Mont Blanc massif', 'Karakoram', 'Patagonia', "
                    "'Cordillera Blanca', 'Sierra Nevada', 'Japanese Alps'. "
                    "Mutually exclusive with near."
                ),
            },
            "near": {
                "type": "string",
                "description": (
                    "Place name to search around (peak, village, hut, valley). "
                    "Geocoded automatically. Combine with radius_km. "
                    "Mutually exclusive with area."
                ),
            },
            "radius_km": {
                "type": "number",
                "description": "Search radius in km around the near location (default 50).",
            },
        },
        "required": ["query"],
    },
}

RETRIEVE_DOCUMENT_TOOL: dict = {
    "name": "retrieve_document",
    "description": (
        "Fetch the full text of a document found via search_documents. "
        "Use the source and pk values from search results. "
        "Read the card summary first and only retrieve documents whose summary suggests "
        "directly relevant route beta, conditions, or reference material."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": (
                    "Source name from search results: summitpost, hikr, sac, "
                    "passion_alpes, lemkeclimbs, freedom_of_hills, memento_ffcam, refuges."
                ),
            },
            "pk": {
                "type": "integer",
                "description": "Primary key from search results (the pk field).",
            },
        },
        "required": ["source", "pk"],
    },
}

FETCH_ROUTE_FULL_TOOL: dict = {
    "name": "fetch_route_full",
    "description": (
        "Search Camptocamp for a route by name, select the best match, fetch its full topo, "
        "pick the most relevant recent trip reports, and return concise extractions — all in one call. "
        "Use this instead of chaining search_routes_by_name → fetch_route → get_outing_list → get_outing_detail. "
        "If multiple plausible routes are found with meaningfully different characteristics, "
        "returns all candidates with ambiguous=true so you can ask the user which they mean."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Route name or description to search for on Camptocamp.",
            },
            "goal": {
                "type": "string",
                "description": (
                    "What to look for in the route and trip reports, e.g. "
                    "'current conditions and approach for a summer ascent'."
                ),
            },
        },
        "required": ["query", "goal"],
    },
}

SEARCH_AND_EXTRACT_TOOL: dict = {
    "name": "search_and_extract",
    "description": (
        "Search the local document corpus and extract relevant information in one step. "
        "Runs semantic search over ~17,000 documents (SummitPost, hikr, SAC, passion-alpes, "
        "lemkeclimbs, Freedom of the Hills, Mémento FFCAM, refuges), selects the most relevant "
        "results via a quick relevance check, fetches those documents, and returns concise "
        "extractions — without exposing full document text. "
        "Use this as the primary way to query the local corpus. "
        "Use multiple queries to search from different angles simultaneously."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "One or more search queries in any language.",
            },
            "goal": {
                "type": "string",
                "description": (
                    "What to extract from retrieved documents, e.g. "
                    "'approach route and hut options for Barre des Écrins'."
                ),
            },
            "n_summaries": {
                "type": "integer",
                "description": "Card summaries to retrieve per query (default 5, max 10).",
            },
            "doc_type": {
                "type": "string",
                "description": "Filter by type: route_description, personal_trip_report, hut, manual, other.",
            },
            "language": {
                "type": "string",
                "description": "Filter by language ISO code (en, fr, de, it, …).",
            },
            "area": {
                "type": "string",
                "description": "Named mountain range to restrict results to.",
            },
            "near": {
                "type": "string",
                "description": "Place name to restrict results to a radius around.",
            },
            "radius_km": {
                "type": "number",
                "description": "Radius in km around 'near' (default 50).",
            },
        },
        "required": ["queries", "goal"],
    },
}

ALL_TOOLS: list[dict] = [
    WEATHER_TOOL,
    AVALANCHE_TOOL,
    FETCH_ROUTE_FULL_TOOL,
    SEARCH_ROUTES_BY_NAME_TOOL,
    SEARCH_ROUTES_BY_AREA_TOOL,
    FETCH_ROUTE_TOOL,
    GET_OUTING_LIST_TOOL,
    GET_OUTING_DETAIL_TOOL,
    MAKE_ROUTE_TOOL,
    SHOW_IMAGES_TOOL,
    SEARCH_AND_EXTRACT_TOOL,
    SEARCH_DOCUMENTS_TOOL,
    RETRIEVE_DOCUMENT_TOOL,
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


_C2C_CDN = "https://media.camptocamp.org/c2corg-active"


def _c2c_image_url(filename: str, size: str = "MI") -> str:
    """Build a Camptocamp CDN URL with the correct size suffix.

    The API returns bare filenames like '1237326095_1984991001.jpg'.
    The CDN serves sized variants by inserting a suffix before the extension:
      MI = medium (displayed on topo pages)
      BI = big (full-size view)
    """
    if "." in filename:
        base, ext = filename.rsplit(".", 1)
        return f"{_C2C_CDN}/{base}{size}.{ext}"
    return f"{_C2C_CDN}/{filename}"


def _extract_c2c_images(route: dict, description_ids: set[int] | None = None) -> list[dict]:
    """Return images associated with a Camptocamp route as gallery-ready dicts.

    Images whose document IDs appear in description_ids (i.e. explicitly embedded
    in the route description via [img=ID] markup) are marked in_description=True
    and sorted first — they are typically topos or annotated photos chosen by
    the route author.
    """
    raw_images = route.get("associations", {}).get("images", [])
    description_ids = description_ids or set()
    result = []
    for img in raw_images:
        filename = img.get("filename")
        if not filename:
            continue
        doc_id = img.get("document_id")
        locales = img.get("locales") or []
        title = next(
            (l.get("title", "") for l in locales if l.get("lang") == "fr"),
            locales[0].get("title", "") if locales else "",
        )
        result.append({
            "url": _c2c_image_url(filename),
            "caption": title or filename,
            "source_url": f"https://www.camptocamp.org/images/{doc_id}" if doc_id else None,
            "in_description": doc_id in description_ids,
        })
    # Description images first, then the rest
    result.sort(key=lambda x: (0 if x["in_description"] else 1))
    return result


def _handle_fetch_route(tool_input: dict) -> dict:
    route_id = int(tool_input["route_id"])
    route = fetch_route(route_id)
    locale = route.get("_locale") or {}
    coords = route_coords(route)
    description = (locale.get("description") or "")
    # Parse [img=ID ...] markup from the description to identify author-chosen images
    desc_image_ids = {int(m) for m in re.findall(r'\[img=(\d+)', description)}
    images = _extract_c2c_images(route, description_ids=desc_image_ids)
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
        "description": description[:2000],
        "remarks": (locale.get("remarks") or "")[:500],
        "gear": (locale.get("gear") or "")[:500],
        "external_resources": (locale.get("external_resources") or "")[:300],
        "camptocamp_url": f"https://www.camptocamp.org/routes/{route_id}",
        "images": images,
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
    associated_routes = outing.get("associations", {}).get("routes", [])
    result = {
        "outing_id": outing_id,
        "date": outing.get("date_start"),
        "condition_rating": outing.get("condition_rating"),
        "elevation_max_m": outing.get("elevation_max"),
        "partial_trip": outing.get("partial_trip") or False,
        "associated_route_ids": [r["document_id"] for r in associated_routes],
        "multi_route": len(associated_routes) > 1,
        "conditions": (locale.get("conditions") or "")[:1500],
        "weather": (locale.get("weather") or "")[:400],
        "camptocamp_url": f"https://www.camptocamp.org/outings/{outing_id}",
    }
    return result


def _handle_make_route(tool_input: dict) -> dict:
    from src.geo import geocode_location
    lat = tool_input.get("lat")
    lon = tool_input.get("lon")

    if lat is not None and lon is not None:
        geocoding_note = "Coordinates provided explicitly — not geocoded."
    else:
        geo = geocode_location(tool_input["location"])
        if geo:
            lat, lon = geo["lat"], geo["lon"]
            importance = geo["importance"]
            importance_str = f"{importance:.2f}" if importance is not None else "unknown"
            geocoding_note = (
                f"Coordinates geocoded via Nominatim: \"{geo['display_name']}\" "
                f"(OSM: {geo['osm_class']}/{geo['osm_type']}, "
                f"importance: {importance_str}, "
                f"query: \"{geo['query_used']}\"). "
                f"Verify this is the intended location before relying on weather data."
            )
        else:
            geocoding_note = (
                "Geocoding failed — no coordinates found for this location. "
                "Weather and avalanche tools cannot be used for this route. "
                "Ask the user to supply explicit coordinates if needed."
            )

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
        "geocoding_note": geocoding_note,
        "description": tool_input.get("description", ""),
        "remarks": "",
        "gear": "",
        "external_resources": "",
        "camptocamp_url": None,
    }


def _handle_search_documents(tool_input: dict) -> dict:
    from src.geo import bbox_around, geocode_location
    from src.rag import is_available, resolve_area, search
    if not is_available():
        return {"available": False, "note": "SummitPost route database not indexed yet."}

    query = tool_input["query"]
    n_results = min(int(tool_input.get("n_results") or 5), 15)
    doc_type = tool_input.get("doc_type")
    language = tool_input.get("language")
    area = tool_input.get("area")
    near = tool_input.get("near")
    radius_km = float(tool_input.get("radius_km") or 50)

    lat_min = lat_max = lon_min = lon_max = None
    geo_note: str | None = None

    if area:
        bbox = resolve_area(area)
        if bbox:
            lat_min, lat_max, lon_min, lon_max = bbox
            geo_note = f"Filtered to area '{area}' (bbox: lat {lat_min}–{lat_max}, lon {lon_min}–{lon_max})."
        else:
            geo_note = f"Area '{area}' not found in ranges database — returning unfiltered results."
    elif near:
        geo = geocode_location(near)
        if geo:
            lat_min, lat_max, lon_min, lon_max = bbox_around(geo["lat"], geo["lon"], radius_km)
            geo_note = (
                f"Filtered to {radius_km:.0f} km around '{near}' "
                f"(geocoded: {geo['display_name']})."
            )
        else:
            geo_note = f"Could not geocode '{near}' — returning unfiltered results."

    results = search(
        query,
        n_results=n_results,
        doc_type=doc_type,
        language=language,
        lat_min=lat_min,
        lat_max=lat_max,
        lon_min=lon_min,
        lon_max=lon_max,
    )

    out: dict = {"available": True, "query": query, "results": results}
    if geo_note:
        out["geo_filter"] = geo_note
    return out


def _handle_retrieve_document(tool_input: dict) -> dict:
    from src.rag import (
        get_freedom_section, get_hikr_report, get_lemkeclimbs_topo,
        get_memento_section, get_passion_alpes_topo, get_refuge,
        get_route_sections, get_sac_topo, is_available,
    )
    if not is_available():
        return {"available": False, "note": "Route database not indexed yet."}

    source = tool_input.get("source")
    pk = tool_input.get("pk")
    if not source or pk is None:
        return {"available": True, "found": False, "note": "Provide source and pk from search results."}

    pk = int(pk)
    dispatch = {
        "summitpost":      get_route_sections,
        "passion_alpes":   get_passion_alpes_topo,
        "sac":             get_sac_topo,
        "hikr":            get_hikr_report,
        "lemkeclimbs":     get_lemkeclimbs_topo,
        "freedom_of_hills": get_freedom_section,
        "memento_ffcam":   get_memento_section,
        "refuges":         get_refuge,
    }
    fn = dispatch.get(source)
    if fn is None:
        return {"available": True, "found": False, "note": f"Unknown source '{source}'."}

    document = fn(pk)
    if not document:
        return {"available": True, "found": False, "source": source, "pk": pk}
    return {"available": True, "found": True, "source": source, "document": document}


def _handle_show_images(tool_input: dict) -> dict:
    """
    Queue images for the gallery panel via the _images side-channel.

    The actual gallery update is handled by app.py, which intercepts the
    _images key before sending the result to the API.
    """
    images = tool_input.get("images", [])
    return {
        "_images": images,
        "queued": len(images),
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

    # Side-channel: surface Météo-France bulletin images to the gallery.
    # These are binary blobs (not public URLs) so they travel via _image_blobs
    # rather than _images. app.py intercepts this key and writes bytes to
    # st.session_state["image_blobs"].
    image_blobs = {}
    for b in bulletins:
        if b.image_meteo:
            key = f"bra_meteo_{b.massif_name or 'unknown'}"
            image_blobs[key] = {
                "data": b.image_meteo,
                "caption": f"Météo overview — {b.massif_name or 'bulletin'}",
                "source_url": "https://meteofrance.com",
            }
        if b.image_7days:
            key = f"bra_7days_{b.massif_name or 'unknown'}"
            image_blobs[key] = {
                "data": b.image_7days,
                "caption": f"7 derniers jours — {b.massif_name or 'bulletin'}",
                "source_url": "https://meteofrance.com",
            }

    result: dict = {
        "coords": {"lat": lat, "lon": lon},
        "bulletins": [_serialise(b) for b in bulletins],
    }
    if image_blobs:
        result["_image_blobs"] = image_blobs
    return result


# ---------------------------------------------------------------------------
# fetch_route_full helpers
# ---------------------------------------------------------------------------

def _select_routes(route_stubs: list[dict], goal: str) -> tuple[list[int], bool]:
    """Haiku call to select which C2C routes match the goal.
    Returns (list of route_ids, ambiguous). ambiguous=True when multiple
    plausible routes with different characteristics are found."""
    import json as _json
    from src.client import _get_client
    lines = "\n".join(
        f"[{i}] id={r.get('document_id')} title={r.get('title_prefix','')} {r.get('title','')} "
        f"grade={r.get('global_rating','')} summary={str(r.get('summary',''))[:150]}"
        for i, r in enumerate(route_stubs[:10])
    )
    response = _get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=(
            'Select routes that match the goal. Return JSON: {"indices": [0,1,...], "ambiguous": false}. '
            'Set ambiguous=true if multiple routes match with meaningfully different characteristics. '
            'Return at most 2 indices.'
        ),
        messages=[{"role": "user", "content": f"Goal: {goal}\n\nRoutes:\n{lines}"}],
    )
    try:
        parsed = _json.loads(response.content[0].text)
        indices = parsed.get("indices", [])[:2]
        ambiguous = bool(parsed.get("ambiguous", False))
        ids = [route_stubs[i]["document_id"] for i in indices if 0 <= i < len(route_stubs)]
        return ids, ambiguous
    except (ValueError, KeyError, IndexError):
        return [route_stubs[0]["document_id"]] if route_stubs else [], False


def _select_outings(outing_stubs: list[dict], goal: str) -> list[int]:
    """Haiku call to pick which outing stubs are worth reading in full."""
    import json as _json
    from src.client import _get_client
    lines = "\n".join(
        f"[{i}] id={s.get('document_id')} date={s.get('date_start','')} "
        f"rating={s.get('condition_rating','')} grade={s.get('global_rating','')}"
        for i, s in enumerate(outing_stubs[:20])
    )
    response = _get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=(
            "Pick the most relevant outings (max 3). Prefer recent and highly rated. "
            "Return a JSON array of indices e.g. [0, 2]."
        ),
        messages=[{"role": "user", "content": f"Goal: {goal}\n\nOutings:\n{lines}"}],
    )
    try:
        indices = _json.loads(response.content[0].text)
        return [outing_stubs[i]["document_id"] for i in indices if 0 <= i < len(outing_stubs)]
    except (ValueError, KeyError, IndexError):
        return [s["document_id"] for s in outing_stubs[:2]]


def _summarise_route(route: dict) -> dict:
    """Lean route summary for the LLM — omits raw locale blobs."""
    loc = route.get("_locale") or {}
    return {
        "id": route.get("document_id"),
        "title": f"{route.get('title_prefix','')} {loc.get('title','')}".strip(),
        "url": f"https://www.camptocamp.org/routes/{route.get('document_id')}",
        "global_rating": route.get("global_rating"),
        "elevation_max": route.get("elevation_max"),
        "description": loc.get("description", "")[:1500],
        "approach": loc.get("approach", "")[:800],
        "gear": loc.get("gear", "")[:400],
    }


def _handle_fetch_route_full(tool_input: dict) -> dict:
    import concurrent.futures
    from src.camptocamp import fetch_outing_full, fetch_outing_stubs, fetch_route, search_routes_by_name

    query = tool_input["query"]
    goal = tool_input["goal"]

    # 1. Search C2C
    stubs = search_routes_by_name(query, limit=10)
    if not stubs:
        return {"found": False, "query": query}

    # 2. Haiku selects which routes to fetch
    route_ids, ambiguous = _select_routes(stubs, goal)
    if not route_ids:
        route_ids = [stubs[0]["document_id"]]

    if ambiguous:
        candidates = [
            {"id": s["document_id"], "title": f"{s.get('title_prefix','')} {s.get('title','')}".strip(),
             "grade": s.get("global_rating"), "summary": s.get("summary", "")}
            for s in stubs[:5]
        ]
        return {"found": True, "ambiguous": True, "candidates": candidates}

    # 3. Fetch full route data (sequential — C2C rate limited)
    routes = [fetch_route(rid) for rid in route_ids]

    # 4. Fetch outing stubs + select outings per route
    results = []
    for route in routes:
        rid = route["document_id"]
        outing_stubs = fetch_outing_stubs(rid, limit=20)
        outing_ids = _select_outings(outing_stubs, goal) if outing_stubs else []

        # 5. Fetch full outings (sequential — C2C rate limited)
        full_outings = [fetch_outing_full(oid) for oid in outing_ids]

        # 6. Haiku extracts from each outing (parallel — Anthropic calls)
        def extract_outing(outing: dict) -> dict:
            loc = outing.get("_locale") or {}
            text = "\n".join(filter(None, [
                f"Date: {outing.get('date_start','')}",
                f"Rating: {outing.get('condition_rating','')}",
                loc.get("conditions", ""),
                loc.get("timing", ""),
            ]))
            extraction = _extract_from_document(text, goal)
            return {
                "id": outing["document_id"],
                "date": outing.get("date_start", ""),
                "condition_rating": outing.get("condition_rating"),
                "url": f"https://www.camptocamp.org/outings/{outing['document_id']}",
                "extraction": extraction,
            }

        outing_extractions: list[dict] = []
        if full_outings:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                outing_extractions = list(executor.map(extract_outing, full_outings))

        results.append({
            "route": _summarise_route(route),
            "outing_extractions": outing_extractions,
        })

    return {"found": True, "ambiguous": False, "results": results}


# ---------------------------------------------------------------------------
# search_and_extract helpers
# ---------------------------------------------------------------------------

def _get_document_text(source: str, document: dict) -> str:
    if source == "summitpost":
        return "\n\n".join(
            f"{s['heading']}\n{s['body']}" for s in document.get("sections", []) if s.get("body")
        )
    if source in ("hikr", "lemkeclimbs", "passion_alpes", "sac"):
        return document.get("full_text", "")
    if source in ("freedom_of_hills", "memento_ffcam"):
        return document.get("text", "")
    if source == "refuges":
        parts = [
            document.get("name", ""),
            f"Type: {document['type']}" if document.get("type") else "",
            f"Altitude: {document['altitude_m']}m" if document.get("altitude_m") else "",
            document.get("description", ""),
            document.get("access_desc", ""),
            f"Opening: {document['opening_dates']}" if document.get("opening_dates") else "",
            f"Phone: {document['phone']}" if document.get("phone") else "",
            f"Website: {document['website_url']}" if document.get("website_url") else "",
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _extract_from_document(text: str, goal: str) -> str:
    from src.client import _get_client
    response = _get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        system=(
            "Extract information relevant to the goal from the document. "
            "Be concise. If nothing is relevant, say so in one sentence."
        ),
        messages=[{"role": "user", "content": f"Goal: {goal}\n\nDocument:\n{text[:12000]}"}],
    )
    return response.content[0].text


def _select_documents(summaries: list[dict], goal: str) -> set[tuple[str, int]]:
    import json as _json
    from src.client import _get_client
    summary_lines = "\n".join(
        f"[{i}] {r['source']}--{r['pk']}: {r.get('title', '')} | {(r.get('summary') or '')[:200]}"
        for i, r in enumerate(summaries)
    )
    response = _get_client().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=(
            "Select which documents are worth reading in full to answer the goal. "
            "Return a JSON array of indices (e.g. [0, 2]). Return [] if none are relevant."
        ),
        messages=[{
            "role": "user",
            "content": f"Goal: {goal}\n\nDocuments:\n{summary_lines}",
        }],
    )
    try:
        indices = _json.loads(response.content[0].text)
        return {(summaries[i]["source"], summaries[i]["pk"]) for i in indices if 0 <= i < len(summaries)}
    except (ValueError, KeyError, IndexError):
        return {(r["source"], r["pk"]) for r in sorted(summaries, key=lambda x: x["distance"])[:2]}


def _handle_search_and_extract(tool_input: dict) -> dict:
    import concurrent.futures
    from src.rag import (
        get_freedom_section, get_hikr_report, get_lemkeclimbs_topo,
        get_memento_section, get_passion_alpes_topo, get_refuge,
        get_route_sections, get_sac_topo, is_available, resolve_area, search,
    )
    from src.geo import bbox_around, geocode_location

    if not is_available():
        return {"available": False, "note": "Document index not available."}

    queries = tool_input.get("queries") or []
    goal = tool_input["goal"]
    n_summaries = min(int(tool_input.get("n_summaries") or 5), 10)
    doc_type = tool_input.get("doc_type")
    language = tool_input.get("language")
    area = tool_input.get("area")
    near = tool_input.get("near")
    radius_km = float(tool_input.get("radius_km") or 50)

    lat_min = lat_max = lon_min = lon_max = None
    geo_note: str | None = None
    if area:
        bbox = resolve_area(area)
        if bbox:
            lat_min, lat_max, lon_min, lon_max = bbox
            geo_note = f"Filtered to area '{area}'."
        else:
            geo_note = f"Area '{area}' not found — returning unfiltered results."
    elif near:
        geo = geocode_location(near)
        if geo:
            lat_min, lat_max, lon_min, lon_max = bbox_around(geo["lat"], geo["lon"], radius_km)
            geo_note = f"Filtered to {radius_km:.0f}km around '{near}'."
        else:
            geo_note = f"Could not geocode '{near}' — returning unfiltered results."

    # Collect unique results across all queries
    all_summaries: list[dict] = []
    seen: set[str] = set()
    for query in queries:
        for r in search(query, n_results=n_summaries, doc_type=doc_type, language=language,
                        lat_min=lat_min, lat_max=lat_max, lon_min=lon_min, lon_max=lon_max):
            key = f"{r['source']}--{r['pk']}"
            if key not in seen:
                seen.add(key)
                all_summaries.append(r)

    if not all_summaries:
        out: dict = {"available": True, "summaries": [], "extractions": []}
        if geo_note:
            out["geo_filter"] = geo_note
        return out

    # Haiku routing: select which documents to expand
    selected = _select_documents(all_summaries, goal)

    # Fetch + extract selected documents in parallel
    dispatch = {
        "summitpost":       get_route_sections,
        "passion_alpes":    get_passion_alpes_topo,
        "sac":              get_sac_topo,
        "hikr":             get_hikr_report,
        "lemkeclimbs":      get_lemkeclimbs_topo,
        "freedom_of_hills": get_freedom_section,
        "memento_ffcam":    get_memento_section,
        "refuges":          get_refuge,
    }

    def fetch_and_extract(summary: dict) -> dict | None:
        source, pk = summary["source"], summary["pk"]
        fn = dispatch.get(source)
        if not fn:
            return None
        document = fn(pk)
        if not document:
            return None
        text = _get_document_text(source, document)
        if not text:
            return None
        extraction = _extract_from_document(text, goal)
        return {
            "source": source,
            "pk": pk,
            "title": document.get("title") or document.get("name", ""),
            "url": document.get("url", ""),
            "extraction": extraction,
        }

    to_fetch = [s for s in all_summaries if (s["source"], s["pk"]) in selected]
    extractions: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_and_extract, s): s for s in to_fetch}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                extractions.append(result)

    out = {"available": True, "summaries": all_summaries, "extractions": extractions}
    if geo_note:
        out["geo_filter"] = geo_note
    return out


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
    "show_images": _handle_show_images,
    "fetch_route_full": _handle_fetch_route_full,
    "search_documents": _handle_search_documents,
    "retrieve_document": _handle_retrieve_document,
    "search_and_extract": _handle_search_and_extract,
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
