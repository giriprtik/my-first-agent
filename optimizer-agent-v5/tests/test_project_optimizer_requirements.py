"""New requirement tests for project_optimizer.py."""
import io
import unittest
import zipfile
from unittest.mock import patch

import project_optimizer as po


class BadUploads(unittest.TestCase):
    def test_non_zip_bytes_raise_value_error(self):
        with self.assertRaises(ValueError):
            po.parse_zip_in_memory(b"this is not a zip file")

    def test_corrupted_entry_raises_value_error(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("a.py", "print('hello world')\n")
        raw = bytearray(buf.getvalue())
        pos = raw.find(b"hello")
        raw[pos:pos + 5] = b"HELLO"          # payload no longer matches its CRC
        with self.assertRaises(ValueError):
            po.parse_zip_in_memory(bytes(raw))


class StrictConsistency(unittest.TestCase):
    TREE = {"main.py": "import helper\n", "helper.py": "def go():\n    pass\n"}

    def run_with(self, reply):
        with patch.object(po, "ask_agent", return_value=reply):
            return po.check_cross_file_consistency(self.TREE, {"main.py"}, "MAP")

    def test_string_false_is_not_consistent(self):
        self.assertFalse(self.run_with('{"consistent": "false", "issues": []}')["consistent"])

    def test_issues_override_consistent_flag(self):
        res = self.run_with('{"consistent": true, "issues": [{"symbol": "x"}]}')
        self.assertFalse(res["consistent"])
        self.assertEqual(len(res["issues"]), 1)

    def test_non_object_json(self):
        res = self.run_with("[]")
        self.assertFalse(res["consistent"])
        self.assertEqual(res["issues"][0]["symbol"], "ParseError")


class HonestReportLabels(unittest.TestCase):
    def test_completed_without_changes_is_not_called_optimized(self):
        data = po.package_overlay_zip({"a.py": b"x = 1\n", "b.py": b"y = 1\n"},
                                      {"a.py": "x = 1\n", "b.py": "y = 2\n"},
                                      {"a.py": "audit", "b.py": "audit"}, {"a.py", "b.py"})
        report = zipfile.ZipFile(io.BytesIO(data)).read("PROJECT_AUDIT_REPORT.md").decode()
        self.assertIn("## `a.py` [REVIEWED — NO CHANGES]", report)
        self.assertIn("## `b.py` [OPTIMIZED & SAVED]", report)


class JsImports(unittest.TestCase):
    def test_commonjs_dynamic_and_parent_relative_imports(self):
        code = ("const db = require('./lib/db');\n"
                "const cfg = require(\"../config\");\n"
                "const lazy = await import('./lazy.mjs');\n"
                "export { thing } from './things';\n"
                "import x from './util.js';\n"
                "const ext = require('express');\n")
        self.assertEqual(po.extract_imports("a.js", code), {"db", "config", "lazy", "things", "util"})


class NeighborContext(unittest.TestCase):
    def test_java_wildcard_imports_resolve_to_neighbors(self):
        tree = {"svc/Svc.java": "package a.svc;\nimport a.dto.*;\npublic class Svc {}",
                "dto/Foo.java": "package a.dto;\npublic record Foo(int x) {}",
                "other/Bar.java": "package a.other;\npublic class Bar {}"}
        ctx = po.build_neighbor_context("svc/Svc.java", tree, "MAP")
        self.assertIn("--- dto/Foo.java ---", ctx)
        self.assertNotIn("--- other/Bar.java ---", ctx)
        with patch.object(po, "ask_agent", return_value='{"consistent": true, "issues": []}'):
            res = po.check_cross_file_consistency(tree, {"svc/Svc.java"}, "MAP")
        self.assertIn(("svc/Svc.java", "dto/Foo.java"), res["checked_pairs"])

    def test_large_neighbor_still_exposes_its_full_public_interface(self):
        big = "def first():\n    pass\n" + ("# filler\n" * 2000) + "def defined_late(x, y=1):\n    pass\n"
        tree = {"main.py": "import big\n", "big.py": big}
        ctx = po.build_neighbor_context("main.py", tree, "MAP")
        self.assertIn("def defined_late(x, y)", ctx)
        self.assertLess(len(ctx), len(big))


if __name__ == "__main__":
    unittest.main()
