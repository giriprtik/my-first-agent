"""New requirement tests for app.py."""
import importlib
import json
import sys
import tempfile
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


def load_app_module():
    with patch("google.genai.Client"):
        sys.modules.pop("app", None)
        return importlib.import_module("app")


class Telemetry(_PreserveMetrics, unittest.TestCase):
    def test_corrupt_metrics_line_does_not_crash_the_app(self):
        METRICS.write_text(json.dumps({"model": "a", "latency_sec": 2.0, "total_tokens": 10}) + "\n"
                           + '{"model": "b", "latency_sec": 1.0, "total_tok' + "\n"   # torn write
                           + "[1, 2]\n", encoding="utf-8")
        with patch("google.genai.Client"):
            at = AppTest.from_file(str(APP), default_timeout=60)
            at.run()
        self.assertFalse(at.exception, at.exception)
        self.assertEqual({m.label: m.value for m in at.sidebar.metric}["Requests"], "1")

    def test_load_metrics_skips_bad_lines(self):
        app = load_app_module()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.jsonl"
            self.assertEqual(app.load_metrics(p), [])
            p.write_text('{"a": 1}\nnot json\n\n"str"\n{"b": 2}\n', encoding="utf-8")
            self.assertEqual(app.load_metrics(p), [{"a": 1}, {"b": 2}])


class ProjectSession(unittest.TestCase):
    def test_fingerprint_is_content_based(self):
        app = load_app_module()
        self.assertEqual(app.zip_fingerprint(b"abc"), app.zip_fingerprint(b"abc"))
        self.assertNotEqual(app.zip_fingerprint(b"abc"), app.zip_fingerprint(b"abd"))

    def test_clear_project_state_also_clears_widget_keys(self):
        app = load_app_module()
        state = {"proj_selected_scope": ["old.py"], "proj_scope_multiselect": ["old.py"], "proj_working_tree": {},
                 "proj_consistency": {}, "last_zip_name": "x", "chat": "keep me"}
        app.clear_project_state(state)
        self.assertEqual(state, {"chat": "keep me"})


class Promote(unittest.TestCase):
    def test_backup_before_overwrite(self):
        app = load_app_module()
        with tempfile.TemporaryDirectory() as d:
            live = Path(d) / "agent.py"
            live.write_text("original", encoding="utf-8")
            backup = app.backup_file(live, Path(d) / ".backups")
            self.assertEqual(backup.read_text(encoding="utf-8"), "original")
            self.assertEqual(backup.parent, Path(d) / ".backups")
            second = app.backup_file(live, Path(d) / ".backups")
            self.assertNotEqual(backup, second)


if __name__ == "__main__":
    unittest.main()
