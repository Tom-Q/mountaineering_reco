"""
Avalanche bulletin integration.

Phase 1: Météo-France BRA (Bulletin de Risque d'Avalanche) for French massifs.
Requires METEOFRANCE_API_KEY in .env (valid ~2 weeks, manually renewed).

Geographic lookup: point-in-polygon against the massif GeoJSON in the repo
root (liste-massifs.geojson). No external GIS library — uses ray-casting for
MultiPolygon containment.
"""

import json
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import requests_cache
from dotenv import load_dotenv

load_dotenv()

_MF_API_KEY = os.getenv("METEOFRANCE_API_KEY", "")
_MF_BRA_URL = "https://public-api.meteofrance.fr/public/DPBRA/v1/massif/BRA"
_MF_IMAGE_URL = "https://public-api.meteofrance.fr/public/DPBRA/v1/massif/image"
_MASSIF_GEOJSON = Path(__file__).parent.parent / "liste-massifs.geojson"

# Bulletins are issued once or twice a day; cache for 6h.
_session = requests_cache.CachedSession("avalanche_cache", expire_after=3600 * 6)

_DANGER_LABELS = {
    1: "Low",
    2: "Limited",
    3: "Considerable",
    4: "High",
    5: "Very High",
}


@dataclass
class AvalancheBulletin:
    source: str                        # "meteofrance"
    massif_name: str
    danger_level: int                  # 1–5 max danger for the day
    danger_level_lo: int | None        # danger below split altitude
    danger_level_hi: int | None        # danger above split altitude
    danger_split_altitude: int | None  # metres; None if uniform danger
    valid_until: str                   # ISO datetime (truncated to minute)
    aspects_at_risk: list[str]         # e.g. ["N", "NE", "E", "NW"]
    summary: str                       # RESUME CDATA (concise)
    full_text: str                     # STABILITE/TEXTE CDATA (detailed, truncated)
    llm_text: str                      # pre-formatted block for LLM injection
    ui_md: str                         # markdown for Streamlit display
    image_meteo: bytes | None = None   # apercu-meteo PNG bytes
    image_7days: bytes | None = None   # sept-derniers-jours PNG bytes
    fetch_error: str | None = None


# ---------------------------------------------------------------------------
# Point-in-polygon (ray-casting, no external dependencies)
# ---------------------------------------------------------------------------

def _ray_cast(lat: float, lon: float, ring: list) -> bool:
    """Return True if (lon, lat) is inside the polygon ring."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]   # GeoJSON: [lon, lat]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i
    return inside


def _point_in_polygon(lat: float, lon: float, rings: list) -> bool:
    """Polygon = outer ring + optional holes."""
    if not rings:
        return False
    if not _ray_cast(lat, lon, rings[0]):
        return False
    for hole in rings[1:]:
        if _ray_cast(lat, lon, hole):
            return False
    return True


def _point_in_multipolygon(lat: float, lon: float, geometry: dict) -> bool:
    gtype = geometry.get("type", "")
    coords = geometry.get("coordinates", [])
    if gtype == "Polygon":
        return _point_in_polygon(lat, lon, coords)
    if gtype == "MultiPolygon":
        return any(_point_in_polygon(lat, lon, poly) for poly in coords)
    return False


_massif_features: list | None = None


def _find_massif(lat: float, lon: float) -> dict | None:
    """Return the massif properties for the given point, or None if outside France."""
    global _massif_features
    if _massif_features is None:
        with open(_MASSIF_GEOJSON, encoding="utf-8") as f:
            _massif_features = json.load(f)["features"]
    for feature in _massif_features:
        if _point_in_multipolygon(lat, lon, feature["geometry"]):
            return feature["properties"]
    return None


# ---------------------------------------------------------------------------
# XML parsing helpers
# ---------------------------------------------------------------------------

def _text(elem) -> str:
    return (elem.text or "").strip() if elem is not None else ""


def _parse_aspects(pente_elem) -> list[str]:
    if pente_elem is None:
        return []
    keys = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return [k for k in keys if pente_elem.get(k, "").lower() == "true"]


# ---------------------------------------------------------------------------
# Text formatters
# ---------------------------------------------------------------------------

def _danger_str_short(b: "AvalancheBulletin") -> str:
    if b.danger_split_altitude and b.danger_level_lo and b.danger_level_hi:
        return (
            f"{b.danger_level_hi}/5 ({_DANGER_LABELS.get(b.danger_level_hi, '')}) "
            f"above {b.danger_split_altitude}m, "
            f"{b.danger_level_lo}/5 ({_DANGER_LABELS.get(b.danger_level_lo, '')}) below"
        )
    lbl = _DANGER_LABELS.get(b.danger_level, "")
    return f"{b.danger_level}/5 ({lbl})"


def _build_llm_text(b: "AvalancheBulletin") -> str:
    aspects = ", ".join(b.aspects_at_risk) if b.aspects_at_risk else "not specified"
    lines = [
        "## Avalanche bulletin (Météo-France)",
        f"Massif: {b.massif_name}  |  Danger: {_danger_str_short(b)}  |  Valid until: {b.valid_until}",
        f"Aspects at risk: {aspects}",
        "",
        f"Summary: {b.summary}",
    ]
    if b.full_text:
        lines += ["", f"Detailed conditions:\n{b.full_text[:1500]}"]
    return "\n".join(lines)


def _build_ui_md(b: "AvalancheBulletin") -> str:
    if b.danger_split_altitude and b.danger_level_lo and b.danger_level_hi:
        danger_md = (
            f"**{b.danger_level_hi}/5** above {b.danger_split_altitude}m, "
            f"**{b.danger_level_lo}/5** below"
        )
    else:
        lbl = _DANGER_LABELS.get(b.danger_level, "")
        danger_md = f"**{b.danger_level}/5 — {lbl}**"

    aspects = ", ".join(b.aspects_at_risk) if b.aspects_at_risk else "—"
    return (
        f"**Météo-France BRA — {b.massif_name}**  ·  Danger: {danger_md}  ·  "
        f"Valid until: {b.valid_until}\n\n"
        f"**Aspects at risk:** {aspects}\n\n"
        f"**Summary:** {b.summary}"
    )


# ---------------------------------------------------------------------------
# Météo-France BRA fetch
# ---------------------------------------------------------------------------

def _error_bulletin(name: str, msg: str) -> AvalancheBulletin:
    return AvalancheBulletin(
        source="meteofrance", massif_name=name,
        danger_level=0, danger_level_lo=None, danger_level_hi=None,
        danger_split_altitude=None, valid_until="", aspects_at_risk=[],
        summary="", full_text="", llm_text="", ui_md="",
        fetch_error=msg,
    )


def _fetch_bra_images(massif_code: int) -> tuple[bytes | None, bytes | None]:
    """Fetch apercu-meteo and sept-derniers-jours PNGs. Best-effort."""
    if not _MF_API_KEY:
        return None, None

    def _get(endpoint: str) -> bytes | None:
        try:
            r = _session.get(
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
        r = _session.get(
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

    # Flatten if no real split
    if not split_alt or danger_lo == danger_hi:
        danger_lo = danger_hi = split_alt = None

    aspects   = _parse_aspects(pente_elem)
    summary   = _text(resume_elem)
    full_text = _text(stabilite.find("TEXTE")) if stabilite is not None else ""

    img_meteo, img_7days = _fetch_bra_images(code)

    b = AvalancheBulletin(
        source="meteofrance",
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


def fetch_avalanche_bulletin(lat: float, lon: float) -> list[AvalancheBulletin]:
    """
    Return all applicable avalanche bulletins for the given coordinates.
    Phase 1: Météo-France BRA only (French massifs).
    Phase 2 will add EAWS feeds (Switzerland, Italy/Austria).
    """
    bulletins = []
    fr = fetch_bra_france(lat, lon)
    if fr is not None:
        bulletins.append(fr)
    return bulletins
