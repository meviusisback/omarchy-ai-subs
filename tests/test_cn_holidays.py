"""Tests for the Nager.Date Chinese-holiday helpers (DeepSeek peak logic).

_valid_holiday_dates must refuse every hostile shape (garbage, wrong types,
bad dates, wrong year, oversized lists) and accept only strict
{"date": "YYYY-MM-DD"} entries in the requested year.  _merge_holiday_days
must never let a corrupted cache value pollute the payload with single-char
entries (review round-1 P2: a string value iterates per character).

Run: python3 -m unittest tests.test_cn_holidays -v
"""

import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "fetch_usage.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "fetch_usage_under_test", MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_usage = _load_module()


class TestValidHolidayDates(unittest.TestCase):
    """_valid_holiday_dates accepts strict entries, refuses everything else."""

    YEAR = 2026

    def test_accepts_minimal_valid_list(self):
        body = [{"date": "2026-01-01"}, {"date": "2026-02-17", "localName": "x"}]
        self.assertEqual(
            fetch_usage._valid_holiday_dates(body, self.YEAR),
            ["2026-01-01", "2026-02-17"],
        )

    def test_refuses_non_list(self):
        for bad in ("nope", None, 42, {"date": "2026-01-01"}):
            self.assertIsNone(fetch_usage._valid_holiday_dates(bad, self.YEAR))

    def test_refuses_bad_entries(self):
        for bad in (
            [{"date": 123}],
            [{"date": "17-02-2026"}],
            [{"date": "2026-13-99"}],
            [{"date": "2026-02-30"}],
            [{"date": "2027-01-01"}],
            [{"nope": "2026-01-01"}],
            ["2026-01-01"],
        ):
            self.assertIsNone(fetch_usage._valid_holiday_dates(bad, self.YEAR))

    def test_refuses_oversized_list(self):
        body = [{"date": "2026-01-%02d" % i} for i in range(1, 60)]
        self.assertIsNone(fetch_usage._valid_holiday_dates(body, self.YEAR))

    def test_dedupes_and_sorts(self):
        body = [{"date": "2026-05-01"}, {"date": "2026-01-01"}, {"date": "2026-05-01"}]
        self.assertEqual(
            fetch_usage._valid_holiday_dates(body, self.YEAR),
            ["2026-01-01", "2026-05-01"],
        )


class TestMergeHolidayDays(unittest.TestCase):
    """_merge_holiday_days merges requested years and ignores corrupt values."""

    def test_merges_requested_years(self):
        cached = {"days": {"2026": ["2026-01-01"], "2027": ["2027-01-01"]}}
        days, years = fetch_usage._merge_holiday_days(cached, [2026, 2027])
        self.assertEqual(days, ["2026-01-01", "2027-01-01"])
        self.assertEqual(years, ["2026", "2027"])

    def test_corrupted_string_value_yields_no_chars(self):
        # A corrupted cache (string instead of list) must not pollute the
        # payload with single characters — guard with isinstance(list).
        cached = {"days": {"2026": "2026-01-01"}}
        days, _ = fetch_usage._merge_holiday_days(cached, [2026])
        self.assertEqual(days, [])

    def test_missing_year_yields_empty(self):
        days, years = fetch_usage._merge_holiday_days({"days": {}}, [2026])
        self.assertEqual(days, [])
        self.assertEqual(years, ["2026"])


class TestNoHolidaysFlag(unittest.TestCase):
    """--no-holidays emits empty holiday lists with no network call."""

    def test_no_holidays_skips_fetch(self):
        import io
        import sys
        from unittest import mock
        argv = ["fetch_usage.py", "--env", "/dev/null", "--no-holidays"]
        with mock.patch.object(sys, "argv", argv):
            with mock.patch.object(
                fetch_usage, "_load_cn_holidays",
                side_effect=AssertionError("must not be called"),
            ):
                captured = io.StringIO()
                with mock.patch.object(sys, "stdout", captured):
                    fetch_usage.main()
        doc = __import__("json").loads(captured.getvalue())
        self.assertEqual(doc["cnHolidays"], [])
        self.assertEqual(doc["cnHolidaysYears"], [])
        self.assertEqual(len(doc["providers"]), len(fetch_usage.PROVIDER_SPECS))


if __name__ == "__main__":
    unittest.main()
