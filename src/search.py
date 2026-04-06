"""
Stateless search and enrichment helpers.

These functions contain no Streamlit dependencies and can be called from any
context (CLI, FastAPI, tests). Session state management and progress indicators
live in app.py.
"""

from src.camptocamp import search_routes, fetch_route
from src.grades import rank_routes


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


def filter_excluded(routes: list[dict], excluded_ids: set) -> list[dict]:
    """Return routes not in the excluded set."""
    return [r for r in routes if r.get("document_id") not in excluded_ids]


_SUPPORTED_ACTIVITIES = {"rock_climbing", "mountain_climbing", "ice_climbing", "snow_ice_mixed"}


def rerank(all_fetched: list[dict], excluded_ids: set, params: dict, easy_penalty: float) -> list[dict]:
    """Filter excluded and off-discipline routes, then re-rank the remainder."""
    eligible = [
        r for r in filter_excluded(all_fetched, excluded_ids)
        if not (set(r.get("activities") or []) - _SUPPORTED_ACTIVITIES)
    ]
    return rank_routes(eligible, params, easy_penalty)
