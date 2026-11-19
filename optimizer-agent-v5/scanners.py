"""Deterministic scanners behind one Finding model.

Security: Semgrep, gitleaks, OSV-Scanner, Bandit.  Code quality / non-standard usage: Ruff, mypy (Python),
PMD (Java), ESLint with security/React/a11y plugins and tsc (JavaScript/TypeScript).

The LLM auditor is good at explaining and fixing code but cannot be trusted to *find* every issue,
and it can hallucinate ones that are not there. Scanners give a repeatable, rule-based baseline:
every tool writes SARIF 2.1, `parse_sarif` normalises it into `Finding`s, and the auditor triages them.

Missing tools are reported, never fatal: `run_scanners` returns a per-tool status so the UI can show
install hints and carry on with whatever is installed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}
SEVERITY_ICONS = {"critical": "🟥", "high": "🟧", "medium": "🟨", "low": "🟦", "info": "⬜"}

# Registry packs used when SEMGREP_RULES is not set. They need semgrep.dev to be reachable.
DEFAULT_SEMGREP_CONFIGS = ("p/python", "p/java", "p/javascript", "p/typescript", "p/react", "p/owasp-top-ten")

DEFAULT_TIMEOUT_SEC = 900


PY = frozenset({".py"})
JAVA = frozenset({".java"})
JS_TS = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"})
TOOLS_DIR = Path(__file__).resolve().parent / "tools"
ESLINT_DIR = TOOLS_DIR / "eslint"


@dataclass(frozen=True)
class ScannerSpec:
    name: str                          # executable name
    label: str
    finds: str
    install: str
    exts: frozenset = frozenset()      # only run when the project has these file types (empty = always)
    output: str = "sarif"              # sarif (written to a file) | eslint-json | mypy-json | tsc-text (stdout)
    ok_exit: frozenset = frozenset({0, 1})
    group: str = "security"            # security | quality (for the UI)


SCANNERS: dict[str, ScannerSpec] = {
    "semgrep": ScannerSpec(
        "semgrep", "Semgrep", "Injection, XSS, unsafe deserialization, weak crypto and risky APIs in Python, Java, JS/TS",
        "pip install semgrep"),
    "gitleaks": ScannerSpec(
        "gitleaks", "gitleaks", "Hard-coded API keys, tokens, passwords and private keys",
        "brew install gitleaks  ·  or a binary from github.com/gitleaks/gitleaks/releases"),
    "osv-scanner": ScannerSpec(
        "osv-scanner", "OSV-Scanner", "Dependencies with known CVEs (requirements.txt, pom.xml, package-lock.json…)",
        "brew install osv-scanner  ·  or go install github.com/google/osv-scanner/v2/cmd/osv-scanner@latest",
        ok_exit=frozenset({0, 1, 128})),                     # 1 = vulnerabilities found, 128 = no packages
    "bandit": ScannerSpec(
        "bandit", "Bandit", "Python security anti-patterns (shell, pickle, yaml, weak crypto, temp files, asserts)",
        'pip install "bandit[sarif]"', exts=PY),
    "ruff": ScannerSpec(
        "ruff", "Ruff", "Python bugs and non-idiomatic code: undefined names, bugbear traps, outdated syntax",
        "pip install ruff", exts=PY, group="quality"),
    "mypy": ScannerSpec(
        "mypy", "mypy", "Python type errors: wrong attributes, arguments and return types",
        "pip install mypy", exts=PY, output="mypy-json", group="quality"),
    "pmd": ScannerSpec(
        "pmd", "PMD", "Java error-prone code, resource leaks, threading and security rules",
        "brew install pmd  ·  or unzip a release from github.com/pmd/pmd/releases (needs Java 8+)",
        exts=JAVA, ok_exit=frozenset({0, 4, 5}), group="quality"),   # 4 = violations, 5 = some files unparsable
    "eslint": ScannerSpec(
        "eslint", "ESLint", "JS/TS/React: XSS sinks, eval, hooks misuse, accessibility, risky TypeScript",
        "Node.js 18+, then: cd tools/eslint && npm ci", exts=JS_TS, output="eslint-json", group="quality"),
    "tsc": ScannerSpec(
        "tsc", "TypeScript (tsc)", "TypeScript type errors (projects with a tsconfig.json)",
        "Node.js 18+, then: cd tools/eslint && npm ci", exts=frozenset({".ts", ".tsx"}), output="tsc-text",
        ok_exit=frozenset({0, 1, 2}), group="quality"),
}


@dataclass(frozen=True)
class Finding:
    tool: str
    rule_id: str
    severity: str                      # one of SEVERITIES
    category: str                      # security | secret | dependency | quality
    title: str
    message: str
    file: str                          # project-relative POSIX path
    line: int = 0                      # 0 = whole file (e.g. a dependency in a manifest)
    end_line: int = 0
    cwe: tuple[str, ...] = ()
    snippet: str = ""
    fix_hint: str = ""
    help_url: str = ""
    fingerprint: str = ""
    in_test: bool = False              # tests/fixtures: often deliberate fake secrets or unsafe calls
    also: tuple[str, ...] = ()         # other tools that flagged the same line and weakness

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cwe"] = list(self.cwe)
        d["also"] = list(self.also)
        return d

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file


@dataclass
class ScanReport:
    findings: list[Finding] = field(default_factory=list)
    tools: dict[str, dict] = field(default_factory=dict)   # name -> {status, detail, seconds, count}
    file_count: int = 0
    hashes: dict[str, str] = field(default_factory=dict)   # path -> sha256 of the content that was scanned

    def is_current(self, path: str, content: str | bytes) -> bool:
        """True when `content` is exactly what was scanned, so line numbers and the re-check baseline are valid."""
        data = content.encode("utf-8") if isinstance(content, str) else content
        return self.hashes.get(path) == hashlib.sha256(data).hexdigest()

    def for_file(self, path: str) -> list[Finding]:
        return [f for f in self.findings if f.file == path]

    def counts_by_file(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.file] = counts.get(f.file, 0) + 1
        return counts

    def counts_by_severity(self) -> dict[str, int]:
        counts = {s: 0 for s in SEVERITIES}
        for f in self.findings:
            counts[f.severity] += 1
        return counts

    def to_dict(self) -> dict:
        return {"findings": [f.to_dict() for f in self.findings], "tools": self.tools, "file_count": self.file_count}


# --------------------------------------------------------------------------------------------------
# Tool discovery
# --------------------------------------------------------------------------------------------------
def find_tool(name: str) -> str | None:
    """Looks in SCANNER_BIN_DIR, tools/eslint (for eslint/tsc), next to the running Python, then on PATH.

    ESLint is only taken from tools/eslint: a global eslint would not have the plugins our config imports."""
    candidates = [p for p in os.environ.get("SCANNER_BIN_DIR", "").split(os.pathsep) if p]
    if name in ("eslint", "tsc"):
        if not shutil.which("node"):                   # the npm shims need Node.js on PATH
            return None
        local = shutil.which(name, path=str(ESLINT_DIR / "node_modules" / ".bin"))
        if local or name == "eslint":
            return local
    candidates.append(str(Path(sys.executable).parent))
    for folder in candidates:
        found = shutil.which(name, path=folder)
        if found:
            return found
    return shutil.which(name)


def available_scanners() -> dict[str, str | None]:
    return {name: find_tool(spec.name) for name, spec in SCANNERS.items()}


def semgrep_configs() -> list[str]:
    """Local rule folders from SEMGREP_RULES (os.pathsep-separated), else the registry packs."""
    local = [p for p in os.environ.get("SEMGREP_RULES", "").split(os.pathsep) if p.strip()]
    return local or list(DEFAULT_SEMGREP_CONFIGS)


RUFF_RULES = "E4,E7,E9,F,B,UP,SIM,PERF,PIE,RUF,PL"
PMD_RULESETS = "rulesets/java/quickstart.xml,category/java/security.xml,category/java/multithreading.xml"


def _has_ruff_config(root: Path | None) -> bool:
    if root is None:
        return False
    if (root / "ruff.toml").is_file() or (root / ".ruff.toml").is_file():
        return True
    pyproject = root / "pyproject.toml"
    return pyproject.is_file() and "[tool.ruff" in pyproject.read_text(encoding="utf-8", errors="replace")


def tsconfig_dirs(root: Path, limit: int = 4) -> list[Path]:
    """Folders with a tsconfig.json (outside node_modules), shallowest first."""
    found = sorted((p.parent for p in root.rglob("tsconfig.json") if "node_modules" not in p.parts),
                   key=lambda d: (len(d.parts), str(d)))
    return found[:limit]


def build_command(tool: str, exe: str, out_file: str, root: Path | None = None) -> list[str]:
    if tool == "semgrep":
        cmd = [exe, "scan", "--sarif", "--output", out_file, "--metrics=off", "--disable-version-check",
               "--quiet", "--timeout", "30"]
        for cfg in semgrep_configs():
            cmd += ["--config", cfg]
        return cmd + ["."]
    if tool == "gitleaks":
        return [exe, "dir", ".", "--report-format", "sarif", "--report-path", out_file,
                "--no-banner", "--exit-code", "0", "--redact"]
    if tool == "osv-scanner":
        cmd = [exe, "scan", "source", "-r", "--format", "sarif", "--output-file", out_file]
        if os.environ.get("OSV_RESOLVE", "") != "1":
            cmd.append("--no-resolve")             # direct deps only; transitive resolution needs deps.dev
        if os.environ.get("OSV_OFFLINE", "") == "1":
            cmd.append("--offline-vulnerabilities")  # uses a DB from `osv-scanner --download-offline-databases`
        return cmd + ["."]
    if tool == "bandit":
        return [exe, "-r", ".", "-f", "sarif", "-o", out_file, "-q", "--skip", "B101",
                "-x", "./node_modules,./venv,./.venv,./build,./dist"]
    if tool == "ruff":
        cmd = [exe, "check", "--output-format", "sarif", "-o", out_file, "--no-cache", "--exit-zero"]
        if not _has_ruff_config(root):               # respect the project's own Ruff config when it has one
            cmd += ["--select", RUFF_RULES]
        return cmd + ["."]
    if tool == "mypy":
        return [exe, "--ignore-missing-imports", "--follow-imports=silent", "--explicit-package-bases",
                "--namespace-packages", "--no-error-summary", "--hide-error-context", "--no-color-output",
                "--no-incremental", "--cache-dir", os.devnull, "-O", "json", "."]
    if tool == "pmd":
        return [exe, "check", "-d", ".", "-R", PMD_RULESETS, "-f", "sarif", "-r", out_file,
                "--no-progress", "--no-cache"]
    if tool == "eslint":
        return [exe, "--config", str(ESLINT_DIR / "eslint.config.mjs"), "--no-warn-ignored", "-f", "json", "."]
    if tool == "tsc":
        return [exe, "--noEmit", "--pretty", "false", "-p"]     # + tsconfig path, once per project folder
    raise ValueError(f"Unknown scanner: {tool}")




# --------------------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------------------
def _safe_rel(path: str) -> str | None:
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return None
    return p.as_posix()


def write_tree(files: dict[str, str | bytes], root: Path) -> int:
    """Materialises an in-memory project; entries that would escape `root` are skipped."""
    written = 0
    for rel, content in files.items():
        safe = _safe_rel(rel)
        if safe is None:
            continue
        target = root / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        target.write_bytes(data)
        written += 1
    return written


def _skip_reason(tool: str, root: Path, exts_present: set[str]) -> str:
    spec = SCANNERS[tool]
    if spec.exts and not (spec.exts & exts_present):
        kinds = {PY: "Python", JAVA: "Java", JS_TS: "JavaScript/TypeScript"}.get(spec.exts, "TypeScript")
        return f"No {kinds} files"
    if tool == "tsc" and not tsconfig_dirs(root):
        return "No tsconfig.json"
    return ""


def _run_one(tool: str, exe: str, root: Path, timeout: int) -> tuple[dict, list[Finding]]:
    spec = SCANNERS[tool]
    out_file = str(root.parent / f"{tool}.sarif")
    cmd = build_command(tool, exe, out_file, root)
    started = time.monotonic()
    env = dict(os.environ, SEMGREP_SEND_METRICS="off", NO_COLOR="1")
    runs = ([cmd + [str((d / "tsconfig.json").relative_to(root))] for d in tsconfig_dirs(root)]
            if tool == "tsc" else [cmd])
    stdout, stderr, codes = [], [], []
    try:
        for c in runs:
            remaining = max(1, timeout - int(time.monotonic() - started))
            proc = subprocess.run(c, cwd=root, capture_output=True, text=True, timeout=remaining, env=env)
            stdout.append(proc.stdout or "")
            stderr.append(proc.stderr or "")
            codes.append(proc.returncode)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "detail": f"Stopped after {timeout}s", "seconds": timeout, "count": 0}, []
    except OSError as e:
        return {"status": "error", "detail": str(e), "seconds": 0.0, "count": 0}, []
    out_text = "\n".join(stdout)

    findings: list[Finding] = []
    parse_error = ""
    note = ""
    try:
        if spec.output == "sarif":
            if Path(out_file).is_file() and Path(out_file).stat().st_size:
                findings = parse_sarif(json.loads(Path(out_file).read_text(encoding="utf-8")), tool, root)
        elif spec.output == "eslint-json":
            findings = parse_eslint_json(out_text, root) if out_text.strip() else []
        elif spec.output == "mypy-json":
            findings = parse_mypy_json(out_text, root)
            broken = sorted({f.file for f in findings if f.rule_id == "syntax"})
            syntax = [f for f in findings if f.rule_id == "syntax"]
            # mypy stops at the first unparsable file (e.g. Python 2 code): exclude it and retry, a few times
            for _ in range(8):
                if not (codes and codes[-1] == 2 and syntax):
                    break
                excludes = [a for b in broken for a in ("--exclude", "^" + re.escape(b) + "$")]
                proc = subprocess.run(cmd[:-1] + excludes + ["."], cwd=root, capture_output=True, text=True,
                                      timeout=max(1, timeout - int(time.monotonic() - started)), env=env)
                codes[-1], stderr[-1] = proc.returncode, proc.stderr or ""
                latest = parse_mypy_json(proc.stdout or "", root)
                syntax = [f for f in latest if f.rule_id == "syntax"]
                findings = [f for f in findings if f.rule_id == "syntax"] + latest
                broken = sorted({f.file for f in findings if f.rule_id == "syntax"})
                if "no .py[i] files" in (proc.stderr or "") + (proc.stdout or ""):
                    codes[-1] = 0                      # every Python file was unparsable: nothing left to check
            if broken:
                note = f"{len(broken)} file(s) could not be parsed and were skipped"
        elif spec.output == "tsc-text":
            stats: dict = {}
            findings = parse_tsc_output(out_text, root, stats)
            if stats.get("untrusted_files"):
                note = (f"{stats['untrusted_files']} file(s) skipped: they import packages that are not installed, "
                        "which makes their types unreliable")
    except (ValueError, KeyError, TypeError) as e:
        parse_error = f"Could not read {tool} output: {e}"

    bad = [c for c in codes if c not in spec.ok_exit]
    if tool == "tsc" and not findings:
        config_errors = [l for l in out_text.splitlines() if re.search(r"error TS[56]\d{3}", l)]
        if config_errors:                                      # e.g. "extends" a package that is not installed
            bad, parse_error = [codes[0]], "tsconfig could not be loaded: " + config_errors[0][:250]
    if bad or parse_error:
        status = "error"
        detail = parse_error or explain_failure(tool, "\n".join(stderr) or out_text, bad[0])
    else:
        status, detail = "ok", ""
    info = {"status": status, "detail": detail, "seconds": round(time.monotonic() - started, 1), "count": len(findings)}
    if note:
        info["note"] = note
    return info, findings


_NETWORK_HINTS = {
    "osv-scanner": ("api.osv.dev", "Could not reach api.osv.dev. On an offline machine run "
                                   "`osv-scanner --download-offline-databases` once, then set OSV_OFFLINE=1."),
    "semgrep": ("semgrep.dev", "Could not download rules from semgrep.dev. Point SEMGREP_RULES at a local "
                               "checkout of github.com/semgrep/semgrep-rules (e.g. its python/, java/, javascript/ folders)."),
}


def explain_failure(tool: str, output: str, returncode: int) -> str:
    """A short, actionable reason instead of the tool's last log lines."""
    host, hint = _NETWORK_HINTS.get(tool, ("", ""))
    if host and host in output:
        return hint
    lines = [l.strip() for l in output.strip().splitlines() if l.strip()]
    errors = [l for l in lines if re.search(r"\b(error|fatal|failed|invalid)\b", l, re.IGNORECASE)]
    picked = errors[-2:] or lines[-2:]
    return " ".join(picked)[:400] or f"exit code {returncode}"


def run_scanners(files: dict[str, str | bytes], scanners: list[str] | None = None,
                 timeout: int = DEFAULT_TIMEOUT_SEC) -> ScanReport:
    """Writes `files` to a temp folder, runs the installed scanners in parallel and merges their findings."""
    wanted = list(scanners) if scanners is not None else list(SCANNERS)
    report = ScanReport()
    available = available_scanners()
    with tempfile.TemporaryDirectory(prefix="scan_") as tmp:
        root = Path(tmp) / "project"
        root.mkdir()
        report.file_count = write_tree(files, root)
        report.hashes = {rel: hashlib.sha256(c.encode("utf-8") if isinstance(c, str) else c).hexdigest()
                         for rel, c in files.items()}
        exts_present = {PurePosixPath(rel).suffix.lower() for rel in files}
        jobs = {}
        with ThreadPoolExecutor(max_workers=max(1, min(6, len(wanted)))) as pool:
            for tool in wanted:
                if tool not in SCANNERS:
                    report.tools[tool] = {"status": "error", "detail": "Unknown scanner", "seconds": 0.0, "count": 0}
                elif reason := _skip_reason(tool, root, exts_present):
                    report.tools[tool] = {"status": "skipped", "detail": reason, "seconds": 0.0, "count": 0}
                elif not available.get(tool):
                    report.tools[tool] = {"status": "missing", "detail": SCANNERS[tool].install, "seconds": 0.0, "count": 0}
                else:
                    jobs[tool] = pool.submit(_run_one, tool, available[tool], root, timeout)
            for tool, job in jobs.items():
                report.tools[tool], found = job.result()
                report.findings.extend(found)
    report.findings = sort_findings(merge_cross_tool(_locate_dependency_lines(dedupe(report.findings), files)))
    return report


# --------------------------------------------------------------------------------------------------
# SARIF parsing
# --------------------------------------------------------------------------------------------------
_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "testdata", "fixtures", "e2e", "cypress"}
_TEST_FILE_RE = re.compile(r"(^test_.*\.py$|^tests?\.py$|.*_test\.(py|go)$|.*\.(spec|test)\.(js|jsx|ts|tsx|mjs)$|.*Tests?\.java$)")


_VENDOR_DIRS = {"vendor", "vendors", "third_party", "third-party", "thirdparty", "external", "bower_components"}
_VENDOR_FILE_RE = re.compile(r".*\.(min|bundle|umd|prod)\.(js|mjs|cjs)$")


def is_vendored_path(path: str) -> bool:
    """Copied-in third-party code (vendor/, *.min.js, plain .js inside an assets folder)."""
    parts = PurePosixPath(path).parts
    lowered = [p.lower() for p in parts[:-1]]
    name = parts[-1].lower() if parts else ""
    return (any(p in _VENDOR_DIRS for p in lowered) or bool(_VENDOR_FILE_RE.match(name))
            or ("assets" in lowered and name.endswith((".js", ".mjs", ".cjs"))))


def is_test_path(path: str) -> bool:
    """Test code, fixtures or vendored libraries: findings there are hidden by default and sorted last."""
    parts = PurePosixPath(path).parts
    return (any(p.lower() in _TEST_DIRS for p in parts[:-1]) or bool(parts and _TEST_FILE_RE.match(parts[-1]))
            or is_vendored_path(path))


_CWE_RE = re.compile(r"CWE-(\d+)", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s)\]>\"'|]+")
_OSV_PKG_RE = re.compile(r"Package '([^']+)@([^']+)'")


def _severity_from_score(score: float) -> str:
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0:
        return "low"
    return "info"


_LEVEL_SEVERITY = {"error": "high", "warning": "medium", "note": "low", "none": "info"}


def severity_for(tool: str, result: dict, rule: dict) -> str:
    props = {**(rule.get("properties") or {}), **(result.get("properties") or {})}
    raw = props.get("security-severity")
    if raw not in (None, ""):
        try:
            return _severity_from_score(float(raw))
        except (TypeError, ValueError):
            pass
    if tool == "gitleaks":
        return "high"                                   # a leaked credential is always urgent
    level = result.get("level") or (rule.get("defaultConfiguration") or {}).get("level") or "warning"
    return _LEVEL_SEVERITY.get(level, "medium")


def props_res(result: dict) -> dict:
    return result.get("properties") or {}


def _downgrade(severity: str) -> str:
    return SEVERITIES[min(SEVERITY_RANK[severity] + 1, len(SEVERITIES) - 1)]


def ruff_severity(code: str) -> str:
    """Ruff marks everything 'error'; rank by what the rule family means."""
    if code.startswith(("PLC", "PLR")) or code in {"RUF012", "RUF005", "RUF010", "RUF015", "SIM108"}:
        return "info"                           # style, refactoring and complexity hints
    if code.startswith("E9") or code in {"F821", "F822", "F823", "F811", "F632", "F702", "F704", "F706", "F707"}:
        return "medium"                         # syntax errors, undefined or shadowed names: real bugs
    if code.startswith(("B", "PLE", "RUF006", "RUF012", "PLW")):
        return "medium" if code.startswith(("B0", "PLE", "RUF006")) else "low"
    return "low"


def clean_rule_id(tool: str, rule_id: str) -> str:
    """Semgrep prefixes local rule ids with their folder path ('home.me.semgrep-rules.python...')."""
    if tool != "semgrep":
        return rule_id
    if ".semgrep-rules." in rule_id:
        return rule_id.split(".semgrep-rules.", 1)[1]
    for cfg in semgrep_configs():
        prefix = str(Path(cfg).expanduser()).strip("/\\").replace("/", ".").replace("\\", ".") + "."
        if rule_id.startswith(prefix):
            return rule_id[len(prefix):]
    return rule_id


def normalize_path(uri: str, root: Path | str | None) -> str:
    if uri.startswith("file:"):
        uri = unquote(urlparse(uri).path)
    path = uri.replace("\\", "/")
    if re.match(r"^/[A-Za-z]:/", path):            # file:///C:/... on Windows
        path = path[1:]
    if root is not None and (path.startswith("/") or re.match(r"^[A-Za-z]:/", path)):
        for base in (str(root), os.path.realpath(str(root))):
            prefix = base.replace("\\", "/").rstrip("/") + "/"
            for candidate in (path, os.path.realpath(path).replace("\\", "/")):
                if candidate.startswith(prefix):
                    return candidate[len(prefix):]
    while path.startswith("./"):
        path = path[2:]
    return path


def _redact(secret: str) -> str:
    secret = secret.strip()
    if len(secret) <= 8:
        return "•" * len(secret)
    return secret[:4] + "•" * min(12, len(secret) - 4)


def _rule_help(rule: dict) -> str:
    h = rule.get("help") or {}
    return h.get("text") or h.get("markdown") or (rule.get("fullDescription") or {}).get("text") or ""


def _osv_fix_hint(rule: dict, package: str) -> str:
    md = (rule.get("help") or {}).get("markdown", "")
    md = md.split("Fixed Version", 1)[1] if "Fixed Version" in md else ""   # skip the "Affected Packages" table
    m = re.search(r"\|\s*[^|]+\|\s*" + re.escape(package) + r"\s*\|\s*([^|\s]+)\s*\|", md, re.IGNORECASE) if package else None
    return f"Upgrade {package} to {m.group(1)} or later" if m else (f"Upgrade {package} to a patched version" if package else "")


def parse_sarif(data: dict, tool: str, root: Path | str | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for run in data.get("runs") or []:
        driver = (run.get("tool") or {}).get("driver") or {}
        rules_list = driver.get("rules") or []
        rules = {r.get("id"): r for r in rules_list}
        for res in run.get("results") or []:
            raw_id = res.get("ruleId") or ""
            rule = rules.get(raw_id)
            if rule is None and isinstance(res.get("ruleIndex"), int) and 0 <= res["ruleIndex"] < len(rules_list):
                rule = rules_list[res["ruleIndex"]]
            rule = rule or {}
            rule_id = clean_rule_id(tool, raw_id)
            message = ((res.get("message") or {}).get("text") or "").strip()

            loc = ((res.get("locations") or [{}])[0] or {}).get("physicalLocation") or {}
            uri = (loc.get("artifactLocation") or {}).get("uri") or ""
            region = loc.get("region") or {}
            line = int(region.get("startLine") or 0)
            end_line = int(region.get("endLine") or line)
            snippet = ((region.get("snippet") or {}).get("text") or "").strip()

            props = rule.get("properties") or {}
            tags = [str(t) for t in props.get("tags") or []]
            cwe = tuple(dict.fromkeys(f"CWE-{m}" for t in tags for m in _CWE_RE.findall(t)))
            severity = severity_for(tool, res, rule)
            help_text = _rule_help(rule)
            help_url = rule.get("helpUri") or ""
            fix_hint = ""
            title = ((rule.get("shortDescription") or {}).get("text") or "").strip()

            if tool == "gitleaks":
                category = "secret"
                snippet = _redact(snippet)
                title = f"Hard-coded secret ({rule_id})"
                fix_hint = "Remove the secret from source, rotate it, and load it from an environment variable or secret store"
            elif tool == "osv-scanner":
                category = "dependency"
                m = _OSV_PKG_RE.search(message)
                package, version = (m.group(1), m.group(2)) if m else ("", "")
                title = title or rule_id
                fix_hint = _osv_fix_hint(rule, package)
                help_url = help_url or f"https://osv.dev/{rule_id}"
            elif tool == "bandit":
                category = "security"
                sev = str(props_res(res).get("issue_severity", "")).lower()
                conf = str(props_res(res).get("issue_confidence", "")).lower()
                severity = {"high": "high", "medium": "medium", "low": "low"}.get(sev, severity)
                if conf == "low":
                    severity = _downgrade(severity)
                title = title or rule_id
                fix_hint = ""
                help_url = help_url or f"https://bandit.readthedocs.io/en/latest/search.html?q={rule_id}"
            elif tool == "ruff":
                category = "quality"
                severity = ruff_severity(rule_id)
                title = (rule.get("properties") or {}).get("name", "").replace("-", " ").capitalize() or rule_id
                help_url = help_url or f"https://docs.astral.sh/ruff/rules/{(rule.get('properties') or {}).get('name', '')}/"
                fix_hint = ""
            elif tool == "pmd":
                ruleset = str((rule.get("properties") or {}).get("ruleset", ""))
                priority = int((rule.get("properties") or {}).get("priority") or 3)
                category = "security" if ruleset.lower() == "security" else "quality"
                severity = {1: "high", 2: "medium", 3: "medium", 4: "low", 5: "low"}.get(priority, "low")
                if category == "quality" and severity == "high":
                    severity = "medium"
                title = rule_id
                fix_hint = ""
            else:
                is_quality = not cwe and ("security" not in tags and ".security." not in f".{rule_id}.")
                category = "quality" if is_quality else "security"
                if is_quality and SEVERITY_RANK[severity] < SEVERITY_RANK["low"]:
                    severity = "low"                    # correctness/style rules should not outrank vulns
                if not title or title.lower().startswith("semgrep finding"):
                    title = rule_id.rsplit(".", 1)[-1].replace("-", " ").capitalize()
                fix_hint = help_text.split("\n")[0][:300] if help_text and help_text.strip() != message else ""

            if not help_url:
                urls = _URL_RE.findall(help_text)
                help_url = urls[0].rstrip(".,") if urls else ""

            fps = {**(res.get("fingerprints") or {}), **(res.get("partialFingerprints") or {})}
            fp = next((str(v) for v in fps.values() if v), "")
            path = normalize_path(uri, root)
            if not fp:
                fp = hashlib.sha256(f"{tool}|{rule_id}|{path}|{line}|{message}".encode()).hexdigest()[:24]

            findings.append(Finding(tool=tool, rule_id=rule_id, severity=severity, category=category,
                                    title=title, message=message, file=path, line=line, end_line=end_line,
                                    cwe=cwe, snippet=snippet[:500], fix_hint=fix_hint, help_url=help_url,
                                    fingerprint=fp, in_test=is_test_path(path)))
    return findings


# --------------------------------------------------------------------------------------------------
# Post-processing and presentation helpers
# --------------------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------------
# Non-SARIF outputs
# --------------------------------------------------------------------------------------------------
_ESLINT_SECURITY = ("security/", "no-unsanitized/")
_ESLINT_SECURITY_RULES = {"no-eval", "no-implied-eval", "no-new-func", "react/no-danger", "react/jsx-no-target-blank",
                          "react/jsx-no-script-url"}
_ESLINT_HIGH = {"no-unsanitized/method", "no-unsanitized/property", "no-eval", "no-implied-eval", "no-new-func",
                "react/jsx-no-script-url", "react-hooks/rules-of-hooks"}
_ESLINT_INFO = {"no-var", "prefer-const", "eqeqeq", "@typescript-eslint/no-explicit-any", "prefer-rest-params",
                "prefer-spread", "no-useless-escape", "@typescript-eslint/no-inferrable-types"}
_ESLINT_CWE = {"no-unsanitized/method": "CWE-79", "no-unsanitized/property": "CWE-79", "react/no-danger": "CWE-79",
               "react/jsx-no-script-url": "CWE-79", "no-eval": "CWE-95", "no-implied-eval": "CWE-95",
               "no-new-func": "CWE-95", "security/detect-eval-with-expression": "CWE-95",
               "security/detect-child-process": "CWE-78", "security/detect-non-literal-fs-filename": "CWE-22",
               "security/detect-non-literal-regexp": "CWE-1333", "security/detect-unsafe-regex": "CWE-1333",
               "react/jsx-no-target-blank": "CWE-1022", "security/detect-pseudoRandomBytes": "CWE-338"}


def _make(tool, rule_id, severity, category, title, message, path, line, end_line=0, cwe=(), snippet="",
          help_url="") -> Finding:
    fp = hashlib.sha256(f"{tool}|{rule_id}|{path}|{line}|{message}".encode()).hexdigest()[:24]
    return Finding(tool=tool, rule_id=rule_id, severity=severity, category=category, title=title, message=message,
                   file=path, line=line, end_line=end_line or line, cwe=tuple(cwe), snippet=snippet[:500],
                   help_url=help_url, fingerprint=fp, in_test=is_test_path(path))


def parse_eslint_json(text: str, root: Path | str | None) -> list[Finding]:
    out = []
    for entry in json.loads(text):
        path = normalize_path(entry.get("filePath", ""), root)
        lines = (entry.get("source") or "").splitlines()
        for m in entry.get("messages") or []:
            rule = m.get("ruleId")
            if not rule:                                      # parse error ("fatal"): not a code finding
                continue
            is_sec = rule.startswith(_ESLINT_SECURITY) or rule in _ESLINT_SECURITY_RULES
            if not is_sec and is_vendored_path(path):         # style of copied-in libraries is not ours to fix
                continue
            if rule in _ESLINT_HIGH:
                sev = "high"
            elif rule in _ESLINT_INFO:
                sev = "info"
            elif is_sec or m.get("severity") == 2:
                sev = "medium"
            else:
                sev = "low"
            line = int(m.get("line") or 0)
            cwe = (_ESLINT_CWE[rule],) if rule in _ESLINT_CWE else ()
            plugin = rule.split("/")[0] if "/" in rule else ""
            url = {"": f"https://eslint.org/docs/latest/rules/{rule}",
                   "@typescript-eslint": f"https://typescript-eslint.io/rules/{rule.split('/', 1)[-1]}"}.get(plugin, "")
            out.append(_make("eslint", rule, sev, "security" if is_sec else "quality",
                             rule.split("/")[-1].replace("-", " ").capitalize(), m.get("message", ""), path, line,
                             int(m.get("endLine") or line), cwe,
                             lines[line - 1].strip() if 0 < line <= len(lines) else "", url))
    return out


_MYPY_MEDIUM = {"name-defined", "attr-defined", "call-arg", "arg-type", "return-value", "union-attr", "index",
                "operator", "call-overload", "has-type", "func-returns-value", "list-item", "dict-item"}
_MYPY_INFO = {"var-annotated", "annotation-unchecked", "import-untyped", "import-not-found", "no-redef"}


def parse_mypy_json(text: str, root: Path | str | None) -> list[Finding]:
    out = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        d = json.loads(raw)
        if d.get("severity") != "error":
            continue
        code = d.get("code") or "misc"
        sev = "medium" if code in _MYPY_MEDIUM or code == "syntax" else ("info" if code in _MYPY_INFO else "low")
        msg = d.get("message", "") + (f" ({d['hint']})" if d.get("hint") else "")
        out.append(_make("mypy", code, sev, "quality", f"Type error: {code}", msg,
                         normalize_path(d.get("file", ""), root), int(d.get("line") or 0), int(d.get("end_line") or 0),
                         help_url=f"https://mypy.readthedocs.io/en/stable/error_code_list.html#{code}"))
    return out


# Errors that only mean "the project's node_modules / @types are not here" — not problems in the code
TSC_ENVIRONMENT_CODES = {"TS2307", "TS2875", "TS7016", "TS7026", "TS2792", "TS2688", "TS2318", "TS2580", "TS2582",
                         "TS2593", "TS2591", "TS2503", "TS2786", "TS2604", "TS6053", "TS5083"}
_TSC_LINE = re.compile(r"^(.+?)\((\d+),(\d+)\): error (TS\d+): (.*)$")


TSC_UNRESOLVED_IMPORT = {"TS2307", "TS7016", "TS2875"}


def parse_tsc_output(text: str, root: Path | str | None, stats: dict | None = None) -> list[Finding]:
    """Type errors from `tsc --noEmit`. An uploaded project has no node_modules, so in any file that imports a
    package which is not installed, types collapse and tsc reports misleading errors; those files are skipped
    (their count goes to stats['untrusted_files'])."""
    matches = [m.groups() for m in (_TSC_LINE.match(l.strip()) for l in text.splitlines()) if m]
    untrusted = {normalize_path(p, root) for p, _, _, code, _ in matches if code in TSC_UNRESOLVED_IMPORT}
    if stats is not None:
        stats["untrusted_files"] = len(untrusted)
    out = []
    for path, ln, _, code, msg in matches:
        if code in TSC_ENVIRONMENT_CODES or code == "TS2584" or normalize_path(path, root) in untrusted:
            continue
        sev = "medium" if code in {"TS2339", "TS2345", "TS2322", "TS2554", "TS2531", "TS2532", "TS18047",
                                   "TS18048", "TS2304", "TS2551"} else "low"
        out.append(_make("tsc", code, sev, "quality", f"Type error {code}", msg, normalize_path(path, root),
                         int(ln), help_url=f"https://typescript.tv/errors/#{code.lower()}"))
    return out


def merge_cross_tool(findings: list[Finding]) -> list[Finding]:
    """Two tools flagging the same weakness on the same line (e.g. Semgrep and Bandit on shell=True) become one
    finding: the most severe is kept and the other tool is listed in `also`."""
    kept: list[Finding] = []
    for f in sorted(findings, key=lambda x: (SEVERITY_RANK.get(x.severity, 9), x.tool != "semgrep")):
        if f.category == "security" and f.cwe and f.line:
            twin = next((k for k in kept if k.file == f.file and k.line == f.line and k.tool != f.tool
                         and k.category == "security" and set(k.cwe) & set(f.cwe)), None)
            if twin is not None:
                if f.tool not in twin.also:
                    kept[kept.index(twin)] = Finding(**{**twin.__dict__, "also": twin.also + (f.tool,)})
                continue
        kept.append(f)
    return kept


def dedupe(findings: list[Finding]) -> list[Finding]:
    seen, out = set(), []
    for f in findings:
        key = (f.tool, f.rule_id, f.file, f.line)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.in_test, SEVERITY_RANK.get(f.severity, 9), f.file, f.line, f.rule_id))


def _locate_dependency_lines(findings: list[Finding], files: dict[str, str | bytes]) -> list[Finding]:
    """OSV reports a manifest without a line; point at the line that names the package."""
    out = []
    for f in findings:
        if f.tool == "osv-scanner" and not f.line and f.file in files:
            m = _OSV_PKG_RE.search(f.message)
            content = files[f.file]
            text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
            if m:
                needle = re.compile(r"(^|[^\w.-])" + re.escape(m.group(1)) + r"([^\w.-]|$)", re.IGNORECASE)
                for i, line in enumerate(text.splitlines(), 1):
                    if needle.search(line):
                        f = Finding(**{**f.__dict__, "line": i, "end_line": i, "snippet": line.strip()[:500]})
                        break
        out.append(f)
    return out


def findings_prompt_block(findings: list[Finding], limit: int = 40) -> str:
    """A compact, model-friendly list of findings for the auditor prompt."""
    lines = []
    for i, f in enumerate(sort_findings(findings)[:limit], 1):
        cwe = f" ({', '.join(f.cwe)})" if f.cwe else ""
        where = f"line {f.line}" if f.line else "whole file"
        lines.append(f"{i}. [{f.severity.upper()}] {f.tool}:{f.rule_id}{cwe} at {where}: {f.message}")
        if f.snippet:
            lines.append(f"   code: {f.snippet[:200]}")
        if f.fix_hint:
            lines.append(f"   hint: {f.fix_hint[:200]}")
    if len(findings) > limit:
        lines.append(f"... and {len(findings) - limit} lower-priority findings not shown.")
    return "\n".join(lines)


def finding_goal(f: Finding) -> str:
    """Text for the optimizer's goals box when the user clicks 'fix this finding'."""
    cwe = f" ({', '.join(f.cwe)})" if f.cwe else ""
    parts = [f"Fix {f.tool} finding {f.rule_id}{cwe} at {f.location}: {f.message}"]
    if f.fix_hint:
        parts.append(f"Suggested fix: {f.fix_hint}")
    parts.append("Keep the public interface unchanged and add a test that fails on the vulnerable version.")
    return "\n".join(parts)


def findings_table_rows(findings: list[Finding]) -> list[dict]:
    return [{"Severity": f"{SEVERITY_ICONS.get(f.severity, '')} {f.severity}", "Tool": f.tool,
             "Category": f.category, "File": f.file, "Line": f.line or None, "Rule": f.rule_id,
             "CWE": ", ".join(f.cwe), "Also flagged by": ", ".join(f.also), "Test/vendored": f.in_test, "Message": f.message[:300], "Fix hint": f.fix_hint[:200],
             "Link": f.help_url or None}
            for f in findings]


def to_sarif(findings: list[Finding]) -> dict:
    """Exports merged findings as one SARIF 2.1 log (for GitHub code scanning or IDE viewers)."""
    level = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "none"}
    score = {"critical": "9.5", "high": "8.0", "medium": "5.5", "low": "2.0", "info": "0.0"}
    runs = []
    for tool in dict.fromkeys(f.tool for f in findings):
        mine = [f for f in findings if f.tool == tool]
        rules = {}
        for f in mine:
            rules.setdefault(f.rule_id, {"id": f.rule_id, "shortDescription": {"text": f.title or f.rule_id},
                                         "helpUri": f.help_url or None,
                                         "properties": {"tags": list(f.cwe), "security-severity": score[f.severity]}})
        results = [{"ruleId": f.rule_id, "level": level[f.severity], "message": {"text": f.message},
                    "locations": [{"physicalLocation": {"artifactLocation": {"uri": f.file},
                                                        **({"region": {"startLine": f.line}} if f.line else {})}}],
                    "partialFingerprints": {"jbFingerprint": f.fingerprint}} for f in mine]
        for r in rules.values():
            if r["helpUri"] is None:
                del r["helpUri"]
        runs.append({"tool": {"driver": {"name": tool, "rules": list(rules.values())}}, "results": results})
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0", "runs": runs}


# --------------------------------------------------------------------------------------------------
# Re-check: did a rewrite actually remove the findings, and did it add new ones?
# --------------------------------------------------------------------------------------------------
BLOCKING_SEVERITIES = ("critical", "high", "medium")


def default_must_fix(findings: list[Finding]) -> list[Finding]:
    """What the optimizer must make disappear unless the user says otherwise: real, urgent, non-test findings."""
    return [f for f in findings if f.severity in ("critical", "high") and f.category != "quality" and not f.in_test]


def _snip(f: Finding) -> str:
    return re.sub(r"\s+", " ", f.snippet).strip()


def compare_findings(before: list[Finding], after: list[Finding], must_fix: list[Finding] | None = None) -> dict:
    """Matches findings across two versions of a file by (tool, rule) — first on identical code, then by count,
    because line numbers shift when code is rewritten.

    Returns resolved / remaining (must-fix findings still present) / still_present / introduced, plus `passed`:
    every must-fix finding is gone and nothing new at medium or above was introduced."""
    must_fix = list(default_must_fix(before) if must_fix is None else must_fix)
    by_key_before: dict[tuple, list[Finding]] = {}
    by_key_after: dict[tuple, list[Finding]] = {}
    for f in before:
        by_key_before.setdefault((f.tool, f.rule_id), []).append(f)
    for f in after:
        by_key_after.setdefault((f.tool, f.rule_id), []).append(f)

    still_present: list[Finding] = []
    resolved: list[Finding] = []
    introduced: list[Finding] = []
    for key in set(by_key_before) | set(by_key_after):
        old = list(by_key_before.get(key, []))
        new = list(by_key_after.get(key, []))
        unmatched_new = []
        for f in new:                                   # same code still flagged
            twin = next((o for o in old if _snip(o) and _snip(o) == _snip(f)), None)
            if twin is not None:
                old.remove(twin)
                still_present.append(twin)
            else:
                unmatched_new.append(f)
        # rewritten lines that are still flagged by the same rule: pair them with the closest old finding
        for f in sorted(unmatched_new, key=lambda x: x.line):
            if old:
                twin = min(old, key=lambda o: abs(o.line - f.line))
                old.remove(twin)
                still_present.append(twin)
            else:
                introduced.append(f)
        resolved.extend(old)

    remaining = [f for f in must_fix if f in still_present]
    blocking_new = [f for f in introduced if f.severity in BLOCKING_SEVERITIES and f.category != "quality"]
    return {
        "passed": not remaining and not blocking_new,
        "resolved": sort_findings(resolved),
        "remaining": sort_findings(remaining),
        "still_present": sort_findings(still_present),
        "introduced": sort_findings(introduced),
        "blocking_introduced": sort_findings(blocking_new),
        "must_fix_count": len(must_fix),
    }


_SUPPRESS_RE = re.compile(r"nosemgrep|#\s*nosec\b|//\s*nosec\b|gitleaks:allow|NOSONAR|@SuppressWarnings\(\s*\"(?:findsecbugs|security)",
                          re.IGNORECASE)


def added_suppressions(original_code: str, new_code: str) -> int:
    return max(0, len(_SUPPRESS_RE.findall(new_code)) - len(_SUPPRESS_RE.findall(original_code)))


def recheck_file(rel_path: str, new_code: str, before: list[Finding], must_fix: list[Finding] | None = None,
                 timeout: int = DEFAULT_TIMEOUT_SEC, original_code: str = "") -> dict:
    """Re-runs the scanners that produced `before` on the rewritten file and compares.

    `status` is "ok", or "unavailable" when no relevant scanner could run (the check is then skipped, not failed)."""
    used = {f.tool for f in before} | {t for f in before for t in f.also}
    tools = sorted(used - {"osv-scanner", "tsc"} or {"semgrep", "gitleaks"})   # tsc needs the whole project
    report = run_scanners({rel_path: new_code}, scanners=tools, timeout=timeout)
    ran = [t for t, info in report.tools.items() if info["status"] == "ok"]
    if not ran:
        reasons = "; ".join(f"{t}: {i['status']}" for t, i in report.tools.items())
        return {"status": "unavailable", "detail": reasons, "tools": report.tools}
    # only compare tools that actually ran, otherwise a crashed scanner would look like "everything fixed"
    before_ran = [f for f in before if f.tool in ran]
    must_ran = None if must_fix is None else [f for f in must_fix if f.tool in ran]
    result = compare_findings(before_ran, [f for f in report.findings if f.file == rel_path], must_ran)
    result["suppressions_added"] = added_suppressions(original_code, new_code)
    if result["suppressions_added"]:
        result["passed"] = False
    result.update(status="ok", tools=report.tools,
                  unchecked=[f for f in (must_fix if must_fix is not None else default_must_fix(before)) if f.tool not in ran])
    return result


def recheck_summary(check: dict) -> str:
    if check.get("status") != "ok":
        return f"Scanner re-check skipped ({check.get('detail', 'no scanner available')})"
    parts = [f"{len(check['resolved'])} resolved"]
    if check["must_fix_count"]:
        parts.append(f"{len(check['remaining'])}/{check['must_fix_count']} must-fix remaining")
    parts.append(f"{len(check['introduced'])} new")
    if check.get("suppressions_added"):
        parts.append(f"{check['suppressions_added']} suppression comment(s) added")
    return ", ".join(parts)


def recheck_feedback(check: dict) -> str:
    """Instructions for the Coder when the re-check fails."""
    lines = ["The security scanners re-ran on your version and it is NOT accepted yet."]
    if check["remaining"]:
        lines.append("These findings are still present and MUST be fixed:")
        for f in check["remaining"]:
            lines.append(f"- {f.tool}:{f.rule_id} ({', '.join(f.cwe) or f.category}) originally at line {f.line}: "
                         f"{f.message[:200]}" + (f" Hint: {f.fix_hint[:160]}" if f.fix_hint else ""))
    if check["blocking_introduced"]:
        lines.append("Your change INTRODUCED these new findings; remove them:")
        for f in check["blocking_introduced"]:
            lines.append(f"- {f.tool}:{f.rule_id} at line {f.line}: {f.message[:200]}"
                         + (f" Code: {f.snippet[:160]}" if f.snippet else ""))
    if check.get("suppressions_added"):
        lines.append(f"You added {check['suppressions_added']} scanner suppression comment(s). Remove them.")
    lines.append("Do not silence scanners with suppression comments (nosemgrep, # nosec, gitleaks:allow); fix the code.")
    return "\n".join(lines)
