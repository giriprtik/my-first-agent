"""Phase 3: per-language tools (Bandit, Ruff, mypy, PMD, ESLint, tsc). Fixtures are real output from Bandit 1.9,
Ruff 0.17, mypy 2.4, PMD 7.29, ESLint 9.39 and TypeScript 6.0 on a small mixed project (paths -> /work/proj)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scanners as sc

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = "/work/proj"


def text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def by_rule(findings):
    return {f.rule_id: f for f in findings}


class Bandit(unittest.TestCase):
    def setUp(self):
        self.f = by_rule(sc.parse_sarif(json.loads(text("bandit.sarif")), "bandit", ROOT))

    def test_severity_cwe_and_category(self):
        self.assertEqual(self.f["B602"].severity, "high")             # shell=True
        self.assertEqual(self.f["B602"].cwe, ("CWE-78",))             # from 'external/cwe/cwe-78'
        self.assertEqual(self.f["B403"].severity, "low")              # 'import pickle' alone
        self.assertEqual(self.f["B506"].severity, "medium")           # yaml.load: Semgrep missed this one
        self.assertTrue(all(f.category == "security" and f.file == "vuln.py" for f in self.f.values()))


class Ruff(unittest.TestCase):
    def test_rule_families_are_ranked(self):
        f = by_rule(sc.parse_sarif(json.loads(text("ruff.sarif")), "ruff", ROOT))
        self.assertEqual((f["F821"].severity, f["F821"].file), ("medium", "typed.py"))   # undefined name: a bug
        self.assertEqual(f["E401"].severity, "low")
        self.assertTrue(all(x.category == "quality" for x in f.values()))
        self.assertIn("docs.astral.sh/ruff/rules/undefined-name", f["F821"].help_url)
        self.assertEqual(sc.ruff_severity("B006"), "medium")
        self.assertEqual(sc.ruff_severity("UP031"), "low")

    def test_project_ruff_config_is_respected(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("--select", sc.build_command("ruff", "ruff", "o", Path(d)))
            Path(d, "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
            self.assertNotIn("--select", sc.build_command("ruff", "ruff", "o", Path(d)))


class Mypy(unittest.TestCase):
    def test_json_lines(self):
        f = by_rule(sc.parse_mypy_json(text("mypy.jsonl"), ROOT))
        self.assertEqual(set(f), {"union-attr", "name-defined", "arg-type"})
        self.assertEqual({x.severity for x in f.values()}, {"medium"})
        self.assertEqual(f["arg-type"].line, 12)
        self.assertEqual(sc.parse_mypy_json('{"severity": "note", "message": "x"}\nnot json\n', ROOT), [])
        info = sc.parse_mypy_json('{"file": "a.py", "line": 1, "message": "m", "code": "var-annotated", '
                                  '"severity": "error", "hint": null}', ROOT)
        self.assertEqual(info[0].severity, "info")


class Pmd(unittest.TestCase):
    def test_sarif(self):
        [f] = sc.parse_sarif(json.loads(text("pmd.sarif")), "pmd", ROOT)
        self.assertEqual((f.rule_id, f.severity, f.category), ("CloseResource", "medium", "quality"))
        self.assertEqual(f.file, "src/main/java/app/Repo.java")


class Eslint(unittest.TestCase):
    def setUp(self):
        self.findings = sc.parse_eslint_json(text("eslint.json"), ROOT)

    def test_rules_are_classified(self):
        f = {(x.file, x.rule_id): x for x in self.findings}
        hooks = f[("src/App.tsx", "react-hooks/rules-of-hooks")]
        self.assertEqual((hooks.severity, hooks.category, hooks.line), ("high", "quality", 5))
        xss = f[("src/App.tsx", "no-unsanitized/property")]
        self.assertEqual((xss.severity, xss.category, xss.cwe), ("high", "security", ("CWE-79",)))
        self.assertEqual(xss.snippet, "el.innerHTML = html;")             # from ESLint's 'source'
        self.assertEqual(f[("src/util.js", "security/detect-child-process")].cwe, ("CWE-78",))
        self.assertEqual(f[("src/App.tsx", "jsx-a11y/alt-text")].category, "quality")
        self.assertEqual(f[("src/App.tsx", "@typescript-eslint/no-explicit-any")].severity, "info")   # style
        self.assertEqual(f[("src/App.tsx", "@typescript-eslint/no-unused-vars")].severity, "low")
        self.assertIn("typescript-eslint.io/rules/no-explicit-any", f[("src/App.tsx", "@typescript-eslint/no-explicit-any")].help_url)

    def test_parse_errors_are_not_findings(self):
        data = json.dumps([{"filePath": "/work/proj/a.js", "messages": [
            {"ruleId": None, "fatal": True, "severity": 2, "message": "Parsing error", "line": 1}]}])
        self.assertEqual(sc.parse_eslint_json(data, ROOT), [])


class Tsc(unittest.TestCase):
    def test_environment_noise_is_dropped(self):
        trusted = "\n".join(l for l in text("tsc.txt").splitlines() if "missing-lib" not in l)
        f = sc.parse_tsc_output(trusted, ROOT)
        self.assertEqual([(x.file, x.line, x.rule_id) for x in f],
                         [("src/math.ts", 2, "TS2322"), ("src/math.ts", 3, "TS2345")])
        self.assertEqual({x.severity for x in f}, {"medium"})

    def test_files_with_uninstalled_imports_are_not_trusted(self):
        stats = {}
        self.assertEqual(sc.parse_tsc_output(text("tsc.txt"), ROOT, stats), [])   # math.ts imports 'missing-lib'
        self.assertEqual(stats["untrusted_files"], 2)

    def test_unloadable_tsconfig_is_an_error_not_a_clean_pass(self):
        class P:
            returncode = 1
            stdout = "tsconfig.json(2,14): error TS6053: File '@tsconfig/node20/tsconfig.json' not found.\n"
            stderr = ""
        with patch.object(sc, "find_tool", return_value="/fake/tsc"), patch.object(sc.subprocess, "run", return_value=P()):
            rep = sc.run_scanners({"tsconfig.json": '{"extends": "@tsconfig/node20"}', "a.ts": "x"}, scanners=["tsc"])
        self.assertEqual(rep.tools["tsc"]["status"], "error")
        self.assertIn("tsconfig could not be loaded", rep.tools["tsc"]["detail"])

    def test_runs_once_per_tsconfig(self):
        calls = []

        class P:
            returncode = 0; stdout = ""; stderr = ""
        def fake(cmd, cwd, **kw):
            calls.append(cmd[-1]); return P()
        files = {"tsconfig.json": "{}", "frontend/tsconfig.json": "{}", "node_modules/x/tsconfig.json": "{}", "a.ts": ""}
        with patch.object(sc, "find_tool", return_value="/fake/tsc"), patch.object(sc.subprocess, "run", side_effect=fake):
            sc.run_scanners(files, scanners=["tsc"])
        self.assertEqual(calls, ["tsconfig.json", "frontend/tsconfig.json"])


class MypySyntaxRetry(unittest.TestCase):
    def test_unparsable_file_is_reported_and_the_rest_still_checked(self):
        syntax = json.dumps({"file": "old/py2.py", "line": 3, "message": "invalid syntax", "code": "syntax",
                             "severity": "error", "hint": None})
        real = json.dumps({"file": "app.py", "line": 7, "message": "Name 'x' is not defined", "code": "name-defined",
                           "severity": "error", "hint": None})
        outputs = iter([(2, syntax), (1, real)])
        cmds = []

        def fake(cmd, cwd, **kw):
            code, out = next(outputs)
            cmds.append(cmd)
            class P: pass
            p = P(); p.returncode, p.stdout, p.stderr = code, out, ""
            return p
        with patch.object(sc, "find_tool", return_value="/fake/mypy"), patch.object(sc.subprocess, "run", side_effect=fake):
            rep = sc.run_scanners({"app.py": "x", "old/py2.py": "print 'x'"}, scanners=["mypy"])
        self.assertEqual(rep.tools["mypy"]["status"], "ok")
        self.assertIn("1 file(s) could not be parsed", rep.tools["mypy"]["note"])
        self.assertIn("^old/py2\\.py$", cmds[1])
        self.assertEqual(sorted(f.rule_id for f in rep.findings), ["name-defined", "syntax"])


class Vendored(unittest.TestCase):
    def test_vendored_paths(self):
        for p in ("frontend/src/assets/private/three.js", "static/vendor/jquery.js", "web/app.min.js", "third_party/x.py"):
            self.assertTrue(sc.is_vendored_path(p), p)
            self.assertTrue(sc.is_test_path(p), p)
        for p in ("frontend/src/app/app.component.ts", "lib/insecurity.ts", "assets/styles.scss", "routes/login.ts"):
            self.assertFalse(sc.is_vendored_path(p), p)

    def test_eslint_style_findings_in_vendored_files_are_dropped_but_security_kept(self):
        data = json.dumps([{"filePath": "/work/proj/src/assets/lib.js", "messages": [
            {"ruleId": "no-var", "severity": 1, "message": "Unexpected var", "line": 1},
            {"ruleId": "no-eval", "severity": 2, "message": "eval can be harmful.", "line": 2}]}])
        [f] = sc.parse_eslint_json(data, ROOT)
        self.assertEqual((f.rule_id, f.in_test), ("no-eval", True))


class Gating(unittest.TestCase):
    def test_tools_only_run_for_their_languages(self):
        with patch.object(sc, "find_tool", return_value="/fake/x"), \
             patch.object(sc, "_run_one", return_value=({"status": "ok", "detail": "", "seconds": 0, "count": 0}, [])) as run:
            rep = sc.run_scanners({"web/a.ts": "let x = 1"}, scanners=["bandit", "pmd", "eslint", "tsc"])
        self.assertEqual(rep.tools["bandit"]["detail"], "No Python files")
        self.assertEqual(rep.tools["pmd"]["detail"], "No Java files")
        self.assertEqual(rep.tools["tsc"]["detail"], "No tsconfig.json")
        self.assertEqual([c.args[0] for c in run.call_args_list], ["eslint"])

    def test_eslint_comes_only_from_the_tools_folder(self):
        with tempfile.TemporaryDirectory() as d, patch.object(sc, "ESLINT_DIR", Path(d)), \
             patch.object(sc.shutil, "which", side_effect=lambda n, path=None: None if path else f"/usr/bin/{n}"):
            self.assertIsNone(sc.find_tool("eslint"))
            self.assertIsNone(sc.find_tool("eslint"))                 # a global eslint lacks our plugins
            self.assertEqual(sc.find_tool("tsc"), "/usr/bin/tsc")      # any tsc is fine
        with patch.object(sc.shutil, "which", side_effect=lambda n, path=None: None if n == "node" else "/x"):
            self.assertIsNone(sc.find_tool("eslint"))                 # no Node.js: reported as not installed


def F(tool, rule, cwe, line=6, severity="high", category="security"):
    return sc.Finding(tool=tool, rule_id=rule, severity=severity, category=category, title=rule, message=rule,
                      file="vuln.py", line=line, cwe=cwe)


class CrossToolMerge(unittest.TestCase):
    def test_same_line_same_weakness_is_one_finding(self):
        merged = sc.merge_cross_tool([F("bandit", "B602", ("CWE-78",)),
                                      F("semgrep", "subprocess-shell-true", ("CWE-78",))])
        self.assertEqual([(f.tool, f.also) for f in merged], [("semgrep", ("bandit",))])

    def test_more_severe_finding_wins(self):
        merged = sc.merge_cross_tool([F("semgrep", "x", ("CWE-89",), severity="medium"),
                                      F("bandit", "B608", ("CWE-89",), severity="high")])
        self.assertEqual([(f.tool, f.also) for f in merged], [("bandit", ("semgrep",))])

    def test_different_weakness_line_or_category_is_kept(self):
        fs = [F("semgrep", "a", ("CWE-78",)), F("bandit", "b", ("CWE-89",)), F("bandit", "c", ("CWE-78",), line=7),
              F("ruff", "F821", (), category="quality"), F("mypy", "name-defined", (), category="quality")]
        self.assertEqual(len(sc.merge_cross_tool(fs)), 5)

    def test_recheck_reruns_the_merged_tools_too(self):
        before = [sc.merge_cross_tool([F("bandit", "B602", ("CWE-78",)), F("semgrep", "s", ("CWE-78",))])[0],
                  F("tsc", "TS2322", (), category="quality")]
        rep = sc.ScanReport(tools={"semgrep": {"status": "ok"}, "bandit": {"status": "ok"}})
        with patch.object(sc, "run_scanners", return_value=rep) as run:
            sc.recheck_file("vuln.py", "x", before)
        self.assertEqual(run.call_args.kwargs["scanners"], ["bandit", "semgrep"])   # tsc needs the whole project


@unittest.skipUnless(all(sc.find_tool(t) for t in ("ruff", "bandit", "mypy")), "ruff/bandit/mypy not installed")
class RealPythonTools(unittest.TestCase):
    def test_real_run(self):
        code = ("import subprocess, yaml\n\ndef run(name: str) -> int:\n    subprocess.call('ls ' + name, shell=True)\n"
                "    return yaml.load(name) + missing\n")
        rep = sc.run_scanners({"pkg/tool.py": code}, scanners=["ruff", "bandit", "mypy"], timeout=120)
        self.assertEqual({k: v["status"] for k, v in rep.tools.items()}, dict.fromkeys(["ruff", "bandit", "mypy"], "ok"), rep.tools)
        got = {(f.tool, f.rule_id) for f in rep.findings}
        self.assertIn(("bandit", "B602"), got)
        self.assertIn(("bandit", "B506"), got)
        self.assertIn(("ruff", "F821"), got)
        self.assertIn(("mypy", "name-defined"), got)
        self.assertTrue(all(f.file == "pkg/tool.py" for f in rep.findings))


@unittest.skipUnless(sc.find_tool("eslint"), "tools/eslint not installed (cd tools/eslint && npm ci)")
class RealEslint(unittest.TestCase):
    def test_real_run(self):
        code = "export function show(el, html) {\n  el.innerHTML = html;\n  return eval(html);\n}\n"
        rep = sc.run_scanners({"web/show.js": code}, scanners=["eslint"], timeout=120)
        self.assertEqual(rep.tools["eslint"]["status"], "ok", rep.tools)
        rules = {f.rule_id for f in rep.findings}
        self.assertTrue({"no-unsanitized/property", "no-eval"} <= rules, rules)


if __name__ == "__main__":
    unittest.main()
