"""
tests/test_concurrency.py
--------------------------
Concurrency tests for the StateStore.

The most critical invariant:
    Only ONE worker may reserve the same agent, even when many workers
    attempt the reservation simultaneously.

How the test works:
    1. Create N worker threads, all targeting the SAME agent.
    2. All threads race to call atomic_reserve_agent.
    3. We collect True/False results.
    4. Assert: exactly ONE result is True.

This is the in-memory equivalent of the PostgreSQL optimistic-concurrency
WHERE state = 'AVAILABLE' pattern.

We also test borrower concurrency with the same approach.
"""

import threading
import pytest

from app.models.agent import Agent, AgentState
from app.models.borrower import Borrower, BorrowerStatus
from app.repository.state_store import StateStore


class TestAgentConcurrency:

    def _race_reserve(self, store: StateStore, agent_id: str, n_workers: int) -> list[bool]:
        """
        Launch n_workers threads all trying to reserve the same agent.
        Collect their results and return the list.
        """
        results: list[bool] = [False] * n_workers
        barrier = threading.Barrier(n_workers)  # ensure all threads start together

        def worker(index: int):
            barrier.wait()  # synchronise all threads at the starting line
            res_id = f"reservation-{index}"
            results[index] = store.atomic_reserve_agent(agent_id, res_id, lease_seconds=30)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        return results

    def test_only_one_worker_reserves_agent_among_two(self):
        """Two workers race — exactly one wins."""
        store = StateStore()
        agent = Agent(name="Contested")
        store.save_agent(agent)

        results = self._race_reserve(store, agent.id, n_workers=2)

        winners = [r for r in results if r is True]
        assert len(winners) == 1, f"Expected 1 winner, got {winners}"
        assert store.get_agent(agent.id).state == AgentState.RESERVED

    def test_only_one_worker_reserves_agent_among_ten(self):
        """Ten workers race — exactly one wins."""
        store = StateStore()
        agent = Agent(name="HotAgent")
        store.save_agent(agent)

        results = self._race_reserve(store, agent.id, n_workers=10)

        winners = sum(1 for r in results if r is True)
        assert winners == 1, f"Expected 1 winner among 10 workers, got {winners}"

    def test_only_one_worker_reserves_agent_among_fifty(self):
        """Fifty workers race — exactly one wins."""
        store = StateStore()
        agent = Agent(name="VeryHotAgent")
        store.save_agent(agent)

        results = self._race_reserve(store, agent.id, n_workers=50)

        winners = sum(1 for r in results if r is True)
        assert winners == 1, f"Expected 1 winner among 50 workers, got {winners}"

    def test_after_release_another_worker_can_reserve(self):
        """After the winning worker releases the agent, a new reservation succeeds."""
        store = StateStore()
        agent = Agent(name="Recyclable")
        store.save_agent(agent)

        results = self._race_reserve(store, agent.id, n_workers=5)
        assert sum(1 for r in results if r) == 1

        # Release the agent.
        store.release_agent(agent.id)

        # A new worker can now reserve it.
        ok = store.atomic_reserve_agent(agent.id, "new-reservation")
        assert ok is True
        assert store.get_agent(agent.id).state == AgentState.RESERVED

    def test_many_agents_independent_reservations_all_succeed(self):
        """
        If each worker targets a DIFFERENT agent, all reservations should succeed.
        This verifies that per-agent locking does not create cross-agent contention.
        """
        store = StateStore()
        n = 20
        agents = [Agent(name=f"Agent-{i}") for i in range(n)]
        for a in agents:
            store.save_agent(a)

        results: list[bool] = [False] * n
        barrier = threading.Barrier(n)

        def worker(index: int, agent_id: str):
            barrier.wait()
            results[index] = store.atomic_reserve_agent(agent_id, f"res-{index}")

        threads = [
            threading.Thread(target=worker, args=(i, agents[i].id))
            for i in range(n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(results), "Every worker targeting a unique agent should succeed"


class TestBorrowerConcurrency:
    """Same locking pattern applies to borrowers."""

    def test_only_one_worker_reserves_borrower(self):
        store = StateStore()
        borrower = Borrower(name="Debtor-X")
        store.save_borrower(borrower)

        results: list[bool] = [False] * 20
        barrier = threading.Barrier(20)

        def worker(i: int):
            barrier.wait()
            results[i] = store.atomic_reserve_borrower(borrower.id, f"res-{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = sum(1 for r in results if r)
        assert winners == 1, f"Borrower race: expected 1 winner, got {winners}"
        assert store.get_borrower(borrower.id).status == BorrowerStatus.RESERVED
