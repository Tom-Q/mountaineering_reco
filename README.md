# Mountaineering Route Recommender

A personal tool for suggesting alpine objectives based on current conditions, weather forecasts, and an honest assessment of what's actually within your limits.
Also an excuse for playing around with Claude Code and integrating an LLM within a simple app.

## What this is

You enter your route history and constraints. The app queries [Camptocamp](https://www.camptocamp.org) for candidate routes and recent conditions reports, fetches weather forecasts from [Open-Meteo](https://open-meteo.com), and uses an LLM to synthesise everything into a plain-language recommendation. A chat tab lets you drill into any route in depth.

Route descriptions and beta also draw on a local database of ~2,300 SummitPost mountaineering routes, scraped and stored in SQLite, with RAG retrieval planned.

## Why an LLM isn't enough on its own

LLMs are bad at mountaineering route assessment. The failure modes are predictable:

- They conflate grade systems (French rock, UIAA, Yosemite, WI, M-grades, PD/AD/D/TD — these are not interchangeable)
- They treat nominal grades as ground truth. An AD in poor late-season shape can be more committing than a TD in perfect conditions. A 150m route graded "TD+" due to one 6b move is incomparable to 1000m of difficulties with difficult route-finding graded AD because the max grade is 4. Comments from other users will be interpreted at face value when that user's history determines whether their "straightforward" is the same as your "straightforward".
- They can't read between the lines of conditions reports — "a few crevasses to avoid" means something very different in July vs October
- They don't know what they don't know, and mountaineering is an area where confident-sounding wrong advice has real consequences

The architecture here is designed around these limitations:

- **Grade filtering is deterministic.** The LLM never decides whether a route is within your limits — that logic is explicit, auditable, and encodes real domain knowledge about how grade systems relate to each other
- **The LLM's job is narrow.** It reads conditions text and weather data and explains, in plain language, whether a route is *in shape right now*. That's a task it can do reasonably well
- **Prompts are versioned separately.** Prompt engineering is a first-class concern; prompts live in `/prompts` and can be iterated independently of the application logic

## Stack

- **Backend / app:** Python, [Streamlit](https://streamlit.io)
- **Route data:** Camptocamp API (unofficial, reverse-engineered from their frontend); SummitPost (scraped, stored locally in SQLite)
- **Weather:** Open-Meteo (free, no API key required)
- **Avalanche bulletins:** Météo-France BRA (French massifs), SLF (Switzerland), EUREGIO / avalanche.report (South Tyrol, Trentino, Tyrol), and AINEVA-affiliated feeds for other Italian regions (seasonal)
- **LLM:** Anthropic Claude API

## Project structure

```
├── app.py                   # Streamlit entry point
├── src/
│   ├── camptocamp.py        # Camptocamp API client
│   ├── weather.py           # Open-Meteo integration
│   ├── avalanche.py         # Avalanche bulletin integration (MF + EAWS)
│   ├── analysis.py          # Per-route LLM analysis and summaries
│   ├── chat.py              # Streaming agentic chat loop
│   ├── grades.py            # Grade parsing, normalization, constraint logic
│   └── search.py            # Route search and enrichment
├── prompts/
│   ├── route_analysis.md    # Per-route analysis prompt
│   └── route_summary.md     # One-line route summary prompt
├── liste-massifs.geojson    # French massif polygons for avalanche geo-lookup
└── data/
    └── grade_systems.yaml   # Grade mappings and domain knowledge
```

The SummitPost scraper and the `data/summitpost.db` database are kept in a separate private repository.

## Setup

```bash
git clone https://github.com/Tom-Q/mountaineering_reco
cd mountaineering_reco
pip install -r requirements.txt
cp .env.example .env
# Add ANTHROPIC_API_KEY and METEOFRANCE_API_KEY to .env
streamlit run app.py
```

The app expects `data/summitpost.db` to exist for SummitPost-backed features. Without it, those features degrade gracefully.

## Usage

1. Enter your route history using the form — route name, grade, discipline, season, conditions encountered
2. Set your hard constraints (max grades per discipline, preferred regions, available dates)
3. The app fetches candidate routes, current conditions, and weather
4. The LLM synthesises conditions reports and weather into a recommendation with explicit reasoning
5. Use the Chat tab to ask follow-up questions about any route in depth

## Design decisions and known limitations

**Grade constraints are opinionated.** The app uses a custom grade mapping that weights commitment and objective hazard alongside technical difficulty. A route with significant glacier approach or serious descent is not treated the same as a route of equivalent rock grade with a walk-off.

**Conditions reports are noisy.** Camptocamp conditions reports vary enormously in quality and recency. The app surfaces report age and gives the LLM explicit instructions to flag uncertainty when reports are stale or contradictory.

**This is a personal tool.** It is not designed for general audiences and makes no safety guarantees. The recommendations are a starting point for research, not a substitute for judgment.

## Author

Thomas Colin — [GitHub](https://github.com/Tom-Q) · [LinkedIn](https://www.linkedin.com/in/thomas-colin-phd)
