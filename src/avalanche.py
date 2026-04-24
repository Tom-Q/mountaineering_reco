"""
Avalanche bulletin integration.

Sources:
- Météo-France BRA (French massifs) — requires METEOFRANCE_API_KEY in .env
- EAWS CAAMLv6 feeds (Switzerland, Italy, Austria) — public, no auth

Geographic lookup for France: point-in-polygon against liste-massifs.geojson.
Geographic lookup for EAWS: micro-region GeoJSON files from regions.avalanches.org,
fetched at runtime and cached for 7 days.
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import requests_cache
from dotenv import load_dotenv

load_dotenv()

_MF_API_KEY = os.getenv("METEOFRANCE_API_KEY", "")
_MF_BRA_URL = "https://public-api.meteofrance.fr/public/DPBRA/v1/massif/BRA"
_MF_IMAGE_URL = "https://public-api.meteofrance.fr/public/DPBRA/v1/massif/image"
_MASSIF_GEOJSON = Path(__file__).parent.parent / "domain_knowledge" / "liste-massifs.geojson"

_EAWS_MICRO_REGIONS_BASE = "https://regions.avalanches.org/micro-regions"

# Two cache TTLs: short for bulletins (issued 1–2× per day), long for region geometry (rarely changes)
_bulletin_session = requests_cache.CachedSession(".cache/avalanche_cache", expire_after=3600 * 6)
_region_session   = requests_cache.CachedSession(".cache/eaws_regions_cache", expire_after=3600 * 24 * 7)

DANGER_LABELS = {1: "Low", 2: "Limited", 3: "Considerable", 4: "High", 5: "Very High"}

# CAAMLv6 text values → integer danger level
_CAAML_DANGER = {
    "low": 1, "limited": 2, "considerable": 3, "high": 4, "very_high": 5,
    "no_snow": 0, "no_rating": 0,
}

# ---------------------------------------------------------------------------
# EAWS provider definitions
# ---------------------------------------------------------------------------
# Each entry: (provider_codes_for_geo_lookup, bulletin_feed_url, display_name)
# provider_codes must match the filenames at regions.avalanches.org/micro-regions/
_EAWS_PROVIDERS = [
    # EUREGIO: South Tyrol (IT-32-BZ) + Trentino (IT-32-TN) + Tyrol (AT-07)
    (
        ["IT-32-BZ", "IT-32-TN", "AT-07"],
        "https://static.avalanche.report/bulletins/latest/EUREGIO_en_CAAMLv6.json",
        "EUREGIO",
    ),
    # Valle d'Aosta
    (
        ["IT-23"],
        "https://static.avalanche.report/bulletins/latest/IT-23_en_CAAMLv6.json",
        "Valle d'Aosta",
    ),
    # Piemonte
    (
        ["IT-21"],
        "https://static.avalanche.report/bulletins/latest/IT-21_en_CAAMLv6.json",
        "Piemonte",
    ),
    # Lombardia (Valtellina, Valchiavenna)
    (
        ["IT-25"],
        "https://static.avalanche.report/bulletins/latest/IT-25_en_CAAMLv6.json",
        "Lombardia",
    ),
    # Switzerland (SLF)
    (
        ["CH"],
        "https://aws.slf.ch/api/bulletin/caaml/en/json",
        "Switzerland (SLF)",
    ),
    # Carinthia
    (
        ["AT-02"],
        "https://static.avalanche.report/bulletins/latest/AT-02_en_CAAMLv6.json",
        "Carinthia",
    ),
    # Salzburg
    (
        ["AT-05"],
        "https://static.avalanche.report/bulletins/latest/AT-05_en_CAAMLv6.json",
        "Salzburg",
    ),
    # Styria
    (
        ["AT-06"],
        "https://static.avalanche.report/bulletins/latest/AT-06_en_CAAMLv6.json",
        "Styria",
    ),
]

# ---------------------------------------------------------------------------
# Regions not yet integrated
# ---------------------------------------------------------------------------
#
# Slovenia — CAAMLv6, same format as above. Date-keyed URL (no confirmed /latest/ path):
#   https://static.lawinen-warnung.eu/bulletins/2026-04-13/2026-04-13_SI_sl_SI_CAAMLv6.json
# Micro-regions likely at: https://regions.avalanches.org/micro-regions/SI_micro-regions.geojson.json
# Could be added to _EAWS_PROVIDERS once a stable /latest/ URL is confirmed.
#
# Spanish Pyrenees (AEMET) — not machine-readable; PDF bulletin only:
#   https://www.aemet.es/es/eltiempo/prediccion/montana/boletin_peligro_aludes
# Future: could link to this URL in the "no bulletin available" warning for routes in that area.


@dataclass
class AvalancheBulletin:
    source: str                        # "meteofrance" | "eaws"
    provider_name: str                 # human-readable: "Météo-France", "EUREGIO", "Switzerland (SLF)", …
    massif_name: str
    danger_level: int                  # 1–5 max danger for the day
    danger_level_lo: int | None        # danger below split altitude
    danger_level_hi: int | None        # danger above split altitude
    danger_split_altitude: int | None  # metres; None if uniform danger
    valid_until: str                   # ISO datetime (truncated to minute)
    aspects_at_risk: list[str]         # e.g. ["N", "NE", "E", "NW"]
    summary: str                       # concise summary
    full_text: str                     # detailed narrative (truncated for LLM)
    llm_text: str                      # pre-formatted block for LLM injection
    ui_md: str                         # markdown for Streamlit display
    image_meteo: bytes | None = None   # MF only: apercu-meteo PNG
    image_7days: bytes | None = None   # MF only: sept-derniers-jours PNG
    fetch_error: str | None = None


from src.spatial import point_in_geometry


# ---------------------------------------------------------------------------
# French massif lookup (local GeoJSON)
# ---------------------------------------------------------------------------

_massif_features: list | None = None


def _find_massif(lat: float, lon: float) -> dict | None:
    global _massif_features
    if _massif_features is None:
        try:
            with open(_MASSIF_GEOJSON, encoding="utf-8") as f:
                _massif_features = json.load(f)["features"]
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            print(f"[avalanche] Failed to load massif GeoJSON: {exc}")
            return None
    for feature in _massif_features:
        if point_in_geometry(lat, lon, feature["geometry"]):
            return feature["properties"]
    return None


# ---------------------------------------------------------------------------
# EAWS micro-region lookup (fetched + cached)
# ---------------------------------------------------------------------------

_micro_region_cache: dict[str, list] = {}


def _load_micro_regions(provider_code: str) -> list:
    """
    Fetch and cache the micro-region GeoJSON features for a provider code.
    Returns an empty list on network/parse error.
    """
    if provider_code in _micro_region_cache:
        return _micro_region_cache[provider_code]

    url = f"{_EAWS_MICRO_REGIONS_BASE}/{provider_code}_micro-regions.geojson.json"
    try:
        r = _region_session.get(url, timeout=15)
        r.raise_for_status()
        features = r.json().get("features", [])
    except Exception:
        features = []

    _micro_region_cache[provider_code] = features
    return features


def _feature_region_id(feature: dict) -> str | None:
    """Extract the region ID from a micro-region GeoJSON feature."""
    props = feature.get("properties", {})
    return props.get("id") or props.get("ID") or feature.get("id")


def _find_eaws_region(lat: float, lon: float, provider_codes: list[str]) -> tuple[str, str] | None:
    """
    Search provider_codes in order. Returns (regionID, provider_code) for the
    first micro-region polygon containing (lat, lon), or None.
    """
    for code in provider_codes:
        for feature in _load_micro_regions(code):
            geom = feature.get("geometry")
            if geom and point_in_geometry(lat, lon, geom):
                region_id = _feature_region_id(feature)
                if region_id:
                    return region_id, code
    return None


# ---------------------------------------------------------------------------
# CAAMLv6 fetch + parse
# ---------------------------------------------------------------------------

def _fetch_caaml_bulletins(feed_url: str) -> list[dict]:
    """
    Fetch a CAAMLv6 JSON feed and return the bulletins list.
    Handles both {"bulletins": [...]} and bare [...] top-level shapes.
    Returns [] on error.
    """
    try:
        r = _bulletin_session.get(feed_url, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("bulletins", [])
    except Exception:
        return []


def _bulletin_for_region(bulletins: list[dict], region_id: str) -> dict | None:
    for b in bulletins:
        for reg in b.get("regions", []):
            if reg.get("regionID") == region_id:
                return b
    return None


def _parse_caaml_danger(ratings: list[dict]) -> tuple[int, int | None, int | None, int | None]:
    """
    Parse dangerRatings list into (max_danger, lo_danger, hi_danger, split_alt).

    Returns uniform danger if no elevation split, or split values if two distinct
    ratings with elevation bounds exist. Prefers all_day over earlier/later.
    """
    # Prefer all_day; fall back to any period
    preferred = [r for r in ratings if r.get("validTimePeriod") == "all_day"] or ratings

    # Collect (danger_int, has_upper_bound, has_lower_bound, altitude)
    parsed = []
    for r in preferred:
        val = _CAAML_DANGER.get(r.get("mainValue", ""), 0)
        elev = r.get("elevation", {})
        upper = elev.get("upperBound")  # e.g. "2500" → below this altitude
        lower = elev.get("lowerBound")  # e.g. "2500" → above this altitude
        parsed.append((val, upper, lower))

    if not parsed:
        return 0, None, None, None

    max_danger = max(v for v, _upper, _lower in parsed)

    # Detect altitude split: one entry with upperBound and one with lowerBound
    upper_entries = [(v, upper_bound) for v, upper_bound, lower_bound in parsed if upper_bound is not None and lower_bound is None]
    lower_entries = [(v, lower_bound) for v, upper_bound, lower_bound in parsed if lower_bound is not None and upper_bound is None]

    if upper_entries and lower_entries:
        lo_val = upper_entries[0][0]   # danger for elevations below the split
        hi_val = lower_entries[0][0]   # danger for elevations above the split
        # The split altitude appears in both entries and should be the same value
        split = int(upper_entries[0][1])
        if lo_val != hi_val:
            return max_danger, lo_val, hi_val, split

    return max_danger, None, None, None


def _parse_caaml_aspects(problems: list[dict]) -> list[str]:
    """Union of all aspects across avalanche problems, in compass order."""
    order = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    seen = set()
    for p in problems:
        seen.update(p.get("aspects", []))
    return [a for a in order if a in seen]


def _parse_caaml_problems_text(problems: list[dict]) -> str:
    """Format avalanche problems as a readable list for the LLM."""
    lines = []
    for p in problems:
        ptype = p.get("problemType", "").replace("_", " ")
        aspects = ", ".join(p.get("aspects", []))
        elev = p.get("elevation") or {}
        if elev.get("lowerBound"):
            elev_str = f"above {elev['lowerBound']}m"
        elif elev.get("upperBound"):
            elev_str = f"below {elev['upperBound']}m"
        else:
            elev_str = "all elevations"
        stability = p.get("snowpackStability", "")
        size = p.get("avalancheSize")
        parts = [f"{ptype} ({elev_str}"]
        if aspects:
            parts[0] += f", aspects: {aspects}"
        parts[0] += ")"
        if stability:
            parts.append(f"stability: {stability.replace('_', ' ')}")
        if size:
            parts.append(f"size {size}/5")
        lines.append(", ".join(parts))
    return "; ".join(lines) if lines else ""


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_caaml_bulletin(bulletin: dict, region_name: str, provider_name: str) -> AvalancheBulletin:
    ratings  = bulletin.get("dangerRatings", [])
    problems = bulletin.get("avalancheProblems", [])

    danger_max, danger_lo, danger_hi, split_alt = _parse_caaml_danger(ratings)
    aspects   = _parse_caaml_aspects(problems)
    problems_text = _parse_caaml_problems_text(problems)

    # Summary: use highlights; fall back to avalanche activity comment
    highlights = _strip_html((bulletin.get("highlights") or "").strip())
    activity   = (bulletin.get("avalancheActivity") or {})
    act_comment = _strip_html((activity.get("comment") or activity.get("highlights") or "").strip())
    snowpack    = _strip_html(((bulletin.get("snowpackStructure") or {}).get("comment") or "").strip())

    summary = highlights or act_comment[:300] or "—"

    full_parts = [t for t in [act_comment, snowpack] if t]
    full_text = "\n\n".join(full_parts)

    valid_until = (bulletin.get("validTime") or {}).get("endTime", "")[:16]

    b = AvalancheBulletin(
        source="eaws",
        provider_name=provider_name,
        massif_name=region_name,
        danger_level=danger_max,
        danger_level_lo=danger_lo,
        danger_level_hi=danger_hi,
        danger_split_altitude=split_alt,
        valid_until=valid_until,
        aspects_at_risk=aspects,
        summary=summary,
        full_text=full_text,
        llm_text="",
        ui_md="",
    )
    b.llm_text = _build_llm_text(b)
    b.ui_md    = _build_ui_md(b)

    # Append problems list to LLM text if available
    if problems_text:
        b.llm_text += f"\n\nAvalanche problems: {problems_text}"

    return b


def fetch_eaws_bulletin(lat: float, lon: float) -> AvalancheBulletin | None:
    """
    Find and return the EAWS bulletin for the given coordinates, or None.
    Tries providers in order; returns the first successful match.
    """
    for provider_codes, feed_url, display_name in _EAWS_PROVIDERS:
        result = _find_eaws_region(lat, lon, provider_codes)
        if result is None:
            continue
        region_id, _ = result
        bulletins = _fetch_caaml_bulletins(feed_url)
        if not bulletins:
            continue
        bulletin = _bulletin_for_region(bulletins, region_id)
        if bulletin is None:
            continue
        # Use the region name from the bulletin if available
        region_name = next(
            (r["name"] for r in bulletin.get("regions", []) if r.get("regionID") == region_id),
            region_id,
        )
        return _parse_caaml_bulletin(bulletin, region_name, display_name)
    return None


# ---------------------------------------------------------------------------
# XML parsing helpers (MF BRA)
# ---------------------------------------------------------------------------

def _text(elem) -> str:
    return (elem.text or "").strip() if elem is not None else ""


def _parse_aspects(pente_elem) -> list[str]:
    if pente_elem is None:
        return []
    keys = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return [k for k in keys if pente_elem.get(k, "").lower() == "true"]


# ---------------------------------------------------------------------------
# Text formatters (source-agnostic)
# ---------------------------------------------------------------------------

def _danger_str_short(b: AvalancheBulletin) -> str:
    if b.danger_split_altitude and b.danger_level_lo is not None and b.danger_level_hi is not None:
        return (
            f"{b.danger_level_hi}/5 ({DANGER_LABELS.get(b.danger_level_hi, '')}) "
            f"above {b.danger_split_altitude}m, "
            f"{b.danger_level_lo}/5 ({DANGER_LABELS.get(b.danger_level_lo, '')}) below"
        )
    lbl = DANGER_LABELS.get(b.danger_level, "")
    return f"{b.danger_level}/5 ({lbl})"


def _build_llm_text(b: AvalancheBulletin) -> str:
    aspects = ", ".join(b.aspects_at_risk) if b.aspects_at_risk else "not specified"
    source_label = b.provider_name or b.source
    lines = [
        f"## Avalanche bulletin ({source_label})",
        f"Region: {b.massif_name}  |  Danger: {_danger_str_short(b)}  |  Valid until: {b.valid_until}",
        f"Aspects at risk: {aspects}",
        "",
        f"Summary: {b.summary}",
    ]
    if b.full_text:
        lines += ["", f"Detailed conditions:\n{b.full_text[:1500]}"]
    return "\n".join(lines)


def _build_ui_md(b: AvalancheBulletin) -> str:
    if b.danger_split_altitude and b.danger_level_lo is not None and b.danger_level_hi is not None:
        danger_md = (
            f"**{b.danger_level_hi}/5** above {b.danger_split_altitude}m, "
            f"**{b.danger_level_lo}/5** below"
        )
    else:
        lbl = DANGER_LABELS.get(b.danger_level, "")
        danger_md = f"**{b.danger_level}/5 — {lbl}**"

    source_label = b.provider_name or b.source
    aspects = ", ".join(b.aspects_at_risk) if b.aspects_at_risk else "—"
    return (
        f"**{source_label} — {b.massif_name}**  ·  Danger: {danger_md}  ·  "
        f"Valid until: {b.valid_until}\n\n"
        f"**Aspects at risk:** {aspects}\n\n"
        f"**Summary:** {b.summary}"
    )


# ---------------------------------------------------------------------------
# Météo-France BRA fetch
# ---------------------------------------------------------------------------

def _error_bulletin(name: str, msg: str, provider: str = "Météo-France") -> AvalancheBulletin:
    return AvalancheBulletin(
        source="meteofrance", provider_name=provider, massif_name=name,
        danger_level=0, danger_level_lo=None, danger_level_hi=None,
        danger_split_altitude=None, valid_until="", aspects_at_risk=[],
        summary="", full_text="", llm_text="", ui_md="",
        fetch_error=msg,
    )


def _fetch_bra_images(massif_code: int) -> tuple[bytes | None, bytes | None]:
    if not _MF_API_KEY:
        return None, None

    def _get(endpoint: str) -> bytes | None:
        try:
            r = _bulletin_session.get(
                f"{_MF_IMAGE_URL}/{endpoint}",
                params={"id-massif": massif_code},
                headers={"apikey": _MF_API_KEY},
                timeout=15,
            )
            return r.content if r.status_code == 200 else None
        except Exception:
            return None

    return _get("apercu-meteo"), _get("sept-derniers-jours")


def fetch_bra_france(lat: float, lon: float) -> AvalancheBulletin | None:
    """
    Fetch the BRA for the French massif containing (lat, lon).
    Returns None if outside all French massifs.
    Returns an AvalancheBulletin with fetch_error set on API failure.
    """
    massif = _find_massif(lat, lon)
    if massif is None:
        return None

    code = massif["code"]
    name = massif["title"]

    if not _MF_API_KEY:
        return _error_bulletin(name, "METEOFRANCE_API_KEY not set — BRA unavailable.")

    try:
        r = _bulletin_session.get(
            _MF_BRA_URL,
            params={"id-massif": code, "format": "xml"},
            headers={"apikey": _MF_API_KEY},
            timeout=15,
        )
        if r.status_code in (401, 403):
            return _error_bulletin(
                name,
                f"Météo-France API key rejected (HTTP {r.status_code}) — key may have expired.",
            )
        r.raise_for_status()
    except Exception as exc:
        return _error_bulletin(name, f"BRA fetch error: {exc}")

    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as exc:
        return _error_bulletin(name, f"BRA XML parse error: {exc}")

    massif_name = root.get("MASSIF", name)
    valid_until = root.get("DATEECHEANCE", "")[:16]

    cartouche   = root.find("CARTOUCHERISQUE")
    risque_elem = cartouche.find("RISQUE") if cartouche is not None else None
    pente_elem  = cartouche.find("PENTE")  if cartouche is not None else None
    resume_elem = cartouche.find("RESUME") if cartouche is not None else None
    stabilite   = root.find("STABILITE")

    danger_max = int(risque_elem.get("RISQUEMAXI", 0)) if risque_elem is not None else 0
    risque1    = (risque_elem.get("RISQUE1", "") or "") if risque_elem is not None else ""
    risque2    = (risque_elem.get("RISQUE2", "") or "") if risque_elem is not None else ""
    alt_str    = (risque_elem.get("ALTITUDE", "") or "") if risque_elem is not None else ""

    danger_lo = int(risque1) if risque1.isdigit() else None
    danger_hi = int(risque2) if risque2.isdigit() else None
    split_alt = int(alt_str) if alt_str.isdigit() else None

    if not split_alt or danger_lo == danger_hi:
        danger_lo = danger_hi = split_alt = None

    aspects   = _parse_aspects(pente_elem)
    summary   = _text(resume_elem)
    full_text = _text(stabilite.find("TEXTE")) if stabilite is not None else ""

    img_meteo, img_7days = _fetch_bra_images(code)

    b = AvalancheBulletin(
        source="meteofrance",
        provider_name="Météo-France",
        massif_name=massif_name,
        danger_level=danger_max,
        danger_level_lo=danger_lo,
        danger_level_hi=danger_hi,
        danger_split_altitude=split_alt,
        valid_until=valid_until,
        aspects_at_risk=aspects,
        summary=summary,
        full_text=full_text,
        llm_text="",
        ui_md="",
        image_meteo=img_meteo,
        image_7days=img_7days,
    )
    b.llm_text = _build_llm_text(b)
    b.ui_md    = _build_ui_md(b)
    return b


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------

def fetch_avalanche_bulletin(lat: float, lon: float) -> list[AvalancheBulletin]:
    """
    Return all applicable avalanche bulletins for the given coordinates.
    Bulletins from multiple providers are returned when a route is near a border.
    """
    bulletins = []

    fr = fetch_bra_france(lat, lon)
    if fr is not None:
        bulletins.append(fr)

    eaws = fetch_eaws_bulletin(lat, lon)
    if eaws is not None:
        bulletins.append(eaws)

    return bulletins
