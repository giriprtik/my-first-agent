"""Tests for the new UI: rendering helpers, and the chat flow driven through Streamlit's AppTest."""
import importlib.util
import json
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

import ui_components as ui

APP = Path(importlib.util.find_spec("app").origin)
CHAT_FILE = APP.parent / ".chat_history.json"
METRICS = APP.parent / "agent_metrics.jsonl"


class PreserveFiles:
    """Never clobber a real chat history or telemetry file when tests run inside the project."""

    def setUp(self):
        self._saved = {p: (p.read_bytes() if p.exists() else None) for p in (CHAT_FILE, METRICS)}
        for p in self._saved:
            p.unlink(missing_ok=True)

    def tearDown(self):
        for p, data in self._saved.items():
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(data)


class Stepper(unittest.TestCase):
    def test_progress_and_iteration_reset(self):
        state = ui.new_stepper_state()
        ui.apply_progress(state, "baseline", "passed", "ok")
        ui.apply_progress(state, "iteration", "running", "1/3")
        ui.apply_progress(state, "coder", "passed")
        ui.apply_progress(state, "gate", "failed", "Removed public symbol")
        ui.apply_progress(state, "iteration", "running", "2/3")
        self.assertEqual(state["iteration"], "2/3")
        self.assertEqual(state["stages"]["baseline"]["state"], "passed")      # kept across iterations
        self.assertEqual(state["stages"]["gate"]["state"], "pending")         # reset for the new iteration
        ui.apply_progress(state, "made_up_stage", "passed")                   # ignored
        ui.apply_progress(state, "review", "bogus-state")
        self.assertEqual(state["stages"]["review"]["state"], "pending")

    def test_html_is_escaped(self):
        state = ui.new_stepper_state()
        ui.apply_progress(state, "review", "failed", "<script>alert(1)</script>")
        markup = ui.stepper_html(state)
        self.assertNotIn("<script>", markup)
        self.assertIn("&lt;script&gt;", markup)
        self.assertIn('class="jb-step failed"', markup)


class Diff(unittest.TestCase):
    def test_side_by_side_escapes_code(self):
        markup = ui.side_by_side_diff_html("a = 1\n", "a = '<b>2</b>'\n")
        self.assertIn('class="jb-diff"', markup)
        self.assertNotIn("<b>2</b>", markup)
        self.assertIn("&lt;b&gt;", markup)

    def test_no_changes(self):
        self.assertIn("No changes", ui.side_by_side_diff_html("x\n", "x\n"))


class Sources(unittest.TestCase):
    def test_cards_drop_unsafe_urls_and_escape(self):
        markup = ui.source_cards_html([
            {"title": "<i>Post</i>", "url": "https://www.kp.example/a?x=1&y=2"},
            {"title": "evil", "url": "javascript:alert(1)"},
            {"title": "file", "url": "file:///etc/passwd"},
        ])
        self.assertEqual(markup.count('class="jb-source"'), 1)
        self.assertNotIn("javascript:", markup)
        self.assertIn("&lt;i&gt;Post&lt;/i&gt;", markup)
        self.assertIn("kp.example", markup)
        self.assertIn('rel="noopener noreferrer"', markup)
        self.assertEqual(ui.source_cards_html([]), "")


class FileTree(unittest.TestCase):
    def test_statuses(self):
        orig = {"a.py": "1", "b.py": "1", "c.py": "1", "d.py": "1", "e.py": "1", "f.py": "1"}
        tree = dict(orig, **{"a.py": "2"})
        results = {"c.py": {"approved": True}, "d.py": {"approved": False}}
        got = {p: ui.file_status(p, {"a.py", "b.py"}, {"e.py": "audit"}, results, tree, orig) for p in orig}
        self.assertEqual(got, {"a.py": "optimized", "b.py": "reviewed", "c.py": "ready",
                               "d.py": "needs_review", "e.py": "audited", "f.py": "pending"})

    def test_tree_groups_by_folder_in_dependency_order(self):
        order = ["src/dto/User.java", "src/service/Svc.java", "app.py"]
        markup = ui.file_tree_html(order, {p: "pending" for p in order}, {"src/service/Svc.java": ["src/dto/User.java"]},
                                   active="app.py")
        self.assertLess(markup.index("src/dto"), markup.index("src/service"))
        self.assertIn("#2", markup)
        self.assertIn("← User.java", markup)
        self.assertIn("app.py ◀", markup)
        self.assertIn("(project root)", markup)
        self.assertIn("Pending", ui.status_legend_html(Counter({"pending": 3})))


class Telemetry(unittest.TestCase):
    def test_summary_separates_requests_tools_and_errors(self):
        entries = [
            {"timestamp": "2026-11-19T01:00:00+00:00", "source": "chat", "model": "m1", "latency_sec": 1.0, "total_tokens": 10},
            {"timestamp": "2026-11-19T01:01:00+00:00", "source": "optimizer", "model": "m2", "latency_sec": 3.0, "total_tokens": 30},
            {"source": "tool", "tool": "search_web", "ok": True},
            {"source": "tool", "tool": "read_page", "ok": False},
            {"source": "chat", "model": "m1", "error": "ServerError: 503"},
            {"model": "old", "latency_sec": 2.0, "total_tokens": 5},
        ]
        s = ui.summarize_metrics(entries)
        self.assertEqual((s["requests"], s["total_tokens"]), (3, 45))
        self.assertAlmostEqual(s["avg_latency"], 2.0)
        self.assertEqual(s["by_source"], {"chat": 1, "optimizer": 1, "unlabelled": 1})
        self.assertEqual((s["tool_calls"], s["tool_failures"]), (2, 1))
        self.assertEqual(s["errors_by_model"], {"m1": 1})
        self.assertEqual(len(s["token_series"]), 2)
        self.assertEqual(ui.summarize_metrics([])["avg_latency"], 0.0)


def scripted_reply(events_done_chat):
    def fake_stream_reply(chat, prompt, confirm_saves=True, tool_budget=6):
        yield {"type": "model", "model": "gemini-test"}
        yield {"type": "tool_call", "name": "search_web", "args": {"query": prompt}}
        yield {"type": "tool_result", "name": "search_web", "ok": True, "summary": "Title: Post",
               "sources": [{"title": "Post", "url": "https://kp.example/a"}]}
        yield {"type": "text", "delta": "Kathmandu "}
        yield {"type": "text", "delta": "is busy."}
        yield {"type": "pending_save", "filename": "kt.md", "content": "Note body"}
        yield {"type": "done", "chat": events_done_chat, "text": "Kathmandu is busy.", "model": "gemini-test",
               "sources": [{"title": "Post", "url": "https://kp.example/a"}],
               "pending_saves": [{"filename": "kt.md", "content": "Note body"}], "tools_used": 1}
    return fake_stream_reply


class ChatFlow(PreserveFiles, unittest.TestCase):
    def make_app(self):
        return AppTest.from_file(str(APP), default_timeout=60)

    def test_streamed_answer_tool_steps_sources_and_confirmed_save(self):
        done_chat = MagicMock()
        done_chat.get_history.return_value = []
        with patch("google.genai.Client"), \
             patch("agent.stream_reply", side_effect=scripted_reply(done_chat)), \
             patch("tools.save_research_note", return_value="Successfully saved note to agent_outputs/kt.md") as save:
            at = self.make_app()
            at.run()
            at.chat_input[0].set_value("kathmandu traffic").run()
            self.assertFalse(at.exception, at.exception)

            messages = at.session_state["messages"]
            self.assertEqual(messages[-1]["content"], "Kathmandu is busy.")
            self.assertEqual(messages[-1]["tools"], ["🔎 Web search: kathmandu traffic"])
            self.assertEqual(messages[-1]["sources"], [{"title": "Post", "url": "https://kp.example/a"}])
            save.assert_not_called()                                    # nothing saved before confirmation

            saved = json.loads(CHAT_FILE.read_text(encoding="utf-8"))   # persisted after the turn
            self.assertEqual(saved["messages"][-1]["content"], "Kathmandu is busy.")

            next(b for b in at.button if b.label == "Save note").click().run()
            self.assertFalse(at.exception, at.exception)
        save.assert_called_once_with("kt.md", "Note body")
        self.assertEqual(at.session_state["pending_saves"], [])
        self.assertTrue(at.session_state["messages"][-1]["content"].startswith("✅ Successfully"))

    def test_discard_does_not_save(self):
        done_chat = MagicMock(); done_chat.get_history.return_value = []
        with patch("google.genai.Client"), patch("agent.stream_reply", side_effect=scripted_reply(done_chat)), \
             patch("tools.save_research_note") as save:
            at = self.make_app(); at.run()
            at.chat_input[0].set_value("q").run()
            next(b for b in at.button if b.label == "Discard").click().run()
        save.assert_not_called()
        self.assertIn("Discarded", at.session_state["messages"][-1]["content"])

    def test_conversation_is_restored_on_start(self):
        CHAT_FILE.write_text(json.dumps({
            "version": 1,
            "messages": [{"role": "user", "content": "earlier question"}, {"role": "assistant", "content": "earlier answer"}],
            "history": [{"role": "user", "parts": [{"text": "earlier question"}]},
                        {"role": "model", "parts": [{"text": "earlier answer"}]}],
        }), encoding="utf-8")
        with patch("google.genai.Client"), patch("agent.create_agent_session") as cas:
            at = self.make_app(); at.run()
        self.assertFalse(at.exception, at.exception)
        self.assertEqual([m["content"] for m in at.session_state["messages"]], ["earlier question", "earlier answer"])
        history = cas.call_args.kwargs["history"]
        self.assertEqual([c.parts[0].text for c in history], ["earlier question", "earlier answer"])
        self.assertTrue(cas.call_args.kwargs["manual_tools"])

    def test_agent_failure_is_shown_not_crashing(self):
        def failing(*a, **k):
            raise RuntimeError("All models failed")
            yield  # pragma: no cover
        with patch("google.genai.Client"), patch("agent.stream_reply", side_effect=failing):
            at = self.make_app(); at.run()
            at.chat_input[0].set_value("q").run()
        self.assertFalse(at.exception, at.exception)
        self.assertTrue(any("All models failed" in e.value for e in at.error))

    def test_sidebar_shows_errors_and_tool_counts(self):
        METRICS.write_text("\n".join(json.dumps(e) for e in [
            {"source": "chat", "model": "m1", "latency_sec": 1.0, "total_tokens": 10},
            {"source": "tool", "tool": "search_web", "ok": False},
            {"source": "chat", "model": "m1", "error": "ServerError: 503"},
        ]) + "\n", encoding="utf-8")
        with patch("google.genai.Client"):
            at = self.make_app(); at.run()
        self.assertFalse(at.exception, at.exception)
        self.assertEqual({m.label: m.value for m in at.sidebar.metric}["Requests"], "1")
        self.assertTrue(any("tools 1 (1 failed)" in c.value for c in at.sidebar.caption))
        self.assertTrue(any("Errors by model (1)" in e.label for e in at.sidebar.expander))


if __name__ == "__main__":
    unittest.main()
