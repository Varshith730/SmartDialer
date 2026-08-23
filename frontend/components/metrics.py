"""
frontend/components/metrics.py
------------------------------
Reusable KPI metric card components.
"""

from __future__ import annotations

import streamlit as st


def render_kpi_card(title: str, value: str, delta: str = "", subtitle: str = ""):
    """Render a styled KPI metric box."""
    st.metric(label=title, value=value, delta=delta if delta else None)
    if subtitle:
        st.caption(subtitle)
