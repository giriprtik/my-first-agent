"""Baseline regression tests for agent.py (network clients mocked)."""
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

with patch("google.genai.Client"):   # the original builds its client at import time
    import agent


def fake_chat(model, reply="ok", fail=False):
    chat = MagicMock()
    chat._model = model
    chat.get_history.return_value = ["h"]
    if fail:
        chat.send_message.side_effect = RuntimeError(f"{model} down")
    else:
        resp = MagicMock()
        resp.text = reply
        resp.usage_metadata = MagicMock(prompt_token_count=3, candidates_token_count=4, total_token_count=7)
        chat.send_message.return_value = resp
    return chat


class AgentBaseline(unittest.TestCase):
    def setUp(self):
        self.metrics = Path(agent.__file__).parent / "agent_metrics.jsonl"
        self.before = self.metrics.read_text(encoding="utf-8") if self.metrics.exists() else None

    def tearDown(self):
        if self.before is None:
            self.metrics.unlink(missing_ok=True)
        else:
            self.metrics.write_text(self.before, encoding="utf-8")

    def test_models_and_prompt(self):
        self.assertEqual(agent.MODELS[0], "gemini-3.5-flash-lite")
        self.assertEqual(len(agent.MODELS), 4)
        self.assertIn("save_research_note", agent.system_prompt)

    def test_create_agent_session_config(self):
        with patch.object(agent, "gemini_client") as gc:
            agent.create_agent_session("m1", history=["x"])
        kwargs = gc.chats.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "m1")
        self.assertEqual(kwargs["history"], ["x"])
        cfg = kwargs["config"]
        self.assertEqual(cfg.temperature, 0.2)
        self.assertEqual(cfg.system_instruction, agent.system_prompt)
        self.assertEqual([t.__name__ for t in cfg.tools], ["search_web", "save_research_note"])

    def test_search_web_formatting(self):
        with patch.object(agent, "tavily_client") as tv:
            tv.search.return_value = {"results": [
                {"title": "A", "url": "u1", "content": "c1"},
                {"title": "B", "url": "u2", "content": "c2"},
            ]}
            out = agent.search_web("nepal")
        tv.search.assert_called_once_with(query="nepal", max_results=3)
        self.assertEqual(out, "Title: A\nURL: u1\nContent: c1\n\n---\nTitle: B\nURL: u2\nContent: c2\n")

    def test_first_model_success_logs_metrics(self):
        chat = fake_chat(agent.MODELS[0], "hi")
        new_chat, text = agent.send_with_fallback(chat, "q")
        self.assertIs(new_chat, chat)
        self.assertEqual(text, "hi")
        line = json.loads(self.metrics.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((line["model"], line["total_tokens"], line["prompt_tokens"]), (agent.MODELS[0], 7, 3))

    def test_falls_back_to_next_model_with_history(self):
        first = fake_chat(agent.MODELS[0], fail=True)
        second = fake_chat(agent.MODELS[1], "from second")
        with patch.object(agent, "create_agent_session", return_value=second) as cas:
            chat, text = agent.send_with_fallback(first, "q")
        cas.assert_called_once_with(agent.MODELS[1], history=["h"])
        self.assertIs(chat, second)
        self.assertEqual(text, "from second")

    def test_all_models_fail(self):
        with patch.object(agent, "create_agent_session", side_effect=lambda m, history=None: fake_chat(m, fail=True)):
            with self.assertRaises(RuntimeError):
                agent.send_with_fallback(fake_chat(agent.MODELS[0], fail=True), "q")

    def test_run_agent(self):
        with patch.object(agent, "create_agent_session", return_value=fake_chat(agent.MODELS[0], "done")):
            self.assertEqual(agent.run_agent("task"), "done")


if __name__ == "__main__":
    unittest.main()
