"""
LLM integration via the Anthropic API.

The LLM's job is to synthesize free-text topo and conditions data into a
structured route analysis. It is never responsible for grade filtering or
route ranking — those are handled deterministically in src/grades.py.
"""

import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import anthropic
import requests_cache

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_MODEL = "claude-haiku-4-5-20251001"

_client: anthropic.Anthropic | None = None
_topo_session = requests_cache.CachedSession("topo_cache", expire_after=3600)
_ROUTE_ANALYSIS_PROMPT = (_PROMPTS_DIR / "route_analysis.md").read_text()

# Skip non-text sources (videos, book purchase pages) that would return empty or
# irrelevant content. "tvmountain" and "eosya" are video/subscription platforms.
_SKIP_URL_PATTERNS = ["youtube.com", "youtu.be", "tvmountain", "eosya", "amazon", "fnac", "glenat"]


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


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
# Outing selection
# ---------------------------------------------------------------------------

def _select_outing_ids(stubs: list[dict], today: date) -> set[int]:
    """
    Choose which outings to full-fetch from a list of stubs.

    Selects:
    - 2 most recent (for current conditions)
    - Up to 3 from the same month ±30 days in prior years, preferring good/excellent
      ratings (for seasonal reference)
    """
    dated: list[tuple[dict, date]] = []
    for s in stubs:
        raw = s.get("date_start")
        if raw:
            try:
                dated.append((s, datetime.strptime(raw, "%Y-%m-%d").date()))
            except ValueError:
                pass

    dated.sort(key=lambda x: x[1], reverse=True)

    # 2 most recent regardless of season
    recent_ids = {s["document_id"] for s, _ in dated[:2]}

    # Up to 3 from same season (±30 days of today's date) in prior years,
    # preferring good/excellent ratings
    window = timedelta(days=30)
    today_no_year = today.replace(year=2000)
    seasonal = [
        (s, d) for s, d in dated
        if d.year < today.year
        and abs(d.replace(year=2000) - today_no_year) <= window
    ]
    _GOOD = {"good", "excellent"}
    seasonal.sort(key=lambda x: (x[0].get("condition_rating") not in _GOOD, -x[1].year))
    seasonal_ids = {s["document_id"] for s, _ in seasonal[:3]}

    return recent_ids | seasonal_ids


# ---------------------------------------------------------------------------
# Main analysis function
# ---------------------------------------------------------------------------

def build_analysis_prompt(
    route: dict,
    stubs: list[dict],
    full_outings: list[dict],
    user_params: dict,
    today: date,
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
    if external:
        urls = _extract_urls(external)
        for url in urls:
            content = _fetch_topo_page(url)
            if content:
                fetched_pages.append(f"[Fetched from {url}]\n{content}")
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
        "rock_redpoint": "Rock redpoint",
        "rock_trad": "Rock trad",
        "ice_max": "Ice max",
        "mixed_max": "Mixed max",
        "alpine_max": "Alpine max",
        "alpine_routes_count": "Alpine routes done",
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
        weather = oloc.get("weather") or ""
        outing_lines.append(f"\n### Report dated {d} ({age})  (rating: {r})")
        if conditions:
            outing_lines.append(f"Conditions: {conditions[:600]}")
        if weather:
            outing_lines.append(f"Weather: {weather[:200]}")
    outing_block = "\n".join(outing_lines)

    return "\n\n".join([
        f"Today's date: {today.isoformat()}",
        topo_block,
        profile_block,
        date_block,
        outing_block,
    ])


def analyze_route(
    route: dict,
    stubs: list[dict],
    full_outings: list[dict],
    user_params: dict,
    today: date,
) -> str:
    """
    Return a markdown-formatted five-section route analysis.

    Sections: Route overview, Topo links, Seasonality, Recent conditions,
    Relative to your level.
    """
    user_msg = build_analysis_prompt(route, stubs, full_outings, user_params, today)
    response = _get_client().messages.create(
        model=_MODEL,
        max_tokens=2000,
        system=_ROUTE_ANALYSIS_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    return response.content[0].text.strip()
