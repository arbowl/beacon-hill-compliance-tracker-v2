"""Source of the current time."""

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """Returns the current time as an aware datetime."""

    def now(self) -> datetime: ...
