"""
frontend/pages/6_📡_Providers.py
--------------------------------
Page 6: Telecom Provider Health & Circuit Breakers.
"""

import streamlit as st

from frontend.state import get_runner

st.set_page_config(page_title="Providers | SmartDialer", page_icon="📡", layout="wide")
runner = get_runner()

st.title("📡 Telecom Providers & Circuit Breakers")
st.markdown("Monitor telephony integrations, circuit breaker trip states, latency profiles, and event processor ingestion.")

cb = runner.circuit_breaker
cb_stats = cb.stats()
proc_stats = runner.processor.stats()

col_p1, col_p2 = st.columns(2)

with col_p1:
    st.subheader("Provider A (Reliable Tier)")
    st.markdown("""
    - **Reliability:** High (98% API success rate)
    - **Sequencing:** Clean ordered events (`INITIATED ➔ RINGING ➔ ANSWERED ➔ CONNECTED ➔ COMPLETED`)
    - **Latencies:** 0.5s - 2.0s
    """)
    st.write(f"**Status:** {'🟢 HEALTHY' if runner.provider.name == 'provider_a' and runner.provider.is_healthy() else '⚪ INACTIVE'}")

with col_p2:
    st.subheader("Provider B (Chaotic / Stress Tier)")
    st.markdown("""
    - **Reliability:** Degraded (Simulates timeouts, drops, and API rejections)
    - **Sequencing:** Chaos injection (Duplicate events, out-of-order deliveries)
    - **Latencies:** 1.0s - 4.0s
    """)
    st.write(f"**Status:** {'🟡 ACTIVE (CHAOS)' if runner.provider.name == 'provider_b' else '⚪ INACTIVE'}")

st.divider()

st.subheader(f"Circuit Breaker Status: `{cb_stats['provider']}`")
c1, c2, c3, c4 = st.columns(4)

cb_state_val = cb_stats['state']
c1.metric("State", f"{cb_state_val}", delta="OK" if cb_state_val == "CLOSED" else "TRIPPED")
c2.metric("Failure Threshold", f"{cb.failure_threshold}")
c3.metric("Total Failures", f"{cb_stats['total_failures']}")
c4.metric("Total Successes", f"{cb_stats['total_successes']}")

st.divider()

st.subheader("Event Processor Ingestion Metrics")
st.caption("Verifying Invariant 4 (Idempotency) and Invariant 5 (Out-of-order rejection).")

e1, e2, e3, e4 = st.columns(4)
e1.metric("Total Events Ingested", proc_stats["total_received"])
e2.metric("Valid Transitions Applied", proc_stats["total_applied"])
e3.metric("Duplicate Events Dropped", proc_stats["duplicates_dropped"], help="Idempotency guard dropped these duplicate event_ids")
e4.metric("Out-of-Order Stale Dropped", proc_stats["out_of_order_dropped"], help="Rank check dropped these backwards/stale transitions")
