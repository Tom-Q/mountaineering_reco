"""
Point-in-polygon geometry (ray-casting, no external dependencies).

Used by src/geo.py (massif classification) and src/avalanche.py (bulletin lookup).
GeoJSON coordinate order is [lon, lat] — functions accept (lat, lon) arguments.
"""


def _ray_cast(lat: float, lon: float, ring: list) -> bool:
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


def point_in_polygon(lat: float, lon: float, rings: list) -> bool:
    """Return True if (lat, lon) is inside the GeoJSON polygon defined by rings."""
    if not rings:
        return False
    if not _ray_cast(lat, lon, rings[0]):
        return False
    for hole in rings[1:]:
        if _ray_cast(lat, lon, hole):
            return False
    return True


def point_in_geometry(lat: float, lon: float, geometry: dict) -> bool:
    """Return True if (lat, lon) is inside a GeoJSON Polygon or MultiPolygon geometry."""
    gtype = geometry.get("type", "")
    coords = geometry.get("coordinates", [])
    if gtype == "Polygon":
        return point_in_polygon(lat, lon, coords)
    if gtype == "MultiPolygon":
        return any(point_in_polygon(lat, lon, poly) for poly in coords)
    return False
