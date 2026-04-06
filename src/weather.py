"""
Weather data integration for route analysis.

Fetches a 7-day forecast and a 90-day historical summary from Open-Meteo
(free, no authentication required) for the route's coordinates.

The output is pre-formatted text blocks ready for LLM injection — no computed
isotherms or complex meteorological processing at this stage.
"""

import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import requests_cache

_session = requests_cache.CachedSession("weather_cache", expire_after=3600)
_archive_session = requests_cache.CachedSession("weather_cache", expire_after=86400)


@dataclass
class WeatherSummary:
    fetch_date: str
    coords: tuple[float, float]
    forecast_text: str         # pre-formatted for LLM injection
    historical_text: str       # pre-formatted for LLM injection
    fetch_errors: list[str] = field(default_factory=list)


def route_coords(route: dict) -> tuple[float, float] | None:
    """
    Extract WGS84 (lat, lon) from the route's Camptocamp geometry.

    Camptocamp stores geometry as a JSON string in EPSG:3857 (Web Mercator).
    For Point geometry: converts the single coordinate.
    For LineString: takes the midpoint.
    Returns None if geometry is absent, malformed, or an unsupported type.
    """
    try:
        geom_raw = (route.get("geometry") or {}).get("geom")
        if not geom_raw:
            return None
        geom = json.loads(geom_raw) if isinstance(geom_raw, str) else geom_raw
        gtype = geom.get("type", "")
        coords = geom.get("coordinates", [])

        if gtype == "Point":
            x, y = coords[0], coords[1]
        elif gtype == "LineString":
            mid = coords[len(coords) // 2]
            x, y = mid[0], mid[1]
        else:
            return None

        # Inverse Mercator: EPSG:3857 → WGS84
        lon = x * 180.0 / 20037508.34
        lat = math.degrees(
            math.atan(math.exp(y * math.pi / 20037508.34)) * 2 - math.pi / 2
        )
        return lat, lon
    except Exception:
        return None


def _fetch_forecast_text(lat: float, lon: float) -> str:
    """Return a formatted 7-day forecast table from Open-Meteo."""
    r = _session.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": [
                "precipitation_sum",
                "snowfall_sum",
                "windspeed_10m_max",
                "windgusts_10m_max",
                "temperature_2m_min",
                "temperature_2m_max",
            ],
            "forecast_days": 7,
            "timezone": "UTC",
        },
        timeout=15,
    )
    r.raise_for_status()
    d = r.json().get("daily", {})

    lines = [
        "Date        | Precip(mm) | Snow(cm) | Wind max(km/h) | Gusts(km/h) | T min/max(°C)",
        "------------|------------|----------|----------------|-------------|---------------",
    ]
    for i, day in enumerate(d.get("time", [])):
        precip  = d["precipitation_sum"][i]
        snow    = d["snowfall_sum"][i]
        wind    = d["windspeed_10m_max"][i]
        gust    = d["windgusts_10m_max"][i]
        t_min   = d["temperature_2m_min"][i]
        t_max   = d["temperature_2m_max"][i]
        storm   = " ⚠ STORM" if (snow or 0) > 10 or (gust or 0) > 80 else ""
        lines.append(
            f"{day} | {precip or 0:>10.1f} | {snow or 0:>8.1f} | "
            f"{wind or 0:>14.0f} | {gust or 0:>11.0f} | "
            f"{t_min or 0:>5.1f}/{t_max or 0:<5.1f}{storm}"
        )
    return "\n".join(lines)


def _fetch_historical_text(lat: float, lon: float, days_back: int = 90) -> str:
    """Return a 1–3 sentence historical summary (large snowfall, heat events)."""
    today = date.today()
    start = (today - timedelta(days=days_back)).isoformat()
    end   = (today - timedelta(days=1)).isoformat()  # archive lags ~1 day

    r = _archive_session.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude": lat,
            "longitude": lon,
            "start_date": start,
            "end_date": end,
            "daily": ["snowfall_sum", "temperature_2m_max"],
            "timezone": "UTC",
        },
        timeout=20,
    )
    r.raise_for_status()
    d = r.json().get("daily", {})
    dates      = d.get("time", [])
    snowfalls  = d.get("snowfall_sum", [])
    temps      = d.get("temperature_2m_max", [])

    # Large snowfall events (>30 cm in a day)
    big_snow = [
        (day, s) for day, s in zip(dates, snowfalls) if s is not None and s > 30
    ]
    # Heatwave: surface temp >25°C for 3+ consecutive days
    heat_events = []
    run: list[tuple[str, float]] = []
    for day, t in zip(dates, temps):
        if t is not None and t > 25:
            run.append((day, t))
        else:
            if len(run) >= 3:
                heat_events.append(run[:])
            run = []
    if len(run) >= 3:
        heat_events.append(run[:])

    sentences = []
    if big_snow:
        biggest = max(big_snow, key=lambda x: x[1])
        sentences.append(
            f"{len(big_snow)} large snowfall event(s) in the past {days_back} days "
            f"(largest: {biggest[1]:.0f} cm on {biggest[0]})."
        )
    else:
        sentences.append(f"No large snowfall events (>30 cm/day) in the past {days_back} days.")

    if heat_events:
        e = heat_events[-1]
        sentences.append(
            f"{len(heat_events)} heat event(s) detected: most recent "
            f"{e[0][0]}–{e[-1][0]}, peak surface temp {max(t for _, t in e):.1f}°C."
        )
    else:
        sentences.append(f"No significant heat events (>25°C surface) in the past {days_back} days.")

    return " ".join(sentences)


def fetch_weather(route: dict, today: date) -> WeatherSummary | None:
    """
    Fetch weather data for a route. Returns None if no coordinates are available.

    Errors in individual fetches are recorded in WeatherSummary.fetch_errors
    rather than propagated, so a partial result is still returned.
    """
    coords = route_coords(route)
    if coords is None:
        return None

    lat, lon = coords
    errors: list[str] = []

    forecast_text = ""
    try:
        forecast_text = _fetch_forecast_text(lat, lon)
    except Exception as e:
        errors.append(f"Forecast unavailable: {e}")

    historical_text = ""
    try:
        historical_text = _fetch_historical_text(lat, lon)
    except Exception as e:
        errors.append(f"Historical data unavailable: {e}")

    return WeatherSummary(
        fetch_date=today.isoformat(),
        coords=(lat, lon),
        forecast_text=forecast_text,
        historical_text=historical_text,
        fetch_errors=errors,
    )
