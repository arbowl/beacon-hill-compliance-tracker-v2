"""Tests for session calendar date computation."""

from datetime import date

import pytest

from beacon.evaluator.calendar import AnnualSession, SessionCalendar, Weekday


@pytest.fixture
def cal() -> SessionCalendar:
    return SessionCalendar(194)


def test_session_years(cal: SessionCalendar):
    assert cal.year(AnnualSession.FIRST) == 2025
    assert cal.year(AnnualSession.SECOND) == 2026
    assert SessionCalendar(195).year(AnnualSession.FIRST) == 2027


@pytest.mark.parametrize(
    ("n", "month", "session", "expected"),
    [
        (1, 12, AnnualSession.FIRST, date(2025, 12, 3)),
        (3, 12, AnnualSession.FIRST, date(2025, 12, 17)),
        (4, 12, AnnualSession.FIRST, date(2025, 12, 24)),
        (3, 3, AnnualSession.SECOND, date(2026, 3, 18)),
    ],
)
def test_nth_wednesday(cal, n, month, session, expected):
    assert cal.nth_weekday(n, Weekday.WEDNESDAY, month, session).value == expected


@pytest.mark.parametrize(
    ("month", "expected"),
    [(1, date(2026, 1, 28)), (5, date(2026, 5, 27))],
)
def test_last_wednesday(cal, month, expected):
    result = cal.last_weekday(Weekday.WEDNESDAY, month, AnnualSession.SECOND)
    assert result.value == expected


def test_descriptions_are_readable(cal: SessionCalendar):
    d = cal.nth_weekday(3, Weekday.WEDNESDAY, 12, AnnualSession.FIRST)
    assert d.description == (
        "third Wednesday in December of the first annual session (2025-12-17)"
    )
    fixed = cal.fixed(10, 1, AnnualSession.FIRST)
    assert fixed.value == date(2025, 10, 1)
    assert fixed.description == "October 1 of the first annual session (2025-10-01)"
