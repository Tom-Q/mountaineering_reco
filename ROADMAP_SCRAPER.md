# SummitPost Data Pipeline — Scraping, RAG & Retrieval

## Why this project

SummitPost complements Camptocamp: for popular European routes, Camptocamp is excellent and has fresher conditions data. SummitPost adds value for North American routes, English-language descriptions, structured gear/approach info, and routes absent from Camptocamp. The two datasets work together, not as replacements.

~2300 mountaineering routes worldwide (the mountaineering filter captures mixed/ice/alpine sub-types). ~500 ski routes can be scraped in the same pass for future use. Trip reports are stale — route info only.

---

## Phases

### Phase 1 — Scraping ✅ Done
Fetched and parsed all SummitPost mountaineering route pages. 2,313 routes stored in `data/summitpost.db` (structured metadata + section text + cover image URL). Parser handles both modern `<p>`-based pages and older table/orphaned-`<li>` layouts.

### Phase 2 — RAG indexing
Embed each section (one embedding per chapter: Overview, Approach, Route Description, Essential Gear, etc.) into ChromaDB. Store route metadata (grade, coords, area, source, date) alongside each vector for filtering.

### Phase 3 — Retrieval integration
`src/rag.py`: at query time, convert the query to a vector, retrieve the nearest sections with optional metadata filters (grade range, area, source_date). Surface results to the LLM as additional context alongside Camptocamp data.

---

## Tech stack decisions

| Component | Choice | Rationale |
|---|---|---|
| Cloudflare bypass | `cloudscraper` | Emulates browser fingerprint without launching a real browser; already used in adjacent project |
| HTML parsing | `BeautifulSoup4` + `lxml` | Stable CSS classes on SummitPost pages; lxml is fastest BS4 backend |
| Structured metadata | SQLite (`data/summitpost.db`) | ~2300 routes is trivial for SQLite; no server needed |
| Vector store | **ChromaDB** | More mature than sqlite-vec, better Python tooling, local/no-server |
| Embeddings | Anthropic or OpenAI `text-embedding-3-small` | TBD at Phase 2 |

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
