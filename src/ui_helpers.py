"""
Streamlit UI helpers: logging, tool status labels, image rendering, gallery widget.
"""

import json
import re
from datetime import date, datetime
from pathlib import Path

import streamlit as st

_LOG_DIR = Path(".logs")
_LOG_DIR.mkdir(exist_ok=True)


def log(entry: dict) -> None:
    entry["ts"] = datetime.now().isoformat(timespec="milliseconds")
    log_path = _LOG_DIR / f"chat_{date.today()}.jsonl"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def tool_status_label(name: str, tool_input: dict) -> str:
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
        return f"Searching Camptocamp for \"{tool_input.get('query', '')}\""
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


def render_chat_images(text: str, attached: list | None = None) -> None:
    images = list(attached or [])
    images.extend(re.findall(r'!\[.*?\]\((https?://[^\)]+)\)', text))
    for img in images:
        try:
            st.image(img)
        except Exception:
            pass


def render_gallery() -> None:
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
