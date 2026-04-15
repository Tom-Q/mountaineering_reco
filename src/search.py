"""
Stateless search and enrichment helpers.

These functions contain no Streamlit dependencies and can be called from any
context (CLI, FastAPI, tests). Session state management and progress indicators
live in app.py.
"""

from datetime import date, datetime, timedelta

from src.camptocamp import search_routes, fetch_route
from src.grades import rank_routes

_GOOD_RATINGS = {"good", "excellent"}


def fetch_page(
    bbox: str,
    activities: list[str],
    offset: int,
    page_size: int,
) -> tuple[list[dict], int]:
    """Fetch one page of routes from the Camptocamp API."""
    return search_routes(bbox, activities=activities, offset=offset, page_size=page_size)


def enrich_routes(
    all_fetched: list[dict],
    to_enrich: list[dict],
    enriched_ids: set[int],
) -> None:
    """
    Full-fetch routes that only have stub data, updating all_fetched in place.

    Search stubs are missing height_diff_access (approach vert) and other fields.
    Enriched route IDs are tracked in enriched_ids to avoid re-fetching.
    """
    for route in to_enrich:
        rid = route.get("document_id")
        if rid in enriched_ids:
            continue
        full = fetch_route(rid)
        for i, r in enumerate(all_fetched):
            if r.get("document_id") == rid:
                all_fetched[i] = {**r, **full}
                break
        enriched_ids.add(rid)


_SUPPORTED_ACTIVITIES = {"rock_climbing", "mountain_climbing", "ice_climbing", "snow_ice_mixed"}


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
    # Replace year with a fixed value on both sides so timedelta arithmetic
    # compares only month+day, ignoring which year the outing was in.
    today_no_year = today.replace(year=2000)
    seasonal = [
        (s, d) for s, d in dated
        if d.year < today.year
        and abs(d.replace(year=2000) - today_no_year) <= window
    ]
    seasonal.sort(key=lambda x: (x[0].get("condition_rating") not in _GOOD_RATINGS, -x[1].year))
    seasonal_ids = {s["document_id"] for s, _ in seasonal[:3]}

    return recent_ids | seasonal_ids


def rerank(all_fetched: list[dict], excluded_ids: set, params: dict, easy_penalty: float) -> list[dict]:
    """Filter excluded and off-discipline routes, then re-rank the remainder."""
    eligible = [
        r for r in all_fetched
        if r.get("document_id") not in excluded_ids
        and set(r.get("activities") or []).issubset(_SUPPORTED_ACTIVITIES)
    ]
    return rank_routes(eligible, params, easy_penalty)
