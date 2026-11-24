"""Extracts classified history rows from a captured bill page."""

import re
from datetime import date, datetime
from uuid import NAMESPACE_URL, uuid5

from bs4 import BeautifulSoup

from beacon.domain.captures import Capture
from beacon.domain.observations import (
    ActionType,
    BillHistoryPayload,
    Branch,
    HistoryRow,
    Observation,
)
from beacon.scraper.history.committees import (
    THIRD_READING_COMMITTEES,
    normalize_committee_name,
)
from beacon.scraper.history.nodes import ACTION_NODES
from beacon.scraper.html import parse_html

EXTRACTOR_ID = "bill_history"
EXTRACTOR_VERSION = 10
_DATE_FORMATS = ("%m/%d/%Y", "%B %d, %Y")
_NODES = sorted(ACTION_NODES, key=lambda n: n.priority)
_BILL_MENTION = re.compile(r"\b([HS])\.?\s?(\d{1,5})\b")
_OF_THIRD_READING = frozenset(
    {ActionType.REFERRED_TO_BILLS_IN_THIRD_READING, ActionType.READ_THIRD}
)


def _parse_date(text: str) -> date | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _iso(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def classify(action_text: str) -> list[tuple[ActionType, dict[str, object]]]:
    """Every node matching the text, in priority order; UNKNOWN if none."""
    matches = []
    for node in _NODES:
        match = node.match(action_text)
        if match:
            matches.append((ActionType(node.action_type), node.extract_data(match)))
    return matches or [(ActionType.UNKNOWN, {})]


def related_bills(text: str, own_bill_id: str | None = None) -> tuple[str, ...]:
    """Bill ids mentioned in action text, in order, excluding the bill itself."""
    found = (f"{m.group(1)}{int(m.group(2))}" for m in _BILL_MENTION.finditer(text))
    return tuple(dict.fromkeys(b for b in found if b != own_bill_id))


def _title(soup: BeautifulSoup) -> str | None:
    for h2 in soup.find_all("h2"):
        parent = h2.parent
        classes = (parent.get("class") or []) if parent is not None else []
        if any(c.startswith("col-md-") for c in classes):
            return " ".join(h2.get_text(" ", strip=True).split())
    return None


def _committee_id(
    action_type: ActionType, data: dict[str, object], branch: Branch, on: date
) -> str | None:
    """The committee a row concerns; Bills in the Third Reading by its branch."""
    raw = data.get("committee")
    committee_id = (
        normalize_committee_name(raw, on)
        if isinstance(raw, str)
        else data.get("committee_id")
    )
    if (
        action_type in _OF_THIRD_READING
        or data.get("third_reading")
        or committee_id in THIRD_READING_COMMITTEES.values()
    ):
        return THIRD_READING_COMMITTEES.get(branch.value)
    return committee_id if isinstance(committee_id, str) else None


def parse_history(body: bytes, bill_id: str | None = None) -> BillHistoryPayload:
    """Classify each dated row of the bill page's history table."""
    soup = parse_html(body)
    rows: list[HistoryRow] = []
    index = 0
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 3:
            continue
        date_text, branch_text, action_text = (
            " ".join(c.get_text(" ", strip=True).split()) for c in cells[:3]
        )
        action_date = _parse_date(date_text)
        if action_date is None:
            continue
        mentioned = related_bills(action_text, bill_id)
        branch = Branch.parse(branch_text)
        for action_type, data in classify(action_text):
            rows.append(
                HistoryRow(
                    row_index=index,
                    action_date=action_date,
                    branch=branch,
                    raw_text=action_text,
                    action_type=action_type,
                    committee_id=_committee_id(action_type, data, branch, action_date),
                    hearing_date=_iso(data.get("hearing_date")),
                    new_deadline=_iso(data.get("new_deadline")),
                    related_bills=mentioned,
                )
            )
        index += 1
    return BillHistoryPayload(title=_title(soup), rows=tuple(rows))


def extract(capture: Capture, body: bytes, session: int, bill_id: str) -> Observation:
    """One observation per capture; the id is deterministic for idempotent runs."""
    key = f"{capture.capture_id}|{EXTRACTOR_ID}|{EXTRACTOR_VERSION}"
    return Observation(
        observation_id=uuid5(NAMESPACE_URL, key),
        capture_id=capture.capture_id,
        observed_at=capture.requested_at,
        extractor_id=EXTRACTOR_ID,
        extractor_version=EXTRACTOR_VERSION,
        session=session,
        bill_id=bill_id,
        payload=parse_history(body, bill_id),
    )
