"""Tests for the Nager.Date Chinese-holiday helpers (DeepSeek peak logic).

_valid_holiday_dates must refuse every hostile shape (garbage, wrong types,
bad dates, wrong year, oversized lists) and accept only strict
{"date": "YYYY-MM-DD"} entries in the requested year.  _merge_holiday_days
must never let a corrupted cache value pollute the payload with single-char
entries (review round-1 P2: a string value iterates per character).

Run: python3 -m unittest tests.test_cn_holidays -v
"""

import importlib.util
import json
import os
import tempfile
import time
import datetime
import unittest
from pathlib import Path
from unittest import mock

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


class TestValidCachedDays(unittest.TestCase):
    """_valid_cached_days neutralizes a poisoned cache file on read."""

    YEAR = 2026

    def test_sanitizes_poison(self):
        poison = [
            "2026-10-12", "garbage", "x" * 1000000, "2026-13-99",
            "2027-01-01", "2026-02-17",
        ]
        self.assertEqual(
            fetch_usage._valid_cached_days(poison, self.YEAR),
            ["2026-02-17", "2026-10-12"],
        )

    def test_rejects_non_list_and_oversized(self):
        self.assertIsNone(fetch_usage._valid_cached_days("2026-01-01", self.YEAR))
        self.assertIsNone(
            fetch_usage._valid_cached_days(["2026-01-01"] * 60, self.YEAR)
        )


class TestLoadCnHolidays(unittest.TestCase):
    """_load_cn_holidays: stale fallback, no bump on failure, size cap."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmpdir, "cn-holidays.json")
        patcher = mock.patch.object(fetch_usage, "HOLIDAY_CACHE_FILE", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        now = datetime.datetime.now(datetime.timezone.utc)
        self.year = now.year
        self.good_day = "%d-01-01" % now.year

    def _read_cache(self):
        with open(self.cache, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_failed_fetch_keeps_stale_without_bump(self):
        stale = {"fetchedAt": 1, "attemptedAt": 0,
                 "days": {str(self.year): [self.good_day]}}
        with open(self.cache, "w", encoding="utf-8") as handle:
            json.dump(stale, handle)
        with mock.patch.object(
            fetch_usage, "_request_public_json",
            side_effect=ConnectionError("offline"),
        ):
            days, _ = fetch_usage._load_cn_holidays()
        self.assertEqual(days, [self.good_day])
        self.assertEqual(self._read_cache()["fetchedAt"], 1)

    def test_fresh_poisoned_cache_neutralized_without_network(self):
        poison = {"fetchedAt": int(time.time()), "attemptedAt": 0,
                  "days": {str(self.year): [self.good_day, "garbage",
                                              "x" * 100000] + ["2027-05-05"]}}
        with open(self.cache, "w", encoding="utf-8") as handle:
            json.dump(poison, handle)
        with mock.patch.object(
            fetch_usage, "_request_public_json",
            side_effect=AssertionError("must not fetch when fresh"),
        ):
            days, _ = fetch_usage._load_cn_holidays()
        self.assertEqual(days, [self.good_day])

    def test_oversized_cache_file_ignored(self):
        with open(self.cache, "w", encoding="utf-8") as handle:
            handle.write('{"days": {"x": "' + "y" * 2000000 + '"}}')
        with mock.patch.object(
            fetch_usage, "_request_public_json",
            side_effect=ConnectionError("offline"),
        ):
            days, _ = fetch_usage._load_cn_holidays()
        self.assertEqual(days, [])


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
