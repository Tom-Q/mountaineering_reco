# Improvement Roadmap: Mountaineering Recommender

## Architecture evolution

### Phase 1 — Tool use ✅ Done

### Phase 2 — Chat tab ✅ Done

### Phase 3 — Hut data
Build a local hut database to eliminate per-request API calls for static hut info.

**Sources:**
- **refuges.info API** — French huts. Free, no auth, GeoJSON, full detail including comments. Endpoint: `/api/massif` or `/api/bbox` with `detail=full`.
- **SAC hut finder** (`sac-cas.ch`) — Swiss huts
- **DAV hut finder** (`alpenverein.de`) — German/Austrian huts
- **CAI / refugi.info** — Italian huts
- **Camptocamp hut pages** — cross-reference; useful for meteoblue links, route lists, guardian contacts

**Data to cache per hut:** coordinates, altitude, capacity, opening dates, guardian contact, access description, price, linked routes, meteoblue URL.

**Storage:** local SQLite or JSON; refresh on a schedule (weekly/monthly for operational data, rarely for static).

### Phase 4 — Topo scraping + RAG 🔄 In progress

Build a curated local vector store for static route beta.

**SummitPost** — first corpus, scraping complete. ~2,300 mountaineering routes scraped into `data/summitpost.db` (structured metadata + section text + cover image). RAG indexing next.

**Further corpus candidates:**

| Source | Coverage | Notes |
|---|---|---|
| `montagnes-magazine.com` | French Alps, Francophone Switzerland, Aosta Valley | High-quality topos for classic routes |
| `passion-alpes.com` | Mont Blanc massif, Aiguilles Rouges | ~150 topos, mostly Mont Blanc area. Fetchable. |
| `bergsteigen.com` | German-speaking Alps (Austria, Bavaria, Swiss-German) | Full route sheets: grade, gear, approach, season |
| `sac-cas.ch` | Switzerland | SAC route database |
| `verticalpirate-escalade.com` | French-language rock + alpine (Mediterranean + worldwide) | Guide's personal site; fetchable |
| `desnivel.com` | Spain, Pyrenees | Spain's main alpinism publication. Spanish-language. |
| `27crags.com` | Scandinavia | Free topo + logbook, strong Norwegian/Swedish coverage |
| `hikr.org` | Alps multilingual | User trip reports in FR/DE/IT/EN. Strong Swiss/Austrian/Italian coverage. Bot-blocked. |
| `mountainproject.com` | North America | Thin on European alpine, strong for NA rock and alpine |
| `lemkeclimbs.com` | Alps | Rich English-language topo source |
| Camptocamp route pages | Worldwide | Complement to API data already integrated |
| Park/reserve access rules | France, Italy | Seasonal closures, permits — Écrins, Mercantour, etc. |

**Historical trip reports:** dated reports remain useful for seasonal pattern recognition. Include with explicit date metadata so the LLM can weight recency appropriately.

**Stack:** Chroma or LanceDB (local, no infrastructure). Embed with sentence-transformers or Claude. ~500–2000 documents total — manageable.

**Ethics / scraping policy:**
- Respect `robots.txt` and terms of service
- Rate-limit aggressively (≥1 req/5s), cache permanently for static content
- Personal/non-commercial use only
- For bot-blocked sites: attempt politely, fall back to manual lookup links on failure

### Phase 5 — Hosting + web frontend

Expose the tool as an API and embed it in the Astro website (thomas-colin.com, hosted on Netlify free tier).

**Backend:** Rewrite or wrap the Streamlit app as a FastAPI service. The UI/logic separation already in place makes this tractable. Host on a persistent server so ChromaDB (vector index) survives restarts.

**Frontend:** JavaScript chatbot UI on the Astro site, calling the FastAPI backend.

**Access control:** shared secret key distributed to friends via email/WhatsApp. Backend checks the key on every request. Keeps Claude API costs bounded (~10 EUR/month hard limit).

**Hosting plan:**
- **Pre-RAG** (app as-is): Render free tier works — no persistent storage needed, cold starts are just annoying
- **With RAG**: Render free tier breaks — ChromaDB index lives on disk, wiped on every restart (every 15 min of inactivity). Re-building takes 5–10 min, not viable
- **Target: Hetzner CAX11** (~€5/month, 2 vCPU ARM, 4GB RAM, persistent disk) — best value once RAG is added. Same price as Render paid tier but full VPS control
- **Alternative**: store the ChromaDB index in the private repo (~50MB binary) and fetch it on startup — hacky but functional for our small corpus if avoiding a VPS is important
- **Oracle Cloud free tier** (2 permanent ARM VMs) — genuinely free with persistent disk, more setup

---

## Backlog

### Multi-agent parallelism
Dispatch separate fetch agents simultaneously (one per data source), each returning a strict JSON contract, then merge results. Reduces latency and keeps concerns separated.

### Report template overhaul
Current route analysis output is unstructured. Adopt a proper report template with consistent sections:
Overview → Route description → Crux → Hazards → Current Conditions (weather + avalanche + daylight) → Trip Reports → Information Gaps → Sources.

Notes:
- "Crux" section: describe the hardest move/section specifically, not just the overall difficulty
- AI disclaimer should be mandatory and appear prominently
- Use bold sparingly — only for critical hazards, grade ratings, and weather windows
- "Information Gaps" section must be explicit and always present (even if empty)
- Reference: dreamiurg report template and Mount Shuksan example output

### Daylight calculation
Add sunrise/sunset/civil twilight for the route's coordinates and planned date. Useful for alpine start planning.
Use the `astral` Python library (pure Python, no API key). Already used by dreamiurg.

### Weather: multiple elevation bands
Consider fetching weather at multiple elevation bands (trailhead, mid-route, summit) for routes with large altitude gain. High-altitude wind and temperature can differ significantly from the base — relevant for routes with >1000m of elevation difference. The weather tool currently accepts a single `elevation_m`; extending it to accept a list of elevations and return one forecast per band would cover this.

### C2C profile integration
Load grades from a public Camptocamp numeric user ID and populate the sidebar selectors automatically.

- `GET /profiles/{user_id}` returns grade fields and outing count
- Main unknown: mapping C2C grade field names → internal names
- Accept numeric ID directly (visible in profile URL: `camptocamp.org/profiles/XXXXXXX`)

### Weather prompt verbosity
LLM weather output is too verbose. Fix: instruct the model to report facts rather than draw stability conclusions.

### NA source integration (good-to-have, worldwide applicability)
Add support for North American mountaineering sources. These are lower priority than European coverage but would make the app more universally useful:
- **PeakBagger** — peak database + ascent logs with trip reports and GPX tracks. Has an unofficial CLI wrapper (peakbagger-cli).
- **WTA** (Washington Trails Association) — detailed trip reports for PNW. Has an AJAX endpoint for report listing.
- **AllTrails** — broad coverage but JS-rendered, hard to scrape. Useful for hiker-grade routes.
- **Mountaineers.org** — technical route descriptions for Cascades.
- **NWAC** — Northwest Avalanche Center. Publishes a JSON API; would slot in alongside EAWS/MF.

Prerequisite: the tool-use architecture (Phase 1) makes this much easier to add incrementally.

### Meteoblue for weather
Switch from Open-Meteo to [meteoblue](https://www.meteoblue.com) for weather forecasts. Meteoblue is better quality (proprietary model, higher resolution in the Alps, multi-model ensemble), and Camptocamp already links to it per-hut. Requires an API key (paid). Relevant once the tool-use architecture is in place — the weather tool is the natural integration point.

### Avalanche — regions not yet integrated
- **Slovenia**: CAAMLv6 format, same as existing EAWS feeds. Date-keyed URL known; needs a stable `/latest/` path confirmed before wiring up. See comment in `src/avalanche.py`.
- **Spanish Pyrenees (AEMET)**: HTML only, not machine-readable. For routes in the relevant Pyrenean regions (Nov–May), surface a direct link to the bulletin page instead of a data integration: https://www.aemet.es/es/eltiempo/prediccion/montana/boletin_peligro_aludes — either in the avalanche tool result or in the chat response when no machine-readable bulletin is found.
- **AT-05/AT-06/AT-08**: wired up in `_EAWS_PROVIDERS` but feeds currently 404 (seasonal). Will activate automatically when feeds come back online.
