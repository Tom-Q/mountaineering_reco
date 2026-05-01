# Improvement Roadmap: Mountaineering Recommender

## Architecture evolution

### Phase 1 — Tool use ✅ Done

### Phase 2 — Chat tab ✅ Done

### Phase 3 — Hut data ✅ Done

Built local hut database from refuges.info API. 4,820 huts in `refuges.db`; 1,199 (all guarded refuges + gîtes d'étape + summarised huts) indexed in ChromaDB and searchable via RAG. Metadata per hut: coordinates, altitude, capacity, opening dates, guardian contact, access description, price, meteoblue URL.

### Phase 4 — Topo scraping ✅ Done

Built a curated local corpus of static route beta and mountaineering reference material. All scrapers live in the private repo (`Tom-Q/mountaineering_scraper`).

**Corpus:**

| Source | Coverage | Status |
|---|---|---|
| Camptocamp route pages | Worldwide | Complement to API data already integrated |
| SummitPost | Worldwide mountaineering routes | ✅ ~2,300 routes in `summitpost.db` |
| hikr.org | Alps multilingual trip reports (DE/IT/FR/EN) | ✅ ~10,700 reports in `hikr.db` |
| passion-alpes.com | French-language Alpine routes | ✅ `passion_alpes.db` |
| lemkeclimbs.com | English-language topos, mostly Americas | ✅ `lemkeclimbs.db` |
| SAC route database | Switzerland | ✅ `sac.db` |
| Freedom of the Hills (10th ed.) | General mountaineering reference | ✅ `freedom_of_the_hills.db` |
| Mémento FFCAM / UIAA (FR) | General mountaineering reference | ✅ `memento_ffcam.db` |


### Phase 4.5 — RAG: document cards + retrieval ✅ Done

1. **Generate cards** ✅ — `scripts/generate_cards.py` generated structured metadata cards for all ~17,000 documents via Anthropic Batch API. Each card: `doc_type`, `date`, `trustworthiness`, `mountain_range`, `grades`, `language`, `summary`. Cards stored as columns in each source DB table.
2. **Embed cards** ✅ — `scripts/build_index.py` builds ChromaDB `cards` collection from all 8 source DBs. 17,043 documents indexed. `doc_type` stored as native array for `$contains` filtering.
3. **Retrieve then read** ✅ — `search_and_extract` tool: search → Haiku routing → parallel Haiku extraction. Full document text never enters main conversation context. `retrieve_document` available for raw access.
4. **Test** — retrieval quality not formally evaluated yet.

**Stack:** ChromaDB (local), `all-mpnet-base-v2` embeddings, Claude Haiku for card generation and in-context extraction.

### Phase 4.6 — API cost optimisation ✅ Done

Single query was costing ~$0.35 due to O(n²) context growth with serial tool calls.

- **Prompt caching** — system prompt + progressive conversation caching. Each loop iteration marks the last message cacheable; subsequent calls only pay for the delta. Cache reads cost 10% of normal input price.
- **Parallel tool calls** — system prompt instructs LLM to batch independent calls in one response. UI shows ⚡ prefix for parallel tool invocations.
- **`search_and_extract`** — replaces `search_documents` + `retrieve_document` for normal RAG use. One tool call: search → Haiku routing on summaries → parallel Haiku extraction. Full document text never enters main context.
- **`fetch_route_full`** — replaces the serial C2C chain (search → fetch → outing list → outing details). One tool call: search → Haiku route selection → fetch topo → Haiku outing selection → parallel Haiku extraction.

### Phase 5 — UI and prompting improvements

- **Report template overhaul** — adopt consistent sections: Overview → Route description → Crux → Hazards → Current Conditions → Trip Reports → Information Gaps → Sources. See backlog for full spec.
- **Weather prompt verbosity** — instruct model to report facts rather than draw stability conclusions.
- **Daylight calculation** — sunrise/sunset/civil twilight for route coordinates and planned date using the `astral` library.
- **Weather: multiple elevation bands** — fetch at trailhead, mid-route, summit for routes with large altitude gain.
- **C2C profile integration** — load grades from a public Camptocamp user ID via `GET /profiles/{user_id}`.

### Phase 6 — Hosting + web frontend

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

### Reviewer agent for route analysis
`src/route_analysis.py` (now deleted) had a two-pass LLM pattern: a writer pass produces a
structured analysis, then a reviewer pass checks for invented conditions or hallucinated data
and either passes or returns a revised output as JSON. The reviewer used a separate system
prompt (`prompts/route_reviewer.md`) and the same Haiku model. If a structured analysis mode
is revived, restore this pattern — it's a clean way to catch confident hallucinations without
adding complexity to the main prompt.

```python
# Sketch of the pattern:
response = client.messages.create(model=..., system=WRITER_PROMPT, messages=[user_msg])
analysis = response.content[0].text

reviewer_msg = "## Source data\n" + user_msg + "\n\n---\n\n## Analysis to review\n" + analysis
verdict = client.messages.create(model=..., system=REVIEWER_PROMPT, messages=[reviewer_msg])
parsed = json.loads(verdict.content[0].text)
if parsed["verdict"] == "revise" and parsed["revised_output"]:
    analysis = parsed["revised_output"]
```

### Seasonality from outing date distribution
`src/route_analysis.py` (now deleted) built a date-distribution block from all Camptocamp
outing stubs (not just the selected full reports), formatted as a dated list with age labels
and condition ratings. This gives the LLM a long-range view of when the route is typically
attempted and in what condition — useful for seasonality assessment independent of recent
conditions. If a structured analysis mode is revived, include this block:

```python
date_lines = [f"## All trip report dates ({len(stubs)} total, today is {today})"]
for s in sorted(stubs, key=lambda x: x.get("date_start") or "", reverse=True):
    d = s.get("date_start") or "?"
    r = s.get("condition_rating") or "—"
    age = _age_label(d)
    date_lines.append(f"- {d}  ({age})  rating: {r}")
```

### Multi-agent parallelism ✅ Partially addressed
Parallel tool call instruction added to system prompt. `search_and_extract` and `fetch_route_full` both use `ThreadPoolExecutor` for parallel Haiku sub-calls internally. Full multi-agent dispatch (separate agents per data source) not implemented.

### Report template overhaul
Current route analysis output is unstructured. Adopt a proper report template with consistent sections:
Overview → Route description → Crux → Hazards → Current Conditions (weather + avalanche + daylight) → Trip Reports → Information Gaps → Sources.

Notes:
- "Crux" section: describe the hardest move/section specifically, not just the overall difficulty
- AI disclaimer should be mandatory and appear prominently
- Use bold sparingly — only for critical hazards, grade ratings, and weather windows
- "Information Gaps" section must be explicit and always present (even if empty)
- Reference: dreamiurg report template and Mount Shuksan example output

### Daylight calculation ✅ Done
`_compute_daylight_text` in `src/weather.py` uses `astral` + `timezonefinder` to compute civil dawn, sunrise, sunset, and civil dusk in local time for all 7 forecast days. Injected into the weather tool result as `daylight` field.

### Weather: multiple elevation bands
Consider fetching weather at multiple elevation bands (trailhead, mid-route, summit) for routes with large altitude gain. High-altitude wind and temperature can differ significantly from the base — relevant for routes with >1000m of elevation difference. The weather tool currently accepts a single `elevation_m`; extending it to accept a list of elevations and return one forecast per band would cover this.

### C2C profile integration
Load grades from a public Camptocamp numeric user ID and populate the sidebar selectors automatically.

- `GET /profiles/{user_id}` returns grade fields and outing count
- Main unknown: mapping C2C grade field names → internal names
- Accept numeric ID directly (visible in profile URL: `camptocamp.org/profiles/XXXXXXX`)

### Weather prompt verbosity
LLM weather output is too verbose. Fix: instruct the model to report facts rather than draw stability conclusions.

### Meteoblue for weather
Switch from Open-Meteo to [meteoblue](https://www.meteoblue.com) for weather forecasts. Meteoblue is better quality (proprietary model, higher resolution in the Alps, multi-model ensemble), and Camptocamp already links to it per-hut. Requires an API key (paid). Relevant once the tool-use architecture is in place — the weather tool is the natural integration point.

### GMBA unnamed polygons — fall back to ancestry name
Some GMBA Basic polygons have no official name (all name fields are `nan`). These get `mountain_range: None` in generated cards. Fix: when all name fields are empty, parse the last named segment from the `ancestry_en` string (e.g. `"... > Glarus Alps > Glarus Alps (nn)"` → `"Glarus Alps"`). Also clean up `nan` values already written to `ranges_lookup.json` at the source, in `scripts/precompute_ranges.py`.

### Avalanche — regions not yet integrated
- **Slovenia**: CAAMLv6 format, same as existing EAWS feeds. Date-keyed URL known; needs a stable `/latest/` path confirmed before wiring up. See comment in `src/avalanche.py`.
- **Spanish Pyrenees (AEMET)**: HTML only, not machine-readable. For routes in the relevant Pyrenean regions (Nov–May), surface a direct link to the bulletin page instead of a data integration: https://www.aemet.es/es/eltiempo/prediccion/montana/boletin_peligro_aludes — either in the avalanche tool result or in the chat response when no machine-readable bulletin is found.
- **AT-05/AT-06/AT-08**: wired up in `_EAWS_PROVIDERS` but feeds currently 404 (seasonal). Will activate automatically when feeds come back online.
