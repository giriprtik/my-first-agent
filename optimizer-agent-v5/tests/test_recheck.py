"""Phase 2: the scanner re-check stage — matching findings across versions and gating the optimizer on it."""
import os
import unittest
from unittest.mock import patch

import file_optimizer as fo
import scanners as sc
import ui_components as ui
from test_optimizer_pipeline import FakeAgent, ORIGINAL, REQ_REAL


def F(rule, line, snippet="", severity="high", category="security", tool="semgrep", in_test=False):
    return sc.Finding(tool=tool, rule_id=rule, severity=severity, category=category, title=rule,
                      message=f"{rule} problem", file="calc.py", line=line, snippet=snippet, in_test=in_test)


SHELL = F("subprocess-shell-true", 6, "subprocess.call('ls ' + name, shell=True)")
PICKLE = F("avoid-pickle", 9, "pickle.loads(data)", severity="medium")
SECRET = F("aws-access-token", 2, "AKIA••••", tool="gitleaks", category="secret")


class CompareFindings(unittest.TestCase):
    def test_fixed_finding_is_resolved(self):
        r = sc.compare_findings([SHELL, PICKLE], [PICKLE])
        self.assertEqual(r["resolved"], [SHELL])
        self.assertEqual(r["still_present"], [PICKLE])
        self.assertTrue(r["passed"])                       # pickle is medium: not must-fix by default

    def test_moved_but_unchanged_code_is_still_present(self):
        moved = F("subprocess-shell-true", 30, "subprocess.call('ls ' + name, shell=True)")
        r = sc.compare_findings([SHELL], [moved])
        self.assertEqual(r["remaining"], [SHELL])
        self.assertFalse(r["passed"])

    def test_rewritten_line_still_flagged_counts_as_present_not_new(self):
        rewritten = F("subprocess-shell-true", 7, "subprocess.run(f'ls {name}', shell=True)")
        r = sc.compare_findings([SHELL], [rewritten])
        self.assertEqual((r["remaining"], r["introduced"]), ([SHELL], []))

    def test_new_findings_block_only_at_medium_and_above(self):
        r = sc.compare_findings([SHELL], [F("eval-detected", 3, "eval(x)", severity="medium")])
        self.assertEqual([f.rule_id for f in r["blocking_introduced"]], ["eval-detected"])
        self.assertFalse(r["passed"])
        r = sc.compare_findings([SHELL], [F("useless-assignment", 3, "x = x", severity="low", category="quality")])
        self.assertEqual(len(r["introduced"]), 1)
        self.assertTrue(r["passed"])

    def test_must_fix_override(self):
        self.assertTrue(sc.compare_findings([SHELL, PICKLE], [SHELL], must_fix=[])["passed"])   # user waived it
        self.assertFalse(sc.compare_findings([SHELL, PICKLE], [SHELL, PICKLE], must_fix=[PICKLE])["passed"])

    def test_default_must_fix_skips_tests_quality_and_medium(self):
        test_secret = F("jwt", 1, "eyJ…", tool="gitleaks", category="secret", in_test=True)
        quality = F("unchecked-call", 1, "x", category="quality")
        self.assertEqual(sc.default_must_fix([SHELL, PICKLE, SECRET, test_secret, quality]), [SHELL, SECRET])

    def test_suppression_comments_are_detected(self):
        self.assertEqual(sc.added_suppressions("x = 1\n", "x = 1  # nosemgrep\ny = 2  # nosec\n"), 2)
        self.assertEqual(sc.added_suppressions("a()  # nosec\n", "a()  # nosec\n"), 0)
        self.assertEqual(sc.added_suppressions("", 'k = "AKIA..."  // gitleaks:allow'), 1)


class RecheckFile(unittest.TestCase):
    def report(self, findings, **status):
        tools = {t: {"status": status.get(t, "ok"), "detail": "", "seconds": 0.1, "count": 0} for t in ("semgrep", "gitleaks")}
        return sc.ScanReport(findings=findings, tools=tools, file_count=1)

    def test_rescans_only_the_tools_that_found_something(self):
        with patch.object(sc, "run_scanners", return_value=self.report([])) as run:
            check = sc.recheck_file("calc.py", "fixed", [SHELL, SECRET])
        self.assertEqual(run.call_args.kwargs["scanners"], ["gitleaks", "semgrep"])
        self.assertEqual(run.call_args.args[0], {"calc.py": "fixed"})
        self.assertTrue(check["passed"])
        self.assertEqual(sc.recheck_summary(check), "2 resolved, 0/2 must-fix remaining, 0 new")

    def test_crashed_scanner_is_not_counted_as_fixed(self):
        with patch.object(sc, "run_scanners", return_value=self.report([], gitleaks="error")):
            check = sc.recheck_file("calc.py", "x", [SHELL, SECRET])
        self.assertEqual([f.rule_id for f in check["resolved"]], ["subprocess-shell-true"])
        self.assertEqual(check["unchecked"], [SECRET])
        with patch.object(sc, "run_scanners", return_value=self.report([], semgrep="missing", gitleaks="timeout")):
            check = sc.recheck_file("calc.py", "x", [SHELL])
        self.assertEqual(check["status"], "unavailable")
        self.assertIn("semgrep: missing", sc.recheck_summary(check))

    def test_suppression_fails_the_check(self):
        with patch.object(sc, "run_scanners", return_value=self.report([])):
            check = sc.recheck_file("calc.py", "call()  # nosemgrep\n", [SHELL], original_code="call()\n")
        self.assertFalse(check["passed"])
        self.assertIn("suppression", sc.recheck_feedback(check))

    def test_feedback_lists_remaining_and_new(self):
        new = F("eval-detected", 12, "eval(expr)", severity="medium")
        check = sc.compare_findings([SHELL], [SHELL, new])
        text = sc.recheck_feedback(check)
        self.assertIn("still present and MUST be fixed", text)
        self.assertIn("subprocess-shell-true", text)
        self.assertIn("INTRODUCED", text)
        self.assertIn("eval(expr)", text)


class PipelineGate(unittest.TestCase):
    def run_with_checks(self, checks, findings=(SHELL,), must_fix=None, iters=3):
        fake = FakeAgent([REQ_REAL])
        events = []
        with patch.object(fo, "ask_agent", side_effect=fake), patch.object(fo, "recheck_file", side_effect=checks) as rc:
            res = fo.optimize_single_file("calc.py", ORIGINAL, "audit", max_iterations=iters, findings=list(findings),
                                          must_fix=must_fix, progress_callback=lambda *e: events.append(e))
        return res, fake, events, rc

    def test_failed_recheck_sends_code_back_to_the_coder(self):
        failing = sc.compare_findings([SHELL], [SHELL])
        passing = sc.compare_findings([SHELL], [])
        for c in (failing, passing):
            c["status"] = "ok"
        res, fake, events, rc = self.run_with_checks([failing, passing])
        coder_calls = fake.roles(fo.CODER_PROMPT)
        self.assertEqual(len(coder_calls), 2)
        self.assertIn("MUST be fixed", coder_calls[1][2])
        self.assertIn("SCANNER FINDINGS THAT MUST BE GONE", coder_calls[0][2])
        self.assertTrue(res["approved"])
        self.assertIs(res["scan_check"], passing)
        self.assertIn(("rescan", "failed", "0 resolved, 1/1 must-fix remaining, 0 new"), events)
        self.assertIn(("rescan", "passed", "1 resolved, 0/1 must-fix remaining, 0 new"), events)
        self.assertEqual(len(fake.roles(fo.REVIEWER_PROMPT)), 1)     # reviewer only sees the passing version
        self.assertIn("Scanner re-check: 1 resolved", fake.roles(fo.REVIEWER_PROMPT)[0][2])
        self.assertEqual(rc.call_args.kwargs["original_code"], ORIGINAL)

    def test_never_fixed_ends_unapproved(self):
        failing = dict(sc.compare_findings([SHELL], [SHELL]), status="ok")
        res, fake, _, _ = self.run_with_checks([failing] * 2, iters=2)
        self.assertFalse(res["approved"])
        self.assertEqual(len(fake.roles(fo.REVIEWER_PROMPT)), 0)
        self.assertTrue(any("Scanner re-check failed" in l for l in res["logs"]))

    def test_unavailable_scanners_skip_the_stage(self):
        res, _, events, _ = self.run_with_checks([{"status": "unavailable", "detail": "semgrep: missing"}])
        self.assertTrue(res["approved"])
        self.assertIn(("rescan", "skipped", "Scanner re-check skipped (semgrep: missing)"), events)

    def test_scanner_crash_does_not_lose_the_candidate(self):
        res, _, events, _ = self.run_with_checks(RuntimeError("boom"))
        self.assertTrue(res["approved"])
        self.assertTrue(any(e[:2] == ("rescan", "skipped") and "boom" in e[2] for e in events))

    def test_explicit_empty_must_fix_still_rechecks_for_new_issues(self):
        check = dict(sc.compare_findings([SHELL], [SHELL], must_fix=[]), status="ok")
        res, fake, _, rc = self.run_with_checks([check], must_fix=[])
        self.assertTrue(res["approved"])
        self.assertEqual(rc.call_args.args[3], [])
        self.assertNotIn("MUST BE GONE", fake.roles(fo.CODER_PROMPT)[0][2])

    def test_followup_forwards_findings(self):
        with patch.object(fo, "ask_agent", return_value="REFACTOR_NEEDED: fix it"), \
             patch.object(fo, "optimize_single_file", return_value={"optimized_code": ORIGINAL, "approved": True}) as opt:
            fo.handle_code_followup("calc.py", ORIGINAL, ORIGINAL, "audit", "fix", findings=[SHELL], must_fix=[])
        self.assertEqual((opt.call_args.kwargs["findings"], opt.call_args.kwargs["must_fix"]), ([SHELL], []))


class RecheckUi(unittest.TestCase):
    def test_panel(self):
        check = dict(sc.compare_findings([SHELL, PICKLE], [PICKLE, F("eval-detected", 3, "eval(x)", severity="medium")]),
                     status="ok")
        markup = ui.recheck_html(check)
        self.assertIn("Resolved (1)", markup)
        self.assertIn("Must-fix still present (0/1)", markup)
        self.assertIn("New findings (1)", markup)
        self.assertIn("Not required, still present (1)", markup)
        self.assertIn('class="jb-col bad"', markup)                 # the new medium finding blocks
        self.assertIn("skipped", ui.recheck_html({"status": "unavailable", "detail": "<x>"}))
        self.assertNotIn("<x>", ui.recheck_html({"status": "unavailable", "detail": "<x>"}))
        self.assertEqual(ui.recheck_html(None), "")
        self.assertIn(("rescan", "Scanner re-check"), fo.PIPELINE_STAGES)

    def test_report_knows_what_it_scanned(self):
        with patch.object(sc, "find_tool", return_value=None):
            rep = sc.run_scanners({"a.py": "x = 1\n"})
        self.assertTrue(rep.is_current("a.py", "x = 1\n"))
        self.assertFalse(rep.is_current("a.py", "x = 2\n"))
        self.assertFalse(rep.is_current("b.py", ""))


@unittest.skipUnless(sc.find_tool("semgrep") and os.environ.get("SEMGREP_RULES"), "semgrep or local SEMGREP_RULES not available")
class RealRecheck(unittest.TestCase):
    def test_real_fix_is_recognised(self):
        vulnerable = "import subprocess\n\ndef run(name):\n    subprocess.call('ls ' + name, shell=True)\n"
        fixed = "import subprocess\n\ndef run(name):\n    subprocess.call(['ls', '--', name])\n"
        before = sc.run_scanners({"app/run.py": vulnerable}, scanners=["semgrep"]).findings
        self.assertTrue(any(f.rule_id.endswith("subprocess-shell-true") for f in before))
        check = sc.recheck_file("app/run.py", fixed, before, original_code=vulnerable)
        self.assertEqual(check["status"], "ok")
        self.assertTrue(any(f.rule_id.endswith("subprocess-shell-true") for f in check["resolved"]))
        self.assertFalse(check["remaining"])
        still = sc.recheck_file("app/run.py", "# moved down a line\n" + vulnerable, before)   # cosmetic change only
        self.assertFalse(still["passed"])
