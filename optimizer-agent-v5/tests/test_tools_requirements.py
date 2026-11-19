"""New requirement tests for tools.py (expected to fail on the original)."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import tools


class ToolsRequirements(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "agent_outputs"   # does not exist yet
        self.p = patch.object(tools, "SAFE_DIR", self.dir)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_creates_sandbox_dir_on_demand(self):
        self.assertIn("Successfully", tools.save_research_note("a.md", "x"))
        self.assertTrue((self.dir / "a.md").exists())

    def test_bare_extension_name_still_gets_md_extension(self):
        msg = tools.save_research_note(".md", "x")
        self.assertIn("Successfully", msg)
        files = [p.name for p in self.dir.iterdir()]
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].endswith(".md"), files)
        self.assertNotEqual(files[0], ".md")

    def test_concurrent_writers_never_overwrite(self):
        self.dir.mkdir(parents=True)
        results = []
        barrier = threading.Barrier(8)

        def worker(i):
            barrier.wait()
            results.append(tools.save_research_note("race.md", f"writer-{i}"))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum("Successfully" in r for r in results), 1, results)
        self.assertEqual(sum("ACCESS DENIED" in r for r in results), 7, results)


class ToolsImportSideEffects(unittest.TestCase):
    def test_import_does_not_create_directories(self):
        import importlib, os
        with tempfile.TemporaryDirectory() as d:
            src = Path(tools.__file__).read_text(encoding="utf-8")
            (Path(d) / "tools_copy.py").write_text(src, encoding="utf-8")
            import sys
            sys.path.insert(0, d)
            try:
                importlib.import_module("tools_copy")
            finally:
                sys.path.remove(d)
                sys.modules.pop("tools_copy", None)
            self.assertFalse((Path(d) / "agent_outputs").exists(), os.listdir(d))


if __name__ == "__main__":
    unittest.main()
