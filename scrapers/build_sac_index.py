"""Index SAC alpine tour routes into the shared ChromaDB RAG collection.

Reads from data/sac.db. Coordinates come directly from the DB (exact, from API)
so geocode_utils is not needed.

Idempotent: already-indexed document IDs are skipped.

Usage:
    python scrapers/build_sac_index.py
    python scrapers/build_sac_index.py --db data/sac.db --limit 10
    python scrapers/build_sac_index.py --force
"""

import argparse
import sqlite3
import sys
from pathlib import Path

_CHROMA_PATH = Path("data/chroma")
_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_COLLECTION_NAME = "route_sections"
_CHUNK_SIZE = 1000
_CHUNK_OVERLAP = 100
_MAX_SINGLE_CHUNK = 10_000


def _chunk_text(text: str) -> list[str]:
    if len(text) <= _MAX_SINGLE_CHUNK:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + _CHUNK_SIZE
        chunks.append(text[start:end])
        start += _CHUNK_SIZE - _CHUNK_OVERLAP
    return chunks


def build_index(db_path: str, limit: int | None, force: bool = False) -> None:
    import chromadb
    from sentence_transformers import SentenceTransformer

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    topos = conn.execute(
        """SELECT id, summit_id, url, title, category, region, grade,
                  timing, altitude, latitude, longitude, full_text, scraped_at
           FROM topos"""
    ).fetchall()
    conn.close()

    if limit:
        topos = topos[:limit]

    print(f"Routes in DB: {len(topos)}")
    topos = [t for t in topos if t["full_text"] and t["full_text"].strip()]
    print(f"Routes with text: {len(topos)}")

    print(f"Loading embedding model '{_MODEL_NAME}'...")
    model = SentenceTransformer(_MODEL_NAME)

    client = chromadb.PersistentClient(path=str(_CHROMA_PATH))
    col = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    existing_ids = set(col.get(include=[])["ids"]) if not force else set()
    print(f"Documents already in collection: {len(existing_ids)}"
          + (" (--force: will re-index all)" if force else ""))

    added = skipped = 0
    for topo in topos:
        route_id = topo["id"]
        chunks = _chunk_text(topo["full_text"])

        lat = topo["latitude"]
        lon = topo["longitude"]

        for chunk_idx, chunk in enumerate(chunks):
            doc_id = f"sac_{route_id}_0_{chunk_idx}"
            if doc_id in existing_ids:
                skipped += 1
                continue

            embedding = model.encode(chunk).tolist()
            metadata: dict = {
                "source": "sac",
                "topo_id": route_id,
                "route_name": topo["title"] or "",
                "url": topo["url"] or "",
                "category": topo["category"] or "",
                "region": topo["region"] or "",
                "grade": topo["grade"] or "",
                "section_heading": "Topo",
                "section_position": 0,
                "chunk_index": chunk_idx,
                "scraped_at": topo["scraped_at"] or "",
            }
            if lat is not None:
                metadata["lat"] = lat
                metadata["lon"] = lon

            col.upsert(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[chunk],
                metadatas=[metadata],
            )
            added += 1

        geo_note = f"({lat:.3f}, {lon:.3f})" if lat else "no coords"
        name = (topo["title"] or str(route_id))[:60]
        print(f"  [{route_id}] {name} — {topo['grade']} {geo_note}")

    print(f"\nDone. Added/updated: {added}  Skipped (already indexed): {skipped}")
    print(f"Collection total: {col.count()} documents")


def main() -> None:
    parser = argparse.ArgumentParser(description="Index SAC routes into ChromaDB")
    parser.add_argument("--db", default="data/sac.db")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true",
                        help="Re-index already-indexed documents")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"DB not found: {args.db}")
        print("Run scrapers/sac_scrape.py first.")
        sys.exit(1)

    build_index(args.db, args.limit, force=args.force)


if __name__ == "__main__":
    main()
