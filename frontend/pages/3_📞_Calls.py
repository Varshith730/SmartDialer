"""
frontend/pages/3_📞_Calls.py
----------------------------
Page 3: Live Call Records & State Transitions.
"""

import streamlit as st

from app.models.call import CallState
from frontend.components.tables import render_calls_table
from frontend.state import get_runner

st.set_page_config(page_title="Calls | SmartDialer", page_icon="📞", layout="wide")
runner = get_runner()

st.title("📞 Call Lifecycle Records")
st.markdown("Inspect call transitions, provider attributions, version counters, and idempotent event histories.")

calls = runner.store.list_calls(runner.campaign.id)

# Filter Controls
f1, f2, f3 = st.columns(3)
with f1:
    selected_provider = st.selectbox("Provider:", ["ALL", "provider_a", "provider_b"])
with f2:
    selected_state = st.selectbox("Call State:", ["ALL"] + [s.value for s in CallState])
with f3:
    agent_search = st.text_input("Filter by Agent ID (prefix):", value="")

filtered = calls
if selected_provider != "ALL":
    filtered = [c for c in filtered if c.provider == selected_provider]
if selected_state != "ALL":
    filtered = [c for c in filtered if c.state.value == selected_state]
if agent_search:
    filtered = [c for c in filtered if c.agent_id and agent_search.lower() in c.agent_id.lower()]

st.caption(f"Showing {len(filtered)} matching calls.")
render_calls_table(filtered, max_rows=100)
