"""Action node definitions with pattern matching rules.

This module defines all recognized legislative action types and their
corresponding regex patterns, extractors, and normalizers.
"""

import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from beacon.domain.observations import ActionType
from beacon.scraper.history.committees import (
    normalize_committee_name,
    normalize_location,
)
from beacon.scraper.history.fields import (
    extract_bill_id,
    extract_committee_name,
    extract_date,
    extract_legislator_name,
    extract_location,
    extract_sections,
    extract_time_range,
)


@dataclass
class ActionNode:
    """Definition of an action type with patterns and extractors.

    Each node represents one type of legislative action (e.g., "REFERRED",
    "REPORTED") with:
    - Multiple regex patterns that match variations of that action
    - Named field extractors to pull structured data from matches
    - Normalizers to clean/standardize extracted data
    - Priority for disambiguation when multiple patterns match
    """

    action_type: ActionType  # "REFERRED", "REPORTED", etc.
    category: str  # Category: "referral-committee", etc.
    patterns: list[re.Pattern]  # Compiled regex patterns
    extractors: dict[str, Callable] = field(default_factory=dict)
    normalizers: dict[str, Callable] = field(default_factory=dict)
    priority: int = 100  # Lower = higher priority
    metadata: dict[str, Any] = field(default_factory=dict)

    def match(self, action_text: str) -> re.Match | None:
        """Try to match action text against this node's patterns.

        Args:
            action_text: Raw action text from bill history

        Returns:
            Match object if successful, None otherwise
        """
        for pattern in self.patterns:
            match = pattern.search(action_text)
            if match:
                return match
        return None

    def extract_data(self, match: re.Match) -> dict[str, Any]:
        """Extract structured data from a regex match.

        Args:
            match: Successful regex match object

        Returns:
            Dictionary of extracted field names to values
        """
        data = {}
        data.update(match.groupdict())
        for field_name, extractor in self.extractors.items():
            try:
                value = extractor(match, data)
                if value is not None:
                    data[field_name] = value
            except Exception:  # pylint: disable=broad-exception-caught
                # Extractors shouldn't break the whole process
                pass
        for field_name, normalizer in self.normalizers.items():
            if field_name in data:
                with contextlib.suppress(Exception):
                    data[field_name] = normalizer(data[field_name])
        return data

    def calculate_confidence(self, match: re.Match) -> float:
        """Calculate confidence score for this match.

        Args:
            match: Successful regex match object

        Returns:
            Confidence score from 0.0 to 1.0
        """
        num_groups = len(match.groupdict())
        base_confidence = min(0.7 + (num_groups * 0.1), 1.0)
        if "confidence" in self.metadata:
            return self.metadata["confidence"]
        return base_confidence


def create_action_nodes() -> list[ActionNode]:
    """Create all action node definitions.

    Returns:
        List of ActionNode objects defining all recognized action types
    """
    nodes = []

    # =========================================================================
    # REFERRAL ACTIONS
    # =========================================================================

    nodes.append(
        ActionNode(
            action_type=ActionType.REFERRED,
            category="referral-committee",
            patterns=[
                # Compound action: extract LAST referral (most recent committee)
                re.compile(
                    r"referred\s+to\s+the\s+committee\s+on\s+[^,]+(?:,\s+[^,]+)*,?\s+"
                    r"(?:reported|discharged)[^,]*,?\s+(?:rules\s+suspended\s+and\s+)?"
                    r"referred\s+(?:.*?\s+)?to\s+the\s+committee\s+on\s+(?P<committee>[^,]+(?:,\s*[^,]+)*)",
                    re.I,
                ),
                re.compile(
                    r"Read[;,]?\s+and\s+referred[,\s]+as\s+relates\s+to\s+sections?\s+"
                    r"(?P<sections>.+?)[,\s]+to\s+the\s+committee\s+on\s+(?P<committee>[^,]+(?:,\s*[^,]+)*)",
                    re.I,
                ),
                re.compile(
                    r"referred\s+to\s+the\s+committee\s+on\s+(?P<committee>[^,]+(?:,\s*[^,]+)*)",
                    re.I,
                ),
                re.compile(
                    r"Read[;,]?\s+and\s+referred\s+to\s+the\s+committee\s+on\s+(?P<committee>[^,]+(?:,\s*[^,]+)*)",
                    re.I,
                ),
                re.compile(
                    r"referred.*?to\s+the\s+committee\s+on\s+(?P<committee>[^,]+(?:,\s*[^,]+)*)",
                    re.I,
                ),
                re.compile(
                    r"Recommitted\s+to\s+the\s+committee\s+on\s+(?P<committee>[^,]+(?:,\s*[^,]+)*)",
                    re.I,
                ),
            ],
            extractors={
                "committee_name": extract_committee_name,
                "sections": extract_sections,
                "committee_id": lambda match, data: normalize_committee_name(
                    data.get("committee_name") or data.get("committee")
                ),
            },
            normalizers={},
            priority=10,
        )
    )
    nodes.append(
        ActionNode(
            action_type=ActionType.DISCHARGED,
            category="referral-committee",
            patterns=[
                re.compile(
                    r"Discharged\s+to\s+the\s+committee\s+on\s+(?P<committee>.+)", re.I
                ),
            ],
            extractors={
                "committee_name": extract_committee_name,
                "committee_id": lambda match, data: normalize_committee_name(
                    data.get("committee_name") or data.get("committee")
                ),
            },
            normalizers={},
            priority=10,
        )
    )

    # =========================================================================
    # REPORTED OUT ACTIONS
    # =========================================================================

    nodes.append(
        ActionNode(
            action_type=ActionType.REPORTED,
            category="committee-passage",
            patterns=[
                re.compile(
                    r"Reported\s+by\s+the\s+committee\s+on\s+(?P<committee>.+)", re.I
                ),
                re.compile(r"Reported\s+favorably\s+by\s+committee", re.I),
                re.compile(r"Reported\s+adversely\s+by\s+committee", re.I),
                re.compile(r"Committee\s+recommended\s+ought\s+to\s+pass", re.I),
                re.compile(r"Committee\s+recommended\s+ought\s+NOT\s+to\s+pass", re.I),
                re.compile(
                    r"ought\s+not\s+to\s+pass\s+\(under\s+Joint\s+Rule\s+\d+\)", re.I
                ),
                re.compile(
                    r"Reported(?:\s+favorably|\s+adversely)?(?:,\s+rules\s+suspended)?",
                    re.I,
                ),
                re.compile(
                    r"Reported(?:,\s+referred\s+to\s+the\s+committee\s+on\s+(?P<committee>.+))?",
                    re.I,
                ),
                re.compile(
                    r"Reported\s+on\s+a\s+part\s+of\s+(?P<related_bill>[HS]\d+)", re.I
                ),
            ],
            extractors={
                "committee_name": extract_committee_name,
                "related_bill": extract_bill_id,
                "committee_id": lambda match, data: (
                    normalize_committee_name(
                        data.get("committee_name") or data.get("committee")
                    )
                    if (data.get("committee_name") or data.get("committee"))
                    else None
                ),
            },
            normalizers={},
            priority=10,
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.STUDY_ORDER,
            category="committee-passage-unfavorable",
            patterns=[
                re.compile(r"\bstudy\s+order\b", re.I),
                re.compile(r"Accompanied\s+a\s+study\s+order", re.I),
            ],
            priority=15,
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.ACCOMPANIED,
            category="other",
            patterns=[
                re.compile(r"Accompanied\s+(?:by\s+)?(?P<related_bill>[HS]\d+)", re.I),
                re.compile(
                    r"Accompanied\s+a\s+new\s+draft[,\s]+(?:see\s+)?(?P<related_bill>[HS]\d+)",
                    re.I,
                ),
                re.compile(r"\bAccompanied\b", re.I),
            ],
            extractors={
                "related_bill": extract_bill_id,
            },
            priority=20,
        )
    )

    # =========================================================================
    # HEARING ACTIONS
    # =========================================================================

    nodes.append(
        ActionNode(
            action_type=ActionType.HEARING_SCHEDULED,
            category="hearing-scheduled",
            patterns=[
                re.compile(
                    r"Hearing\s+scheduled\s+\((?P<committee>.+?)\)\s+"
                    r"(?:to|for)\s+(?P<hearing_date>\d{2}/\d{2}/\d{4})\s+"
                    r"from\s+(?P<time_start>\d{2}:\d{2}\s+[AP]M)-(?P<time_end>\d{2}:\d{2}\s+[AP]M)\s+"
                    r"in\s+(?P<location>.+)",
                    re.I,
                ),
                re.compile(
                    r"Hearing\s+scheduled\s+\((?P<committee>.+?)\)\s+"
                    r"for\s+(?P<hearing_date>\d{2}/\d{2}/\d{4})\s+"
                    r"from\s+(?P<time_start>\d{2}:\d{2}\s+[AP]M)-(?P<time_end>\d{2}:\d{2}\s+[AP]M)",
                    re.I,
                ),
                re.compile(
                    r"(?:Public\s+)?[Hh]earing\s+scheduled\s+for\s+(?P<hearing_date>\d{2}/\d{2}/\d{4})",
                    re.I,
                ),
            ],
            extractors={
                "committee_name": extract_committee_name,
                "hearing_date": extract_date,
                "time_range": extract_time_range,
                "location": extract_location,
                "committee_id": lambda match, data: (
                    normalize_committee_name(
                        data.get("committee_name") or data.get("committee")
                    )
                    if (data.get("committee_name") or data.get("committee"))
                    else None
                ),
            },
            normalizers={
                "location": normalize_location,
            },
            priority=5,
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.HEARING_RESCHEDULED,
            category="hearing-rescheduled",
            patterns=[
                # With committee name in parentheses
                re.compile(
                    r"Hearing\s+rescheduled\s+\((?P<committee>.+?)\)\s+"
                    r"(?:to|for)\s+(?P<hearing_date>\d{2}/\d{2}/\d{4})\s+"
                    r"from\s+(?P<time_start>\d{2}:\d{2}\s+[AP]M)-(?P<time_end>\d{2}:\d{2}\s+[AP]M)\s+"
                    r"in\s+(?P<location>.+)",
                    re.I,
                ),
                re.compile(
                    r"Hearing\s+rescheduled\s+\((?P<committee>.+?)\)\s+"
                    r"(?P<hearing_date>\d{2}/\d{2}/\d{4})\s+"
                    r"from\s+(?P<time_start>\d{2}:\d{2}\s+[AP]M)-(?P<time_end>\d{2}:\d{2}\s+[AP]M)",
                    re.I,
                ),
                # Without committee name (uses context from previous actions)
                re.compile(
                    r"Hearing\s+rescheduled\s+(?:to|for)\s+(?P<hearing_date>\d{2}/\d{2}/\d{4})\s+"
                    r"from\s+(?P<time_start>\d{2}:\d{2}\s+[AP]M)-(?P<time_end>\d{2}:\d{2}\s+[AP]M)\s+"
                    r"in\s+(?P<location>.+)",
                    re.I,
                ),
                re.compile(
                    r"Hearing\s+rescheduled\s+to\s+(?P<hearing_date>\d{2}/\d{2}/\d{4})\s+"
                    r"from\s+(?P<time_start>\d{2}:\d{2}\s+[AP]M)-(?P<time_end>\d{2}:\d{2}\s+[AP]M)",
                    re.I,
                ),
            ],
            extractors={
                "committee_name": extract_committee_name,
                "hearing_date": extract_date,
                "time_range": extract_time_range,
                "location": extract_location,
                "committee_id": lambda match, data: (
                    normalize_committee_name(
                        data.get("committee_name") or data.get("committee")
                    )
                    if (data.get("committee_name") or data.get("committee"))
                    else None
                ),
            },
            normalizers={
                "location": normalize_location,
            },
            priority=5,
            metadata={"supersedes": ActionType.HEARING_SCHEDULED},
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.HEARING_LOCATION_CHANGED,
            category="hearing-updated",
            patterns=[
                re.compile(r"Hearing\s+location\s+changed", re.I),
            ],
            priority=5,
            metadata={"modifies": ActionType.HEARING_SCHEDULED},
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.HEARING_TIME_CHANGED,
            category="hearing-updated",
            patterns=[
                re.compile(r"Hearing\s+updated\s+to\s+[Nn]ew\s+[Ee]nd\s+[Tt]ime", re.I),
            ],
            priority=5,
            metadata={"modifies": ActionType.HEARING_SCHEDULED},
        )
    )

    # =========================================================================
    # DEADLINE EXTENSION ACTIONS
    # =========================================================================

    nodes.append(
        ActionNode(
            action_type=ActionType.REPORTING_EXTENDED,
            category="deadline-extension",
            patterns=[
                re.compile(
                    r"Reporting\s+date\s+extended\s+to\s+"
                    r"(?P<date>[A-Za-z]+,?\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})"
                    r"(?:\s+\(sections?\s+(?P<sections>.+?)\))?",
                    re.I,
                ),
            ],
            extractors={
                "new_deadline": extract_date,
                "sections": extract_sections,
            },
            priority=5,
        )
    )

    # =========================================================================
    # LEGISLATIVE PROCESS ACTIONS
    # =========================================================================

    nodes.append(
        ActionNode(
            action_type=ActionType.READ,
            category="reading-1",
            patterns=[
                re.compile(r"^Read[,;]?\s*$", re.I),
                re.compile(r"^Read[,;]?\s+and\s+referred", re.I),  # Covered by REFERRED
            ],
            priority=50,  # Lower priority, generic
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.READ_SECOND,
            category="reading-2",
            patterns=[
                re.compile(r"Read\s+second", re.I),
                re.compile(r"ordered\s+to\s+a\s+third\s+reading", re.I),
            ],
            priority=15,
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.READ_THIRD,
            category="reading-3",
            patterns=[
                re.compile(r"Read\s+third", re.I),
                re.compile(r"read\s+third\s+and\s+passed\s+to\s+be\s+engrossed", re.I),
            ],
            priority=15,
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.RULES_SUSPENDED,
            category="other",
            patterns=[
                re.compile(r"Rules\s+suspended", re.I),
            ],
            priority=20,
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.CONCURRED,
            category="passage",
            patterns=[
                re.compile(r"Senate\s+concurred", re.I),
                re.compile(r"House\s+concurred", re.I),
                re.compile(
                    r"concurred\s+in\s+the\s+(?P<branch>Senate|House)\s+amendment", re.I
                ),
            ],
            priority=15,
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.PASSED_TO_BE_ENGROSSED,
            category="passage",
            patterns=[
                re.compile(r"[Pp]assed\s+to\s+be\s+engrossed", re.I),
            ],
            priority=15,
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.ENACTED,
            category="passage",
            patterns=[
                re.compile(r"^Enacted$", re.I),
                re.compile(r"Enacted\s+and\s+laid\s+before\s+the\s+Governor", re.I),
            ],
            priority=15,
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.SIGNED,
            category="executive-signature",
            patterns=[
                re.compile(r"Signed\s+by\s+the\s+Governor[,\s]+(?P<chapter>.+)", re.I),
            ],
            priority=10,
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.PLACED_IN_ORDERS,
            category="other",
            patterns=[
                re.compile(
                    r"Committee\s+reported\s+that\s+the\s+matter\s+be\s+placed\s+in\s+the\s+"
                    r"Orders\s+of\s+the\s+Day\s+for\s+the\s+next\s+sitting",
                    re.I,
                ),
                re.compile(r"Taken\s+out\s+of\s+the\s+Orders\s+of\s+the\s+Day", re.I),
            ],
            priority=20,
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.REFERRED_TO_BILLS_IN_THIRD_READING,
            category="referral-committee",
            patterns=[
                re.compile(
                    r"Referred\s+to\s+the\s+committee\s+on\s+Bills\s+in\s+the\s+Third\s+Reading",
                    re.I,
                ),
            ],
            priority=15,
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.STEERING_REFERRAL,
            category="referral-committee",
            patterns=[
                re.compile(
                    r"Committee\s+recommended\s+ought\s+to\s+pass\s+and\s+referred\s+to\s+the\s+"
                    r"committee\s+on\s+(?P<committee>.+)",
                    re.I,
                ),
            ],
            extractors={
                "committee_name": extract_committee_name,
                "committee_id": lambda match, data: (
                    normalize_committee_name(
                        data.get("committee_name") or data.get("committee")
                    )
                    if (data.get("committee_name") or data.get("committee"))
                    else None
                ),
            },
            normalizers={},
            priority=10,
        )
    )

    # =========================================================================
    # AMENDMENT ACTIONS
    # =========================================================================

    nodes.append(
        ActionNode(
            action_type=ActionType.AMENDED,
            category="amendment-passage",
            patterns=[
                re.compile(
                    r"Amended\s+\((?P<legislator>.+?)\)\s+by\s+striking\s+out\s+all\s+after\s+the\s+"
                    r"enacting\s+clause\s+and\s+inserting\s+in\s+place\s+thereof\s+the\s+text\s+of\s+"
                    r"(?P<related_bill>[HS]\d+)",
                    re.I,
                ),
            ],
            extractors={
                "legislator": extract_legislator_name,
                "related_bill": extract_bill_id,
            },
            priority=15,
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.TITLE_CHANGED,
            category="other",
            patterns=[
                re.compile(r"Read\s+third\s+\(title\s+changed\)", re.I),
            ],
            priority=20,
        )
    )

    nodes.append(
        ActionNode(
            action_type=ActionType.EMERGENCY_PREAMBLE,
            category="other",
            patterns=[
                re.compile(r"Emergency\s+preamble\s+adopted", re.I),
            ],
            priority=20,
        )
    )

    return nodes


# Create the default action node registry
ACTION_NODES = create_action_nodes()
