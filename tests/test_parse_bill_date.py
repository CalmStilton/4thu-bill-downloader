import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from download_bills import parse_bill_date  # noqa: E402


@pytest.mark.parametrize(
    "text, expected",
    [
        ("11th August 2026", datetime(2026, 8, 11)),  # old long format
        ("1st March 2026", datetime(2026, 3, 1)),
        ("11 Aug 2026", datetime(2026, 8, 11)),  # current en-GB short format
        ("11 Sept 2026", datetime(2026, 9, 11)),  # Chrome's en-GB abbreviation
        ("11 Sep 2026", datetime(2026, 9, 11)),
        ("05 Jan 2027", datetime(2027, 1, 5)),
        ("  11 Aug 2026  ", datetime(2026, 8, 11)),
    ],
)
def test_parses_known_formats(text, expected):
    assert parse_bill_date(text) == expected


@pytest.mark.parametrize("text", ["", "Aug 2026", "11 Foo 2026", "INV1402505"])
def test_bad_input_raises_value_error(text):
    with pytest.raises(ValueError):
        parse_bill_date(text)
