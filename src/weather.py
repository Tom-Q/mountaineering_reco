"""
Weather data integration for route analysis.

Fetches a 7-day forecast and a snowfall history from Open-Meteo
(free, no authentication required) for the route's coordinates.

The forecast uses hourly data aggregated to daily values, including pressure-level
variables (925/850/700/600/500 hPa) for altitude wind and 0°C isotherm computation.

Snowfall history has two signals:
- Recent (past 15 days, always): tracks total and large events (>15 cm/day).
- Seasonal (since season start): total accumulation, only shown when in-season.
  Season windows are range-aware — see domain_knowledge/snow_seasons.yaml. Ranges
  with no seasonal concept (Himalaya, equatorial/tropical) show a note or recent-only.
"""

import json
import math
import yaml
from dataclasses import dataclass, field
from datetime import date, timedelta

import requests
import requests_cache
from pathlib import Path

_session = requests_cache.CachedSession(".cache/weather_cache", expire_after=3600)

_snow_seasons = yaml.safe_load(
    (Path(__file__).parent.parent / "domain_knowledge" / "snow_seasons.yaml").read_text()
)["ranges"]


@dataclass
class WeatherSummary:
    fetch_date: str
    coords: tuple[float, float]
    forecast_text: str         # pre-formatted for LLM injection
    historical_text: str       # snowfall history (recent + seasonal) for LLM injection
    ui_table: str              # markdown table for display in the app
    daylight_text: str = ""    # sunrise/sunset/dawn/dusk for LLM injection
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
    except (TypeError, ValueError, KeyError, IndexError):
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


def _build_all_days(lat: float, lon: float, past_days: int = 7,
                    elevation_m: int | None = None) -> list[_DayForecast]:
    """
    Fetch past `past_days` days + 7-day forecast from Open-Meteo in one call.

    Returns all days in chronological order. The caller splits on today's date.
    Pressure-level data (5 levels: 925/850/700/600/500 hPa) is available for
    both historical and forecast days via the `past_days` parameter.

    Isotherms:
    - refreeze_isotherm: minimum 0°C altitude over 00–09 UTC (coldest hours)
    - melt_isotherm:     maximum 0°C altitude over 07–23 UTC (hottest hours)
    """
    params: dict = {
        "latitude": lat,
        "longitude": lon,
    }
    if elevation_m is not None:
        params["elevation"] = elevation_m
    r = _session.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            **params,
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

        find_min=True  → refreeze isotherm (lowest 0°C altitude during night/morning)
        find_min=False → melt isotherm (highest 0°C altitude during the day)
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


def _fmt_isotherm(iso: str, hour: int | None) -> str:
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
            f"{d.wind_850hpa:>17.0f} | {_fmt_isotherm(d.refreeze_isotherm, d.refreeze_hour):>21} | "
            f"{_fmt_isotherm(d.melt_isotherm, d.melt_hour):>16} | {d.night_cloud_pct:>12.0f} | "
            f"{d.temp_min:>5.1f}/{d.temp_max:<5.1f}{storm}"
        )
    return "\n".join(lines)


def _compute_daylight_text(lat: float, lon: float, dates: list[str]) -> str:
    """Compute civil dawn/sunrise/sunset/dusk for each date at the given coordinates.

    Times are returned in local time (timezone resolved from coordinates).
    Handles polar day/night gracefully.
    """
    from astral import LocationInfo
    from astral.sun import sun
    from zoneinfo import ZoneInfo
    try:
        from timezonefinder import TimezoneFinder
        tz_name = TimezoneFinder().timezone_at(lat=lat, lng=lon) or "UTC"
    except Exception:
        tz_name = "UTC"

    tz = ZoneInfo(tz_name)
    observer = LocationInfo(latitude=lat, longitude=lon).observer

    # Compute UTC offset label for the header (use first date)
    try:
        import datetime as _dt
        sample_dt = _dt.datetime(2000, 6, 21, 12, tzinfo=tz)
        offset_h = int(sample_dt.utcoffset().total_seconds() // 3600)
        offset_label = f"UTC{offset_h:+d}"
    except Exception:
        offset_label = ""

    lines = [f"Daylight ({tz_name}, {offset_label}):"]
    for date_str in dates:
        d = date.fromisoformat(date_str)
        try:
            s = sun(observer, date=d, tzinfo=tz)
            dawn_s    = s["dawn"].strftime("%H:%M")
            sunrise_s = s["sunrise"].strftime("%H:%M")
            sunset_s  = s["sunset"].strftime("%H:%M")
            dusk_s    = s["dusk"].strftime("%H:%M")
            daylight_secs = (s["sunset"] - s["sunrise"]).total_seconds()
            daylight_h = int(daylight_secs // 3600)
            daylight_m = int((daylight_secs % 3600) // 60)
            lines.append(
                f"{date_str}  dawn {dawn_s}  sunrise {sunrise_s}  "
                f"sunset {sunset_s}  dusk {dusk_s}  ({daylight_h}h {daylight_m:02d}m)"
            )
        except Exception:
            lines.append(f"{date_str}  polar day or night — no standard dawn/dusk")

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
    rfz_fmt  = _fmt_isotherm(d.refreeze_isotherm, d.refreeze_hour)
    melt_fmt = _fmt_isotherm(d.melt_isotherm,     d.melt_hour)
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



def _season_start_for_range(range_name: str, today: date) -> date | None:
    """
    Return the start date of the current snow season for a range, or None if:
    - the range has no seasonal window in snow_seasons.yaml (null entry), or
    - today falls outside the season window.

    Handles year-spanning NH seasons (e.g. Nov–May crosses a calendar year).
    """
    entry = _snow_seasons.get(range_name)
    if not entry:
        return None

    sm, sd = entry["season_start"]
    em, ed = entry["season_end"]
    crosses_year = sm > em  # True for NH winters (Nov–May), False for SH summers (Apr–Sep)

    if crosses_year:
        today_in_end_half   = (today.month, today.day) <= (em, ed)
        today_in_start_half = (today.month, today.day) >= (sm, sd)
        if today_in_end_half:
            # e.g. today is Feb 2026 → season started Nov 2025
            return date(today.year - 1, sm, sd)
        elif today_in_start_half:
            # e.g. today is Nov 2025 → season just started
            return date(today.year, sm, sd)
        else:
            return None  # out of season (e.g. Alps in July)
    else:
        # Season stays within one calendar year (SH ranges)
        if (today.month, today.day) >= (sm, sd) and (today.month, today.day) <= (em, ed):
            return date(today.year, sm, sd)
        return None


def _fetch_snowfall_summary(
    lat: float,
    lon: float,
    today: date,
    range_name: str,
    elevation_m: int | None = None,
) -> str:
    """
    Return a formatted snowfall summary for the given location and range.

    Always includes recent snowfall (past 15 days, events >15 cm/day flagged).
    Adds seasonal accumulation (since season start) when the range is in-season.
    Himalaya returns a fixed note about monsoon-driven accumulation instead.
    """
    if range_name == "himalaya":
        return (
            "Snowpack note: this is a monsoon-accumulation range. Seasonal snowfall "
            "figures are not meaningful here — consult local sources (guiding outfits, "
            "Himalayan Experience bulletins) for current snowpack conditions."
        )

    base_params: dict = {"latitude": lat, "longitude": lon}
    if elevation_m is not None:
        base_params["elevation"] = elevation_m

    archive_end = (today - timedelta(days=3)).isoformat()  # archive lags ~3 days
    parts: list[str] = []

    # --- Recent snowfall (always shown) ---
    recent_start = (today - timedelta(days=15)).isoformat()
    try:
        r = _session.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                **base_params,
                "start_date": recent_start,
                "end_date": archive_end,
                "daily": ["snowfall_sum"],
                "timezone": "UTC",
            },
            timeout=20,
        )
        r.raise_for_status()
        d = r.json().get("daily", {})
        dates     = d.get("time", [])
        snowfalls = d.get("snowfall_sum", [])
        recent_total = sum(s for s in snowfalls if s is not None)
        big_events = [(day, s) for day, s in zip(dates, snowfalls) if s is not None and s > 15]
        recent_str = f"Recent snowfall (past 15 days): {recent_total:.0f} cm total."
        if big_events:
            biggest = max(big_events, key=lambda x: x[1])
            recent_str += (
                f" {len(big_events)} large event(s) >15 cm/day"
                f" (largest: {biggest[1]:.0f} cm on {biggest[0]})."
            )
        else:
            recent_str += " No single-day events above 15 cm."
        parts.append(recent_str)
    except Exception as e:
        parts.append(f"Recent snowfall unavailable: {e}")

    # --- Seasonal accumulation (only when in-season) ---
    season_start = _season_start_for_range(range_name, today)
    if season_start is not None:
        try:
            r = _session.get(
                "https://archive-api.open-meteo.com/v1/archive",
                params={
                    **base_params,
                    "start_date": season_start.isoformat(),
                    "end_date": archive_end,
                    "daily": ["snowfall_sum"],
                    "timezone": "UTC",
                },
                timeout=20,
            )
            r.raise_for_status()
            d = r.json().get("daily", {})
            snowfalls = d.get("snowfall_sum", [])
            seasonal_total = sum(s for s in snowfalls if s is not None)
            parts.append(
                f"Seasonal accumulation since {season_start.strftime('%-d %b')}: {seasonal_total:.0f} cm."
            )
        except Exception as e:
            parts.append(f"Seasonal data unavailable: {e}")

    return " ".join(parts)


def fetch_weather_for_coords(
    lat: float,
    lon: float,
    today: date,
    elevation_m: int | None = None,
) -> "WeatherSummary":
    """
    Fetch weather data for explicit coordinates.

    This is the core fetch function, usable as a Claude tool.
    Avalanche data is NOT included — that is a separate tool.
    Errors in individual fetches are recorded in WeatherSummary.fetch_errors.
    """
    errors: list[str] = []

    hist_days:     list[_DayForecast] = []
    forecast_days: list[_DayForecast] = []
    try:
        all_days = _build_all_days(lat, lon, past_days=7, elevation_m=elevation_m)
        today_str_split = today.isoformat()
        hist_days     = [d for d in all_days if d.date <  today_str_split]
        forecast_days = [d for d in all_days if d.date >= today_str_split]
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code == 429:
            errors.append("Weather unavailable: Open-Meteo rate limit hit — try again in a moment.")
        elif code in (502, 503, 504):
            errors.append(f"Weather unavailable: Open-Meteo service error (HTTP {code}).")
        else:
            errors.append(f"Weather unavailable: HTTP {code}.")
    except Exception as e:
        errors.append(f"Weather unavailable: {e}")

    historical_text = ""
    try:
        from src.geo import classify_range
        range_name = classify_range(lat, lon)
        historical_text = _fetch_snowfall_summary(lat, lon, today, range_name, elevation_m=elevation_m)
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code == 429:
            errors.append("Snowfall data unavailable: Open-Meteo rate limit hit — try again in a moment.")
        elif code in (502, 503, 504):
            errors.append(f"Snowfall data unavailable: Open-Meteo service error (HTTP {code}).")
        else:
            errors.append(f"Snowfall data unavailable: HTTP {code}.")
    except Exception as e:
        errors.append(f"Snowfall data unavailable: {e}")

    daylight_text = ""
    try:
        forecast_dates = [d.date for d in forecast_days] if forecast_days else []
        if forecast_dates:
            daylight_text = _compute_daylight_text(lat, lon, forecast_dates)
    except Exception as e:
        errors.append(f"Daylight calculation unavailable: {e}")

    today_str = today.isoformat()
    return WeatherSummary(
        fetch_date=today_str,
        coords=(lat, lon),
        forecast_text=_format_forecast_text(forecast_days) if forecast_days else "",
        historical_text=historical_text,
        ui_table=_format_ui_table(hist_days, forecast_days, today_str, elevation_m),
        daylight_text=daylight_text,
        fetch_errors=errors,
    )


def fetch_weather(route: dict, today: date) -> "WeatherSummary | None":
    """
    Fetch weather data for a route. Returns None if no coordinates are available.

    Thin wrapper around fetch_weather_for_coords that extracts coords from the
    route dict and bundles avalanche bulletins for the Streamlit app flow.
    """
    coords = route_coords(route)
    if coords is None:
        return None

    lat, lon = coords
    elevation_max = route.get("elevation_max")
    elev_int = int(elevation_max) if elevation_max is not None else None

    return fetch_weather_for_coords(lat, lon, today, elevation_m=elev_int)
