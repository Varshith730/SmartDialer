"""
frontend/pages/1_📊_Dashboard.py
--------------------------------
Page 1: Operations Dashboard.
"""

import streamlit as st

from app.models.agent import AgentState
from app.models.call import CallState
from frontend.components.charts import (
    render_agent_distribution_chart,
    render_answer_rate_convergence_chart,
    render_pacing_history_chart,
)
from frontend.components.tables import (
    render_calls_table,
    render_safety_decisions_table,
)
from frontend.state import get_runner

st.set_page_config(page_title="Dashboard | SmartDialer", page_icon="📊", layout="wide")
runner = get_runner()

st.title("📊 SmartDialer Operations Dashboard")
st.markdown("Global overview of agent allocation, active dialing progress, and safety controller actions.")

# Top metrics
agents = runner.store.list_agents()
calls = runner.store.list_calls(runner.campaign.id)

agent_counts = {s: 0 for s in AgentState}
for a in agents:
    agent_counts[a.state] += 1

total_agents = len(agents)
available = agent_counts[AgentState.AVAILABLE]
connected = agent_counts[AgentState.CONNECTED]
dialing = agent_counts[AgentState.DIALING]
utilization = (connected / total_agents * 100) if total_agents > 0 else 0.0

snap = runner.pacing_engine.last_snapshot()
answer_rate = (snap.smoothed_answer_rate * 100) if snap else 50.0

m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Total Agents", f"{total_agents}")
m2.metric("Available", f"{available}")
m3.metric("Dialing", f"{dialing}")
m4.metric("Connected", f"{connected}")
m5.metric("Utilization", f"{utilization:.1f}%")
m6.metric("Answer Rate", f"{answer_rate:.1f}%")

st.divider()

col_left, col_right = st.columns([1, 2])
with col_left:
    render_agent_distribution_chart({s.name: count for s, count in agent_counts.items()})

with col_right:
    cycles_data = [
        {
            "cycle_num": m.cycle_num,
            "requested_calls": m.requested_calls,
            "approved_calls": m.approved_calls,
            "calls_inflight": m.calls_inflight,
            "smoothed_answer_rate": m.smoothed_answer_rate,
        }
        for m in runner.report.cycles
    ]
    render_pacing_history_chart(cycles_data)

st.divider()

t1, t2 = st.tabs(["🛡️ Recent Safety Decisions", "📞 Recent Call Records"])
with t1:
    render_safety_decisions_table(runner.safety_controller.decision_log, max_rows=15)
with t2:
    render_calls_table(calls, max_rows=20)
