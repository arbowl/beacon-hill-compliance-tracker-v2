"""The ruleset for the 194th General Court (2025-2026)."""

from beacon.domain.evaluation import default_roll_up
from beacon.evaluator.rulesets.base import Ruleset
from beacon.evaluator.rulesets.s194.notice import HearingNotice
from beacon.evaluator.rulesets.s194.posting import SummaryPosted, VotesPosted
from beacon.evaluator.rulesets.s194.report_out import ReportOutDeadline

RULESET = Ruleset(
    ruleset_id="s194",
    version="5",
    requirements=(
        ReportOutDeadline(),
        HearingNotice(),
        SummaryPosted(),
        VotesPosted(),
    ),
    roll_up=default_roll_up,
)
