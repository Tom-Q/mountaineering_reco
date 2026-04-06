import streamlit as st
from dotenv import load_dotenv
import folium
from folium.plugins import GeoMan
from branca.element import MacroElement
from jinja2 import Template
from streamlit_folium import st_folium
from collections import Counter

from datetime import date, datetime

from src.camptocamp import latlon_bbox_to_mercator, fetch_outing_stubs, fetch_outing_full, fetch_route, CHAMONIX_BBOX, search_routes_by_name
from src.llm import analyze_route, _select_outing_ids, summarize_route
from src.weather import fetch_weather
from src.search import fetch_page, enrich_routes, rerank
from src.grades import (
    match_colour, match_label, delta_colour, delta_label, GRADE_FIELDS,
    ROCK, ICE, MIXED, ALPINE,
    ENGAGEMENT, ENGAGEMENT_LABELS,
    RISK, RISK_LABELS,
    EXPOSITION, EXPOSITION_LABELS,
    EQUIPMENT, EQUIPMENT_LABELS,
)

# Pin GeoMan to a specific version to avoid slow @latest resolution on unpkg
GeoMan.default_js  = [("leaflet_geoman_js",  "https://unpkg.com/@geoman-io/leaflet-geoman-free@2.19.2/dist/leaflet-geoman.js")]
GeoMan.default_css = [("leaflet_geoman_css", "https://unpkg.com/@geoman-io/leaflet-geoman-free@2.19.2/dist/leaflet-geoman.css")]


class _GeoManBridge(MacroElement):
    """Bridges GeoMan's pm:create/pm:remove events into the draw:created/draw:deleted
    events that streamlit-folium listens for. Also enforces single-rectangle: clears
    drawnItems before adding a newly drawn layer."""
    _template = Template("""
        {% macro script(this, kwargs) %}
        (function bridge() {
            var m = {{ this._parent.get_name() }};
            if (!m || !m.pm || !window.drawnItems) { setTimeout(bridge, 200); return; }
            m.on('pm:create', function(e) {
                window.drawnItems.clearLayers();
                window.drawnItems.addLayer(e.layer);
                m.fire('draw:created', {layer: e.layer, layerType: 'rectangle'});
            });
            m.on('pm:remove', function(e) {
                window.drawnItems.removeLayer(e.layer);
                m.fire('draw:deleted', {layers: {getLayers: function(){ return [e.layer]; }}});
            });
        })();
        {% endmacro %}
    """)

load_dotenv()

st.set_page_config(
    page_title="Mountaineering Route Recommender",
    page_icon="🏔️",
    layout="wide",
)

st.title("🏔️ Mountaineering Route Recommender")
st.caption("Suggests alpine objectives based on your history, current conditions, and weather.")

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
_TARGET      = 5    # number of routes to display


def _fmt_time(hours: float | None) -> str:
    """Format a duration in hours as a compact string."""
    if not hours or hours <= 0:
        return "?"
    if hours < 24:
        h, m = divmod(int(round(hours * 60)), 60)
        return f"{h}h{m:02d}" if m else f"{h}h"
    return f"{int(round(hours / 24))}d"


def _enrich_and_rerank() -> None:
    """Enrich the top _TARGET routes with full data, then re-rank."""
    state = st.session_state["search"]
    to_enrich = [
        r for r in state["ranked"][:_TARGET]
        if r.get("document_id") not in state["enriched_ids"]
    ]
    if not to_enrich:
        return
    with st.spinner(f"Loading full details for {len(to_enrich)} routes..."):
        enrich_routes(state["all_fetched"], to_enrich, state["enriched_ids"])
    state["ranked"] = rerank(
        state["all_fetched"], state["excluded_ids"], state["params"], state["easy_penalty"]
    )


def _fetch_until_enough(params: dict, ep: float) -> None:
    """Page through the Camptocamp API until we have _TARGET ranked matches."""
    state = st.session_state["search"]
    bbox = st.session_state["bbox"]
    with st.spinner("Querying Camptocamp..."):
        while len(state["ranked"]) < _TARGET and not state["api_exhausted"]:
            page, total = fetch_page(bbox, _ACTIVITIES, state["api_offset"], _PAGE_SIZE)
            state["all_fetched"].extend(page)
            state["api_offset"] += len(page)
            if state["api_offset"] >= total or len(page) < _PAGE_SIZE:
                state["api_exhausted"] = True
            state["ranked"] = rerank(
                state["all_fetched"], state["excluded_ids"], params, ep
            )

    _enrich_and_rerank()


TIME_VALUES = [2, 3, 4, 5, 6, 8, 10, 12, 18, 24, 48, 72]
TIME_LABELS = {2: "< 3h", 3: "3h", 4: "4h", 5: "5h", 6: "6h",
               8: "8h", 10: "10h", 12: "12h", 18: "18h", 24: "1 day",
               48: "2 days", 72: "3 days+"}
SPEED_OPTIONS = [0.5, 0.67, 0.8, 1.0, 1.25, 1.5, 2.0]
SPEED_LABELS  = {0.5: "2× faster", 0.67: "1.5× faster", 0.8: "1.2× faster",
                 1.0: "on par", 1.25: "1.25× slower", 1.5: "1.5× slower", 2.0: "2× slower"}


if "bbox" not in st.session_state:
    st.session_state["bbox"] = None

# ---------------------------------------------------------------------------
# Sidebar: user profile (grades, fitness, risk)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### Your profile")

    # --- Skill ---------------------------------------------------------------
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

    # --- Fitness -------------------------------------------------------------
    st.markdown("**Fitness**")
    hiking_vert_max = st.number_input(
        "Max approach/descent vert (m)",
        min_value=0, max_value=4000, value=1500, step=100,
        help="Non-technical terrain only (approach + descent).",
    )
    pace = st.select_slider(
        "Pace vs. C2C estimates",
        options=SPEED_OPTIONS, value=1.0,
        format_func=lambda v: SPEED_LABELS[v],
        help="Your speed relative to Camptocamp's time estimates. Applied to the displayed moving time and used for scoring.",
    )

    # --- Risk ----------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab1, tab2 = st.tabs(["Find routes", "Analyse a route"])

# ===========================================================================
# TAB 1 — Find routes: map + search + triage cards
# ===========================================================================
with tab1:
    col_map, col_search = st.columns([3, 1])

    with col_map:
        st.markdown("**Search area**")
        if st.session_state.get("bbox"):
            st.caption("Draw a new rectangle to change the search area.")
        else:
            st.caption("Draw a rectangle on the map, or use the default area below.")

        _m = folium.Map(
            location=[45.85, 6.87],
            zoom_start=8,
            tiles="https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
            attr=(
                'Map data: &copy; <a href="https://openstreetmap.org">OpenStreetMap</a> contributors, '
                '<a href="http://viewfinderpanoramas.org">SRTM</a> | Map style: &copy; '
                '<a href="https://opentopomap.org">OpenTopoMap</a>'
            ),
        )
        if st.session_state.get("bbox") == CHAMONIX_BBOX:
            folium.Rectangle(bounds=[[45.7, 6.6], [46.0, 7.1]], fill=True, fill_opacity=0.2).add_to(_m)
        GeoMan(
            position="topleft",
            drawRectangle=True,
            drawMarker=False, drawCircleMarker=False, drawPolyline=False,
            drawPolygon=False, drawCircle=False, drawText=False,
            editMode=False, dragMode=False, cutPolygon=False, rotateMode=False,
            removalMode=True,
        ).add_to(_m)
        _GeoManBridge().add_to(_m)
        # Key switches when Chamonix bbox is active, forcing a map remount with the
        # pre-drawn rectangle.
        _map_key = "area_map_chamonix" if st.session_state.get("bbox") == CHAMONIX_BBOX else "area_map"
        _prev_bbox = st.session_state.get("bbox")
        _map_result = st_folium(_m, key=_map_key, use_container_width=True, height=500,
                                returned_objects=["all_drawings"])
        _drawings = (_map_result or {}).get("all_drawings")
        if _drawings is not None:
            # all_drawings is None on reruns with no map interaction — don't touch bbox.
            # [] means user deleted all drawings; a list means a shape was drawn.
            if _drawings:
                _coords = _drawings[-1]["geometry"]["coordinates"][0]
                _lons = [c[0] for c in _coords]
                _lats = [c[1] for c in _coords]
                st.session_state["bbox"] = latlon_bbox_to_mercator(
                    min(_lons), min(_lats), max(_lons), max(_lats)
                )
            else:
                st.session_state["bbox"] = None
        if st.session_state.get("bbox") != _prev_bbox:
            st.rerun()

        if st.button("Use Mont Blanc massif (default)", use_container_width=True):
            if st.session_state.get("bbox") != CHAMONIX_BBOX:
                st.session_state["bbox"] = CHAMONIX_BBOX
                st.rerun()
        if st.session_state.get("bbox") == CHAMONIX_BBOX:
            st.caption("Active area: Mont Blanc massif")

    with col_search:
        st.markdown("**Search filters**")
        moving_time = st.select_slider(
            "Moving time (min – max)",
            options=TIME_VALUES, value=(3, 12),
            format_func=lambda v: TIME_LABELS[v],
            help="Total moving time on the route.",
        )
        difficulties_vert = st.slider(
            "Technical vert (min – max, m)",
            min_value=0, max_value=1500, value=(100, 600), step=50,
            help="Vertical extent of sustained technical difficulties.",
        )
        easy_penalty = st.slider(
            "Penalise routes below my limit",
            min_value=0.0, max_value=1.0, value=0.0, step=0.25,
            format="%.2f",
            help="Does not affect routes that are too hard. "
                 "Off: easy routes rank the same as routes at your limit. "
                 "On: routes well below your limit are pushed down in results.",
        )
        if st.button(
            "Search routes in selected area",
            disabled=not st.session_state.get("bbox"),
            use_container_width=True,
        ):
            params = {
                # Skill
                "rock_onsight":           rock_onsight,
                "rock_redpoint":          rock_redpoint,
                "rock_trad":              None if rock_trad == "N/A" else rock_trad,
                "ice_max":                None if ice_max   == "—"   else ice_max,
                "mixed_max":              None if mixed_max == "—"   else mixed_max,
                "alpine_max":             alpine_max,
                "alpine_routes_count":    alpine_routes_count,
                # Fitness
                "hiking_vert_max":        hiking_vert_max,
                "difficulties_vert_min":  difficulties_vert[0],
                "difficulties_vert_max":  difficulties_vert[1],
                "moving_time_min":        moving_time[0],
                "moving_time_max":        moving_time[1],
                "pace":                   pace,
                # Risk
                "engagement_max":         engagement_max,
                "risk_max":               risk_max,
                "exposition_max":         exposition_max,
                "equipment_min":          equipment_min,
            }
            st.session_state["applied_params"] = params
            st.session_state["search"] = {
                "all_fetched":   [],
                "api_offset":    0,
                "api_exhausted": False,
                "ranked":        [],
                "shown":         _TARGET,
                "enriched_ids":  set(),
                "params":        params,
                "easy_penalty":  easy_penalty,
                "open_analyses": set(),
                "excluded_ids":  set(),
                "summaries":     {},
            }
            _fetch_until_enough(params, easy_penalty)

    st.divider()

    search_state = st.session_state.get("search")

    if search_state:
        params  = st.session_state.get("applied_params", {})
        ranked  = search_state["ranked"]
        shown   = search_state["shown"]
        fetched = len(search_state["all_fetched"])

        if params:
            st.subheader(f"Top {min(shown, len(ranked))} routes (of {len(ranked)} matched, {fetched} fetched)")
        else:
            st.subheader(f"{fetched} routes found")

        display = ranked[:shown] if params else search_state["all_fetched"][:shown]

        st.warning(
            "This tool may give inaccurate or dangerous advice. "
            "Always read the actual source topos in full before committing to any route.",
            icon="⚠️",
        )

        for route in display:
            location = route.get("title_prefix") or "Unknown location"
            name     = route.get("title")        or "Unnamed route"
            score    = route.get("_score")
            direction = route.get("_direction")
            warnings = route.get("_warnings", [])

            route_id = route.get("document_id")
            url = f"https://www.camptocamp.org/routes/{route_id}" if route_id else None
            link = f"[{location} — {name}]({url})" if url else f"**{location}** — {name}"

            col_main, col_analyse, col_remove = st.columns([12, 1, 1])

            with col_main:
                if score is not None:
                    colour = match_colour(score, direction)
                    dot_tip = f"Score {score:.1f} — {match_label(score, direction)}. Lower is better; 0 = perfect match."
                    dot = f'<span style="color:{colour};font-size:1.3em;" title="{dot_tip}">●</span>'

                    # One coloured pill per grade field that has a value on this route
                    deltas = route.get("_deltas", {})
                    pills = []
                    for sp_key, api_field, *_ in GRADE_FIELDS:
                        val = route.get(api_field)
                        if val is None:
                            continue
                        delta    = deltas.get(sp_key)
                        colour_g = delta_colour(delta)
                        label    = _GRADE_LABEL.get(sp_key, sp_key)
                        limit    = params.get(sp_key) or "—"
                        tip = f"{label}: {val} — {delta_label(delta)} (your limit: {limit})"
                        pills.append(f'<span style="color:{colour_g}" title="{tip}">{val}</span>')
                    grades_html = " &nbsp; ".join(pills)

                    # Fitness pills: time, approach vert, difficulties vert
                    fit_pills = []

                    duration_h = (route.get("calculated_duration") or 0) * 24
                    if duration_h > 0:
                        adjusted_pace = params.get("pace", 1.0)
                        adjusted_h = duration_h * adjusted_pace
                        d_time = deltas.get("moving_time", 0)
                        t_min = params.get("moving_time_min")
                        t_max = params.get("moving_time_max")
                        rng = f"{_fmt_time(t_min)}–{_fmt_time(t_max)}" if t_min and t_max else "—"
                        tip_f = f"Moving time: {_fmt_time(adjusted_h)} — {delta_label(d_time)} (your range: {rng})"
                        fit_pills.append(f'<span style="color:{delta_colour(d_time)}" title="{tip_f}">{_fmt_time(adjusted_h)}</span>')

                    approach = route.get("height_diff_access")
                    if approach is not None:
                        d_vert = deltas.get("hiking_vert", 0)
                        v_max = params.get("hiking_vert_max")
                        lim = f"{int(v_max)}m" if v_max else "—"
                        tip_f = f"Approach vert: {int(approach)}m — {delta_label(d_vert)} (your max: {lim})"
                        fit_pills.append(f'<span style="color:{delta_colour(d_vert)}" title="{tip_f}">{int(approach)}m↑</span>')

                    diff_vert = route.get("height_diff_difficulties")
                    if diff_vert is not None:
                        d_diff = deltas.get("difficulties_vert", 0)
                        v_min = params.get("difficulties_vert_min")
                        v_max = params.get("difficulties_vert_max")
                        rng = f"{int(v_min)}–{int(v_max)}m" if v_min is not None and v_max is not None else "—"
                        tip_f = f"Technical vert: {int(diff_vert)}m — {delta_label(d_diff)} (your range: {rng})"
                        fit_pills.append(f'<span style="color:{delta_colour(d_diff)}" title="{tip_f}">{int(diff_vert)}m⬦</span>')

                    elev_max = route.get("elevation_max")
                    if elev_max is not None:
                        fit_pills.append(f'<span title="Summit altitude">{int(elev_max)}m▲</span>')

                    fitness_html = (" &thinsp;·&thinsp; ".join(fit_pills)) if fit_pills else ""
                    sep = " &nbsp;|&nbsp; " if grades_html and fitness_html else ""

                    if warnings:
                        warn_tip = "&#10;".join(warnings)
                        warn = f' <span title="{warn_tip}">⚠️</span>'
                    else:
                        warn = ""

                    st.markdown(
                        f"{dot} {link} &nbsp; <small>{grades_html}{sep}{fitness_html}{warn}</small>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(link)

            with col_analyse:
                if route_id:
                    is_open = route_id in search_state.get("open_analyses", set())
                    if st.button("Close" if is_open else "Analyse", key=f"analyse_{route_id}"):
                        if is_open:
                            search_state["open_analyses"].discard(route_id)
                        else:
                            search_state["open_analyses"].add(route_id)
                        st.rerun()

            with col_remove:
                if route_id:
                    if st.button("✕", key=f"remove_{route_id}", help="Remove this route"):
                        search_state["excluded_ids"].add(route_id)
                        search_state["open_analyses"].discard(route_id)
                        search_state["ranked"] = rerank(
                            search_state["all_fetched"], search_state["excluded_ids"],
                            search_state["params"], search_state["easy_penalty"],
                        )
                        _enrich_and_rerank()
                        st.rerun()

            if route_id and route_id in search_state.get("open_analyses", set()):
                with st.container(border=True):
                    # --- Fetch and cache outing stubs ---
                    stubs_cache = search_state.setdefault("stubs", {})
                    if route_id not in stubs_cache:
                        with st.spinner("Loading trip reports..."):
                            stubs_cache[route_id] = fetch_outing_stubs(route_id, limit=200)
                    route_stubs = stubs_cache[route_id]

                    # --- Compute stats from stubs ---
                    _MONTH_ABBR = ["Jan","Feb","Mar","Apr","May","Jun",
                                   "Jul","Aug","Sep","Oct","Nov","Dec"]
                    dated_stubs: list[tuple[dict, date]] = []
                    for s in route_stubs:
                        raw = s.get("date_start")
                        if raw:
                            try:
                                dated_stubs.append((s, datetime.strptime(raw, "%Y-%m-%d").date()))
                            except ValueError:
                                pass
                    dated_stubs.sort(key=lambda x: x[1], reverse=True)

                    total_count = len(route_stubs)
                    last_date   = dated_stubs[0][1] if dated_stubs else None
                    if last_date:
                        days_ago = (date.today() - last_date).days
                        if days_ago < 14:
                            staleness = f"{days_ago}d ago"
                        elif days_ago < 60:
                            staleness = f"{days_ago // 7}w ago"
                        else:
                            staleness = f"{days_ago // 30}mo ago"
                    else:
                        staleness = "no reports"

                    month_counts = Counter(d.month for _, d in dated_stubs)
                    top_months   = [_MONTH_ABBR[m - 1] for m, _ in month_counts.most_common(3)]

                    _RATING_COLOUR = {
                        "excellent": "#2e7d32", "good": "#2e7d32",
                        "average": "#e65100",
                        "bad": "#c62828", "awful": "#c62828",
                    }
                    rating_dots = " ".join(
                        f'<span style="color:{_RATING_COLOUR.get(s.get("condition_rating",""),"#888")}'
                        f';font-size:1.1em;" title="{s.get("condition_rating") or "unknown"}">●</span>'
                        for s, _ in dated_stubs[:5]
                    )

                    # --- Stats row ---
                    c2c_card_url = f"https://www.camptocamp.org/routes/{route_id}"
                    months_str   = " · ".join(top_months) if top_months else "—"
                    ratings_str  = rating_dots if rating_dots else "—"
                    st.markdown(
                        f"**{total_count}** reports &nbsp;·&nbsp; last: **{staleness}**"
                        f" &nbsp;·&nbsp; best months: **{months_str}**"
                        f" &nbsp;·&nbsp; recent: {ratings_str}"
                        f" &nbsp;·&nbsp; [C2C ↗]({c2c_card_url})",
                        unsafe_allow_html=True,
                    )

                    # --- One-sentence summary (on demand) ---
                    summaries   = search_state.setdefault("summaries", {})
                    summary_key = ("1sentence", route_id)
                    col_s, col_f = st.columns(2)
                    with col_s:
                        if summary_key not in summaries:
                            if st.button("Conditions summary ▶", key=f"summarize_{route_id}"):
                                with st.spinner("Summarising conditions..."):
                                    recent_full = []
                                    for s, _ in dated_stubs[:3]:
                                        try:
                                            recent_full.append(fetch_outing_full(s["document_id"]))
                                        except Exception:
                                            pass
                                    summaries[summary_key] = summarize_route(route, recent_full)
                                st.rerun()
                        else:
                            st.markdown(f"*{summaries[summary_key]}*")
                    with col_f:
                        if st.button("Full analysis →", key=f"full_analysis_{route_id}"):
                            search_state["analysis_target"] = route
                            st.info("Route loaded — switch to the **Analyse a route** tab for the full analysis.")

# ===========================================================================
# TAB 2 — Analyse a route: name search or pre-loaded, then full analysis
# ===========================================================================
with tab2:
    # --- Route selection ---
    target = st.session_state.get("search", {}).get("analysis_target") if st.session_state.get("search") else None
    tab2_route = st.session_state.get("tab2_route")

    if target and tab2_route is None:
        rid   = target.get("document_id")
        rname = target.get("title") or "Unknown route"
        rarea = target.get("title_prefix") or ""
        st.info(f"Route loaded from recommendations: **{rarea} — {rname}**")
        col_load, col_clear = st.columns([2, 1])
        with col_load:
            if st.button("Analyse this route", use_container_width=True):
                st.session_state["tab2_route"] = target
                tab2_route = target
                st.rerun()
        with col_clear:
            if st.button("Clear", use_container_width=True):
                st.session_state["search"]["analysis_target"] = None
                st.rerun()

    # Name search (always visible when no route is loaded for analysis)
    if tab2_route is None:
        st.markdown("**Search for a route by name**")
        query = st.text_input("Route name", placeholder="e.g. Frendo spur, Gervasutti pillar…", label_visibility="collapsed")
        if query:
            with st.spinner("Searching Camptocamp..."):
                name_results = search_routes_by_name(query, limit=15)
            if not name_results:
                st.warning("No routes found. Try a different name or spelling.")
            else:
                for r in name_results:
                    rid   = r.get("document_id")
                    rname = r.get("title") or "Unnamed"
                    rarea = r.get("title_prefix") or ""
                    grade = r.get("global_rating") or ""
                    acts  = ", ".join(r.get("activities") or [])
                    label = f"{rarea} — {rname}" if rarea else rname
                    sublabel = "  ·  ".join(filter(None, [grade, acts]))
                    col_r, col_btn = st.columns([6, 1])
                    with col_r:
                        st.markdown(f"**{label}**" + (f"  <small>{sublabel}</small>" if sublabel else ""),
                                    unsafe_allow_html=True)
                    with col_btn:
                        if st.button("Select", key=f"select_{rid}"):
                            with st.spinner("Loading route details..."):
                                st.session_state["tab2_route"] = fetch_route(rid)
                            st.rerun()

    # --- Full analysis ---
    if tab2_route is not None:
        rid   = tab2_route.get("document_id")
        rname = tab2_route.get("title") or "Unknown route"
        rarea = tab2_route.get("title_prefix") or ""
        grade = tab2_route.get("global_rating") or ""
        c2c_url = f"https://www.camptocamp.org/routes/{rid}" if rid else None

        st.subheader(f"{rarea} — {rname}" if rarea else rname)
        if grade or c2c_url:
            meta = "  ·  ".join(filter(None, [grade, f"[C2C ↗]({c2c_url})" if c2c_url else None]))
            st.markdown(meta)

        weather_check2 = st.checkbox(
            "Planning to go in the next few days — include weather check",
            value=False,
            key="weather_check_tab2",
        )

        col_analyse_btn, col_change = st.columns([2, 1])
        with col_analyse_btn:
            do_analyse = st.button("Analyse", use_container_width=True, key="tab2_analyse_btn")
        with col_change:
            if st.button("Choose a different route", use_container_width=True, key="tab2_change"):
                st.session_state.pop("tab2_route", None)
                if st.session_state.get("search"):
                    st.session_state["search"]["analysis_target"] = None
                st.rerun()

        analyses = st.session_state.setdefault("tab2_analyses", {})
        cache_key = (rid, weather_check2)

        if do_analyse and cache_key not in analyses:
            with st.spinner("Fetching trip reports and analysing route..."):
                stubs2 = fetch_outing_stubs(rid, limit=200)
                selected_ids2 = _select_outing_ids(stubs2, date.today())
                full_outings2 = []
                for oid in selected_ids2:
                    try:
                        full_outings2.append(fetch_outing_full(oid))
                    except Exception:
                        pass
                weather2 = None
                if weather_check2:
                    with st.spinner("Fetching weather data..."):
                        weather2 = fetch_weather(tab2_route, date.today())
                    if weather2 is None:
                        st.warning("Weather unavailable: no coordinates found for this route.")
                user_params2 = {
                    "rock_onsight":        rock_onsight,
                    "rock_redpoint":       rock_redpoint,
                    "rock_trad":           None if rock_trad == "N/A" else rock_trad,
                    "ice_max":             None if ice_max   == "—"   else ice_max,
                    "mixed_max":           None if mixed_max == "—"   else mixed_max,
                    "alpine_max":          alpine_max,
                    "alpine_routes_count": alpine_routes_count,
                }
                analyses[cache_key] = {
                    "text":    analyze_route(tab2_route, stubs2, full_outings2, user_params2, date.today(), weather=weather2),
                    "weather": weather2,
                }
            st.rerun()

        if cache_key in analyses:
            result = analyses[cache_key]
            wx = result["weather"]
            if wx and wx.ui_table:
                elev_str = f"  ·  summit {int(tab2_route['elevation_max'])}m" if tab2_route.get("elevation_max") else ""
                with st.expander("Raw weather data", expanded=False):
                    st.caption(f"Open-Meteo forecast for {wx.coords[0]:.2f}N, {wx.coords[1]:.2f}E{elev_str}  ·  fetched {wx.fetch_date}")
                    st.markdown(wx.ui_table)
                    if wx.historical_text:
                        st.caption(wx.historical_text)
                    if wx.fetch_errors:
                        st.warning("  \n".join(wx.fetch_errors))
            st.markdown(result["text"])
            st.caption(
                "Source topos and trip reports linked above are the authoritative references. "
                "This AI analysis may be incomplete or wrong — verify conditions independently before your climb."
            )
