# Mountaineering Route Recommender

A personal tool for suggesting alpine objectives based on current conditions, weather forecasts, and an honest assessment of what's actually within your limits.
Also an excuse for playing around with claude code and integrating an LLM within a simple app.

## What this is

You enter your route history and hard constraints (grades, disciplines, commitment level). The app queries [Camptocamp](https://www.camptocamp.org) for candidate routes and recent conditions reports, fetches weather forecasts from [Open-Meteo](https://open-meteo.com), and uses an LLM to synthesize everything into a plain-language recommendation.

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
- **Route data:** Camptocamp API (unofficial, reverse-engineered from their frontend)
- **Weather:** Open-Meteo (free, no API key required, good mountain forecasts)
- **LLM:** Anthropic Claude API

## Project structure

```
├── app.py                  # Streamlit entry point
├── src/
│   ├── camptocamp.py       # Camptocamp API client
│   ├── weather.py          # Open-Meteo integration
│   ├── llm.py              # Anthropic API calls and prompt assembly
│   └── grades.py           # Grade parsing, normalization, constraint logic
├── prompts/
│   └── recommendation.md   # Prompt templates
└── data/
    └── grade_systems.yaml  # Grade mappings and domain knowledge
```

## Setup

```bash
git clone https://github.com/Tom-Q/mountaineering_reco
cd mountaineering_reco
pip install -r requirements.txt
cp .env.example .env
# Add your Anthropic API key to .env
streamlit run app.py
```

## Usage

1. Enter your route history using the form — route name, grade, discipline, season, conditions encountered
2. Set your hard constraints (max grades per discipline, preferred regions, available dates)
3. The app fetches candidate routes, current conditions, and weather
4. The LLM synthesizes conditions reports and weather into a recommendation with explicit reasoning

## Design decisions and known limitations

**Grade constraints are opinionated.** The app uses a custom grade mapping that weights commitment and objective hazard alongside technical difficulty. A route with significant glacier approach or serious descent is not treated the same as a route of equivalent rock grade with a walk-off.

**Conditions reports are noisy.** Camptocamp conditions reports vary enormously in quality and recency. The app surfaces report age and gives the LLM explicit instructions to flag uncertainty when reports are stale or contradictory.

**This is a personal tool.** It is not designed for general audiences and makes no safety guarantees. The recommendations are a starting point for research, not a substitute for judgment.

## Author

Thomas Colin — [GitHub](https://github.com/Tom-Q) · [LinkedIn](https://www.linkedin.com/in/thomas-colin-phd)
