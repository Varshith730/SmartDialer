"""
frontend/pages/5_🛡️_Safety.py
-----------------------------
Page 5: Safety Controller Authority & Audit Log.
"""

import pandas as pd
import plotly.express as px
import streamlit as st

from frontend.components.tables import render_safety_decisions_table
from frontend.state import get_runner

st.set_page_config(page_title="Safety Controller | SmartDialer", page_icon="🛡️", layout="wide")
runner = get_runner()

st.title("🛡️ Safety Controller & Circuit Guard")
st.markdown("Auditing Safety Controller decisions. The Safety Controller enforces real-time hard capacity limits regardless of statistical pacing.")

st.markdown("""
```
PACING REQUEST (from PredictiveEngine)
       │
       ▼
SAFETY CHECK (Evaluates: available agents, in-flight limits, provider health, circuit breaker)
       │
       ▼
APPROVED CAPACITY (Definitive upper bound)
       │
       ▼
CALL ALLOCATION (Dispatched to CallAllocator)
```
""")

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
