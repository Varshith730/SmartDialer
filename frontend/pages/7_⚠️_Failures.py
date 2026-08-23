"""
frontend/pages/7_⚠️_Failures.py
-------------------------------
Page 7: Interactive Failure Injections & Chaos Engineering.
"""

import streamlit as st

from app.models.call import CallState
from app.providers.provider_b import ProviderB
from frontend.state import get_runner

st.set_page_config(page_title="Failures | SmartDialer", page_icon="⚠️", layout="wide")
runner = get_runner()

st.title("⚠️ Chaos & Failure Injections")
st.markdown("Trigger real backend fault conditions to test the Safety Controller, Circuit Breaker, Reconciler, and Event Processor.")

st.warning("All actions below execute real backend state changes. The UI will reflect actual system reactions.")

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
        from app.models.call import Call

        agents = [a for a in runner.store.list_agents() if a.state.value == "AVAILABLE"]
        borrowers = runner.store.list_dialable_borrowers(runner.campaign.id)
        if agents and borrowers:
            a = agents[0]
            b = borrowers[0]
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
        else:
            st.info("Need available agents and borrowers to inject worker crash.")

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
