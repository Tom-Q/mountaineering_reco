import re
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
from src.avalanche import DANGER_LABELS
from src.analysis import analyze_route, summarize_route
from src.chat import chat_alpinist
from src.weather import fetch_weather
from src.search import fetch_page, enrich_routes, rerank, _select_outing_ids
from src.grades import (
    match_colour, match_label, delta_colour, delta_label, GRADE_FIELDS,
    ROCK, ICE, MIXED, ALPINE,
    ENGAGEMENT, ENGAGEMENT_LABELS,
    RISK, RISK_LABELS,
    EXPOSITION, EXPOSITION_LABELS,
    EQUIPMENT, EQUIPMENT_LABELS,
)

def _tool_status_label(name: str, tool_input: dict) -> str:
    """Human-readable label for a tool call status indicator."""
    if name == "get_weather_forecast":
        lat = tool_input.get("latitude", "?")
        lon = tool_input.get("longitude", "?")
        elev = tool_input.get("elevation_m")
        elev_str = f" ({elev}m)" if elev else ""
        try:
            return f"Fetching weather for {lat:.2f}°N, {lon:.2f}°E{elev_str}"
        except (TypeError, ValueError):
            return "Fetching weather..."
    if name == "get_avalanche_bulletin":
        lat = tool_input.get("latitude", "?")
        lon = tool_input.get("longitude", "?")
        try:
            return f"Fetching avalanche bulletin for {lat:.2f}°N, {lon:.2f}°E"
        except (TypeError, ValueError):
            return "Fetching avalanche bulletin..."
    if name == "search_routes_by_name":
        query = tool_input.get("query", "")
        return f"Searching Camptocamp for \"{query}\""
    if name == "search_routes_by_area":
        return "Searching Camptocamp routes in area"
    if name == "fetch_route":
        return f"Fetching route #{tool_input.get('route_id')}"
    if name == "get_outing_list":
        return f"Fetching trip report list for route #{tool_input.get('route_id')}"
    if name == "get_outing_detail":
        return f"Fetching trip report #{tool_input.get('outing_id')}"
    return f"Calling {name}..."


def _render_chat_images(text: str, attached: list | None = None) -> None:
    """Render images for a chat message bubble.
    - attached: list of bytes/PIL/URL strings from tool calls (Phase 2)
    - text: scanned for markdown ![alt](url) syntax embedded by Claude
    Images appear below the text in the same chat bubble.
    st.image() accepts URLs, bytes, PIL images, and numpy arrays.
    """
    images = list(attached or [])
    images.extend(re.findall(r'!\[.*?\]\((https?://[^\)]+)\)', text))
    for img in images:
        try:
            st.image(img)
        except Exception:
            pass


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

st.markdown("""<style>
section[data-testid="stMainBlockContainer"] { padding-top: 0 !important; }
.stMainBlockContainer { padding-top: 0 !important; }
div[data-testid="stAppViewBlockContainer"] { padding-top: 0 !important; }
div[data-testid="stSidebarHeader"] { display: none; }
header[data-testid="stHeader"] { display: none; }
.stTabs [data-baseweb="tab-list"] [data-testid="stMarkdownContainer"] p { font-size: 1.4rem !important; font-weight: 600; }
section[data-testid="stSidebar"] { min-width: 350px !important; max-width: 350px !important; }
</style>""", unsafe_allow_html=True)

GRADE_LABEL = {
    "rock_onsight":   "Rock (onsight)",
    "ice_max":        "Ice",
    "mixed_max":      "Mixed",
    "alpine_max":     "Alpine",
    "engagement_max": "Engagement",
    "risk_max":       "Objective risk",
    "exposition_max": "Exposition",
    "equipment_min":  "Equipment",
}

DANGER_COLORS = {1: "green", 2: "blue", 3: "orange", 4: "red", 5: "red"}

ACTIVITIES = ["rock_climbing", "mountain_climbing", "ice_climbing", "snow_ice_mixed"]
PAGE_SIZE  = 100
TARGET     = 5    # number of routes to display

MONTH_ABBR = ["Jan","Feb","Mar","Apr","May","Jun",
               "Jul","Aug","Sep","Oct","Nov","Dec"]


def _fmt_time(hours: float | None) -> str:
    """Format a duration in hours as a compact string."""
    if not hours or hours <= 0:
        return "?"
    if hours < 24:
        h, m = divmod(int(round(hours * 60)), 60)
        return f"{h}h{m:02d}" if m else f"{h}h"
    return f"{int(round(hours / 24))}d"


def _build_user_params(rock_onsight, rock_trad, ice_max, mixed_max, alpine_max) -> dict:
    return {
        "rock_onsight": rock_onsight,
        "rock_trad":    None if rock_trad == "N/A" else rock_trad,
        "ice_max":      None if ice_max   == "—"   else ice_max,
        "mixed_max":    None if mixed_max == "—"   else mixed_max,
        "alpine_max":   alpine_max,
    }


def _enrich_and_rerank() -> None:
    """Enrich the top TARGET routes with full data, then re-rank."""
    state = st.session_state["search"]
    to_enrich = [
        r for r in state["ranked"][:TARGET]
        if r.get("document_id") not in state["enriched_ids"]
    ]
    if not to_enrich:
        return
    with st.spinner(f"Loading full details for {len(to_enrich)} routes..."):
        enrich_routes(state["all_fetched"], to_enrich, state["enriched_ids"])
    state["ranked"] = rerank(
        state["all_fetched"], state["excluded_ids"], state["params"], state["easy_penalty"]
    )


def _prefetch_summaries() -> None:
    """
    For each of the top TARGET routes:
    - Fetch outing stubs (limit=50) for the stats row (report count, last date, peak months)
    - Generate a one-sentence guidebook description via the LLM using topo fields

    Stubs and summaries are cached so re-renders don't re-fetch.
    No full outing text is fetched here — that happens only in Tab 2.
    """
    state = st.session_state["search"]
    stubs_cache = state.setdefault("stubs", {})
    summaries   = state.setdefault("summaries", {})

    routes_needed = [
        r for r in state["ranked"][:TARGET]
        if r.get("document_id") not in summaries
    ]
    if not routes_needed:
        return

    with st.spinner(f"Loading summaries for {len(routes_needed)} routes..."):
        for route in routes_needed:
            rid = route.get("document_id")
            if rid is None:
                continue
            if rid not in stubs_cache:
                stubs_cache[rid] = fetch_outing_stubs(rid, limit=50)
            report_count = len(stubs_cache[rid])
            summaries[rid] = summarize_route(route, report_count)


def _fetch_until_enough(params: dict, ep: float) -> None:
    """Page through the Camptocamp API until we have TARGET ranked matches."""
    state = st.session_state["search"]
    bbox = st.session_state["bbox"]
    with st.spinner("Querying Camptocamp..."):
        while len(state["ranked"]) < TARGET and not state["api_exhausted"]:
            page, total = fetch_page(bbox, ACTIVITIES, state["api_offset"], PAGE_SIZE)
            state["all_fetched"].extend(page)
            state["api_offset"] += len(page)
            if state["api_offset"] >= total or len(page) < PAGE_SIZE:
                state["api_exhausted"] = True
            state["ranked"] = rerank(
                state["all_fetched"], state["excluded_ids"], params, ep
            )

    _enrich_and_rerank()
    _prefetch_summaries()


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

    s1, s2 = st.columns(2)
    rock_onsight = s1.selectbox("Onsight", ROCK, index=ROCK.index("6a+"),
        help="Hardest sport grade you can lead first-try, no falls, no beta.")
    rock_trad = s2.selectbox("Trad", ["N/A"] + ROCK, index=1 + ROCK.index("6a+"),
        help="Hardest grade you can lead on gear, first try.")

    s1, s2 = st.columns(2)
    ice_max = s1.selectbox("Ice", ["—"] + ICE, index=1 + ICE.index("WI3"))
    mixed_max = s2.selectbox("Mixed", ["—"] + MIXED, index=0)

    alpine_max = st.selectbox("Alpine", ALPINE, index=ALPINE.index("TD+"),
        help="Hardest overall alpine grade completed in reasonable conditions.")

    pace = st.select_slider(
        "Pace vs. C2C estimates",
        options=SPEED_OPTIONS, value=1.0,
        format_func=lambda v: SPEED_LABELS[v],
        help="Your speed relative to Camptocamp's time estimates. Applied to the displayed moving time and used for scoring.",
    )

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
if "chat_history" not in st.session_state:
    st.session_state["chat_history"] = []
if "api_messages" not in st.session_state:
    # Initialise from chat_history (plain string content is API-compatible)
    st.session_state["api_messages"] = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state["chat_history"]
    ]

tab1, tab2, tab3, tab4 = st.tabs(["Chat with AI", "Find routes", "Analyse a route", "About"])

# ===========================================================================
# TAB 1 — Chat with AI
# ===========================================================================
with tab1:
    st.warning(
        "This assistant may give inaccurate or dangerous advice. "
        "Always verify conditions and route information from authoritative sources "
        "before committing to any mountain objective.",
        icon="⚠️",
    )

    # Declare the messages container first so it occupies space above the input.
    # New messages are rendered into this container, keeping the input pinned below.
    messages = st.container()
    user_input = st.chat_input("Ask anything about alpine routes, gear, or conditions...")

    with messages:
        for msg in st.session_state["chat_history"]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                _render_chat_images(msg["content"], msg.get("images"))

        if user_input:
            st.session_state["chat_history"].append({"role": "user", "content": user_input})
            st.session_state["api_messages"].append({"role": "user", "content": user_input})
            with st.chat_message("user"):
                st.markdown(user_input)

            reply = ""
            with st.chat_message("assistant"):
                try:
                    text_placeholder = st.empty()
                    accumulated = ""
                    current_status = None
                    current_status_label = None

                    for event in chat_alpinist(
                        st.session_state["api_messages"],
                        date.today(),
                        user_params=_build_user_params(rock_onsight, rock_trad, ice_max, mixed_max, alpine_max),
                    ):
                        if event["type"] == "text":
                            accumulated += event["text"]
                            text_placeholder.markdown(accumulated + "▌")

                        elif event["type"] == "tool_start":
                            current_status_label = _tool_status_label(event["name"], event["input"])
                            current_status = st.status(current_status_label + "...", expanded=False)

                        elif event["type"] == "tool_end":
                            if current_status is not None:
                                if event["error"]:
                                    current_status.update(
                                        label=f"⚠ {current_status_label} — failed",
                                        state="error",
                                    )
                                else:
                                    current_status.update(
                                        label=f"✓ {current_status_label}",
                                        state="complete",
                                    )
                                current_status = None
                                current_status_label = None

                        elif event["type"] == "done":
                            text_placeholder.markdown(accumulated)
                            st.session_state["api_messages"].extend(event["new_api_messages"])
                            reply = accumulated

                    _render_chat_images(reply)
                except Exception as e:
                    reply = f"Sorry, I couldn't reach the assistant ({e}). Please try again."
                    st.markdown(reply)

            st.session_state["chat_history"].append(
                {"role": "assistant", "content": reply, "images": []}
            )

    if st.session_state["chat_history"]:
        if st.button("Clear conversation", key="chat_clear"):
            st.session_state["chat_history"] = []
            st.session_state["api_messages"] = []
            st.rerun()

# ===========================================================================
# TAB 2 — Find routes: map + search + triage cards
# ===========================================================================
with tab2:
    col_results, col_map = st.columns([3, 2])

    with col_map:
        if st.session_state.get("bbox"):
            st.caption("Draw a new rectangle to change the search area.")
        else:
            st.caption("Draw a rectangle on the map to select a search area.")

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
        _map_result = st_folium(_m, key=_map_key, use_container_width=True, height=450,
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
        moving_time = st.select_slider(
            "Moving time (min – max)",
            options=TIME_VALUES, value=(3, 12),
            format_func=lambda v: TIME_LABELS[v],
            help="Total moving time on the route.",
        )
        _c = st.columns(2)
        difficulties_vert = _c[0].slider(
            "Tech vert (m)",
            min_value=0, max_value=1500, value=(100, 600), step=50,
            help="Vertical extent of sustained technical difficulties.",
        )
        hiking_vert_max = _c[1].slider(
            "Approach vert (m)",
            min_value=0, max_value=4000, value=1500, step=100,
            help="Non-technical terrain only (approach + descent).",
        )
        easy_penalty = st.slider(
            "Penalise routes below my limit",
            min_value=0.0, max_value=1.0, value=0.0, step=0.25,
            format="%.2f",
            help="Does not affect routes that are too hard. "
                 "Off: easy routes rank the same as routes at your limit. "
                 "On: routes well below your limit are pushed down in results.",
        )

    with col_results:
        if st.button(
            "Search routes in selected area",
            disabled=not st.session_state.get("bbox"),
            use_container_width=True,
        ):
            params = {
                # Skill
                "rock_onsight":           rock_onsight,
                "rock_trad":              None if rock_trad == "N/A" else rock_trad,
                "ice_max":                None if ice_max   == "—"   else ice_max,
                "mixed_max":              None if mixed_max == "—"   else mixed_max,
                "alpine_max":             alpine_max,
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
                "shown":         TARGET,
                "enriched_ids":  set(),
                "params":        params,
                "easy_penalty":  easy_penalty,
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

                # --- Build stats + summary strings (merged into title line) ---
                _stubs_cache = search_state.setdefault("stubs", {})
                _route_stubs = _stubs_cache.get(route_id, []) if route_id else []
                _stats_html = ""
                if route_id in _stubs_cache and not _route_stubs:
                    _stats_html = "<small style='color:#888'>0 reports</small>"
                elif _route_stubs:
                    _dated: list[tuple[dict, date]] = []
                    for _s in _route_stubs:
                        _raw = _s.get("date_start")
                        if _raw:
                            try:
                                _dated.append((_s, datetime.strptime(_raw, "%Y-%m-%d").date()))
                            except ValueError:
                                print(f"[app] Could not parse trip report date: {_raw!r}")
                    _dated.sort(key=lambda x: x[1], reverse=True)
                    _total = len(_route_stubs)
                    _last  = _dated[0][1] if _dated else None
                    if _last:
                        _days = (date.today() - _last).days
                        _staleness = f"{_days}d ago" if _days < 14 else (f"{_days // 7}w ago" if _days < 60 else f"{_days // 30}mo ago")
                    else:
                        _staleness = "no reports"
                    _mcounts  = Counter(_d.month for _, _d in _dated)
                    _months   = " · ".join(MONTH_ABBR[_m - 1] for _m, _ in _mcounts.most_common(3))
                    _stats_html = (
                        f"<small style='color:#888'>"
                        f"{_total} reports &thinsp;·&thinsp; last {_staleness}"
                        + (f" &thinsp;·&thinsp; peak {_months}" if _months else "")
                        + f"</small>"
                    )
                _summaries = search_state.setdefault("summaries", {})

                col_main, col_analyse, col_remove = st.columns([10, 2, 1])

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
                            label    = GRADE_LABEL.get(sp_key, sp_key)
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

                        _stats_sep = " &thinsp;·&thinsp; " if (fitness_html or grades_html) and _stats_html else ""
                        st.markdown(
                            f"{dot} {link} &nbsp; <small>{grades_html}{sep}{fitness_html}{warn}{_stats_sep}{_stats_html}</small>"
                            + (f"<br><small><em>{_summaries[route_id]}</em></small>" if route_id and route_id in _summaries else ""),
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            link
                            + (f" &nbsp; <small>{_stats_html}</small>" if _stats_html else "")
                            + (f"<br><small><em>{_summaries[route_id]}</em></small>" if route_id and route_id in _summaries else ""),
                            unsafe_allow_html=True,
                        )

                with col_analyse:
                    if route_id:
                        if st.button("Full analysis →", key=f"full_analysis_{route_id}",
                                     help="Open in Analyse a route tab"):
                            search_state["analysis_target"] = route
                            st.rerun()

                with col_remove:
                    if route_id:
                        if st.button("✕", key=f"remove_{route_id}", help="Remove this route"):
                            search_state["excluded_ids"].add(route_id)
                            search_state["ranked"] = rerank(
                                search_state["all_fetched"], search_state["excluded_ids"],
                                search_state["params"], search_state["easy_penalty"],
                            )
                            _enrich_and_rerank()
                            _prefetch_summaries()
                            st.rerun()


# ===========================================================================
# TAB 3 — Analyse a route: select from Tab 2 results or search by name
# ===========================================================================
with tab3:
    weather_check2 = st.checkbox(
        "Planning to go in the next few days — include weather check",
        value=False,
        key="weather_check_tab2",
    )

    # --- Route selector ---
    ranked = (st.session_state.get("search") or {}).get("ranked") or []
    top5   = ranked[:5]

    # If Tab 1 "Full analysis →" was clicked, auto-select that route
    target    = (st.session_state.get("search") or {}).get("analysis_target")
    target_id = target.get("document_id") if target else None

    tab2_route  = st.session_state.get("tab2_route")
    selected_id = tab2_route.get("document_id") if tab2_route else None

    if target_id and selected_id != target_id:
        st.session_state["tab2_route"] = target
        tab2_route  = target
        selected_id = target_id

    if top5:
        st.markdown("**Routes from your search**")

        def _route_label(r):
            area  = r.get("title_prefix") or ""
            name  = r.get("title") or "Unnamed"
            grade = r.get("global_rating") or ""
            label = f"{area} — {name}" if area else name
            return f"{label}  ({grade})" if grade else label

        ids    = [r.get("document_id") for r in top5]
        try:
            current_idx = ids.index(selected_id) if selected_id in ids else None
        except ValueError:
            current_idx = None

        chosen_idx = st.radio(
            "Select a route",
            options=range(len(top5)),
            format_func=lambda i: _route_label(top5[i]),
            index=current_idx,
            label_visibility="collapsed",
        )
        if chosen_idx is not None:
            chosen_route = top5[chosen_idx]
            if chosen_route.get("document_id") != selected_id:
                st.session_state["tab2_route"] = chosen_route
                tab2_route  = chosen_route
                selected_id = chosen_route.get("document_id")
                # Clear analysis_target so it doesn't fight the radio selection
                if st.session_state.get("search"):
                    st.session_state["search"]["analysis_target"] = None

                st.rerun()

    # Name search — prominent when no Tab 1 results, collapsed otherwise
    with st.expander("Search by name", expanded=not top5):
        query = st.text_input(
            "Route name",
            placeholder="e.g. Frendo spur, Gervasutti pillar…",
            label_visibility="collapsed",
            key="tab2_name_query",
        )
        if query:
            with st.spinner("Searching Camptocamp..."):
                name_results = search_routes_by_name(query, limit=15)
            if not name_results:
                st.warning("No routes found. Try a different name or spelling.")
            else:
                for r in name_results:
                    rid_r    = r.get("document_id")
                    rname_r  = r.get("title") or "Unnamed"
                    rarea_r  = r.get("title_prefix") or ""
                    grade_r  = r.get("global_rating") or ""
                    acts_r   = ", ".join(r.get("activities") or [])
                    label_r  = f"{rarea_r} — {rname_r}" if rarea_r else rname_r
                    sublabel = "  ·  ".join(filter(None, [grade_r, acts_r]))
                    col_r, col_btn = st.columns([6, 1])
                    with col_r:
                        st.markdown(
                            f"**{label_r}**" + (f"  <small>{sublabel}</small>" if sublabel else ""),
                            unsafe_allow_html=True,
                        )
                    with col_btn:
                        if st.button("Select", key=f"select_{rid_r}"):
                            with st.spinner("Loading route details..."):
                                st.session_state["tab2_route"] = fetch_route(rid_r)

                            st.rerun()

    # --- Full analysis (auto-triggered once a route is selected) ---
    if tab2_route is not None:
        rid   = tab2_route.get("document_id")
        rname = tab2_route.get("title") or "Unknown route"
        rarea = tab2_route.get("title_prefix") or ""
        grade = tab2_route.get("global_rating") or ""
        c2c_url = f"https://www.camptocamp.org/routes/{rid}" if rid else None

        st.divider()
        meta_parts = [f"**{rarea} — {rname}**" if rarea else f"**{rname}**"]
        if grade:   meta_parts.append(grade)
        if c2c_url: meta_parts.append(f"[C2C ↗]({c2c_url})")
        st.markdown("  ·  ".join(meta_parts))

        analyses  = st.session_state.setdefault("tab2_analyses", {})
        cache_key = (rid, weather_check2)

        if cache_key not in analyses:
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
                analyses[cache_key] = {
                    "text":    analyze_route(tab2_route, stubs2, full_outings2, _build_user_params(rock_onsight, rock_trad, ice_max, mixed_max, alpine_max), date.today(), weather=weather2),
                    "weather": weather2,
                }

            st.rerun()

        result = analyses[cache_key]
        wx = result["weather"]
        if weather_check2 and wx is None:
            st.warning("Weather unavailable: no coordinates found for this route.")
        elif wx and wx.fetch_errors and not wx.ui_table:
            st.warning("Weather fetch failed: " + "  \n".join(wx.fetch_errors))
        if wx and wx.ui_table:
            elev_str = f"  ·  summit {int(tab2_route['elevation_max'])}m" if tab2_route.get("elevation_max") else ""
            with st.expander("Raw weather data", expanded=False):
                st.caption(f"Open-Meteo forecast for {wx.coords[0]:.2f}N, {wx.coords[1]:.2f}E{elev_str}  ·  fetched {wx.fetch_date}")
                st.markdown(wx.ui_table)
                if wx.historical_text:
                    st.caption(wx.historical_text)
                if wx.fetch_errors:
                    st.warning("  \n".join(wx.fetch_errors))
                if not wx.avalanche_bulletins:
                    st.divider()
                    st.caption(
                        "⚠ No integrated avalanche bulletin for this area. "
                        "Check your local/regional avalanche service before heading out "
                        "(e.g. [avalanche.org](https://www.avalanches.org), "
                        "SLF, AINEVA, Météo-France, or the relevant national service)."
                    )
                for bulletin in wx.avalanche_bulletins:
                    st.divider()
                    if bulletin.fetch_error:
                        st.warning(f"Avalanche bulletin ({bulletin.massif_name}): {bulletin.fetch_error}")
                    else:
                        lvl = bulletin.danger_level
                        color = DANGER_COLORS.get(lvl, "gray")
                        label = DANGER_LABELS.get(lvl, str(lvl))
                        st.markdown(
                            f"**Avalanche bulletin — {bulletin.massif_name}**  "
                            f"·  Danger :{color}[**{lvl}/5 — {label}**]  "
                            f"·  Valid until {bulletin.valid_until}"
                        )
                        if bulletin.aspects_at_risk:
                            st.caption("Aspects at risk: " + ", ".join(bulletin.aspects_at_risk))
                        if bulletin.summary:
                            st.markdown(bulletin.summary)
                        if bulletin.image_meteo or bulletin.image_7days:
                            img_cols = st.columns(2)
                            if bulletin.image_meteo:
                                img_cols[0].image(bulletin.image_meteo, caption="Météo overview", width="stretch")
                            if bulletin.image_7days:
                                img_cols[1].image(bulletin.image_7days, caption="Last 7 days", width="stretch")
        st.markdown(result["text"])
        st.caption(
            "Source topos and trip reports linked above are the authoritative references. "
            "This AI analysis may be incomplete or wrong — verify conditions independently before your climb."
        )

# ===========================================================================
# TAB 4 — About
# ===========================================================================
with tab4:
    st.markdown("""
## About this tool

Mountaineering is a domain where LLMs are particularly unreliable on their own. Most of the real
knowledge — what a route actually feels like, how it comes into condition, what gear works — lives
in guidebooks behind paywalls, in conversations between guides and experienced alpinists, in hut
beta that never gets written down. What *does* make it online is disproportionately noise: forum
threads from beginners asking whether a serious route is "doable in trail runners," trip reports
from people who turned around halfway and reported confidently anyway. An LLM trained on that
corpus will pattern-match to the noise more than the signal.

At the same time, putting together a well-informed go/no-go assessment for a route is genuinely
time-consuming. You need to cross-reference the grade against your level, read recent conditions
reports, check the forecast, look at the past week's freeze-thaw cycle to judge snow stability —
all from separate sources, none of which talk to each other.

This tool addresses both problems. It combines deterministic filtering with real data and a
narrowly-scoped LLM call to make that preparation fast and structured.

---

### How it works

Routes are filtered against your stated limits using an explicit grading model — the LLM plays
no role here. French rock, alpine (F→ABO), ice (WI), and mixed (M) grades are all handled, with
scoring that accounts for partial matches and penalises routes that are hard across multiple
dimensions simultaneously. Only routes that survive this filter reach the analysis stage.

For each candidate route, the tool fetches:

- **Conditions reports and outing history** from [Camptocamp](https://www.camptocamp.org), the
  main community platform for Alpine routes. Reports are read by Claude and summarised.
- **Weather data** from [Open-Meteo](https://open-meteo.com): a 7-day forecast and 7-day
  historical record, both including pressure-level data (850/700/500 hPa) used to compute the
  0°C isotherm — the freeze line that determines overnight consolidation and afternoon wet-snow risk.
- **Avalanche bulletins** from multiple official services, matched to the route's coordinates:
  - [Météo-France BRA](https://meteofrance.fr) for all 35 French massifs
  - [SLF](https://www.slf.ch) (Swiss avalanche bulletin) for Switzerland
  - [EUREGIO](https://avalanche.report) for South Tyrol, Trentino, and Tyrol (Austria)
  - [AINEVA](https://www.aineva.it)-affiliated regional feeds for Valle d'Aosta, Piemonte, and Lombardia (seasonal — active roughly November to April)
  - Carinthia (Austria) via the EAWS static feed
  - A warning is shown when no integrated bulletin is available for the route's region (e.g. Slovenia, Norway, Spanish Pyrenees) — consult your local service in those cases.

Claude synthesises these inputs into a per-route conditions assessment: recent activity, weather
trend, freeze quality, avalanche danger and problem types, any storm flags.

**Tech stack:** Python · Streamlit · Anthropic Claude API · Camptocamp (unofficial API) · Open-Meteo · Météo-France API · SLF API · EAWS / avalanche.report

---

### A note on scope

This is a personal project, not a public tool — partly by design. The consequences of a route
recommender giving confident but wrong advice to someone underqualified for a serious alpine
objective are severe. The safety margin here is that the tool is used by someone who already
knows what they're doing and can sanity-check the output; it's not designed to replace that judgment.
""")

