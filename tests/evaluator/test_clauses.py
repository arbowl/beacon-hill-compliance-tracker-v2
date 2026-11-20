"""Tests for the numbered-rule clause loader."""

import pytest

from beacon.domain.provenance import ClauseRef, RuleSource
from beacon.evaluator.clauses import ClauseIndex, parse_rules
from tests.conftest import RULES_DIR

SYNTHETIC = """Title Of The Rules

GENERAL.

1. The first rule has one paragraph.

     2. The second rule begins here.

     It continues in a second paragraph.

     [Amended Jan. 1, 2020.]

LIMIT OF TIME.

3. The third rule
spans two lines.

     1. An enumerated item that is not a rule start.

3A. A lettered rule. [Adopted 1971.]
"""


class TestParseRules:
    def test_rule_starts_and_paragraphs(self):
        rules = parse_rules(SYNTHETIC)
        assert list(rules) == ["1", "2", "3", "3A"]
        assert rules["1"] == ("The first rule has one paragraph.",)

    def test_amendment_notes_and_trailing_headings_excluded(self):
        rules = parse_rules(SYNTHETIC)
        assert rules["2"] == (
            "The second rule begins here.",
            "It continues in a second paragraph.",
        )

    def test_lines_joined_and_non_monotonic_numbers_are_content(self):
        rules = parse_rules(SYNTHETIC)
        assert rules["3"] == (
            "The third rule spans two lines.",
            "1. An enumerated item that is not a rule start.",
        )

    def test_inline_bracket_notes_kept(self):
        assert parse_rules(SYNTHETIC)["3A"] == ("A lettered rule. [Adopted 1971.]",)


class TestRealRules:
    def test_joint_rule_10_paragraphs(self, clause_index: ClauseIndex):
        p1 = clause_index.get(ClauseRef(RuleSource.JOINT, "10", 1))
        p2 = clause_index.get(ClauseRef(RuleSource.JOINT, "10", 2))
        assert p1.text.startswith("All joint committees")
        assert "by rule of the branch where the matter was filed" in p1.text
        assert p2.text.startswith("In the absence of final report deadline")
        assert "first Wednesday in December" in p2.text

    def test_joint_rule_10a_is_separate(self, clause_index: ClauseIndex):
        text = clause_index.get(ClauseRef(RuleSource.JOINT, "10A", 1)).text
        assert text.startswith("The form for all subjects")

    def test_house_rule_27_paragraphs(self, clause_index: ClauseIndex):
        paragraphs = clause_index.paragraphs(RuleSource.HOUSE, "27")
        assert len(paragraphs) == 3
        assert paragraphs[1].startswith("The House chair of each joint standing")
        assert paragraphs[2].startswith("When the time within which")

    def test_whole_rule_and_hash_stability(self, clause_index: ClauseIndex):
        whole = clause_index.get(ClauseRef(RuleSource.HOUSE, "27"))
        assert whole.text.count("\n\n") == 2
        reloaded = ClauseIndex.load(RULES_DIR)
        again = reloaded.get(ClauseRef(RuleSource.HOUSE, "27"))
        assert whole.text_hash == again.text_hash

    def test_missing_clause_raises(self, clause_index: ClauseIndex):
        with pytest.raises(KeyError):
            clause_index.get(ClauseRef(RuleSource.HOUSE, "27", 9))


def test_half_numbered_rule_follows_its_base():
    text = "12B. Base rule.\n\n12B " + chr(0xBD) + " . Half rule.\n\n13. Next rule.\n"
    assert list(parse_rules(text)) == ["12B", "12B-1/2", "13"]
