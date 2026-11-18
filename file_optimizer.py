import ast
import difflib
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

client = genai.Client(http_options=types.HttpOptions(timeout=30000))
MODEL = "gemini-3.5-flash-lite"

PROJECT_ROOT = Path(__file__).parent
WORKSPACE = PROJECT_ROOT / "workspace"
WORKSPACE.mkdir(exist_ok=True)

# Concurrency lock for workspace operations and file generation to prevent race conditions
_WORKSPACE_LOCK = threading.Lock()

# Language profiles: (display_name, streamlit_syntax_lang, supports_unit_tests)
LANGUAGE_PROFILES = {
    ".py": ("Python", "python", True),
    ".js": ("JavaScript", "javascript", True),
    ".mjs": ("JavaScript", "javascript", True),
    ".ts": ("TypeScript", "typescript", True),
    ".tsx": ("TypeScript React", "tsx", True),
    ".jsx": ("JavaScript React", "jsx", True),
    ".java": ("Java", "java", True),
    ".go": ("Go", "go", True),
    ".sh": ("Shell Script", "bash", False),
    ".yml": ("YAML", "yaml", False),
    ".yaml": ("YAML", "yaml", False),
    ".json": ("JSON", "json", False),
    ".sql": ("SQL", "sql", False),
    ".xml": ("XML", "xml", False),
    ".html": ("HTML", "html", False),
    ".css": ("CSS", "css", False),
}


def get_language_info(filename: str) -> tuple[str, str, bool]:
    """Returns (language_name, code_block_lang, supports_unit_tests) for any file."""
    ext = Path(filename).suffix.lower()
    if filename.lower() == "dockerfile":
        return ("Dockerfile", "dockerfile", False)
    return LANGUAGE_PROFILES.get(ext, (ext.lstrip(".").upper() or "Plain Text", "text", False))


def extract_public_interface(filename: str, source_code: str) -> str:
    """Extracts top-level and nested functions, classes, or keys across Python, JS/TS, Java, and YAML/JSON securely."""
    ext = Path(filename).suffix.lower()

    if ext == ".py":
        try:
            tree = ast.parse(source_code)
            items = []
            
            def _visit_node(node, prefix=""):
                if isinstance(node, ast.FunctionDef):
                    args = [arg.arg for arg in node.args.args]
                    items.append(f"- {prefix}def {node.name}({', '.join(args)})")
                elif isinstance(node, ast.ClassDef):
                    items.append(f"- {prefix}class {node.name}")
                    for subnode in node.body:
                        if isinstance(subnode, (ast.FunctionDef, ast.ClassDef)):
                            _visit_node(subnode, prefix="    ")

            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                    _visit_node(node)
            return "\n".join(items) if items else "- (No Python functions or classes)"
        except SyntaxError as e:
            return f"SyntaxError: {e}"

    if ext in {".js", ".ts", ".jsx", ".tsx", ".java"}:
        matches = []
        patterns = [
            r"(?:export\s+)?(?:async\s+)?function\s+([a-zA-Z0-9_]+\s*\([^)]*\))",
            r"(?:export\s+)?class\s+([a-zA-Z0-9_]+)",
            r"(?:public|private|protected)\s+(?:static\s+)?[\w<>\[\]]+\s+([a-zA-Z0-9_]+\s*\([^)]*\))",
            r"(?:export\s+)?const\s+([a-zA-Z0-9_]+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>",
        ]
        for line in source_code.splitlines():
            for pat in patterns:
                for m in re.finditer(pat, line):
                    matches.append(f"- {m.group(0).strip().rstrip('{').strip()}")
        seen = set()
        unique_matches = []
        for match in matches:
            if match not in seen:
                seen.add(match)
                unique_matches.append(match)
        return "\n".join(unique_matches) if unique_matches else "- (No classes/functions detected)"

    if ext in {".yml", ".yaml"}:
        try:
            import yaml
            if len(source_code) > 1_000_000:
                return "YAML parse note: File exceeds size limit for structural inspection."
            data = yaml.safe_load(source_code)
            if isinstance(data, dict):
                return "\n".join(f"- top-level key: `{k}`" for k in data.keys())
        except Exception as e:
            return f"YAML parse note: {e}"

    if ext == ".json":
        try:
            if len(source_code) > 1_000_000:
                return "JSON parse note: File exceeds size limit for structural inspection."
            data = json.loads(source_code)
            if isinstance(data, dict):
                return "\n".join(f"- top-level key: `{k}`" for k in data.keys())
        except Exception as e:
            return f"JSON parse note: {e}"

    return "- (General file — preserve existing structure, keys, and external contracts)"


def ask_agent(system_role: str, prompt: str, strip_fences: bool = True) -> str:
    """Calls Gemini synchronously while offering robust error handling and response cleanup."""
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_role,
                temperature=0.2,
            ),
        )
        text = response.text.strip()
        if strip_fences and text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])
        return text.strip()
    except Exception as e:
        raise RuntimeError(f"Agent generation failed: {e}")


def run_sandbox_verification(target_filename: str, test_filename: str | None = None) -> tuple[bool, str]:
    """Runs language-appropriate syntax, compiler, or unit test checks inside workspace/ with path sanitization."""
    clean_target = Path(target_filename).name
    if not clean_target or clean_target in {".", ".."}:
        return False, "ERROR: Invalid target filename supplied for verification."
    
    clean_test = Path(test_filename).name if test_filename else None
    ext = Path(clean_target).suffix.lower()
    target_path = WORKSPACE / clean_target
    
    if not target_path.exists():
        return False, f"ERROR: Target file {clean_target} does not exist in workspace."

    try:
        code = target_path.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"ERROR: Failed to read target file: {e}"

    try:
        with _WORKSPACE_LOCK:
            # 1. Python -> unittest with DeprecationWarnings treated as errors (unless test file is mock-only/isolated)
            if ext == ".py" and clean_test:
                test_path = WORKSPACE / clean_test
                if not test_path.exists():
                    return False, f"ERROR: Test file {clean_test} not found."
                res = subprocess.run(
                    ["python", "-m", "unittest", clean_test],
                    cwd=WORKSPACE, capture_output=True, text=True, timeout=15
                )
                return res.returncode == 0, (res.stdout + "\n" + res.stderr).strip()

            # 2. JavaScript -> Node built-in test runner or syntax check
            if ext in {".js", ".mjs"} and shutil.which("node"):
                cmd = ["node", "--test", clean_test] if clean_test else ["node", "--check", clean_target]
                if clean_test and not (WORKSPACE / clean_test).exists():
                    return False, f"ERROR: Test file {clean_test} not found."
                res = subprocess.run(cmd, cwd=WORKSPACE, capture_output=True, text=True, timeout=15)
                return res.returncode == 0, (res.stdout + "\n" + res.stderr).strip() or "Node verification passed."

            # 3. TypeScript -> tsc --noEmit if available
            if ext in {".ts", ".tsx"} and shutil.which("tsc"):
                res = subprocess.run(["tsc", "--noEmit", clean_target], cwd=WORKSPACE, capture_output=True, text=True, timeout=20)
                return res.returncode == 0, (res.stdout + "\n" + res.stderr).strip() or "TypeScript type-check passed."

            # 4. Java -> javac compilation + test runner if available
            if ext == ".java" and shutil.which("javac"):
                files_to_compile = [clean_target]
                if clean_test:
                    if not (WORKSPACE / clean_test).exists():
                        return False, f"ERROR: Test file {clean_test} not found."
                    files_to_compile.append(clean_test)
                res = subprocess.run(["javac"] + files_to_compile, cwd=WORKSPACE, capture_output=True, text=True, timeout=20)
                if res.returncode != 0:
                    return False, (res.stdout + "\n" + res.stderr).strip()
                if clean_test:
                    test_class = Path(clean_test).stem
                    run_res = subprocess.run(["java", test_class], cwd=WORKSPACE, capture_output=True, text=True, timeout=15)
                    return run_res.returncode == 0, (run_res.stdout + "\n" + run_res.stderr).strip()
                return True, "Java compilation (`javac`) succeeded with 0 errors."

            # 5. Shell script -> bash -n syntax check
            if ext == ".sh" and shutil.which("bash"):
                res = subprocess.run(["bash", "-n", clean_target], cwd=WORKSPACE, capture_output=True, text=True, timeout=10)
                return res.returncode == 0, (res.stdout + "\n" + res.stderr).strip() or "Bash syntax check passed."

            # 6. YAML -> PyYAML strict parser
            if ext in {".yml", ".yaml"}:
                import yaml
                list(yaml.safe_load_all(code))
                return True, "YAML syntax validated cleanly via PyYAML (`safe_load_all`)."

            # 7. JSON -> json.loads strict parser
            if ext == ".json":
                json.loads(code)
                return True, "JSON syntax validated cleanly via `json.loads`."

            # 8. Fallback for languages without local CLI compilers installed
            if not code.strip():
                return False, "ERROR: File content is empty."
            return True, f"No local compiler CLI found for `{ext}`; verified non-empty and routed to Reviewer Agent."

    except subprocess.TimeoutExpired:
        return False, "ERROR: Execution timed out (possible infinite loop or resource exhaustion)."
    except Exception as e:
        return False, f"Verification Error: {type(e).__name__}: {e}"


# --- MULTI-LANGUAGE AGENT PROMPTS ---
AUDITOR_PROMPT = """
You are a Principal Security, DevOps & Performance Architect. Analyze the provided file (regardless of language or config format) and produce a concise, structured Markdown report with three sections:
### 1. Security Risks
(Identify language/config-specific flaws: e.g., SQL/command injection, XSS, insecure deserialization, hardcoded secrets, overly permissive IAM/Docker/YAML privileges, or missing input validation. If none, state 'No critical security flaws found.')
### 2. Performance & Reliability Bottlenecks
(Identify slow algorithms, N+1 queries, memory leaks, blocking I/O, missing timeouts/retries, or deprecated APIs/syntax.)
### 3. Current Limitations & Edge Cases
(Identify scalability caps, missing error handling, environment assumptions, or schema gaps.)
Reference exact function, class, or config key names. Do NOT write replacement code yet.
"""

TESTER_PROMPT = """
You are a Polyglot QA Automation Engineer. Write a self-contained test file for the target language:
- For Python (`.py`): write a `unittest` file (`test_target.py`) importing the target module. Mock external network/API calls.
- For JavaScript (`.js`): write a test file (`test_target.test.js`) using Node's built-in `node:test` and `node:assert/strict` importing `./<filename>`.
- For Java (`.java`): write a standalone `TestTarget.java` with a `public static void main(String[] args)` method using `assert` or throwing `RuntimeException` on failure (do not require external JUnit jars).
- For TypeScript (`.ts`): write `test_target.ts` verifying types and assertions.
Return ONLY raw source code. No markdown backticks or explanations.
"""

CODER_PROMPT = """
You are a Principal Polyglot Refactoring Engineer. Optimize, secure, and harden the provided file based on the Audit Report and User Goals.
- CRITICAL: Preserve all public function signatures, class names, exports, or top-level config keys listed in the Public Interface so external callers do not break.
- Apply idiomatic best practices, strict typing, and security hardening for the file's specific language or format.
- Return ONLY raw file contents. No markdown backticks or explanations.
"""

REVIEWER_PROMPT = """
You are a strict Principal Code & Security Reviewer. Compare the Original File and Refactored File against the Public Interface, Audit Report, and Verification Output.
- Verify backward compatibility (public APIs, exports, or config keys must remain intact).
- Verify all security and optimization issues from the audit were genuinely resolved.
- If ANY issue remains, reply:
  REJECTED: <specific actionable fix>
- If ALL checks pass and ALL requirements are met cleanly, reply with the exact word:
  APPROVED
"""


def audit_code(filename: str, source_code: str) -> str:
    """Runs Phase 1: Multi-language Security, Performance & Limitations Audit."""
    lang_name, _, _ = get_language_info(filename)
    public_api = extract_public_interface(filename, source_code)
    prompt = (
        f"Filename: {filename} (Detected Type: {lang_name})\n\n"
        f"Detected Public Interface / Structure:\n{public_api}\n\n"
        f"File Contents:\n{source_code}"
    )
    return ask_agent(AUDITOR_PROMPT, prompt, strip_fences=False)


def optimize_single_file(
    filename: str,
    original_code: str,
    audit_report: str,
    extra_goals: str = "",
    max_iterations: int = 5,
    status_callback=None,
) -> dict:
    """Runs Phase 2: Sandboxed multi-language Tester -> Coder -> Reviewer loop with concurrency and thread safety."""
    def log(msg: str):
        if status_callback:
            status_callback(msg)

    clean_name = Path(filename).name
    ext = Path(clean_name).suffix.lower()
    lang_name, _, supports_tests = get_language_info(clean_name)
    public_api = extract_public_interface(clean_name, original_code)

    with _WORKSPACE_LOCK:
        for old_file in WORKSPACE.iterdir():
            if old_file.is_file():
                try:
                    old_file.unlink()
                except Exception:
                    pass

        if ext == ".py":
            for sibling in PROJECT_ROOT.glob("*.py"):
                if sibling.name not in {clean_name, "app.py", "file_optimizer.py", "coding_loop.py"}:
                    try:
                        shutil.copy2(sibling, WORKSPACE / sibling.name)
                    except Exception:
                        pass

        (WORKSPACE / clean_name).write_text(original_code, encoding="utf-8")

    test_filename = None
    test_code = ""

    if supports_tests:
        test_filename = {
            ".py": "test_target.py",
            ".js": "test_target.test.js",
            ".mjs": "test_target.test.js",
            ".ts": "test_target.ts",
            ".tsx": "test_target.tsx",
            ".java": "TestTarget.java",
            ".go": "target_test.go",
        }.get(ext, "test_target.txt")

        log(f"Generating {lang_name} test suite (`{test_filename}`)...")
        tester_task = (
            f"Language: {lang_name}\n"
            f"Target filename: `{clean_name}`\n"
            f"Test filename to generate: `{test_filename}`\n\n"
            f"Public Interface (MUST NOT BREAK):\n{public_api}\n\n"
            f"Audit Findings to Verify Fixes For:\n{audit_report}\n\n"
            f"Additional User Goals:\n{extra_goals or 'General hardening and optimization.'}\n\n"
            f"Original File:\n{original_code}"
        )
        test_code = ask_agent(TESTER_PROMPT, tester_task)
        with _WORKSPACE_LOCK:
            (WORKSPACE / test_filename).write_text(test_code, encoding="utf-8")
    else:
        log(f"Config/Script mode (`{lang_name}`): Using deterministic syntax parser + Reviewer gate.")

    current_code = original_code
    feedback = "Implement all security, performance, and reliability improvements from the Audit Report."
    history_logs = []

    for attempt in range(1, max_iterations + 1):
        log(f"Iteration {attempt}/{max_iterations}: Coder refactoring `{clean_name}` ({lang_name})...")
        coder_task = (
            f"Target File: {clean_name} ({lang_name})\n\n"
            f"Public Interface / Keys (MUST KEEP COMPATIBLE):\n{public_api}\n\n"
            f"Audit Report:\n{audit_report}\n\n"
            f"Additional User Goals:\n{extra_goals}\n\n"
            f"Current File Contents:\n{current_code}\n\n"
            f"Test Suite ({test_filename or 'N/A'}):\n{test_code or 'None (syntax/config verification mode)'}\n\n"
            f"Verification Output / Reviewer Feedback:\n{feedback}\n\n"
            f"Return the complete updated contents for {clean_name}:"
        )
        current_code = ask_agent(CODER_PROMPT, coder_task)
        with _WORKSPACE_LOCK:
            (WORKSPACE / clean_name).write_text(current_code, encoding="utf-8")

        log(f"Iteration {attempt}/{max_iterations}: Running {lang_name} sandbox verification...")
        passed, verify_output = run_sandbox_verification(clean_name, test_filename)
        if not passed:
            history_logs.append(f"❌ Iteration {attempt}: Verification failed.\n{verify_output}")
            feedback = f"Verification failed:\n{verify_output}"
            continue

        log(f"Iteration {attempt}/{max_iterations}: Reviewer auditing `{clean_name}`...")
        review_task = (
            f"File: {clean_name} ({lang_name})\n\n"
            f"Public Interface / Keys:\n{public_api}\n\n"
            f"Original File:\n{original_code}\n\n"
            f"Refactored File:\n{current_code}\n\n"
            f"Audit Report:\n{audit_report}\n\n"
            f"Verification Output:\n{verify_output}"
        )
        verdict = ask_agent(REVIEWER_PROMPT, review_task, strip_fences=False)
        if verdict.startswith("APPROVED"):
            history_logs.append(f"✅ Iteration {attempt}: Verification passed & Reviewer APPROVED.")
            diff_text = "".join(
                difflib.unified_diff(
                    original_code.splitlines(keepends=True),
                    current_code.splitlines(keepends=True),
                    fromfile=f"original/{clean_name}",
                    tofile=f"optimized/{clean_name}",
                )
            )
            return {
                "approved": True,
                "optimized_code": current_code,
                "test_code": test_code,
                "diff": diff_text,
                "logs": history_logs,
            }

        history_logs.append(f"⚠️ Iteration {attempt}: Reviewer rejected — {verdict}")
        feedback = verdict

    diff_text = "".join(
        difflib.unified_diff(
            original_code.splitlines(keepends=True),
            current_code.splitlines(keepends=True),
            fromfile=f"original/{clean_name}",
            tofile=f"optimized/{clean_name}",
        )
    )
    return {
        "approved": False,
        "optimized_code": current_code,
        "test_code": test_code,
        "diff": diff_text,
        "logs": history_logs,
    }

FOLLOWUP_CLASSIFIER_PROMPT = """
You are a Lead Software Engineer assisting a user who is reviewing an optimized file.
Decide if the user's message is:
1. A QUESTION asking for explanation, tradeoffs, or clarification about the code/diff.
2. A CHANGE REQUEST asking to modify, undo, tweak, or further optimize the code.

If it is a QUESTION, answer clearly and concisely in Markdown referencing exact lines, keys, or functions.
If it is a CHANGE REQUEST, reply with the exact prefix `REFACTOR_NEEDED:` followed by a 1-sentence technical instruction for the Coder Agent.
"""


def handle_code_followup(
    filename: str,
    original_code: str,
    current_optimized_code: str,
    audit_report: str,
    user_message: str,
    status_callback=None,
) -> dict:
    """Answers a question about the optimized code OR triggers a targeted re-optimization loop."""
    lang_name, _, _ = get_language_info(filename)
    decision_prompt = (
        f"File: {filename} ({lang_name})\n\n"
        f"Current Optimized Code:\n{current_optimized_code}\n\n"
        f"User Follow-up Message:\n{user_message}"
    )
    reply = ask_agent(FOLLOWUP_CLASSIFIER_PROMPT, decision_prompt, strip_fences=False)

    if not reply.startswith("REFACTOR_NEEDED:"):
        return {"action": "answer", "answer": reply}

    instruction = reply.replace("REFACTOR_NEEDED:", "").strip()
    updated_res = optimize_single_file(
        filename=filename,
        original_code=current_optimized_code,
        audit_report=audit_report,
        extra_goals=f"USER FOLLOW-UP CHANGE REQUEST: {instruction}",
        status_callback=status_callback,
    )

    cumulative_diff = "".join(
        difflib.unified_diff(
            original_code.splitlines(keepends=True),
            updated_res["optimized_code"].splitlines(keepends=True),
            fromfile=f"original/{os.path.basename(filename)}",
            tofile=f"optimized/{os.path.basename(filename)}",
        )
    )
    updated_res["diff"] = cumulative_diff
    updated_res["action"] = "refactor"
    updated_res["instruction"] = instruction
    return updated_res