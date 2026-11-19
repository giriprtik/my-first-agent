import os
import shutil
import subprocess
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

def safe_write(filename: str, code: str) -> Path:
    """Writes code strictly inside the workspace/ sandbox."""
    clean_name = os.path.basename(filename)
    target = WORKSPACE / clean_name
    target.write_text(code, encoding="utf-8")
    return target

def run_sandbox_tests(test_filename: str) -> tuple[bool, str]:
    """Runs Python unittest inside workspace/ with a strict 5-second timeout."""
    clean_name = os.path.basename(test_filename)
    try:
        result = subprocess.run(
            ["python", "-m", "unittest", clean_name],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=5
        )
        passed = (result.returncode == 0)
        output = result.stdout + "\n" + result.stderr
        return passed, output.strip()
    except subprocess.TimeoutExpired:
        return False, "ERROR: Execution timed out after 5 seconds (possible infinite loop)."

def ask_agent(system_role: str, prompt: str) -> str:
    """Calls Gemini and strips markdown code fences."""
    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_role,
            temperature=0.2,
        ),
    )
    text = response.text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])
    return text.strip()

# --- 3 SPECIALIZED AGENT ROLES ---
TESTER_PROMPT = """
You are a QA Automation Engineer. Write a comprehensive Python `unittest` file named `test_target.py` that imports functions from the target module name provided.
- Test all existing functionality so nothing breaks (regression tests) PLUS all new optimization/security requirements.
- Use `tempfile` or mock directories if testing file operations so tests stay clean.
- Use `self.assertEqual` and `with self.assertRaises(...)` for clear error messages.
- Return ONLY raw Python code. No markdown backticks or explanations.
"""

CODER_PROMPT = """
You are a Senior Python Refactoring Engineer. Optimize and harden the provided source file while keeping its public function signatures 100% compatible.
- Review the `test_target.py` code and tracebacks carefully so you fix failures without breaking already-passing tests.
- Return ONLY raw Python code. No markdown backticks or explanations.
"""

REVIEWER_PROMPT = """
You are a strict Principal Code Reviewer. Audit the refactored code against the original code, requirements, and test output.
- Verify backward compatibility (existing callers must not break) and check that every new requirement is met.
- If ANY requirement is violated or existing behavior was broken, reply:
  REJECTED: <specific actionable fix>
- If ALL tests pass and ALL requirements are met, reply with the exact word:
  APPROVED
"""

def refactor_project_file(target_filename: str, requirements: str, max_iterations: int = 5):
    source_path = PROJECT_ROOT / os.path.basename(target_filename)
    if not source_path.exists():
        raise FileNotFoundError(f"Could not find {source_path}")

    module_name = source_path.stem  # e.g., 'tools' for 'tools.py'
    original_code = source_path.read_text(encoding="utf-8")
    current_code = original_code
    safe_write(source_path.name, current_code)

    print(f"[STAGING] Copied {source_path.name} into workspace/{source_path.name}")
    print(f"[TESTER] Generating regression + requirement tests for `{module_name}`...")

    tester_task = (
        f"Target module to import: `{module_name}`\n\n"
        f"Original Code ({source_path.name}):\n{original_code}\n\n"
        f"New Optimization & Security Requirements:\n{requirements}"
    )
    test_code = ask_agent(TESTER_PROMPT, tester_task)
    safe_write("test_target.py", test_code)

    feedback = "Perform the initial optimization and hardening based on the requirements."

    for attempt in range(1, max_iterations + 1):
        print(f"\n=== ITERATION {attempt}/{max_iterations} ===")

        coder_task = (
            f"Target File: {source_path.name}\n\n"
            f"Requirements:\n{requirements}\n\n"
            f"Current Code:\n{current_code}\n\n"
            f"Unit Test Suite (test_target.py):\n{test_code}\n\n"
            f"Test Results / Reviewer Feedback:\n{feedback}\n\n"
            f"Return the complete updated Python code for {source_path.name}:"
        )
        current_code = ask_agent(CODER_PROMPT, coder_task)
        safe_write(source_path.name, current_code)
        print(f"[CODER] Updated workspace/{source_path.name}")

        passed, test_output = run_sandbox_tests("test_target.py")
        print(f"[TEST RUNNER] Passed: {passed}")
        if not passed:
            print(f"[TEST DETAILS]\n{test_output}")
            feedback = f"Unit tests failed:\n{test_output}"
            continue

        review_task = (
            f"Original Code:\n{original_code}\n\n"
            f"Refactored Code:\n{current_code}\n\n"
            f"Requirements:\n{requirements}\n\n"
            f"Test Output:\n{test_output}\n\n"
            f"Verify backward compatibility and all new requirements:"
        )
        verdict = ask_agent(REVIEWER_PROMPT, review_task)
        print(f"[REVIEWER] {verdict}")

        if verdict.startswith("APPROVED"):
            print(f"\nSUCCESS! workspace/{source_path.name} passed all tests and review.")
            choice = input(f"\nPromote workspace/{source_path.name} to overwrite your live {source_path.name}? [y/N]: ").strip().lower()
            if choice == "y":
                shutil.copy2(WORKSPACE / source_path.name, source_path)
                print(f"[PROMOTED] Live {source_path.name} updated!")
            else:
                print(f"[KEPT IN SANDBOX] Review it manually at workspace/{source_path.name}")
            return

        feedback = verdict

    print("\nMAX ITERATIONS REACHED without final approval. Your live file was not touched.")

if __name__ == "__main__":
    agent_reqs = """
    Upgrade `agent.py` to add automatic latency and token-usage telemetry without breaking any existing functions:
    1. Keep the exact signatures and behavior of `search_web`, `create_agent_session`, `send_with_fallback`, and `run_agent`.
    2. Measure exact request latency in seconds using `time.perf_counter()` inside `send_with_fallback`.
    3. Safely extract `response.usage_metadata` (using `getattr(..., None)` so mocked or missing metadata never crashes) to read `prompt_token_count`, `candidates_token_count`, and `total_token_count`.
    4. Print a telemetry summary line after every successful response:
       `[METRICS] Model: <model_name> | Latency: <X.XX>s | Tokens: <total> (in: <prompt>, out: <candidates>)`
    5. Append a JSON line for every request to `agent_metrics.jsonl` (using `Path(__file__).parent / "agent_metrics.jsonl"`) containing `timestamp`, `model`, `latency_sec`, `prompt_tokens`, `candidates_tokens`, and `total_tokens`.
    6. IMPORTANT FOR TESTING: In `test_target.py`, mock `gemini_client` and `tavily_client` using `unittest.mock.patch` so unit tests never make live network/API calls.
    """
    refactor_project_file("agent.py", agent_reqs)
