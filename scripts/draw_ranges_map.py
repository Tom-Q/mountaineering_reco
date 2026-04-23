"""Draw mountain range bounding boxes on the world map image.

Output: ranges_map.png in the project root.

Usage:
    python scripts/draw_ranges_map.py
"""

import math
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = Path(__file__).parent
IMG_PATH = SCRIPTS_DIR / "draw_ranges_map_base.jpg"
YAML_PATH = ROOT / "domain_knowledge" / "ranges.yaml"
OUT_PATH = SCRIPTS_DIR / "draw_ranges_map.jpg"

# Map bounds
LON_MIN, LON_MAX = -180, 180
LAT_MIN, LAT_MAX = -82, 82

# Colors per continent section (RGBA)
COLORS = [
    (220,  50,  50, 80),   # red      — Europe
    (200, 140,  40, 80),   # orange   — Africa
    ( 50, 160,  80, 80),   # green    — Asia
    ( 50, 120, 220, 80),   # blue     — North America
    (160,  60, 200, 80),   # purple   — South America
    ( 40, 180, 180, 80),   # teal     — Oceania/Antarctica
]

BORDER_ALPHA = 200


def mercator_y(lat_deg: float) -> float:
    lat_rad = math.radians(max(min(lat_deg, 84.9), -84.9))
    return math.log(math.tan(math.pi / 4 + lat_rad / 2))


def to_pixel(lat: float, lon: float, w: int, h: int):
    x = (lon - LON_MIN) / (LON_MAX - LON_MIN) * w
    y_min = mercator_y(LAT_MIN)
    y_max = mercator_y(LAT_MAX)
    y = (y_max - mercator_y(lat)) / (y_max - y_min) * h
    return x, y


def continent_index(rng: dict) -> int:
    """Color by geographic center of bbox."""
    b = rng["bbox"]
    lat = (b["lat_min"] + b["lat_max"]) / 2
    lon = (b["lon_min"] + b["lon_max"]) / 2
    if lat < -20:                          return 5  # teal   — Antarctica / Southern
    if -82 < lon < 35 and lat > 20:        return 0  # red    — Europe
    if -20 < lon < 55 and lat < 40:        return 1  # orange — Africa
    if lon > 35:                           return 2  # green  — Asia
    if lon < -30 and lat > 10:             return 3  # blue   — North America
    if lon < -30 and lat <= 10:            return 4  # purple — South America
    return 0


def main():
    base = Image.open(IMG_PATH).convert("RGBA")
    w, h = base.size
    print(f"Image: {w}x{h}")

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    data = yaml.safe_load(YAML_PATH.read_text())
    ranges = data["ranges"]
    n = len(ranges)

    # Try to load a small system font; fall back to default
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 11)
        font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 9)
    except OSError:
        font = ImageFont.load_default()
        font_sm = font

    for idx, rng in enumerate(ranges):
        bbox = rng["bbox"]
        name = rng["name"]

        x0, y1 = to_pixel(bbox["lat_min"], bbox["lon_min"], w, h)
        x1, y0 = to_pixel(bbox["lat_max"], bbox["lon_max"], w, h)

        color = COLORS[continent_index(rng)]
        border = color[:3] + (BORDER_ALPHA,)

        # Fill
        draw.rectangle([x0, y0, x1, y1], fill=color)
        # Border
        draw.rectangle([x0, y0, x1, y1], outline=border, width=1)

        # Label: only if box is wide enough
        box_w = x1 - x0
        box_h = y1 - y0
        f = font if box_w > 60 else font_sm
        try:
            tw = draw.textlength(name, font=f)
        except AttributeError:
            tw = len(name) * 6  # fallback estimate
        th = 11

        if tw < box_w - 4 and box_h > th + 2:
            tx = x0 + (box_w - tw) / 2
            ty = y0 + (box_h - th) / 2
            # Dark shadow for legibility
            draw.text((tx + 1, ty + 1), name, font=f, fill=(0, 0, 0, 180))
            draw.text((tx, ty), name, font=f, fill=(255, 255, 255, 240))

    composite = Image.alpha_composite(base, overlay).convert("RGB")
    composite.save(OUT_PATH, quality=88, optimize=True)
    print(f"Saved: {OUT_PATH}")


if __name__ == "__main__":
    main()
