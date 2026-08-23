"""
tests/test_agents.py
--------------------
Unit tests for the Agent model and its lifecycle.

Tests verify:
- Default construction
- State helpers (is_reservable, is_active)
- Correct state transitions via the state store
"""

import pytest
from app.models.agent import Agent, AgentState, RESERVABLE_STATES, ACTIVE_STATES
from app.repository.state_store import StateStore


class TestAgentModel:
    """Tests for the Agent dataclass itself (no store needed)."""

    def test_default_state_is_available(self):
        agent = Agent(name="Alice")
        assert agent.state == AgentState.AVAILABLE

    def test_is_reservable_when_available(self):
        agent = Agent()
        assert agent.is_reservable() is True

    def test_not_reservable_when_offline(self):
        agent = Agent(state=AgentState.OFFLINE)
        assert agent.is_reservable() is False

    def test_not_reservable_when_reserved(self):
        agent = Agent(state=AgentState.RESERVED)
        assert agent.is_reservable() is False

    def test_not_reservable_when_connected(self):
        agent = Agent(state=AgentState.CONNECTED)
        assert agent.is_reservable() is False

    def test_is_active_when_reserved(self):
        agent = Agent(state=AgentState.RESERVED)
        assert agent.is_active() is True

    def test_is_active_when_dialing(self):
        agent = Agent(state=AgentState.DIALING)
        assert agent.is_active() is True

    def test_not_active_when_available(self):
        agent = Agent()
        assert agent.is_active() is False

    def test_not_active_when_offline(self):
        agent = Agent(state=AgentState.OFFLINE)
        assert agent.is_active() is False

    def test_agent_has_unique_id(self):
        a1 = Agent()
        a2 = Agent()
        assert a1.id != a2.id

    def test_repr_does_not_raise(self):
        agent = Agent(name="Bob")
        r = repr(agent)
        assert "Bob" in r
        assert "AVAILABLE" in r


class TestAgentStateStore:
    """Tests for agent persistence in the StateStore."""

    def setup_method(self):
        self.store = StateStore()

    def test_save_and_get_agent(self):
        agent = Agent(name="Carol")
        self.store.save_agent(agent)
        fetched = self.store.get_agent(agent.id)
        assert fetched is agent  # same object (in-memory)
        assert fetched.name == "Carol"

    def test_get_nonexistent_agent_returns_none(self):
        result = self.store.get_agent("no-such-id")
        assert result is None

    def test_list_agents_returns_all(self):
        self.store.save_agent(Agent(name="A"))
        self.store.save_agent(Agent(name="B"))
        assert len(self.store.list_agents()) == 2

    def test_list_available_agents_filters_correctly(self):
        a1 = Agent(name="Available1")
        a2 = Agent(name="Available2")
        a3 = Agent(name="Offline", state=AgentState.OFFLINE)
        a4 = Agent(name="Reserved", state=AgentState.RESERVED)
        for a in [a1, a2, a3, a4]:
            self.store.save_agent(a)

        available = self.store.list_available_agents()
        assert len(available) == 2
        names = {a.name for a in available}
        assert names == {"Available1", "Available2"}

    def test_count_agents_by_state(self):
        self.store.save_agent(Agent(state=AgentState.AVAILABLE))
        self.store.save_agent(Agent(state=AgentState.AVAILABLE))
        self.store.save_agent(Agent(state=AgentState.DIALING))
        counts = self.store.count_agents_by_state()
        assert counts["AVAILABLE"] == 2
        assert counts["DIALING"] == 1

    def test_atomic_reserve_succeeds_when_available(self):
        agent = Agent(name="D")
        self.store.save_agent(agent)
        result = self.store.atomic_reserve_agent(agent.id, "res-001", lease_seconds=30)
        assert result is True
        refreshed = self.store.get_agent(agent.id)
        assert refreshed.state == AgentState.RESERVED
        assert refreshed.reservation_id == "res-001"
        assert refreshed.lease_until is not None

    def test_atomic_reserve_fails_when_already_reserved(self):
        agent = Agent(name="E")
        self.store.save_agent(agent)
        # First reservation should succeed.
        ok1 = self.store.atomic_reserve_agent(agent.id, "res-A")
        assert ok1 is True
        # Second reservation on the same agent must fail.
        ok2 = self.store.atomic_reserve_agent(agent.id, "res-B")
        assert ok2 is False
        # Agent must still hold the first reservation.
        assert self.store.get_agent(agent.id).reservation_id == "res-A"

    def test_atomic_reserve_fails_for_missing_agent(self):
        result = self.store.atomic_reserve_agent("ghost-id", "res-X")
        assert result is False

    def test_release_agent_restores_available(self):
        agent = Agent(name="F")
        self.store.save_agent(agent)
        self.store.atomic_reserve_agent(agent.id, "res-Y")
        assert self.store.get_agent(agent.id).state == AgentState.RESERVED

        self.store.release_agent(agent.id)
        refreshed = self.store.get_agent(agent.id)
        assert refreshed.state == AgentState.AVAILABLE
        assert refreshed.reservation_id is None
        assert refreshed.lease_until is None

    def test_release_then_re_reserve(self):
        """After a release an agent should be reservable again."""
        agent = Agent(name="G")
        self.store.save_agent(agent)
        self.store.atomic_reserve_agent(agent.id, "res-1")
        self.store.release_agent(agent.id)
        ok = self.store.atomic_reserve_agent(agent.id, "res-2")
        assert ok is True
        assert self.store.get_agent(agent.id).reservation_id == "res-2"
