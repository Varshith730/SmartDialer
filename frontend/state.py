"""
frontend/state.py
-----------------
Session state management for the SmartDialer Streamlit dashboard.
Maintains a shared SimulationRunner singleton in st.session_state.
"""

from __future__ import annotations

import streamlit as st

from app.models.campaign import DialMode
from app.simulation.runner import SimulationConfig, SimulationRunner


def get_runner() -> SimulationRunner:
    """Retrieve or initialize the active SimulationRunner in session state."""
    if "runner" not in st.session_state:
        init_runner()
    return st.session_state["runner"]


def init_runner(
    n_agents: int = 20,
    n_borrowers: int = 100,
    answer_rate: float = 0.55,
    dial_mode: DialMode = DialMode.PREDICTIVE,
    provider: str = "provider_a",
) -> SimulationRunner:
    """Initialize a fresh SimulationRunner and store in session_state."""
    config = SimulationConfig(
        campaign_name="SmartDialer Live Control Center",
        dial_mode=dial_mode,
        provider=provider,
        n_agents=n_agents,
        n_borrowers=n_borrowers,
        answer_rate=answer_rate,
        ring_time=0.8,
        talk_time=1.5,
        delay_scale=0.05,
        cycle_interval=0.0,
        wrap_up_seconds=0.0,
        verbose=False,
    )
    runner = SimulationRunner(config)
    st.session_state["runner"] = runner
    st.session_state["auto_run"] = False
    return runner
