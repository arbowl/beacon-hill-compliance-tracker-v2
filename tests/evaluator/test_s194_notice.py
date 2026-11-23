"""Hearing notice: JR 1D para 4 (joint), SR 12 para 2 (Senate), HR 17A(e) (House).

Each hearing is judged on the announcement that set its date: 10 days (joint), 5
(Senate), or 72 hours (House). Restatements of the date, corrections posted on or
after the hearing, and time or place updates are not judged. A hearing with no
announcement is Unmet. Announcements before 2025-06-26 are exempt.
"""

from datetime import datetime

from beacon.domain.evaluation import ProvisionalReason as R
from beacon.domain.evaluation import RequirementResult
from beacon.domain.evaluation import RequirementStatus as S
from beacon.domain.ids import BillKey
from beacon.domain.observations import ActionType as A
from beacon.domain.observations import Observation
from beacon.domain.provenance import ClauseRef, RuleSource
from beacon.evaluator.facts import project
from beacon.evaluator.rulesets.s194.notice import HearingNotice
from tests.builders import ET, hearing, history, row

JR1D_P4 = ClauseRef(RuleSource.JOINT, "1D", 4)
SR12_P2 = ClauseRef(RuleSource.SENATE, "12", 2)
HR17A_P15 = ClauseRef(RuleSource.HOUSE, "17A", 15)


def results(
    rows: list, as_of: str = "2026-06-01", committee: str = "J19", dockets=()
) -> list[RequirementResult]:
    observations: list[Observation] = [history("H1", rows)]
    facts = project(BillKey(194, "H1"), observations, dockets)
    (tenure,) = facts.tenures_for(committee)
    moment = datetime.fromisoformat(as_of).replace(hour=12, tzinfo=ET)
    requirement = HearingNotice()
    return [
        requirement.check(o, facts, tenure, moment)
        for o in requirement.obligations(facts, tenure)
    ]


def one(rows, **kwargs) -> RequirementResult:
    (result,) = results(rows, **kwargs)
    return result


def cites(result: RequirementResult, ref: ClauseRef) -> bool:
    return any(s.clause == ref for s in result.derivation)


def referred(committee: str = "J19") -> dict:
    return row("2025-07-01", A.REFERRED, committee)


def scheduled(announced: str, on: str, committee: str = "J19") -> dict:
    return row(announced, A.HEARING_SCHEDULED, committee, hearing_date=on)


class TestJointInitialNotice:
    def test_ten_days_is_met(self):
        result = one([referred(), scheduled("2025-09-01", "2025-09-11")])
        assert result.status == S.MET
        assert cites(result, JR1D_P4)

    def test_nine_days_is_unmet(self):
        result = one([referred(), scheduled("2025-09-02", "2025-09-11")])
        assert result.status == S.UNMET
        assert "9 days" in result.detail

    def test_before_rules_adopted_is_exempt(self):
        rows = [
            row("2025-02-01", A.REFERRED, "J19"),
            scheduled("2025-04-01", "2025-04-08"),
        ]
        result = one(rows)
        assert result.status == S.MET
        assert "exempt" in result.detail


def rescheduled(announced: str, on: str, committee: str = "J19") -> dict:
    return row(announced, A.HEARING_RESCHEDULED, committee, hearing_date=on)


class TestJointAmendments:
    def test_new_date_needs_ten_days(self):
        rows = [
            referred(),
            scheduled("2025-09-01", "2025-09-15"),
            rescheduled("2025-09-10", "2025-09-17"),
        ]
        result = one(rows)
        assert result.status == S.UNMET
        assert result.detail.startswith("New hearing date posted 2025-09-10")
        assert cites(result, JR1D_P4)

    def test_rescheduled_to_same_date_keeps_original_notice(self):
        rows = [
            referred(),
            scheduled("2025-09-01", "2025-09-15"),
            rescheduled("2025-09-13", "2025-09-15"),
        ]
        result = one(rows)
        assert result.status == S.MET
        assert "posted 2025-09-01" in result.detail
        assert any("Restatement" in s.description for s in result.derivation)

    def test_scheduled_again_for_same_date_keeps_original_notice(self):
        rows = [
            referred(),
            scheduled("2025-10-17", "2025-10-29"),
            scheduled("2025-10-28", "2025-10-29"),
            scheduled("2025-10-28", "2025-10-29"),
            [
                rescheduled("2025-10-29", "2025-10-29"),
                row("2025-10-29", A.HEARING_TIME_CHANGED, "J19"),
            ],
        ]
        result = one(rows)
        assert result.status == S.MET
        assert result.detail == (
            "Notice given: Hearing notice posted 2025-10-17 for the 2025-10-29 "
            "hearing: 12 days' notice, 10 days required"
        )

    def test_restatement_cannot_cure_a_late_notice(self):
        rows = [
            referred(),
            scheduled("2025-09-10", "2025-09-15"),
            scheduled("2025-09-12", "2025-09-15"),
        ]
        result = one(rows)
        assert result.status == S.UNMET
        assert "posted 2025-09-10" in result.detail

    def test_moved_away_and_back_is_judged_from_the_move_back(self):
        rows = [
            referred(),
            scheduled("2025-09-01", "2025-09-17"),
            rescheduled("2025-09-05", "2025-09-22"),
            rescheduled("2025-09-10", "2025-09-17"),
        ]
        result = one(rows)
        assert result.status == S.UNMET
        assert result.detail.startswith(
            "New hearing date posted 2025-09-10 for the 2025-09-17 hearing: 7 days'"
        )
        assert any("superseded" in s.description for s in result.derivation)

    def test_time_and_location_changes_are_not_judged(self):
        rows = [
            referred(),
            scheduled("2025-09-01", "2025-09-15"),
            row("2025-09-13", A.HEARING_LOCATION_CHANGED, "J19"),
            row("2025-09-14", A.HEARING_TIME_CHANGED, "J19"),
        ]
        result = one(rows)
        assert result.status == S.MET
        assert any(
            s.description.startswith("Location change") for s in result.derivation
        )

    def test_update_on_or_after_hearing_is_clerical(self):
        rows = [
            referred(),
            scheduled("2025-09-01", "2025-09-15"),
            row("2025-10-14", A.HEARING_TIME_CHANGED, "J19"),
            scheduled("2025-10-14", "2025-09-15"),
        ]
        result = one(rows)
        assert result.status == S.MET
        assert any("clerical" in s.description for s in result.derivation)


class TestMissingAnnouncement:
    def test_docket_hearing_without_announcement_is_unmet(self):
        dockets = [hearing("1", "2025-09-15", ("H1",))]
        result = one([referred()], dockets=dockets)
        assert result.status == S.UNMET
        assert "no announcement" in result.detail.lower()

    def test_future_hearing_without_announcement_is_not_yet_due(self):
        dockets = [hearing("1", "2025-09-15", ("H1",))]
        result = one([referred()], as_of="2025-09-01", dockets=dockets)
        assert (result.status, result.reason) == (S.PROVISIONAL, R.NOT_YET_DUE)

    def test_hearing_before_rules_adopted_is_exempt(self):
        dockets = [hearing("1", "2025-04-08", ("H1",))]
        rows = [row("2025-02-01", A.REFERRED, "J19")]
        assert one(rows, dockets=dockets).status == S.MET

    def test_no_hearing_no_obligation(self):
        assert results([referred()]) == []


class TestSenateCommittee:
    def test_five_days_is_met(self):
        rows = [referred("S30"), scheduled("2025-09-10", "2025-09-15", "S30")]
        result = one(rows, committee="S30")
        assert result.status == S.MET
        assert cites(result, SR12_P2)

    def test_four_days_is_unmet(self):
        rows = [referred("S30"), scheduled("2025-09-11", "2025-09-15", "S30")]
        assert one(rows, committee="S30").status == S.UNMET

    def test_location_change_is_not_judged(self):
        rows = [
            referred("S30"),
            scheduled("2025-09-01", "2025-09-15", "S30"),
            row("2025-09-14", A.HEARING_LOCATION_CHANGED, "S30"),
        ]
        assert one(rows, committee="S30").status == S.MET


class TestHouseCommittee:
    def test_seventy_two_hours_is_met(self):
        rows = [referred("H34"), scheduled("2025-09-12", "2025-09-15", "H34")]
        result = one(rows, committee="H34")
        assert result.status == S.MET
        assert cites(result, HR17A_P15)

    def test_under_seventy_two_hours_is_unmet(self):
        rows = [referred("H34"), scheduled("2025-09-13", "2025-09-15", "H34")]
        result = one(rows, committee="H34")
        assert result.status == S.UNMET
        assert "72 hours" in result.detail

    def test_change_is_not_judged(self):
        rows = [
            referred("H34"),
            scheduled("2025-09-01", "2025-09-15", "H34"),
            row("2025-09-14", A.HEARING_TIME_CHANGED, "H34"),
        ]
        assert one(rows, committee="H34").status == S.MET

    def test_house_rules_is_exempt(self):
        rows = [referred("H33"), scheduled("2025-09-13", "2025-09-15", "H33")]
        assert results(rows, committee="H33") == []


def test_announcement_before_handover_counts_for_the_new_committee():
    rows = [
        row("2025-02-27", A.REFERRED, "J38"),
        row("2025-06-26", A.HEARING_SCHEDULED, hearing_date="2025-07-09"),
        row("2025-06-27", A.REFERRED, "J45"),
        row("2025-08-07", A.DISCHARGED, "J33"),
    ]
    docket = [hearing("5266", "2025-07-09", ("H1",), committee_id="J45")]
    result = one(rows, committee="J45", dockets=docket)
    assert result.status == S.MET
    assert results(rows, committee="J38", dockets=docket) == []
