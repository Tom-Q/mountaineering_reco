# Scraping Plan

General pattern for all scrapers:
- Output to `data/<source>.db` (SQLite)
- Index into shared ChromaDB collection via `scrapers/build_<source>_index.py`
- Coordinates via `scrapers/geocode_utils.py` fallback chain (or exact if available)
- Same two-table schema as passion-alpes: `topos` + `topo_images`

---

## Done

### passion-alpes.com
- `scrapers/passion_alpes_scrape.py` + `scrapers/build_passion_alpes_index.py`
- 148 topos, French Alps, French language, full topo text
- Indexed: 166 ChromaDB documents

### SAC Swiss Alpine Club
- `scrapers/sac_scrape.py` + `scrapers/build_sac_index.py`
- 820 routes across 499 summits, Swiss Alps, German language
- Pure API scraper (`suissealpine.sac-cas.ch/api/1/poi/search`) — no HTML needed
- Exact WGS84 coordinates from API (Swiss LV95 converted)
- Note: descriptions are summit-level overview only; per-route topo text is behind paywall and not in the API
- Indexed: 820 ChromaDB documents
- `src/rag.py`: `get_sac_topo()` for deep-dive retrieval
- `src/tools.py`: `retrieve_document` handles `sac_id`

### SummitPost
- `scrapers/summitpost_scrape.py` + `scrapers/summitpost_collect_urls.js`
- ~4800 mountaineering routes, global coverage, English
- Indexed: 12,731 ChromaDB documents

---

## Backlog

### 1. hikr.org — PRIORITY 1

**URL:** https://www.hikr.org  
**Scale:** Tens of thousands of trip reports  
**Language:** DE, FR, IT, EN (community site, multilingual)  
**Format:** Community trip reports with photos, GPS tracks, structured metadata (grade, elevation gain, duration, region)  
**Coverage:** Primarily Swiss/Austrian/French Alps, but global  
**Bot protection:** Unknown — needs verification

### URL discovery
hikr uses a structured search/filter system. Likely options:
- Browse by activity type (Hochtour, Skitour, etc.) and region
- May have an underlying API similar to SAC — check DevTools Network tab first
- Fall back to browser JS script if JS-rendered

Recommended first step: open the mountaineering/Hochtour listing in a browser, inspect XHR calls in DevTools.

### Per-page extraction
Trip reports have structured header fields (grade, elevation, duration, date) followed by free-text narrative and photos. Rich narrative content makes these valuable for RAG.

### Coordinates
GPS tracks are often attached — if accessible, these give exact coordinates. Otherwise summit geocoding from title should work well (reports typically name the summit).

### Notes
- Very large scale — may want to filter to alpine/mountaineering discipline and Alps region rather than scraping everything
- Reports have dates → useful for currency of conditions info
- Community content: quality varies, but volume is high

---

### 2. Lemke Climbs — PRIORITY 2

**URL:** https://www.lemkeclimbs.com/  
**Scale:** ~150 routes  
**Language:** English  
**Format:** Personal trip reports, 2500–3000 words, extensive photos  
**Coverage:** Primarily North American (North Cascades, Colorado, Wyoming, Alaska) plus some international  
**Bot protection:** None — static Weebly site, plain HTML

### URL discovery
Walk the sidebar navigation (region → sub-region → route). Entry point pages:
- https://www.lemkeclimbs.com/north-cascades.html
- https://www.lemkeclimbs.com/colorado.html
- etc.

All route pages: `lemkeclimbs.com/route-name-with-hyphens.html`. No JS needed.

### Per-page extraction
- Title: `<h1>`
- Full text: main Weebly content div
- Photos: `<img>` tags in main content
- Grade/region: parse from title or first paragraph

### Coordinates
Summit names in titles → geocode_utils summit chain works well for North American peaks.

### Notes
- Trip reports, not topos — richer narrative, less structured
- Dates of ascent present → useful for currency

---

### 3. andeshandbook.org — PRIORITY 3

**URL:** https://www.andeshandbook.org  
**Scale:** Hundreds of routes  
**Language:** Spanish and English  
**Format:** Structured route guides with approach, route description, descent, grade, photos  
**Coverage:** Andes (primarily Chile and Argentina — Aconcagua region, Patagonia, central Chile/Argentina volcanoes)  
**Bot protection:** Unknown — needs verification

### URL discovery
Site likely has a route index browsable by region or mountain range. Check for:
- A paginated route listing
- Underlying API (check DevTools)
- Static HTML index amenable to `requests`

### Per-page extraction
Structured route pages expected: approach, route description, descent as separate sections. Store each section in `full_text`, let RAG chunk.

### Coordinates
Andes peaks are well-indexed in OSM — summit geocoding should work. Some peaks (volcanoes, high Andes) may need manual centroid fallbacks added to `geocode_utils.py`.

### Notes
- Unique geographic coverage not in other sources (South America)
- Bilingual content — the multilingual embedding model handles Spanish natively
- Verify paywall status before scraping

---

### 4. verticalpirate-escalade.com — PRIORITY 4

**URL:** https://www.verticalpirate-escalade.com  
**Scale:** Unknown  
**Language:** French  
**Format:** Rock climbing topos (escalade)  
**Coverage:** Likely French Alps / Mediterranean limestone  
**Bot protection:** Unknown — needs verification

### URL discovery
Unknown — needs a browser visit to understand site structure. Check for:
- Static topo listing page
- JS-rendered content requiring browser script

### Per-page extraction
Rock climbing topos typically have: sector name, route name, grade (French sport/trad), length, description. May include topo diagrams (images).

### Coordinates
Sector/crag names → geocode_utils. Rock climbing crags are often well-indexed in OSM.

### Notes
- Rock climbing focus complements the alpine content in the other sources
- Verify scale and content quality before investing scraping effort
- Lower priority than the alpine sources given the app's alpine focus

---

### 5. Montagnes-Magazine — PRIORITY 5

**URL:** https://www.montagnes-magazine.com/topos?categorie=Alpinisme  
(also: `?categorie=glace` for ice)  
**Scale:** ~150 articles  
**Language:** French  
**Format:** Editorial articles, often covering multiple routes per article  
**Bot protection:** Consistently blocks WebFetch — JS-rendered or aggressive rate limiting

### URL discovery
JS-rendered listing. Browser JS script required:
1. Open the topo listing page
2. Scroll to load all articles
3. Collect article links, download as text file

### Per-page content
- Simple: one article = one DB record, let RAG chunk
- Better: detect route boundaries within article, split into sub-records

Start simple, revisit if retrieval quality suffers.

### Paywall check
WebFetch failures may be bot protection, not paywall. Verify with actual browser visit before building scraper.

### Coordinates
Parse massif from URL slug (e.g. `chamonix-aiguilles-rouges`) → geocode_utils region centroid.
