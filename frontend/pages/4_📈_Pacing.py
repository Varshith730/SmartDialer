"""
frontend/pages/4_📈_Pacing.py
-----------------------------
Page 4: Predictive Pacing Engine Explanation & Analytics.
"""

import streamlit as st

from frontend.components.charts import render_answer_rate_convergence_chart, render_pacing_history_chart
from frontend.state import get_runner

st.set_page_config(page_title="Pacing Engine | SmartDialer", page_icon="📈", layout="wide")
runner = get_runner()

st.title("📈 Predictive Pacing Engine")
st.markdown("Visualizing the statistical over-dialling calculations and safety controller boundary.")

st.info("""
**Core Architecture Invariant:**
Prediction Proposes (`PredictiveEngine`) ➔ Safety Decides (`SafetyController`) ➔ Allocation Executes (`CallAllocator`).
The Predictive Engine **never** directly initiates telecom calls.
""")

snap = runner.pacing_engine.last_snapshot()
last_decision = runner.safety_controller.last_decision()

# 4-stage pipeline visualization
st.subheader("Decision Pipeline")
p1, p2, p3, p4 = st.columns(4)

with p1:
    st.markdown("### 1. Conditions")
    if snap:
        st.write(f"**Available Agents:** `{snap.available_agents}`")
        st.write(f"**In-Flight Calls:** `{snap.currently_inflight}`")
        st.write(f"**EMA Answer Rate:** `{snap.smoothed_answer_rate:.1%}`")
        st.write(f"**EMA Talk Time:** `{snap.smoothed_talk_time:.1f}s`")
    else:
        st.caption("No cycle executed yet.")

with p2:
    st.markdown("### 2. Prediction")
    if snap:
        st.metric("Requested Calls", f"{snap.requested_calls}", help="Calculated via target_inflight = ceil(avail / answer_rate) - inflight")
        st.caption("Formula: `ceil(avail / answer_rate) - inflight`")
    else:
        st.caption("No prediction yet.")

with p3:
    st.markdown("### 3. Safety Check")
    if last_decision:
        dec_color = "🟢" if last_decision.decision_type.value == "APPROVE" else "⚠️"
        st.metric("Safe Capacity", f"{last_decision.safe_capacity}")
        st.write(f"**Decision:** {dec_color} `{last_decision.decision_type.value}`")
    else:
        st.caption("No safety evaluation yet.")

with p4:
    st.markdown("### 4. Approved Calls")
    if last_decision:
        st.metric("Final Approved", f"{last_decision.approved_count}", help="Calls allocated to provider")
        st.caption(f"Reason: {last_decision.reason}")
    else:
        st.caption("No allocations yet.")

st.divider()

st.subheader("Answer Rate Learning & Convergence")
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

# ---------------------------------------------------------------------------
# Interactive What-If Pacing Simulator
# ---------------------------------------------------------------------------
st.subheader("🎛️ Interactive 'What-If' Pacing & Safety Simulator")
st.markdown("Test hypothetical operational conditions and observe the exact pacing calculation and Safety Controller clamping.")

with st.expander("🔬 Open What-If Scenario Sandbox", expanded=True):
    sim_col1, sim_col2 = st.columns(2)
    with sim_col1:
        sim_avail_agents = st.slider("Hypothetical Available Agents:", min_value=1, max_value=50, value=10)
        sim_answer_rate = st.slider("Hypothetical Answer Rate (%):", min_value=5, max_value=100, value=40)
        sim_max_calls_agent = st.slider("Campaign Max Calls / Agent:", min_value=1.0, max_value=5.0, value=3.0, step=0.5)
    with sim_col2:
        sim_inflight = st.slider("Current In-Flight Calls:", min_value=0, max_value=30, value=2)
        sim_campaign_cap = st.slider("Hard Campaign Concurrency Cap:", min_value=5, max_value=100, value=50)

    # Compute pacing math step-by-step
    import math
    rate_frac = max(0.01, sim_answer_rate / 100.0)
    raw_target = math.ceil(sim_avail_agents / rate_frac)
    agent_cap = int(sim_avail_agents * sim_max_calls_agent)
    capped_target = min(raw_target, agent_cap)
    sim_requested = max(0, capped_target - sim_inflight)
    sim_requested = min(sim_requested, sim_avail_agents)

    # Compute safety capacity
    safe_capacity = min(
        sim_avail_agents,
        sim_campaign_cap - sim_inflight,
    )
    safe_capacity = max(0, safe_capacity)
    
    if sim_requested <= safe_capacity and sim_requested > 0:
        sim_decision = "APPROVE"
        sim_approved = sim_requested
    elif safe_capacity > 0:
        sim_decision = "REDUCE"
        sim_approved = safe_capacity
    else:
        sim_decision = "REJECT"
        sim_approved = 0

    st.markdown("#### Calculation Outcome:")
    o1, o2, o3, o4 = st.columns(4)
    o1.metric("1. Target In-Flight", f"{capped_target}", f"Formula: ceil({sim_avail_agents} / {rate_frac:.2f})")
    o2.metric("2. Pacing Requested", f"{sim_requested}", f"- {sim_inflight} in-flight")
    o3.metric("3. Safe Capacity", f"{safe_capacity}", f"Max agents free")
    o4.metric("4. Safety Decision", f"{sim_decision} ({sim_approved})")

    st.caption(f"Mathematical Trace: `min(ceil({sim_avail_agents} / {rate_frac:.2f}), {agent_cap}) - {sim_inflight} = {sim_requested}` ➔ Safety Bounds: `{sim_decision} ➔ {sim_approved} approved calls`")

