"""
frontend/components/charts.py
-----------------------------
Plotly interactive visualizations for SmartDialer.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


def render_agent_distribution_chart(state_counts: dict[str, int]):
    """Render a donut chart of agent state distribution."""
    df = pd.DataFrame(list(state_counts.items()), columns=["State", "Count"])
    # Filter out 0 counts for clean chart
    df = df[df["Count"] > 0]
    
    color_map = {
        "AVAILABLE": "#10B981",  # Emerald Green
        "RESERVED": "#F59E0B",   # Amber
        "DIALING": "#3B82F6",    # Blue
        "CONNECTED": "#8B5CF6",  # Violet
        "WRAP_UP": "#EC4899",    # Pink
        "PAUSED": "#EF4444",     # Red
        "OFFLINE": "#6B7280",    # Gray
    }
    
    fig = px.pie(
        df,
        values="Count",
        names="State",
        hole=0.45,
        color="State",
        color_discrete_map=color_map,
        title="Live Agent State Distribution",
    )
    fig.update_layout(
        margin=dict(l=20, r=20, t=40, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=-0.2, xanchor="center", x=0.5),
        height=320,
    )
    st.plotly_chart(fig, use_container_width=True)


def render_pacing_history_chart(cycles_data: list[dict]):
    """Render time series of requested vs approved vs inflight calls."""
    if not cycles_data:
        st.info("No cycle history available yet. Advance cycles to generate metrics.")
        return

    df = pd.DataFrame(cycles_data)
    fig = go.Figure()
    
    if "cycle_num" in df.columns:
        x_axis = df["cycle_num"]
        
        if "requested_calls" in df.columns:
            fig.add_trace(go.Scatter(x=x_axis, y=df["requested_calls"], mode="lines+markers", name="Pacing Requested", line=dict(color="#F59E0B", width=2)))
        if "approved_calls" in df.columns:
            fig.add_trace(go.Scatter(x=x_axis, y=df["approved_calls"], mode="lines+markers", name="Safety Approved", line=dict(color="#10B981", width=3)))
        if "calls_inflight" in df.columns:
            fig.add_trace(go.Scatter(x=x_axis, y=df["calls_inflight"], mode="lines", name="Calls In-Flight", line=dict(color="#3B82F6", dash="dash")))

        fig.update_layout(
            title="Pacing vs Safety Approval Over Time",
            xaxis_title="Cycle Number",
            yaxis_title="Call Count",
            margin=dict(l=20, r=20, t=40, b=20),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            height=340,
        )
        st.plotly_chart(fig, use_container_width=True)


def render_answer_rate_convergence_chart(cycles_data: list[dict]):
    """Render EMA answer rate convergence over cycles."""
    if not cycles_data:
        return

    df = pd.DataFrame(cycles_data)
    if "smoothed_answer_rate" in df.columns and "cycle_num" in df.columns:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=df["cycle_num"],
            y=df["smoothed_answer_rate"] * 100,
            mode="lines+markers",
            name="EMA Answer Rate (%)",
            line=dict(color="#8B5CF6", width=3),
        ))
        fig.update_layout(
            title="EMA Answer Rate Convergence (%)",
            xaxis_title="Cycle Number",
            yaxis_title="Answer Rate (%)",
            yaxis=dict(range=[0, 100]),
            margin=dict(l=20, r=20, t=40, b=20),
            height=300,
        )
        st.plotly_chart(fig, use_container_width=True)
