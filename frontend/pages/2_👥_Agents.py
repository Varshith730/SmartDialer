"""
frontend/pages/2_👥_Agents.py
-----------------------------
Page 2: Live Agent Monitoring.
"""

import streamlit as st

from app.models.agent import AgentState
from frontend.components.tables import render_agents_table
from frontend.state import get_runner

st.set_page_config(page_title="Agents | SmartDialer", page_icon="👥", layout="wide")
runner = get_runner()

st.title("👥 Agent State Monitor")
st.markdown("Track atomic agent states across dialling lifecycles to ensure zero double-booking.")

agents = runner.store.list_agents()
agent_counts = {s: 0 for s in AgentState}
for a in agents:
    agent_counts[a.state] += 1

# Summary badge counters
c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
c1.metric("🟢 AVAILABLE", agent_counts[AgentState.AVAILABLE])
c2.metric("🟡 RESERVED", agent_counts[AgentState.RESERVED])
c3.metric("🔵 DIALING", agent_counts[AgentState.DIALING])
c4.metric("🟣 CONNECTED", agent_counts[AgentState.CONNECTED])
c5.metric("🟠 WRAP_UP", agent_counts[AgentState.WRAP_UP])
c6.metric("🔴 PAUSED", agent_counts[AgentState.PAUSED])
c7.metric("⚪ OFFLINE", agent_counts[AgentState.OFFLINE])

st.divider()

# Filter options
selected_state = st.selectbox(
    "Filter by Agent State:",
    options=["ALL"] + [s.value for s in AgentState],
)

if selected_state != "ALL":
    filtered_agents = [a for a in agents if a.state.value == selected_state]
else:
    filtered_agents = agents

st.caption(f"Showing {len(filtered_agents)} of {len(agents)} agents.")
render_agents_table(filtered_agents)

st.divider()

# ---------------------------------------------------------------------------
# Interactive Agent Management Forms
# ---------------------------------------------------------------------------
st.subheader("🛠️ Interactive Agent Management")
col_form1, col_form2 = st.columns(2)

with col_form1:
    with st.expander("➕ Add New Agent to System", expanded=True):
        new_name = st.text_input("Agent Name:", value=f"Agent-{len(agents)+1:02d}")
        init_state_choice = st.selectbox("Initial State:", ["AVAILABLE", "PAUSED", "OFFLINE"])
        if st.button("➕ Register Agent", use_container_width=True):
            from app.models.agent import Agent, AgentState
            state_enum = getattr(AgentState, init_state_choice)
            new_agent = Agent(name=new_name, state=state_enum)
            runner.store.save_agent(new_agent)
            st.success(f"Agent '{new_name}' created in {init_state_choice} state!")
            st.rerun()

with col_form2:
    with st.expander("⚙️ Modify Agent Status Live", expanded=True):
        if agents:
            agent_dict = {f"{a.name} ({a.id[:8]}... - {a.state.value})": a.id for a in agents}
            selected_agent_label = st.selectbox("Select Agent:", list(agent_dict.keys()))
            target_agent_id = agent_dict[selected_agent_label]
            target_agent = runner.store.get_agent(target_agent_id)
            
            new_status_choice = st.selectbox(
                "Change State To:",
                ["AVAILABLE", "PAUSED", "OFFLINE", "WRAP_UP"],
            )
            if st.button("🔄 Apply State Transition", use_container_width=True):
                target_agent.state = getattr(AgentState, new_status_choice)
                if new_status_choice in ("AVAILABLE", "PAUSED", "OFFLINE"):
                    target_agent.call_id = None
                    target_agent.borrower_id = None
                    target_agent.reservation_id = None
                    target_agent.lease_until = None
                runner.store.save_agent(target_agent)
                st.success(f"Updated {target_agent.name} to {new_status_choice}!")
                st.rerun()
        else:
            st.info("No agents registered.")

if agent_counts[AgentState.WRAP_UP] > 0:
    st.info(f"There are **{agent_counts[AgentState.WRAP_UP]}** agent(s) in WRAP_UP.")
    if st.button("🧹 Release All Agents from WRAP_UP to AVAILABLE", type="primary"):
        for a in agents:
            if a.state == AgentState.WRAP_UP:
                runner.processor.complete_wrap_up(a.id)
        st.success("All WRAP_UP agents released back to AVAILABLE!")
        st.rerun()
