"""Baseline regression tests for tools.py: current behaviour that must survive refactoring."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tools


class ToolsBaseline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.p = patch.object(tools, "SAFE_DIR", self.dir)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_success_message_and_file(self):
        msg = tools.save_research_note("report.md", "hello")
        self.assertEqual(msg, "Successfully saved note to agent_outputs/report.md")
        text = (self.dir / "report.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("Saved at: "))
        self.assertTrue(text.endswith("\n\nhello"))

    def test_existing_header_not_duplicated(self):
        tools.save_research_note("h.md", "Saved at: yesterday\n\nbody")
        self.assertEqual((self.dir / "h.md").read_text(encoding="utf-8"), "Saved at: yesterday\n\nbody")

    def test_extension_forced_and_names_sanitized(self):
        cases = {
            "notes.txt": "notes_txt.md",
            "../../evil!@#note": "evil_note.md",
            "a.b.md": "a_b.md",
            "my  file???.md": "my_file_.md",
            "  spaced.md  ": "spaced.md",
            "dir/sub/x.md": "x.md",
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                msg = tools.save_research_note(given, "c")
                self.assertEqual(msg, f"Successfully saved note to agent_outputs/{expected}")
                self.assertTrue((self.dir / expected).exists())

    def test_overwrite_denied_and_original_kept(self):
        tools.save_research_note("r.md", "first")
        msg = tools.save_research_note("r.md", "second")
        self.assertEqual(msg, "ACCESS DENIED: 'r.md' already exists. Choose a different filename.")
        self.assertIn("first", (self.dir / "r.md").read_text(encoding="utf-8"))

    def test_validation_errors(self):
        for name, content in [("", "x"), ("   ", "x"), ("a.md", ""), ("a.md", "  \n"), ("a.md", "x" * 50_001)]:
            with self.subTest(name=name, n=len(content)):
                with self.assertRaises(ValueError):
                    tools.save_research_note(name, content)
        self.assertIn("Successfully", tools.save_research_note("max.md", "x" * 50_000))


if __name__ == "__main__":
    unittest.main()
