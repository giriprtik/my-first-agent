"""New requirement tests for file_optimizer.py."""
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import file_optimizer as fo


class StrictVerdict(unittest.TestCase):
    def test_only_boolean_true_approves(self):
        for raw in ['{"approved": "false", "reason": "x"}', '{"approved": "true"}', '{"approved": 1}',
                    "APPROVED", "approved - looks good", "", "null"]:
            with self.subTest(raw=raw):
                self.assertFalse(fo._parse_reviewer_verdict(raw)[0])
        self.assertTrue(fo._parse_reviewer_verdict('{"approved": true, "reason": "ok"}')[0])

    def test_non_object_json_does_not_crash(self):
        for raw in ["[]", "[true]", '"APPROVED"', "42"]:
            with self.subTest(raw=raw):
                approved, reason = fo._parse_reviewer_verdict(raw)
                self.assertFalse(approved)
                self.assertTrue(reason)


ORIG = "def f(a, b=1):\n    pass\nasync def g(x, *args, **kw):\n    pass\nNAMES = [1]\nclass C:\n    def m(self, y):\n        pass\n"


class CompatibleGate(unittest.TestCase):
    def check(self, new):
        return fo.verify_interface_preserved("m.py", ORIG, new)[0]

    def test_backward_compatible_additions_allowed(self):
        self.assertTrue(self.check(ORIG.replace("def f(a, b=1)", "def f(a, b=1, c=None)")))
        self.assertTrue(self.check(ORIG.replace("def f(a, b=1)", "def f(a, b=1, *, strict=False)")))
        self.assertTrue(self.check(ORIG.replace("def m(self, y)", "def m(self, y, z=0)")))
        self.assertTrue(self.check(ORIG.replace("def f(a, b=1)", "def f(a, b=1, *extra)")))
        self.assertTrue(self.check(ORIG + "def new_helper():\n    pass\n"))

    def test_breaking_changes_rejected(self):
        self.assertFalse(self.check(ORIG.replace("def f(a, b=1)", "def f(a, b=1, c)")))       # syntax error
        self.assertFalse(self.check(ORIG.replace("def f(a, b=1)", "def f(a, b)")))           # default removed
        self.assertFalse(self.check(ORIG.replace("def f(a, b=1)", "def f(b=1, a=2)")))       # reordered
        self.assertFalse(self.check(ORIG.replace("def f(a, b=1)", "def f(a, b=1, *, must)")))  # required kw-only
        self.assertFalse(self.check(ORIG.replace("def f(a, b=1)", "def f(a, /, b=1)")))      # now positional-only
        self.assertFalse(self.check(ORIG.replace("(x, *args, **kw)", "(x, **kw)")))          # *args removed
        self.assertFalse(self.check(ORIG.replace("(x, *args, **kw)", "(x, *args)")))         # **kw removed
        self.assertFalse(self.check(ORIG.replace("async def g", "def g")))

    def test_removed_module_level_name_rejected(self):
        ok, msg = fo.verify_interface_preserved("m.py", ORIG, ORIG.replace("NAMES = [1]\n", ""))
        self.assertFalse(ok)
        self.assertIn("NAMES", msg)
        self.assertTrue(fo.verify_interface_preserved("m.py", ORIG, ORIG.replace("NAMES = [1]", "NAMES: list = [1, 2]"))[0])


class Followup(unittest.TestCase):
    def test_formatted_refactor_prefix_recognised(self):
        for reply in ["**REFACTOR_NEEDED:** keep the 30s timeout", "  REFACTOR_NEEDED: keep the 30s timeout",
                      "`REFACTOR_NEEDED:` keep the 30s timeout", "refactor_needed: keep the 30s timeout"]:
            with self.subTest(reply=reply):
                with patch.object(fo, "ask_agent", return_value=reply), \
                     patch.object(fo, "optimize_single_file", return_value={"optimized_code": "x = 1\n"}):
                    out = fo.handle_code_followup("a.py", "x = 1\n", "x = 1\n", "", "keep timeout")
                self.assertEqual(out["action"], "refactor")
                self.assertEqual(out["instruction"], "keep the 30s timeout")


class SandboxPaths(unittest.TestCase):
    def test_target_outside_run_dir_is_refused(self):
        ok, tier, out = fo.run_isolated_verification("../outside.py", {"../outside.py": "x = 1\n"})
        self.assertFalse(ok)
        self.assertIn("outside", out.lower())

    def test_missing_target_reported_not_raised(self):
        ok, tier, out = fo.run_isolated_verification("missing.py", {"other.py": "x = 1\n"})
        self.assertFalse(ok)


class Pacing(unittest.TestCase):
    def test_concurrent_calls_are_paced(self):
        stamps = []
        client = MagicMock()
        def gen(**kw):
            stamps.append(time.perf_counter())
            r = MagicMock(); r.text = "ok"; r.usage_metadata = None
            return r
        client.models.generate_content.side_effect = gen
        with patch.object(fo, "get_client", return_value=client), patch.object(fo, "_log_telemetry"), \
             patch.object(fo, "MIN_SECONDS_BETWEEN_CALLS", 0.2), patch.object(fo, "_last_call_ts", 0.0):
            threads = [threading.Thread(target=fo.ask_agent, args=("r", "p")) for _ in range(4)]
            for t in threads: t.start()
            for t in threads: t.join()
        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertEqual(len(stamps), 4)
        self.assertTrue(all(g >= 0.18 for g in gaps), gaps)

    def test_exhausted_error_names_cause(self):
        from google.genai.errors import ServerError
        client = MagicMock()
        client.models.generate_content.side_effect = ServerError(500, {"error": {"code": 500, "message": "x", "status": "INTERNAL"}})
        with patch.object(fo, "get_client", return_value=client), patch.object(fo.time, "sleep"), \
             patch.object(fo, "MIN_SECONDS_BETWEEN_CALLS", 0):
            with self.assertRaises(RuntimeError) as ctx:
                fo.ask_agent("r", "p", max_retries=2)
        self.assertNotIn("Rate limit", str(ctx.exception))
        self.assertIn("INTERNAL", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
