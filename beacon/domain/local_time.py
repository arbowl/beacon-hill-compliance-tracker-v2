"""The Legislature's local time."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

LEGISLATURE_TZ = ZoneInfo("America/New_York")


def local_date(moment: datetime) -> date:
    """The calendar date at the State House for an aware datetime."""
    return moment.astimezone(LEGISLATURE_TZ).date()
