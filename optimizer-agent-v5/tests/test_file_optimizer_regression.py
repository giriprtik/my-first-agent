"""Baseline regression tests for file_optimizer.py (Gemini mocked; no network, no sleeping)."""
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import file_optimizer as fo
from google.genai.errors import ServerError

SRC = '''
import os
CONST = 1
def plain(a, b=2):
    pass
async def fetch(url, *, timeout=3):
    pass
def run(a, /, b, *args, key=1, **kw):
    pass
def _hidden():
    pass
class Box:
    def open(self, x):
        pass
    def _seal(self):
        pass
class _Private:
    pass
'''


def response(text):
    r = MagicMock()
    r.text = text
    r.usage_metadata = None
    return r


class Interface(unittest.TestCase):
    def test_language_info(self):
        self.assertEqual(fo.get_language_info("a.py"), ("Python", "python", True))
        self.assertEqual(fo.get_language_info("Dockerfile"), ("Dockerfile", "dockerfile", False))
        self.assertEqual(fo.get_language_info("x.weird"), ("WEIRD", "text", False))
        self.assertEqual(fo.get_language_info("README"), ("Plain Text", "text", False))

    def test_public_symbols(self):
        self.assertEqual(fo.get_python_public_symbols(SRC), {
            "plain": "def plain(a, b)",
            "fetch": "async def fetch(url, *, timeout)",
            "run": "def run(a, /, b, *args, key, **kw)",
            "Box": "class Box",
            "Box.open": "def open(self, x)",
        })

    def test_gate_detects_removal_and_breaking_change(self):
        ok, msg = fo.verify_interface_preserved("m.py", SRC, SRC.replace("def plain(a, b=2)", "def plain2(a, b=2)"))
        self.assertFalse(ok); self.assertIn("Removed public symbol `def plain(a, b)`", msg)
        ok, msg = fo.verify_interface_preserved("m.py", SRC, SRC.replace("def open(self, x)", "def open(self)"))
        self.assertFalse(ok); self.assertIn("Changed signature `def open(self, x)`", msg)
        ok, msg = fo.verify_interface_preserved("m.py", SRC, SRC.replace("async def fetch", "def fetch"))
        self.assertFalse(ok)
        self.assertEqual(fo.verify_interface_preserved("m.py", SRC, SRC), (True, "All public Python symbols preserved."))
        self.assertTrue(fo.verify_interface_preserved("m.py", SRC, SRC.replace("def _hidden():", "def _renamed():"))[0])

    def test_gate_syntax_and_non_python(self):
        ok, msg = fo.verify_interface_preserved("m.py", SRC, "def broken(:\n")
        self.assertFalse(ok); self.assertTrue(msg.startswith("SyntaxError in refactored Python code"))
        self.assertEqual(fo.verify_interface_preserved("a.js", "x", "y"), (True, "Non-Python interface check skipped."))

    def test_extract_public_interface(self):
        self.assertIn("- async def fetch(url, *, timeout)", fo.extract_public_interface("m.py", SRC))
        self.assertEqual(fo.extract_public_interface("m.py", "x = 1\n"), "- (No public Python symbols)")
        self.assertIn("export function add(a, b)", fo.extract_public_interface("a.js", "export function add(a, b) {\n}\n"))
        self.assertEqual(fo.extract_public_interface("c.json", '{"a": 1, "b": 2}'), "- top-level key: `a`\n- top-level key: `b`")
        self.assertEqual(fo.extract_public_interface("c.yml", "a: 1\nb: 2\n"), "- top-level key: `a`\n- top-level key: `b`")
        self.assertIn("General file", fo.extract_public_interface("q.sql", "select 1"))


class Verdict(unittest.TestCase):
    def test_json_verdicts(self):
        self.assertEqual(fo._parse_reviewer_verdict('{"approved": true, "reason": "fine"}'), (True, "fine"))
        self.assertEqual(fo._parse_reviewer_verdict('```json\n{"approved": false, "reason": "bug"}\n```'), (False, "bug"))
        self.assertFalse(fo._parse_reviewer_verdict("REJECTED: nope")[0])


class Verification(unittest.TestCase):
    PASSING = "import unittest\nimport target\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertEqual(target.add(1, 2), 3)\n"

    def test_python_tests_pass_and_fail(self):
        good = {"target.py": "def add(a, b):\n    return a + b\n"}
        bad = {"target.py": "def add(a, b):\n    return a - b\n"}
        self.assertEqual(fo.run_isolated_verification("target.py", good, "test_t.py", self.PASSING)[:2], (True, "tests_ran"))
        self.assertEqual(fo.run_isolated_verification("target.py", bad, "test_t.py", self.PASSING)[:2], (False, "tests_ran"))

    def test_syntax_tiers(self):
        self.assertEqual(fo.run_isolated_verification("t.py", {"t.py": "x = 1\n"})[:2], (True, "syntax_only"))
        self.assertFalse(fo.run_isolated_verification("t.py", {"t.py": "def (:\n"})[0])
        self.assertEqual(fo.run_isolated_verification("c.json", {"c.json": '{"a": 1}'})[:2], (True, "syntax_only"))
        self.assertFalse(fo.run_isolated_verification("c.json", {"c.json": '{"a": '})[0])
        self.assertEqual(fo.run_isolated_verification("q.sql", {"q.sql": "select 1"})[:2], (True, "none"))
        self.assertEqual(fo.run_isolated_verification("t.py", {"t.py": "   \n"}), (False, "none", "ERROR: Refactored file is empty."))

    def test_child_does_not_inherit_api_keys(self):
        probe = ("import os, unittest\nclass T(unittest.TestCase):\n    def test_env(self):\n"
                 "        self.assertNotIn('GEMINI_API_KEY', os.environ)\n")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "secret"}):
            ok, tier, out = fo.run_isolated_verification("t.py", {"t.py": "x = 1\n"}, "test_env.py", probe)
        self.assertTrue(ok, out)

    def test_timeout_reported(self):
        loop = "import unittest\nclass T(unittest.TestCase):\n    def test_hang(self):\n        while True: pass\n"
        import subprocess
        with patch.object(fo.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertEqual(fo.run_isolated_verification("t.py", {"t.py": "x=1\n"}, "test_h.py", loop)[:2], (False, "none"))


class AskAgent(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(fo, "MIN_SECONDS_BETWEEN_CALLS", 0), patch.object(fo.time, "sleep"),
                        patch.object(fo, "_log_telemetry")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_strips_fences_and_falls_back_on_server_error(self):
        client = MagicMock()
        client.models.generate_content.side_effect = [
            ServerError(503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}}),
            response("```python\nprint(1)\n```"),
        ]
        with patch.object(fo, "get_client", return_value=client):
            self.assertEqual(fo.ask_agent("role", "p"), "print(1)")
        models = [c.kwargs["model"] for c in client.models.generate_content.call_args_list]
        self.assertEqual(models, fo.OPTIMIZER_MODELS[:2])

    def test_keeps_fences_when_asked(self):
        client = MagicMock(); client.models.generate_content.return_value = response("```x```")
        with patch.object(fo, "get_client", return_value=client):
            self.assertEqual(fo.ask_agent("r", "p", strip_fences=False), "```x```")

    def test_non_retryable_error_raises(self):
        client = MagicMock(); client.models.generate_content.side_effect = ValueError("bad request")
        with patch.object(fo, "get_client", return_value=client):
            with self.assertRaises(ValueError):
                fo.ask_agent("r", "p")

    def test_gives_up_after_max_retries(self):
        client = MagicMock()
        client.models.generate_content.side_effect = ServerError(500, {"error": {"code": 500, "message": "x", "status": "INTERNAL"}})
        with patch.object(fo, "get_client", return_value=client):
            with self.assertRaises(RuntimeError):
                fo.ask_agent("r", "p", max_retries=2)
        self.assertEqual(client.models.generate_content.call_count, 2)


class Loop(unittest.TestCase):
    ORIGINAL = "def add(a, b):\n    return a + b\n"
    REG = "import unittest\nimport calc\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n"
    REQ = "import unittest\nimport calc\nclass T(unittest.TestCase):\n    def test_doc(self):\n        self.assertTrue(calc.add.__doc__)\n"
    NEW = 'def add(a, b):\n    """Adds."""\n    return a + b\n'

    def fake_agent(self, verdict):
        def answer(role, prompt, strip_fences=True, max_retries=4, json_mode=False):
            if role is fo.REGRESSION_TESTER_PROMPT: return self.REG
            if role is fo.REQUIREMENT_TESTER_PROMPT: return self.REQ
            if role is fo.CODER_PROMPT: return self.NEW
            if role is fo.REVIEWER_PROMPT: return verdict
            raise AssertionError(role)
        return answer

    def test_approved_path(self):
        with patch.object(fo, "ask_agent", side_effect=self.fake_agent('{"approved": true, "reason": "ok"}')):
            res = fo.optimize_single_file("calc.py", self.ORIGINAL, "audit", max_iterations=2)
        self.assertTrue(res["approved"])
        self.assertTrue(res["baseline_verified"])
        self.assertEqual(res["verification_tier"], "tests_ran")
        self.assertEqual(res["optimized_code"], self.NEW)
        self.assertIn('+    """Adds."""', res["diff"])
        self.assertIn("NEW REQUIREMENT TESTS", res["test_code"])

    def test_rejected_path(self):
        with patch.object(fo, "ask_agent", side_effect=self.fake_agent('{"approved": false, "reason": "nope"}')):
            res = fo.optimize_single_file("calc.py", self.ORIGINAL, "audit", max_iterations=2)
        self.assertFalse(res["approved"])
        self.assertEqual(len(res["logs"]), 2)
        self.assertIn("Reviewer rejected", res["logs"][0])

    def test_interface_violation_is_fed_back(self):
        def answer(role, prompt, strip_fences=True, max_retries=4, json_mode=False):
            if role is fo.CODER_PROMPT: return "def plus(a, b):\n    return a + b\n"
            return self.REG if role is fo.REGRESSION_TESTER_PROMPT else self.REQ
        with patch.object(fo, "ask_agent", side_effect=answer):
            res = fo.optimize_single_file("calc.py", self.ORIGINAL, "audit", max_iterations=1)
        self.assertFalse(res["approved"])
        self.assertIn("Removed public symbol", res["logs"][0])

    def test_followup_question_and_change(self):
        with patch.object(fo, "ask_agent", return_value="Because it is faster."):
            out = fo.handle_code_followup("calc.py", self.ORIGINAL, self.NEW, "audit", "why?")
        self.assertEqual(out, {"action": "answer", "answer": "Because it is faster."})
        with patch.object(fo, "ask_agent", return_value="REFACTOR_NEEDED: rename nothing"), \
             patch.object(fo, "optimize_single_file", return_value={"optimized_code": self.NEW, "approved": True}) as opt:
            out = fo.handle_code_followup("calc.py", self.ORIGINAL, self.NEW, "audit", "do it")
        self.assertEqual((out["action"], out["instruction"]), ("refactor", "rename nothing"))
        self.assertIn("MANDATORY USER CORRECTION", opt.call_args.kwargs["extra_goals"])


if __name__ == "__main__":
    unittest.main()
