"""
frontend/app.py
---------------
SmartDialer Operations Control Center — Main Entry Point.
"""

from __future__ import annotations

import os
import sys
import time

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import streamlit as st

from app.models.agent import AgentState
from app.models.call import CallState
from app.models.campaign import DialMode
from frontend.components.charts import (
    render_agent_distribution_chart,
    render_answer_rate_convergence_chart,
    render_pacing_history_chart,
)
from frontend.components.tables import (
    render_calls_table,
    render_safety_decisions_table,
)
from frontend.state import get_runner, init_runner

# Page configuration
st.set_page_config(
    page_title="SmartDialer Control Center",
    page_icon="📞",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom Styling
st.markdown("""
<style>
    .reportview-container {
        background: #0E1117;
    }
    .metric-card {
        background-color: #1E293B;
        border-radius: 8px;
        padding: 16px;
        border: 1px solid #334155;
    }
    .status-badge {
        display: inline-block;
        padding: 4px 8px;
        border-radius: 4px;
        font-weight: bold;
        font-size: 0.85em;
    }
</style>
""", unsafe_allow_html=True)

runner = get_runner()

# ---------------------------------------------------------------------------
# Sidebar Controls & Navigation
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("📞 SmartDialer")
    st.caption("Predictive Dialing & Safety Engine")
    st.divider()

    st.subheader("⚙️ Simulation Controls")

    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        if st.button("▶️ Step Cycle", use_container_width=True, type="primary"):
            runner.step()
            st.rerun()

    with col_btn2:
        if st.button("🔄 Reset Store", use_container_width=True):
            init_runner()
            st.rerun()

    auto_run = st.checkbox("⚡ Auto-Advance (2s)", value=st.session_state.get("auto_run", False))
    st.session_state["auto_run"] = auto_run

    st.divider()

    st.subheader("🎯 Manual Call Request")
    st.caption("Submit custom call request through the Safety Controller.")
    req_count = st.number_input("Requested Calls:", min_value=1, max_value=50, value=5, step=1)
    if st.button("🚀 Dispatch Request", use_container_width=True):
        # 1. Safety Controller evaluates request
        decision = runner.safety_controller.evaluate(req_count, runner.campaign, runner.provider)
        
        # 2. If approved > 0, Call Allocator initiates through provider
        if decision.approved_count > 0:
            avail_agents = runner.store.list_available_agents()
            dialable_borrowers = runner.store.list_dialable_borrowers(runner.campaign.id)
            n_to_dial = min(decision.approved_count, len(avail_agents), len(dialable_borrowers))
            
            from app.dialer.allocator import AllocationRequest
            alloc_requests = [
                AllocationRequest(
                    agent_id=avail_agents[i].id,
                    borrower_id=dialable_borrowers[i].id,
                    campaign_id=runner.campaign.id,
                    provider_name=runner.provider.name,
                    lease_seconds=runner.config.lease_seconds,
                )
                for i in range(n_to_dial)
            ]
            results = runner.allocator.bulk_allocate(alloc_requests, runner.provider, event_callback=runner.processor.process)
            succeeded = sum(1 for r in results if r.success)
            st.success(f"Safety Decision: {decision.decision_type.value} | Allocated {succeeded}/{req_count} calls!")
        else:
            st.warning(f"Safety Controller {decision.decision_type.value}: {decision.reason}")
        st.rerun()

    st.divider()

    st.subheader("⚙️ Live Campaign Tuner")
    with st.expander("Edit Parameters", expanded=False):
        new_mode_str = st.selectbox(
            "Dial Mode:",
            ["predictive", "progressive"],
            index=0 if runner.campaign.dial_mode == DialMode.PREDICTIVE else 1,
        )
        new_provider_str = st.selectbox(
            "Telecom Provider:",
            ["provider_a", "provider_b"],
            index=0 if runner.provider.name == "provider_a" else 1,
        )
        new_max_calls_agent = st.slider(
            "Max Calls / Agent:",
            min_value=1.0,
            max_value=5.0,
            value=float(runner.campaign.max_calls_per_agent),
            step=0.5,
        )
        new_max_concurrent = st.slider(
            "Max Concurrent Ceiling:",
            min_value=5,
            max_value=150,
            value=int(runner.campaign.max_concurrent_calls),
            step=5,
        )
        new_sim_rate = st.slider(
            "Provider Answer Rate (%):",
            min_value=5,
            max_value=100,
            value=int(runner.provider._answer_rate * 100) if hasattr(runner.provider, '_answer_rate') else 55,
            step=5,
        )

        if st.button("💾 Apply Settings", use_container_width=True):
            runner.campaign.dial_mode = DialMode.PREDICTIVE if new_mode_str == "predictive" else DialMode.PROGRESSIVE
            runner.config.dial_mode = runner.campaign.dial_mode
            runner.campaign.max_calls_per_agent = new_max_calls_agent
            runner.campaign.max_concurrent_calls = new_max_concurrent
            runner.set_answer_rate(new_sim_rate / 100.0)
            
            if new_provider_str != runner.provider.name:
                from app.providers.provider_a import ProviderA
                from app.providers.provider_b import ProviderB
                if new_provider_str == "provider_b":
                    runner.provider = ProviderB(answer_rate=new_sim_rate / 100.0, delay_scale=0.05)
                else:
                    runner.provider = ProviderA(answer_rate=new_sim_rate / 100.0, delay_scale=0.05)
            st.success("Campaign parameters updated!")
            st.rerun()

    st.write(f"**Mode:** `{runner.campaign.dial_mode.value}`")
    st.write(f"**Provider:** `{runner.provider.name}`")
    st.write(f"**Max Concurrent:** `{runner.campaign.max_concurrent_calls}`")
    st.write(f"**Max Calls/Agent:** `{runner.campaign.max_calls_per_agent}x`")
    st.write(f"**Cycles Completed:** `{runner.cycle_count}`")

    st.divider()
    st.caption("Technical Prototype for Collections Dialing.")

# ---------------------------------------------------------------------------
# Main Operations Header
# ---------------------------------------------------------------------------
st.title("🎯 SmartDialer Live Control Center")
st.markdown("Real-time monitoring of agent capacity, predictive pacing, safety bounds, and telecom provider health.")

# Top status banner
cb_state = runner.circuit_breaker.state.value
cb_color = "🟢" if cb_state == "CLOSED" else ("🟡" if cb_state == "HALF_OPEN" else "🔴")
st.markdown(
    f"**System Status:** `ACTIVE` &nbsp;|&nbsp; **Circuit Breaker:** {cb_color} `{cb_state}` &nbsp;|&nbsp; "
    f"**Provider Health:** {'🟢 HEALTHY' if runner.provider.is_healthy() else '🔴 UNHEALTHY'}"
)

# ---------------------------------------------------------------------------
# Top KPI Metrics Row
# ---------------------------------------------------------------------------
agents = runner.store.list_agents()
calls = runner.store.list_calls(runner.campaign.id)

agent_counts = {s: 0 for s in AgentState}
for a in agents:
    agent_counts[a.state] += 1

total_agents = len(agents)
available_agents = agent_counts[AgentState.AVAILABLE]
connected_agents = agent_counts[AgentState.CONNECTED]
utilization = (connected_agents / total_agents * 100) if total_agents > 0 else 0.0

inflight_states = {CallState.INITIATED, CallState.RINGING, CallState.RESERVED, CallState.ANSWERED, CallState.CONNECTED}
active_calls = sum(1 for c in calls if c.state in inflight_states)
completed_calls = sum(1 for c in calls if c.state == CallState.COMPLETED)
failed_calls = sum(1 for c in calls if c.state == CallState.FAILED)

pacing_snap = runner.pacing_engine.last_snapshot()
answer_rate = (pacing_snap.smoothed_answer_rate * 100) if pacing_snap else 50.0

kpi1, kpi2, kpi3, kpi4, kpi5, kpi6 = st.columns(6)
with kpi1:
    st.metric("Total Agents", f"{total_agents}", help="Total agents logged into system")
with kpi2:
    st.metric("Available", f"{available_agents}", f"{available_agents - total_agents}" if available_agents < total_agents else None)
with kpi3:
    st.metric("Active In-Flight", f"{active_calls}")
with kpi4:
    st.metric("Connected", f"{connected_agents}")
with kpi5:
    st.metric("Agent Utilization", f"{utilization:.1f}%")
with kpi6:
    st.metric("EMA Answer Rate", f"{answer_rate:.1f}%")

st.divider()

# ---------------------------------------------------------------------------
# Visual Analytics Row
# ---------------------------------------------------------------------------
col_chart1, col_chart2 = st.columns([1, 2])

with col_chart1:
    render_agent_distribution_chart({s.name: count for s, count in agent_counts.items()})

with col_chart2:
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

# ---------------------------------------------------------------------------
# Tables Row (Recent Calls & Recent Safety Decisions)
# ---------------------------------------------------------------------------
tab1, tab2 = st.tabs(["🛡️ Recent Safety Controller Decisions", "📞 Live Call Records"])

with tab1:
    st.subheader("Safety Controller Evaluation Log")
    st.caption("Demonstrating that the Safety Controller is the final authority approving or reducing pacing requests.")
    render_safety_decisions_table(runner.safety_controller.decision_log, max_rows=15)

with tab2:
    st.subheader("Active & Recent Call State Transitions")
    render_calls_table(calls, max_rows=20)

# Auto-advance handler
if st.session_state.get("auto_run", False):
    time.sleep(2.0)
    runner.step()
    st.rerun()
