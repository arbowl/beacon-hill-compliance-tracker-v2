"""Session 194 posting: votes (JR 1D para 14) and summaries (JR 1B para 4).

Policy: present and not proven late is Met; Unmet only when posting is shown to be
3 or more days late (by an absence observation, or by the date the document could
first have been public: the site's posting date, else the file creation date), or
the document is still absent on an exhaustive search after that point.
"""

from datetime import date, datetime

from beacon.domain.evaluation import ProvisionalReason as R
from beacon.domain.evaluation import RequirementResult, StepRole
from beacon.domain.evaluation import RequirementStatus as S
from beacon.domain.ids import BillKey
from beacon.domain.observations import ActionType as A
from beacon.domain.observations import DocumentKind as D
from beacon.domain.observations import Observation
from beacon.domain.provenance import ClauseRef, RuleSource
from beacon.evaluator.facts import project
from beacon.evaluator.rulesets.s194.posting import SummaryPosted, VotesPosted
from tests.builders import ET, document, history, row, search

JR1D_P14 = ClauseRef(RuleSource.JOINT, "1D", 14)
JR1B_P4 = ClauseRef(RuleSource.JOINT, "1B", 4)
HEARD = [
    row("2025-02-01", A.REFERRED, "J19"),
    row("2025-02-01", A.HEARING_SCHEDULED, "J19", hearing_date="2025-04-08"),
]
REPORTED = [*HEARD, row("2025-07-22", A.REPORTED, "J19")]


def results(requirement, rows: list[dict], docs: list[Observation], as_of: str):
    facts = project(BillKey(194, "H1"), [history("H1", rows), *docs])
    (tenure,) = facts.tenures_for("J19")
    moment = datetime.fromisoformat(as_of).replace(hour=12, tzinfo=ET)
    return [
        requirement.check(o, facts, tenure, moment)
        for o in requirement.obligations(facts, tenure)
    ]


def one(requirement, rows, docs, as_of) -> RequirementResult:
    (result,) = results(requirement, rows, docs, as_of)
    return result


def cites(result: RequirementResult, ref: ClauseRef) -> bool:
    return any(s.clause == ref for s in result.derivation)


class TestVotesPosted:
    def test_seen_within_48_hours_is_verified_met(self):
        docs = [
            search("H1", D.VOTES, "2025-07-21T02:00"),
            search("H1", D.VOTES, "2025-07-22T02:00", found=True),
            document("H1", D.VOTES, "2025-07-22T02:00", vote_date="2025-07-21"),
        ]
        result = one(VotesPosted(), REPORTED, docs, "2025-08-01")
        assert result.status == S.MET
        assert "verified" in result.detail
        assert result.completed_on == date(2025, 7, 22)
        assert cites(result, JR1D_P14)

    def test_shown_late_by_absence_is_unmet(self):
        docs = [
            search("H1", D.VOTES, "2025-07-29T02:00"),
            search("H1", D.VOTES, "2025-07-30T02:00", found=True),
            document("H1", D.VOTES, "2025-07-30T02:00", vote_date="2025-07-21"),
        ]
        assert one(VotesPosted(), REPORTED, docs, "2025-08-01").status == S.UNMET

    def test_present_before_tracking_is_presumed_met(self):
        docs = [
            search("H1", D.VOTES, "2025-11-18T02:00", found=True),
            document("H1", D.VOTES, "2025-11-18T02:00", vote_date="2025-07-21"),
        ]
        result = one(VotesPosted(), REPORTED, docs, "2025-11-18")
        assert result.status == S.MET
        assert "presumed" in result.detail
        assert any(s.role == StepRole.TOLERANCE for s in result.derivation)

    def test_created_after_tolerance_is_unmet(self):
        docs = [
            search("H1", D.VOTES, "2025-11-18T02:00", found=True),
            document(
                "H1",
                D.VOTES,
                "2025-11-18T02:00",
                vote_date="2025-07-21",
                created_at="2025-08-01T10:00",
            ),
        ]
        assert one(VotesPosted(), REPORTED, docs, "2025-11-18").status == S.UNMET

    def test_posted_after_tolerance_is_unmet_though_created_on_time(self):
        docs = [
            search("H1", D.VOTES, "2025-11-18T02:00", found=True),
            document(
                "H1",
                D.VOTES,
                "2025-11-18T02:00",
                vote_date="2025-07-21",
                created_at="2025-07-21T15:00",
                posted_from="2025-12-10T00:00",
            ),
        ]
        result = one(VotesPosted(), REPORTED, docs, "2026-01-05")
        assert result.status == S.UNMET
        assert "was posted on 2025-12-10" in result.detail

    def test_posted_on_time_is_met_though_file_created_late(self):
        docs = [
            search("H1", D.VOTES, "2025-11-18T02:00", found=True),
            document(
                "H1",
                D.VOTES,
                "2025-11-18T02:00",
                vote_date="2025-07-21",
                created_at="2025-09-01T10:00",
                posted_from="2025-07-22T00:00",
            ),
        ]
        assert one(VotesPosted(), REPORTED, docs, "2025-11-18").status == S.MET

    def test_missing_after_report_runs_from_report_date(self):
        docs = [search("H1", D.VOTES, "2025-11-18T02:00")]
        result = one(VotesPosted(), REPORTED, docs, "2025-11-18")
        assert result.status == S.UNMET
        assert result.due == date(2025, 7, 24)

    def test_missing_but_within_tolerance_is_not_yet_due(self):
        docs = [search("H1", D.VOTES, "2025-07-23T02:00")]
        result = one(VotesPosted(), REPORTED, docs, "2025-07-24")
        assert (result.status, result.reason) == (S.PROVISIONAL, R.NOT_YET_DUE)
        assert result.resolves_on == date(2025, 7, 25)

    def test_never_searched_is_missing_data(self):
        result = one(VotesPosted(), REPORTED, [], "2025-11-18")
        assert (result.status, result.reason) == (S.PROVISIONAL, R.MISSING_DATA)

    def test_automatic_study_order_needs_no_vote(self):
        rows = [
            *HEARD,
            row(
                "2026-03-26",
                A.STUDY_ORDER,
                text="Accompanied a study order, see H5281 (under House Rule 27)",
            ),
        ]
        assert results(VotesPosted(), rows, [], "2026-04-01") == []

    def test_unreported_without_vote_has_no_obligation(self):
        assert results(VotesPosted(), HEARD, [], "2025-11-18") == []

    def test_vote_found_before_report_creates_obligation(self):
        docs = [
            search("H1", D.VOTES, "2025-07-22T02:00", found=True),
            document("H1", D.VOTES, "2025-07-22T02:00", vote_date="2025-07-21"),
        ]
        assert one(VotesPosted(), HEARD, docs, "2025-08-01").status == S.MET


class TestSummaryPosted:
    def test_posted_before_hearing_is_verified_met(self):
        docs = [search("H1", D.SUMMARY, "2025-04-01T02:00", found=True)]
        result = one(SummaryPosted(), HEARD, docs, "2025-05-01")
        assert result.status == S.MET
        assert "verified" in result.detail
        assert result.due == date(2025, 4, 8)
        assert cites(result, JR1B_P4)

    def test_present_before_tracking_is_presumed_met(self):
        docs = [search("H1", D.SUMMARY, "2025-11-18T02:00", found=True)]
        result = one(SummaryPosted(), HEARD, docs, "2025-11-18")
        assert result.status == S.MET
        assert "presumed" in result.detail

    def test_shown_late_by_absence_is_unmet(self):
        docs = [
            search("H1", D.SUMMARY, "2025-04-12T02:00"),
            search("H1", D.SUMMARY, "2025-04-13T02:00", found=True),
        ]
        assert one(SummaryPosted(), HEARD, docs, "2025-05-01").status == S.UNMET

    def test_absent_within_tolerance_then_found_is_met(self):
        docs = [
            search("H1", D.SUMMARY, "2025-04-09T02:00"),
            search("H1", D.SUMMARY, "2025-04-10T02:00", found=True),
        ]
        assert one(SummaryPosted(), HEARD, docs, "2025-05-01").status == S.MET

    def test_created_after_tolerance_is_unmet(self):
        docs = [
            search("H1", D.SUMMARY, "2025-11-18T02:00", found=True),
            document(
                "H1", D.SUMMARY, "2025-11-18T02:00", created_at="2025-05-01T09:00"
            ),
        ]
        assert one(SummaryPosted(), HEARD, docs, "2025-11-18").status == S.UNMET

    def test_never_found_is_unmet(self):
        docs = [search("H1", D.SUMMARY, "2025-11-18T02:00")]
        assert one(SummaryPosted(), HEARD, docs, "2025-11-18").status == S.UNMET

    def test_future_hearing_is_not_yet_due(self):
        result = one(SummaryPosted(), HEARD, [], "2025-04-01")
        assert (result.status, result.reason) == (S.PROVISIONAL, R.NOT_YET_DUE)

    def test_no_hearing_has_no_obligation(self):
        rows = [row("2025-02-01", A.REFERRED, "J19")]
        assert results(SummaryPosted(), rows, [], "2025-11-18") == []


class TestChamberCommitteeVotes:
    def _results(self, committee: str):
        rows = [
            row("2025-02-01", A.REFERRED, committee),
            row("2025-07-22", A.REPORTED, committee),
        ]
        facts = project(BillKey(194, "H1"), [history("H1", rows)])
        (tenure,) = facts.tenures_for(committee)
        requirement = VotesPosted()
        moment = datetime(2025, 11, 18, 12, tzinfo=ET)
        return [
            requirement.check(o, facts, tenure, moment)
            for o in requirement.obligations(facts, tenure)
        ]

    def test_house_committee_votes_cite_house_rule_17b(self):
        (result,) = self._results("H34")
        assert cites(result, ClauseRef(RuleSource.HOUSE, "17B", 1))

    def test_senate_committee_has_no_vote_obligation(self):
        assert self._results("S30") == []
