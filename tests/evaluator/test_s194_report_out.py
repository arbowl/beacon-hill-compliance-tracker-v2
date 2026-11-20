"""Report-out deadline cases for session 194, one per clause behavior.

House-filed matters before joint committees: House Rule 27 para 2.
Senate-filed matters: Joint Rule 10 para 2 (the Senate rules set no branch deadline).
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from beacon.domain.evaluation import ProvisionalReason, RequirementResult, StepRole
from beacon.domain.evaluation import RequirementStatus as S
from beacon.domain.ids import BillKey
from beacon.domain.observations import ActionType as A
from beacon.domain.provenance import ClauseRef, RuleSource
from beacon.evaluator.facts import project
from beacon.evaluator.rulesets.s194.report_out import ReportOutDeadline
from tests.builders import history, row

ET = ZoneInfo("America/New_York")
HR27_P2 = ClauseRef(RuleSource.HOUSE, "27", 2)
JR10_P2 = ClauseRef(RuleSource.JOINT, "10", 2)


def check(
    bill_id: str, rows: list[dict], as_of: str, committee_id: str = "J19"
) -> RequirementResult:
    bill = BillKey(194, bill_id)
    facts = project(bill, [history(bill_id, rows)])
    (tenure,) = facts.tenures_for(committee_id)
    requirement = ReportOutDeadline()
    (obligation,) = requirement.obligations(facts, tenure)
    moment = datetime.fromisoformat(as_of).replace(hour=12, tzinfo=ET)
    return requirement.check(obligation, facts, tenure, moment)


def heard(on: str, referred: str = "2025-02-01", committee: str = "J19") -> list[dict]:
    return [
        row(referred, A.REFERRED, committee),
        row(referred, A.HEARING_SCHEDULED, committee, hearing_date=on),
    ]


def cites(result: RequirementResult, ref: ClauseRef) -> bool:
    return any(step.clause == ref for step in result.derivation)


class TestHouseRule27:
    def test_reported_within_60_days_is_met(self):
        result = check(
            "H1",
            [*heard("2025-03-04"), row("2025-04-20", A.REPORTED, "J19")],
            "2025-06-01",
        )
        assert result.status == S.MET
        assert result.due == date(2025, 5, 3)
        assert cites(result, HR27_P2)

    def test_before_deadline_is_provisional_not_yet_due(self):
        result = check("H1", heard("2025-03-04"), "2025-04-01")
        assert result.status == S.PROVISIONAL
        assert result.reason == ProvisionalReason.NOT_YET_DUE
        assert result.resolves_on == date(2025, 5, 3)

    def test_due_date_itself_is_still_on_time(self):
        result = check(
            "H1",
            [*heard("2025-03-04"), row("2025-05-03", A.REPORTED, "J19")],
            "2025-06-01",
        )
        assert result.status == S.MET

    def test_deadline_passed_without_report_is_unmet(self):
        result = check("H1", heard("2025-03-04"), "2025-06-01")
        assert result.status == S.UNMET

    def test_late_report_is_unmet(self):
        result = check(
            "H1",
            [*heard("2025-03-04"), row("2025-05-10", A.REPORTED, "J19")],
            "2025-06-01",
        )
        assert result.status == S.UNMET
        assert "7 days" in result.detail

    def test_chair_extension_within_30_days(self):
        rows = [
            *heard("2025-03-04"),
            row("2025-04-30", A.REPORTING_EXTENDED, "J19", new_deadline="2025-06-02"),
            row("2025-06-01", A.REPORTED, "J19"),
        ]
        result = check("H1", rows, "2025-07-01")
        assert result.status == S.MET
        assert result.due == date(2025, 6, 2)

    def test_extension_order_beyond_90_days_is_honored(self):
        rows = [
            *heard("2025-03-04"),
            row("2025-05-30", A.REPORTING_EXTENDED, "J19", new_deadline="2025-08-01"),
            row("2025-07-15", A.REPORTED, "J19"),
        ]
        result = check("H1", rows, "2025-09-01")
        assert result.status == S.MET
        assert result.due == date(2025, 8, 1)

    def test_extension_capped_at_third_wednesday_of_march(self):
        rows = [
            *heard("2025-11-04"),
            row("2025-12-20", A.REPORTING_EXTENDED, "J19", new_deadline="2026-04-30"),
        ]
        result = check("H1", rows, "2026-03-20")
        assert result.due == date(2026, 3, 18)
        assert result.status == S.UNMET

    def test_latest_extension_governs(self):
        rows = [
            *heard("2025-03-04"),
            row("2025-04-30", A.REPORTING_EXTENDED, "J19", new_deadline="2025-06-02"),
            row("2025-05-30", A.REPORTING_EXTENDED, "J19", new_deadline="2025-07-15"),
        ]
        assert check("H1", rows, "2025-06-10").due == date(2025, 7, 15)

    def test_extension_without_date_grants_30_days(self):
        rows = [*heard("2025-03-04"), row("2025-04-30", A.REPORTING_EXTENDED, "J19")]
        assert check("H1", rows, "2025-05-10").due == date(2025, 6, 2)

    def test_heard_after_third_wednesday_of_december_uses_march_floor(self):
        result = check("H1", heard("2026-01-07", referred="2025-12-01"), "2026-02-01")
        assert result.due == date(2026, 3, 18)

    def test_heard_late_session_whichever_later(self):
        result = check("H1", heard("2026-02-10", referred="2026-01-15"), "2026-03-01")
        assert result.due == date(2026, 4, 11)

    def test_heard_on_third_wednesday_takes_later_branch(self):
        # "prior to" and "after" leave the day itself unaddressed; v1 treats it as
        # after, and so do we.
        result = check("H1", heard("2025-12-17", referred="2025-11-01"), "2026-01-01")
        assert result.due == date(2026, 3, 18)

    def test_rescheduled_hearing_uses_actual_date(self):
        rows = [
            row("2025-02-01", A.REFERRED, "J19"),
            row("2025-02-20", A.HEARING_SCHEDULED, "J19", hearing_date="2025-03-04"),
            row("2025-02-28", A.HEARING_RESCHEDULED, "J19", hearing_date="2025-03-11"),
        ]
        assert check("H1", rows, "2025-04-01").due == date(2025, 5, 10)

    def test_future_hearing_is_not_yet_heard(self):
        result = check("H1", heard("2025-03-04"), "2025-03-01")
        assert result.status == S.PROVISIONAL
        assert result.reason == ProvisionalReason.NOT_YET_DUE
        assert result.due == date(2026, 3, 18)

    def test_unheard_before_march_cutoff_is_provisional(self):
        result = check("H1", [row("2025-02-01", A.REFERRED, "J19")], "2025-10-01")
        assert result.status == S.PROVISIONAL
        assert result.resolves_on == date(2026, 3, 18)

    def test_unheard_reported_before_cutoff_is_met(self):
        rows = [row("2025-02-01", A.REFERRED, "J19"), row("2025-11-01", A.STUDY_ORDER)]
        assert check("H1", rows, "2026-04-01").status == S.MET

    def test_unheard_unreported_after_cutoff_is_unmet(self):
        result = check("H1", [row("2025-02-01", A.REFERRED, "J19")], "2026-03-19")
        assert result.status == S.UNMET
        assert result.due == date(2026, 3, 18)


class TestHouseRule27HealthCareFinancing:
    def test_late_referral_from_other_joint_committee_due_last_wednesday_may(self):
        rows = [
            *heard("2025-03-04"),
            row("2025-05-01", A.REPORTED, "J19"),
            row("2026-04-01", A.REFERRED, "J24"),
        ]
        result = check("H1", rows, "2026-05-01", committee_id="J24")
        assert result.due == date(2026, 5, 27)
        assert result.status == S.PROVISIONAL
        assert cites(result, HR27_P2)

    def test_late_referral_unreported_after_may_deadline_is_unmet(self):
        rows = [
            row("2025-02-01", A.REFERRED, "J19"),
            row("2026-04-01", A.REFERRED, "J24"),
        ]
        assert check("H1", rows, "2026-05-28", committee_id="J24").status == S.UNMET

    def test_referral_on_march_cutoff_does_not_qualify(self):
        rows = [
            row("2025-02-01", A.REFERRED, "J19"),
            row("2026-03-18", A.REFERRED, "J24"),
        ]
        result = check("H1", rows, "2026-04-01", committee_id="J24")
        assert result.due != date(2026, 5, 27)

    def test_initial_referral_to_health_care_financing_does_not_qualify(self):
        rows = [row("2026-04-01", A.REFERRED, "J24")]
        result = check("H1", rows, "2026-04-15", committee_id="J24")
        assert result.due != date(2026, 5, 27)

    def test_prior_house_committee_is_not_a_joint_standing_committee(self):
        rows = [
            row("2025-02-01", A.REFERRED, "H33"),
            row("2026-04-01", A.REFERRED, "J24"),
        ]
        result = check("H1", rows, "2026-04-15", committee_id="J24")
        assert result.due != date(2026, 5, 27)


class TestJointRule10:
    def test_referred_before_october_due_first_wednesday_december(self):
        rows = [row("2025-02-27", A.REFERRED, "J19"), row("2025-11-30", A.REPORTED)]
        result = check("S1", rows, "2025-12-10")
        assert result.status == S.MET
        assert result.due == date(2025, 12, 3)
        assert cites(result, JR10_P2)

    def test_referred_on_october_first_due_in_60_days(self):
        result = check("S1", [row("2025-10-01", A.REFERRED, "J19")], "2025-11-01")
        assert result.due == date(2025, 11, 30)

    def test_deadline_applies_without_hearing(self):
        result = check("S1", [row("2025-02-27", A.REFERRED, "J19")], "2025-12-04")
        assert result.status == S.UNMET

    def test_extensions_not_recognized(self):
        rows = [
            row("2025-02-27", A.REFERRED, "J19"),
            row("2025-11-20", A.REPORTING_EXTENDED, "J19", new_deadline="2026-01-15"),
            row("2026-01-10", A.REPORTED, "J19"),
        ]
        result = check("S1", rows, "2026-02-01")
        assert result.status == S.UNMET
        assert result.due == date(2025, 12, 3)
        assert any("not recognized" in s.description for s in result.derivation)

    @pytest.mark.parametrize(
        ("referred", "due"),
        [("2025-12-24", date(2026, 1, 28)), ("2025-12-26", date(2026, 2, 24))],
    )
    def test_health_care_financing(self, referred: str, due: date):
        rows = [row(referred, A.REFERRED, "J24")]
        assert check("S1", rows, "2026-01-01", committee_id="J24").due == due


class TestScope:
    def test_house_committee_generates_no_obligation(self):
        bill = BillKey(194, "H1")
        facts = project(bill, [history("H1", [row("2025-02-01", A.REFERRED, "H33")])])
        (tenure,) = facts.tenures_for("H33")
        assert ReportOutDeadline().obligations(facts, tenure) == []

    def test_discharged_before_due_is_met(self):
        rows = [
            row("2025-02-27", A.REFERRED, "J19"),
            row("2025-04-01", A.REFERRED, "J33"),
        ]
        result = check("S1", rows, "2025-12-10")
        assert result.status == S.MET
        assert "J33" in result.detail


class TestStructuredBreakdown:
    """Extension usage must be computable from results alone, after the fact."""

    def test_base_due_and_extension_are_tagged(self):
        rows = [
            *heard("2025-03-04"),
            row("2025-04-30", A.REPORTING_EXTENDED, "J19", new_deadline="2025-06-02"),
            row("2025-05-20", A.REPORTED, "J19"),
        ]
        result = check("H1", rows, "2025-07-01")
        roles = {s.role: s.value for s in result.derivation if s.role}
        assert roles[StepRole.BASE_DUE] == date(2025, 5, 3)
        assert roles[StepRole.EXTENSION] == date(2025, 6, 2)
        assert result.completed_on == date(2025, 5, 20)

    def test_ignored_extension_is_tagged(self):
        rows = [
            row("2025-02-27", A.REFERRED, "J19"),
            row("2025-11-20", A.REPORTING_EXTENDED, "J19", new_deadline="2026-01-15"),
        ]
        result = check("S1", rows, "2025-11-25")
        assert any(s.role == StepRole.EXTENSION_IGNORED for s in result.derivation)
        assert result.completed_on is None
