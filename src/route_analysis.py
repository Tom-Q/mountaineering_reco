"""
Route analysis via the Anthropic API.

Handles the single-shot LLM call that synthesises topo and conditions data
into a structured route analysis. Grade filtering and route ranking are not
done here — those live in src/grades.py.
"""

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import requests_cache

from src.client import _get_client

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_MODEL = "claude-haiku-4-5-20251001"
# Haiku is fast and cheap for the reviewer's structured-output task.
# Switch to claude-sonnet-4-6 if invented-condition misses become a problem in practice.
_REVIEWER_MODEL = "claude-haiku-4-5-20251001"

_topo_session = requests_cache.CachedSession(".cache/topo_cache", expire_after=3600)
_ROUTE_ANALYSIS_PROMPT = (_PROMPTS_DIR / "route_analysis.md").read_text()
_ROUTE_SUMMARY_PROMPT  = (_PROMPTS_DIR / "route_summary.md").read_text()
_ROUTE_REVIEWER_PROMPT = (_PROMPTS_DIR / "route_reviewer.md").read_text()

# Skip non-text sources (videos, book purchase pages) that would return empty or
# irrelevant content. "tvmountain" and "eosya" are video/subscription platforms.
_SKIP_URL_PATTERNS = ["youtube.com", "youtu.be", "tvmountain", "eosya", "amazon", "fnac", "glenat"]


# ---------------------------------------------------------------------------
# Web fetch helpers
# ---------------------------------------------------------------------------

def _extract_urls(text: str) -> list[str]:
    """Extract http(s) URLs from markdown-flavoured text."""
    return re.findall(r'https?://[^\s\)\]"\']+', text)


def _fetch_topo_page(url: str) -> str | None:
    """
    Fetch a topo page and return its readable text content (first 2000 chars).

    Skips video sites and book-purchase pages. Returns None on any failure.
    """
    if any(pat in url for pat in _SKIP_URL_PATTERNS):
        return None
    try:
        resp = _topo_session.get(url, timeout=8, headers={"User-Agent": "mountaineering-reco-dev/0.1"})
        resp.raise_for_status()
        html = resp.text
        # Strip script / style blocks
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        # Strip all remaining tags
        text = re.sub(r"<[^>]+>", " ", html)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text[:2000] if text else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Lightweight one-sentence summary
# ---------------------------------------------------------------------------

def summarize_route(route: dict, report_count: int = 0) -> str:
    """
    Return a single guidebook-style sentence describing the route.

    Uses topo fields from the enriched route dict (_locale populated by fetch_route).
    report_count is passed separately (derived from stubs) for popularity context.
    """
    name  = route.get("title") or "Unknown route"
    area  = route.get("title_prefix") or ""
    grade = route.get("global_rating") or "unknown grade"
    locale = route.get("_locale") or {}

    lines = [f"Route: {area} — {name} (grade: {grade})"]

    duration_h = (route.get("calculated_duration") or 0) * 24
    if duration_h > 0:
        lines.append(f"Duration: {duration_h:.0f}h")

    elev_max = route.get("elevation_max")
    if elev_max:
        lines.append(f"Summit elevation: {int(elev_max)}m")

    activities = route.get("activities") or []
    if activities:
        lines.append(f"Activities: {', '.join(activities)}")

    if report_count:
        lines.append(f"Trip reports on Camptocamp: {report_count}")

    for field in ("description", "remarks", "slope"):
        val = locale.get(field)
        if val:
            lines.append(f"{field}: {val[:600]}")

    response = _get_client().messages.create(
        model=_MODEL,
        max_tokens=80,
        system=_ROUTE_SUMMARY_PROMPT,
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )
    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Main analysis function
# ---------------------------------------------------------------------------

def build_analysis_prompt(
    route: dict,
    stubs: list[dict],
    full_outings: list[dict],
    user_params: dict,
    today: date,
    weather=None,
    avalanche: list | None = None,
    gaps: list[str] | None = None,
) -> str:
    """
    Build the user message for the route analysis LLM call.

    Returns the assembled prompt string. Pure function — no API calls.
    """
    locale = route.get("_locale") or {}
    name = route.get("title") or "Unknown route"
    area = route.get("title_prefix") or ""
    grade = route.get("global_rating") or "unknown grade"

    # --- Route topo data ---
    route_id = route.get("document_id")
    c2c_url = f"https://www.camptocamp.org/routes/{route_id}" if route_id else None
    topo_parts = [f"# Route: {area} — {name}  (grade: {grade})\n"
                  + (f"Camptocamp URL: {c2c_url}\n" if c2c_url else "")]
    for field in ("description", "remarks", "slope", "route_history", "gear"):
        val = locale.get(field)
        if val:
            topo_parts.append(f"## {field}\n{val[:1500]}")

    external = locale.get("external_resources") or ""

    # --- External topo pages ---
    fetched_pages: list[str] = []
    _gaps: list[str] = list(gaps) if gaps else []
    if external:
        urls = _extract_urls(external)
        for url in urls:
            content = _fetch_topo_page(url)
            if content:
                fetched_pages.append(f"[Fetched from {url}]\n{content}")
            elif not any(pat in url for pat in _SKIP_URL_PATTERNS):
                _gaps.append(f"Topo page could not be fetched: {url}")
            if len(fetched_pages) >= 2:
                break

    topo_block = "\n\n".join(topo_parts)
    if external:
        topo_block += f"\n\n## external_resources\n{external[:1000]}"
    for page in fetched_pages:
        topo_block += f"\n\n## Fetched topo page\n{page}"

    # --- User profile ---
    profile_lines = ["## User climbing profile"]
    profile_map = {
        "rock_onsight": "Rock onsight",
        "rock_trad": "Rock trad",
        "ice_max": "Ice max",
        "mixed_max": "Mixed max",
        "alpine_max": "Alpine max",
    }
    for key, label in profile_map.items():
        val = user_params.get(key)
        if val:
            profile_lines.append(f"- {label}: {val}")
    profile_block = "\n".join(profile_lines)

    def _age_label(date_str: str) -> str:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            days = (today - d).days
            if days < 14:
                return f"{days}d ago"
            if days < 60:
                return f"{days // 7}w ago"
            return f"{days // 30}mo ago"
        except ValueError:
            return "?"

    # --- Date distribution from all stubs ---
    date_lines = [f"## All trip report dates ({len(stubs)} total, today is {today.isoformat()})"]
    for s in sorted(stubs, key=lambda x: x.get("date_start") or "", reverse=True):
        d = s.get("date_start") or "?"
        r = s.get("condition_rating") or "—"
        age = _age_label(d) if d != "?" else "?"
        date_lines.append(f"- {d}  ({age})  rating: {r}")
    date_block = "\n".join(date_lines)

    # --- Selected full outings ---
    outing_lines = [f"## Selected trip reports (full text, {len(full_outings)} reports)"]
    for o in sorted(full_outings, key=lambda x: x.get("date_start") or "", reverse=True):
        d = o.get("date_start") or "?"
        r = o.get("condition_rating") or "—"
        age = _age_label(d) if d != "?" else "?"
        oloc = o.get("_locale") or {}
        conditions = oloc.get("conditions") or ""
        outing_weather = oloc.get("weather") or ""
        outing_lines.append(f"\n### Report dated {d} ({age})  (rating: {r})")
        if conditions:
            outing_lines.append(f"Conditions: {conditions[:600]}")
        if outing_weather:
            outing_lines.append(f"Weather: {outing_weather[:200]}")
    outing_block = "\n".join(outing_lines)

    parts = [
        f"Today's date: {today.isoformat()}",
        topo_block,
        profile_block,
        date_block,
        outing_block,
    ]

    if weather is not None:
        wx_lines = [
            f"## Current weather",
            f"Fetched: {weather.fetch_date}  |  "
            f"Coords: {weather.coords[0]:.3f}N, {weather.coords[1]:.3f}E",
        ]
        if weather.fetch_errors:
            wx_lines.append(f"⚠ Fetch errors: {'; '.join(weather.fetch_errors)}")
        if weather.forecast_text:
            wx_lines.append("\n### 7-day forecast\n" + weather.forecast_text)
        if weather.historical_text:
            wx_lines.append("\n### Snowfall history\n" + weather.historical_text)
        parts.append("\n".join(wx_lines))

    for bulletin in (avalanche or []):
        if not bulletin.fetch_error and bulletin.llm_text:
            parts.append(bulletin.llm_text)
        elif bulletin.fetch_error:
            _gaps.append(
                f"Avalanche bulletin unavailable ({bulletin.provider_name}): {bulletin.fetch_error}"
            )

    if _gaps:
        parts.append("## Information gaps\n" + "\n".join(f"- {g}" for g in _gaps))

    return "\n\n".join(parts)


def _review_analysis(user_msg: str, analysis: str, weather, avalanche) -> str:
    """
    Run the reviewer LLM call against the writer's output.

    Returns the (possibly revised) analysis string. Falls back to the original
    analysis if the verdict is pass, JSON parsing fails, or revised_output is empty.
    """
    reviewer_msg = (
        "## Source data\n"
        + user_msg
        + "\n\n---\n\n"
        "## Analysis to review\n"
        + analysis
    )
    response = _get_client().messages.create(
        model=_REVIEWER_MODEL,
        max_tokens=_max_tokens_for_analysis(weather, avalanche),
        system=_ROUTE_REVIEWER_PROMPT,
        messages=[{"role": "user", "content": reviewer_msg}],
    )
    raw = response.content[0].text.strip()
    # Strip markdown code fences if the model wraps the JSON
    if raw.startswith("```"):
        raw = re.sub(r"^```[^\n]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    try:
        verdict = json.loads(raw)
    except Exception:
        return analysis  # malformed JSON — use writer output unchanged

    if verdict.get("verdict") == "revise" and verdict.get("revised_output"):
        return verdict["revised_output"].strip()
    return analysis


def _max_tokens_for_analysis(weather, avalanche) -> int:
    if weather is None:
        return 2000
    if avalanche:
        return 2800
    return 2500


def analyze_route(
    route: dict,
    stubs: list[dict],
    full_outings: list[dict],
    user_params: dict,
    today: date,
    weather=None,
    avalanche: list | None = None,
    gaps: list[str] | None = None,
) -> str:
    """
    Return a markdown-formatted route analysis.

    Standard sections: Route overview, Topo links, Seasonality, Recent conditions,
    Relative to your level. A Weather outlook section is appended when weather data
    is provided. An Information gaps section is appended when any data source failed.
    """
    user_msg = build_analysis_prompt(
        route, stubs, full_outings, user_params, today, weather,
        avalanche=avalanche, gaps=gaps,
    )
    response = _get_client().messages.create(
        model=_MODEL,
        max_tokens=_max_tokens_for_analysis(weather, avalanche),
        system=_ROUTE_ANALYSIS_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    analysis = response.content[0].text.strip()
    return _review_analysis(user_msg, analysis, weather, avalanche)
