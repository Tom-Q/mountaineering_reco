"""SummitPost RAG retrieval layer.

Public API:
    is_available() -> bool
    resolve_area(name) -> (lat_min, lat_max, lon_min, lon_max) | None
    search(query, n_results, section_heading, source,
           lat_min, lat_max, lon_min, lon_max) -> list[dict]
    get_route_sections(sp_id) -> dict
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

_CHROMA_PATH = Path(__file__).parent.parent / "data" / "chroma"
_DB_PATH = Path(__file__).parent.parent / "data" / "summitpost.db"
_RANGES_YAML = Path(__file__).parent.parent / "domain_knowledge" / "ranges.yaml"
_COLLECTION_NAME = "route_sections"
_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

_model = None
_collection = None
_ranges_cache: list | None = None


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


def _load_ranges() -> list:
    global _ranges_cache
    if _ranges_cache is None:
        import yaml
        data = yaml.safe_load(_RANGES_YAML.read_text())
        _ranges_cache = data.get("ranges", [])
    return _ranges_cache


def resolve_area(name: str) -> tuple[float, float, float, float] | None:
    """Match a range name or alias against ranges.yaml.

    Returns (lat_min, lat_max, lon_min, lon_max) for the first match, or None.
    Tries exact match first, then substring fallback.
    """
    needle = name.strip().lower()
    ranges = _load_ranges()

    for rng in ranges:
        candidates = [rng["name"].lower()] + [a.lower() for a in rng.get("aliases", [])]
        if needle in candidates:
            b = rng["bbox"]
            return b["lat_min"], b["lat_max"], b["lon_min"], b["lon_max"]

    # Substring fallback: needle contained in a candidate, or vice-versa
    for rng in ranges:
        candidates = [rng["name"].lower()] + [a.lower() for a in rng.get("aliases", [])]
        if any(needle in c or c in needle for c in candidates):
            b = rng["bbox"]
            return b["lat_min"], b["lat_max"], b["lon_min"], b["lon_max"]

    return None


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
