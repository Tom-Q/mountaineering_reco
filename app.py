import json
import re
import streamlit as st
from dotenv import load_dotenv
from pathlib import Path
from datetime import date, datetime

from src.chat import chat_alpinist
from src.grades import (
    ROCK, ICE, MIXED, ALPINE,
    ENGAGEMENT, ENGAGEMENT_LABELS,
    RISK, RISK_LABELS,
    EXPOSITION, EXPOSITION_LABELS,
    EQUIPMENT, EQUIPMENT_LABELS,
)

_LOG_DIR = Path(".logs")
_LOG_DIR.mkdir(exist_ok=True)


def _log(entry: dict) -> None:
    entry["ts"] = datetime.now().isoformat(timespec="milliseconds")
    log_path = _LOG_DIR / f"chat_{date.today()}.jsonl"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _tool_status_label(name: str, tool_input: dict) -> str:
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
    if name == "show_images":
        n = len(tool_input.get("images", []))
        return f"Queuing {n} image{'s' if n != 1 else ''} for gallery"
    return f"Calling {name}..."


def _render_chat_images(text: str, attached: list | None = None) -> None:
    images = list(attached or [])
    images.extend(re.findall(r'!\[.*?\]\((https?://[^\)]+)\)', text))
    for img in images:
        try:
            st.image(img)
        except Exception:
            pass


def _render_gallery() -> None:
    gallery: list[dict] = st.session_state.get("image_gallery", [])
    blobs: dict = st.session_state.get("image_blobs", {})

    st.markdown("#### Photos & images")

    if not gallery:
        st.caption("Images surfaced by the assistant will appear here.")
        return

    idx = st.session_state.get("gallery_index", 0)
    idx = max(0, min(idx, len(gallery) - 1))
    st.session_state["gallery_index"] = idx

    item = gallery[idx]

    blob_key = item.get("blob_key")
    if blob_key and blob_key in blobs:
        image_data = blobs[blob_key]
    else:
        image_data = item.get("url")

    url = item.get("url", "")
    if url.lower().endswith(".svg"):
        st.markdown(f"[Open diagram (SVG)]({url})")
    elif image_data:
        try:
            st.image(image_data, width="stretch")
        except Exception as e:
            st.caption(f"Could not load image: {e}")

    caption = item.get("caption", "")
    source_url = item.get("source_url")
    if caption:
        st.caption(caption)
    if source_url:
        st.markdown(f"[Source]({source_url})", unsafe_allow_html=False)

    if len(gallery) > 1:
        prev_col, counter_col, next_col = st.columns([1, 2, 1])
        with prev_col:
            if st.button("◀", key="gallery_prev", disabled=(idx == 0)):
                st.session_state["gallery_index"] = idx - 1
                st.rerun()
        with counter_col:
            st.markdown(
                f"<div style='text-align:center;padding-top:6px'>{idx + 1} / {len(gallery)}</div>",
                unsafe_allow_html=True,
            )
        with next_col:
            if st.button("▶", key="gallery_next", disabled=(idx == len(gallery) - 1)):
                st.session_state["gallery_index"] = idx + 1
                st.rerun()


load_dotenv()

st.set_page_config(
    page_title="Alpinist AI",
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


def _build_user_params(
    rock_onsight, rock_trad, ice_max, mixed_max, alpine_max,
    engagement_max=None, risk_max=None, exposition_max=None, equipment_min=None,
) -> dict:
    return {
        "rock_onsight":    rock_onsight,
        "rock_trad":       None if rock_trad == "N/A" else rock_trad,
        "ice_max":         None if ice_max   == "—"   else ice_max,
        "mixed_max":       None if mixed_max == "—"   else mixed_max,
        "alpine_max":      alpine_max,
        "engagement_max":  engagement_max,
        "risk_max":        risk_max,
        "exposition_max":  exposition_max,
        "equipment_min":   equipment_min,
    }


# ---------------------------------------------------------------------------
# Sidebar: user profile
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
# Session state
# ---------------------------------------------------------------------------
if "chat_history" not in st.session_state:
    st.session_state["chat_history"] = []
if "api_messages" not in st.session_state:
    st.session_state["api_messages"] = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state["chat_history"]
    ]
if "image_gallery" not in st.session_state:
    st.session_state["image_gallery"] = []
if "gallery_index" not in st.session_state:
    st.session_state["gallery_index"] = 0
if "image_blobs" not in st.session_state:
    st.session_state["image_blobs"] = {}

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_chat, tab_about = st.tabs(["Chat with AI", "About"])

# ===========================================================================
# TAB 1 — Chat
# ===========================================================================
with tab_chat:
    st.warning(
        "This assistant may give inaccurate or dangerous advice. "
        "Always verify conditions and route information from authoritative sources "
        "before committing to any mountain objective.",
        icon="⚠️",
    )

    chat_col, gallery_col = st.columns([7, 3])

    with gallery_col:
        _render_gallery()

    with chat_col:
        messages = st.container()
        user_input = st.chat_input("Ask anything about alpine routes, gear, or conditions...")

        with messages:
            for msg in st.session_state["chat_history"]:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])
                    _render_chat_images(msg["content"], msg.get("images"))

            if user_input:
                _log({"type": "user", "content": user_input})
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

                        def _show_thinking() -> None:
                            thinking = "*Thinking...*"
                            if accumulated.strip():
                                text_placeholder.markdown(accumulated + "\n\n" + thinking)
                            else:
                                text_placeholder.markdown(thinking)

                        _show_thinking()

                        for event in chat_alpinist(
                            st.session_state["api_messages"],
                            date.today(),
                            user_params=_build_user_params(
                                rock_onsight, rock_trad, ice_max, mixed_max, alpine_max,
                                engagement_max, risk_max, exposition_max, equipment_min,
                            ),
                        ):
                            if event["type"] == "text":
                                accumulated += event["text"]
                                if accumulated.strip():
                                    text_placeholder.markdown(accumulated + "▌")

                            elif event["type"] == "tool_start":
                                _log({"type": "tool_call", "name": event["name"], "input": event["input"]})
                                current_status_label = _tool_status_label(event["name"], event["input"])
                                current_status = st.status(current_status_label + "...", expanded=False)

                            elif event["type"] == "tool_end":
                                _log({
                                    "type": "tool_result",
                                    "name": event["name"],
                                    "error": event["error"],
                                    "result": event.get("result_preview"),
                                })
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
                                _show_thinking()

                            elif event["type"] == "tool_images":
                                new_images = []
                                for img in event.get("images", []):
                                    st.session_state["image_gallery"].append(img)
                                    new_images.append({"url": img.get("url"), "caption": img.get("caption")})
                                for key, blob in event.get("image_blobs", {}).items():
                                    st.session_state["image_blobs"][key] = blob["data"]
                                    st.session_state["image_gallery"].append({
                                        "blob_key": key,
                                        "caption": blob["caption"],
                                        "source_url": blob.get("source_url"),
                                    })
                                    new_images.append({"blob_key": key, "caption": blob["caption"]})
                                if event.get("images") or event.get("image_blobs"):
                                    st.session_state["gallery_index"] = 0
                                if new_images:
                                    _log({"type": "images_queued", "images": new_images})

                            elif event["type"] == "done":
                                text_placeholder.markdown(accumulated)
                                st.session_state["api_messages"].extend(event["new_api_messages"])
                                reply = accumulated
                                _log({"type": "assistant", "content": reply})

                        _render_chat_images(reply)
                    except Exception as e:
                        reply = f"Sorry, I couldn't reach the assistant ({e}). Please try again."
                        _log({"type": "error", "error": str(e)})
                        st.markdown(reply)

                st.session_state["chat_history"].append(
                    {"role": "assistant", "content": reply, "images": []}
                )
                if st.session_state["image_gallery"]:
                    st.rerun()

        if st.session_state["chat_history"]:
            if st.button("Clear conversation", key="chat_clear"):
                st.session_state["chat_history"] = []
                st.session_state["api_messages"] = []
                st.session_state["image_gallery"] = []
                st.session_state["gallery_index"] = 0
                st.session_state["image_blobs"] = {}
                st.rerun()

# ===========================================================================
# TAB 2 — About
# ===========================================================================
with tab_about:
    st.warning(
        "This assistant may give inaccurate or dangerous advice — LLMs make surprising mistakes, "
        "and mountaineering is a domain where confident-sounding wrong information has real "
        "consequences. Always verify conditions and route information from authoritative sources "
        "before committing to any objective.",
        icon="⚠️",
    )
    st.markdown(Path("README.md").read_text(encoding="utf-8"))
