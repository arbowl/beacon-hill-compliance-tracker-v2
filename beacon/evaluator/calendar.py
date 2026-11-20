"""Session-relative dates as the rules phrase them."""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from enum import IntEnum, StrEnum

_FIRST_SESSION_NUMBER = 194
_FIRST_SESSION_YEAR = 2025
_ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}


class AnnualSession(StrEnum):
    """The two annual sessions of a General Court."""

    FIRST = "first"
    SECOND = "second"


class Weekday(IntEnum):
    """Weekdays numbered as datetime.weekday()."""

    MONDAY = 0
    TUESDAY = 1
    WEDNESDAY = 2
    THURSDAY = 3
    FRIDAY = 4


@dataclass(frozen=True)
class CalendarDate:
    """A computed date and the rule phrasing that produced it."""

    value: date
    description: str


@dataclass(frozen=True)
class SessionCalendar:
    """Dates within a two-year General Court."""

    session: int

    def year(self, annual: AnnualSession) -> int:
        """Calendar year of an annual session."""
        first = _FIRST_SESSION_YEAR + 2 * (self.session - _FIRST_SESSION_NUMBER)
        return first if annual == AnnualSession.FIRST else first + 1

    def _describe(self, phrase: str, annual: AnnualSession, value: date) -> str:
        return f"{phrase} of the {annual.value} annual session ({value.isoformat()})"

    def nth_weekday(
        self, n: int, weekday: Weekday, month: int, annual: AnnualSession
    ) -> CalendarDate:
        """The nth given weekday of a month, e.g. third Wednesday in December."""
        first = date(self.year(annual), month, 1)
        offset = (weekday - first.weekday()) % 7
        value = first + timedelta(days=offset + 7 * (n - 1))
        if value.month != month:
            raise ValueError(f"No {_ORDINALS[n]} {weekday.name.title()} in month")
        phrase = (
            f"{_ORDINALS[n]} {weekday.name.title()} in {calendar.month_name[month]}"
        )
        return CalendarDate(value, self._describe(phrase, annual, value))

    def last_weekday(
        self, weekday: Weekday, month: int, annual: AnnualSession
    ) -> CalendarDate:
        """The last given weekday of a month, e.g. last Wednesday of January."""
        year = self.year(annual)
        last = date(year, month, calendar.monthrange(year, month)[1])
        value = last - timedelta(days=(last.weekday() - weekday) % 7)
        phrase = f"last {weekday.name.title()} of {calendar.month_name[month]}"
        return CalendarDate(value, self._describe(phrase, annual, value))

    def fixed(self, month: int, day: int, annual: AnnualSession) -> CalendarDate:
        """A fixed month and day, e.g. October 1."""
        value = date(self.year(annual), month, day)
        phrase = f"{calendar.month_name[month]} {day}"
        return CalendarDate(value, self._describe(phrase, annual, value))
