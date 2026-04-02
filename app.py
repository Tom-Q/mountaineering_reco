import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="Mountaineering Route Recommender",
    page_icon="🏔️",
    layout="wide",
)

st.title("🏔️ Mountaineering Route Recommender")
st.caption("Suggests alpine objectives based on your history, current conditions, and weather.")

st.info("Work in progress — components being built out.", icon="🚧")
