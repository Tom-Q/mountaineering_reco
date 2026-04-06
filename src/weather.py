"""
Weather data integration for route analysis.

Fetches a 7-day forecast and a 90-day historical summary from Open-Meteo
(free, no authentication required) for the route's coordinates.

The forecast uses hourly data aggregated to daily values, including pressure-level
variables (850/700 hPa) for altitude wind and nighttime 0°C isotherm computation.
"""

import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import requests_cache

_session = requests_cache.CachedSession("weather_cache", expire_after=3600)


@dataclass
class WeatherSummary:
    fetch_date: str
    coords: tuple[float, float]
    forecast_text: str         # pre-formatted for LLM injection
    historical_text: str       # pre-formatted for LLM injection
    ui_table: str              # markdown table for display in the app
    fetch_errors: list[str] = field(default_factory=list)


@dataclass
class _DayForecast:
    date: str
    precip_mm: float
    snowfall_cm: float
    wind_10m_max: float        # max wind at 10m (valley/surface)
    gusts_max: float
    temp_min: float
    temp_max: float
    wind_850hpa: float         # mean daytime wind at ~1500m
    nighttime_isotherm: str    # e.g. "2450m", ">3100m", "<1580m"
    night_cloud_pct: float     # mean cloud cover during night hours
    storm: bool                # snowfall > 10cm OR gusts > 80 km/h


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


def _mean(values: list) -> float | None:
    """Mean of a list, ignoring None values. Returns None if all are None."""
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def _nighttime_isotherm(t850: float | None, t700: float | None, t500: float | None,
                         h850: float | None, h700: float | None, h500: float | None) -> str:
    """
    Derive the nighttime 0°C isotherm altitude from mean nighttime values at
    three pressure levels: 850 hPa (~1500m), 700 hPa (~3000m), 500 hPa (~5500m).

    Interpolates between the two levels that straddle 0°C.
    Returns a formatted string like "2450m", "<1580m", or ">5500m".
    Falls back to "n/a" if data is missing.
    """
    if any(v is None for v in (t850, t700, h850, h700)):
        return "n/a"
    if t850 <= 0:
        return f"<{h850:.0f}m"
    if t700 <= 0:
        fraction = t850 / (t850 - t700)
        return f"{h850 + fraction * (h700 - h850):.0f}m"
    # Isotherm is above 700hPa — try 500hPa
    if t500 is None or h500 is None:
        return f">{h700:.0f}m"
    if t500 <= 0:
        fraction = t700 / (t700 - t500)
        return f"{h700 + fraction * (h500 - h700):.0f}m"
    # Above 500hPa — genuinely warm throughout the column
    return f">{h500:.0f}m"


def _build_forecast(lat: float, lon: float) -> list[_DayForecast]:
    """
    Fetch 7-day hourly forecast from Open-Meteo and aggregate to daily values.
    """
    r = _session.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": [
                "temperature_2m",
                "temperature_850hPa",
                "temperature_700hPa",
                "temperature_500hPa",
                "geopotential_height_850hPa",
                "geopotential_height_700hPa",
                "geopotential_height_500hPa",
                "windspeed_850hPa",
                "cloudcover",
                "precipitation",
                "snowfall",
                "windspeed_10m",
                "windgusts_10m",
            ],
            "forecast_days": 7,
            "timezone": "UTC",
        },
        timeout=15,
    )
    r.raise_for_status()
    h = r.json()["hourly"]

    times     = h["time"]
    t2m       = h["temperature_2m"]
    t850      = h["temperature_850hPa"]
    t700      = h["temperature_700hPa"]
    t500      = h["temperature_500hPa"]
    gh850     = h["geopotential_height_850hPa"]
    gh700     = h["geopotential_height_700hPa"]
    gh500     = h["geopotential_height_500hPa"]
    w850      = h["windspeed_850hPa"]
    cloud     = h["cloudcover"]
    precip    = h["precipitation"]
    snowfall  = h["snowfall"]
    wind10    = h["windspeed_10m"]
    gusts     = h["windgusts_10m"]

    # Group by date
    days: dict[str, dict] = {}
    for i, ts in enumerate(times):
        day_str = ts[:10]
        hour = int(ts[11:13])
        if day_str not in days:
            days[day_str] = {k: [] for k in (
                "t2m", "t850_day", "t850_night", "t700_night", "t500_night",
                "gh850_night", "gh700_night", "gh500_night", "w850_day",
                "cloud_night", "precip", "snowfall", "wind10", "gusts"
            )}
        d = days[day_str]
        d["t2m"].append(t2m[i])
        d["precip"].append(precip[i])
        d["snowfall"].append(snowfall[i])
        d["wind10"].append(wind10[i])
        d["gusts"].append(gusts[i])
        # Daytime: 06–18 UTC
        if 6 <= hour < 18:
            d["t850_day"].append(t850[i])
            d["w850_day"].append(w850[i])
        # Nighttime: 20–23 of this day + 00–06 belongs to next day's night
        # Treat 20–06 UTC as the night starting on this calendar date
        if hour >= 20 or hour < 6:
            d["t850_night"].append(t850[i])
            d["t700_night"].append(t700[i])
            d["t500_night"].append(t500[i])
            d["gh850_night"].append(gh850[i])
            d["gh700_night"].append(gh700[i])
            d["gh500_night"].append(gh500[i])
            d["cloud_night"].append(cloud[i])

    result = []
    for day_str, d in sorted(days.items()):
        snow_sum  = sum(v for v in d["snowfall"] if v is not None)
        prec_sum  = sum(v for v in d["precip"]   if v is not None)
        wind_max  = max((v for v in d["wind10"]  if v is not None), default=0.0)
        gust_max  = max((v for v in d["gusts"]   if v is not None), default=0.0)
        temp_min  = min((v for v in d["t2m"]     if v is not None), default=0.0)
        temp_max  = max((v for v in d["t2m"]     if v is not None), default=0.0)
        w850_mean = _mean(d["w850_day"]) or 0.0
        cloud_night = _mean(d["cloud_night"]) or 0.0

        isotherm = _nighttime_isotherm(
            _mean(d["t850_night"]),
            _mean(d["t700_night"]),
            _mean(d["t500_night"]),
            _mean(d["gh850_night"]),
            _mean(d["gh700_night"]),
            _mean(d["gh500_night"]),
        )

        result.append(_DayForecast(
            date=day_str,
            precip_mm=round(prec_sum, 1),
            snowfall_cm=round(snow_sum, 1),
            wind_10m_max=round(wind_max, 0),
            gusts_max=round(gust_max, 0),
            temp_min=round(temp_min, 1),
            temp_max=round(temp_max, 1),
            wind_850hpa=round(w850_mean, 0),
            nighttime_isotherm=isotherm,
            night_cloud_pct=round(cloud_night, 0),
            storm=snow_sum > 15 or gust_max > 80,
        ))
    return result


def _format_forecast_text(days: list[_DayForecast]) -> str:
    """Format forecast as compact text for LLM injection."""
    lines = [
        "Date       | Snow(cm) | Gusts(km/h) | Wind@850hPa(km/h) | Night isotherm | Night cloud% | T min/max°C",
        "-----------|----------|-------------|-------------------|----------------|--------------|------------",
    ]
    for d in days:
        storm = " ⚠STORM" if d.storm else ""
        lines.append(
            f"{d.date} | {d.snowfall_cm:>8.1f} | {d.gusts_max:>11.0f} | "
            f"{d.wind_850hpa:>17.0f} | {d.nighttime_isotherm:>14} | "
            f"{d.night_cloud_pct:>12.0f} | {d.temp_min:>5.1f}/{d.temp_max:<5.1f}{storm}"
        )
    return "\n".join(lines)


def _isotherm_above(isotherm: str, elevation_m: int | None) -> bool:
    """Return True if the nighttime isotherm is above the given elevation."""
    if elevation_m is None or isotherm in ("n/a", ""):
        return False
    if isotherm.startswith(">"):
        # isotherm is above the 700hPa level — almost certainly above any alpine summit
        return True
    if isotherm.startswith("<"):
        # isotherm is below the 850hPa level — refreeze guaranteed at summit
        return False
    try:
        return int(isotherm.rstrip("m")) > elevation_m
    except ValueError:
        return False


def _table_row(d: _DayForecast, today_str: str, elevation_max: int | None = None) -> str:
    """Format a single _DayForecast as a markdown table row."""
    storm = " ⚠" if d.storm else ""
    date_cell = f"**{d.date}**" if d.date == today_str else d.date
    wind_cell = f"{d.wind_850hpa:.0f} km/h" if d.wind_850hpa > 0 else "—"
    cloud_cell = f"{d.night_cloud_pct:.0f}%" if d.night_cloud_pct >= 0 else "—"
    iso_raw = d.nighttime_isotherm if d.nighttime_isotherm != "n/a" else "—"
    # Flag when the isotherm is above the summit — no overnight refreeze
    if _isotherm_above(d.nighttime_isotherm, elevation_max):
        iso_cell = f"**{iso_raw} ⚠**"
    else:
        iso_cell = iso_raw
    return (
        f"| {date_cell}{storm} "
        f"| {d.snowfall_cm:.1f} cm "
        f"| {d.gusts_max:.0f} km/h "
        f"| {wind_cell} "
        f"| {iso_cell} "
        f"| {cloud_cell} "
        f"| {d.temp_min:.1f}/{d.temp_max:.1f}°C |"
    )


_TABLE_HEADER = (
    "| Date | Snow | Gusts | Wind ~1500m | Night isotherm | Night cloud | T min/max |\n"
    "|------|------|-------|-------------|----------------|-------------|-----------|"
)


def _format_ui_table(hist_days: list[_DayForecast], forecast_days: list[_DayForecast],
                     today_str: str, elevation_max: int | None = None) -> str:
    """Format historical and forecast days as two labelled markdown tables."""
    parts = []
    if hist_days:
        parts.append("**Past 7 days**\n\n" + _TABLE_HEADER)
        parts.extend(_table_row(d, today_str, elevation_max) for d in hist_days)
    if forecast_days:
        parts.append("\n**7-day forecast**\n\n" + _TABLE_HEADER)
        parts.extend(_table_row(d, today_str, elevation_max) for d in forecast_days)
    return "\n".join(parts)


def _fetch_historical_days(lat: float, lon: float, today: date, days: int = 7) -> list[_DayForecast]:
    """
    Fetch the past `days` days from the archive API as _DayForecast objects.
    Pressure-level fields (isotherm, altitude wind, night cloud) are not available
    from the archive and are set to sentinel values ("—" / -1).
    """
    start = (today - timedelta(days=days)).isoformat()
    end   = (today - timedelta(days=1)).isoformat()

    r = _session.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude": lat,
            "longitude": lon,
            "start_date": start,
            "end_date": end,
            "daily": [
                "snowfall_sum",
                "precipitation_sum",
                "windspeed_10m_max",
                "windgusts_10m_max",
                "temperature_2m_min",
                "temperature_2m_max",
            ],
            "timezone": "UTC",
        },
        timeout=20,
    )
    r.raise_for_status()
    d = r.json().get("daily", {})
    dates = d.get("time", [])

    result = []
    for i, day_str in enumerate(dates):
        snow  = d["snowfall_sum"][i]     or 0.0
        prec  = d["precipitation_sum"][i] or 0.0
        wind  = d["windspeed_10m_max"][i] or 0.0
        gust  = d["windgusts_10m_max"][i] or 0.0
        t_min = d["temperature_2m_min"][i] or 0.0
        t_max = d["temperature_2m_max"][i] or 0.0
        result.append(_DayForecast(
            date=day_str,
            precip_mm=round(prec, 1),
            snowfall_cm=round(snow, 1),
            wind_10m_max=round(wind, 0),
            gusts_max=round(gust, 0),
            temp_min=round(t_min, 1),
            temp_max=round(t_max, 1),
            wind_850hpa=0.0,        # not available from archive
            nighttime_isotherm="n/a",
            night_cloud_pct=-1,     # sentinel: not available
            storm=snow > 15 or gust > 80,
        ))
    return result


def _fetch_historical_text(lat: float, lon: float, today: date, days_back: int = 90) -> str:
    """Return a sentence summarising snowfall in the past `days_back` days."""
    start = (today - timedelta(days=days_back)).isoformat()
    end   = (today - timedelta(days=3)).isoformat()  # archive lags a few days

    r = _session.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude": lat,
            "longitude": lon,
            "start_date": start,
            "end_date": end,
            "daily": ["snowfall_sum"],
            "timezone": "UTC",
        },
        timeout=20,
    )
    r.raise_for_status()
    d = r.json().get("daily", {})
    dates     = d.get("time", [])
    snowfalls = d.get("snowfall_sum", [])

    total = sum(s for s in snowfalls if s is not None)
    big_snow = [
        (day, s) for day, s in zip(dates, snowfalls) if s is not None and s > 15
    ]
    parts = [f"Total snowfall past {days_back} days: {total:.0f} cm."]
    if big_snow:
        biggest = max(big_snow, key=lambda x: x[1])
        parts.append(
            f"{len(big_snow)} large event(s) >15 cm/day "
            f"(largest: {biggest[1]:.0f} cm on {biggest[0]})."
        )
    else:
        parts.append("No single-day events above 15 cm.")
    return " ".join(parts)


def fetch_weather(route: dict, today: date) -> "WeatherSummary | None":
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

    forecast_days: list[_DayForecast] = []
    try:
        forecast_days = _build_forecast(lat, lon)
    except Exception as e:
        errors.append(f"Forecast unavailable: {e}")

    hist_days: list[_DayForecast] = []
    try:
        hist_days = _fetch_historical_days(lat, lon, today)
    except Exception as e:
        errors.append(f"Recent history unavailable: {e}")

    historical_text = ""
    try:
        historical_text = _fetch_historical_text(lat, lon, today)
    except Exception as e:
        errors.append(f"Historical data unavailable: {e}")

    today_str = today.isoformat()
    elevation_max = route.get("elevation_max")
    elev_int = int(elevation_max) if elevation_max is not None else None
    return WeatherSummary(
        fetch_date=today_str,
        coords=(lat, lon),
        forecast_text=_format_forecast_text(forecast_days) if forecast_days else "",
        historical_text=historical_text,
        ui_table=_format_ui_table(hist_days, forecast_days, today_str, elev_int),
        fetch_errors=errors,
    )
