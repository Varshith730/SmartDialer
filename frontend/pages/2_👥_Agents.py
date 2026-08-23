"""
frontend/pages/2_👥_Agents.py
-----------------------------
Page 2: Live Agent Monitoring.
"""

import streamlit as st

from app.models.agent import AgentState
from frontend.components.tables import render_agents_table
from frontend.state import get_runner

st.set_page_config(page_title="Agents | SmartDialer", page_icon="👥", layout="wide")
runner = get_runner()

st.title("👥 Agent State Monitor")
st.markdown("Track atomic agent states across dialling lifecycles to ensure zero double-booking.")

agents = runner.store.list_agents()
agent_counts = {s: 0 for s in AgentState}
for a in agents:
    agent_counts[a.state] += 1

# Summary badge counters
c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
c1.metric("🟢 AVAILABLE", agent_counts[AgentState.AVAILABLE])
c2.metric("🟡 RESERVED", agent_counts[AgentState.RESERVED])
c3.metric("🔵 DIALING", agent_counts[AgentState.DIALING])
c4.metric("🟣 CONNECTED", agent_counts[AgentState.CONNECTED])
c5.metric("🟠 WRAP_UP", agent_counts[AgentState.WRAP_UP])
c6.metric("🔴 PAUSED", agent_counts[AgentState.PAUSED])
c7.metric("⚪ OFFLINE", agent_counts[AgentState.OFFLINE])

st.divider()

# Filter options
selected_state = st.selectbox(
    "Filter by Agent State:",
    options=["ALL"] + [s.value for s in AgentState],
)

if selected_state != "ALL":
    filtered_agents = [a for a in agents if a.state.value == selected_state]
else:
    filtered_agents = agents

st.caption(f"Showing {len(filtered_agents)} of {len(agents)} agents.")
render_agents_table(filtered_agents)
