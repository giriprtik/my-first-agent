"""New requirement tests for agent.py."""
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def fresh_import():
    sys.modules.pop("agent", None)
    return importlib.import_module("agent")


class AgentRequirements(unittest.TestCase):
    def test_imports_without_api_key_and_builds_client_lazily(self):
        with patch("google.genai.Client") as client_cls:
            agent = fresh_import()
            self.assertEqual(client_cls.call_count, 0, "client must not be created at import time")
            agent.create_agent_session(agent.MODELS[0])
            agent.create_agent_session(agent.MODELS[0])
            self.assertEqual(client_cls.call_count, 1, "client must be created once, on first use")

    def test_telemetry_failure_does_not_resend_prompt(self):
        agent = fresh_import()
        sent_to = []

        def make(model, history=None):
            chat = MagicMock(); chat._model = model; chat.get_history.return_value = []
            def send(prompt):
                sent_to.append(model)
                r = MagicMock(); r.text = "reply"; r.usage_metadata = None
                return r
            chat.send_message.side_effect = send
            return chat

        with patch.object(agent, "create_agent_session", side_effect=make), \
             patch("builtins.open", side_effect=OSError("disk full")):
            _, text = agent.send_with_fallback(make(agent.MODELS[0]), "save a note")
        self.assertEqual(text, "reply")
        self.assertEqual(sent_to, [agent.MODELS[0]], "a logging failure must not replay the prompt on other models")

    def test_metrics_line_is_tagged_as_chat(self):
        agent = fresh_import()
        metrics = Path(agent.__file__).parent / "agent_metrics.jsonl"
        before = metrics.read_text(encoding="utf-8") if metrics.exists() else None
        try:
            chat = MagicMock(); chat._model = agent.MODELS[0]
            r = MagicMock(); r.text = "x"; r.usage_metadata = None
            chat.send_message.return_value = r
            agent.send_with_fallback(chat, "q")
            import json
            self.assertEqual(json.loads(metrics.read_text(encoding="utf-8").splitlines()[-1]).get("source"), "chat")
        finally:
            if before is None:
                metrics.unlink(missing_ok=True)
            else:
                metrics.write_text(before, encoding="utf-8")

    def test_search_web_tolerates_partial_results(self):
        agent = fresh_import()
        with patch.object(agent, "tavily_client") as tv:
            tv.search.return_value = {"results": [{"title": "only title", "url": "u"}, {}]}
            out = agent.search_web("q")
        self.assertIn("Title: only title", out)
        self.assertIn("URL: u", out)


if __name__ == "__main__":
    unittest.main()
