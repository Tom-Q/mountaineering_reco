# Scraping Plan — Lemke, SAC, Montagnes-Magazine

General pattern for all scrapers:
- Output to `data/<source>.db` (SQLite)
- Index into shared ChromaDB collection via `scrapers/build_<source>_index.py`
- Coordinates via `scrapers/geocode_utils.py` fallback chain
- Same two-table schema as passion-alpes: `topos` + `topo_images`

---

## 1. Lemke Climbs — PRIORITY 1

**URL:** https://www.lemkeclimbs.com/  
**Scale:** ~150 routes  
**Language:** English  
**Format:** Personal trip reports, 2500–3000 words, extensive photos  
**Coverage:** Primarily North American (North Cascades, Colorado, Wyoming, Alaska, etc.) plus some international  
**Bot protection:** None — static Weebly site, plain HTML  

### URL discovery
Walk the sidebar navigation: the site has a nested geographic menu (region → sub-region → route).
Entry point pages to crawl:
- https://www.lemkeclimbs.com/north-cascades.html
- https://www.lemkeclimbs.com/colorado.html
- etc. (one per region in the sidebar)

All route pages follow the pattern: `lemkeclimbs.com/route-name-with-hyphens.html`

Strategy: fetch the homepage/regional index pages, extract all `.html` links from the sidebar nav, deduplicate. No JS needed.

### Per-page extraction
- Title: `<h1>` or page title
- Full text: main content area (Weebly uses a standard content div)
- Photos: `<img>` tags in the main content, with captions
- Grade/region: parse from title or first paragraph (e.g. "Class 5.4", "WI3", region from URL or nav context)
- No structured fields — store everything in `full_text`, let RAG handle it

### Coordinates
Titles often name a specific peak → geocode_utils summit chain should work well for North American peaks.

### Notes
- Trip reports, not topos — richer narrative, less structured. Fine for RAG.
- Many routes have "year" context (dates of ascent) — useful for currency assessment.

---

## 2. SAC Swiss Alpine Club — PRIORITY 2

**URL:** https://www.sac-cas.ch/en/huts-and-tours/sac-route-portal/  
**Scale:** ~500 alpine routes  
**Language:** DE (primary — most complete), FR, IT, EN  
**Format:** Structured route cards: grade, ascent time, elevation, description, photos, related huts  
**Bot protection:** None on individual pages — confirmed public content  

### URL discovery — the hard part

Individual route pages confirmed working:
`https://www.sac-cas.ch/en/huts-and-tours/sac-route-portal/[NUMERIC_ID]/alpine_tour`

Example confirmed: `/1209/alpine_tour` = Monte Rosso

The *listing/index* is JS-rendered and can't be crawled with requests. Two options:

**Option A — Browser JS script (like summitpost_collect_urls.js)**  
Navigate to the portal filter page, intercept the API calls or walk the rendered DOM.
Write a console script similar to `scrapers/summitpost_collect_urls.js` that:
1. Opens https://www.sac-cas.ch/en/huts-and-tours/sac-route-portal/?discipline=alpine_tour
2. Scrolls/paginates through all results
3. Collects route IDs or full URLs
4. Downloads as a text file

**Option B — Find the underlying API**  
Open DevTools → Network tab on the portal page, filter XHR/fetch requests while browsing.
The portal likely calls an internal API like `/api/tours?discipline=alpine_tour&page=N`.
If found, can call it directly with requests (much faster than browser scripting).

**Recommended:** Try Option B first (inspect network requests), fall back to Option A.

### Language decision
Scrape **German** (`/de/huetten-und-touren/sac-tourenportal/[ID]/alpine_tour`) as primary.
German will have the most complete content for a Swiss alpine club.
The multilingual embedding model handles German natively.

### Per-page extraction
Confirmed structure on route pages:
- Peak name + elevation in `<h1>`
- Route table: difficulty (AD+), ascent time, descent time, elevation gain
- Description text in main content div
- Photo gallery
- Related huts (useful metadata)
- Archive routes (historical variations — scrape as part of full_text)

HTML structure: semantic `<section>` tags, route details in `<table>` format.

### Coordinates
SAC routes are Swiss/French/Italian Alps — geocode_utils region centroids cover most of this.
Summit geocoding from peak name should work well (named Swiss peaks are well-indexed in OSM).

---

## 3. Montagnes-Magazine — PRIORITY 3

**URL:** https://www.montagnes-magazine.com/topos?categorie=Alpinisme  
(also: `?categorie=glace` for ice climbing)  
**Scale:** ~150 articles  
**Language:** French  
**Format:** Editorial articles, many covering multiple routes per article  
**Bot protection:** Consistently blocks WebFetch — likely JS-rendered or aggressive rate limiting  

### URL discovery
The listing page is JS-rendered. Same approach as SAC:

**Browser JS script** to collect article URLs:
1. Open the topo listing page in browser
2. Scroll to load all articles (infinite scroll or pagination)
3. Collect all article links
4. Download as text file

### Per-page content
Articles may cover 1–5 routes each. Options:
- **Simple:** Store the full article as one DB record, let RAG chunk it. Easiest to implement.
- **Better:** Try to detect route boundaries within the article (look for route name headers) and split into sub-records. More work, better retrieval precision.

Recommendation: start simple (one article = one record), revisit if retrieval quality suffers.

### Paywall check
WebFetch failures may be bot protection, not a paywall. Need to verify with actual browser visit whether article content is fully public or requires subscription. Check before building scraper.

### Coordinates
Articles often name a massif or valley in the URL slug (e.g. `chamonix-aiguilles-rouges`).
Parse massif from URL → geocode_utils region centroid chain.

---

## What's already done (context for next session)

- `scrapers/passion_alpes_scrape.py` — working scraper for passion-alpes.com (148 topos)
- `scrapers/build_passion_alpes_index.py` — indexes into ChromaDB with geocoding
- `scrapers/geocode_utils.py` — shared geocoding fallback chain (summit → region centroid)
- `src/rag.py` — `get_passion_alpes_topo()` for deep-dive retrieval
- `src/tools.py` — `search_documents` and `retrieve_document` handle both SummitPost and passion-alpes
- ChromaDB collection `route_sections`: 12,897 documents (SummitPost + passion-alpes)
- `scrapers/summitpost_collect_urls.js` — reference implementation for browser-based URL collection
