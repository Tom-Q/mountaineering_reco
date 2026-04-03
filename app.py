import streamlit as st
from dotenv import load_dotenv

from src.camptocamp import latlon_bbox_to_mercator, search_routes
from src.grades import (
    rank_routes, match_colour, delta_colour, GRADE_FIELDS,
    ROCK, ICE, MIXED, ALPINE,
    ENGAGEMENT, ENGAGEMENT_LABELS,
    RISK, RISK_LABELS,
    EXPOSITION, EXPOSITION_LABELS,
    EQUIPMENT, EQUIPMENT_LABELS,
)

load_dotenv()

st.set_page_config(
    page_title="Mountaineering Route Recommender",
    page_icon="🏔️",
    layout="wide",
)

st.title("🏔️ Mountaineering Route Recommender")
st.caption("Suggests alpine objectives based on your history, current conditions, and weather.")


TIME_VALUES = [2, 3, 4, 5, 6, 8, 10, 12, 18, 24, 48, 72]
TIME_LABELS = {2: "< 3h", 3: "3h", 4: "4h", 5: "5h", 6: "6h",
               8: "8h", 10: "10h", 12: "12h", 18: "18h", 24: "1 day",
               48: "2 days", 72: "3 days+"}
SPEED_OPTIONS = [0.5, 0.67, 0.8, 1.0, 1.25, 1.5, 2.0]
SPEED_LABELS  = {0.5: "2× faster", 0.67: "1.5× faster", 0.8: "1.2× faster",
                 1.0: "on par", 1.25: "1.25× slower", 1.5: "1.5× slower", 2.0: "2× slower"}


# ---------------------------------------------------------------------------
# Search parameters form — 3-column layout across the full page width
# ---------------------------------------------------------------------------
with st.form("search_params"):
    c_skill, c_fitness, c_risk = st.columns(3)

    # --- Skill -----------------------------------------------------------
    with c_skill:
        st.markdown("**Skill**")
        s1, s2, s3 = st.columns(3)
        rock_onsight  = s1.selectbox("Onsight",  ROCK, index=ROCK.index("6a+"),
            help="Hardest sport grade you can lead first-try, no falls, no beta.")
        rock_redpoint = s2.selectbox("Redpoint", ROCK, index=ROCK.index("6c"),
            help="Hardest sport grade you can lead after working the moves.")
        rock_trad     = s3.selectbox("Trad",     ["N/A"] + ROCK, index=1 + ROCK.index("6a+"),
            help="Hardest grade you can lead on gear, first try.")
        s1, s2, s3 = st.columns(3)
        ice_max    = s1.selectbox("Ice",    ["—"] + ICE,   index=1 + ICE.index("WI3"))
        mixed_max  = s2.selectbox("Mixed",  ["—"] + MIXED, index=0)
        alpine_max = s3.selectbox("Alpine", ALPINE,        index=ALPINE.index("TD+"),
            help="Hardest overall alpine grade completed in reasonable conditions.")
        alpine_routes_count = st.selectbox("Alpine routes done", ["<5", "5–20", "20–50", "50+"], index=1)

    # --- Fitness ---------------------------------------------------------
    with c_fitness:
        st.markdown("**Fitness**")
        hiking_vert_max = st.number_input(
            "Max approach/descent vert (m)",
            min_value=0, max_value=4000, value=1500, step=100,
            help="Non-technical terrain only (approach + descent).",
        )
        difficulties_vert = st.slider(
            "Technical vert (min – max, m)",
            min_value=0, max_value=1500, value=(100, 600), step=50,
            help="Vertical extent of sustained technical difficulties.",
        )
        moving_time = st.select_slider(
            "Moving time (min – max)",
            options=TIME_VALUES, value=(3, 12),
            format_func=lambda v: TIME_LABELS[v],
            help="Total moving time on the route.",
        )
        pace_hiking = st.select_slider(
            "Hiking & glacier pace (vs. Camptocamp estimates)",
            options=SPEED_OPTIONS, value=1.0,
            format_func=lambda v: SPEED_LABELS[v],
        )
        pace_technical = st.select_slider(
            "Rock & ice pace (vs. Camptocamp estimates)",
            options=SPEED_OPTIONS, value=1.0,
            format_func=lambda v: SPEED_LABELS[v],
        )

    # --- Risk ------------------------------------------------------------
    with c_risk:
        st.markdown("**Risk**")
        engagement_max = st.selectbox(
            "Max engagement",
            ENGAGEMENT, index=ENGAGEMENT.index("III"),
            format_func=lambda g: ENGAGEMENT_LABELS[g],
            help="How serious it would be to have a problem or accident: "
                 "retreat difficulty, isolation, route length, and descent complexity all factor in.",
        )
        risk_max = st.selectbox(
            "Max objective risk",
            RISK, index=RISK.index("X2"),
            format_func=lambda v: RISK_LABELS[v],
            help="Avalanche, serac, rockfall, etc.",
        )
        exposition_max = st.selectbox(
            "Max exposition",
            EXPOSITION, index=EXPOSITION.index("E3"),
            format_func=lambda v: EXPOSITION_LABELS[v],
            help="Consequence of a fall / protection spacing on rock.",
        )
        equipment_min = st.selectbox(
            "Min equipment in place",
            EQUIPMENT, index=EQUIPMENT.index("P3+"),
            format_func=lambda v: EQUIPMENT_LABELS[v],
            help="Minimum fixed gear expected. Higher P = more self-reliance required.",
        )

    submitted = st.form_submit_button("Apply", use_container_width=True)
    if submitted:
        st.session_state["applied_params"] = {
            # Skill
            "rock_onsight":           rock_onsight,
            "rock_redpoint":          rock_redpoint,          # not yet used in scoring
            "rock_trad":              None if rock_trad == "N/A" else rock_trad,
            "ice_max":                None if ice_max   == "—"   else ice_max,
            "mixed_max":              None if mixed_max == "—"   else mixed_max,
            "alpine_max":             alpine_max,
            "alpine_routes_count":    alpine_routes_count,    # not yet used in scoring
            # Fitness
            "hiking_vert_max":        hiking_vert_max,
            "difficulties_vert_min":  difficulties_vert[0],
            "difficulties_vert_max":  difficulties_vert[1],
            "moving_time_min":        moving_time[0],   # hours
            "moving_time_max":        moving_time[1],
            "pace_hiking":            pace_hiking,      # not yet used in scoring; multiplier: <1 faster, >1 slower
            "pace_technical":         pace_technical,   # not yet used in scoring
            # Risk
            "engagement_max":         engagement_max,
            "risk_max":               risk_max,
            "exposition_max":         exposition_max,
            "equipment_min":          equipment_min,
        }
        st.success("Parameters applied.")

if "applied_params" in st.session_state:
    st.caption("Parameters active ✓")

st.divider()

# ---------------------------------------------------------------------------
# Route search
# ---------------------------------------------------------------------------
CHAMONIX_BBOX = latlon_bbox_to_mercator(lon_min=6.6, lat_min=45.7, lon_max=7.1, lat_max=46.0)

_GRADE_LABEL = {
    "rock_onsight":   "Rock (onsight)",
    "ice_max":        "Ice",
    "mixed_max":      "Mixed",
    "alpine_max":     "Alpine",
    "engagement_max": "Engagement",
    "risk_max":       "Objective risk",
    "exposition_max": "Exposition",
    "equipment_min":  "Equipment",
}

_ACTIVITIES = ["rock_climbing", "mountain_climbing", "ice_climbing", "snow_ice_mixed"]
_PAGE_SIZE   = 100
_TARGET      = 10   # stop fetching once we have this many ranked matches


def _fetch_until_enough(params: dict) -> None:
    """
    Page through the Camptocamp API (quality-sorted) until we have _TARGET ranked
    matches or there are no more results. Progress is stored in st.session_state
    under "search" so a future "show more" button can resume from where we left off.

    State schema:
        all_fetched   list[dict]   all route stubs fetched so far
        api_offset    int          next offset to request from the API
        api_exhausted bool         True when the API has no more pages
        ranked        list[dict]   all passing routes sorted by score (best first)
        shown         int          how many results are currently displayed
    """
    state = st.session_state["search"]

    with st.spinner("Querying Camptocamp..."):
        while len(state["ranked"]) < _TARGET and not state["api_exhausted"]:
            page, total = search_routes(
                CHAMONIX_BBOX,
                activities=_ACTIVITIES,
                offset=state["api_offset"],
                page_size=_PAGE_SIZE,
            )
            state["all_fetched"].extend(page)
            state["api_offset"] += len(page)
            if state["api_offset"] >= total or len(page) < _PAGE_SIZE:
                state["api_exhausted"] = True

            state["ranked"] = rank_routes(state["all_fetched"], params)


if st.button("Search routes in the Mont Blanc massif"):
    params = st.session_state.get("applied_params", {})
    st.session_state["search"] = {
        "all_fetched":   [],
        "api_offset":    0,
        "api_exhausted": False,
        "ranked":        [],
        "shown":         _TARGET,
    }
    if params:
        _fetch_until_enough(params)

search_state = st.session_state.get("search")
if search_state:
    params  = st.session_state.get("applied_params", {})
    ranked  = search_state["ranked"]
    shown   = search_state["shown"]
    fetched = len(search_state["all_fetched"])

    if params:
        st.subheader(f"{len(ranked)} routes matched (of {fetched} fetched)")
    else:
        st.subheader(f"{fetched} routes found (apply parameters to filter & rank)")

    display = ranked[:shown] if params else search_state["all_fetched"][:shown]

    for route in display:
        location = route.get("title_prefix") or "Unknown location"
        name     = route.get("title")        or "Unnamed route"
        score    = route.get("_score")
        direction = route.get("_direction")
        warnings = route.get("_warnings", [])

        route_id = route.get("document_id")
        url = f"https://www.camptocamp.org/routes/{route_id}" if route_id else None
        link = f"[{location} — {name}]({url})" if url else f"**{location}** — {name}"

        if score is not None:
            colour = match_colour(score, direction)
            dot_tip = f"Overall match: score {score:.1f} ({direction}). Lower is better; 0 = perfect match."
            dot = f'<span style="color:{colour};font-size:1.3em;" title="{dot_tip}">●</span>'

            # One coloured pill per grade field that has a value on this route
            deltas = route.get("_deltas", {})
            pills = []
            for sp_key, api_field, *_ in GRADE_FIELDS:
                val = route.get(api_field)
                if val is None:
                    continue
                delta   = deltas.get(sp_key)
                colour_g = delta_colour(delta)
                label   = _GRADE_LABEL.get(sp_key, sp_key)
                limit   = params.get(sp_key) or "—"
                if delta is None:
                    tip = f"{label}: {val} (not evaluated)"
                elif delta == 0:
                    tip = f"{label}: {val} — matches your limit ({limit})"
                elif delta > 0:
                    tip = f"{label}: {val} — {delta} step(s) above your limit ({limit})"
                else:
                    tip = f"{label}: {val} — {abs(delta)} step(s) below your limit ({limit})"
                pills.append(f'<span style="color:{colour_g}" title="{tip}">{val}</span>')
            grades_html = " &nbsp; ".join(pills)

            if warnings:
                warn_tip = "&#10;".join(warnings)  # newline between each warning
                warn = f' <span title="{warn_tip}">⚠️</span>'
            else:
                warn = ""

            st.markdown(f"{dot} {link} &nbsp; <small>{grades_html}{warn}</small>",
                        unsafe_allow_html=True)
        else:
            st.markdown(link)
