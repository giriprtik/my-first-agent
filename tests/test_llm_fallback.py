"""ask_agent falls back to non-Gemini providers (llm_router) only after Gemini gives up."""
import os
import unittest
from unittest.mock import MagicMock, patch

import file_optimizer as fo
from google.genai.errors import ServerError

DAILY = "429 RESOURCE_EXHAUSTED. Quota exceeded GenerateRequestsPerDayPerProjectPerModel-FreeTier. retryDelay': '8272s'"


def gemini_ok(text="gemini says hi"):
    r = MagicMock(); r.text = text; r.usage_metadata = None
    return r


def router_reply(text, provider="groq", model="openai/gpt-oss-120b"):
    r = MagicMock()
    r.choices = [MagicMock()]; r.choices[0].message.content = text
    r.router_provider = provider; r.model = model; r.usage = None
    return r


class FallbackToRouter(unittest.TestCase):
    def setUp(self):
        fo._model_cooldown.clear(); fo._cooldown_reason.clear()
        self.patches = [patch.object(fo, "MIN_SECONDS_BETWEEN_CALLS", 0), patch.object(fo, "_log_telemetry"),
                        patch.object(fo, "_log_fallback_telemetry"), patch.object(fo, "_log_error"),
                        patch.object(fo.time, "sleep"), patch.dict(os.environ, {"LLM_FALLBACK": "on"})]
        for p in self.patches:
            p.start()
        self.router = MagicMock()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        fo._model_cooldown.clear(); fo._cooldown_reason.clear()

    def gemini(self, effects):
        c = MagicMock(); c.models.generate_content.side_effect = effects
        return c

    def test_gemini_success_never_touches_the_router(self):
        with patch.object(fo, "get_client", return_value=self.gemini([gemini_ok()])), \
             patch.object(fo, "get_fallback_llm", return_value=self.router):
            self.assertEqual(fo.ask_agent("r", "p"), "gemini says hi")
        self.router.chat.assert_not_called()

    def test_all_gemini_quota_spent_goes_to_router(self):
        self.router.chat.return_value = router_reply("```python\nprint(1)\n```")
        with patch.object(fo, "get_client", return_value=self.gemini([Exception(DAILY)] * 3)), \
             patch.object(fo, "get_fallback_llm", return_value=self.router):
            self.assertEqual(fo.ask_agent("ROLE", "PROMPT", max_retries=6), "print(1)")
        msgs = self.router.chat.call_args.args[0]
        self.assertEqual(msgs, [{"role": "system", "content": "ROLE"}, {"role": "user", "content": "PROMPT"}])

    def test_gemini_server_errors_go_to_router(self):
        err = ServerError(503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}})
        self.router.chat.return_value = router_reply("ok")
        with patch.object(fo, "get_client", return_value=self.gemini([err] * 4)), \
             patch.object(fo, "get_fallback_llm", return_value=self.router):
            self.assertEqual(fo.ask_agent("r", "p", max_retries=2), "ok")

    def test_json_mode_asks_for_json_and_retries_without_response_format(self):
        self.router.chat.side_effect = [RuntimeError("All providers failed: 400"), router_reply('{"approved": true}')]
        with patch.object(fo, "get_client", return_value=self.gemini([Exception(DAILY)] * 3)), \
             patch.object(fo, "get_fallback_llm", return_value=self.router):
            self.assertEqual(fo.ask_agent("r", "p", strip_fences=False, json_mode=True, max_retries=6), '{"approved": true}')
        first, second = self.router.chat.call_args_list
        self.assertEqual(first.kwargs["response_format"], {"type": "json_object"})
        self.assertNotIn("response_format", second.kwargs)
        self.assertIn("JSON object", first.args[0][0]["content"])

    def test_router_failure_reraises_the_original_gemini_error(self):
        self.router.chat.side_effect = RuntimeError("All providers failed")
        with patch.object(fo, "get_client", return_value=self.gemini([Exception(DAILY)] * 3)), \
             patch.object(fo, "get_fallback_llm", return_value=self.router), \
             self.assertRaises(fo.GeminiQuotaError) as ctx:
            fo.ask_agent("r", "p", max_retries=6)
        self.assertIn("out of quota", str(ctx.exception))

    def test_bad_request_is_not_sent_to_the_router(self):
        with patch.object(fo, "get_client", return_value=self.gemini([ValueError("bad request")])), \
             patch.object(fo, "get_fallback_llm", return_value=self.router), self.assertRaises(ValueError):
            fo.ask_agent("r", "p")
        self.router.chat.assert_not_called()

    def test_disabled_by_env(self):
        with patch.dict(os.environ, {"LLM_FALLBACK": "off"}):
            self.assertIsNone(fo.get_fallback_llm())


if __name__ == "__main__":
    unittest.main()
