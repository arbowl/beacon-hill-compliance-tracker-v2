"""Shared fixtures."""

from pathlib import Path

import pytest

from beacon.evaluator.clauses import ClauseIndex

RULES_DIR = Path(__file__).resolve().parents[1] / "rules"


@pytest.fixture(scope="session")
def clause_index() -> ClauseIndex:
    return ClauseIndex.load(RULES_DIR)
