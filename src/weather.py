"""
Weather data integration for route analysis.

Fetches a 7-day forecast and a 90-day historical summary from Open-Meteo
(free, no authentication required) for the route's coordinates.

The forecast uses hourly data aggregated to daily values, including pressure-level
variables (850/700 hPa) for altitude wind and nighttime 0°C isotherm computation.

TODO: consider whether it's worth getting a more precise isotherm estimate by including more hPa ranges.
Additionally, consider: is hPa temp a good enough approximation for isotherm?
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
    avalanche_bulletins: list = field(default_factory=list)  # list[AvalancheBulletin]


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
    refreeze_isotherm: str    # lowest 0°C altitude seen during 00–09 UTC
    refreeze_hour: int | None # UTC hour when refreeze minimum occurred
    melt_isotherm: str        # highest 0°C altitude seen during 07–23 UTC
    melt_hour: int | None     # UTC hour when melt maximum occurred
    night_cloud_pct: float    # mean cloud cover during 00–09 UTC
    storm: bool               # snowfall > 15cm OR gusts > 80 km/h


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


def _compute_isotherm(levels: list[tuple[float | None, float | None]]) -> str:
    """
    Derive the 0°C isotherm altitude from (temp_°C, geopotential_height_m) pairs.

    Accepts up to 5 pressure levels (925/850/700/600/500 hPa). Pairs with None
    values are skipped. Sorts by height and interpolates linearly between the two
    levels that straddle 0°C.

    Returns: "2450m" (interpolated), "<1580m" (below lowest level),
    ">5500m" (above highest level), or "n/a" if fewer than 2 valid levels.
    """
    valid = sorted(
        [(t, h) for t, h in levels if t is not None and h is not None],
        key=lambda x: x[1],
    )
    if len(valid) < 2:
        return "n/a"
    if valid[0][0] <= 0:
        return f"<{valid[0][1]:.0f}m"
    for i in range(len(valid) - 1):
        t_lo, h_lo = valid[i]
        t_hi, h_hi = valid[i + 1]
        if t_hi <= 0:
            frac = t_lo / (t_lo - t_hi)
            return f"{h_lo + frac * (h_hi - h_lo):.0f}m"
    return f">{valid[-1][1]:.0f}m"


def _iso_meters(iso: str) -> float | None:
    """
    Convert an isotherm string to a float altitude in metres for comparison.

    ">5500m" → 5500.1 (just above that level)
    "<760m"  → 759.9 (just below that level)
    "2450m"  → 2450.0
    "n/a"    → None
    """
    if iso == "n/a":
        return None
    if iso.startswith(">"):
        try:
            return float(iso[1:].rstrip("m")) + 0.1
        except ValueError:
            return None
    if iso.startswith("<"):
        try:
            return float(iso[1:].rstrip("m")) - 0.1
        except ValueError:
            return None
    try:
        return float(iso.rstrip("m"))
    except ValueError:
        return None


def _build_all_days(lat: float, lon: float, past_days: int = 7) -> list[_DayForecast]:
    """
    Fetch past `past_days` days + 7-day forecast from Open-Meteo in one call.

    Returns all days in chronological order. The caller splits on today's date.
    Pressure-level data (5 levels: 925/850/700/600/500 hPa) is available for
    both historical and forecast days via the `past_days` parameter.

    Isotherms:
    - refreeze_isotherm: minimum 0°C altitude over 00–09 UTC (coldest hours)
    - melt_isotherm:     maximum 0°C altitude over 07–23 UTC (hottest hours)
    """
    r = _session.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": [
                "temperature_2m",
                "temperature_925hPa",
                "temperature_850hPa",
                "temperature_700hPa",
                "temperature_600hPa",
                "temperature_500hPa",
                "geopotential_height_925hPa",
                "geopotential_height_850hPa",
                "geopotential_height_700hPa",
                "geopotential_height_600hPa",
                "geopotential_height_500hPa",
                "windspeed_850hPa",
                "cloudcover",
                "precipitation",
                "snowfall",
                "windspeed_10m",
                "windgusts_10m",
            ],
            "past_days": past_days,
            "forecast_days": 7,
            "timezone": "UTC",
        },
        timeout=15,
    )
    r.raise_for_status()
    h = r.json()["hourly"]

    times  = h["time"]
    t2m    = h["temperature_2m"]
    t925   = h["temperature_925hPa"]
    t850   = h["temperature_850hPa"]
    t700   = h["temperature_700hPa"]
    t600   = h["temperature_600hPa"]
    t500   = h["temperature_500hPa"]
    gh925  = h["geopotential_height_925hPa"]
    gh850  = h["geopotential_height_850hPa"]
    gh700  = h["geopotential_height_700hPa"]
    gh600  = h["geopotential_height_600hPa"]
    gh500  = h["geopotential_height_500hPa"]
    w850   = h["windspeed_850hPa"]
    cloud  = h["cloudcover"]
    precip = h["precipitation"]
    snow   = h["snowfall"]
    wind10 = h["windspeed_10m"]
    gusts  = h["windgusts_10m"]

    days: dict[str, dict] = {}
    for i, ts in enumerate(times):
        day_str = ts[:10]
        hour = int(ts[11:13])
        if day_str not in days:
            days[day_str] = {
                "t2m": [], "w850_day": [], "cloud_rfz": [],
                "precip": [], "snowfall": [], "wind10": [], "gusts": [],
                # Each entry: (hour, t925, t850, t700, t600, t500, gh925, gh850, gh700, gh600, gh500)
                "rfz_hourly": [],
                "mlt_hourly": [],
            }
        d = days[day_str]
        d["t2m"].append(t2m[i])
        d["precip"].append(precip[i])
        d["snowfall"].append(snow[i])
        d["wind10"].append(wind10[i])
        d["gusts"].append(gusts[i])

        if 6 <= hour < 18:
            d["w850_day"].append(w850[i])

        row = (hour,
               t925[i], t850[i], t700[i], t600[i], t500[i],
               gh925[i], gh850[i], gh700[i], gh600[i], gh500[i])
        if 0 <= hour < 9:
            d["cloud_rfz"].append(cloud[i])
            d["rfz_hourly"].append(row)
        if 7 <= hour < 24:
            d["mlt_hourly"].append(row)

    def _best_isotherm(hourly_rows: list, find_min: bool) -> tuple[str, int | None]:
        """
        Compute the isotherm at each hour from simultaneous pressure-level readings,
        then return the minimum (find_min=True) or maximum altitude and its UTC hour.
        This ensures each isotherm is physically consistent — all levels from the same moment.
        """
        best_m: float | None = None
        best_str = "n/a"
        best_hour: int | None = None
        for row in hourly_rows:
            hr = row[0]
            levels = list(zip(row[1:6], row[6:11]))   # (temp, height) for each of 5 levels
            iso = _compute_isotherm(levels)
            m = _iso_meters(iso)
            if m is None:
                continue
            if best_m is None or (find_min and m < best_m) or (not find_min and m > best_m):
                best_m, best_str, best_hour = m, iso, hr
        return best_str, best_hour

    result = []
    for day_str, d in sorted(days.items()):
        snow_sum  = sum(v for v in d["snowfall"] if v is not None)
        prec_sum  = sum(v for v in d["precip"]   if v is not None)
        wind_max  = max((v for v in d["wind10"]  if v is not None), default=0.0)
        gust_max  = max((v for v in d["gusts"]   if v is not None), default=0.0)
        temp_min  = min((v for v in d["t2m"]     if v is not None), default=0.0)
        temp_max  = max((v for v in d["t2m"]     if v is not None), default=0.0)
        w850_mean = _mean(d["w850_day"]) or 0.0
        cloud_rfz = _mean(d["cloud_rfz"]) or 0.0

        rfz_iso, rfz_hour = _best_isotherm(d["rfz_hourly"], find_min=True)
        mlt_iso, mlt_hour = _best_isotherm(d["mlt_hourly"], find_min=False)

        result.append(_DayForecast(
            date=day_str,
            precip_mm=round(prec_sum, 1),
            snowfall_cm=round(snow_sum, 1),
            wind_10m_max=round(wind_max, 0),
            gusts_max=round(gust_max, 0),
            temp_min=round(temp_min, 1),
            temp_max=round(temp_max, 1),
            wind_850hpa=round(w850_mean, 0),
            refreeze_isotherm=rfz_iso,
            refreeze_hour=rfz_hour,
            melt_isotherm=mlt_iso,
            melt_hour=mlt_hour,
            night_cloud_pct=round(cloud_rfz, 0),
            storm=snow_sum > 15 or gust_max > 80,
        ))
    return result


def _fmt_iso(iso: str, hour: int | None) -> str:
    """Format isotherm with its UTC hour: '2450m @03h', or '—' if n/a."""
    if iso == "n/a":
        return "—"
    return f"{iso} @{hour:02d}h" if hour is not None else iso


def _format_forecast_text(days: list[_DayForecast]) -> str:
    """Format forecast as compact text for LLM injection."""
    lines = [
        "Date       | Snow(cm) | Gusts(km/h) | Wind@850hPa(km/h) | Refreeze 0°C (00-09) | Melt 0°C (07-23) | Night cloud% | T min/max°C",
        "-----------|----------|-------------|-------------------|-----------------------|------------------|--------------|------------",
    ]
    for d in days:
        storm = " ⚠STORM" if d.storm else ""
        lines.append(
            f"{d.date} | {d.snowfall_cm:>8.1f} | {d.gusts_max:>11.0f} | "
            f"{d.wind_850hpa:>17.0f} | {_fmt_iso(d.refreeze_isotherm, d.refreeze_hour):>21} | "
            f"{_fmt_iso(d.melt_isotherm, d.melt_hour):>16} | {d.night_cloud_pct:>12.0f} | "
            f"{d.temp_min:>5.1f}/{d.temp_max:<5.1f}{storm}"
        )
    return "\n".join(lines)


def _isotherm_above(isotherm: str, elevation_m: int | None) -> bool:
    """Return True if the given isotherm altitude is above elevation_m."""
    if elevation_m is None or isotherm in ("n/a", ""):
        return False
    if isotherm.startswith(">"):
        return True
    if isotherm.startswith("<"):
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
    rfz_fmt  = _fmt_iso(d.refreeze_isotherm, d.refreeze_hour)
    melt_fmt = _fmt_iso(d.melt_isotherm,     d.melt_hour)
    rfz_cell  = f"**{rfz_fmt} ⚠**" if _isotherm_above(d.refreeze_isotherm, elevation_max) else rfz_fmt
    return (
        f"| {date_cell}{storm} "
        f"| {d.snowfall_cm:.1f} cm "
        f"| {d.gusts_max:.0f} km/h "
        f"| {wind_cell} "
        f"| {rfz_cell} "
        f"| {melt_fmt} "
        f"| {cloud_cell} "
        f"| {d.temp_min:.1f}/{d.temp_max:.1f}°C |"
    )


_TABLE_HEADER = (
    "| Date | Snow | Gusts | Wind ~1500m | Refreeze 0°C | Melt 0°C | Night cloud | T min/max |\n"
    "|------|------|-------|-------------|--------------|----------|-------------|-----------|"
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

    hist_days:     list[_DayForecast] = []
    forecast_days: list[_DayForecast] = []
    try:
        all_days = _build_all_days(lat, lon, past_days=7)
        today_str_split = today.isoformat()
        hist_days     = [d for d in all_days if d.date <  today_str_split]
        forecast_days = [d for d in all_days if d.date >= today_str_split]
    except Exception as e:
        errors.append(f"Weather unavailable: {e}")

    historical_text = ""
    try:
        historical_text = _fetch_historical_text(lat, lon, today)
    except Exception as e:
        errors.append(f"Historical data unavailable: {e}")

    from src.avalanche import fetch_avalanche_bulletin  # local import avoids circular dep
    avalanche_bulletins = []
    try:
        avalanche_bulletins = fetch_avalanche_bulletin(lat, lon)
    except Exception as e:
        errors.append(f"Avalanche bulletin unavailable: {e}")

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
        avalanche_bulletins=avalanche_bulletins,
    )
