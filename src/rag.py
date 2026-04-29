"""RAG retrieval layer for local route databases (SummitPost, passion-alpes, SAC).

Public API:
    is_available() -> bool
    resolve_area(name) -> (lat_min, lat_max, lon_min, lon_max) | None
    search(query, n_results, section_heading, source,
           lat_min, lat_max, lon_min, lon_max) -> list[dict]
    get_route_sections(sp_id) -> dict          # SummitPost deep-dive
    get_passion_alpes_topo(topo_id) -> dict    # passion-alpes deep-dive
    get_sac_topo(route_id) -> dict             # SAC deep-dive
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

_CHROMA_PATH = Path(__file__).parent.parent / "data" / "chroma"
_DB_PATH = Path(__file__).parent.parent / "data" / "summitpost.db"
_PA_DB_PATH = Path(__file__).parent.parent / "data" / "passion_alpes.db"
_SAC_DB_PATH = Path(__file__).parent.parent / "data" / "sac.db"
_COLLECTION_NAME = "route_sections"
_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

_model = None
_collection = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _get_collection():
    global _collection
    if _collection is None:
        import chromadb
        client = chromadb.PersistentClient(path=str(_CHROMA_PATH))
        _collection = client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def resolve_area(name: str, radius_km: float = 200.0) -> tuple[float, float, float, float] | None:
    """Resolve a mountain range name to a bounding box via GMBA fuzzy search.

    Finds the best-matching GMBA polygon, then returns a bbox of radius_km around
    its centroid. Returns None if no match scores above 60.
    """
    from src.mountain_ranges import search_range
    from src.geo import bbox_around

    results = search_range(name, top_k=1)
    if not results or results[0]["score"] < 60:
        return None
    r = results[0]
    return bbox_around(r["centroid_lat"], r["centroid_lon"], radius_km)


def is_available() -> bool:
    """True if the ChromaDB index exists and contains documents."""
    if not _CHROMA_PATH.exists():
        return False
    try:
        return _get_collection().count() > 0
    except Exception:
        return False


def search(
    query: str,
    n_results: int = 5,
    section_heading: str | None = None,
    source: str | None = None,
    lat_min: float | None = None,
    lat_max: float | None = None,
    lon_min: float | None = None,
    lon_max: float | None = None,
) -> list[dict]:
    """Semantic search over embedded route sections.

    Returns up to n_results dicts, each containing the section text,
    similarity distance (lower = more similar), and all stored metadata
    (route_name, sp_id, section_heading, location, difficulty, etc.).

    lat_min/lat_max/lon_min/lon_max: if provided, restrict results to routes
    whose coordinates fall within that bounding box.
    """
    col = _get_collection()
    model = _get_model()

    embedding = model.encode(query).tolist()

    conditions: list[dict] = []
    if section_heading:
        conditions.append({"section_heading": {"$eq": section_heading}})
    if source:
        conditions.append({"source": {"$eq": source}})
    if lat_min is not None:
        conditions.append({"lat": {"$gte": lat_min}})
    if lat_max is not None:
        conditions.append({"lat": {"$lte": lat_max}})
    if lon_min is not None:
        conditions.append({"lon": {"$gte": lon_min}})
    if lon_max is not None:
        conditions.append({"lon": {"$lte": lon_max}})

    where: dict[str, Any] | None = None
    if len(conditions) == 1:
        where = conditions[0]
    elif len(conditions) > 1:
        where = {"$and": conditions}

    kwargs: dict[str, Any] = {
        "query_embeddings": [embedding],
        "n_results": min(n_results, max(col.count(), 1)),
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        kwargs["where"] = where

    results = col.query(**kwargs)

    output = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        output.append({"text": doc, "distance": round(float(dist), 4), **meta})
    return output


def get_passion_alpes_topo(topo_id: int) -> dict:
    """Return the full topo record from passion_alpes.db (deep-dive expansion).

    Returns a dict with topo metadata, full text, and images.
    Returns an empty dict if the topo is not found or the DB doesn't exist.
    """
    if not _PA_DB_PATH.exists():
        return {}
    conn = sqlite3.connect(_PA_DB_PATH)
    conn.row_factory = sqlite3.Row
    topo = conn.execute(
        "SELECT id, url, title, category, region, grade, departure, timing, full_text, scraped_at "
        "FROM topos WHERE id = ?",
        (topo_id,),
    ).fetchone()
    if not topo:
        conn.close()
        return {}
    images = conn.execute(
        "SELECT image_url, caption, is_diagram FROM topo_images WHERE topo_id = ?",
        (topo_id,),
    ).fetchall()
    conn.close()
    return {
        "topo_id": topo_id,
        "url": topo["url"],
        "title": topo["title"],
        "category": topo["category"],
        "region": topo["region"],
        "grade": topo["grade"],
        "departure": topo["departure"],
        "timing": topo["timing"],
        "full_text": topo["full_text"],
        "scraped_at": topo["scraped_at"],
        "images": [
            {"url": img["image_url"], "caption": img["caption"], "is_diagram": bool(img["is_diagram"])}
            for img in images
        ],
    }


def get_sac_topo(route_id: int) -> dict:
    """Return the full route record from sac.db (deep-dive expansion)."""
    if not _SAC_DB_PATH.exists():
        return {}
    conn = sqlite3.connect(_SAC_DB_PATH)
    conn.row_factory = sqlite3.Row
    topo = conn.execute(
        """SELECT id, summit_id, url, title, category, region, grade,
                  timing, altitude, latitude, longitude, full_text, scraped_at
           FROM topos WHERE id = ?""",
        (route_id,),
    ).fetchone()
    if not topo:
        conn.close()
        return {}
    images = conn.execute(
        "SELECT image_url, caption FROM topo_images WHERE topo_id = ?",
        (route_id,),
    ).fetchall()
    conn.close()
    return {
        "route_id": route_id,
        "url": topo["url"],
        "title": topo["title"],
        "region": topo["region"],
        "grade": topo["grade"],
        "timing": topo["timing"],
        "altitude": topo["altitude"],
        "latitude": topo["latitude"],
        "longitude": topo["longitude"],
        "full_text": topo["full_text"],
        "scraped_at": topo["scraped_at"],
        "images": [{"url": img["image_url"], "caption": img["caption"]} for img in images],
    }


def get_route_sections(sp_id: int) -> dict:
    """Return all sections for a route from SQLite (deep-dive expansion).

    Returns a dict with route metadata and a list of sections ordered by
    position. Returns an empty dict if the route is not found.
    """
    if not _DB_PATH.exists():
        return {}
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    route = conn.execute(
        "SELECT name, url, difficulty, location, lat, lon FROM routes WHERE sp_id = ?",
        (sp_id,),
    ).fetchone()
    if not route:
        conn.close()
        return {}
    sections = conn.execute(
        "SELECT heading, body, position FROM sections WHERE route_id = ? ORDER BY position",
        (sp_id,),
    ).fetchall()
    conn.close()
    return {
        "sp_id": sp_id,
        "name": route["name"],
        "url": route["url"],
        "difficulty": route["difficulty"],
        "location": route["location"],
        "lat": route["lat"],
        "lon": route["lon"],
        "sections": [
            {"heading": s["heading"], "body": s["body"], "position": s["position"]}
            for s in sections
        ],
    }
