"""Large files go to heavy models first and get more iterations; small files are unchanged."""
import os
import unittest
from unittest.mock import MagicMock, patch

import file_optimizer as fo
from google.genai.errors import ServerError

SMALL = "def add(a, b):\n    return a + b\n"
LARGE = "\n".join(f"x{i} = {i}" for i in range(fo.LARGE_FILE_LINES + 10)) + "\n"
DAILY = "429 RESOURCE_EXHAUSTED. GenerateRequestsPerDayPerProjectPerModel-FreeTier retryDelay': '8272s'"


def ok(text="done"):
    r = MagicMock(); r.text = text; r.usage_metadata = None
    return r


class Tiers(unittest.TestCase):
    def setUp(self):
        fo._model_cooldown.clear(); fo._cooldown_reason.clear()
        self.patches = [patch.object(fo, "MIN_SECONDS_BETWEEN_CALLS", 0), patch.object(fo, "_log_telemetry"),
                        patch.object(fo, "_log_error"), patch.object(fo.time, "sleep"),
                        patch.dict(os.environ, {"LLM_FALLBACK": "off"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        fo._model_cooldown.clear(); fo._cooldown_reason.clear()

    def models(self, c):
        return [k.kwargs["model"] for k in c.models.generate_content.call_args_list]

    def test_size_threshold(self):
        self.assertFalse(fo.is_large_file(SMALL))
        self.assertTrue(fo.is_large_file(LARGE))

    def test_default_tier_uses_light_models(self):
        c = MagicMock(); c.models.generate_content.return_value = ok()
        with patch.object(fo, "get_client", return_value=c):
            fo.ask_agent("r", "p")
        self.assertEqual(self.models(c), [fo.OPTIMIZER_MODELS[0]])

    def test_heavy_tier_tries_heavy_first_then_light(self):
        c = MagicMock(); c.models.generate_content.side_effect = [Exception(DAILY), ok()]
        with patch.object(fo, "get_client", return_value=c), fo.use_heavy_models():
            fo.ask_agent("r", "p")
        self.assertEqual(self.models(c), [fo.HEAVY_OPTIMIZER_MODELS[0], fo.OPTIMIZER_MODELS[0]])
        self.assertEqual(fo.active_optimizer_models(), fo.OPTIMIZER_MODELS)   # tier resets after the block

    def test_audit_of_large_file_uses_heavy_model(self):
        c = MagicMock(); c.models.generate_content.return_value = ok("report")
        with patch.object(fo, "get_client", return_value=c):
            fo.audit_code("big.py", LARGE)
            fo.audit_code("small.py", SMALL)
        self.assertEqual(self.models(c), [fo.HEAVY_OPTIMIZER_MODELS[0], fo.OPTIMIZER_MODELS[0]])

    def test_optimize_picks_iterations_and_tier_by_size(self):
        seen = []
        def fake(filename, original_code, audit_report, extra_goals, max_iterations, *a, **kw):
            seen.append((max_iterations, fo.active_optimizer_models()[0]))
            return {}
        with patch.object(fo, "_optimize_single_file", side_effect=fake):
            fo.optimize_single_file("s.py", SMALL, "audit")
            fo.optimize_single_file("b.py", LARGE, "audit")
            fo.optimize_single_file("b.py", LARGE, "audit", max_iterations=2)   # explicit caller value wins
        self.assertEqual(seen, [(fo.DEFAULT_MAX_ITERATIONS, fo.OPTIMIZER_MODELS[0]),
                                (fo.LARGE_FILE_MAX_ITERATIONS, fo.HEAVY_OPTIMIZER_MODELS[0]),
                                (2, fo.HEAVY_OPTIMIZER_MODELS[0])])

    def test_large_file_status_message(self):
        msgs = []
        with patch.object(fo, "_optimize_single_file", return_value={}):
            fo.optimize_single_file("b.py", LARGE, "audit", status_callback=msgs.append)
        self.assertIn("Large file", msgs[0])


    def test_timing_out_heavy_model_is_skipped_on_later_calls(self):
        """The 07:36-07:40 log: gemini-3.5-flash 504'd after ~90s on EVERY call. Now it is skipped after the first."""
        deadline = ServerError(504, {"error": {"code": 504, "message": "Deadline expired", "status": "DEADLINE_EXCEEDED"}})
        c = MagicMock(); c.models.generate_content.side_effect = [deadline, ok("a"), ok("b"), ok("c")]
        with patch.object(fo, "get_client", return_value=c), fo.use_heavy_models():
            for _ in range(3):
                fo.ask_agent("r", "p")
        heavy = fo.HEAVY_OPTIMIZER_MODELS[0]
        self.assertEqual(self.models(c).count(heavy), 1)
        self.assertEqual(fo.model_cooldowns()[heavy]["reason"], "timing out (504)")
        fo.time.sleep.assert_not_called()                 # another model was available, so no pause

    def test_overloaded_model_cools_down(self):
        busy = ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})
        c = MagicMock(); c.models.generate_content.side_effect = [busy, ok()]
        with patch.object(fo, "get_client", return_value=c):
            fo.ask_agent("r", "p")
        self.assertEqual(fo.model_cooldowns()[fo.OPTIMIZER_MODELS[0]]["reason"], "overloaded (503)")

    def test_client_timeout_cools_down(self):
        c = MagicMock(); c.models.generate_content.side_effect = [Exception("Request timed out"), ok()]
        with patch.object(fo, "get_client", return_value=c):
            self.assertEqual(fo.ask_agent("r", "p"), "done")
        self.assertEqual(fo.model_cooldowns()[fo.OPTIMIZER_MODELS[0]]["reason"], "timing out")


if __name__ == "__main__":
    unittest.main()
