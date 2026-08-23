"""
tests/conftest.py
-----------------
Shared pytest fixtures available to all test modules.

Phase 1: A fresh StateStore for each test that requests it.
"""

import pytest
from app.repository.state_store import StateStore


@pytest.fixture
def store() -> StateStore:
    """Return a clean, empty StateStore for a single test."""
    return StateStore()
