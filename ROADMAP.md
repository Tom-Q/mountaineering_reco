# Improvement Roadmap: Mountaineering Recommender

## What's been built

**Phases 1–4.6 complete.** The app has: tool-use agentic chat (Sonnet/Haiku toggle), Camptocamp API integration, a local corpus of ~17,000 documents across 8 sources (SummitPost, hikr, SAC, passion-alpes, lemkeclimbs, Freedom of the Hills, Mémento FFCAM, refuges), ChromaDB RAG with card-based retrieval, prompt caching, parallel tool calls, web search, a seasonality histogram from C2C outing stubs, daylight calculation, avalanche bulletins (France/CH/IT/AT), GMBA mountain range lookup, and a post-hoc reviewer agent for hallucination detection.

---

## Phase 5 — UI and prompting improvements

### Remaining items

- **Weather: multiple elevation bands** — fetch at trailhead, mid-route, and summit for routes with large altitude gain. The weather tool currently accepts a single `elevation_m`; extend to a list.
- **C2C profile integration** — load grades from a public Camptocamp numeric user ID via `GET /profiles/{user_id}` and populate the sidebar selectors automatically.
- **Meteoblue for weather** — switch from Open-Meteo to Meteoblue (better resolution in the Alps, multi-model ensemble). Requires a paid API key.
- **Weather prompt verbosity** — instruct the model to report facts rather than draw stability conclusions.

### Avalanche — regions not yet integrated

- **Slovenia**: CAAMLv6 format, same as existing EAWS feeds. Stable `/latest/` URL path needs confirming before wiring up.
- **Spanish Pyrenees (AEMET)**: HTML only, not machine-readable. Surface a direct link to the bulletin page for routes in affected regions (Nov–May): https://www.aemet.es/es/eltiempo/prediccion/montana/boletin_peligro_aludes
- **AT-05/AT-06/AT-08**: wired up in `_EAWS_PROVIDERS` but feeds currently 404 (seasonal). Will activate automatically when feeds return.

---

## Phase 6 — Hosting + web frontend

Expose the tool as an API and embed it in the Astro website (thomas-colin.com, hosted on Netlify free tier).

**Backend:** Wrap the Streamlit app as a FastAPI service. The UI/logic separation already in place makes this tractable. Host on a persistent server so ChromaDB survives restarts.

**Frontend:** JavaScript chatbot UI on the Astro site, calling the FastAPI backend.

**Access control:** shared secret key distributed to friends via email/WhatsApp. Backend checks the key on every request. Keeps Claude API costs bounded (~10 EUR/month hard limit).

**Hosting plan:**
- **Target: Hetzner CAX11** (~€5/month, 2 vCPU ARM, 4GB RAM, persistent disk) — best value. Same price as Render paid tier but full VPS control.
- **Alternative**: store the ChromaDB index in the private repo (~50MB binary) and fetch it on startup — hacky but functional for a small corpus.
- **Oracle Cloud free tier** (2 permanent ARM VMs) — genuinely free with persistent disk, more setup.

---

## Backlog

### Reviewer agent — known limitations
The post-hoc reviewer (Haiku) receives raw JSON tool results as source data, which is verbose. A future improvement: pre-format source data into a clean human-readable summary before passing it to the reviewer, so fact-checking is more reliable.

### Reviewer agent for multi-turn analysis
The current reviewer only fires when `find_route` is called in a single turn. It does not cover cases where the analysis is built across multiple turns or uses only RAG sources.
