"""Tests for projecting observations into bill facts."""

from datetime import UTC, date, datetime
from uuid import uuid4

from beacon.domain.ids import BillKey
from beacon.domain.observations import ActionType as A
from beacon.domain.observations import Branch
from beacon.evaluator.facts import project
from beacon.evaluator.record_gaps import SuppliedReferral, UnrecordedTransfer
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


def test_repeated_history_row_is_read_once():
    reported = [
        row("2025-12-01", A.REFERRED, "S30", branch=Branch.SENATE, text="Reported"),
        row("2025-12-01", A.REPORTED, branch=Branch.SENATE, text="Reported"),
    ]
    obs = history(
        "S1",
        [row("2025-02-27", A.REFERRED, "J30"), reported, reported],
    )
    facts = project(BillKey(194, "S1"), [obs])
    (j30,) = facts.tenures_for("J30")
    (s30,) = facts.tenures_for("S30")
    assert j30.report is not None
    assert s30.report is None
    assert s30.referral.row_index == 1


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


def test_third_reading_tenure_runs_from_the_order_to_the_reading():
    obs = history(
        "S23",
        [
            row("2025-03-06", A.REFERRED, "J10", branch=Branch.SENATE),
            row("2025-06-18", A.REPORTED, None, branch=Branch.SENATE),
            row(
                "2025-07-17",
                A.REFERRED_TO_BILLS_IN_THIRD_READING,
                "S31",
                branch=Branch.SENATE,
            ),
            row("2025-09-18", A.READ_THIRD, "S31", branch=Branch.SENATE),
            row("2025-09-22", A.REFERRED, "H52"),
        ],
    )
    facts = project(BillKey(194, "S23"), [obs])
    (tenure,) = facts.tenures_for("S31")
    assert tenure.referred_on == date(2025, 7, 17)
    assert tenure.report is not None
    assert tenure.report.reported_on == date(2025, 9, 18)
    assert tenure.discharged_on is None
    assert facts.tenures_for("J10")[0].discharged_on is None


def test_ordered_and_read_third_in_one_row_opens_then_closes():
    obs = history(
        "S1",
        [
            row("2025-02-01", A.REFERRED, "J19"),
            row("2025-05-01", A.REPORTED, "J19"),
            [
                row("2025-06-01", A.READ_THIRD, "S31", branch=Branch.SENATE),
                row(
                    "2025-06-01",
                    A.REFERRED_TO_BILLS_IN_THIRD_READING,
                    "S31",
                    branch=Branch.SENATE,
                ),
            ],
        ],
    )
    (tenure,) = project(BillKey(194, "S1"), [obs]).tenures_for("S31")
    assert tenure.report is not None
    assert tenure.report.reported_on == date(2025, 6, 1)


def test_reading_without_its_referral_closes_nothing():
    obs = history(
        "H1",
        [
            row("2025-02-01", A.REFERRED, "H34"),
            row("2025-05-01", A.READ_THIRD, "H36"),
        ],
    )
    facts = project(BillKey(194, "H1"), [obs])
    assert facts.tenures_for("H34")[0].report is None
    assert facts.tenures_for("H36") == ()


S185_ROWS = [
    row("2025-02-27", A.REFERRED, "J47", branch=Branch.SENATE),
    [
        row(
            "2025-12-18",
            kind,
            branch=Branch.SENATE,
            text="Accompanied a study order (under JR10), see S2891",
            related_bills=("S2891",),
        )
        for kind in (A.STUDY_ORDER, A.ACCOMPANIED)
    ],
    row("2026-05-07", A.REFERRED, "J47", branch=Branch.SENATE),
]
S2891 = history(
    "S2891",
    [
        row("2025-12-18", A.REPORTED, "J47", branch=Branch.SENATE),
        row(
            "2025-12-18",
            A.ACCOMPANIED,
            branch=Branch.SENATE,
            text="Accompanied by S185",
            related_bills=("S185",),
        ),
        row("2025-12-18", A.REFERRED, "H33", branch=Branch.SENATE),
        row("2025-12-24", A.DISCHARGED, "S29", branch=Branch.SENATE),
    ],
)


def vehicles(*observations):
    by_bill = {o.bill_id: [o] for o in observations}
    return lambda bill_id: by_bill.get(bill_id, [])


def test_accompanying_bill_rides_through_vehicle_committees():
    facts = project(
        BillKey(194, "S185"),
        [history("S185", S185_ROWS)],
        vehicle_history=vehicles(S2891),
    )
    (ride,) = facts.rides
    assert ride.vehicle == "S2891"
    assert ride.accompanied_on == date(2025, 12, 18)
    assert ride.released_on == date(2026, 5, 7)
    assert ride.committee_ids == ("H33", "S29")
    assert facts.carried_before("S29") is ride
    assert facts.carried_before("S30") is None
    assert S2891.observation_id in facts.source_observations


def test_vehicle_committee_after_release_does_not_carry():
    later = history(
        "S2891",
        [
            row("2025-12-18", A.REPORTED, "J47", branch=Branch.SENATE),
            row("2026-06-01", A.REFERRED, "S29", branch=Branch.SENATE),
        ],
    )
    facts = project(
        BillKey(194, "S185"),
        [history("S185", S185_ROWS)],
        vehicle_history=vehicles(later),
    )
    assert facts.carried_before("S29") is None


def test_vehicle_side_of_accompaniment_is_no_ride():
    facts = project(BillKey(194, "S2891"), [S2891], vehicle_history=vehicles())
    assert facts.rides == ()


def test_ride_without_vehicle_history_names_no_committee():
    facts = project(BillKey(194, "S185"), [history("S185", S185_ROWS)])
    (ride,) = facts.rides
    assert ride.committee_ids == ()


# S2712 was ordered to a third reading in the Senate; on October 1, 2026 it was read
# third and the new draft S3273 substituted for it, which went on to the House.
S = Branch.SENATE
S2712 = history(
    "S2712",
    [
        row("2025-11-10", A.REFERRED, "J23", branch=S),
        row("2026-03-19", A.REPORTED, branch=S),
        row("2026-04-09", A.REFERRED_TO_BILLS_IN_THIRD_READING, "S31", branch=S),
        row("2026-10-01", A.READ_THIRD, "S31", branch=S),
        row(
            "2026-10-01",
            A.UNKNOWN,
            branch=S,
            text="New draft substituted (Cyr), see S3273",
            related_bills=("S3273",),
        ),
    ],
)
S3273_ROWS = [
    row(
        "2026-10-01",
        A.UNKNOWN,
        branch=S,
        text="New draft (Cyr) of S2712",
        related_bills=("S2712",),
    ),
    row("2026-10-01", A.PASSED_TO_BE_ENGROSSED, branch=S),
    row("2026-10-05", A.REFERRED, "H52"),
]


def test_substituted_draft_rides_on_its_predecessor():
    facts = project(
        BillKey(194, "S3273"),
        [history("S3273", S3273_ROWS)],
        vehicle_history=vehicles(S2712),
    )
    ride = facts.carried_before("S31")
    assert ride is not None and ride.vehicle == "S2712"
    assert ride.accompanied_on == date(2026, 10, 1)
    assert ride.committee_ids == ("S31",)
    assert facts.carried_before("J23") is None
    assert S2712.observation_id in facts.source_observations


def test_substitution_must_end_the_predecessors_tenure():
    # The substitution named here falls on a day no tenure of the predecessor ended.
    later = history(
        "S2712",
        [
            row("2026-04-09", A.REFERRED_TO_BILLS_IN_THIRD_READING, "S31", branch=S),
            row(
                "2026-09-01",
                A.UNKNOWN,
                branch=S,
                text="New draft substituted (Cyr), see S3273",
                related_bills=("S3273",),
            ),
        ],
    )
    facts = project(
        BillKey(194, "S3273"),
        [history("S3273", S3273_ROWS)],
        vehicle_history=vehicles(later),
    )
    assert facts.carried_before("S31") is None


def test_predecessor_must_name_the_draft():
    other = history(
        "S2712",
        [
            row("2026-04-09", A.REFERRED_TO_BILLS_IN_THIRD_READING, "S31", branch=S),
            row("2026-10-01", A.READ_THIRD, "S31", branch=S),
            row(
                "2026-10-01",
                A.UNKNOWN,
                branch=S,
                text="New draft substituted, see S9999",
                related_bills=("S9999",),
            ),
        ],
    )
    facts = project(
        BillKey(194, "S3273"),
        [history("S3273", S3273_ROWS)],
        vehicle_history=vehicles(other),
    )
    assert facts.rides == ()


def test_substituted_draft_with_its_own_tenure_keeps_it():
    # S3235's own history orders it to a third reading; that tenure is judged.
    rows = [
        *S3273_ROWS[:1],
        row("2026-10-01", A.REFERRED_TO_BILLS_IN_THIRD_READING, "S31", branch=S),
        row("2026-10-02", A.READ_THIRD, "S31", branch=S),
    ]
    facts = project(
        BillKey(194, "S3273"), [history("S3273", rows)], vehicle_history=vehicles(S2712)
    )
    assert len(facts.tenures_for("S31")) == 1


# S3188: House Ways and Means recorded a vote on July 31, 2026, but the history never
# refers the bill there.
S3188_ROWS = [
    row("2026-07-30", A.PASSED_TO_BE_ENGROSSED, branch=S),
    [
        row(
            "2026-07-31",
            kind,
            committee,
            text=(
                "Committee recommended ought to pass with an amendment, striking out "
                "all after the enacting clause and inserting the text of H5634 , and "
                "referred to the committee on House Steering, Policy and Scheduling"
            ),
        )
        for kind, committee in ((A.REPORTED, None), (A.REFERRED, "H52"))
    ],
    row("2026-07-31", A.REPORTED, text="Committee reported that the matter be placed"),
]
SUPPLIED = SuppliedReferral(
    session=194,
    bill_id="S3188",
    committee_id="H34",
    referred_on=date(2026, 7, 30),
    anchor_on=date(2026, 7, 31),
    anchor_prefix="Committee recommended ought to pass with an amendment",
    note="House Ways and Means recorded a vote.",
)


def test_supplied_referral_opens_a_tenure_closed_by_its_report():
    obs = history("S3188", S3188_ROWS)
    facts = project(BillKey(194, "S3188"), [obs], gaps=(SUPPLIED,))
    (h34,) = facts.tenures_for("H34")
    assert h34.tenure_id == "194/S3188:H34:2026-07-30"
    assert h34.report is not None and h34.report.reported_on == date(2026, 7, 31)
    assert h34.referral.row_index == 1
    (h52,) = facts.tenures_for("H52")
    assert h52.referred_on == date(2026, 7, 31)
    assert facts.stale_gaps == ()


def test_supplied_referral_is_stale_once_the_history_has_it():
    rows = [row("2026-07-30", A.REFERRED, "H34"), *S3188_ROWS[1:]]
    facts = project(BillKey(194, "S3188"), [history("S3188", rows)], gaps=(SUPPLIED,))
    assert facts.tenures_for("H34")[0].referral.row_index == 0
    assert facts.stale_gaps == (SUPPLIED,)


def test_supplied_referral_without_its_anchor_is_stale():
    rows = [S3188_ROWS[0], S3188_ROWS[2]]
    facts = project(BillKey(194, "S3188"), [history("S3188", rows)], gaps=(SUPPLIED,))
    assert facts.tenures_for("H34") == ()
    assert facts.stale_gaps == (SUPPLIED,)


TRANSFER = UnrecordedTransfer(
    session=194,
    bill_id="H4319",
    committee_id="J45",
    transferred_on=date(2025, 6, 27),
    note="The transfer is not recorded.",
)


def test_unrecorded_transfer_applies_until_a_tenure_appears():
    rows = [
        row("2025-04-03", A.REFERRED, "J38"),
        row("2025-09-04", A.DISCHARGED, "J21"),
    ]
    facts = project(BillKey(194, "H4319"), [history("H4319", rows)], gaps=(TRANSFER,))
    assert facts.unrecorded_before("J45") == TRANSFER
    assert facts.unrecorded_before("J21") is None

    rows.insert(1, row("2025-06-27", A.REFERRED, "J45"))
    facts = project(BillKey(194, "H4319"), [history("H4319", rows)], gaps=(TRANSFER,))
    assert facts.unrecorded_before("J45") is None
    assert facts.stale_gaps == (TRANSFER,)


def test_gaps_apply_only_to_their_bill():
    facts = project(BILL, [history("H1234", S3188_ROWS)], gaps=(SUPPLIED, TRANSFER))
    assert facts.tenures_for("H34") == () and facts.stale_gaps == ()
