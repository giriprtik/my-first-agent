"""Baseline regression tests for app.py, run through Streamlit's AppTest harness (Gemini mocked)."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import importlib.util

# Locate app.py wherever it is importable from (sandbox run dir or project root), and never
# destroy a real telemetry file: tests save and restore it.
APP = Path(importlib.util.find_spec("app").origin)
METRICS = APP.parent / "agent_metrics.jsonl"


class _PreserveMetrics:
    def setUp(self):
        self._saved_metrics = METRICS.read_bytes() if METRICS.exists() else None
        METRICS.unlink(missing_ok=True)

    def tearDown(self):
        if self._saved_metrics is None:
            METRICS.unlink(missing_ok=True)
        else:
            METRICS.write_bytes(self._saved_metrics)


def run_app():
    with patch("google.genai.Client"):
        at = AppTest.from_file(str(APP), default_timeout=60)
        at.run()
    return at


class AppBaseline(_PreserveMetrics, unittest.TestCase):
    def test_renders_three_tabs_without_errors(self):
        at = run_app()
        self.assertFalse(at.exception, at.exception)
        self.assertEqual([t.label for t in at.tabs][:3],
                         ["💬 Research & File Agent", "🛠️ Single-File Studio", "🗂️ Full Project (.zip) Optimizer"])
        self.assertIn("🤖 Research & Sandboxed File Agent", [t.value for t in at.title])

    def test_sidebar_totals(self):
        METRICS.write_text("\n".join(json.dumps(m) for m in [
            {"model": "a", "latency_sec": 1.0, "total_tokens": 100},
            {"model": "b", "latency_sec": 3.0, "total_tokens": 50},
        ]) + "\n", encoding="utf-8")
        at = run_app()
        self.assertFalse(at.exception, at.exception)
        metrics = {m.label: m.value for m in at.sidebar.metric}
        self.assertEqual(metrics["Requests"], "2")
        self.assertEqual(metrics["Avg Latency"], "2.00s")
        self.assertEqual(metrics["Total Tokens"], "150")

    def test_format_tier_badge(self):
        with patch("google.genai.Client"):
            import importlib, sys
            sys.modules.pop("app", None)
            app = importlib.import_module("app")
        self.assertEqual(app.format_tier_badge(None, "none"), "🔍 Audited Only")
        self.assertEqual(app.format_tier_badge(False, "tests_ran"), "⚠️ Needs Review (tests_ran)")
        self.assertEqual(app.format_tier_badge(True, "tests_ran"), "✅ Verified (Tests Ran & Approved)")
        self.assertEqual(app.format_tier_badge(True, "syntax_only"), "✅ Syntax Checked & Approved")
        self.assertEqual(app.format_tier_badge(True, "none"), "🟡 Approved (Unverified Format — Review Diff)")


if __name__ == "__main__":
    unittest.main()
