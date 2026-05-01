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
_HIKR_DB_PATH = Path(__file__).parent.parent / "data" / "hikr.db"
_LEMKE_DB_PATH = Path(__file__).parent.parent / "data" / "lemkeclimbs.db"
_FOTH_DB_PATH = Path(__file__).parent.parent / "data" / "freedom_of_the_hills.db"
_FFCAM_DB_PATH = Path(__file__).parent.parent / "data" / "memento_ffcam.db"
_REFUGES_DB_PATH = Path(__file__).parent.parent / "data" / "refuges.db"
_COLLECTION_NAME = "cards"
_MODEL_NAME = "all-mpnet-base-v2"

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
    source: str | None = None,
    doc_type: str | None = None,
    language: str | None = None,
    min_trustworthiness: float | None = None,
    lat_min: float | None = None,
    lat_max: float | None = None,
    lon_min: float | None = None,
    lon_max: float | None = None,
) -> list[dict]:
    """Semantic search over embedded card summaries.

    Returns up to n_results dicts, each containing the embedded text,
    similarity distance (lower = more similar), and all stored metadata
    (source, pk, url, title, doc_type, language, grades, mountain_range, etc.).

    lat_min/lat_max/lon_min/lon_max: restrict to documents with coordinates
    in that bounding box (documents with no coordinates are excluded).
    """
    col = _get_collection()
    model = _get_model()

    embedding = model.encode(query).tolist()

    conditions: list[dict] = []
    if source:
        conditions.append({"source": {"$eq": source}})
    if doc_type:
        conditions.append({"doc_type": {"$contains": doc_type}})
    if language:
        conditions.append({"language": {"$eq": language}})
    if min_trustworthiness is not None:
        conditions.append({"trustworthiness": {"$gte": min_trustworthiness}})
    if lat_min is not None:
        conditions.append({"lat": {"$gte": lat_min}})
        conditions.append({"lat": {"$ne": 0.0}})
    if lat_max is not None:
        conditions.append({"lat": {"$lte": lat_max}})
    if lon_min is not None:
        conditions.append({"lon": {"$gte": lon_min}})
        conditions.append({"lon": {"$ne": 0.0}})
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
    with sqlite3.connect(_PA_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        topo = conn.execute(
            "SELECT id, url, title, category, region, grade, departure, timing, full_text, scraped_at "
            "FROM topos WHERE id = ?",
            (topo_id,),
        ).fetchone()
        if not topo:
            return {}
        images = conn.execute(
            "SELECT image_url, caption, is_diagram FROM topo_images WHERE topo_id = ?",
            (topo_id,),
        ).fetchall()
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
    with sqlite3.connect(_SAC_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        topo = conn.execute(
            """SELECT id, summit_id, url, title, category, region, grade,
                      timing, altitude, latitude, longitude, full_text, scraped_at
               FROM topos WHERE id = ?""",
            (route_id,),
        ).fetchone()
        if not topo:
            return {}
        images = conn.execute(
            "SELECT image_url, caption FROM topo_images WHERE topo_id = ?",
            (route_id,),
        ).fetchall()
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
    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        route = conn.execute(
            "SELECT name, url, difficulty, location, lat, lon FROM routes WHERE sp_id = ?",
            (sp_id,),
        ).fetchone()
        if not route:
            return {}
        sections = conn.execute(
            "SELECT heading, body, position FROM sections WHERE route_id = ? ORDER BY position",
            (sp_id,),
        ).fetchall()
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


def get_hikr_report(report_id: int) -> dict:
    """Return a hikr trip report by id."""
    if not _HIKR_DB_PATH.exists():
        return {}
    with sqlite3.connect(_HIKR_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, url, title, date_of_hike, region, author, language, full_text, scraped_at "
            "FROM reports WHERE id = ?",
            (report_id,),
        ).fetchone()
    if not row:
        return {}
    return {
        "report_id": report_id,
        "url": row["url"],
        "title": row["title"],
        "date_of_hike": row["date_of_hike"],
        "region": row["region"],
        "author": row["author"],
        "language": row["language"],
        "full_text": row["full_text"],
        "scraped_at": row["scraped_at"],
    }


def get_lemkeclimbs_topo(topo_id: int) -> dict:
    """Return a lemkeclimbs topo by id."""
    if not _LEMKE_DB_PATH.exists():
        return {}
    with sqlite3.connect(_LEMKE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, url, title, area, region, grade, elevation, language, full_text, scraped_at "
            "FROM topos WHERE id = ?",
            (topo_id,),
        ).fetchone()
    if not row:
        return {}
    return {
        "topo_id": topo_id,
        "url": row["url"],
        "title": row["title"],
        "area": row["area"],
        "region": row["region"],
        "grade": row["grade"],
        "elevation": row["elevation"],
        "language": row["language"],
        "full_text": row["full_text"],
        "scraped_at": row["scraped_at"],
    }


def get_freedom_section(section_id: int) -> dict:
    """Return a Freedom of the Hills section by id."""
    if not _FOTH_DB_PATH.exists():
        return {}
    with sqlite3.connect(_FOTH_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, part, chapter, section, text, char_count FROM sections WHERE id = ?",
            (section_id,),
        ).fetchone()
    if not row:
        return {}
    return {
        "section_id": section_id,
        "part": row["part"],
        "chapter": row["chapter"],
        "section": row["section"],
        "text": row["text"],
        "char_count": row["char_count"],
    }


def get_memento_section(section_id: int) -> dict:
    """Return a Mémento FFCAM section by id."""
    if not _FFCAM_DB_PATH.exists():
        return {}
    with sqlite3.connect(_FFCAM_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, major_section, chapter, section, text, char_count FROM sections WHERE id = ?",
            (section_id,),
        ).fetchone()
    if not row:
        return {}
    return {
        "section_id": section_id,
        "major_section": row["major_section"],
        "chapter": row["chapter"],
        "section": row["section"],
        "text": row["text"],
        "char_count": row["char_count"],
    }


def get_refuge(refuge_id: int) -> dict:
    """Return a hut record from refuges.db by id."""
    if not _REFUGES_DB_PATH.exists():
        return {}
    with sqlite3.connect(_REFUGES_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT id, name, type, lat, lon, altitude_m, capacity, status, url,
                      opening_dates, contact, phone, phone_custodian, website_url,
                      price_eur, meteoblue_url, access_desc, description
               FROM huts WHERE id = ?""",
            (refuge_id,),
        ).fetchone()
    if not row:
        return {}
    return {k: row[k] for k in row.keys()}
