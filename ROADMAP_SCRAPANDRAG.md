# SummitPost Data Pipeline — Scraping, RAG & Retrieval

## Why this project

SummitPost complements Camptocamp: for popular European routes, Camptocamp is excellent and has fresher conditions data. SummitPost adds value for North American routes, English-language descriptions, structured gear/approach info, and routes absent from Camptocamp. Additionally, having multiple sources increases the chances of covering all the aspects of one route. The two datasets work together, not as replacements.

~2300 mountaineering routes worldwide (the mountaineering filter captures mixed/ice/alpine sub-types). Trip reports are stale — route info only.

---

## Phases

### Phase 1 — Scraping ✅ Done
Fetched and parsed all SummitPost mountaineering route pages. 2,313 routes stored in `data/summitpost.db` (structured metadata + section text + cover image URL). Parser handles both modern `<p>`-based pages and older table/orphaned-`<li>` layouts.

### Phase 2 — RAG indexing 🔄 Next

**Embedding model:** `sentence-transformers` (`paraphrase-multilingual-MiniLM-L12-v2`) — free, runs locally, no API key, 50+ languages including FR/DE/IT/ES, cross-lingual alignment (a French query retrieves German and Italian documents correctly). Essential given multilingual corpus and multilingual users. ~420MB, 384-dimensional vectors. Upgrade path: `intfloat/multilingual-e5-large` (2.2GB, better cross-lingual quality) if retrieval quality is insufficient.

**Chunking strategy:**
- Sections ≤10,000 chars (10,805 of 10,877): embed as one chunk per section
- Sections >10,000 chars (72 outliers — mega-route compilations): split into ~1,000-char chunks with 100-char overlap, each chunk tagged with its parent section

**ChromaDB collection:** single collection `route_sections`. Each document:
```
id:        "{source}_{route_id}_{section_position}_{chunk_index}"
document:  section body text (or chunk)
metadata: {
    source:          "summitpost",
    route_id:        155970,
    route_name:      "Arête des Cosmiques",
    section_heading: "Route Description",
    lat:             45.87,
    lon:             6.89,
    difficulty:      "D",
    location:        "Mont Blanc, France, Europe",
    scraped_at:      "2026-04-17"
}
```

**Index build script:** `scripts/build_rag.py` (private repo — reads from `summitpost.db`). Idempotent: skips already-indexed documents by ID. Estimated time: ~15 min for 10,877 sections on CPU.

**Output:** `data/chroma/` — gitignored, regenerated from SQLite as needed.

### Phase 3 — Retrieval integration

`src/rag.py` (public repo — no scraping, just ChromaDB queries):

**Retrieve → select → optionally expand:**
1. Embed the query with the same model
2. Vector search → top-k most relevant section chunks, with optional metadata filters (lat/lon radius, difficulty range, source, section type)
3. **Default (breadth queries):** return only the matched sections as context — do not expand to the full route. Keeps LLM context and cost bounded.
4. **Deep-dive (chat tab, explicit route question):** expand to all sections for that one route from SQLite by `route_id`. Acceptable cost for a single focused route.

The agent decides which mode based on query type. "Routes with ice climbing in Switzerland" → breadth, matched sections only. "Tell me everything about the Cosmiques Arête" → deep-dive, full record.

**Key filters available at query time:**
- Geographic: lat/lon bounding box or radius
- Grade: `difficulty` string match or range (requires normalisation)
- Source: `source == "summitpost"` etc.
- Section type: `section_heading IN ("Route Description", "Approach", ...)`

**Integration point:** the chat tool in `src/chat.py`. When the user asks about a route, the agent calls a `search_route_beta` tool that invokes `src/rag.py` and returns relevant sections as additional context alongside Camptocamp data.

---

## Tech stack decisions

| Component | Choice | Rationale |
|---|---|---|
| Cloudflare bypass | `cloudscraper` | Emulates browser fingerprint without launching a real browser; already used in adjacent project |
| HTML parsing | `BeautifulSoup4` + `lxml` | Stable CSS classes on SummitPost pages; lxml is fastest BS4 backend |
| Structured metadata | SQLite (`data/summitpost.db`) | ~2300 routes is trivial for SQLite; no server needed |
| Vector store | **ChromaDB** | More mature than sqlite-vec, better Python tooling, local/no-server |
| Embeddings | `paraphrase-multilingual-MiniLM-L12-v2` | Free, local, no API key, 50+ languages, cross-lingual alignment (FR query → DE/IT results) |

---

## Key design decisions

### No automatic route linking across sources

Routes to the same summit share coordinates and similar names but can be completely different climbs with different grades and hazards. The Walker Spur and its directissima on the Grandes Jorasses: same coordinates, overlapping names, grades 6a vs 7a. Automatic merging risks giving the user wrong grade/gear information — a safety issue.

**Every source document stays independent** with explicit source + date attribution. The LLM presents them separately and does not assert identity between descriptions.

### Section-level embeddings

Embed one chunk per chapter (Overview, Approach, Route Description, Essential Gear…), not one chunk per route. This allows precise retrieval — a query about gear retrieves gear sections specifically, not whole-route documents that mention gear once.

### Chronological tracking is critical

`source_date` on every description. Mountains change fast — a 2008 glacier approach may no longer exist. The LLM should flag when descriptions are old and contradict each other.

### Cover image only

Only the cover/hero image at the top of the page is reliably downloadable without authentication. Inline section images are either tiny thumbnails (70-100px, useless) or gated behind registration for full-size.

---

## HTML structure (confirmed against Cosmiques Arête page)

| Field | CSS selector |
|---|---|
| Route name | `h1.adventure-title` |
| Location string | `.location > a > span` |
| Lat / Lon | `table.object-properties-table` — row where `th` contains "Lat/Lon" |
| Route type | same table, "Route Type" row |
| Time required | same table, "Time Required" row |
| Difficulty | same table, "Difficulty" row |
| Views / score / votes | `.stats ul li .info` |
| Description sections | `div.full-content` → `h2.in-title` + following `<p>` tags |
| Cover image | `div.cover-image` inline `background-image` style |

Images are served from `images-sp.summitpost.org` via ImageKit — modify the `w-N` parameter for resolution.

---

## RAG query types

| Query | Mechanism |
|---|---|
| "Routes near Chamonix" | Spatial query on lat/lon — SQLite, no vectors |
| "Routes starting from Col du Midi" | Vector search on embedded approach sections |
| "Compare routes on this mountain" | Retrieve all routes with matching parent area, LLM summarises |
| "Deep dive on route X" | Direct lookup all sources by coords/area, ordered newest-first |
| "Fill missing gear info" | Embed route overview → nearest-neighbour retrieval of gear sections from similar-grade/area routes |

---

## File layout

```
src/
  summitpost.py       # Phase 1: fetcher + parser
  rag.py              # Phase 3: ChromaDB retrieval layer
scripts/
  discover_routes.py  # Phase 1: collect route URLs
  scrape_routes.py    # Phase 1: bulk scrape loop
data/
  summitpost.db       # SQLite: metadata + section text
  route_urls.txt      # discovered URLs (input to scraper)
  failed_urls.txt     # scrape failures (for retry)
  images/             # downloaded cover images
```

---

## Future corpora (after SummitPost RAG is wired up)

See ROADMAP.md Phase 4 for the full source list and coverage notes. Priority candidates for scraping:

- `bergsteigen.com` — structured route sheets, good for DACH coverage
- `passion-alpes.com` — ~150 Mont Blanc area topos, fetchable
- `lemkeclimbs.com` — rich English-language Alps topos
- `desnivel.com` — Pyrenees and Spain
- `hikr.org` / `mountainproject.com` — trip reports (bot-blocked, handle carefully)
