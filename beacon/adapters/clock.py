"""Clock implementations."""

from dataclasses import dataclass
from datetime import UTC, datetime


class SystemClock:
    """The wall clock, in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class FixedClock:
    """A settable clock for tests and replays."""

    current: datetime

    def now(self) -> datetime:
        return self.current
