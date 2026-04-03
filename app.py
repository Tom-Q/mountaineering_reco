import yaml
import streamlit as st
from dotenv import load_dotenv

from src.camptocamp import latlon_bbox_to_mercator, search_routes

load_dotenv()

st.set_page_config(
    page_title="Mountaineering Route Recommender",
    page_icon="🏔️",
    layout="wide",
)

st.title("🏔️ Mountaineering Route Recommender")
st.caption("Suggests alpine objectives based on your history, current conditions, and weather.")


# ---------------------------------------------------------------------------
# Load grade scales from the single source of truth
# ---------------------------------------------------------------------------
@st.cache_data
def load_grades():
    with open("grade_systems.yaml") as f:
        return yaml.safe_load(f)

grades = load_grades()
ROCK    = [str(g) for g in grades["rock_french"]["ordered"]]
ICE     = grades["ice_wI"]["ordered"]
MIXED   = grades["mixed_m"]["ordered"]
ALPINE  = grades["alpine"]["ordered"]

ENGAGEMENT = ["I", "II", "III", "IV", "V", "VI"]
ENGAGEMENT_LABELS = {
    "I":   "I — retreat easy at any point",
    "II":  "II — retreat possible throughout",
    "III": "III — retreat difficult once committed",
    "IV":  "IV — retreat very difficult, rescue complicated",
    "V":   "V — very few retreat options, serious isolation",
    "VI":  "VI — retreat is itself a major undertaking",
}
RISK = ["X1", "X2", "X3", "X4", "X5"]
RISK_LABELS = {
    "X1": "X1 — minor objective hazard",
    "X2": "X2 — moderate objective hazard",
    "X3": "X3 — marked objective hazard",
    "X4": "X4 — severe objective hazard",
    "X5": "X5 — very severe objective hazard",
}
EXPOSITION = ["E1", "E2", "E3", "E4", "E5", "E6"]
EXPOSITION_LABELS = {
    "E1": "E1 — over-protected",
    "E2": "E2 — well protected",
    "E3": "E3 — spaced, long fall possible",
    "E4": "E4 — fall causes injury",
    "E5": "E5 — fall causes severe accident",
    "E6": "E6 — fall likely fatal",
}
EQUIPMENT = ["P1", "P1+", "P2", "P2+", "P3", "P3+", "P4", "P4+"]
EQUIPMENT_LABELS = {
    "P1":  "P1 — sport, fully bolted",
    "P1+": "P1+ — sport, mostly bolted",
    "P2":  "P2 — trad, anchors equipped",
    "P2+": "P2+ — trad, most anchors equipped",
    "P3":  "P3 — trad, anchors often not equipped",
    "P3+": "P3+ — trad, few anchors equipped",
    "P4":  "P4 — almost nothing in place",
    "P4+": "P4+ — nothing in place",
}
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
        st.session_state["search_params"] = {
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
            "moving_time_min":        moving_time[0],   # hours
            "moving_time_max":        moving_time[1],
            "pace_hiking":            pace_hiking,      # multiplier: <1 faster, >1 slower
            "pace_technical":         pace_technical,
            # Risk
            "engagement_max":         engagement_max,
            "risk_max":               risk_max,
            "exposition_max":         exposition_max,
            "equipment_min":          equipment_min,
        }
        st.success("Parameters applied.")

if "search_params" in st.session_state:
    st.caption("Parameters active ✓")

st.divider()

# ---------------------------------------------------------------------------
# Route search
# ---------------------------------------------------------------------------
CHAMONIX_BBOX = latlon_bbox_to_mercator(lon_min=6.6, lat_min=45.7, lon_max=7.1, lat_max=46.0)

if st.button("Search routes in the Mont Blanc massif"):
    with st.spinner("Querying Camptocamp..."):
        routes = search_routes(CHAMONIX_BBOX, limit=5)

    st.subheader(f"{len(routes)} routes found")
    for route in routes:
        location = route.get("title_prefix") or "Unknown location"
        name     = route.get("title")        or "Unnamed route"
        st.write(f"**{location}** — {name}")
