"""Gemini quota handling: a spent daily quota skips that model immediately instead of sleeping and retrying it."""
import time
import unittest
from unittest.mock import MagicMock, patch

import file_optimizer as fo

DAILY = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota... Quota exceeded "
         "for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 500, model: "
         "gemini-3.5-flash-lite\\nPlease retry in 2h17m52.666167057s.', 'status': 'RESOURCE_EXHAUSTED', 'details': "
         "[{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{'quotaId': "
         "'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}, {'@type': 'type.googleapis.com/google.rpc.RetryInfo', "
         "'retryDelay': '8272s'}]}}")
PER_MINUTE = "429 RESOURCE_EXHAUSTED. Quota exceeded per minute. Please retry in 12.5s. 'retryDelay': '12s'"


def ok(text="done"):
    r = MagicMock(); r.text = text; r.usage_metadata = None
    return r


class QuotaHandling(unittest.TestCase):
    def setUp(self):
        fo._model_cooldown.clear(); fo._cooldown_reason.clear()
        self.patches = [patch.object(fo, "MIN_SECONDS_BETWEEN_CALLS", 0), patch.object(fo, "_log_telemetry"),
                        patch.object(fo, "_log_error"), patch.object(fo.time, "sleep")]
        self.sleep = [p.start() for p in self.patches][-1]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        fo._model_cooldown.clear(); fo._cooldown_reason.clear()

    def client(self, effects):
        c = MagicMock(); c.models.generate_content.side_effect = effects
        return c

    def models_called(self, c):
        return [k.kwargs["model"] for k in c.models.generate_content.call_args_list]

    def test_retry_delay_parsing(self):
        self.assertEqual(fo._parse_retry_delay(DAILY), 8272.0)
        self.assertAlmostEqual(fo._parse_retry_delay("Please retry in 2h17m52.5s."), 8272.5)
        self.assertEqual(fo._parse_retry_delay("retry in 41s"), 41.0)
        self.assertIsNone(fo._parse_retry_delay("no hint here"))

    def test_daily_quota_skips_the_model_without_sleeping(self):
        c = self.client([Exception(DAILY), ok(), ok()])
        with patch.object(fo, "get_client", return_value=c):
            self.assertEqual(fo.ask_agent("r", "p"), "done")
            self.assertEqual(self.models_called(c), fo.OPTIMIZER_MODELS[:2])
            self.sleep.assert_not_called()
            fo.ask_agent("r", "again")                   # later calls don't even try the spent model
        self.assertEqual(self.models_called(c)[2], fo.OPTIMIZER_MODELS[1])
        info = fo.model_cooldowns()[fo.OPTIMIZER_MODELS[0]]
        self.assertEqual(info["reason"], "daily quota used")
        self.assertGreater(info["until"] - time.time(), 8000)

    def test_all_models_spent_raises_a_clear_quota_error(self):
        c = self.client([Exception(DAILY)] * len(fo.OPTIMIZER_MODELS))
        with patch.object(fo, "get_client", return_value=c), self.assertRaises(fo.GeminiQuotaError) as ctx:
            fo.ask_agent("r", "p", max_retries=6)
        self.assertEqual(c.models.generate_content.call_count, len(fo.OPTIMIZER_MODELS))   # each tried once
        self.assertIn("out of quota", str(ctx.exception))
        self.assertIn("available again in 2h", str(ctx.exception))
        self.assertEqual(set(ctx.exception.resets), set(fo.OPTIMIZER_MODELS))
        with patch.object(fo, "get_client", return_value=c), self.assertRaises(fo.GeminiQuotaError):
            fo.ask_agent("r", "p")                       # fails fast now, no API call at all
        self.assertEqual(c.models.generate_content.call_count, len(fo.OPTIMIZER_MODELS))

    def test_per_minute_limit_moves_on_and_only_waits_when_nothing_is_left(self):
        c = self.client([Exception(PER_MINUTE), ok()])
        with patch.object(fo, "get_client", return_value=c):
            fo.ask_agent("r", "p")
        self.assertEqual(self.models_called(c), fo.OPTIMIZER_MODELS[:2])
        self.sleep.assert_not_called()
        self.assertEqual(fo.model_cooldowns()[fo.OPTIMIZER_MODELS[0]]["reason"], "per-minute limit")

        fo._model_cooldown.clear()
        c = self.client([Exception(PER_MINUTE)] * len(fo.OPTIMIZER_MODELS) + [ok()])
        with patch.object(fo, "get_client", return_value=c), patch.object(fo, "LONG_COOLDOWN_SEC", 90):
            with patch.object(fo, "available_optimizer_models",
                              side_effect=[fo.OPTIMIZER_MODELS[:]] + [[m] for m in fo.OPTIMIZER_MODELS[1:]] + [[], [fo.OPTIMIZER_MODELS[0]]]):
                self.assertEqual(fo.ask_agent("r", "p", max_retries=4), "done")
        self.assertTrue(self.sleep.called)              # every model was rate limited: wait for the first reset

    def test_unknown_model_is_skipped(self):
        c = self.client([Exception("404 NOT_FOUND. models/gemini-x is not found"), ok()])
        with patch.object(fo, "get_client", return_value=c):
            self.assertEqual(fo.ask_agent("r", "p"), "done")
        self.assertEqual(fo.model_cooldowns()[fo.OPTIMIZER_MODELS[0]]["reason"], "model not available")


class QuotaInTheUi(unittest.TestCase):
    def test_audit_shows_a_readable_error_instead_of_a_traceback(self):
        import importlib.util
        from pathlib import Path
        from streamlit.testing.v1 import AppTest
        app = Path(importlib.util.find_spec("app").origin)
        err = fo.GeminiQuotaError("All Gemini models are out of quota: gemini-3.5-flash-lite (daily quota used, "
                                  "available again in 2h17m)", {"gemini-3.5-flash-lite": time.time() + 8000})
        with patch("google.genai.Client"), patch("file_optimizer.audit_code", side_effect=err):
            at = AppTest.from_file(str(app), default_timeout=60)
            at.run()
            at.radio[0].set_value("Select a file from project folder").run()
            next(s for s in at.selectbox if s.label == "Target project file:").set_value("automate.py").run()
            next(b for b in at.button if b.label.startswith("🔍 Step 1")).click().run()
        self.assertFalse(at.exception, at.exception)
        self.assertTrue(any("out of quota" in e.value for e in at.error))
        self.assertIsNone(at.session_state["audit_report"] if "audit_report" in at.session_state else None)


if __name__ == "__main__":
    unittest.main()
