"""Tests for the streaming agent loop, research tools and model validation (no network)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from google.genai import types

import agent


def chunk(*parts):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=5, candidates_token_count=7, total_token_count=12
        ),
    )


def text(t, thought=False):
    return types.Part(text=t, thought=thought or None)


def call(name, **args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


class FakeChat:
    """Scripted chat: each send_message_stream() returns the next list of chunks."""

    def __init__(self, turns, model=agent.MODELS[0], fail=None):
        self._model = model
        self._jb_manual_tools = True
        self.turns = list(turns)
        self.sent = []
        self.fail = fail

    def get_history(self):
        return []

    def send_message_stream(self, message):
        if self.fail:
            raise self.fail
        self.sent.append(message)
        return iter(self.turns.pop(0))


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.metrics = Path(self.tmp.name) / "metrics.jsonl"
        self.patches = [
            patch.object(agent, "METRICS_PATH", self.metrics),
            patch.object(agent, "active_models", return_value=list(agent.MODELS)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def metric_lines(self):
        if not self.metrics.exists():
            return []
        return [json.loads(line) for line in self.metrics.read_text().splitlines()]


class StreamReply(Isolated):
    def test_text_streams_in_order(self):
        chat = FakeChat([[chunk(text("Hello ")), chunk(text("world"))]])
        events = list(agent.stream_reply(chat, "hi"))
        self.assertEqual(events[0], {"type": "model", "model": agent.MODELS[0]})
        self.assertEqual([e["delta"] for e in events if e["type"] == "text"], ["Hello ", "world"])
        done = events[-1]
        self.assertEqual((done["type"], done["text"], done["tools_used"]), ("done", "Hello world", 0))
        self.assertIs(done["chat"], chat)
        self.assertEqual(self.metric_lines()[-1]["total_tokens"], 12)

    def test_thoughts_are_not_shown(self):
        chat = FakeChat([[chunk(text("planning...", thought=True), text("Answer"))]])
        events = list(agent.stream_reply(chat, "hi"))
        self.assertEqual([e["delta"] for e in events if e["type"] == "text"], ["Answer"])

    def test_tool_call_runs_and_result_is_sent_back(self):
        chat = FakeChat([[chunk(call("search_web", query="nepal"))], [chunk(text("Done."))]])
        with patch.object(agent, "tavily_client") as tv:
            tv.search.return_value = {"results": [{"title": "Kathmandu Post", "url": "https://kp.example/a", "content": "c"}]}
            events = list(agent.stream_reply(chat, "news?"))
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["model", "tool_call", "tool_result", "text", "done"])
        self.assertEqual(events[1]["args"], {"query": "nepal"})
        self.assertTrue(events[2]["ok"])
        self.assertEqual(events[-1]["sources"], [{"title": "Kathmandu Post", "url": "https://kp.example/a"}])
        reply_parts = chat.sent[1]
        self.assertEqual(reply_parts[0].function_response.name, "search_web")
        self.assertIn("https://kp.example/a", reply_parts[0].function_response.response["result"])
        self.assertEqual(self.metric_lines()[1]["source"], "tool")

    def test_save_waits_for_confirmation(self):
        chat = FakeChat([[chunk(call("save_research_note", filename="n.md", content="body"))], [chunk(text("Ready."))]])
        with patch.object(agent, "save_research_note") as save:
            events = list(agent.stream_reply(chat, "save it", confirm_saves=True))
        save.assert_not_called()
        pending = [e for e in events if e["type"] == "pending_save"]
        self.assertEqual(pending, [{"type": "pending_save", "filename": "n.md", "content": "body"}])
        self.assertEqual(events[-1]["pending_saves"], [{"filename": "n.md", "content": "body"}])
        self.assertIn("confirm", chat.sent[1][0].function_response.response["result"])

    def test_save_runs_directly_without_confirmation(self):
        chat = FakeChat([[chunk(call("save_research_note", filename="n.md", content="body"))], [chunk(text("Saved."))]])
        with patch.object(agent, "save_research_note", return_value="Successfully saved note to agent_outputs/n.md") as save:
            events = list(agent.stream_reply(chat, "save it", confirm_saves=False))
        save.assert_called_once_with("n.md", "body")
        self.assertFalse([e for e in events if e["type"] == "pending_save"])

    def test_tool_budget_is_enforced(self):
        chat = FakeChat([
            [chunk(call("search_web", query="a"), call("search_web", query="b"), call("search_web", query="c"))],
            [chunk(text("Answer."))],
        ])
        with patch.object(agent, "tavily_client") as tv:
            tv.search.return_value = {"results": []}
            events = list(agent.stream_reply(chat, "q", tool_budget=2))
        self.assertEqual(tv.search.call_count, 2)
        self.assertEqual([e["type"] for e in events].count("tool_skipped"), 1)
        self.assertEqual(events[-1]["tools_used"], 2)
        self.assertIn("budget", chat.sent[1][2].function_response.response["result"])

    def test_loop_is_bounded_even_if_model_keeps_calling(self):
        turns = [[chunk(call("search_web", query=str(i)))] for i in range(10)]
        chat = FakeChat(turns)
        with patch.object(agent, "tavily_client") as tv:
            tv.search.return_value = {"results": []}
            events = list(agent.stream_reply(chat, "q", tool_budget=2))
        self.assertEqual(events[-1]["type"], "done")
        self.assertLessEqual(len(chat.sent), 4)

    def test_tool_errors_are_reported_not_raised(self):
        chat = FakeChat([[chunk(call("search_web", query="x"), call("launch_rockets"))], [chunk(text("Sorry."))]])
        with patch.object(agent, "tavily_client") as tv:
            tv.search.side_effect = RuntimeError("tavily down")
            events = list(agent.stream_reply(chat, "q"))
        results = [e for e in events if e["type"] == "tool_result"]
        self.assertEqual([r["ok"] for r in results], [False, False])
        self.assertIn("tavily down", chat.sent[1][0].function_response.response["result"])
        self.assertIn("unknown tool", chat.sent[1][1].function_response.response["result"])

    def test_falls_back_to_next_model_before_any_output(self):
        broken = FakeChat([], fail=RuntimeError("503"))
        healthy = FakeChat([[chunk(text("from model 2"))]], model=agent.MODELS[1])
        with patch.object(agent, "create_agent_session", return_value=healthy) as cas:
            events = list(agent.stream_reply(broken, "q"))
        cas.assert_called_once_with(agent.MODELS[1], history=[], manual_tools=True)
        self.assertEqual(events[0]["model"], agent.MODELS[1])
        self.assertEqual(events[-1]["text"], "from model 2")
        self.assertIn("error", self.metric_lines()[0])

    def test_no_retry_after_output_started(self):
        def broken_stream():
            yield chunk(text("partial "))
            raise RuntimeError("connection reset")

        chat = FakeChat([broken_stream()])
        with patch.object(agent, "create_agent_session") as cas:
            with self.assertRaises(RuntimeError):
                list(agent.stream_reply(chat, "q"))
        cas.assert_not_called()

    def test_all_models_failing_raises(self):
        with patch.object(agent, "create_agent_session", side_effect=lambda m, **k: FakeChat([], model=m, fail=RuntimeError("down"))):
            with self.assertRaises(RuntimeError):
                list(agent.stream_reply(FakeChat([], fail=RuntimeError("down")), "q"))


class ResearchTools(Isolated):
    def test_deep_search_includes_summary_answer(self):
        with patch.object(agent, "tavily_client") as tv:
            tv.search.return_value = {"answer": "Short answer.", "results": [{"title": "T", "url": "https://u", "content": "c"}]}
            out = agent.deep_search("q")
        tv.search.assert_called_once_with(query="q", search_depth="advanced", include_answer=True, max_results=5)
        self.assertTrue(out.startswith("Summary answer: Short answer."))
        self.assertIn("URL: https://u", out)

    def test_read_page_validates_and_truncates(self):
        self.assertIn("only accepts", agent.read_page("file:///etc/passwd"))
        self.assertIn("only accepts", agent.read_page("javascript:alert(1)"))
        with patch.object(agent, "tavily_client") as tv:
            tv.extract.return_value = {"results": [{"url": "https://a.example", "raw_content": "x" * (agent.MAX_PAGE_CHARS + 50)}]}
            out = agent.read_page("https://a.example")
            self.assertIn(f"first {agent.MAX_PAGE_CHARS} of", out)
            tv.extract.return_value = {"results": [], "failed_results": [{"url": "https://b.example"}]}
            self.assertTrue(agent.read_page("https://b.example").startswith("Could not read"))

    def test_read_page_sources(self):
        self.assertEqual(agent._extract_sources("read_page", {"url": "https://news.example/x"}, "..."),
                         [{"title": "news.example", "url": "https://news.example/x"}])


class ModelValidation(unittest.TestCase):
    def setUp(self):
        agent._validated_models = None
        agent._unavailable_models = []

    tearDown = setUp

    def test_filters_to_served_models_and_caches(self):
        listed = [MagicMock(), MagicMock()]
        listed[0].name = "models/gemini-3.5-flash-lite"
        listed[1].name = "models/gemini-3.7-flash"
        with patch.object(agent, "gemini_client") as gc:
            gc.models.list.return_value = listed
            self.assertEqual(agent.active_models(), ["gemini-3.5-flash-lite", "gemini-3.7-flash"])
            self.assertEqual(agent.active_models(), ["gemini-3.5-flash-lite", "gemini-3.7-flash"])
            self.assertEqual(gc.models.list.call_count, 1)
            status = agent.model_status()
        self.assertEqual(status["unavailable"], ["gemini-3.1-flash-lite", "gemini-4-hydrogen"])

    def test_listing_failure_falls_back_without_caching(self):
        with patch.object(agent, "gemini_client") as gc:
            gc.models.list.side_effect = RuntimeError("offline")
            self.assertEqual(agent.active_models(), agent.MODELS)
            agent.active_models()
            self.assertEqual(gc.models.list.call_count, 2)

    def test_manual_session_config(self):
        with patch.object(agent, "gemini_client") as gc:
            agent.create_agent_session("m", manual_tools=True)
        cfg = gc.chats.create.call_args.kwargs["config"]
        self.assertTrue(cfg.automatic_function_calling.disable)
        self.assertEqual([t.__name__ for t in cfg.tools], ["search_web", "deep_search", "read_page", "save_research_note"])
        self.assertEqual(cfg.system_instruction, agent.AGENT_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
