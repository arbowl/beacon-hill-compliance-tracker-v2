"""Tenures that owe nothing: reported apart from labels, with why."""

from datetime import datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

from beacon.adapters.sqlite_store import SqliteStore
from beacon.domain.evaluation import NoRequirementsReason as N
from beacon.domain.ids import BillKey
from beacon.domain.inventory import BillRecord
from beacon.domain.observations import ActionType as A
from beacon.domain.observations import Branch
from beacon.evaluator.clauses import ClauseIndex
from beacon.evaluator.engine import evaluate, no_requirements
from beacon.evaluator.facts import project
from beacon.evaluator.rulesets.s194.ruleset import RULESET
from beacon.ports.store import RunStatus, Stage
from beacon.reporters.audit_html import AuditReporter
from beacon.reporters.base import ReportInput
from beacon.reporters.site_bundle import build_bundle
from tests.builders import history, row

AS_OF = datetime(2025, 12, 1, 12, tzinfo=ZoneInfo("America/New_York"))
RUN = UUID(int=7)
S23 = [
    row("2025-03-06", A.REFERRED, "J10", branch=Branch.SENATE),
    row("2025-06-18", A.REPORTED, None, branch=Branch.SENATE),
    row(
        "2025-07-17", A.REFERRED_TO_BILLS_IN_THIRD_READING, "S31", branch=Branch.SENATE
    ),
    row("2025-09-18", A.READ_THIRD, "S31", branch=Branch.SENATE),
    row("2025-10-16", A.REFERRED_TO_BILLS_IN_THIRD_READING, "H36"),
    row("2025-11-06", A.READ_THIRD, "H36"),
]


def _owed_nothing(rows, bill_id: str, committee_id: str):
    facts = project(BillKey(194, bill_id), [history(bill_id, rows)])
    return facts, no_requirements(facts, committee_id, RULESET, AS_OF, RUN)


@pytest.mark.parametrize("committee_id", ["S31", "H36"])
def test_third_reading_committees_owe_nothing(
    committee_id: str, clause_index: ClauseIndex
):
    facts, (item,) = _owed_nothing(S23, "S23", committee_id)
    assert item.reason == N.NONE_APPLY and item.awaiting == ()
    assert evaluate(facts, committee_id, RULESET, clause_index, AS_OF, RUN) == []


def test_open_house_tenure_awaits_its_vote_record():
    _, (item,) = _owed_nothing([row("2025-08-04", A.REFERRED, "H34")], "S2576", "H34")
    assert item.reason == N.NONE_YET
    assert item.awaiting == ("votes_posted",)


def test_discharged_house_tenure_awaits_nothing():
    rows = [row("2025-08-04", A.REFERRED, "H33"), row("2025-09-01", A.REFERRED, "H34")]
    _, (item,) = _owed_nothing(rows, "H1", "H33")
    assert item.reason == N.NONE_APPLY


def test_tenure_with_an_obligation_is_not_listed():
    _, found = _owed_nothing(S23, "S23", "J10")
    assert found == []


def test_run_with_only_owed_nothing_is_latest_and_reports(
    tmp_path: Path, clause_index: ClauseIndex
):
    store = SqliteStore(tmp_path / "beacon.db")
    run_id = store.runs.start(Stage.EVALUATE, "0", "cfg")
    facts = project(BillKey(194, "S23"), [history("S23", S23)])
    items = no_requirements(facts, "S31", RULESET, AS_OF, run_id)
    store.evaluations.append_no_requirements(items)
    store.runs.finish(run_id, RunStatus.SUCCEEDED, {})
    assert store.evaluations.latest_run(194, "S31", "s194") == run_id
    assert store.evaluations.no_requirements_for_run(run_id) == items
    store.close()

    report = ReportInput(
        session=194,
        committee_id="S31",
        committee_name="Senate Committee on Bills in the Third Reading",
        evaluations=[],
        bills=[
            BillRecord(
                session=194,
                bill_id="S23",
                url="https://malegislature.gov/Bills/194/S23",
                title="An Act enabling alternates",
            )
        ],
        committee_url="https://malegislature.gov/Committees/Detail/S31",
        no_requirements=items,
    )
    bundle = build_bundle(report, clause_index)
    assert bundle.tenures == ()
    assert [b.bill_id for b in bundle.bills] == ["S23"]
    (published,) = bundle.no_requirements
    assert (published.tenure_id, published.reason.value) == (
        items[0].tenure_id,
        "none_apply",
    )

    (path,) = AuditReporter(clause_index).write(report, tmp_path / "out")
    html = path.read_text(encoding="utf-8")
    assert "No applicable requirements" in html
    assert "No tracked rule applies to this referral." in html
