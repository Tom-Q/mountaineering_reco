#!/usr/bin/env python3
"""
Build the ChromaDB card index from all source DBs.

Wipes the existing 'cards' collection and rebuilds from scratch.
Embeds: title + mountain_range + grades + summary for each document.
Safe to re-run.

Usage:
    python scripts/build_index.py
"""

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
CHROMA_PATH = DATA_DIR / "chroma"
COLLECTION_NAME = "cards"
MODEL_NAME = "all-mpnet-base-v2"
BATCH_SIZE = 500

SOURCES = [
    {
        "name": "hikr",
        "db": "hikr.db",
        "table": "reports",
        "pk": "id",
        "title_col": "title",
        "url_col": "url",
        "lat_col": None,
        "lon_col": None,
    },
    {
        "name": "summitpost",
        "db": "summitpost.db",
        "table": "routes",
        "pk": "sp_id",
        "title_col": "name",
        "url_col": "url",
        "lat_col": "lat",
        "lon_col": "lon",
    },
    {
        "name": "sac",
        "db": "sac.db",
        "table": "topos",
        "pk": "id",
        "title_col": "title",
        "url_col": "url",
        "lat_col": "latitude",
        "lon_col": "longitude",
    },
    {
        "name": "passion_alpes",
        "db": "passion_alpes.db",
        "table": "topos",
        "pk": "id",
        "title_col": "title",
        "url_col": "url",
        "lat_col": None,
        "lon_col": None,
    },
    {
        "name": "lemkeclimbs",
        "db": "lemkeclimbs.db",
        "table": "topos",
        "pk": "id",
        "title_col": "title",
        "url_col": "url",
        "lat_col": None,
        "lon_col": None,
    },
    {
        "name": "freedom_of_hills",
        "db": "freedom_of_the_hills.db",
        "table": "sections",
        "pk": "id",
        "title_col": None,
        "title_parts": ["part", "chapter", "section"],
        "url_col": None,
        "lat_col": None,
        "lon_col": None,
    },
    {
        "name": "memento_ffcam",
        "db": "memento_ffcam.db",
        "table": "sections",
        "pk": "id",
        "title_col": None,
        "title_parts": ["major_section", "chapter", "section"],
        "url_col": None,
        "lat_col": None,
        "lon_col": None,
    },
    {
        "name": "refuges",
        "db": "refuges.db",
        "table": "huts",
        "pk": "id",
        "title_col": "name",
        "url_col": "url",
        "lat_col": "lat",
        "lon_col": "lon",
        "where_clause": "(summary IS NOT NULL AND summary != '') OR type IN ('refuge gardé', 'gîte d''étape')",
        "fallback_cols": ["type", "altitude_m"],
    },
]


def _grades_text(grades_json: str | None) -> str:
    if not grades_json:
        return ""
    try:
        grades = json.loads(grades_json)
        return ", ".join(f"{k}: {v}" for k, v in grades.items() if v)
    except (json.JSONDecodeError, AttributeError):
        return ""


def _embed_text(title: str, mountain_range: str | None, grades_json: str | None, summary: str) -> str:
    parts = [title]
    if mountain_range:
        parts.append(mountain_range)
    gt = _grades_text(grades_json)
    if gt:
        parts.append(gt)
    parts.append(summary)
    return ". ".join(parts)


def _build_title(row: sqlite3.Row, src: dict) -> str:
    if src["title_col"]:
        return row[src["title_col"]] or ""
    parts = [row[col] for col in src.get("title_parts", []) if row[col]]
    return " > ".join(parts)


def index_source(src: dict, collection, model) -> tuple[int, int]:
    db_path = DATA_DIR / src["db"]
    if not db_path.exists():
        print(f"  {src['name']}: DB not found, skipping")
        return 0, 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    extra_cols = ""
    if src["title_col"] is None:
        parts = src.get("title_parts", [])
        if parts:
            extra_cols = ", " + ", ".join(parts)
    else:
        extra_cols = f", {src['title_col']}"

    fallback_cols = src.get("fallback_cols", [])
    if fallback_cols:
        extra_cols += ", " + ", ".join(fallback_cols)

    lat_col = src["lat_col"] or "NULL"
    lon_col = src["lon_col"] or "NULL"
    url_col = src["url_col"] or "NULL"

    if "where_clause" in src:
        where_clause = f"WHERE {src['where_clause']}"
    else:
        where_clause = "WHERE summary IS NOT NULL AND summary != ''"

    rows = conn.execute(f"""
        SELECT {src['pk']}, {url_col} AS url, {lat_col} AS lat, {lon_col} AS lon,
               doc_type, trustworthiness, mountain_range, grades, language,
               summary, date{extra_cols}
        FROM {src['table']}
        {where_clause}
    """).fetchall()

    conn.close()

    if not rows:
        print(f"  {src['name']}: 0 cards found")
        return 0, 0

    print(f"  {src['name']}: {len(rows)} cards to index…", end="", flush=True)

    ids, documents, metadatas = [], [], []

    for row in rows:
        pk = row[src["pk"]]
        title = _build_title(row, src)
        summary = row["summary"] or ""
        mountain_range = row["mountain_range"] or ""

        if not summary and fallback_cols:
            fallback_parts = [str(row[c]) for c in fallback_cols if row[c] is not None]
            summary = ", ".join(fallback_parts)

        text = _embed_text(title, mountain_range, row["grades"], summary)

        try:
            lat = float(row["lat"]) if row["lat"] is not None else 0.0
            lon = float(row["lon"]) if row["lon"] is not None else 0.0
        except (TypeError, ValueError):
            lat, lon = 0.0, 0.0

        try:
            trust = float(row["trustworthiness"]) if row["trustworthiness"] else 0.5
        except (TypeError, ValueError):
            trust = 0.5

        meta = {
            "source":         src["name"],
            "pk":             int(pk),
            "url":            row["url"] or "",
            "title":          title,
            "doc_type":       json.loads(row["doc_type"]) if row["doc_type"] else ["other"],
            "language":       row["language"] or "",
            "mountain_range": mountain_range,
            "grades":         row["grades"] or "{}",
            "trustworthiness": trust,
            "date":           row["date"] or "",
            "lat":            lat,
            "lon":            lon,
        }

        ids.append(f"{src['name']}--{pk}")
        documents.append(text)
        metadatas.append(meta)

    # Embed and upsert in batches
    for i in range(0, len(ids), BATCH_SIZE):
        batch_ids = ids[i:i + BATCH_SIZE]
        batch_docs = documents[i:i + BATCH_SIZE]
        batch_metas = metadatas[i:i + BATCH_SIZE]
        embeddings = model.encode(batch_docs, show_progress_bar=False).tolist()
        collection.upsert(ids=batch_ids, embeddings=embeddings,
                          documents=batch_docs, metadatas=batch_metas)
        print(".", end="", flush=True)

    print(f" done ({len(ids)})")
    return len(ids), 0


def main() -> None:
    from sentence_transformers import SentenceTransformer
    import chromadb

    print(f"Loading model {MODEL_NAME}…")
    model = SentenceTransformer(MODEL_NAME)

    print(f"Opening ChromaDB at {CHROMA_PATH}…")
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))

    # Wipe existing collection if present
    existing = [c.name for c in client.list_collections()]
    if COLLECTION_NAME in existing:
        print(f"Deleting existing '{COLLECTION_NAME}' collection…")
        client.delete_collection(COLLECTION_NAME)

    collection = client.create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    print("\nIndexing sources…")
    total = 0
    for src in SOURCES:
        indexed, _ = index_source(src, collection, model)
        total += indexed

    print(f"\nDone. {total} documents indexed into '{COLLECTION_NAME}'.")
    print(f"Collection count: {collection.count()}")


if __name__ == "__main__":
    main()
