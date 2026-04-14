# Improvement Roadmap: Mountaineering Recommender

## Status

Last reviewed: 2026-04-14.

| Item | Status |
|------|--------|
| 1a. Improve forecast isotherm (925/600/500 hPa, min/max split) | ✅ Done |
| 1b. Historical isotherm — ERA5/CDS approach | ✅ Dropped — Open-Meteo handles this adequately |
| 2. Better C2C pre-filtering (qa=1, larger stubs limit) | ✅ Done |
| 3. About tab | ✅ Done |
| 4. Avalanche risk integration (MF BRA + EAWS/SLF) | ✅ Done |

---

## Architecture evolution

### Phase 1 — Tool use
Shift from "pre-fetch everything, call LLM once" to a tool-calling architecture where Claude decides what to fetch and when.

- Expose existing integrations (Camptocamp, weather, avalanche) as callable tools
- Keep grade filtering deterministic — expose it as a tool Claude can call, but logic stays in code
- Multi-agent pattern: separate fetch agents (parallelisable) + a synthesis agent + optional reviewer agent
- Streamlit UI remains unchanged at this stage

### Phase 2 — Chat tab
Add a conversational interface alongside (or replacing) the current form-based UI.

- New Streamlit tab using `st.chat_message` / `st.chat_input`
- Claude can ask clarifying questions ("what grade are you targeting?", "ski or ice?")
- Session-level conversation history
- Existing results tab can coexist during transition

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

### Phase 4 — Topo scraping + RAG
Build a curated local vector store for static route beta.

**Corpus candidates:**
- Guide/topo sites: `montagnes-magazine.com`, `passion-alpes.com`, `bergsteigen.com`, `sac-cas.ch`, `verticalpirate-escalade.com`
- SummitPost — historical route descriptions; site is effectively abandonware, polite scraping defensible for personal use
- Camptocamp route pages (complement to API data)
- Park/reserve access rules (seasonal closures, permits — Écrins, Mercantour, etc.)
- Approach road / parking notes where findable

**Historical trip reports:** dated reports remain useful for seasonal pattern recognition and repeat-difficulty signals. Worth including with explicit date metadata so the LLM can weight them appropriately.

**Stack:** Chroma or LanceDB (local, no infrastructure). Embed with sentence-transformers or Claude. ~500–2000 documents total — manageable.

**Ethics / scraping policy:**
- Respect `robots.txt` and terms of service
- Rate-limit aggressively (≥1 req/5s), cache permanently for static content
- Personal/non-commercial use only
- For bot-blocked sites: attempt politely, fall back to manual lookup links on failure

---

## Backlog

### C2C profile integration
Load grades from a public Camptocamp numeric user ID and populate the sidebar selectors automatically.

- `GET /profiles/{user_id}` returns grade fields and outing count
- Main unknown: mapping C2C grade field names → internal names
- Accept numeric ID directly (visible in profile URL: `camptocamp.org/profiles/XXXXXXX`)

### Weather prompt verbosity
LLM weather output is too verbose. Fix: instruct the model to report facts rather than draw stability conclusions.

### Weather checkbox cache split
Cache key is `(route_id, weather_check)` — toggling the checkbox forces a full re-run including LLM call. Weather fetch and analysis should be cached independently.

### Avalanche — regions not yet integrated
- **Slovenia**: CAAMLv6 format, same as existing EAWS feeds. Date-keyed URL known; needs a stable `/latest/` path confirmed before wiring up. See comment in `src/avalanche.py`.
- **Spanish Pyrenees (AEMET)**: HTML only, not machine-readable. URL saved as comment in `src/avalanche.py` for a future user-facing link.
- **AT-05/AT-06/AT-08**: wired up in `_EAWS_PROVIDERS` but feeds currently 404 (seasonal). Will activate automatically when feeds come back online.
