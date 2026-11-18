import json
from pathlib import Path
import streamlit as st
from agent import create_agent_session, send_with_fallback, MODELS
from file_optimizer import (
    audit_code,
    optimize_single_file,
    extract_public_interface,
    get_language_info,
    handle_code_followup,
)
from project_optimizer import (
    extract_zip_to_sandbox,
    build_repo_map_and_order,
    optimize_project_files,
    package_sandbox_as_zip,
)


st.set_page_config(page_title="AI Agent & Universal Code Optimizer", page_icon="🤖", layout="wide")

PROJECT_ROOT = Path(__file__).parent
OUTPUTS_DIR = PROJECT_ROOT / "agent_outputs"
METRICS_FILE = PROJECT_ROOT / "agent_metrics.jsonl"

# --- SIDEBAR: TELEMETRY & SAVED NOTES ---
with st.sidebar:
    st.header("📊 Live Telemetry")
    if METRICS_FILE.exists():
        lines = [
            json.loads(line)
            for line in METRICS_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if lines:
            total_tokens = sum(m.get("total_tokens", 0) for m in lines)
            avg_latency = sum(m.get("latency_sec", 0) for m in lines) / len(lines)
            col1, col2 = st.columns(2)
            col1.metric("Requests", len(lines))
            col2.metric("Avg Latency", f"{avg_latency:.2f}s")
            st.metric("Total Tokens", f"{total_tokens:,}")
    else:
        st.caption("No metrics logged yet.")

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
        st.session_state.messages = []
        st.session_state.chat = create_agent_session(MODELS)
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
    if "chat" not in st.session_state:
        st.session_state.chat = create_agent_session(MODELS)
    if "messages" not in st.session_state:
        st.session_state.messages = []

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("Ask a question or say 'Save a note about...'"):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                try:
                    st.session_state.chat, reply = send_with_fallback(st.session_state.chat, prompt)
                    st.markdown(reply)
                    st.session_state.messages.append({"role": "assistant", "content": reply})
                    st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")

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
        ignored = {"app.py", "file_optimizer.py", "coding_loop.py", ".env"}
        local_files = sorted(
            [
                f.name
                for f in PROJECT_ROOT.iterdir()
                if f.is_file() and not f.name.startswith(".") and f.name not in ignored and f.suffix != ".jsonl"
            ]
        )
        if local_files:
            target_filename = st.selectbox("Target project file:", local_files)
            source_code = (PROJECT_ROOT / target_filename).read_text(encoding="utf-8")
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

        # STEP 1: AUDIT & LIST LIMITATIONS
        with col_audit:
            if st.button("🔍 Step 1: Audit Security & Limitations", use_container_width=True):
                with st.spinner(f"Auditing `{target_filename}` ({lang_name})..."):
                    st.session_state.audit_report = audit_code(target_filename, source_code)
                    st.session_state.audited_file = target_filename
                    st.session_state.opt_result = None
                    st.session_state.code_qa_history = []

        # STEP 2: OPTIMIZE & VERIFY
        with col_opt:
            audit_ready = (
                st.session_state.get("audited_file") == target_filename
                and st.session_state.get("audit_report")
            )
            if st.button("⚡ Step 2: Optimize & Verify in Sandbox", disabled=not audit_ready, use_container_width=True):
                with st.status(f"Running {lang_name} Tester → Coder → Reviewer loop...", expanded=True) as status:
                    result = optimize_single_file(
                        filename=target_filename,
                        original_code=source_code,
                        audit_report=st.session_state.audit_report,
                        extra_goals=extra_goals,
                        status_callback=lambda msg: status.write(msg),
                    )
                    st.session_state.opt_result = result
                    st.session_state.code_qa_history = []
                    status.update(
                        label="✅ Optimization Verified & Approved!" if result["approved"] else "⚠️ Max Iterations Reached",
                        state="complete" if result["approved"] else "error",
                    )

        # DISPLAY AUDIT REPORT
        if st.session_state.get("audited_file") == target_filename and st.session_state.get("audit_report"):
            st.divider()
            st.subheader(f"📋 Audit & Limitations Report: `{target_filename}`")
            st.markdown(st.session_state.audit_report)

        # DISPLAY OPTIMIZATION DIFF & PROMOTION GATE
        if st.session_state.get("opt_result"):
            res = st.session_state.opt_result
            st.divider()
            st.subheader("🧪 Optimization Results & Unified Diff")

            for entry in res["logs"]:
                st.write(entry)

            tab_diff, tab_new_code, tab_tests = st.tabs(["🔀 Unified Diff", "✅ Optimized File", "🧪 Generated Tests"])
            with tab_diff:
                st.code(res["diff"] or "No changes made.", language="diff")
            with tab_new_code:
                st.code(res["optimized_code"], language=syntax_lang)
            with tab_tests:
                if res["test_code"]:
                    st.code(res["test_code"], language=syntax_lang)
                else:
                    st.info(f"Config/Script mode (`{lang_name}`): Verified via deterministic syntax parser and Reviewer gate.")

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
                        (PROJECT_ROOT / target_filename).write_text(res["optimized_code"], encoding="utf-8")
                        st.success(f"Live `{target_filename}` updated! Reloading...")
                        st.rerun()

            # --- NEW: FOLLOW-UP Q&A & RE-OPTIMIZE BOX ---
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
                with st.status("Analyzing follow-up request...", expanded=True) as f_status:
                    outcome = handle_code_followup(
                        filename=target_filename,
                        original_code=source_code,
                        current_optimized_code=res["optimized_code"],
                        audit_report=st.session_state.audit_report,
                        user_message=followup_input,
                        status_callback=lambda msg: f_status.write(msg),
                    )
                    if outcome["action"] == "answer":
                        st.session_state.code_qa_history.append({"role": "assistant", "content": outcome["answer"]})
                        f_status.update(label="✅ Answered!", state="complete")
                    else:
                        st.session_state.opt_result = outcome
                        msg = (
                            f"🔄 **Re-optimized `{target_filename}`** based on: *{outcome['instruction']}*\n\n"
                            f"Status: {'✅ Verified & Approved' if outcome['approved'] else '⚠️ Max iterations reached'}. "
                            f"Check the **Unified Diff** and **Optimized File** tabs above!"
                        )
                        st.session_state.code_qa_history.append({"role": "assistant", "content": msg})
                        f_status.update(label="✅ Code Updated & Re-verified!", state="complete")
                st.rerun()


# ================= TAB 3: FULL PROJECT (.ZIP) OPTIMIZER =================
with tab_project:
    st.title("🗂️ Full Project (.zip) Security & Optimization Pipeline")
    st.caption(
        "Upload a project .zip → Automatically filters junk folders → Builds a low-token AST dependency map → "
        "Audits & optimizes files in topological order → Exports a clean .zip + PROJECT_AUDIT_REPORT.md."
    )

    uploaded_zip = st.file_uploader("Upload a zipped project folder (.zip)", type=["zip"])

    if uploaded_zip is not None:
        if st.session_state.get("last_zip_name") != uploaded_zip.name:
            extracted = extract_zip_to_sandbox(uploaded_zip.getvalue())
            repo_map, ordered_files = build_repo_map_and_order(extracted)
            st.session_state.last_zip_name = uploaded_zip.name
            st.session_state.proj_files = ordered_files
            st.session_state.proj_repo_map = repo_map
            st.session_state.proj_results = None

        ordered_files = st.session_state.get("proj_files", [])
        repo_map = st.session_state.get("proj_repo_map", "")

        if not ordered_files:
            st.warning("No supported code or config files found inside this .zip archive.")
        else:
            st.success(f"Found **{len(ordered_files)}** code/config files (sorted in dependency order: utilities first, entrypoints last).")

            with st.expander("🗺️ View Compact Repository Map (Low-Token Blueprint)", expanded=False):
                st.code(repo_map, language="markdown")

            selected_files = st.multiselect(
                "Select which files to include in this run (deselect any you want to skip to save tokens):",
                options=ordered_files,
                default=ordered_files,
            )

            proj_goals = st.text_input(
                "Project-wide optimization goals (optional):",
                placeholder="e.g., Harden security, add type hints, standardize error handling...",
            )

            col_mode1, col_mode2 = st.columns(2)
            with col_mode1:
                run_audit_only = st.button("🔍 Audit All Selected Files Only (Fast & Ultra-Low Tokens)", use_container_width=True)
            with col_mode2:
                run_full_opt = st.button("⚡ Audit + Optimize + Verify All Selected Files", type="primary", use_container_width=True)

            if (run_audit_only or run_full_opt) and selected_files:
                with st.status("Processing project files one by one...", expanded=True) as p_status:
                    proj_res = optimize_project_files(
                        selected_files=selected_files,
                        repo_map=repo_map,
                        extra_goals=proj_goals,
                        audit_only=run_audit_only,
                        status_callback=lambda m: p_status.write(m),
                    )
                    st.session_state.proj_results = proj_res
                    p_status.update(label="✅ Project Run Complete!", state="complete")

            if st.session_state.get("proj_results"):
                proj_res = st.session_state.proj_results
                st.divider()
                st.subheader("📦 Export Optimized Project & Full Audit Report")

                zip_output = package_sandbox_as_zip(proj_res)
                st.download_button(
                    label="⬇️ Download Optimized Project (.zip + PROJECT_AUDIT_REPORT.md)",
                    data=zip_output,
                    file_name=f"optimized_{uploaded_zip.name}",
                    mime="application/zip",
                    type="primary",
                    use_container_width=True,
                )

                st.subheader("📄 File-by-File Results")
                for rel_path, data in proj_res.items():
                    badge = "🔍 Audited" if data["approved"] is None else ("✅ Approved" if data["approved"] else "⚠️ Needs Review")
                    with st.expander(f"{badge} — `{rel_path}`", expanded=False):
                        t1, t2, t3 = st.tabs(["📋 Audit & Limitations", "🔀 Unified Diff", "✅ Final Code"])
                        with t1:
                            st.markdown(data["audit_report"])
                        with t2:
                            st.code(data["diff"] or "No changes made.", language="diff")
                        with t3:
                            _, syn_lang, _ = get_language_info(Path(rel_path).name)
                            st.code(data["optimized_code"], language=syn_lang)

