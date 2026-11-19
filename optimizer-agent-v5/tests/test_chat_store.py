"""Tests for chat persistence."""
import tempfile
import unittest
from pathlib import Path

from google.genai import types

import chat_store


class ChatStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "chat.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_including_tool_calls(self):
        messages = [{"role": "user", "content": "news?"},
                    {"role": "assistant", "content": "Here.", "sources": [{"title": "T", "url": "https://u"}]}]
        history = [
            types.Content(role="user", parts=[types.Part(text="news?")]),
            types.Content(role="model", parts=[types.Part(
                function_call=types.FunctionCall(name="search_web", args={"query": "n"}), thought_signature=b"\x00\xffsig")]),
            types.Content(role="user", parts=[types.Part.from_function_response(name="search_web", response={"result": "r"})]),
            types.Content(role="model", parts=[types.Part(text="Here.")]),
        ]
        chat_store.save_conversation(self.path, messages, history)
        loaded_messages, loaded_history = chat_store.load_conversation(self.path)
        self.assertEqual(loaded_messages, messages)
        self.assertEqual(loaded_history, history)
        self.assertEqual(loaded_history[1].parts[0].thought_signature, b"\x00\xffsig")
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["chat.json"])  # no temp files left

    def test_bad_files_give_an_empty_chat(self):
        self.assertEqual(chat_store.load_conversation(self.path), ([], []))
        for content in ["{not json", "[]", '{"version": 99, "messages": [], "history": []}',
                        '{"version": 1, "messages": [], "history": [{"role": 5, "parts": "x"}]}']:
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                self.assertEqual(chat_store.load_conversation(self.path), ([], []))

    def test_malformed_messages_are_skipped(self):
        chat_store.save_conversation(self.path, [{"role": "user", "content": "ok"}, {"oops": 1}, "str"], [])
        self.assertEqual(chat_store.load_conversation(self.path)[0], [{"role": "user", "content": "ok"}])

    def test_clear(self):
        chat_store.save_conversation(self.path, [], [])
        chat_store.clear_conversation(self.path)
        chat_store.clear_conversation(self.path)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
