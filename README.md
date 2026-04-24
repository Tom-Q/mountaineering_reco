# Mountaineering Preparation Assistant

Technical mountaineering involves a lot of online preparation work: finding good routes, checking hut availability, reading conditions reports, cross-referencing topos from multiple sources, checking out multiple trip reports, assessing whether a route is in shape for the current season. A bare LLM is not capable of this:

- No access to the relevant information — trip reports and route topos are behind paywalls or bot-protected, sometimes in JavaScript-rendered pages
- Pollution from beginner forums (Reddit, etc.) that dominate search results for popular objectives
- High-level mountaineering knowledge is tacit and rarely written down at all — the kind of judgment that separates a guide from a tourist doesn't end up in training data

This app is a conversational assistant: an LLM (Claude via the Anthropic API) empowered by a set of tools that give it structure and real access to the information that actually matters.

## Tools

- **Camptocamp integration** — route search by name or geographic area, full route details (description, approach, required gear, grades, images), trip report lists and full trip report text, via API
- **Avalanche forecasts** — Météo-France BRA for French massifs; EAWS CAAMLv6 feeds for Switzerland, Italy, and Austria, via API 
- **Weather** — Open-Meteo: 7-day forecast, recent snowfall history, seasonal snow accumulation, re-freeze altitude; calibrated to snow-season windows per mountain range, via API
- **RAG on ethically scraped route databases** — SummitPost (~2,300 mountaineering routes worldwide), SAC (Swiss Alpine Club, 800 routes or route stubs for those paywalled); semantic search with geographic filtering via ChromaDB and a multilingual sentence-transformer model
- **Domain knowledge** — mountain range bounding boxes, French massif polygons, grade system encodings (French rock, UIAA, WI, M-grades, alpine commitment), snow season windows per range

## Why an LLM isn't enough on its own

Even with tool access, the LLM needs to be constrained in what judgments it makes. The failure modes of LLMs on mountaineering questions are predictable:

- They treat nominal grades as ground truth. An AD in poor late-season shape can be more committing than a TD in perfect conditions. A 150m route graded TD+ due to one 6b move is incomparable to 1000m of sustained difficulties at AD where the hardest move is 4.
- They can't read between the lines of conditions reports
- They don't know what they don't know, and mountaineering is a domain where confident-sounding wrong advice has real consequences
- They make very surprising mistakes reflecting lack of common-sense, with potentially dramatic consequences in an alpine context

## Stack

- **App:** Python, [Streamlit](https://streamlit.io)
- **LLM:** Anthropic Claude API (Sonnet for chat)
- **Route data:** Camptocamp API; SummitPost, SAC, passion-alpes.com (scraped, stored in SQLite)
- **RAG:** [ChromaDB](https://www.trychroma.com) + [sentence-transformers](https://www.sbert.net) (`paraphrase-multilingual-mpnet-base-v2`)
- **Weather:** [Open-Meteo](https://open-meteo.com) (free, no API key required)
- **Avalanche:** Météo-France BRA (requires API key, free on their website), EAWS CAAMLv6 (public API)

## Project structure

```
├── app.py
├── src/
│   ├── camptocamp.py       # Camptocamp API client
│   ├── weather.py          # Open-Meteo integration
│   ├── avalanche.py        # Avalanche bulletin (MF BRA + EAWS)
│   ├── rag.py              # RAG search and retrieval
│   ├── tools.py            # Tool definitions passed to Claude
│   ├── chat.py             # Streaming agentic chat loop
│   ├── grades.py           # Grade parsing and constraint logic
│   ├── geo.py              # Range classification, geocoding
│   └── search.py           # Route search and enrichment
├── prompts/
│   └── alpinist_chat.md    # Chat system prompt
├── domain_knowledge/
│   ├── grade_systems.yaml
│   ├── ranges.yaml
│   ├── snow_seasons.yaml
│   └── liste-massifs.geojson
├── scrapers/               # Data collection scripts (SummitPost, SAC, passion-alpes)
└── data/
    ├── summitpost.db
    ├── sac.db
    ├── passion_alpes.db
    └── chroma/             # Vector store (gitignored — build artifact)
```

The SQLite databases and `chroma/` vector store are gitignored. Run the scrapers in `scrapers/` to populate them.

## Setup

```bash
git clone https://github.com/Tom-Q/mountaineering_reco
cd mountaineering_reco
pip install -r requirements.txt
cp .env.example .env
# Add ANTHROPIC_API_KEY and METEOFRANCE_API_KEY to .env
streamlit run app.py
```

The app degrades gracefully without the local databases — Camptocamp, weather, and avalanche features work without them.

## Ongoing and planned work

- Various UI and ergonomy improvements
- Better prompt engineering
- Mountain hut info tool — hut name, capacity, booking contact, opening season, warden availability
- Expanding route database coverage (Andes Handbook, hobbyist route documentation sites)
- Key-protected deployment on [thomascolin.com](https://www.thomascolin.com) via FastAPI

## Author

Thomas Colin — [GitHub](https://github.com/Tom-Q) · [LinkedIn](https://www.linkedin.com/in/thomas-colin-phd)
