"""Tests for the scanner layer. Fixtures are real SARIF output captured from Semgrep 1.x, gitleaks 8.30 and
OSV-Scanner 2.7 run on a small deliberately vulnerable project (paths rewritten to /work/proj)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import file_optimizer as fo
import scanners as sc
import ui_components as ui

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = "/work/proj"


def load(name):
    return json.loads((FIXTURES / f"{name}.sarif").read_text(encoding="utf-8"))


class SemgrepParsing(unittest.TestCase):
    def setUp(self):
        self.findings = sc.parse_sarif(load("semgrep"), "semgrep", ROOT)
        self.by_rule = {f.rule_id: f for f in self.findings}

    def test_all_results_parsed_with_clean_rule_ids(self):
        self.assertEqual(len(self.findings), 11)
        self.assertIn("python.lang.security.audit.subprocess-shell-true", self.by_rule)
        self.assertFalse(any("semgrep-rules" in f.rule_id for f in self.findings))

    def test_locations_cwe_and_severity(self):
        f = self.by_rule["java.lang.security.audit.formatted-sql-string"]
        self.assertEqual((f.file, f.line), ("src/main/java/app/Repo.java", 6))
        self.assertEqual(f.cwe, ("CWE-89",))
        self.assertEqual(f.severity, "high")              # rule defaultConfiguration.level = error
        self.assertEqual(f.category, "security")
        self.assertIn("executeQuery", f.snippet)
        self.assertTrue(f.help_url.startswith("https://"))

    def test_correctness_rules_are_quality_and_low(self):
        f = self.by_rule["python.lang.correctness.unchecked-subprocess-call"]
        self.assertEqual((f.category, f.severity), ("quality", "low"))


class GitleaksParsing(unittest.TestCase):
    def test_secret_is_high_and_redacted(self):
        [f] = sc.parse_sarif(load("gitleaks"), "gitleaks", ROOT)
        self.assertEqual((f.tool, f.rule_id, f.category, f.severity), ("gitleaks", "aws-access-token", "secret", "high"))
        self.assertEqual((f.file, f.line), ("src/web/page.js", 1))
        self.assertNotIn("AKIAQ3EGVJ7Z4MXD2PLA", f.snippet)      # never show or send the secret itself
        self.assertNotIn("AKIAQ3EGVJ7Z4MXD2PLA", json.dumps(f.to_dict()))
        self.assertIn("rotate", f.fix_hint)
        self.assertTrue(f.fingerprint)                           # gitleaks' own fingerprints are empty: we hash


class OsvParsing(unittest.TestCase):
    def setUp(self):
        self.findings = {f.rule_id: f for f in sc.parse_sarif(load("osv"), "osv-scanner", ROOT)}

    def test_absolute_file_uris_become_relative(self):
        self.assertEqual(self.findings["CVE-2020-8203"].file, "package-lock.json")
        self.assertEqual(self.findings["CVE-2020-14343"].file, "requirements.txt")

    def test_severity_from_cvss_and_fixed_version(self):
        yaml = self.findings["CVE-2020-14343"]
        self.assertEqual(yaml.severity, "critical")              # security-severity 9.8
        self.assertEqual(yaml.category, "dependency")
        self.assertEqual(yaml.fix_hint, "Upgrade pyyaml to 5.4 or later")
        self.assertEqual(yaml.help_url, "https://osv.dev/CVE-2020-14343")
        self.assertEqual(self.findings["CVE-2020-8203"].fix_hint, "Upgrade lodash to 4.17.19 or later")

    def test_manifest_line_is_located(self):
        files = {"requirements.txt": b"requests==2.19.0\ndjango==2.0\npyyaml==5.3\n"}
        located = {f.rule_id: f for f in sc._locate_dependency_lines(list(self.findings.values()), files)}
        self.assertEqual(located["CVE-2020-14343"].line, 3)
        self.assertEqual(located["CVE-2018-18074"].line, 1)
        self.assertEqual(located["CVE-2020-14343"].snippet, "pyyaml==5.3")
        self.assertEqual(located["CVE-2020-8203"].line, 0)       # file not provided: left as whole-file


class Helpers(unittest.TestCase):
    def test_score_and_level_mapping(self):
        cases = [("9.0", "critical"), ("7.5", "high"), ("4.0", "medium"), ("0.1", "low")]
        for score, expected in cases:
            self.assertEqual(sc.severity_for("x", {}, {"properties": {"security-severity": score}}), expected)
        self.assertEqual(sc.severity_for("x", {"level": "note"}, {}), "low")
        self.assertEqual(sc.severity_for("x", {}, {"defaultConfiguration": {"level": "error"}}), "high")
        self.assertEqual(sc.severity_for("x", {}, {"properties": {"security-severity": "n/a"}}), "medium")

    def test_normalize_paths(self):
        self.assertEqual(sc.normalize_path("file:///work/proj/a/b.py", ROOT), "a/b.py")
        self.assertEqual(sc.normalize_path("./a/b.py", ROOT), "a/b.py")
        self.assertEqual(sc.normalize_path("file:///C:/scan/proj/x%20y.js", "C:\\scan\\proj"), "x y.js")

    def test_write_tree_refuses_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            n = sc.write_tree({"ok/a.py": "x", "../evil.py": "x", "/abs.py": "x", "b.bin": b"\x00"}, Path(d))
            self.assertEqual(n, 2)
            self.assertFalse((Path(d).parent / "evil.py").exists())
            self.assertEqual((Path(d) / "b.bin").read_bytes(), b"\x00")

    def test_commands(self):
        with patch.dict(os.environ, {"SEMGREP_RULES": os.pathsep.join(["/r/python", "/r/java"])}):
            cmd = sc.build_command("semgrep", "semgrep", "out.sarif")
        self.assertIn("--metrics=off", cmd)
        self.assertEqual([cmd[i + 1] for i, a in enumerate(cmd) if a == "--config"], ["/r/python", "/r/java"])
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sc.semgrep_configs(), list(sc.DEFAULT_SEMGREP_CONFIGS))
            osv = sc.build_command("osv-scanner", "osv-scanner", "o")
            self.assertIn("--no-resolve", osv)
            self.assertNotIn("--offline-vulnerabilities", osv)
        with patch.dict(os.environ, {"OSV_OFFLINE": "1", "OSV_RESOLVE": "1"}):
            osv = sc.build_command("osv-scanner", "osv-scanner", "o")
        self.assertIn("--offline-vulnerabilities", osv)
        self.assertNotIn("--no-resolve", osv)
        self.assertIn("--redact", sc.build_command("gitleaks", "gitleaks", "o"))

    def test_sarif_export_round_trips(self):
        original = sc.parse_sarif(load("semgrep"), "semgrep", ROOT) + sc.parse_sarif(load("osv"), "osv-scanner", ROOT)
        exported = sc.to_sarif(original)
        self.assertEqual(exported["version"], "2.1.0")
        back = []
        for run in exported["runs"]:
            back += sc.parse_sarif({"runs": [run]}, run["tool"]["driver"]["name"], ROOT)
        self.assertEqual(sorted((f.rule_id, f.file, f.line, f.severity) for f in back),
                         sorted((f.rule_id, f.file, f.line, f.severity) for f in original))

    def test_test_code_is_flagged_and_sorted_last(self):
        for path in ("test/api/loginSpec.ts", "src/test/java/AppTest.java", "pkg/test_views.py", "web/app.spec.ts",
                     "tests/fixtures/keys.json", "challenge/tests.py"):
            self.assertTrue(sc.is_test_path(path), path)
        for path in ("routes/login.ts", "src/main/java/Testimony.java", "contest.py", "latest/app.js"):
            self.assertFalse(sc.is_test_path(path), path)
        real = sc.Finding("t", "r", "medium", "security", "", "", "app.py", 1)
        fake = sc.Finding("t", "r", "critical", "secret", "", "", "test/x.py", 1, in_test=True)
        self.assertEqual(sc.sort_findings([fake, real]), [real, fake])

    def test_goal_and_prompt_text(self):
        f = sc.parse_sarif(load("osv"), "osv-scanner", ROOT)[0]
        goal = sc.finding_goal(f)
        self.assertIn(f.rule_id, goal)
        self.assertIn("Upgrade", goal)
        many = sc.parse_sarif(load("semgrep"), "semgrep", ROOT)
        block = sc.findings_prompt_block(many, limit=3)
        self.assertTrue(block.startswith("1. [HIGH]"))
        self.assertIn("and 8 lower-priority findings", block)
        self.assertEqual(sc.sort_findings(many)[0].severity, "high")


class RunScanners(unittest.TestCase):
    def test_missing_tools_are_reported_not_fatal(self):
        with patch.object(sc, "find_tool", return_value=None):
            report = sc.run_scanners({"a.py": "print(1)\n"})
        status = {k: t["status"] for k, t in report.tools.items()}
        self.assertEqual({k for k, v in status.items() if v == "skipped"}, {"pmd", "eslint", "tsc"})   # no Java/JS here
        self.assertEqual({v for k, v in status.items() if k not in ("pmd", "eslint", "tsc")}, {"missing"})
        self.assertIn("pip install semgrep", report.tools["semgrep"]["detail"])
        self.assertEqual((report.findings, report.file_count), ([], 1))

    def test_results_are_merged_located_and_sorted(self):
        def fake_run(cmd, cwd, **kw):
            tool = Path(cmd[0]).name
            out = cmd[cmd.index({"semgrep": "--output", "gitleaks": "--report-path", "osv-scanner": "--output-file"}[tool]) + 1]
            fixture = {"semgrep": "semgrep", "gitleaks": "gitleaks", "osv-scanner": "osv"}[tool]
            text = (FIXTURES / f"{fixture}.sarif").read_text(encoding="utf-8").replace("/work/proj", str(cwd))
            Path(out).write_text(text, encoding="utf-8")
            class P: returncode = 1 if tool == "osv-scanner" else 0; stdout = ""; stderr = ""
            return P()
        files = {"requirements.txt": "requests==2.19.0\ndjango==2.0\npyyaml==5.3\n", "vuln.py": "x = 1\n"}
        with patch.object(sc, "find_tool", side_effect=lambda n: f"/fake/{n}"), patch.object(sc.subprocess, "run", side_effect=fake_run):
            report = sc.run_scanners(files, scanners=["semgrep", "gitleaks", "osv-scanner"])
        self.assertEqual({k: v["status"] for k, v in report.tools.items()},
                         dict.fromkeys(["semgrep", "gitleaks", "osv-scanner"], "ok"))
        self.assertEqual(len(report.findings), 15)
        self.assertEqual(report.findings[0].severity, "critical")
        self.assertEqual(next(f for f in report.findings if f.rule_id == "CVE-2020-14343").line, 3)
        self.assertEqual(report.counts_by_file()["requirements.txt"], 2)
        self.assertEqual(len(report.for_file("vuln.py")), 6)

    def test_failure_messages_are_actionable(self):
        osv_log = "End status: 115 dirs visited\nError during extraction: request failed: Post \"https://api.osv.dev/v1/querybatch\""
        self.assertIn("OSV_OFFLINE=1", sc.explain_failure("osv-scanner", osv_log, 127))
        self.assertIn("SEMGREP_RULES", sc.explain_failure("semgrep", "Failed to download config from https://semgrep.dev/c/p/python", 2))
        self.assertEqual(sc.explain_failure("gitleaks", "scanning...\nfatal: bad flag\ndone", 1), "fatal: bad flag")
        self.assertEqual(sc.explain_failure("gitleaks", "", 3), "exit code 3")

    def test_tool_crash_and_timeout(self):
        class Crash: returncode = 2; stdout = ""; stderr = "fatal: bad config\n"
        with patch.object(sc, "find_tool", return_value="/fake/semgrep"), patch.object(sc.subprocess, "run", return_value=Crash()):
            report = sc.run_scanners({"a.py": "x"}, scanners=["semgrep"])
        self.assertEqual(report.tools["semgrep"]["status"], "error")
        self.assertIn("bad config", report.tools["semgrep"]["detail"])
        with patch.object(sc, "find_tool", return_value="/fake/gitleaks"), \
             patch.object(sc.subprocess, "run", side_effect=sc.subprocess.TimeoutExpired("g", 5)):
            report = sc.run_scanners({"a.py": "x"}, scanners=["gitleaks"], timeout=5)
        self.assertEqual(report.tools["gitleaks"]["status"], "timeout")


@unittest.skipUnless(sc.find_tool("gitleaks"), "gitleaks not installed")
class GitleaksIntegration(unittest.TestCase):
    def test_real_gitleaks_finds_and_redacts_a_key(self):
        key = "AKIA" + "Q3EGVJ7Z4MXD2PLA"
        report = sc.run_scanners({"cfg/settings.js": f'const k = "{key}";\n'}, scanners=["gitleaks"], timeout=60)
        self.assertEqual(report.tools["gitleaks"]["status"], "ok")
        [f] = report.findings
        self.assertEqual((f.file, f.line, f.category), ("cfg/settings.js", 1, "secret"))
        self.assertNotIn(key, f.snippet + f.message)


@unittest.skipUnless(sc.find_tool("semgrep") and os.environ.get("SEMGREP_RULES"), "semgrep or local SEMGREP_RULES not available")
class SemgrepIntegration(unittest.TestCase):
    def test_real_semgrep_flags_shell_injection(self):
        code = "import subprocess\ndef run(name):\n    subprocess.call('ls ' + name, shell=True)\n"
        report = sc.run_scanners({"app/run.py": code}, scanners=["semgrep"], timeout=300)
        self.assertEqual(report.tools["semgrep"]["status"], "ok", report.tools)
        self.assertTrue(any("CWE-78" in f.cwe and f.line == 3 for f in report.findings))


class AuditorTriage(unittest.TestCase):
    def test_findings_switch_the_auditor_to_triage(self):
        findings = sc.parse_sarif(load("semgrep"), "semgrep", ROOT)
        vuln = [f for f in findings if f.file == "vuln.py"]
        with patch.object(fo, "ask_agent", return_value="report") as ask:
            fo.audit_code("vuln.py", "import pickle\n", findings=vuln)
        role, prompt = ask.call_args.args[:2]
        self.assertIn("Scanner Findings Triage", role)
        self.assertIn("SCANNER FINDINGS (6)", prompt)
        self.assertIn("avoid-pickle", prompt)

    def test_no_findings_keeps_the_plain_audit(self):
        with patch.object(fo, "ask_agent", return_value="report") as ask:
            fo.audit_code("a.py", "x = 1\n")
            fo.audit_code("a.py", "x = 1\n", findings=[])
        for call in ask.call_args_list:
            self.assertIs(call.args[0], fo.AUDITOR_PROMPT)
            self.assertNotIn("SCANNER FINDINGS", call.args[1])


class ScanUi(unittest.TestCase):
    def test_status_cards_escape_and_show_install_hint(self):
        markup = ui.scanner_status_html({
            "semgrep": {"status": "ok", "count": 3, "seconds": 1.5},
            "gitleaks": {"status": "missing", "detail": "brew install gitleaks"},
            "osv-scanner": {"status": "error", "detail": "<b>boom</b>"},
        }, {"semgrep": "Semgrep"})
        self.assertIn("3 finding(s) in 1.5s", markup)
        self.assertIn("Not installed: <b>gitleaks</b> <code>brew install gitleaks</code>", markup)
        self.assertIn("&lt;b&gt;boom", markup)
        self.assertEqual(markup.count('class="jb-scanner '), 2)                 # missing tools get no card
        skipped = ui.scanner_status_html({"pmd": {"status": "skipped", "detail": "No Java files"}}, {"pmd": "PMD"})
        self.assertIn("Not needed for this code: PMD (No Java files)", skipped)

    def test_tree_shows_counts_with_worst_severity(self):
        findings = sc.parse_sarif(load("semgrep"), "semgrep", ROOT)
        counts = ui.finding_counts_by_file(findings)
        self.assertEqual(counts["vuln.py"], (6, "high"))
        markup = ui.file_tree_html(["vuln.py", "other.py"], {}, {}, finding_counts=counts)
        self.assertIn("🛡 6", markup)
        self.assertEqual(markup.count("jb-findcount"), 1)
        self.assertIn("High 6", ui.severity_summary_html({"high": 6}))
        self.assertIn("No findings", ui.severity_summary_html({}))


if __name__ == "__main__":
    unittest.main()


class StudioScanFlow(unittest.TestCase):
    """Single-File Studio: scan → findings table → 'add to goals' → audit receives the findings."""

    def test_scan_fix_and_triaged_audit(self):
        import importlib.util
        from streamlit.testing.v1 import AppTest
        app = Path(importlib.util.find_spec("app").origin)
        target = "automate.py"
        finding = sc.Finding(tool="semgrep", rule_id="python.lang.security.audit.subprocess-shell-true",
                             severity="high", category="security", title="Shell true", message="shell=True is dangerous",
                             file=target, line=4, cwe=("CWE-78",), fix_hint="Pass a list and drop shell=True")
        report = sc.ScanReport(findings=[finding], file_count=1,
                               tools={"semgrep": {"status": "ok", "count": 1, "seconds": 0.4},
                                      "gitleaks": {"status": "missing", "detail": "brew install gitleaks"}})
        with patch("google.genai.Client"), patch("scanners.run_scanners", return_value=report) as run, \
             patch("file_optimizer.ask_agent", return_value="### 0. Scanner Findings Triage\nok") as ask:
            at = AppTest.from_file(str(app), default_timeout=60)
            at.run()
            at.radio[0].set_value("Select a file from project folder").run()
            box = next(s for s in at.selectbox if s.label == "Target project file:")
            box.set_value(target).run()
            next(b for b in at.button if b.label.startswith("🛡️ Step 0")).click().run()
            self.assertFalse(at.exception, at.exception)
            run.assert_called_once()
            self.assertEqual(list(run.call_args.args[0]), [target])
            self.assertEqual(len(at.dataframe), 1)

            next(b for b in at.button if b.label == "🛠️ Add to optimization goals").click().run()
            self.assertIn("subprocess-shell-true", at.session_state["single_goals"])
            self.assertIn("drop shell=True", at.session_state["single_goals"])

            next(b for b in at.button if b.label.startswith("🔍 Step 1: Audit + triage")).click().run()
            self.assertFalse(at.exception, at.exception)
        self.assertIn("SCANNER FINDINGS (1)", ask.call_args.args[1])
        self.assertEqual(at.session_state["audit_report"], "### 0. Scanner Findings Triage\nok")
