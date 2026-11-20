"""Identifiers for sessions, bills, and committees."""

import re
from dataclasses import dataclass
from enum import StrEnum

_BILL_RX = re.compile(r"^\s*([HS])\s*\.?\s*(\d+)\s*$", re.I)
_COMMITTEE_RX = re.compile(r"^\s*([HSJ])\s*(\d+)\s*$", re.I)


class Chamber(StrEnum):
    """A legislative chamber, or Joint for joint committees."""

    HOUSE = "House"
    SENATE = "Senate"
    JOINT = "Joint"

    @staticmethod
    def from_prefix(prefix: str) -> "Chamber":
        """Map an id prefix letter to a chamber."""
        return {"H": Chamber.HOUSE, "S": Chamber.SENATE, "J": Chamber.JOINT}[
            prefix.upper()
        ]


def normalize_bill_id(raw: str) -> str:
    """Normalize forms like 'H.1234' or 'h 1234' to 'H1234'."""
    match = _BILL_RX.match(raw)
    if not match:
        raise ValueError(f"Invalid bill id: {raw!r}")
    return f"{match.group(1).upper()}{int(match.group(2))}"


def normalize_committee_id(raw: str) -> str:
    """Normalize forms like 'j19' to 'J19'."""
    match = _COMMITTEE_RX.match(raw)
    if not match:
        raise ValueError(f"Invalid committee id: {raw!r}")
    return f"{match.group(1).upper()}{int(match.group(2))}"


@dataclass(frozen=True)
class BillKey:
    """A bill within a session."""

    session: int
    bill_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "bill_id", normalize_bill_id(self.bill_id))

    @property
    def filed_in(self) -> Chamber:
        """The branch the bill was filed in."""
        return Chamber.from_prefix(self.bill_id[0])

    def __str__(self) -> str:
        return f"{self.session}/{self.bill_id}"


@dataclass(frozen=True)
class CommitteeKey:
    """A committee within a session."""

    session: int
    committee_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "committee_id", normalize_committee_id(self.committee_id)
        )

    @property
    def chamber(self) -> Chamber:
        """The chamber the committee belongs to."""
        return Chamber.from_prefix(self.committee_id[0])

    def __str__(self) -> str:
        return f"{self.session}/{self.committee_id}"
