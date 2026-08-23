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
