"""
frontend/app.py
---------------
SmartDialer Operations Control Center — Main Application with Top Navigation Bar.
"""

from __future__ import annotations

import math
import os
import sys
import time

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from app.models.agent import Agent, AgentState
from app.models.borrower import Borrower, BorrowerPriority
from app.models.call import Call, CallState
from app.models.campaign import DialMode
from app.providers.provider_a import ProviderA
from app.providers.provider_b import ProviderB
from app.simulation.scenarios import (
    ALL_SCENARIOS,
    SCENARIO_A,
    SCENARIO_B,
    SCENARIO_C,
    run_scenario,
    run_scenario_d,
)
from frontend.components.charts import (
    render_agent_distribution_chart,
    render_answer_rate_convergence_chart,
    render_pacing_history_chart,
)
from frontend.components.tables import (
    format_agent_state,
    format_call_state,
    render_agents_table,
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

# Custom Styling to hide default sidebar nav and style the top navbar
st.markdown("""
<style>
    /* Hide default Streamlit sidebar navigation */
    [data-testid="stSidebarNav"] {
        display: none;
    }
    .reportview-container {
        background: #0E1117;
    }
    .top-nav {
        background-color: #1E293B;
        border-radius: 10px;
        padding: 8px 16px;
        margin-bottom: 20px;
        border: 1px solid #334155;
    }
    .metric-card {
        background-color: #1E293B;
        border-radius: 8px;
        padding: 16px;
        border: 1px solid #334155;
    }
</style>
""", unsafe_allow_html=True)

runner = get_runner()

# ---------------------------------------------------------------------------
# Global Sidebar: Simulation Stepper & Quick Tuner
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("📞 SmartDialer")
    st.caption("Predictive Dialing & Safety Engine")
    st.divider()

    st.subheader("⚡ Quick Controls")
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
    req_count = st.number_input("Requested Calls:", min_value=1, max_value=50, value=5, step=1)
    if st.button("🚀 Dispatch Request", use_container_width=True):
        decision = runner.safety_controller.evaluate(req_count, runner.campaign, runner.provider)
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
            st.success(f"Decision: {decision.decision_type.value} | Allocated {succeeded}/{req_count} calls!")
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
                if new_provider_str == "provider_b":
                    runner.provider = ProviderB(answer_rate=new_sim_rate / 100.0, delay_scale=0.05)
                else:
                    runner.provider = ProviderA(answer_rate=new_sim_rate / 100.0, delay_scale=0.05)
            st.success("Campaign updated!")
            st.rerun()

    st.write(f"**Mode:** `{runner.campaign.dial_mode.value}`")
    st.write(f"**Provider:** `{runner.provider.name}`")
    st.write(f"**Max Concurrent:** `{runner.campaign.max_concurrent_calls}`")
    st.write(f"**Max Calls/Agent:** `{runner.campaign.max_calls_per_agent}x`")
    st.write(f"**Cycles Completed:** `{runner.cycle_count}`")

    st.divider()
    st.caption("SmartDialer Operations Prototype")

# ---------------------------------------------------------------------------
# Top Navigation Bar Menu
# ---------------------------------------------------------------------------
nav_options = [
    "📊 Dashboard",
    "👥 Agents",
    "📞 Calls",
    "📈 Pacing",
    "🛡️ Safety",
    "📡 Providers",
    "⚠️ Failures",
    "🧪 Simulation",
]

# Use top horizontal menu
st.markdown("### 📞 SmartDialer Control Center")
selected_tab = st.pills("Navigation Menu", nav_options, default=nav_options[0], selection_mode="single")
if not selected_tab:
    selected_tab = nav_options[0]

st.divider()

# Common queries
agents = runner.store.list_agents()
calls = runner.store.list_calls(runner.campaign.id)
agent_counts = {s: 0 for s in AgentState}
for a in agents:
    agent_counts[a.state] += 1

total_agents = len(agents)
available_agents = agent_counts[AgentState.AVAILABLE]
connected_agents = agent_counts[AgentState.CONNECTED]
utilization = (connected_agents / total_agents * 100) if total_agents > 0 else 0.0

# ---------------------------------------------------------------------------
# VIEW 1: DASHBOARD
# ---------------------------------------------------------------------------
if selected_tab == "📊 Dashboard":
    st.subheader("📊 Global Operations Dashboard")

    cb_state = runner.circuit_breaker.state.value
    cb_color = "🟢" if cb_state == "CLOSED" else ("🟡" if cb_state == "HALF_OPEN" else "🔴")
    st.markdown(
        f"**System Status:** `ACTIVE` &nbsp;|&nbsp; **Circuit Breaker:** {cb_color} `{cb_state}` &nbsp;|&nbsp; "
        f"**Provider Health:** {'🟢 HEALTHY' if runner.provider.is_healthy() else '🔴 UNHEALTHY'}"
    )

    inflight_states = {CallState.INITIATED, CallState.RINGING, CallState.RESERVED, CallState.ANSWERED, CallState.CONNECTED}
    active_calls = sum(1 for c in calls if c.state in inflight_states)
    pacing_snap = runner.pacing_engine.last_snapshot()
    answer_rate = (pacing_snap.smoothed_answer_rate * 100) if pacing_snap else 50.0

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Total Agents", f"{total_agents}")
    k2.metric("Available", f"{available_agents}")
    k3.metric("In-Flight Calls", f"{active_calls}")
    k4.metric("Connected", f"{connected_agents}")
    k5.metric("Agent Utilization", f"{utilization:.1f}%")
    k6.metric("EMA Answer Rate", f"{answer_rate:.1f}%")

    st.divider()

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
    tab_dec, tab_c = st.tabs(["🛡️ Recent Safety Controller Decisions", "📞 Live Call Records"])
    with tab_dec:
        render_safety_decisions_table(runner.safety_controller.decision_log, max_rows=15)
    with tab_c:
        render_calls_table(calls, max_rows=20)

# ---------------------------------------------------------------------------
# VIEW 2: AGENTS
# ---------------------------------------------------------------------------
elif selected_tab == "👥 Agents":
    st.subheader("👥 Live Agent Monitoring & Management")

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("🟢 AVAILABLE", agent_counts[AgentState.AVAILABLE])
    c2.metric("🟡 RESERVED", agent_counts[AgentState.RESERVED])
    c3.metric("🔵 DIALING", agent_counts[AgentState.DIALING])
    c4.metric("🟣 CONNECTED", agent_counts[AgentState.CONNECTED])
    c5.metric("🟠 WRAP_UP", agent_counts[AgentState.WRAP_UP])
    c6.metric("🔴 PAUSED", agent_counts[AgentState.PAUSED])
    c7.metric("⚪ OFFLINE", agent_counts[AgentState.OFFLINE])

    st.divider()

    selected_state = st.selectbox("Filter by Agent State:", options=["ALL"] + [s.value for s in AgentState])
    filtered_agents = agents if selected_state == "ALL" else [a for a in agents if a.state.value == selected_state]
    st.caption(f"Showing {len(filtered_agents)} of {len(agents)} agents.")
    render_agents_table(filtered_agents)

    st.divider()
    st.subheader("🛠️ Interactive Agent Management")
    col_a1, col_a2 = st.columns(2)
    with col_a1:
        with st.expander("➕ Add New Agent to System", expanded=True):
            new_name = st.text_input("Agent Name:", value=f"Agent-{len(agents)+1:02d}")
            init_state_choice = st.selectbox("Initial State:", ["AVAILABLE", "PAUSED", "OFFLINE"])
            if st.button("➕ Register Agent", use_container_width=True):
                state_enum = getattr(AgentState, init_state_choice)
                new_agent = Agent(name=new_name, state=state_enum)
                runner.store.save_agent(new_agent)
                st.success(f"Agent '{new_name}' created in {init_state_choice} state!")
                st.rerun()

    with col_a2:
        with st.expander("⚙️ Modify Agent Status Live", expanded=True):
            if agents:
                agent_dict = {f"{a.name} ({a.id[:8]}... - {a.state.value})": a.id for a in agents}
                selected_agent_label = st.selectbox("Select Agent:", list(agent_dict.keys()))
                target_agent_id = agent_dict[selected_agent_label]
                target_agent = runner.store.get_agent(target_agent_id)
                
                new_status_choice = st.selectbox("Change State To:", ["AVAILABLE", "PAUSED", "OFFLINE", "WRAP_UP"])
                if st.button("🔄 Apply State Transition", use_container_width=True):
                    target_agent.state = getattr(AgentState, new_status_choice)
                    if new_status_choice in ("AVAILABLE", "PAUSED", "OFFLINE"):
                        target_agent.call_id = None
                        target_agent.borrower_id = None
                        target_agent.reservation_id = None
                        target_agent.lease_until = None
                    runner.store.save_agent(target_agent)
                    st.success(f"Updated {target_agent.name} to {new_status_choice}!")
                    st.rerun()

    if agent_counts[AgentState.WRAP_UP] > 0:
        if st.button("🧹 Release All Agents from WRAP_UP to AVAILABLE", type="primary"):
            for a in agents:
                if a.state == AgentState.WRAP_UP:
                    runner.processor.complete_wrap_up(a.id)
            st.success("All WRAP_UP agents released!")
            st.rerun()

# ---------------------------------------------------------------------------
# VIEW 3: CALLS
# ---------------------------------------------------------------------------
elif selected_tab == "📞 Calls":
    st.subheader("📞 Call Lifecycle Records & Direct Dispatch")

    f1, f2, f3 = st.columns(3)
    with f1:
        selected_provider = st.selectbox("Provider:", ["ALL", "provider_a", "provider_b"])
    with f2:
        selected_call_state = st.selectbox("Call State:", ["ALL"] + [s.value for s in CallState])
    with f3:
        agent_search = st.text_input("Filter by Agent ID:", value="")

    filtered_calls = calls
    if selected_provider != "ALL":
        filtered_calls = [c for c in filtered_calls if c.provider == selected_provider]
    if selected_call_state != "ALL":
        filtered_calls = [c for c in filtered_calls if c.state.value == selected_call_state]
    if agent_search:
        filtered_calls = [c for c in filtered_calls if c.agent_id and agent_search.lower() in c.agent_id.lower()]

    st.caption(f"Showing {len(filtered_calls)} calls.")
    render_calls_table(filtered_calls, max_rows=100)

    st.divider()
    st.subheader("🛠️ Direct Call Dispatch & Borrower Enrolment")
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        with st.expander("📞 Initiate Single Direct Call", expanded=True):
            avail_agents = runner.store.list_available_agents()
            dialable_borrowers = runner.store.list_dialable_borrowers(runner.campaign.id)
            if avail_agents and dialable_borrowers:
                agent_options = {f"{a.name} ({a.id[:8]}...)": a.id for a in avail_agents}
                borrower_options = {f"{b.name} (Priority {b.priority.name} - {b.id[:8]}...)": b.id for b in dialable_borrowers}
                chosen_agent = st.selectbox("Select Available Agent:", list(agent_options.keys()))
                chosen_borrower = st.selectbox("Select Target Borrower:", list(borrower_options.keys()))
                if st.button("🚀 Dial Single Call Now", use_container_width=True, type="primary"):
                    decision = runner.safety_controller.evaluate(1, runner.campaign, runner.provider)
                    if decision.approved_count > 0:
                        from app.dialer.allocator import AllocationRequest
                        req = AllocationRequest(
                            agent_id=agent_options[chosen_agent],
                            borrower_id=borrower_options[chosen_borrower],
                            campaign_id=runner.campaign.id,
                            provider_name=runner.provider.name,
                            lease_seconds=runner.config.lease_seconds,
                        )
                        res = runner.allocator.allocate(req, runner.provider, event_callback=runner.processor.process)
                        if res.success:
                            st.success(f"Call initiated! ID: {res.call.id[:8]}...")
                        else:
                            st.error(f"Allocation failed: {res.failure_reason}")
                    else:
                        st.warning(f"Safety Controller {decision.decision_type.value}: {decision.reason}")
                    st.rerun()
            else:
                st.info("Requires at least 1 available agent and 1 pending borrower.")

    with col_c2:
        with st.expander("➕ Enrol New Borrower to Queue", expanded=True):
            b_name = st.text_input("Borrower Name:", value="Jane Smith")
            b_priority = st.selectbox("Priority Tier:", ["HIGH", "MEDIUM", "LOW"])
            if st.button("➕ Add Borrower to Queue", use_container_width=True):
                new_borrower = Borrower(
                    name=b_name,
                    campaign_id=runner.campaign.id,
                    priority=getattr(BorrowerPriority, b_priority),
                )
                runner.store.save_borrower(new_borrower)
                st.success(f"Borrower '{b_name}' added to campaign with priority {b_priority}!")
                st.rerun()

# ---------------------------------------------------------------------------
# VIEW 4: PACING
# ---------------------------------------------------------------------------
elif selected_tab == "📈 Pacing":
    st.subheader("📈 Predictive Pacing Engine & Live Mathematical Trace")

    snap = runner.pacing_engine.last_snapshot()
    last_decision = runner.safety_controller.last_decision()

    p1, p2, p3, p4 = st.columns(4)
    with p1:
        st.markdown("### 1. Conditions")
        if snap:
            st.write(f"**Available:** `{snap.available_agents}`")
            st.write(f"**In-Flight:** `{snap.currently_inflight}`")
            st.write(f"**EMA Answer Rate:** `{snap.smoothed_answer_rate:.1%}`")
        else:
            st.caption("No cycle executed yet.")

    with p2:
        st.markdown("### 2. Prediction")
        if snap:
            st.metric("Requested Calls", f"{snap.requested_calls}")
            st.caption("Formula: `ceil(avail / answer_rate) - inflight`")
        else:
            st.caption("No prediction yet.")

    with p3:
        st.markdown("### 3. Safety Check")
        if last_decision:
            st.metric("Safe Capacity", f"{last_decision.safe_capacity}")
            st.write(f"**Decision:** `{last_decision.decision_type.value}`")
        else:
            st.caption("No evaluation yet.")

    with p4:
        st.markdown("### 4. Approved Calls")
        if last_decision:
            st.metric("Final Approved", f"{last_decision.approved_count}")
            st.caption(f"Reason: {last_decision.reason}")
        else:
            st.caption("No allocations yet.")

    st.divider()
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
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        render_answer_rate_convergence_chart(cycles_data)
    with col_c2:
        render_pacing_history_chart(cycles_data)

    st.divider()
    st.subheader("🎛️ Interactive 'What-If' Pacing & Safety Simulator")
    with st.expander("🔬 Open What-If Scenario Sandbox", expanded=True):
        sim_col1, sim_col2 = st.columns(2)
        with sim_col1:
            sim_avail_agents = st.slider("Hypothetical Available Agents:", min_value=1, max_value=50, value=10)
            sim_answer_rate = st.slider("Hypothetical Answer Rate (%):", min_value=5, max_value=100, value=40)
            sim_max_calls_agent = st.slider("Campaign Max Calls / Agent:", min_value=1.0, max_value=5.0, value=3.0, step=0.5)
        with sim_col2:
            sim_inflight = st.slider("Current In-Flight Calls:", min_value=0, max_value=30, value=2)
            sim_campaign_cap = st.slider("Hard Campaign Concurrency Cap:", min_value=5, max_value=100, value=50)

        rate_frac = max(0.01, sim_answer_rate / 100.0)
        raw_target = math.ceil(sim_avail_agents / rate_frac)
        agent_cap = int(sim_avail_agents * sim_max_calls_agent)
        capped_target = min(raw_target, agent_cap)
        sim_requested = max(0, capped_target - sim_inflight)
        sim_requested = min(sim_requested, sim_avail_agents)

        safe_capacity = max(0, min(sim_avail_agents, sim_campaign_cap - sim_inflight))
        if sim_requested <= safe_capacity and sim_requested > 0:
            sim_decision = "APPROVE"
            sim_approved = sim_requested
        elif safe_capacity > 0:
            sim_decision = "REDUCE"
            sim_approved = safe_capacity
        else:
            sim_decision = "REJECT"
            sim_approved = 0

        o1, o2, o3, o4 = st.columns(4)
        o1.metric("1. Target In-Flight", f"{capped_target}", f"ceil({sim_avail_agents} / {rate_frac:.2f})")
        o2.metric("2. Pacing Requested", f"{sim_requested}", f"- {sim_inflight} in-flight")
        o3.metric("3. Safe Capacity", f"{safe_capacity}", "Max agents free")
        o4.metric("4. Safety Decision", f"{sim_decision} ({sim_approved})")

# ---------------------------------------------------------------------------
# VIEW 5: SAFETY
# ---------------------------------------------------------------------------
elif selected_tab == "🛡️ Safety":
    st.subheader("🛡️ Safety Controller & Capacity Enforcement Audit")

    decisions = runner.safety_controller.decision_log
    summary = runner.safety_controller.decision_summary()

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Total Evaluations", len(decisions))
    s2.metric("APPROVE", summary.get("APPROVE", 0))
    s3.metric("REDUCE", summary.get("REDUCE", 0))
    s4.metric("REJECT", summary.get("REJECT", 0))

    st.divider()
    if summary:
        df_summary = pd.DataFrame(list(summary.items()), columns=["Decision Type", "Count"])
        fig = px.pie(
            df_summary,
            values="Count",
            names="Decision Type",
            title="Safety Decision Distribution",
            color="Decision Type",
            color_discrete_map={
                "APPROVE": "#10B981",
                "REDUCE": "#F59E0B",
                "REJECT": "#EF4444",
                "FALLBACK_PROGRESSIVE": "#3B82F6",
            },
        )
        fig.update_layout(height=280, margin=dict(l=20, r=20, t=40, b=20))
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("Complete Safety Decision Audit Log")
    render_safety_decisions_table(decisions, max_rows=50)

# ---------------------------------------------------------------------------
# VIEW 6: PROVIDERS
# ---------------------------------------------------------------------------
elif selected_tab == "📡 Providers":
    st.subheader("📡 Telecom Providers & Circuit Breakers")

    cb = runner.circuit_breaker
    cb_stats = cb.stats()
    proc_stats = runner.processor.stats()

    col_p1, col_p2 = st.columns(2)
    with col_p1:
        st.subheader("Provider A (Reliable Tier)")
        st.markdown("- **API Success Rate:** 98%\n- **Sequencing:** Clean ordered events")
        st.write(f"**Status:** {'🟢 HEALTHY' if runner.provider.name == 'provider_a' and runner.provider.is_healthy() else '⚪ INACTIVE'}")

    with col_p2:
        st.subheader("Provider B (Chaotic / Stress Tier)")
        st.markdown("- **API Success Rate:** Degraded\n- **Sequencing:** Chaos injection (duplicates & out-of-order)")
        st.write(f"**Status:** {'🟡 ACTIVE (CHAOS)' if runner.provider.name == 'provider_b' else '⚪ INACTIVE'}")

    st.divider()
    st.subheader(f"Circuit Breaker Status: `{cb_stats['provider']}`")
    cb1, cb2, cb3, cb4 = st.columns(4)
    cb_state_val = cb_stats['state']
    cb1.metric("State", f"{cb_state_val}", delta="OK" if cb_state_val == "CLOSED" else "TRIPPED")
    cb2.metric("Failure Threshold", f"{cb.failure_threshold}")
    cb3.metric("Total Failures", f"{cb_stats['total_failures']}")
    cb4.metric("Total Successes", f"{cb_stats['total_successes']}")

    st.divider()
    st.subheader("Event Processor Ingestion Metrics")
    e1, e2, e3, e4 = st.columns(4)
    e1.metric("Total Events Ingested", proc_stats["total_received"])
    e2.metric("Valid Transitions Applied", proc_stats["total_applied"])
    e3.metric("Duplicate Events Dropped", proc_stats["duplicates_dropped"])
    e4.metric("Out-of-Order Stale Dropped", proc_stats["out_of_order_dropped"])

# ---------------------------------------------------------------------------
# VIEW 7: FAILURES
# ---------------------------------------------------------------------------
elif selected_tab == "⚠️ Failures":
    st.subheader("⚠️ Chaos Engineering & Fault Injections")

    col_f1, col_f2 = st.columns(2)
    with col_f1:
        st.subheader("1. Provider & Telephony Disruptions")
        if st.button("🔴 Trip Circuit Breaker (Force OPEN)", use_container_width=True):
            runner.circuit_breaker.force_open()
            st.error("Circuit Breaker forced to OPEN state! Next cycle Safety Controller will REJECT calls.")

        custom_rate = st.slider("Inject Custom Answer Rate (%):", min_value=1, max_value=100, value=15, step=5)
        if st.button(f"📉 Set Answer Rate to {custom_rate}%", use_container_width=True):
            runner.set_answer_rate(custom_rate / 100.0)
            st.warning(f"Provider answer rate set to {custom_rate}%. Predictive Engine EMA will adapt downward.")

        if st.button("🌪️ Switch to Provider B (Chaotic Provider)", use_container_width=True):
            runner.provider = ProviderB(answer_rate=0.40, delay_scale=0.05, duplicate_probability=0.5, out_of_order_probability=0.3)
            st.warning("Active provider switched to Provider B. Expect duplicates and out-of-order deliveries.")

    with col_f2:
        st.subheader("2. Worker Crash & Agent Drops")
        avail_count = len([a for a in runner.store.list_agents() if a.state.value == "AVAILABLE"])
        drop_count = st.number_input("Number of Agents to Take OFFLINE:", min_value=1, max_value=max(1, avail_count), value=min(10, max(1, avail_count)))
        if st.button(f"💥 Drop {drop_count} Available Agents", use_container_width=True):
            runner.set_agents_offline(drop_count)
            st.error(f"{drop_count} available agents moved to OFFLINE. Safety Controller will throttle capacity immediately.")

        if st.button("💀 Simulate Worker Crash (Expired Reservation Lease)", use_container_width=True):
            import uuid
            from datetime import datetime, timedelta, timezone
            agents_avail = [a for a in runner.store.list_agents() if a.state.value == "AVAILABLE"]
            borrowers_avail = runner.store.list_dialable_borrowers(runner.campaign.id)
            if agents_avail and borrowers_avail:
                a = agents_avail[0]
                b = borrowers_avail[0]
                res_id = str(uuid.uuid4())
                runner.store.atomic_reserve_agent(a.id, res_id, lease_seconds=0.01)
                runner.store.atomic_reserve_borrower(b.id, res_id)
                call = Call(
                    agent_id=a.id,
                    borrower_id=b.id,
                    campaign_id=runner.campaign.id,
                    reservation_id=res_id,
                    lease_until=datetime.now(timezone.utc) - timedelta(seconds=10),
                )
                call.apply_transition(CallState.RESERVED, event_id=f"crash-{call.id}")
                runner.store.save_call(call)
                st.warning(f"Injected crashed worker on Agent {a.id[:8]}... with expired lease. Run Reconciler to reclaim.")

        if st.button("🧹 Run Reconciler Cleanup Now", use_container_width=True):
            rec_res = runner.reconciler.run()
            if rec_res.cleaned_up > 0:
                st.success(f"Reconciler recovered {rec_res.cleaned_up} crashed call(s) and freed orphaned agents!")
            else:
                st.info("Reconciler found no expired leases.")

    st.divider()
    st.subheader("3. Restore System to Healthy Baseline")
    if st.button("💚 Restore All Systems (Clear Outages & Recover Agents)", type="primary", use_container_width=True):
        runner.circuit_breaker.force_close()
        runner.restore_agents()
        runner.set_answer_rate(0.60)
        runner.reconciler.run()
        st.success("System restored: Circuit Breaker CLOSED, all agents back ONLINE, answer rate 60%.")

# ---------------------------------------------------------------------------
# VIEW 8: SIMULATION
# ---------------------------------------------------------------------------
elif selected_tab == "🧪 Simulation":
    st.subheader("🧪 Scenario Benchmarking & Comparative Simulation")

    scenario_choice = st.selectbox(
        "Select Scenario:",
        options=[
            "Scenario A — Low Answer Rate (Stress)",
            "Scenario B — Normal Balanced Operations",
            "Scenario C — High Answer Rate (High Load)",
            "Scenario D — Dynamic Disruption & Outage Recovery",
        ],
    )

    if "A" in scenario_choice:
        active_scenario = SCENARIO_A
    elif "B" in scenario_choice:
        active_scenario = SCENARIO_B
    elif "C" in scenario_choice:
        active_scenario = SCENARIO_C
    else:
        active_scenario = None

    if active_scenario:
        st.info(f"**Description:** {active_scenario.description}")
    else:
        st.info("**Description:** Multi-phase execution: 5 cycles normal, 5 cycles forced provider outage (Safety Controller rejects all), 5 cycles recovery.")

    if st.button("🚀 Run Scenario Benchmark", type="primary", use_container_width=True):
        with st.spinner("Executing simulation scenario..."):
            if active_scenario:
                sim_runner, report = run_scenario(active_scenario)
                logs = None
            else:
                sim_runner, report, logs = run_scenario_d()

        st.success(f"Simulation completed in {report.elapsed_seconds:.2f}s across {len(report.cycles)} cycles!")
        if logs:
            st.subheader("Phase Execution Log")
            for log_entry in logs:
                st.write(f"- {log_entry}")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Calls Initiated", report.total_initiated)
        m2.metric("Calls Answered", report.total_completed)
        m3.metric("Calls Failed", report.total_failed)
        m4.metric("Observed Answer Rate", f"{report.observed_answer_rate:.1%}")

        st.divider()
        cycles_data = [
            {
                "cycle_num": m.cycle_num,
                "requested_calls": m.requested_calls,
                "approved_calls": m.approved_calls,
                "calls_inflight": m.calls_inflight,
                "smoothed_answer_rate": m.smoothed_answer_rate,
            }
            for m in report.cycles
        ]
        c1, c2 = st.columns(2)
        with c1:
            render_pacing_history_chart(cycles_data)
        with c2:
            render_answer_rate_convergence_chart(cycles_data)

# Auto-advance handler
if st.session_state.get("auto_run", False):
    time.sleep(2.0)
    runner.step()
    st.rerun()
