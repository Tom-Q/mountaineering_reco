"""Index passion-alpes.com topos into the shared ChromaDB RAG collection.

Reads from data/passion_alpes.db, embeds each topo's full_text, and upserts
into the same ChromaDB collection used by SummitPost (route_sections).

Idempotent: already-indexed document IDs are skipped.

Usage:
    python scrapers/build_passion_alpes_index.py
    python scrapers/build_passion_alpes_index.py --db data/passion_alpes.db --limit 10
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from geocode_utils import resolve_coordinates

_CHROMA_PATH = Path("data/chroma")
_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
_COLLECTION_NAME = "route_sections"
_CHUNK_SIZE = 1000
_CHUNK_OVERLAP = 100
_MAX_SINGLE_CHUNK = 10_000


def _chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks if it exceeds MAX_SINGLE_CHUNK chars."""
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
        "SELECT id, url, title, category, region, grade, departure, full_text, scraped_at FROM topos"
    ).fetchall()
    conn.close()

    if limit:
        topos = topos[:limit]

    print(f"Topos in DB: {len(topos)}")
    topos = [t for t in topos if t["full_text"] and t["full_text"].strip()]
    print(f"Topos with text: {len(topos)}")

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
        topo_id = topo["id"]
        chunks = _chunk_text(topo["full_text"])

        lat, lon, geo_precision = resolve_coordinates(
            topo["title"], topo["departure"], topo["region"]
        )

        for chunk_idx, chunk in enumerate(chunks):
            doc_id = f"passion_alpes_{topo_id}_0_{chunk_idx}"
            if doc_id in existing_ids:
                skipped += 1
                continue

            embedding = model.encode(chunk).tolist()
            metadata: dict = {
                "source": "passion_alpes",
                "topo_id": topo_id,
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
            col.upsert(ids=[doc_id], embeddings=[embedding], documents=[chunk], metadatas=[metadata])
            added += 1

        name = (topo["title"] or topo["url"])[:60]
        geo_note = f"({lat:.3f}, {lon:.3f})" if lat else "no coords"
        print(f"  [{topo_id}] {name} — {geo_note}")

    print(f"\nDone. Added/updated: {added}  Skipped (already indexed): {skipped}")
    print(f"Collection total: {col.count()} documents")


def main() -> None:
    parser = argparse.ArgumentParser(description="Index passion-alpes topos into ChromaDB")
    parser.add_argument("--db", default="data/passion_alpes.db")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max topos to index in this run (default: all)")
    parser.add_argument("--force", action="store_true",
                        help="Re-index already-indexed documents (updates metadata)")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"DB not found: {args.db}")
        print("Run scrapers/passion_alpes_scrape.py first.")
        sys.exit(1)

    build_index(args.db, args.limit, force=args.force)


if __name__ == "__main__":
    main()
