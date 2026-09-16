"""Tests for _fetch_collector() — Omarchy agent-usage collector reader.

The Omarchy agent collectors (omarchy-agent-usage-claude, etc.) normalize
utilization to a fraction (0..1) in a field named ``percent``.  The widget
bar and fetch_usage both expect real percentages (0..100), so
_fetch_collector must scale the fraction by 100 before returning.

Run: python3 -m unittest tests.test_fetch_collector -v
"""

import importlib.util
import json
import os
import tempfile
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


class TestFetchCollectorPercentScaling(unittest.TestCase):
    """_fetch_collector must scale Omarchy's 0..1 fractions to 0..100."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.usage_dir = os.path.join(self.tmpdir, "usage")
        os.makedirs(self.usage_dir, exist_ok=True)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_record(self, agent_id, record):
        path = os.path.join(self.usage_dir, agent_id + ".json")
        with open(path, "w") as fh:
            json.dump(record, fh)

    # -- happy-path: fractions scaled to real percentages ------------------

    def test_fraction_0_71_becomes_71_percent(self):
        """Weekly utilization at 71% (fraction 0.71) must render as 71.0."""
        self._write_record(
            "claude",
            {
                "limits": [
                    {"label": "5h", "percent": 0.01},
                    {"label": "7d", "percent": 0.71},
                ]
            },
        )
        with mock.patch.object(
            fetch_usage, "OMARCHY_USAGE_DIR", self.usage_dir
        ):
            result = fetch_usage._fetch_collector("claude")
        self.assertEqual(result["kind"], "percent")
        percents = {w["label"]: w["percent"] for w in result["windows"]}
        self.assertAlmostEqual(percents["5h"], 1.0, places=1)
        self.assertAlmostEqual(percents["7d"], 71.0, places=1)

    def test_fraction_at_1_0_becomes_100_percent(self):
        """100% utilization (fraction 1.0) must render as 100.0."""
        self._write_record(
            "codex",
            {
                "limits": [
                    {"label": "5h", "percent": 1.0},
                ]
            },
        )
        with mock.patch.object(
            fetch_usage, "OMARCHY_USAGE_DIR", self.usage_dir
        ):
            result = fetch_usage._fetch_collector("codex")
        self.assertEqual(result["kind"], "percent")
        self.assertEqual(result["windows"][0]["percent"], 100.0)

    def test_zero_utilization_stays_zero(self):
        """0% utilization (fraction 0.0) must render as 0.0."""
        self._write_record(
            "claude",
            {
                "limits": [
                    {"label": "5h", "percent": 0.0},
                ]
            },
        )
        with mock.patch.object(
            fetch_usage, "OMARCHY_USAGE_DIR", self.usage_dir
        ):
            result = fetch_usage._fetch_collector("claude")
        self.assertEqual(result["windows"][0]["percent"], 0.0)

    # -- edge cases --------------------------------------------------------

    def test_already_a_whole_percent_capped_at_100(self):
        """A value already > 1 is treated as a real percent and capped at 100.

        If a future collector writes 71.0 (not 0.71), the function should
        cap at 100 rather than producing 7100.
        """
        self._write_record(
            "claude",
            {
                "limits": [
                    {"label": "5h", "percent": 55.0},
                ]
            },
        )
        with mock.patch.object(
            fetch_usage, "OMARCHY_USAGE_DIR", self.usage_dir
        ):
            result = fetch_usage._fetch_collector("claude")
        self.assertEqual(result["windows"][0]["percent"], 55.0)

    def test_missing_percent_key_skipped(self):
        """Entries without 'percent' are silently skipped."""
        self._write_record(
            "claude",
            {"limits": [{"label": "5h"}]},
        )
        with mock.patch.object(
            fetch_usage, "OMARCHY_USAGE_DIR", self.usage_dir
        ):
            result = fetch_usage._fetch_collector("claude")
        # No valid windows → returns error
        self.assertIn("error", result)

    def test_no_file_returns_error(self):
        """Missing collector file returns an error record."""
        with mock.patch.object(
            fetch_usage, "OMARCHY_USAGE_DIR", self.usage_dir
        ):
            result = fetch_usage._fetch_collector("nonexistent")
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
