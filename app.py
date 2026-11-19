import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

import streamlit as st

from agent import MODELS, cached_model_status, create_agent_session, stream_reply
from chat_store import clear_conversation, load_conversation, save_conversation
from file_optimizer import (
    audit_code,
    optimize_single_file,
    extract_public_interface,
    get_language_info,
    handle_code_followup,
)
from project_optimizer import (
    parse_zip_in_memory,
    build_repo_map_and_order,
    build_neighbor_context,
    package_overlay_zip,
    check_cross_file_consistency,
)
from tools import save_research_note
from ui_components import (
    APP_CSS,
    apply_progress,
    file_status,
    file_tree_html,
    new_stepper_state,
    side_by_side_diff_html,
    source_cards_html,
    status_legend_html,
    stepper_html,
    summarize_metrics,
)


st.set_page_config(page_title="AI Agent & Universal Code Optimizer", page_icon="🤖", layout="wide")
st.markdown(APP_CSS, unsafe_allow_html=True)

PROJECT_ROOT = Path(__file__).parent.resolve()
OUTPUTS_DIR = PROJECT_ROOT / "agent_outputs"
METRICS_FILE = PROJECT_ROOT / "agent_metrics.jsonl"
BACKUP_DIR = PROJECT_ROOT / ".backups"
CHAT_FILE = PROJECT_ROOT / ".chat_history.json"

# Everything the zip workbench keeps in session state, including widget keys whose old values
# would otherwise be offered as defaults for a different project (and crash the multiselect).
PROJECT_STATE_KEYS = (
    "last_zip_name", "last_zip_fingerprint", "proj_all_files", "proj_orig_code", "proj_working_tree",
    "proj_ordered_files", "proj_dep_map", "proj_repo_map", "proj_audits", "proj_results", "proj_completed",
    "proj_qa", "proj_learned_rules", "proj_active_file", "proj_selected_scope", "proj_scope_multiselect",
    "proj_consistency",
)

TOOL_LABELS = {
    "search_web": "🔎 Web search",
    "deep_search": "🧭 Deep search",
    "read_page": "📄 Read page",
    "save_research_note": "💾 Save note",
}


@st.cache_data(show_spinner=False)
def load_metrics(path: str | Path) -> list[dict]:
    """Reads telemetry JSONL efficiently, skipping blank, torn or non-object lines instead of crashing."""
    p = Path(path)
    if not p.exists():
        return []
    entries = []
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


@st.cache_data(show_spinner=False)
def zip_fingerprint(data: bytes) -> str:
    """Identifies an upload by content, so re-uploading an edited project with the same name is re-parsed."""
    return hashlib.sha256(data).hexdigest()


def clear_project_state(state) -> None:
    """Drops all zip-workbench state (works on st.session_state or a plain dict)."""
    for key in PROJECT_STATE_KEYS:
        state.pop(key, None)


def backup_file(path: Path, backup_dir: Path = BACKUP_DIR) -> Path:
    """Copies a live file to backup_dir/<name>.<timestamp>.bak before it is overwritten; returns the copy."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    target = backup_dir / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, target)
    return target


def format_tier_badge(approved: bool | None, tier: str) -> str:
    """Returns an honest status label reflecting the actual verification depth."""
    if approved is None:
        return "🔍 Audited Only"
    if not approved:
        return f"⚠️ Needs Review ({tier})"
    if tier == "tests_ran":
        return "✅ Verified (Tests Ran & Approved)"
    if tier == "syntax_only":
        return "✅ Syntax Checked & Approved"
    return "🟡 Approved (Unverified Format — Review Diff)"


def describe_tool_call(name: str, args: dict) -> str:
    """One readable line for a tool step, e.g. '🔎 Web search: kathmandu weather'."""
    label = TOOL_LABELS.get(name, f"🔧 {name}")
    detail = args.get("query") or args.get("url") or args.get("filename") or ""
    return f"{label}: {detail}" if detail else label


def html_block(markup: str) -> None:
    """Renders trusted, already-escaped HTML from ui_components with st.html, which bypasses Markdown
    (Markdown would turn `_names_` inside code into italics and corrupt the diff)."""
    if markup:
        st.html(markup)


def render_results_tabs(res: dict, original: str, syntax_lang: str, lang_name: str, label: str) -> None:
    tab_side, tab_diff, tab_code, tab_tests = st.tabs(
        ["↔️ Side-by-side", "🔀 Unified Diff", "✅ Optimized File", "🧪 Generated Tests"]
    )
    with tab_side:
        html_block(side_by_side_diff_html(original, res["optimized_code"], f"Original {label}", f"Optimized {label}"))
    with tab_diff:
        st.code(res["diff"] or "No changes made.", language="diff")
    with tab_code:
        st.code(res["optimized_code"], language=syntax_lang)
    with tab_tests:
        if res["test_code"]:
            st.code(res["test_code"], language=syntax_lang)
        else:
            st.info(f"Verification Tier `{res.get('verification_tier', 'none')}` (`{lang_name}`): no standalone unit tests.")


def run_with_stepper(run, title: str) -> dict:
    """Runs an optimizer call with a live pipeline stepper plus a collapsible log; returns its result."""
    stepper = st.empty()
    state = new_stepper_state()
    html_block_into(stepper, stepper_html(state))
    with st.status(title, expanded=False) as log_box:
        def on_progress(stage, status, detail):
            apply_progress(state, stage, status, detail)
            html_block_into(stepper, stepper_html(state))

        result = run(lambda msg: log_box.write(msg), on_progress)
        if result.get("action") == "answer":
            log_box.update(label="✅ Answered", state="complete")
        else:
            log_box.update(
                label=format_tier_badge(result["approved"], result.get("verification_tier", "none")),
                state="complete" if result["approved"] else "error",
            )
    result["stepper"] = state
    return result


def html_block_into(placeholder, markup: str) -> None:
    placeholder.html(markup)


def persist_chat() -> None:
    try:
        save_conversation(CHAT_FILE, st.session_state.messages, st.session_state.chat.get_history())
    except Exception as e:  # persistence is a convenience; never break the chat over it
        st.toast(f"Could not save chat history: {e}", icon="⚠️")


# --- SIDEBAR: TELEMETRY & SAVED NOTES ---
with st.sidebar:
    st.header("📊 Live Telemetry")
    if METRICS_FILE.exists():
        lines = load_metrics(METRICS_FILE)
        if lines:
            summary = summarize_metrics(lines)
            col1, col2 = st.columns(2)
            col1.metric("Requests", summary["requests"])
            col2.metric("Avg Latency", f"{summary['avg_latency']:.2f}s")
            st.metric("Total Tokens", f"{summary['total_tokens']:,}")

            by_source = summary["by_source"]
            split = " · ".join(f"{name} {count}" for name, count in sorted(by_source.items()))
            tools_txt = f" · tools {summary['tool_calls']}"
            if summary["tool_failures"]:
                tools_txt += f" ({summary['tool_failures']} failed)"
            st.caption(f"{split}{tools_txt if summary['tool_calls'] else ''}")

            if summary["errors_by_model"]:
                with st.expander(f"⚠️ Errors by model ({sum(summary['errors_by_model'].values())})"):
                    for model, count in sorted(summary["errors_by_model"].items(), key=lambda kv: -kv[1]):
                        st.markdown(f"- `{model}`: {count}")
                    st.caption(f"Latest: {summary['last_error'][:160]}")

            if len(summary["token_series"]) >= 2:
                with st.expander("📈 Tokens over time"):
                    import pandas as pd  # bundled with Streamlit

                    df = pd.DataFrame(summary["token_series"])
                    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
                    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
                    df["cumulative"] = df.groupby("source")["tokens"].cumsum()
                    chart = df.pivot_table(index="timestamp", columns="source", values="cumulative", aggfunc="last").ffill()
                    st.line_chart(chart, height=180)
        else:
            st.caption("No metrics logged yet.")
    else:
        st.caption("No metrics logged yet.")

    status = cached_model_status()
    if status["unavailable"]:
        st.caption("⏭️ Skipped models the API doesn't serve: " + ", ".join(status["unavailable"]))

    st.divider()
    st.header("📁 Sandboxed Notes")
    OUTPUTS_DIR.mkdir(exist_ok=True)
    saved_files = sorted(OUTPUTS_DIR.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True)
    if saved_files:
        selected_note = st.selectbox("Preview note:", [f.name for f in saved_files])
        if selected_note:
            st.info((OUTPUTS_DIR / selected_note).read_text(encoding="utf-8"))
    else:
        st.caption("No saved notes yet.")

    if st.button("🗑️ Clear Chat History", use_container_width=True):
        clear_conversation(CHAT_FILE)
        st.session_state.messages = []
        st.session_state.pending_saves = []
        st.session_state.chat = create_agent_session(MODELS[0], manual_tools=True)
        st.rerun()

# --- TOP-LEVEL TABS ---
tab_chat, tab_optimizer, tab_project = st.tabs([
    "💬 Research & File Agent",
    "🛠️ Single-File Studio",
    "🗂️ Full Project (.zip) Optimizer",
])

# ================= TAB 1: RESEARCH AGENT =================
with tab_chat:
    st.title("🤖 Research & Sandboxed File Agent")
    st.caption("Searches the web, reads sources and cites them. Notes are saved only after you confirm.")

    if "messages" not in st.session_state:
        restored_messages, restored_history = load_conversation(CHAT_FILE)
        st.session_state.messages = restored_messages
        st.session_state.restored_history = restored_history
    if "chat" not in st.session_state:
        st.session_state.chat = create_agent_session(
            MODELS[0], history=st.session_state.pop("restored_history", None) or None, manual_tools=True
        )
    st.session_state.setdefault("pending_saves", [])

    if not st.session_state.messages:
        st.info("Try: *“What changed in Nepal's budget this year? Read the best source and cite it.”* "
                "or *“Save a note summarising that.”*")

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            steps = msg.get("tools") or []
            if steps:
                with st.expander(f"🔧 {len(steps)} tool step{'s' if len(steps) != 1 else ''}"
                                 + (f" · {msg['model']}" if msg.get("model") else "")):
                    for step in steps:
                        st.markdown(f"- {step}")
            st.markdown(msg["content"])
            if msg.get("sources"):
                html_block(source_cards_html(msg["sources"]))

    # Notes the agent prepared: nothing is written until the user confirms
    for idx, pending in enumerate(list(st.session_state.pending_saves)):
        with st.container(border=True):
            st.markdown(f"**💾 Save note `{pending['filename']}`?** The agent prepared this note; review it first.")
            with st.expander("Preview", expanded=False):
                st.markdown(pending["content"])
            col_yes, col_no = st.columns(2)
            if col_yes.button("Save note", key=f"save_note_{idx}", type="primary", use_container_width=True):
                try:
                    outcome = save_research_note(pending["filename"], pending["content"])
                except ValueError as e:
                    outcome = f"Could not save: {e}"
                icon = "✅" if outcome.startswith("Successfully") else "⚠️"
                st.session_state.messages.append({"role": "assistant", "content": f"{icon} {outcome}"})
                st.session_state.pending_saves.pop(idx)
                persist_chat()
                st.rerun()
            if col_no.button("Discard", key=f"discard_note_{idx}", use_container_width=True):
                st.session_state.pending_saves.pop(idx)
                st.session_state.messages.append(
                    {"role": "assistant", "content": f"🗑️ Discarded the note `{pending['filename']}`."}
                )
                persist_chat()
                st.rerun()

    if prompt := st.chat_input("Ask a question or say 'Save a note about...'"):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            progress_box = st.status("Thinking…", expanded=False)
            collected = {"steps": [], "done": None}

            def answer_deltas():
                for event in stream_reply(st.session_state.chat, prompt, confirm_saves=True):
                    kind = event["type"]
                    if kind == "text":
                        yield event["delta"]
                    elif kind == "model":
                        progress_box.update(label=f"Answering with `{event['model']}`…")
                    elif kind == "tool_call":
                        line = describe_tool_call(event["name"], event["args"])
                        collected["steps"].append(line)
                        progress_box.update(label=line, expanded=True)
                        progress_box.write(line)
                    elif kind == "tool_result":
                        progress_box.write(("　✓ " if event["ok"] else "　✕ ") + event["summary"])
                    elif kind == "tool_skipped":
                        collected["steps"].append(f"⏭️ Skipped {event['name']} (tool budget reached)")
                        progress_box.write(f"⏭️ Skipped {event['name']}: tool budget reached")
                    elif kind == "done":
                        collected["done"] = event

            try:
                streamed = st.write_stream(answer_deltas())
            except Exception as e:
                progress_box.update(label="Failed", state="error")
                st.error(f"Error: {e}")
                st.stop()

            done = collected["done"] or {}
            reply = done.get("text") or (streamed if isinstance(streamed, str) else "") or "_(No answer returned.)_"
            st.session_state.chat = done.get("chat", st.session_state.chat)
            n_steps = len(collected["steps"])
            progress_box.update(
                label=f"{done.get('model', 'model')} · {n_steps} tool step{'s' if n_steps != 1 else ''}",
                state="complete",
                expanded=False,
            )
            st.session_state.messages.append({
                "role": "assistant",
                "content": reply,
                "sources": done.get("sources", []),
                "tools": collected["steps"],
                "model": done.get("model"),
            })
            st.session_state.pending_saves.extend(done.get("pending_saves", []))
            persist_chat()
            st.rerun()

# ================= TAB 2: UNIVERSAL FILE AUDITOR & OPTIMIZER =================
with tab_optimizer:
    st.title("🛠️ Universal File Security, Optimization & Limitations Studio")
    st.caption("Supports Python, JavaScript, TypeScript, Java, Go, YAML, JSON, SQL, Shell, Dockerfiles, and more.")

    source_mode = st.radio(
        "Choose input method:",
        ["Upload any code/config file", "Select a file from project folder"],
        horizontal=True,
    )

    target_filename = None
    source_code = None

    if source_mode == "Upload any code/config file":
        uploaded = st.file_uploader(
            "Upload any source code or config file (.py, .js, .ts, .java, .yml, .json, .sql, .sh, Dockerfile, etc.)",
            type=None,
        )
        if uploaded:
            target_filename = uploaded.name
            try:
                source_code = uploaded.read().decode("utf-8")
            except UnicodeDecodeError:
                st.error("Binary files are not supported. Please upload a UTF-8 text, code, or config file.")
    else:
        ignored = {
            "app.py",
            "file_optimizer.py",
            "project_optimizer.py",
            "tools.py",
            "coding_loop.py",
            "ui_components.py",
            "chat_store.py",
            ".env",
            "id_rsa",
            "agent_metrics.jsonl",
        }
        local_files = sorted(
            [
                f.name
                for f in PROJECT_ROOT.iterdir()
                if f.is_file() and not f.name.startswith(".") and f.name not in ignored and f.suffix != ".jsonl" and not f.name.endswith((".json", ".key", ".pem"))
            ]
        )
        if local_files:
            target_filename = st.selectbox("Target project file:", local_files)
            try:
                resolved_target_path = (PROJECT_ROOT / target_filename).resolve()
                if not resolved_target_path.is_relative_to(PROJECT_ROOT.resolve()) or any(part.startswith(".") for part in resolved_target_path.parts):
                    st.error("Access denied: Invalid file path.")
                else:
                    source_code = resolved_target_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                st.error(f"`{target_filename}` is not a UTF-8 text file and cannot be optimized.")
            except Exception as exc:
                st.error(f"Could not read file: {exc}")
        else:
            st.warning("No target files found in project root.")

    if target_filename and source_code:
        lang_name, syntax_lang, _ = get_language_info(target_filename)

        with st.expander(f"📄 View Original `{target_filename}` ({lang_name}) & Public Structure", expanded=False):
            st.markdown("**Detected Public Interface / Keys (Protected from Breaking Changes):**")
            st.code(extract_public_interface(target_filename, source_code), language="text")
            st.code(source_code, language=syntax_lang)

        extra_goals = st.text_input(
            "Optional custom optimization goals:",
            placeholder="Leave blank for full default security & performance optimization, or add custom instructions...",
        )

        col_audit, col_opt = st.columns(2)

        with col_audit:
            if st.button("🔍 Step 1: Audit Security & Limitations", use_container_width=True):
                with st.spinner(f"Auditing `{target_filename}` ({lang_name})..."):
                    st.session_state.audit_report = audit_code(target_filename, source_code)
                    st.session_state.audited_file = target_filename
                    st.session_state.opt_result = None
                    st.session_state.code_qa_history = []

        with col_opt:
            audit_ready = (
                st.session_state.get("audited_file") == target_filename
                and st.session_state.get("audit_report")
            )
            run_clicked = st.button("⚡ Step 2: Optimize & Verify in Sandbox", disabled=not audit_ready, use_container_width=True)

        if run_clicked:
            st.session_state.opt_result = run_with_stepper(
                lambda log, progress: optimize_single_file(
                    filename=target_filename,
                    original_code=source_code,
                    audit_report=st.session_state.audit_report,
                    extra_goals=extra_goals,
                    status_callback=log,
                    progress_callback=progress,
                ),
                f"Running {lang_name} Tester → Coder → Reviewer loop (details)",
            )
            st.session_state.code_qa_history = []
            st.rerun()

        if st.session_state.get("audited_file") == target_filename and st.session_state.get("audit_report"):
            st.divider()
            with st.expander(f"📋 Audit & Limitations Report: `{target_filename}`", expanded=not st.session_state.get("opt_result")):
                st.markdown(st.session_state.audit_report)

        if st.session_state.get("opt_result"):
            res = st.session_state.opt_result
            tier = res.get("verification_tier", "none")
            st.divider()
            st.subheader(f"🧪 Optimization Results — {format_tier_badge(res['approved'], tier)}")
            if res.get("stepper"):
                html_block(stepper_html(res["stepper"]))

            with st.expander(f"Iteration log ({len(res['logs'])})", expanded=not res["approved"]):
                for entry in res["logs"]:
                    st.write(entry)

            render_results_tabs(res, source_code, syntax_lang, lang_name, target_filename)

            col_dl, col_promote = st.columns(2)
            with col_dl:
                st.download_button(
                    label=f"⬇️ Download Optimized `{target_filename}`",
                    data=res["optimized_code"],
                    file_name=target_filename,
                    mime="text/plain",
                    use_container_width=True,
                )
            with col_promote:
                if source_mode == "Select a file from project folder" and res["approved"]:
                    if st.button(f"🚀 Promote & Overwrite Live `{target_filename}`", type="primary", use_container_width=True):
                        safe_live_path = (PROJECT_ROOT / target_filename).resolve()
                        if not safe_live_path.is_relative_to(PROJECT_ROOT.resolve()):
                            st.error("Access denied: Target path traversal detected.")
                        else:
                            backup_path = backup_file(safe_live_path)
                            safe_live_path.write_text(res["optimized_code"], encoding="utf-8")
                            st.success(
                                f"Live `{target_filename}` updated! Previous version saved to "
                                f"`{backup_path.relative_to(PROJECT_ROOT)}`. Reloading..."
                            )
                            st.rerun()

            st.divider()
            st.subheader("💬 Ask About the Optimized Code or Request Changes")
            st.caption("Ask why a change was made, or instruct the agent to modify and re-verify the file.")

            for qa in st.session_state.get("code_qa_history", []):
                with st.chat_message(qa["role"]):
                    st.markdown(qa["content"])

            with st.form(key="code_followup_form", clear_on_submit=True):
                followup_input = st.text_input(
                    "Your question or change request:",
                    placeholder="e.g., 'Why did you change port 8080?' or 'Keep the original timeout value of 30s and redo it'",
                )
                submitted = st.form_submit_button("Send to Code Agent", use_container_width=True)

            if submitted and followup_input.strip():
                st.session_state.code_qa_history.append({"role": "user", "content": followup_input})
                outcome = run_with_stepper(
                    lambda log, progress: handle_code_followup(
                        filename=target_filename,
                        original_code=source_code,
                        current_optimized_code=res["optimized_code"],
                        audit_report=st.session_state.audit_report,
                        user_message=followup_input,
                        status_callback=log,
                        progress_callback=progress,
                    ),
                    "Analyzing follow-up request (details)",
                )
                if outcome["action"] == "answer":
                    st.session_state.code_qa_history.append({"role": "assistant", "content": outcome["answer"]})
                else:
                    st.session_state.opt_result = outcome
                    new_badge = format_tier_badge(outcome["approved"], outcome.get("verification_tier", "none"))
                    msg = (
                        f"🔄 **Re-optimized `{target_filename}`** based on: *{outcome['instruction']}*\n\n"
                        f"Status: {new_badge}. Check the diff tabs above!"
                    )
                    st.session_state.code_qa_history.append({"role": "assistant", "content": msg})
                st.rerun()

# ================= TAB 3: GUIDED PROJECT (.ZIP) WORKBENCH =================
with tab_project:
    st.title("🗂️ Guided Project (.zip) Step-by-Step Workbench")
    st.caption(
        "Upload a project .zip → Automatically orders files by dependency (DTOs/utilities first) → "
        "Work through one file at a time with full imported-file context, diff inspection, and Q&A."
    )

    uploaded_zip = st.file_uploader("Upload a zipped project folder (.zip)", type=["zip"])

    if uploaded_zip is not None:
        zip_bytes_in = uploaded_zip.getvalue()
        fingerprint = zip_fingerprint(zip_bytes_in)
        if st.session_state.get("last_zip_fingerprint") != fingerprint:
            # A different upload (even one with the same filename): start from a clean slate
            clear_project_state(st.session_state)
            try:
                all_files, code_files = parse_zip_in_memory(zip_bytes_in)
                repo_map, ordered_files, dep_map = build_repo_map_and_order(code_files)
                st.session_state.last_zip_fingerprint = fingerprint
                st.session_state.last_zip_name = uploaded_zip.name
                st.session_state.proj_all_files = all_files
                st.session_state.proj_orig_code = dict(code_files)
                st.session_state.proj_working_tree = dict(code_files)
                st.session_state.proj_ordered_files = ordered_files
                st.session_state.proj_dep_map = dep_map
                st.session_state.proj_repo_map = repo_map
                st.session_state.proj_audits = {}
                st.session_state.proj_results = {}
                st.session_state.proj_completed = set()
                st.session_state.proj_qa = {}
                st.session_state.proj_learned_rules = []
                st.session_state.proj_active_file = ordered_files[0] if ordered_files else None
            except ValueError as e:
                st.error(f"Zip validation failed: {e}")
                st.stop()  # don't fall through and render the previous project's state

        all_ordered_files = st.session_state.get("proj_ordered_files", [])
        if not all_ordered_files:
            st.warning("No supported code or config files found inside this .zip archive.")
        else:
            # --- FILE SCOPE FILTER (UNSELECT FILES YOU WANT TO SKIP) ---
            with st.expander(f"🎯 Filter Active Files ({len(all_ordered_files)} detected in archive)", expanded=False):
                st.caption(
                    "Unselect files you want to skip. Skipped files remain in the Repository Map for cross-file context and stay untouched in the final .zip."
                )
                active_scope = st.multiselect(
                    "Files included in your optimization queue:",
                    options=all_ordered_files,
                    default=st.session_state.get("proj_selected_scope", all_ordered_files),
                    key="proj_scope_multiselect",
                )
                st.session_state.proj_selected_scope = active_scope

            ordered_files = st.session_state.get("proj_selected_scope", all_ordered_files)

            if not ordered_files:
                st.info("No files selected in your active queue. Re-select at least one file above to begin.")
                st.stop()

            completed_set = st.session_state.proj_completed
            audits_map = st.session_state.proj_audits
            dep_map = st.session_state.proj_dep_map
            working_tree = st.session_state.proj_working_tree
            results_map = st.session_state.proj_results
            orig_tree = st.session_state.proj_orig_code

            # Count progress strictly among your selected files
            completed_in_scope = [f for f in ordered_files if f in completed_set]
            remaining = [f for f in ordered_files if f not in completed_set]
            recommended_next = remaining[0] if remaining else None

            # Top progress & export bar
            col_prog, col_zip_dl = st.columns([2, 1])
            with col_prog:
                st.progress(len(completed_in_scope) / len(ordered_files))
                if recommended_next:
                    st.info(
                        f"👉 **Recommended Next File:** `{recommended_next}` "
                        f"({len(completed_in_scope)}/{len(ordered_files)} selected files completed)"
                    )
                else:
                    st.success(f"🎉 All {len(ordered_files)} selected files completed!")

            with col_zip_dl:
                zip_bytes = package_overlay_zip(
                    st.session_state.proj_all_files,
                    working_tree,
                    audits_map,
                    completed_set,
                )
                st.download_button(
                    label=f"⬇️ Download Project .zip ({len(completed_set)} updated)",
                    data=zip_bytes,
                    file_name=f"optimized_{uploaded_zip.name}",
                    mime="application/zip",
                    type="primary",
                    use_container_width=True,
                )

            # --- FILE TREE COLOURED BY STATUS, IN DEPENDENCY ORDER ---
            statuses = {
                path: file_status(path, completed_set, audits_map, results_map, working_tree, orig_tree)
                for path in ordered_files
            }
            with st.expander("🗂️ Project files by status (numbered in dependency order)", expanded=True):
                html_block(status_legend_html(Counter(statuses.values())))
                html_block(file_tree_html(ordered_files, statuses, dep_map, st.session_state.get("proj_active_file")))

            with st.expander("🗺️ View Compact Repository Map & Dependency Order", expanded=False):
                st.code(st.session_state.proj_repo_map, language="markdown")

            # --- PERSISTENT SESSION MEMORY (LEARNED FROM FOLLOW-UPS) ---
            learned_rules = st.session_state.setdefault("proj_learned_rules", [])
            with st.expander(f"🧠 Session Memory & Learned Rules ({len(learned_rules)} active)", expanded=False):
                st.caption("Corrections you make in follow-ups are automatically saved here and enforced on all future files.")
                rules_text = st.text_area(
                    "Active rules (one per line):",
                    value="\n".join(learned_rules),
                    height=110,
                    key="session_rules_editor",
                )
                if st.button("💾 Save Rules Update", key="save_session_rules"):
                    st.session_state.proj_learned_rules = [
                        line.strip().lstrip("- ").strip()
                        for line in rules_text.splitlines()
                        if line.strip()
                    ]
                    st.success("Session rules updated for all upcoming files!")

            # --- VISUAL CROSS-FILE CONSISTENCY CHECK ---
            col_chk_btn, col_chk_info = st.columns([1, 2])
            with col_chk_btn:
                if st.button(
                    f"🔗 Run Cross-File Consistency Check ({len(completed_set)} saved)",
                    disabled=len(completed_set) == 0,
                    use_container_width=True,
                ):
                    with st.spinner("Auditing cross-file method calls, getters, and repository contracts..."):
                        st.session_state.proj_consistency = check_cross_file_consistency(
                            working_tree=working_tree,
                            completed_files=completed_set,
                            repo_map=st.session_state.proj_repo_map,
                        )
            with col_chk_info:
                st.caption(
                    "Verifies that every method call, DTO getter, and repository query in your saved files actually exists in its imported target class."
                )

            if st.session_state.get("proj_consistency"):
                cons = st.session_state.proj_consistency
                pairs = cons.get("checked_pairs", [])

                with st.container(border=True):
                    st.subheader("🧭 Cross-File Dependency & Contract Report")
                    if pairs:
                        st.markdown(
                            "**Verified Dependency Links:** "
                            + " • ".join(f"`{Path(a).name}` ➔ `{Path(b).name}`" for a, b in pairs)
                        )

                    if cons["consistent"] and not cons["issues"]:
                        st.success("✅ All cross-file method calls, getters, and repository contracts match!")
                    else:
                        st.error(f"⚠️ Found {len(cons['issues'])} cross-file contract mismatch(es):")
                        for idx_i, issue in enumerate(cons["issues"], start=1):
                            caller = issue.get("caller_file", "Unknown")
                            target = issue.get("target_file", "Unknown")
                            sym = issue.get("symbol", "unknown")
                            desc = issue.get("description", "")
                            fix = issue.get("suggested_fix", "")

                            st.markdown(
                                f"**{idx_i}. `{Path(caller).name}` ➔ `{Path(target).name}` (`{sym}`)**\n"
                                f"- **Mismatch:** {desc}\n"
                                f"- **Suggested Fix:** {fix}"
                            )
                            if caller in ordered_files:
                                if st.button(f"🛠️ Jump to `{Path(caller).name}` to Fix", key=f"jump_fix_{idx_i}_{caller}"):
                                    st.session_state.proj_active_file = caller
                                    st.rerun()

            # File selector with status badges
            status_icons = {"optimized": "✅", "reviewed": "☑️", "ready": "🟢", "needs_review": "⚠️", "audited": "🔍", "pending": "⬜"}

            def _format_file_option(path_str: str) -> str:
                position = ordered_files.index(path_str) + 1
                marker = " 👉 next" if path_str == recommended_next else ""
                return f"{status_icons[statuses[path_str]]} #{position} {path_str}{marker}"

            default_idx = (
                ordered_files.index(st.session_state.proj_active_file)
                if st.session_state.get("proj_active_file") in ordered_files
                else 0
            )

            active_file = st.selectbox(
                "Select file to inspect & optimize (ordered from dependencies → dependents):",
                options=ordered_files,
                index=default_idx,
                format_func=_format_file_option,
            )
            st.session_state.proj_active_file = active_file

            # Show which local files this file imports
            file_deps = dep_map.get(active_file, [])
            if file_deps:
                unoptimized_deps = [d for d in file_deps if d not in completed_set]
                if unoptimized_deps:
                    st.warning(
                        f"⚠️ `{active_file}` imports: {', '.join(f'`{d}`' for d in file_deps)}. "
                        f"Tip: Consider completing {', '.join(f'`{d}`' for d in unoptimized_deps)} first so this file sees their updated signatures!"
                    )
                else:
                    st.caption(f"🔗 Imported context automatically loaded from: {', '.join(f'`{d}`' for d in file_deps)}")

            orig_code = orig_tree[active_file]
            curr_code = working_tree[active_file]
            lang_name, syntax_lang, _ = get_language_info(active_file)

            with st.expander(f"📄 View Current Source: `{active_file}` ({lang_name})", expanded=False):
                st.code(curr_code, language=syntax_lang)

            file_goals = st.text_input(
                f"Optional custom goals for `{active_file}`:",
                key=f"goals_{active_file}",
                placeholder="Leave blank for default security & performance optimization, or add specific instructions...",
            )

            col_b1, col_b2 = st.columns(2)
            with col_b1:
                if st.button(f"🔍 Step 1: Audit `{Path(active_file).name}`", use_container_width=True):
                    with st.spinner(f"Auditing `{active_file}`..."):
                        audits_map[active_file] = audit_code(active_file, curr_code)
                        results_map.pop(active_file, None)
                    st.rerun()

            with col_b2:
                has_audit = active_file in audits_map
                optimize_clicked = st.button(
                    f"⚡ Step 2: Optimize `{Path(active_file).name}` (With Neighbor Context)",
                    disabled=not has_audit,
                    use_container_width=True,
                )

            if optimize_clicked:
                context_goals = build_neighbor_context(
                    target_rel_path=active_file,
                    working_tree=working_tree,
                    repo_map=st.session_state.proj_repo_map,
                    extra_goals=file_goals,
                    session_rules=st.session_state.get("proj_learned_rules", []),
                )
                results_map[active_file] = run_with_stepper(
                    lambda log, progress: optimize_single_file(
                        filename=Path(active_file).name,
                        original_code=curr_code,
                        audit_report=audits_map[active_file],
                        extra_goals=context_goals,
                        project_context_files=working_tree,
                        target_rel_path=active_file,
                        status_callback=log,
                        progress_callback=progress,
                    ),
                    f"Optimizing `{active_file}` with imported neighbor context (details)",
                )
                st.rerun()

            # Show Audit Report for active file
            if active_file in audits_map:
                st.divider()
                with st.expander(f"📋 Audit & Limitations: `{active_file}`", expanded=active_file not in results_map):
                    st.markdown(audits_map[active_file])

            # Show Optimization Diff, Save & Advance Button, and Follow-Up Q&A
            if active_file in results_map:
                res = results_map[active_file]
                tier = res.get("verification_tier", "none")
                st.divider()
                st.subheader(f"🧪 Results for `{active_file}` — {format_tier_badge(res['approved'], tier)}")
                if res.get("stepper"):
                    html_block(stepper_html(res["stepper"]))

                with st.expander(f"Iteration log ({len(res['logs'])})", expanded=not res["approved"]):
                    for log_line in res["logs"]:
                        st.write(log_line)

                render_results_tabs(res, curr_code, syntax_lang, lang_name, active_file)

                col_save, col_skip = st.columns(2)
                with col_save:
                    if st.button(
                        "✅ Accept Changes & Advance to Next File",
                        type="primary",
                        use_container_width=True,
                    ):
                        working_tree[active_file] = res["optimized_code"]
                        completed_set.add(active_file)
                        next_rem = [f for f in ordered_files if f not in completed_set]
                        if next_rem:
                            st.session_state.proj_active_file = next_rem[0]
                        st.rerun()

                with col_skip:
                    if st.button("⏭️ Mark Completed Without Changes & Advance", use_container_width=True):
                        completed_set.add(active_file)
                        next_rem = [f for f in ordered_files if f not in completed_set]
                        if next_rem:
                            st.session_state.proj_active_file = next_rem[0]
                        st.rerun()

                # Follow-up Q&A or Tweaks for this file
                st.divider()
                st.subheader(f"💬 Ask or Apply Suggestions on `{Path(active_file).name}`")
                st.caption("Suggestions and corrections rewrite the code above, re-verify it with the files it imports, and save to Session Memory for future files.")

                qa_list = st.session_state.proj_qa.setdefault(active_file, [])
                for qa in qa_list:
                    with st.chat_message(qa["role"]):
                        st.markdown(qa["content"])

                with st.form(key=f"proj_qa_form_{active_file}", clear_on_submit=True):
                    q_input = st.text_input(
                        "Question or suggestion:",
                        placeholder="e.g., 'Remove existsByMedicalLicenseNumber and only use methods in UserAccountRepository'",
                    )
                    col_f1, col_f2 = st.columns([2, 1])
                    with col_f1:
                        q_sub = st.form_submit_button("Send to Code Agent", type="primary", use_container_width=True)
                    with col_f2:
                        force_code_edit = st.checkbox("Force code rewrite", value=True)

                if q_sub and q_input.strip():
                    qa_list.append({"role": "user", "content": q_input})
                    neighbor_ctx = build_neighbor_context(
                        target_rel_path=active_file,
                        working_tree=working_tree,
                        repo_map=st.session_state.proj_repo_map,
                        extra_goals=q_input,
                        session_rules=st.session_state.get("proj_learned_rules", []),
                    )
                    effective_msg = (
                        f"Apply this code change: {q_input}"
                        if force_code_edit and not q_input.strip().endswith("?")
                        else q_input
                    )
                    outcome = run_with_stepper(
                        lambda log, progress: handle_code_followup(
                            filename=Path(active_file).name,
                            original_code=orig_code,
                            current_optimized_code=res["optimized_code"],
                            audit_report=audits_map[active_file],
                            user_message=effective_msg,
                            extra_context=neighbor_ctx,
                            status_callback=log,
                            project_context_files=working_tree,
                            target_rel_path=active_file,
                            progress_callback=progress,
                        ),
                        "Applying suggestion & re-verifying with imported files (details)",
                    )
                    if outcome["action"] == "answer":
                        qa_list.append({"role": "assistant", "content": outcome["answer"]})
                    else:
                        results_map[active_file] = outcome
                        new_rule = f"[{Path(active_file).name}] {outcome['instruction']}"
                        if new_rule not in st.session_state.proj_learned_rules:
                            st.session_state.proj_learned_rules.append(new_rule)
                        qa_list.append({
                            "role": "assistant",
                            "content": (
                                f"🔄 **Code Updated & Re-verified!**\n"
                                f"- **Applied Change:** {outcome['instruction']}\n"
                                f"- **Saved to Session Memory:** `{new_rule}` (will be enforced on all upcoming files)"
                            ),
                        })
                    st.rerun()

                # Show active learned rules visibly right below the chat too
                if st.session_state.get("proj_learned_rules"):
                    with st.container(border=True):
                        st.markdown("**🧠 Active Rules Carried Forward to Future Files:**")
                        for r_idx, rule_item in enumerate(st.session_state["proj_learned_rules"], start=1):
                            st.markdown(f"{r_idx}. `{rule_item}`")