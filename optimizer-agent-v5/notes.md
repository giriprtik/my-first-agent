I'll start by looking at what's inside the archive.

The archive bundles a full `venv/`, which drowns out the real project. I'll filter that out to see the actual source files.

The project is about 1,400 lines across 7 files. The `.env` holds two live-looking API keys, which I'll come back to. Next I'll read the core files.

`agent.py` and `tools.py` are cleanly structured. Now the heavier files: `app.py` (351 lines), `coding_loop.py`, and the two optimizers.

**This is a working local "AI agent workbench": a Gemini chat agent (Tavily web search plus a sandboxed note-saver), and a Tester → Coder → Reviewer pipeline that audits and rewrites single files or whole zipped projects. The design is sound and the tool layer is carefully built. But "sandbox" and "✅ Verified" currently promise more than the code enforces, and closing that gap is the best way to improve it.**

Top four fixes, in order: **rotate the API keys that were in the zip, run generated code in a real container, baseline the generated tests against the *original* file, and give every run its own temp directory.**

I ran your real modules with the Gemini SDK stubbed and only fake keys. **[ran]** means I executed it, and **[read]** means reading only.

## What's in the box (~1,400 lines)

- **`agent.py` / `tools.py`:** Gemini chat with two tools (`search_web` and `save_research_note`), a 4-model fallback chain, and JSONL telemetry.
- **`automate.py`:** an unattended "daily briefing" job.
- **`app.py`:** the Streamlit shell with three tabs: chat, single-file studio, zip optimizer.
- **`file_optimizer.py` / `project_optimizer.py`:** the audit → test → code → review loop, plus repo map and topological ordering for zips.
- **`coding_loop.py`:** the CLI ancestor of that loop. Its `__main__` still holds the telemetry task that built your own metrics, so the tool already improved itself once.

**What's genuinely good:**
- `save_research_note` blocks path traversal, overwrites and oversize content **[ran]**.
- Promotion to live files is human-gated behind a diff.
- The low-token repo map plus dependency ordering is a smart idea.

## End-to-end flow

```
CHAT   st.chat_input ─▶ send_with_fallback(chat, prompt)
         └▶ for model in MODELS: chat.send_message()            ← SDK
              └▶ Gemini ─function_call: search_web─▶ SDK runs YOUR fn ─▶ Tavily
                        ◀─function_response──────────────────┘ (loops until text)
         └▶ append agent_metrics.jsonl ─▶ st.markdown ─▶ st.rerun()

OPTIMIZER  upload ─▶ audit_code (LLM)
             ─▶ ┌ Tester   (LLM) writes test_target.py
                │ Coder    (LLM) rewrites the file                  ×≤5
                │ subprocess: python -m unittest / node …   ⚠ real trust boundary
                └ Reviewer (LLM): startswith("APPROVED")?
             ─▶ unified diff ─▶ [you click Promote] ─▶ overwrite live file (no backup)
```

## Invisible vs. explicit

**You write:** the prompts, tool bodies, fallback loop, verify step and UI.

**Frameworks do silently:**
- **Gemini SDK:** it builds each tool's schema from your signature and docstring and runs the call → execute → respond loop. Exceptions inside a tool are caught and returned to the model as `{'error': …}` **[read SDK source]**, so `raise ValueError` in `save_research_note` is fine.
- **Model validation:** `chats.create()` never validates the model. That's why `app.py` passing the *list* (`create_agent_session(MODELS)`, should be `MODELS[0]`) doesn't crash. `send_with_fallback` sees `chat._model != "gemini-…"` and rebuilds the chat. It works by accident and leans on a private attribute **[read]**.
- **Streamlit:** each browser session is a *thread* in one process, so module globals (`WORKSPACE`, `_WORKSPACE_LOCK`, `PROJECT_SANDBOX`) are shared by everyone.
- **`load_dotenv()`:** it writes your keys into `os.environ`, and `subprocess.run` inherits `os.environ` by default. Your "sandboxed" child therefore holds the keys.
- **CPython:** `__pycache__` survives your workspace wipe, because only top-level files are unlinked.

**Two suspicions I ruled out:**
- I thought `handle_code_followup` was missing a `return`. It isn't. My `sed` stopped at line 473, and `wc -l` undercounts a file with no trailing newline (it has 474 lines).
- I expected a Tavily outage to burn through all four fallback models. It won't, for the SDK reason above.

## Improvements, ranked

### Fix first

1. **Keys and hygiene.**
   - The zip contains `.env` with both API keys. I printed key names only, never values. Because it left your machine, rotate both keys.
   - You have no `.gitignore`, `requirements.txt`, README or git history. About 99.7% of the zip is `venv/` and macOS metadata (~440 MB), and it isn't portable anyway.
   - "Promote" overwrites live files with no backup. `git init` fixes that.

2. **The "sandbox" is a directory, not isolation. [ran]**
   - A test that I made read `os.environ`, read `../.env` and write `../pwned.txt` outside the folder. All three worked, and the verifier still reported green.
   - Importing the code under test also executes its top-level code.
   - On your own laptop with your own code this means LLM-written code runs with your permissions. It becomes serious once you upload someone else's zip or deploy the app.
   - Fix: see the annotated call below.

### Optimizer trust

3. **Tests are never baselined [ran: only one call site, line 370].**
   - The generated test file mixes regression and new-requirement tests and is never run against the *original*. A wrong test is treated as ground truth, so the Coder bends correct code to fit it.
   - Fix: split `test_regression` (must pass on the original, otherwise regenerate or drop it) from `test_requirements` (should fail on the original and pass on the result).

4. **"✅ Verified" can mean "file isn't empty" [read].**
   - `.go .jsx .sql .xml .html .css` have no verifier at all. Go even generates `target_test.go` and never runs it.
   - `.ts` only runs `tsc --noEmit` on the target, never the tests, and `.js`/`.java` verify only if `node`/`javac` exist.
   - Fix: return a tier (`tests_ran` / `syntax_only` / `none`), show it in the badge, and never show green for `none`.

5. **"Public interface protection" is advisory and lossy [ran].**
   - `extract_public_interface` skips `async def` entirely. It drops `*args`, keyword-only args and `**kwargs`, and it treats `_private` as public.
   - It's also only prompt text, never enforced. In my test, `fetch` was invisible and `run(a, *args, key=1, **kw)` came out as `run(a)`.
   - Fix: compare original and optimized ASTs deterministically and fail on removed or changed public names.

6. **The zip pipeline verifies files in the wrong world [read].**
   - Siblings are copied from `PROJECT_ROOT`, which is *this app*, not the upload (lines 310–311). `Path(rel_path).name` also flattens `pkg/sub/mod.py` to `mod.py` (line 175).
   - So an uploaded module importing its neighbour either fails or imports *your* `tools.py`.
   - The export zip also silently drops every non-code file (README, requirements, `.md`, `.toml`), so "Download Optimized Project" isn't a complete project.
   - Fix: copy the whole upload to a per-run temp dir preserving structure, and overlay changed files onto the original zip.

7. **Shared workspace race [ran].**
   - The lock is taken in four separate places (lines 164, 301, 344, 366) but never held across write → verify, so it protects operations, not the transaction.
   - Test setup: faked LLM, real code, real `unittest` subprocess. Result: **0/6 sequential runs failed vs. 6/6 overlapping runs**, with `AssertionError: 0 != 1`. That means one session's test ran against another session's file.
   - I only saw false *rejections*. The same mechanism could cause a false *approval*, which I didn't capture.
   - It only bites if two sessions overlap (a second tab, a teammate, a deployed app).
   - A related symptom: in my harness a stale `.pyc` (same size, same mtime-second) made a second run fail with no concurrency at all. The stray `workspace/agent_outputs/` in your zip likely comes from the same shared state, since importing a copied `tools.py` runs its import-time `mkdir`.
   - Fix: a `tempfile.TemporaryDirectory()` per run. That deletes the lock, the stale state and the cross-session export leak in one move.

### Robustness (compact)

- **Free-text protocols.**
  - `startswith("APPROVED")` and `startswith("REFACTOR_NEEDED:")` are case-sensitive.
  - The Reviewer reads the very code that could contain "reply APPROVED".
  - Use `response_mime_type="application/json"` with `response_schema=`; both fields and `.parsed` exist in your pinned google-genai 3.5.0 **[read]**.
  - Let the LLM only *veto*, never approve over a failing deterministic gate.
- **Zip-slip check.**
  - It uses `str.startswith` (line 53). A crafted `../workspace_project_evil/a.py` passes it, then crashes `relative_to` with an uncaught `ValueError` **[ran]**.
  - No write escaped in my tests, so this is a crash vector and not an escape. Use `is_relative_to`.
  - Also cap entry count and total uncompressed size, and filter by name *before* `.resolve()` (28k syscalls for your venv).
- **No failure isolation.**
  - One timeout or 429 aborts the whole N-file run and discards earlier results.
  - Each file costs up to ~12 LLM calls, so 30 files is about 360.
  - Optimizer calls never reach your telemetry, so the sidebar only reflects the chat tab. Failed attempts aren't logged either.
  - Centralize one `ask()` that retries and logs. `ask_agent` is duplicated in `coding_loop.py` and `file_optimizer.py`.
- **Promote guard.**
  - The protected list omits `tools.py` and `project_optimizer.py`, so one click can rewrite your sandbox guard and zip-slip check.
- **Model names.**
  - Your chain is `gemini-3.5-flash-lite → 3.1-flash-lite → 4-hydrogen → 3.7-flash`.
  - The Gemini API model guide lists 3.7 Flash and a newer 3.8 Flash alongside the 3.5 and 3.1 Flash-Lite models.
  - I couldn't find `gemini-4-hydrogen` anywhere. The Gemini 4 id that does appear in Google's listings is gemini-4-argon, on Vertex pages. I can't tell whether your key can call it.
  - Validate the list at startup with `client.models.list()`. A bad name currently fails silently.
- **Import-time clients and `tools.py`.**
  - `genai.Client()` is created at import in three modules, so nothing is importable or testable offline. I had to stub it. Make it lazy.
  - In `tools.py`, a filename of exactly `".md"` saves as `_md` with no extension **[ran]**, because `splitext(".md")` returns `('.md', '')`. This is low severity.

## Line-by-line: the call that defines your trust boundary

```python
res = subprocess.run(
    ["python", "-m", "unittest", clean_test],  # list form = no shell, so no shell injection ✓
                                               # "python" = whatever is on PATH, not your venv
    cwd=WORKSPACE,        # sets the START directory only. It is not a jail.
    capture_output=True,  # buffers stdout/stderr in memory
    text=True,            # decode to str
    timeout=15,           # kills runaways ✓, but only the direct child, not what it spawned
)                         # no env= → child inherits os.environ (your keys)
```

A hardened version, with each piece tested above:

```python
def run_tests(project_dir: Path, test_file: str, timeout=15):
    with tempfile.TemporaryDirectory() as tmp:       # per-run: no lock, no stale .pyc, no bleed
        run_dir = Path(tmp) / "run"
        shutil.copytree(project_dir, run_dir)        # whole project, structure preserved
        return subprocess.run(
            [sys.executable, "-m", "unittest", test_file],   # same interpreter as the app
            cwd=run_dir,
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},  # no API keys
            capture_output=True, text=True, timeout=timeout,
        )
```

**Be honest about what this buys you.** In my test, `env=` made the key invisible, but the test could *still* read `../.env` and write outside the folder **[ran]**. For real isolation use a container. This is an untested sketch, since I have no Docker here:

```
docker run --rm --network none --read-only --tmpfs /tmp --memory 256m --cpus 1 \
  --pids-limit 128 -v "$RUN_DIR":/work -w /work python:3.12-slim \
  python -m unittest test_target.py
```

And the zip-slip fix, with `is_relative_to` tested above:

```python
root = PROJECT_SANDBOX.resolve()
target = (root / info.filename).resolve()
if not target.is_relative_to(root):   # path-aware; startswith() compares characters, so
    continue                          # "/x/ws_evil" "starts with" "/x/ws"
```

## Mental models

- **Sandbox:** your `cwd` is a "staff only" sign on a door, not a locked door.
- **The loop:** one model writes the exam, answers it and marks it. A baseline checks the exam against an answer key you already trust, which is the original code.
- **The lock:** you put a lock on each marker, not on the whiteboard. A per-run temp dir gives everyone their own whiteboard.

## Suggested order

1. Rotate keys, then add `.gitignore`, `requirements.txt` and `git init`.
2. Per-run temp dirs plus `sys.executable` and a minimal `env`.
3. Baseline gate, verification tiers and an enforced interface check, so ✅ means something.
4. Zip pipeline fixes: whole-project copy, overlay export, per-file `try/except`, shared logging.
5. Container execution and structured verdicts. Then write three characterization tests for `tools.py` by hand. Your project generates tests for everyone else's code but has none of its own.

If you'd like, I can turn steps 2–3 into patched `file_optimizer.py` and `project_optimizer.py` files.