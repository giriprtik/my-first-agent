# Project Security, Optimization & Limitations Report

## `tools.py` [OPTIMIZED & SAVED]

### 1. Security Risks
- **Overwrite race in `save_research_note`:** the `target_path.exists()` check and `write_text()` were separate steps, so two concurrent saves of one name could both pass the check and the second silently overwrote the first. **Fixed:** exclusive create (`open(..., "x")`) makes check-and-write one atomic step. This also refuses to write through a pre-planted symlink.

### 2. Performance & Reliability Bottlenecks
- **Import-time side effect:** `SAFE_DIR.mkdir()` ran on import, so every copy of `tools.py` (the optimizer's sandbox, `workspace/`) grew a stray `agent_outputs/`. **Fixed:** the folder is created on first save.

### 3. Current Limitations & Edge Cases
- **A filename of exactly `.md` saved a file named `_md` with no extension** (reproduced). `os.path.splitext(".md")` returns `(".md", "")`. **Fixed:** an empty sanitized name falls back to `note.md`.
- **Still by design:** there is no update or append path. A repeated name is refused, so callers must pick unique names.

**Verification:** tests ran. 5 regression tests (baselined on the original) and 4 requirement tests pass, your `test_tools.py` still passes, and the original interface gate passes.


---

## `agent.py` [OPTIMIZED & SAVED]

### 1. Security Risks
- No new exposures found. The keys stay in the environment. Tool sandboxing lives in `tools.py`.

### 2. Performance & Reliability Bottlenecks
- **A telemetry failure replayed the prompt on every model** (reproduced: 1 prompt, 4 sends). Writing `agent_metrics.jsonl` sat inside the same `try` as `send_message`. So a disk error *after* a successful reply was treated as a model failure, and the prompt, with its tool calls (web searches, note saves), was re-sent to all 4 models. **Fixed:** telemetry is written after the `try` and never raises.
- **Clients were built at import time.** `genai.Client()` at import raises without a key. That made `agent.py`, `app.py` and `automate.py` impossible to import in the optimizer's own key-stripped sandbox, so it could never baseline tests for them (reproduced). **Fixed:** `gemini_client`/`tavily_client` are created on first use, and `patch.object(agent, "gemini_client")` still works.
- **`search_web` raised `KeyError`** when a Tavily result lacked `content`. **Fixed** with `.get()`.
- Telemetry lines are now tagged `"source": "chat"`, alongside the optimizer's `"source": "optimizer"`.

### 3. Current Limitations & Edge Cases
- **Unverified model name:** `MODELS` still contains `gemini-4-hydrogen`, which I could not verify, so I left the list untouched. Validate the list at startup with `client.models.list()`.
- **Private attribute:** the fallback still detects the active model through the private `chat._model`.

**Verification:** tests ran. 7 regression and 4 requirement tests pass, and the original interface gate passes.


---

## `file_optimizer.py` [OPTIMIZED & SAVED]

### 1. Security Risks
- **The reviewer verdict failed open** (reproduced):
  - `{"approved": "false"}` was **approved**, because `bool("false")` is `True`.
  - Any non-JSON reply starting with "APPROVED" was approved. That is the prompt-injection route the reviewer prompt tries to guard against.
  - A JSON list crashed the run with `AttributeError`.
  
  **Fixed:** only a JSON object with a real boolean `true` approves. Everything else is a rejection with a reason.
- **The target path was not confined.** In `run_isolated_verification`, a `target_rel_path` of `../x.py` was read from *outside* the temp run directory. **Fixed:** the target must resolve inside the run directory and exist. The run directory is also resolved once, so the checks stay consistent on macOS, where `/var` is `/private/var`.
- **Still open:** the sandbox is a temp directory with API keys removed, not isolation. Child processes can still read `$HOME` and write anywhere your user can. Use a container for code you didn't write.

### 2. Performance & Reliability Bottlenecks
- **Pacing state was raced across sessions.** Each Streamlit session is a thread, and `_last_call_ts` was shared without a lock, so concurrent sessions could burst past the 15 RPM pacing. **Fixed:** slots are reserved under a lock, and the test confirms ≥0.2 s gaps across 4 threads.
- **Misleading give-up message.** It always said "Rate limit persisted" even when the cause was a 5xx or a timeout. **Fixed:** it names the last error.

### 3. Current Limitations & Edge Cases
- **The interface gate was both too strict and too lax.**
  - Too strict: it compared signature strings, so adding even an optional keyword argument counted as a "violation". That blocked any backward-compatible improvement.
  - Too lax: it ignored module-level names, so renaming `MODELS` would pass the gate and break `app.py`.
  
  **Fixed:** the check is now call-compatibility based. Defaulted additions, new keyword-only arguments with defaults, and new `*args`/`**kwargs` are allowed. Removed, reordered or newly required parameters, a keyword parameter made positional-only, and removed public module-level names are rejected.
- **The follow-up router missed formatted markers.** A reply of `**REFACTOR_NEEDED:** …` (Markdown-wrapped, or a different case) was treated as a plain answer, so the requested change was silently skipped. **Fixed** with a tolerant marker match.
- **Blocked this run by the original gate** (both need a new optional parameter, which the original gate rejects; the improved gate allows it, so a next run can do them):
  - `handle_code_followup` does not pass `project_context_files`/`target_rel_path` to `optimize_single_file`, so follow-ups in the zip workbench verify the file without its neighbouring files.
  - The reviewer could use Gemini JSON mode (`response_mime_type="application/json"`), which needs a parameter on `ask_agent`.
- **Not enforced:** requirement tests are not yet checked to *fail* on the original. A vacuous requirement test passes trivially.

**Verification:** tests ran. 18 regression and 10 requirement tests pass, and the original interface gate passes.


---

## `project_optimizer.py` [OPTIMIZED & SAVED]

### 1. Security Risks
- **Bad uploads crashed the app.** A non-zip or corrupted upload raised `zipfile.BadZipFile`, but `app.py` only catches `ValueError`, so users saw a traceback (reproduced). **Fixed:** bad, corrupt and CRC-mismatched archives raise `ValueError` with a clear message.

### 2. Performance & Reliability Bottlenecks
- **The cross-file verdict failed open.** `{"consistent": "false"}` was reported as consistent, and a reply with `consistent: true` *plus* issues showed a green check. **Fixed:** a report is consistent only with a real `true` and an empty issue list.

### 3. Current Limitations & Edge Cases
- **Neighbour context hid large files' APIs.** Context was cut at 4,500 characters, and in this very project `optimize_single_file` starts about 26 KB into `file_optimizer.py`. So when optimizing `app.py`, the Coder never saw the function it calls. **Fixed:** truncated neighbours are led by their complete public interface.
- **Java wildcard imports were only half supported.** `import a.dto.*` was used for ordering but ignored when building neighbour context and in the consistency check. **Fixed** with one shared resolver.
- **JS imports were missed.** CommonJS `require('./x')`, `import('./x')`, re-exports and `../` paths were never detected (found when my first baseline test failed on the original). **Fixed.**
- **The report mislabelled unchanged files.** Files marked "completed without changes" were labelled `OPTIMIZED & SAVED`. **Fixed:** they're labelled `REVIEWED — NO CHANGES`.
- **Still open:**
  - The repo map shows only the first 6 symbols per file.
  - Stem matching can create false edges (two `utils.py` files).
  - The export omits `agent_outputs/` and `workspace*/`, because they're in `IGNORED_DIRS`.

**Verification:** tests ran. 11 regression and 9 requirement tests pass, and the original interface gate passes.


---

## `app.py` [OPTIMIZED & SAVED]

### 1. Security Risks
- **Promote had no undo.** "Promote & Overwrite Live" replaced the file with no way back. **Fixed:** the previous version is copied to `.backups/<name>.<timestamp>.bak` first, and the success message shows where.

### 2. Performance & Reliability Bottlenecks
- **One bad telemetry line crashed every render.** A single torn line in `agent_metrics.jsonl` raised `JSONDecodeError` on every rerun, so the whole app was down until the file was edited by hand. **Fixed:** `load_metrics()` skips bad lines.
- **Zip workbench state handling** (three bugs):
  - Uploads were identified by **filename**, so re-uploading an edited project with the same name kept the stale analysis.
  - A new project inherited the previous project's file-scope selection, which isn't among the new options. Streamlit rejects that.
  - A failed validation fell through and rendered the *previous* project.
  
  **Fixed:** uploads are identified by a content hash (`zip_fingerprint`), `clear_project_state()` resets all workbench and widget keys, and the page stops on a validation error.
- **Local file picker:** a non-UTF-8 file in the project folder crashed the picker. **Fixed:** it shows an error instead.

### 3. Current Limitations & Edge Cases
- The export zip is rebuilt on every rerun, and that cost grows with project size.
- The zip-tab fixes are covered by unit tests of the new helpers, not by an upload-driven UI test.

**Verification:** tests ran. 3 regression and 5 requirement tests pass under Streamlit's AppTest harness, and the original interface gate passes.


---

## `automate.py` [REVIEWED — NO CHANGES]

### 1. Security Risks
- None found.

### 2. Performance & Reliability Bottlenecks
- Benefits from the `agent.py` fixes: it can now be imported without keys, and a telemetry error no longer re-runs the job on 4 models.

### 3. Current Limitations & Edge Cases
- Filenames have minute resolution. Two runs within the same minute get `ACCESS DENIED` on save. Low impact; left as is.


---

## `coding_loop.py` [AUDITED ONLY]

### 1. Security Risks
- **Legacy sandbox problems.** This legacy CLI keeps every problem `file_optimizer.py` has since fixed:
  - It runs generated tests with the `python` on `PATH`.
  - Child processes inherit the full environment, including API keys.
  - It accepts any reply starting with "APPROVED".

### 2. Performance & Reliability Bottlenecks
- **Shared, stateful setup:** it uses one shared `workspace/` with no per-run isolation, builds a Gemini client at import time, and creates `workspace/` on import.

### 3. Current Limitations & Edge Cases
- **Superseded:** nothing imports it, and `optimize_single_file` replaces it. **Recommendation:** delete it rather than maintain two pipelines. Not optimized in this run.


---

## `test_tools.py` [REVIEWED — NO CHANGES]

### 1. Security Risks
- None found.

### 2. Performance & Reliability Bottlenecks
- None found.

### 3. Current Limitations & Edge Cases
- **Still passes:** it runs unchanged against the new `tools.py`. Broader coverage now lives in `tests/test_tools_regression.py` and `tests/test_tools_requirements.py`.


---
