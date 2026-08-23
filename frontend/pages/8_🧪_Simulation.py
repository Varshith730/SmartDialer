"""
frontend/pages/8_🧪_Simulation.py
---------------------------------
Page 8: Predefined Scenario Simulations & Comparative Analytics.
"""

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from app.simulation.scenarios import (
    ALL_SCENARIOS,
    SCENARIO_A,
    SCENARIO_B,
    SCENARIO_C,
    run_scenario,
    run_scenario_d,
)
from frontend.components.charts import (
    render_answer_rate_convergence_chart,
    render_pacing_history_chart,
)

st.set_page_config(page_title="Simulation | SmartDialer", page_icon="🧪", layout="wide")

st.title("🧪 Scenario Simulations & Comparative Analytics")
st.markdown("Execute benchmark scenarios (Scenarios A, B, C, D) to compare system resilience, agent utilization, and safety bounding.")

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

    # Top summary metrics
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Calls Initiated", report.total_initiated)
    m2.metric("Calls Answered", report.total_completed)
    m3.metric("Calls Failed", report.total_failed)
    m4.metric("Observed Answer Rate", f"{report.observed_answer_rate:.1%}")

    st.divider()

    # Analytics charts
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

    st.divider()
    st.subheader("Raw Cycle Metrics Table")
    df_cycles = pd.DataFrame([
        {
            "Cycle": m.cycle_num,
            "Available Agents": m.agents_available,
            "Connected Agents": m.agents_connected,
            "Requested": m.requested_calls,
            "Approved": m.approved_calls,
            "Inflight": m.calls_inflight,
            "Completed": m.calls_completed_total,
            "Failed": m.calls_failed_total,
            "EMA Rate": f"{m.smoothed_answer_rate:.1%}",
            "SC Decision": m.sc_decision,
        }
        for m in report.cycles
    ])
    st.dataframe(df_cycles, use_container_width=True, hide_index=True)
