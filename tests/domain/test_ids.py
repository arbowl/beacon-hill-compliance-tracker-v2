"""Tests for bill id parsing."""

import pytest

from beacon.domain.ids import bill_ids_in


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("House 318 An Act relative to sales", ("H318",)),
        ("H. 3443, An Act relative to libraries", ("H3443",)),
        ("Bills in Poll: H.2243, H.2244, and H.4205.", ("H2243", "H2244", "H4205")),
        ("(Senate, No.\n2728)", ("S2728",)),
        ("H.0016_Summary_Shapiro", ("H16",)),
        ("H.2253-S.1458_summary", ("H2253", "S1458")),
        ("(w/ H413 and H413 attached)", ("H413",)),
        ("Pursuant to Joint Rule 10 and House Rule 27", ()),
        ("Section 3 of THS 12 items 5", ()),
    ],
)
def test_bill_ids_in(text, expected):
    assert bill_ids_in(text) == expected
