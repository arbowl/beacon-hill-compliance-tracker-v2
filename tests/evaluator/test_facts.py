"""Tests for projecting observations into bill facts."""

from datetime import UTC, date, datetime
from uuid import uuid4

from beacon.domain.ids import BillKey
from beacon.domain.observations import ActionType as A
from beacon.evaluator.facts import project
from tests.builders import hearing, history, row

BILL = BillKey(194, "H1234")


def test_referral_hearing_extension_report():
    obs = history(
        "H1234",
        [
            row("2025-02-27", A.REFERRED, "J19"),
            row("2025-02-27", A.REFERRED, "J19", text="Senate concurred"),
            row("2025-02-28", A.HEARING_SCHEDULED, "J19", hearing_date="2025-03-04"),
            row("2025-04-30", A.REPORTING_EXTENDED, "J19", new_deadline="2025-06-02"),
            row("2025-05-20", A.REPORTED, "J19"),
            row("2025-05-21", A.REFERRED, "H33"),
        ],
    )
    facts = project(BILL, [obs])
    j19 = facts.tenures_for("J19")
    assert len(j19) == 1
    tenure = j19[0]
    assert tenure.referred_on == date(2025, 2, 27)
    assert tenure.hearing_dates() == (date(2025, 3, 4),)
    assert tenure.extensions[0].until == date(2025, 6, 2)
    assert tenure.report is not None
    assert tenure.report.reported_on == date(2025, 5, 20)
    assert tenure.report.evidence.row_index == 4
    assert tenure.discharged_on is None
    assert facts.tenures_for("H33")[0].referred_on == date(2025, 5, 21)


def test_report_without_committee_attributed_to_current():
    obs = history(
        "H1",
        [
            row("2025-02-27", A.REFERRED, "J19"),
            row("2025-06-01", A.STUDY_ORDER, None),
        ],
    )
    report = project(BillKey(194, "H1"), [obs]).tenures_for("J19")[0].report
    assert report is not None
    assert report.action_type == A.STUDY_ORDER


def test_referral_elsewhere_discharges_open_tenure():
    obs = history(
        "H1",
        [
            row("2025-02-27", A.REFERRED, "J19"),
            row("2025-04-01", A.REFERRED, "J33"),
        ],
    )
    tenure = project(BillKey(194, "H1"), [obs]).tenures_for("J19")[0]
    assert tenure.report is None
    assert tenure.discharged_on == date(2025, 4, 1)
    assert tenure.discharged_to == "J33"


def test_rereferral_opens_new_tenure():
    obs = history(
        "H1",
        [
            row("2025-02-27", A.REFERRED, "J19"),
            row("2025-04-01", A.REPORTED, "J19"),
            row("2025-09-01", A.REFERRED, "J19"),
        ],
    )
    tenures = project(BillKey(194, "H1"), [obs]).tenures_for("J19")
    assert [t.referred_on for t in tenures] == [date(2025, 2, 27), date(2025, 9, 1)]
    assert tenures[0].tenure_id != tenures[1].tenure_id


def test_recommittal_on_report_day_opens_new_tenure():
    obs = history(
        "H3430",
        [
            row("2025-02-27", A.REFERRED, "J25"),
            row("2025-12-01", A.STUDY_ORDER, None, related_bills=("H4800",)),
            row("2025-12-01", A.REPORTED, None),
            row("2025-12-01", A.REFERRED, "J25"),
            row("2025-12-18", A.ACCOMPANIED, None, related_bills=("H3326",)),
            row("2025-12-18", A.REFERRED, "H34"),
        ],
    )
    first, second = project(BillKey(194, "H3430"), [obs]).tenures_for("J25")
    assert first.report is not None
    assert first.report.reported_on == date(2025, 12, 1)
    assert second.referred_on == date(2025, 12, 1)
    assert second.report is not None
    assert second.report.reported_on == date(2025, 12, 18)


def test_reschedule_before_hearing_replaces_date():
    obs = history(
        "H1",
        [
            row("2025-02-01", A.REFERRED, "J19"),
            row("2025-02-20", A.HEARING_SCHEDULED, "J19", hearing_date="2025-03-04"),
            row("2025-02-28", A.HEARING_RESCHEDULED, "J19", hearing_date="2025-03-11"),
            row("2025-03-05", A.HEARING_LOCATION_CHANGED, "J19"),
        ],
    )
    tenure = project(BillKey(194, "H1"), [obs]).tenures_for("J19")[0]
    assert tenure.hearing_dates() == (date(2025, 3, 11),)
    assert len(tenure.hearing_notices) == 3


def test_reschedule_after_hearing_adds_date():
    obs = history(
        "H1",
        [
            row("2025-02-01", A.REFERRED, "J19"),
            row("2025-02-20", A.HEARING_SCHEDULED, "J19", hearing_date="2025-03-04"),
            row("2025-05-01", A.HEARING_RESCHEDULED, "J19", hearing_date="2025-05-15"),
        ],
    )
    tenure = project(BillKey(194, "H1"), [obs]).tenures_for("J19")[0]
    assert tenure.hearing_dates() == (date(2025, 3, 4), date(2025, 5, 15))


def test_hearing_keeps_the_notice_that_set_its_date():
    obs = history(
        "H1",
        [
            row("2025-02-01", A.REFERRED, "J19"),
            row("2025-02-20", A.HEARING_SCHEDULED, "J19", hearing_date="2025-03-04"),
            row("2025-03-01", A.HEARING_SCHEDULED, "J19", hearing_date="2025-03-04"),
            row("2025-03-03", A.HEARING_RESCHEDULED, "J19", hearing_date="2025-03-04"),
            row("2025-03-03", A.HEARING_TIME_CHANGED, "J19"),
        ],
    )
    (h,) = project(BillKey(194, "H1"), [obs]).tenures_for("J19")[0].hearings()
    assert h.hearing_date == date(2025, 3, 4)
    assert h.notice is not None
    assert h.notice.announced_on == date(2025, 2, 20)
    assert len(h.notices) == 4


def test_correction_to_earlier_committee_hearing_stays_with_that_tenure():
    obs = history(
        "H1",
        [
            row("2025-02-27", A.REFERRED, "J11"),
            row("2025-04-23", A.HEARING_SCHEDULED, hearing_date="2025-04-29"),
            [row("2025-07-31", A.REFERRED, "J24"), row("2025-07-31", A.REPORTED)],
            [
                row("2025-08-28", A.HEARING_RESCHEDULED, hearing_date="2025-04-29"),
                row("2025-08-28", A.HEARING_TIME_CHANGED),
            ],
        ],
    )
    facts = project(BillKey(194, "H1"), [obs])
    (j11,) = facts.tenures_for("J11")
    (j24,) = facts.tenures_for("J24")
    assert j24.hearing_notices == ()
    (h,) = j11.hearings()
    assert h.notice is not None
    assert h.notice.announced_on == date(2025, 4, 23)
    assert [n.announced_on for n in h.notices] == [
        date(2025, 4, 23),
        date(2025, 8, 28),
        date(2025, 8, 28),
    ]


def test_hearing_before_any_referral_is_dropped():
    obs = history(
        "H1",
        [
            row("2025-03-01", A.REFERRED, "J24"),
            row("2025-03-05", A.HEARING_SCHEDULED, hearing_date="2025-02-20"),
        ],
    )
    (tenure,) = project(BillKey(194, "H1"), [obs]).tenures_for("J24")
    assert tenure.hearing_notices == ()


def test_reschedule_onto_another_pending_hearing_keeps_both():
    obs = history(
        "H1",
        [
            row("2025-02-01", A.REFERRED, "J19"),
            row("2025-02-20", A.HEARING_SCHEDULED, "J19", hearing_date="2025-03-04"),
            row("2025-02-21", A.HEARING_SCHEDULED, "J19", hearing_date="2025-03-11"),
            row("2025-02-25", A.HEARING_RESCHEDULED, "J19", hearing_date="2025-03-04"),
        ],
    )
    tenure = project(BillKey(194, "H1"), [obs]).tenures_for("J19")[0]
    assert tenure.hearing_dates() == (date(2025, 3, 4), date(2025, 3, 11))


def test_latest_capture_and_extractor_version_win():
    old_capture, new_capture = uuid4(), uuid4()
    early = datetime(2025, 3, 1, tzinfo=UTC)
    late = datetime(2025, 4, 1, tzinfo=UTC)
    stale = history(
        "H1",
        [row("2025-02-01", A.REFERRED, "J19")],
        capture_id=old_capture,
        observed_at=early,
    )
    current_v1 = history(
        "H1",
        [row("2025-02-01", A.REFERRED, "J33")],
        capture_id=new_capture,
        observed_at=late,
    )
    current_v2 = history(
        "H1",
        [row("2025-02-01", A.REFERRED, "J19")],
        capture_id=new_capture,
        observed_at=late,
        extractor_version=2,
    )
    facts = project(BillKey(194, "H1"), [current_v2, stale, current_v1])
    assert facts.source_observations == (current_v2.observation_id,)
    assert facts.last_observed_at == late
    assert len(facts.tenures_for("J19")) == 1


def test_no_observations():
    facts = project(BILL, [])
    assert facts.tenures == ()
    assert facts.last_observed_at is None


def test_compound_row_reports_before_referring():
    obs = history(
        "H1",
        [
            row("2025-02-01", A.REFERRED, "J19"),
            [
                row("2025-05-01", A.REFERRED, "H33"),
                row("2025-05-01", A.REPORTED, "J19"),
            ],
        ],
    )
    facts = project(BillKey(194, "H1"), [obs])
    j19 = facts.tenures_for("J19")[0]
    assert j19.report is not None
    assert j19.discharged_on is None
    assert j19.report.evidence.row_index == 1
    assert facts.tenures_for("H33")[0].referred_on == date(2025, 5, 1)


def test_report_before_any_referral_is_committee_origin():
    obs = history(
        "H4372",
        [
            [
                row("2025-08-11", A.REPORTED, text="Reported from the committee"),
                row(
                    "2025-08-11",
                    A.UNKNOWN,
                    text="New draft of H1768",
                    related_bills=("H1768",),
                ),
            ],
            row("2025-08-11", A.REFERRED, "H34"),
        ],
    )
    facts = project(BillKey(194, "H4372"), [obs])
    assert facts.origin is not None
    assert facts.origin.originated_on == date(2025, 8, 11)
    assert facts.origin.source_bills == ("H1768",)


def test_referred_bill_has_no_origin():
    obs = history(
        "H1", [row("2025-02-01", A.REFERRED, "J19"), row("2025-04-01", A.REPORTED)]
    )
    assert project(BillKey(194, "H1"), [obs]).origin is None


def test_docket_supplies_hearing_missing_from_history():
    obs = history(
        "H110",
        [row("2025-03-01", A.REFERRED, "J45"), row("2025-09-08", A.REPORTED, "J45")],
    )
    dockets = [
        hearing("1", "2025-07-09", ("H110", "H111"), committee_id="J45"),
        hearing("2", "2025-07-09", ("H110",), committee_id="J19"),
        hearing("3", "2025-10-01", ("H110",), committee_id="J45"),
    ]
    facts = project(BillKey(194, "H110"), [obs], dockets)
    (tenure,) = facts.tenures_for("J45")
    assert tenure.hearing_dates() == (date(2025, 7, 9),)
    assert tenure.hearing_notices[0].announced_on is None
    assert dockets[0].observation_id in facts.source_observations


def test_docket_hearing_already_in_history_is_not_duplicated():
    obs = history(
        "H1",
        [
            row("2025-02-01", A.REFERRED, "J19"),
            row("2025-03-20", A.HEARING_SCHEDULED, "J19", hearing_date="2025-04-08"),
        ],
    )
    facts = project(BillKey(194, "H1"), [obs], [hearing("1", "2025-04-08", ("H1",))])
    assert len(facts.tenures_for("J19")[0].hearing_notices) == 1


def test_as_of_drops_later_events_and_observations():
    obs = history(
        "H1",
        [row("2025-02-01", A.REFERRED, "J19"), row("2026-07-22", A.REPORTED, "J19")],
    )
    early = datetime(2025, 7, 1, tzinfo=UTC)
    (tenure,) = project(BillKey(194, "H1"), [obs], as_of=early).tenures_for("J19")
    assert tenure.report is None
    (later,) = project(BillKey(194, "H1"), [obs]).tenures_for("J19")
    assert later.report is not None
    dockets = [hearing("1", "2025-04-08", ("H1",), observed_at="2026-01-01T02:00")]
    (blind,) = project(BillKey(194, "H1"), [obs], dockets, as_of=early).tenures_for(
        "J19"
    )
    assert blind.hearing_dates() == ()


def _handover(*dockets_for: str):
    """H108: announced on 6/26 under J38, moved to J45 on 6/27, heard 7/9 by J45."""
    obs = history(
        "H108",
        [
            row("2025-02-27", A.REFERRED, "J38"),
            row("2025-06-26", A.HEARING_SCHEDULED, hearing_date="2025-07-09"),
            row("2025-06-27", A.REFERRED, "J45"),
            row("2025-08-07", A.DISCHARGED, "J33"),
        ],
    )
    dockets = [
        hearing("5266", "2025-07-09", ("H108",), committee_id=c) for c in dockets_for
    ]
    return obs, project(BillKey(194, "H108"), [obs], dockets)


def test_announcement_made_just_before_handover_follows_the_hearing():
    obs, facts = _handover("J45")
    (old,) = facts.tenures_for("J38")
    (new,) = facts.tenures_for("J45")
    assert old.hearing_notices == ()
    (notice,) = new.hearing_notices
    assert notice.announced_on == date(2025, 6, 26)
    assert notice.hearing_date == date(2025, 7, 9)
    assert notice.evidence.observation_id == obs.observation_id
    assert notice.evidence.row_index == 1


def test_announcement_stays_without_a_later_committee_confirming_the_hearing():
    _, facts = _handover()
    (old,) = facts.tenures_for("J38")
    assert [n.announced_on for n in old.hearing_notices] == [date(2025, 6, 26)]
    assert facts.tenures_for("J45")[0].hearing_notices == ()
