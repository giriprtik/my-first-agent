"""Baseline regression tests for project_optimizer.py (LLM mocked)."""
import io
import json
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import project_optimizer as po


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


class Parsing(unittest.TestCase):
    def test_supported_files(self):
        self.assertTrue(po.is_supported_code_file(Path("src/app.py")))
        self.assertTrue(po.is_supported_code_file(Path("Dockerfile")))
        self.assertFalse(po.is_supported_code_file(Path("venv/lib/x.py")))
        self.assertFalse(po.is_supported_code_file(Path(".hidden/x.py")))
        self.assertFalse(po.is_supported_code_file(Path("img.png")))
        self.assertFalse(po.is_supported_code_file(Path("package-lock.json")))

    def test_filters_and_zip_slip(self):
        data = make_zip({
            "proj/app.py": "import util\n",
            "proj/util.py": "def f():\n    pass\n",
            "proj/README.md": "# readme",
            "proj/venv/lib/site.py": "x",
            "__MACOSX/proj/._app.py": "junk",
            "proj/node_modules/a.js": "x",
            "../evil.py": "pwn",
            "/abs/evil.py": "pwn",
            "proj/bin.py": b"\xff\xfe\x00",
        })
        all_files, code_files = po.parse_zip_in_memory(data)
        self.assertEqual(sorted(all_files), ["proj/README.md", "proj/app.py", "proj/bin.py", "proj/util.py"])
        self.assertEqual(sorted(code_files), ["proj/app.py", "proj/util.py"])

    def test_lockfiles_are_kept_but_not_optimized(self):
        all_files, code_files = po.parse_zip_in_memory(make_zip({
            "p/package-lock.json": '{"lockfileVersion": 3}', "p/app.js": "x = 1\n", "p/.DS_Store": "junk"}))
        self.assertIn("p/package-lock.json", all_files)          # dependency scanners need it
        self.assertNotIn("p/package-lock.json", code_files)
        self.assertNotIn("p/.DS_Store", all_files)
        out = zipfile.ZipFile(io.BytesIO(po.package_overlay_zip(all_files, code_files, {}, set())))
        self.assertIn("p/package-lock.json", out.namelist())      # the downloaded project keeps its lockfile

    def test_binary_assets_have_their_own_budget(self):
        files = {"logo.png": b"\x89PNG" + b"0" * 50, "a.py": "x = 1\n"}
        with patch.object(po, "MAX_TOTAL_BYTES", 20):
            all_files, _ = po.parse_zip_in_memory(make_zip(files))
        self.assertIn("logo.png", all_files)
        self.assertNotIn("logo.png", po.scannable_files(all_files, {"a.py": "y = 2\n"}))
        self.assertEqual(po.scannable_files(all_files, {"a.py": "y = 2\n"})["a.py"], b"y = 2\n")
        with patch.object(po, "MAX_ASSET_BYTES", 20), self.assertRaises(ValueError):
            po.parse_zip_in_memory(make_zip(files))

    def test_limits(self):
        with patch.object(po, "MAX_ZIP_ENTRIES", 2):
            with self.assertRaises(ValueError):
                po.parse_zip_in_memory(make_zip({"a.py": "1", "b.py": "2", "c.py": "3"}))
        with patch.object(po, "MAX_TOTAL_BYTES", 10):
            with self.assertRaises(ValueError):
                po.parse_zip_in_memory(make_zip({"a.py": "x" * 20}))
        with patch.object(po, "MAX_FILE_BYTES", 5):
            all_files, code_files = po.parse_zip_in_memory(make_zip({"big.py": "x" * 6}))
        self.assertIn("big.py", all_files)
        self.assertNotIn("big.py", code_files)


class Graph(unittest.TestCase):
    def test_extract_imports(self):
        self.assertEqual(po.extract_imports("a.py", "import os.path\nfrom tools import x\nfrom .rel import y\n"), {"os", "tools", "rel"})
        self.assertEqual(po.extract_imports("a.js", "import x from './util.js';\nimport { a } from './lib/api';\n"), {"util", "api"})
        self.assertEqual(
            po.extract_imports("A.java", "import com.x.dto.UserDto;\nimport com.x.model.*;\nimport static com.x.Util.helper;\n"),
            {"UserDto", "com.x.model.*", "Util"})

    def test_order_and_deps(self):
        files = {
            "src/controller/UserController.java": "package a.controller;\nimport a.service.UserService;\npublic class UserController {}",
            "src/service/UserService.java": "package a.service;\nimport a.dto.*;\npublic class UserService {}",
            "src/dto/UserDto.java": "package a.dto;\npublic record UserDto(String name) {}",
            "main.py": "import helper\n",
            "helper.py": "def go():\n    pass\n",
        }
        repo_map, order, deps = po.build_repo_map_and_order(files)
        self.assertLess(order.index("src/dto/UserDto.java"), order.index("src/service/UserService.java"))
        self.assertLess(order.index("src/service/UserService.java"), order.index("src/controller/UserController.java"))
        self.assertLess(order.index("helper.py"), order.index("main.py"))
        self.assertEqual(deps["src/service/UserService.java"], ["src/dto/UserDto.java"])
        self.assertEqual(deps["main.py"], ["helper.py"])
        self.assertIn("- `helper.py`: def go()", repo_map)

    def test_neighbor_context(self):
        tree = {"main.py": "import helper\n", "helper.py": "def go():\n    pass\n", "other.py": "x = 1\n"}
        ctx = po.build_neighbor_context("main.py", tree, "MAP", "", ["use logging"])
        self.assertIn("PROJECT REPOSITORY MAP (PRESERVE PUBLIC SIGNATURES):\nMAP", ctx)
        self.assertIn("--- helper.py ---", ctx)
        self.assertIn("def go():", ctx)
        self.assertNotIn("--- other.py ---", ctx)
        self.assertIn("- use logging", ctx)
        self.assertIn("Apply concrete security", ctx)
        self.assertIn("USER GOALS:\nmy goals", po.build_neighbor_context("main.py", tree, "MAP", "my goals"))


class Overlay(unittest.TestCase):
    def test_overlay_and_report(self):
        original = {"a.py": b"x = 1\n", "b.py": b"y = 2\n", "README.md": b"keep me"}
        tree = {"a.py": "x = 10\n", "b.py": "y = 2\n"}
        data = po.package_overlay_zip(original, tree, {"a.py": "audit A", "b.py": "audit B"}, {"a.py"})
        zf = zipfile.ZipFile(io.BytesIO(data))
        self.assertEqual(zf.read("a.py"), b"x = 10\n")
        self.assertEqual(zf.read("b.py"), b"y = 2\n")
        self.assertEqual(zf.read("README.md"), b"keep me")
        report = zf.read("PROJECT_AUDIT_REPORT.md").decode()
        self.assertIn("## `a.py` [OPTIMIZED & SAVED]\n\naudit A", report)
        self.assertIn("## `b.py` [AUDITED ONLY]", report)

    def test_no_report_without_audits(self):
        data = po.package_overlay_zip({"a.py": b"1"}, {"a.py": "1"}, {}, set())
        self.assertNotIn("PROJECT_AUDIT_REPORT.md", zipfile.ZipFile(io.BytesIO(data)).namelist())


class Consistency(unittest.TestCase):
    TREE = {"main.py": "import helper\nhelper.go()\n", "helper.py": "def go():\n    pass\n"}

    def test_nothing_completed(self):
        self.assertEqual(po.check_cross_file_consistency(self.TREE, set(), "MAP"),
                         {"consistent": True, "issues": [], "checked_pairs": []})

    def test_parses_verdict(self):
        with patch.object(po, "ask_agent", return_value='{"consistent": true, "issues": []}') as ask:
            res = po.check_cross_file_consistency(self.TREE, {"main.py"}, "MAP")
        self.assertTrue(res["consistent"])
        self.assertEqual(res["checked_pairs"], [("main.py", "helper.py")])
        self.assertIn("--- IMPORTED TARGET: helper.py ---", ask.call_args.args[1])
        issue = {"caller_file": "main.py", "target_file": "helper.py", "symbol": "go2", "severity": "HIGH",
                 "description": "missing", "suggested_fix": "use go"}
        with patch.object(po, "ask_agent", return_value=json.dumps({"consistent": False, "issues": [issue]})):
            res = po.check_cross_file_consistency(self.TREE, {"main.py"}, "MAP")
        self.assertEqual((res["consistent"], res["issues"]), (False, [issue]))

    def test_unparseable_reply(self):
        with patch.object(po, "ask_agent", return_value="not json"):
            res = po.check_cross_file_consistency(self.TREE, {"main.py"}, "MAP")
        self.assertFalse(res["consistent"])
        self.assertEqual(res["issues"][0]["symbol"], "ParseError")


if __name__ == "__main__":
    unittest.main()
