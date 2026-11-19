import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import tools


class TestToolsSecurity(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.safe_path = Path(self.tmp_dir.name)
        self.patcher = patch.object(tools, "SAFE_DIR", self.safe_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp_dir.cleanup()

    def test_blocks_path_traversal_and_sanitizes_name(self):
        result = tools.save_research_note("../../evil!@#note", "Valid research content")
        self.assertIn("Successfully saved", result)
        saved_files = list(self.safe_path.glob("*.md"))
        self.assertEqual(len(saved_files), 1)
        self.assertFalse((self.safe_path.parent / "evil!@#note.md").exists())

    def test_blocks_overwrite_of_existing_file(self):
        tools.save_research_note("report.md", "First version")
        second_attempt = tools.save_research_note("report.md", "Overwritten version")
        self.assertIn("ACCESS DENIED", second_attempt)
        self.assertIn("First version", (self.safe_path / "report.md").read_text(encoding="utf-8"))

    def test_rejects_empty_or_oversized_content(self):
        with self.assertRaises(ValueError):
            tools.save_research_note("empty.md", "   ")
        with self.assertRaises(ValueError):
            tools.save_research_note("huge.md", "x" * 50_001)


if __name__ == "__main__":
    unittest.main()
