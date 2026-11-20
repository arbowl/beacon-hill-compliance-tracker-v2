"""Committees and bills known to the collector."""

from pydantic import BaseModel, ConfigDict

from beacon.domain.ids import Chamber


class CommitteeRecord(BaseModel):
    """A committee as listed on the Legislature's site."""

    model_config = ConfigDict(frozen=True)

    session: int
    committee_id: str
    name: str
    chamber: Chamber
    url: str


class BillRecord(BaseModel):
    """A bill as listed on the Legislature's site."""

    model_config = ConfigDict(frozen=True)

    session: int
    bill_id: str
    url: str
    title: str | None = None
