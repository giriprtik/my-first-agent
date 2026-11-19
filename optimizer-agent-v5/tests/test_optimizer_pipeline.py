"""Tests for the optimizer upgrades: JSON mode, progress events, vacuous requirement tests, follow-up context."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import file_optimizer as fo
import project_optimizer as po

ORIGINAL = "def add(a, b):\n    return a + b\n"
NEW = 'def add(a, b):\n    """Adds."""\n    return a + b\n'
REG = "import unittest\nimport calc\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n"
REQ_REAL = "import unittest\nimport calc\nclass T(unittest.TestCase):\n    def test_doc(self):\n        self.assertTrue(calc.add.__doc__)\n"
REQ_VACUOUS = "import unittest\nimport calc\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(1, 1), 2)\n"


class FakeAgent:
    def __init__(self, req_answers, verdict='{"approved": true, "reason": "ok"}'):
        self.req_answers = list(req_answers)
        self.verdict = verdict
        self.calls = []

    def __call__(self, role, prompt, strip_fences=True, max_retries=4, json_mode=False):
        self.calls.append((role, json_mode, prompt))
        if role is fo.REGRESSION_TESTER_PROMPT:
            return REG
        if role is fo.REQUIREMENT_TESTER_PROMPT:
            return self.req_answers.pop(0)
        if role is fo.CODER_PROMPT:
            return NEW
        if role is fo.REVIEWER_PROMPT:
            return self.verdict
        raise AssertionError("unexpected role")

    def roles(self, role):
        return [c for c in self.calls if c[0] is role]


class JsonMode(unittest.TestCase):
    def test_reviewer_uses_json_mode(self):
        fake = FakeAgent([REQ_REAL])
        with patch.object(fo, "ask_agent", side_effect=fake):
            fo.optimize_single_file("calc.py", ORIGINAL, "audit", max_iterations=1)
        self.assertTrue(all(json_mode for _, json_mode, _ in fake.roles(fo.REVIEWER_PROMPT)))
        self.assertFalse(any(json_mode for _, json_mode, _ in fake.roles(fo.CODER_PROMPT)))

    def test_ask_agent_requests_json_mime_type(self):
        client = MagicMock()
        r = MagicMock(); r.text = '{"approved": true}'; r.usage_metadata = None
        client.models.generate_content.return_value = r
        with patch.object(fo, "get_client", return_value=client), patch.object(fo, "MIN_SECONDS_BETWEEN_CALLS", 0), \
             patch.object(fo, "_log_telemetry"):
            fo.ask_agent("r", "p", json_mode=True)
            self.assertEqual(client.models.generate_content.call_args.kwargs["config"].response_mime_type, "application/json")
            fo.ask_agent("r", "p")
            self.assertIsNone(client.models.generate_content.call_args.kwargs["config"].response_mime_type)

    def test_consistency_check_uses_json_mode(self):
        with patch.object(po, "ask_agent", return_value='{"consistent": true, "issues": []}') as ask:
            po.check_cross_file_consistency({"a.py": "import b\n", "b.py": "x = 1\n"}, {"a.py"}, "MAP")
        self.assertTrue(ask.call_args.kwargs.get("json_mode"))


class VacuousRequirementTests(unittest.TestCase):
    def test_tests_that_pass_on_original_are_regenerated(self):
        fake = FakeAgent([REQ_VACUOUS, REQ_REAL])
        with patch.object(fo, "ask_agent", side_effect=fake):
            res = fo.optimize_single_file("calc.py", ORIGINAL, "audit", max_iterations=1)
        self.assertEqual(len(fake.roles(fo.REQUIREMENT_TESTER_PROMPT)), 2)
        self.assertIn("ALREADY PASS", fake.roles(fo.REQUIREMENT_TESTER_PROMPT)[1][2])
        self.assertIn("calc.add.__doc__", res["test_code"])
        self.assertTrue(res["approved"])

    def test_still_vacuous_tests_are_dropped(self):
        fake = FakeAgent([REQ_VACUOUS, REQ_VACUOUS])
        with patch.object(fo, "ask_agent", side_effect=fake):
            res = fo.optimize_single_file("calc.py", ORIGINAL, "audit", max_iterations=1)
        self.assertNotIn("NEW REQUIREMENT TESTS", res["test_code"])
        self.assertIn("calc.add(2, 3)", res["test_code"])   # regression tests kept

    def test_real_tests_are_kept_first_time(self):
        fake = FakeAgent([REQ_REAL])
        with patch.object(fo, "ask_agent", side_effect=fake):
            fo.optimize_single_file("calc.py", ORIGINAL, "audit", max_iterations=1)
        self.assertEqual(len(fake.roles(fo.REQUIREMENT_TESTER_PROMPT)), 1)


class ProgressEvents(unittest.TestCase):
    def run_pipeline(self, verdict, **kw):
        events = []
        fake = FakeAgent([REQ_REAL] * 3, verdict=verdict)
        with patch.object(fo, "ask_agent", side_effect=fake):
            res = fo.optimize_single_file("calc.py", ORIGINAL, "audit", max_iterations=kw.get("iters", 1),
                                          progress_callback=lambda *e: events.append(e))
        return res, events

    def test_approved_run_reports_every_stage(self):
        res, events = self.run_pipeline('{"approved": true, "reason": "ok"}')
        final = {}
        for stage, state, _ in events:
            final[stage] = state
        stage_ids = [sid for sid, _ in fo.PIPELINE_STAGES]
        expected = {sid: "passed" for sid in stage_ids}
        expected["rescan"] = "skipped"                 # no scanner findings were passed in
        self.assertEqual({k: v for k, v in final.items() if k in stage_ids}, expected)
        self.assertEqual(final["iteration"], "running")
        self.assertTrue(res["approved"])

    def test_rejection_is_reported(self):
        _, events = self.run_pipeline('{"approved": false, "reason": "missing docs"}')
        self.assertIn(("review", "failed", "missing docs"), events)

    def test_iterations_are_counted(self):
        _, events = self.run_pipeline('{"approved": false, "reason": "x"}', iters=2)
        self.assertEqual([d for s, st, d in events if s == "iteration"], ["1/2", "2/2"])

    def test_broken_callback_does_not_abort(self):
        fake = FakeAgent([REQ_REAL])
        def explode(*_):
            raise RuntimeError("UI bug")
        with patch.object(fo, "ask_agent", side_effect=fake):
            res = fo.optimize_single_file("calc.py", ORIGINAL, "audit", max_iterations=1, progress_callback=explode)
        self.assertTrue(res["approved"])

    def test_non_testable_language_marks_test_stages_skipped(self):
        events = []
        with patch.object(fo, "ask_agent", side_effect=lambda role, *a, **k: '{"a": 2}' if role is fo.CODER_PROMPT else '{"approved": true, "reason": "ok"}'):
            fo.optimize_single_file("c.json", '{"a": 1}', "audit", max_iterations=1, progress_callback=lambda *e: events.append(e))
        self.assertIn(("baseline", "skipped", "No unit-test runner for JSON"), events)


class FollowupContext(unittest.TestCase):
    def test_project_files_reach_the_re_optimization(self):
        tree = {"pkg/calc.py": ORIGINAL, "pkg/helper.py": "X = 1\n"}
        with patch.object(fo, "ask_agent", return_value="REFACTOR_NEEDED: add a docstring"), \
             patch.object(fo, "optimize_single_file", return_value={"optimized_code": NEW, "approved": True}) as opt:
            fo.handle_code_followup("calc.py", ORIGINAL, ORIGINAL, "audit", "add docs",
                                    project_context_files=tree, target_rel_path="pkg/calc.py",
                                    progress_callback=print)
        kwargs = opt.call_args.kwargs
        self.assertIs(kwargs["project_context_files"], tree)
        self.assertEqual(kwargs["target_rel_path"], "pkg/calc.py")
        self.assertIs(kwargs["progress_callback"], print)


class ErrorTelemetry(unittest.TestCase):
    def test_failed_calls_are_logged_with_model(self):
        from google.genai.errors import ServerError
        with tempfile.TemporaryDirectory() as d:
            metrics = Path(d) / "m.jsonl"
            client = MagicMock()
            client.models.generate_content.side_effect = ServerError(503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}})
            with patch.object(fo, "get_client", return_value=client), patch.object(fo, "METRICS_FILE", metrics), \
                 patch.object(fo.time, "sleep"), patch.object(fo, "MIN_SECONDS_BETWEEN_CALLS", 0):
                with self.assertRaises(RuntimeError):
                    fo.ask_agent("r", "p", max_retries=2)
            lines = [json.loads(l) for l in metrics.read_text().splitlines()]
        self.assertEqual([l["model"] for l in lines], fo.OPTIMIZER_MODELS[:2])
        self.assertTrue(all("UNAVAILABLE" in l["error"] for l in lines))


if __name__ == "__main__":
    unittest.main()
