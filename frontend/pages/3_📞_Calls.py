"""
frontend/pages/3_📞_Calls.py
----------------------------
Page 3: Live Call Records & State Transitions.
"""

import streamlit as st

from app.models.call import CallState
from frontend.components.tables import render_calls_table
from frontend.state import get_runner

st.set_page_config(page_title="Calls | SmartDialer", page_icon="📞", layout="wide")
runner = get_runner()

st.title("📞 Call Lifecycle Records")
st.markdown("Inspect call transitions, provider attributions, version counters, and idempotent event histories.")

calls = runner.store.list_calls(runner.campaign.id)

# Filter Controls
f1, f2, f3 = st.columns(3)
with f1:
    selected_provider = st.selectbox("Provider:", ["ALL", "provider_a", "provider_b"])
with f2:
    selected_state = st.selectbox("Call State:", ["ALL"] + [s.value for s in CallState])
with f3:
    agent_search = st.text_input("Filter by Agent ID (prefix):", value="")

filtered = calls
if selected_provider != "ALL":
    filtered = [c for c in filtered if c.provider == selected_provider]
if selected_state != "ALL":
    filtered = [c for c in filtered if c.state.value == selected_state]
if agent_search:
    filtered = [c for c in filtered if c.agent_id and agent_search.lower() in c.agent_id.lower()]

st.caption(f"Showing {len(filtered)} matching calls.")
render_calls_table(filtered, max_rows=100)

st.divider()

# ---------------------------------------------------------------------------
# Interactive Call Dispatch & Borrower Enrolment
# ---------------------------------------------------------------------------
st.subheader("🛠️ Direct Call Dispatch & Borrower Queue")
col_call1, col_call2 = st.columns(2)

with col_call1:
    with st.expander("📞 Initiate Single Direct Call", expanded=True):
        avail_agents = runner.store.list_available_agents()
        dialable_borrowers = runner.store.list_dialable_borrowers(runner.campaign.id)
        
        if avail_agents and dialable_borrowers:
            agent_options = {f"{a.name} ({a.id[:8]}...)": a.id for a in avail_agents}
            borrower_options = {f"{b.name} (Priority {b.priority.name} - {b.id[:8]}...)": b.id for b in dialable_borrowers}
            
            chosen_agent = st.selectbox("Select Available Agent:", list(agent_options.keys()))
            chosen_borrower = st.selectbox("Select Target Borrower:", list(borrower_options.keys()))
            
            if st.button("🚀 Dial Single Call Now", use_container_width=True, type="primary"):
                # Always goes through the Safety Controller first!
                decision = runner.safety_controller.evaluate(1, runner.campaign, runner.provider)
                if decision.approved_count > 0:
                    from app.dialer.allocator import AllocationRequest
                    req = AllocationRequest(
                        agent_id=agent_options[chosen_agent],
                        borrower_id=borrower_options[chosen_borrower],
                        campaign_id=runner.campaign.id,
                        provider_name=runner.provider.name,
                        lease_seconds=runner.config.lease_seconds,
                    )
                    res = runner.allocator.allocate(req, runner.provider, event_callback=runner.processor.process)
                    if res.success:
                        st.success(f"Call initiated successfully! Call ID: {res.call.id[:8]}...")
                    else:
                        st.error(f"Allocation failed: {res.failure_reason}")
                else:
                    st.warning(f"Safety Controller {decision.decision_type.value}: {decision.reason}")
                st.rerun()
        else:
            if not avail_agents:
                st.warning("No available agents right now to dial a call.")
            if not dialable_borrowers:
                st.warning("No pending borrowers in campaign queue.")

with col_call2:
    with st.expander("➕ Enrol New Borrower to Queue", expanded=True):
        b_name = st.text_input("Borrower Full Name:", value="John Doe")
        b_priority = st.selectbox("Urgency / Priority:", ["HIGH", "MEDIUM", "LOW"])
        if st.button("➕ Add Borrower to Campaign Queue", use_container_width=True):
            from app.models.borrower import Borrower, BorrowerPriority
            new_borrower = Borrower(
                name=b_name,
                campaign_id=runner.campaign.id,
                priority=getattr(BorrowerPriority, b_priority),
            )
            runner.store.save_borrower(new_borrower)
            st.success(f"Borrower '{b_name}' added to campaign with priority {b_priority}!")
            st.rerun()
