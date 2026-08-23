"""
frontend/components/tables.py
-----------------------------
Formatted tables and badge representations for agents, calls, and decisions.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from app.models.agent import Agent, AgentState
from app.models.call import Call, CallState
from app.safety.controller import SafetyDecision


def format_agent_state(state: AgentState) -> str:
    """Format AgentState with visual status indicator icon."""
    icons = {
        AgentState.AVAILABLE: "🟢 AVAILABLE",
        AgentState.RESERVED:  "🟡 RESERVED",
        AgentState.DIALING:   "🔵 DIALING",
        AgentState.CONNECTED: "🟣 CONNECTED",
        AgentState.WRAP_UP:   "🟠 WRAP_UP",
        AgentState.PAUSED:    "🔴 PAUSED",
        AgentState.OFFLINE:   "⚪ OFFLINE",
    }
    return icons.get(state, state.value)


def format_call_state(state: CallState) -> str:
    """Format CallState with visual icon."""
    icons = {
        CallState.QUEUED:    "⏳ QUEUED",
        CallState.RESERVED:  "🟡 RESERVED",
        CallState.INITIATED: "📞 INITIATED",
        CallState.RINGING:   "🔔 RINGING",
        CallState.ANSWERED:  "🗣️ ANSWERED",
        CallState.CONNECTED: "🟣 CONNECTED",
        CallState.COMPLETED: "✅ COMPLETED",
        CallState.FAILED:    "❌ FAILED",
        CallState.CANCELLED: "🚫 CANCELLED",
    }
    return icons.get(state, state.value)


def render_agents_table(agents: list[Agent]):
    """Render structured table of agents."""
    if not agents:
        st.info("No agents found.")
        return

    data = []
    for a in agents:
        data.append({
            "Agent ID": a.id[:8] + "...",
            "Name": a.name,
            "State": format_agent_state(a.state),
            "Current Call": (a.call_id[:8] + "...") if a.call_id else "—",
            "Borrower": (a.borrower_id[:8] + "...") if a.borrower_id else "—",
            "Reservation": (a.reservation_id[:8] + "...") if a.reservation_id else "—",
            "Lease Expiry": a.lease_until.strftime("%H:%M:%S") if a.lease_until else "—",
        })

    df = pd.DataFrame(data)
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_calls_table(calls: list[Call], max_rows: int = 50):
    """Render structured table of calls."""
    if not calls:
        st.info("No calls in store yet.")
        return

    data = []
    for c in sorted(calls, key=lambda x: x.created_at, reverse=True)[:max_rows]:
        data.append({
            "Call ID": c.id[:8] + "...",
            "State": format_call_state(c.state),
            "Agent": (c.agent_id[:8] + "...") if c.agent_id else "—",
            "Borrower": (c.borrower_id[:8] + "...") if c.borrower_id else "—",
            "Provider": c.provider,
            "Version": c.version,
            "Events": len(c.processed_event_ids),
            "Created At": c.created_at.strftime("%H:%M:%S"),
            "Reason": c.failure_reason if c.failure_reason else "—",
        })

    df = pd.DataFrame(data)
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_safety_decisions_table(decisions: list[SafetyDecision], max_rows: int = 25):
    """Render structured table of safety controller decisions."""
    if not decisions:
        st.info("No safety decisions recorded yet.")
        return

    decision_icons = {
        "APPROVE": "✅ APPROVE",
        "REDUCE":  "⚠️ REDUCE",
        "REJECT":  "🚫 REJECT",
        "FALLBACK_PROGRESSIVE": "🔄 FALLBACK",
    }

    data = []
    for d in reversed(decisions[-max_rows:]):
        data.append({
            "Timestamp": d.timestamp.strftime("%H:%M:%S"),
            "Decision": decision_icons.get(d.decision_type.value, d.decision_type.value),
            "Requested": d.requested_count,
            "Safe Capacity": d.safe_capacity,
            "Approved": d.approved_count,
            "Available Agents": d.available_agents,
            "Inflight Calls": d.inflight_calls,
            "Reason": d.reason,
        })

    df = pd.DataFrame(data)
    st.dataframe(df, use_container_width=True, hide_index=True)
