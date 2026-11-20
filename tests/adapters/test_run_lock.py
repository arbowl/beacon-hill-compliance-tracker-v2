"""Tests for the per-data-directory run lock."""

from pathlib import Path

import pytest

from beacon.adapters.run_lock import RunLock, RunLockedError


def test_second_holder_is_refused_until_release(tmp_path: Path):
    with RunLock(tmp_path), pytest.raises(RunLockedError):
        RunLock(tmp_path).__enter__()
    with RunLock(tmp_path):
        pass
