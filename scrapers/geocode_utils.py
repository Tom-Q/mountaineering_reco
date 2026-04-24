"""Shared geocoding utilities for scrapers.

Provides a fallback chain for assigning lat/lon + precision to scraped route
records that don't have exact coordinates:

    1. Summit geocoding  — extract summit name from title, geocode via Nominatim.
    2. Departure geocoding — geocode the departure point field.
    3. Region centroid   — hardcoded approximate centroid for the region label.

Returns (lat, lon, precision) where precision is one of:
    "summit"    — geocoded from summit/route name
    "departure" — geocoded from departure point
    "region"    — regional centroid (least precise, fallback)

The caller is responsible for storing geo_precision alongside lat/lon in the
index so the chatbot can reason about coordinate reliability.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.geo import geocode_location

# ---------------------------------------------------------------------------
# Region centroids — fallback of last resort.
# Coordinates are intentionally approximate; precision = "region".
# For Mont-Blanc, the centroid is placed on the French summit (Bosses ridge)
# so it falls inside the Météo-France Mont-Blanc massif polygon, which matters
# for avalanche bulletin routing.
# ---------------------------------------------------------------------------

REGION_CENTROIDS: dict[str, tuple[float, float]] = {
    "Mont-Blanc":                                        (45.8326,  6.8652),
    "Bassin d'Argentière (Mont-Blanc)":                  (45.980,   6.930),
    "Secteur Aiguilles de Chamonix (Mont-Blanc)":        (45.878,   6.883),
    "Secteur Aiguille du midi (Mont-Blanc)":             (45.879,   6.888),
    "Secteur Combe Maudite – Glacier du Géant (Mont-Blanc)": (45.860, 6.950),
    "Chamonix":                                          (45.924,   6.869),
    "Aiguilles Rouges":                                  (45.960,   6.830),
    "Secteur Aiguilles Rouges":                          (45.960,   6.830),
    "Valais":                                            (46.100,   7.550),
    "Valpelline":                                        (45.820,   7.420),
    "Oisans":                                            (44.990,   6.350),
    "Écrins":                                            (44.920,   6.370),
    "Secteur Glacier Blanc (Massif des Écrins)":         (44.920,   6.370),
    "Secteur Valgaudemar":                               (44.800,   6.200),
    "Belledonne":                                        (45.220,   5.970),
    "Bornes – Aravis":                                   (45.880,   6.380),
    "Secteur Bornes – Aravis":                           (45.880,   6.380),
    "Aravis":                                            (45.880,   6.400),
    "Beaufortain ( secteur Val Montjoie)":               (45.740,   6.700),
    "Vercors":                                           (44.870,   5.520),
    "Secteur Vercors":                                   (44.870,   5.520),
    "Dévoluy":                                           (44.720,   5.840),
    "Taillefer":                                         (45.050,   5.900),
    "Dolomites":                                         (46.410,  11.840),
    "Écosse":                                            (56.800,  -5.010),  # Ben Nevis area
}

# OSM classes/types that are plausibly a mountain summit or named peak.
_MOUNTAIN_CLASSES = frozenset({"natural"})
_MOUNTAIN_TYPES = frozenset({"peak", "volcano", "ridge", "cliff", "saddle", "mountain_pass"})
_MIN_IMPORTANCE = 0.25  # below this even a "natural/peak" result is too obscure to trust


def _looks_like_summit(geo: dict) -> bool:
    """Return True if the geocoding result plausibly represents a mountain feature."""
    if geo["osm_class"] in _MOUNTAIN_CLASSES:
        imp = geo.get("importance") or 0
        return float(imp) >= _MIN_IMPORTANCE
    return False


# ---------------------------------------------------------------------------
# Title parsing
# ---------------------------------------------------------------------------

def extract_summit_name(title: str) -> str | None:
    """Extract the summit/peak name from a passion-alpes topo title.

    Handles titles like:
      "Topo – Aiguille du Peigne (3192m) : Voie normale, AD/III/600m"
        → "Aiguille du Peigne"
      "Topo – Aiguille de la Gliere, contreforts (2600m) : ..."
        → "Aiguille de la Gliere"
      "Débuter et progresser en alpinisme hivernal mixte..." (no pattern)
        → None
    """
    # Must start with "Topo –" (or "Topo -") to be a route entry
    m = re.match(r"^Topo\s*[–\-]\s*(.+?)(?:\s*:)", title)
    if not m:
        return None
    raw = m.group(1).strip()
    # Strip elevation like "(3192m)" or "(3192 m)"
    raw = re.sub(r"\(\d[\d\s]*m\)", "", raw).strip()
    # Cut at first comma — "Aiguille de la Gliere, contreforts" → "Aiguille de la Gliere"
    raw = raw.split(",")[0].strip()
    return raw if raw else None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def resolve_coordinates(
    title: str,
    departure: str | None,
    region: str | None,
) -> tuple[float | None, float | None, str | None]:
    """Return (lat, lon, precision) using the best available source.

    Tries in order:
      1. Geocode the summit name extracted from title
      2. Geocode the departure point
      3. Look up a region centroid

    Returns (None, None, None) if nothing works.
    """
    # 1. Summit name from title
    summit = extract_summit_name(title or "")
    if summit:
        geo = geocode_location(summit)
        if geo and _looks_like_summit(geo):
            return geo["lat"], geo["lon"], "summit"

    # 2. Departure point
    if departure and departure.strip():
        geo = geocode_location(departure.strip())
        if geo:
            return geo["lat"], geo["lon"], "departure"

    # 3. Region centroid
    if region and region in REGION_CENTROIDS:
        lat, lon = REGION_CENTROIDS[region]
        return lat, lon, "region"

    return None, None, None
