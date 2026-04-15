# Improvement Roadmap: Mountaineering Recommender

## Architecture evolution

### Phase 1 — Tool use ✅ Mostly done

Camptocamp, weather, and avalanche are exposed as callable tools in `src/tools.py` with strict input/output contracts. Streaming agentic loop in the chat tab calls them.

Remaining:
- Grade filtering not yet exposed as a tool — Claude can't call `rank_routes()` directly from chat. Logic is in `src/grades.py`; wrapping it in a tool is the last piece.

**Architecture patterns still to consider (informed by dreamiurg/claude-mountaineering-skills):**

- **Multi-agent parallelism** — dispatch separate fetch agents simultaneously (one per data source), each returning a strict JSON contract, then merge results. Reduces latency and keeps concerns separated. Worth exploring once basic tool use is working; not necessarily the right first step.
- **Writer + Reviewer agent separation** — a dedicated writer agent generates the report from a structured data package; a separate reviewer agent (potentially a stronger model) validates factual consistency, completeness, and safety before presenting to user. This is almost certainly the right pattern: it catches hallucinations and formatting issues without making the main agent prompt unwieldy.
- **Graceful degradation with explicit gaps** — every source failure is logged to a `gaps` array that flows through to the final output. Missing data is never silently dropped; it appears in a visible "Information Gaps" section with manual lookup links. Adopt this regardless of agent architecture.

### Phase 2 — Chat tab ✅ Mostly done

- `st.chat_message` / `st.chat_input` UI ✅
- Session-level conversation history (UI + API formats) ✅
- Streaming agentic loop with tool calling ✅
- Climber grade profile injected into system prompt ✅
- Coexists with route finder tabs ✅

Remaining gaps:
- System prompt doesn't actively instruct Claude to ask clarifying questions
- Tool results (weather data, route summaries) not shown inline — only status indicators, then prose synthesis
- Sidebar risk/engagement preferences not passed to chat context, only climbing grades are

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

### Weather: verify elevation= parameter
✅ Done — `_build_all_days` and `_fetch_historical_text` now accept and pass `elevation_m` to Open-Meteo.

Consider fetching weather at multiple elevation bands (trailhead, mid-route, summit) for routes with large altitude gain. High-altitude wind and temperature can differ significantly from the base — relevant for routes with >1000m of elevation difference. The weather tool currently accepts a single `elevation_m`; extending it to accept a list of elevations and return one forecast per band would cover this.

### C2C profile integration
Load grades from a public Camptocamp numeric user ID and populate the sidebar selectors automatically.

- `GET /profiles/{user_id}` returns grade fields and outing count
- Main unknown: mapping C2C grade field names → internal names
- Accept numeric ID directly (visible in profile URL: `camptocamp.org/profiles/XXXXXXX`)

### Weather prompt verbosity
LLM weather output is too verbose. Fix: instruct the model to report facts rather than draw stability conclusions.

### Weather checkbox cache split
Cache key is `(route_id, weather_check)` — toggling the checkbox forces a full re-run including LLM call. Weather fetch and analysis should be cached independently.

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
