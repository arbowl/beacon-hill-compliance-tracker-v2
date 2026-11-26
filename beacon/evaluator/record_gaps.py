"""Gaps in the legislative record, each reviewed by hand against its sources.

A committee can list a bill whose history never records how the bill came before
it. Usually the extractor is at fault and is fixed; these are the cases where the
published record itself is silent. Each entry names its evidence in `note`, and
applies only while the history still shows no tenure before the committee: once
the record is corrected, or the extractor learns to read it, the entry goes stale
and is ignored.
"""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class SuppliedReferral:
    """A referral the history omits, though the committee demonstrably held the
    bill. It is supplied on referred_on, just before the row (anchor_on, starting
    anchor_prefix) that records the committee's report, where the tenure ends."""

    session: int
    bill_id: str
    committee_id: str
    referred_on: date
    anchor_on: date
    anchor_prefix: str
    note: str


@dataclass(frozen=True)
class UnrecordedTransfer:
    """A transfer to the committee that the history never records. Nothing about
    the tenure can be judged; it owes nothing and says why."""

    session: int
    bill_id: str
    committee_id: str
    transferred_on: date
    note: str


RecordGap = SuppliedReferral | UnrecordedTransfer

RECORD_GAPS: tuple[RecordGap, ...] = (
    SuppliedReferral(
        session=194,
        bill_id="S3188",
        committee_id="H34",
        referred_on=date(2026, 7, 30),
        anchor_on=date(2026, 7, 31),
        anchor_prefix="Committee recommended ought to pass with an amendment",
        note=(
            "The House Committee on Ways and Means recorded a vote on the bill on "
            "July 31, 2026, but the bill history omits its referral there."
        ),
    ),
    UnrecordedTransfer(
        session=194,
        bill_id="H4319",
        committee_id="J45",
        transferred_on=date(2025, 6, 27),
        note=(
            "Referred to the Joint Committee on Agriculture before that committee "
            "was retired; the bill history does not record its transfer to the "
            "Joint Committee on Agriculture and Fisheries."
        ),
    ),
)


def gaps_for(
    gaps: tuple[RecordGap, ...], session: int, bill_id: str
) -> tuple[RecordGap, ...]:
    """The entries for one bill."""
    return tuple(g for g in gaps if (g.session, g.bill_id) == (session, bill_id))
