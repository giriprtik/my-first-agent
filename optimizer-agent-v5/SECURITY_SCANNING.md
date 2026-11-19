# Security scanning

The optimizer runs three rule-based scanners before the LLM auditor sees a file. The scanners find the issues and the
auditor triages them: it marks each finding Confirmed, False positive or Needs context, then looks for what the
scanners missed (logic, authorization, second-order flows).

| Scanner | Runs on | Finds | Install |
|---|---|---|---|
| Semgrep | all | Injection, XSS, deserialization, weak crypto, risky APIs | `pip install -r tools/requirements-scanners.txt` |
| gitleaks | all | Hard-coded keys, tokens, passwords, private keys | `brew install gitleaks` or GitHub release |
| OSV-Scanner | manifests | Known CVEs in dependencies | `brew install osv-scanner` or GitHub release |
| Bandit | Python | Security anti-patterns (shell, pickle, yaml.load, weak crypto, temp files) | requirements-scanners.txt |
| Ruff | Python | Bugs and non-idiomatic code: undefined names, bugbear traps, outdated syntax | requirements-scanners.txt |
| mypy | Python | Type errors: wrong attributes, arguments, return types | requirements-scanners.txt |
| PMD 7 | Java | Error-prone code, resource leaks, threading, security rules | `brew install pmd` or GitHub release (Java 8+) |
| ESLint 9 | JS/TS/React | XSS sinks, eval, hooks misuse, accessibility, risky TypeScript | Node.js 18+, then `cd tools/eslint && npm ci` |
| tsc | TypeScript | Type errors (projects with a tsconfig.json) | same as ESLint |

Only the tools for the languages in the code run; the others are listed as "Not needed for this code".
Categories: `security`, `secret`, `dependency` (security tools) and `quality` (Ruff, mypy, PMD, most ESLint rules, tsc).
Style-level rules (`no-var`, `prefer-const`, `eqeqeq`, `no-explicit-any`, Ruff `PLC`/`PLR`/`RUF012`) are `info`
and hidden by the default severity filter.

The scanners are optional. A missing tool shows up as "not installed" with its install command, and the rest still run.

## Where it shows up
- **Single-File Studio:** *Step 0: Scan with security tools*. This gives a findings table (severity and category
  filters, SARIF download) and *Fix a finding*, which appends the finding to the optimization goals. *Step 1* then
  becomes *Audit + triage findings*.
- **Zip workbench:** the *Security scan* panel scans the whole project. This includes manifests and lockfiles; images
  and other binaries are skipped. Each file in the tree gets a 🛡 counter coloured by its worst severity, and each
  file's audit receives that file's findings.

## Scanner re-check (Phase 2)
When a scan exists for the file, the optimizer pipeline gets a stage between *Verification* and *Reviewer*:
**Coder → Interface gate → Regression check → Verification → Scanner re-check → Reviewer**.

How it works:
- **Choose what must go.** *🎯 Must be gone after optimization* defaults to high/critical findings outside test code.
  Remove any finding the audit triaged as a false positive.
- **The Coder is told.** The must-fix findings are added to the Coder's goals.
- **Re-scan.** Each candidate that passes the tests is re-scanned with the same tools.
- **Match across versions.** Findings are matched by (tool, rule). Unchanged code is matched first, then the
  nearest remaining line, because line numbers shift when code is rewritten.
- **Send back to the Coder when:**
  - a must-fix finding is still present, or
  - the change introduced a new medium+ finding, or
  - the change added a suppression comment (`nosemgrep`, `# nosec`, `gitleaks:allow`, `NOSONAR`).

  The Coder gets the exact rules, lines and hints.
- **Reviewer sees the result.** The re-check summary is added to the Reviewer's input, and the result screen shows
  Resolved / Must-fix still present / New / Not required.
- **Failure handling:**
  - If no scanner can run, the stage is skipped rather than failed.
  - A scanner that crashes is never counted as "fixed". Its findings are listed as unchecked.
- **Zip workbench.** The re-check is only active while the file is byte-for-byte what was scanned. After accepting
  changes, re-scan the project.

## Configuration (environment variables)
| Variable | Effect |
|---|---|
| `SEMGREP_RULES` | Local rule folders separated by `:` (`;` on Windows), e.g. a checkout of `github.com/semgrep/semgrep-rules` (`…/python:…/java:…/javascript:…/typescript`). If unset, the app uses registry packs `p/python p/java p/javascript p/typescript p/react p/owasp-top-ten`, which need semgrep.dev. Narrow folders scan much faster than whole languages. |
| `SCANNER_BIN_DIR` | Extra folder(s) to look in for the scanner binaries. |
| `OSV_OFFLINE=1` | Use a local vulnerability DB. Run `osv-scanner --download-offline-databases` once first. |
| `OSV_RESOLVE=1` | Resolve transitive dependencies. This needs deps.dev; the default is direct dependencies only. |

## Phase 3 notes (per-language tools)
- **ESLint** always uses `tools/eslint/eslint.config.mjs`, not the project's own config: an uploaded .zip has no
  `node_modules`, so the project's plugins couldn't load. `security/detect-object-injection` is off, because it flags
  every `obj[key]`.
- **tsc** runs once per `tsconfig.json` (up to 4, outside `node_modules`). Because the project's dependencies are not
  installed, tsc's results need filtering:
  - Errors that only mean "package not installed" are dropped.
  - Any file that imports a missing package is skipped, because its types collapse and tsc's errors there are
    misleading. The scanner card says how many files were skipped.
  - A `tsconfig.json` that `extends` an uninstalled package is reported as an error, not as a clean pass.
- **mypy** runs with `--ignore-missing-imports --explicit-package-bases`, so two `app.py` files in different folders
  don't collide. Unparsable files, such as Python 2 code, are reported as `syntax` findings and excluded, and the rest
  is still checked.
- **Ruff** respects the project's own config (`ruff.toml`, or `[tool.ruff]` in `pyproject.toml`). Otherwise it runs
  `E4,E7,E9,F,B,UP,SIM,PERF,PIE,RUF,PL`.
- **Bandit** skips `B101` (asserts). Its severity comes from Bandit's own rating and is lowered one step when
  Bandit's confidence is low.
- **Cross-tool merge.** When two tools flag the same weakness (same CWE) on the same line, such as Semgrep and Bandit
  on `shell=True`, they become one finding. The more severe one is kept, the table's *Also flagged by* column names
  the other tool, and the re-check re-runs both.
- **Vendored code** (`vendor/`, `third_party/`, `*.min.js`, `.js` inside an `assets/` folder) is treated like test
  code: hidden by default. ESLint style findings in it are dropped.
- **Not included: SpotBugs/FindSecBugs.** They need compiled `.class` files, i.e. a Maven/Gradle build of the
  project. That's planned with the sandbox work.

## Design notes
- `scanners.py` has no Streamlit or Gemini dependency. Every tool writes SARIF 2.1, which `parse_sarif` normalises
  into a single `Finding` model with these fields: tool, rule, severity, category, file, line, CWE, fix hint, help
  URL, fingerprint and `in_test`.
- How severity is assigned:
  - SARIF `security-severity` (CVSS): ≥9 critical, ≥7 high, ≥4 medium.
  - Otherwise the SARIF level: error→high, warning→medium, note→low.
  - Leaked secrets are always high.
  - Semgrep correctness rules without a CWE are capped at low (category `quality`).
- Secrets are redacted before they reach the UI or the LLM prompt. gitleaks runs with `--redact`, and the parser
  masks the snippet again.
- Findings in test code (`test/`, `tests/`, `*.spec.ts`, `test_*.py`, `*Test.java`…) are hidden by default and
  sorted last.
- Tools run in parallel in a temp copy of the project with no shell, a timeout and their own telemetry turned off.
- Zip limits: 25 MB for text and code, plus a separate 150 MB for images, fonts and other binaries. Lockfiles are
  kept in the archive: OSV needs them, and the downloaded .zip no longer drops them (that was a bug).

## Benchmark, Phase 3 (all tools except OSV)
- **pygoat:** 581 findings in 40 s. The default view (no info, no test or vendored code) shows 556.
  - Bandit adds 130, including `yaml.load` and hard-coded passwords, which Semgrep missed.
  - Ruff adds 204 and mypy 14.
- **Juice Shop:** 1358 findings in 82 s, but only 211 in the default view.
  - ESLint's 1185 findings are mostly `no-explicit-any` (info) and vendored three.js (hidden).
  - tsc gives 36 after skipping 178 files whose npm imports are missing.

## Benchmark, Phase 1 (Nov 2026, rule folders from semgrep-rules; OSV could not reach its API from the test sandbox)
**pygoat** (`2aa4cc1`, 343 code files, 37 s): 241 findings.
- 12 of 21 labs had their core flaw flagged:
  - SQLi, command injection, eval, SSRF, XXE
  - pickle/yaml deserialization, open redirect, XSS
  - MD5 passwords, hard-coded JWT and Flask secrets
- 3 labs are dependency labs, which need OSV online.
- Missed: broken access control, insufficient logging, insecure design and a second-order SSTI (user input written
  to a template file and rendered later). These are logic and data-flow issues that need the LLM triage or a
  cross-file analysis (Phase 4).
- Noise: 58 hits of `is-function-without-parentheses`. These are Django `is_authenticated` properties; the category
  is `quality` and severity low.

**OWASP Juice Shop** (`266448a`, 965 code files, 66 s): 179 findings.
- Hit:
  - SQLi in `login.ts` and `search.ts`
  - hard-coded RSA private key, HMAC and JWT secrets
  - open redirect, directory listing
  - `eval` in `userProfile.ts` and `captcha.ts`
  - `sendFile` path traversal, DOM XSS
- Missed:
  - NoSQL `$where` injection (3 routes)
  - SSRF via `fetch(url)`
  - MD5 hashing
  - XXE and RCE hidden inside `vm.runInContext` strings
  - Angular `bypassSecurityTrust*`
- Noise: 58 of 75 gitleaks hits are JWT fixtures under `test/`. These are now hidden by default.
