# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project context

This is a personal learning project. The primary goal is to learn to use Claude Code and integrate an LLM within an app — not to ship a production tool. The mountaineering recommender is the vehicle, not the destination.

## Running the app

```bash
pip install -r requirements.txt
cp .env.example .env  # add Anthropic API key
streamlit run app.py
```

## Architecture

This is a Streamlit app that recommends alpine climbing routes by combining deterministic grade filtering with a narrowly-scoped LLM call.

**Key design principle:** The LLM never decides whether a route is within the user's limits. Grade filtering is explicit, auditable code in `src/grades.py`. The LLM's only job is assessing whether a candidate route is *in good shape right now* based on conditions reports and weather data.

**Data flow:**
1. User enters route history + constraints (grades, disciplines, commitment level)
2. `src/grades.py` deterministically filters routes against constraints using `grade_systems.yaml`
3. `src/camptocamp.py` fetches candidate routes + conditions reports from Camptocamp (unofficial API)
4. `src/weather.py` fetches forecasts from Open-Meteo
5. `src/analysis.py` calls Anthropic Claude (Haiku) for route analysis and one-line summaries; `src/chat.py` runs the streaming agentic chat loop (Sonnet)
6. Streamlit displays the plain-language recommendation

**Grade systems (`grade_systems.yaml`):** Encodes ordered progressions for alpine (F→ABO), French rock (3→8a), ice (WI1→WI6), mixed (M1→M8), and ski (S1→S6). These are not interchangeable scales — a route's commitment and objective hazard matter beyond the technical grade.

**Prompts (`prompts/`):** Versioned separately from application logic so prompt iterations are tracked independently. The prompt must instruct the model to flag uncertainty when conditions reports are stale (>2 weeks) or contradictory, and must inject user history in structured form rather than prose.

## Git commits

Do not add "Co-Authored-By: Claude" or any AI attribution to commit messages.

Never run `git commit` or `git push` unless explicitly instructed. Completing a task does not imply permission to commit.

## LLM usage

Uses the Anthropic Python SDK (`anthropic` package). API key loaded from `.env` via `python-dotenv`.
