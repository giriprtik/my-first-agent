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
    parse_zip_in_memory,
    build_repo_map_and_order,
    build_neighbor_context,
    package_overlay_zip,
    check_cross_file_consistency,
)


st.set_page_config(page_title="AI Agent & Universal Code Optimizer", page_icon="🤖", layout="wide")

PROJECT_ROOT = Path(__file__).parent
OUTPUTS_DIR = PROJECT_ROOT / "agent_outputs"
METRICS_FILE = PROJECT_ROOT / "agent_metrics.jsonl"


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
        st.session_state.chat = create_agent_session(MODELS[0])
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
        st.session_state.chat = create_agent_session(MODELS[0])
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
        ignored = {
            "app.py",
            "file_optimizer.py",
            "project_optimizer.py",
            "tools.py",
            "coding_loop.py",
            ".env",
        }
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
                    badge_label = format_tier_badge(result["approved"], result.get("verification_tier", "none"))
                    status.update(
                        label=badge_label,
                        state="complete" if result["approved"] else "error",
                    )

        if st.session_state.get("audited_file") == target_filename and st.session_state.get("audit_report"):
            st.divider()
            st.subheader(f"📋 Audit & Limitations Report: `{target_filename}`")
            st.markdown(st.session_state.audit_report)

        if st.session_state.get("opt_result"):
            res = st.session_state.opt_result
            tier = res.get("verification_tier", "none")
            st.divider()
            st.subheader(f"🧪 Optimization Results — {format_tier_badge(res['approved'], tier)}")

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
                    st.info(f"Verification Tier `{tier}` (`{lang_name}`): Verified without standalone unit tests.")

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
                        new_badge = format_tier_badge(outcome["approved"], outcome.get("verification_tier", "none"))
                        msg = (
                            f"🔄 **Re-optimized `{target_filename}`** based on: *{outcome['instruction']}*\n\n"
                            f"Status: {new_badge}. Check the **Unified Diff** and **Optimized File** tabs above!"
                        )
                        st.session_state.code_qa_history.append({"role": "assistant", "content": msg})
                        f_status.update(label="✅ Code Updated & Re-verified!", state="complete")
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
        if st.session_state.get("last_zip_name") != uploaded_zip.name:
            try:
                all_files, code_files = parse_zip_in_memory(uploaded_zip.getvalue())
                repo_map, ordered_files, dep_map = build_repo_map_and_order(code_files)
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

            with st.expander("🗺️ View Compact Repository Map & Dependency Order", expanded=False):
                st.code(st.session_state.proj_repo_map, language="markdown")

            # --- PERSISTENT SESSION MEMORY (LEARNED FROM FOLLOW-UPS) ---
            learned_rules = st.session_state.setdefault("proj_learned_rules", [])
            with st.expander(f"🧠 Session Memory & Learned Rules ({len(learned_rules)} active)", expanded=bool(learned_rules)):
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
            def _format_file_option(path_str: str) -> str:
                if path_str in completed_set:
                    prefix = "✅ [Saved]"
                elif path_str == recommended_next:
                    prefix = "👉 [Next]"
                elif path_str in audits_map:
                    prefix = "🔍 [Audited]"
                else:
                    prefix = "⬜ [Pending]"
                return f"{prefix} {path_str}"

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

            orig_code = st.session_state.proj_orig_code[active_file]
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
                        st.session_state.proj_results.pop(active_file, None)

            with col_b2:
                has_audit = active_file in audits_map
                if st.button(
                    f"⚡ Step 2: Optimize `{Path(active_file).name}` (With Neighbor Context)",
                    disabled=not has_audit,
                    use_container_width=True,
                ):
                    with st.status(f"Optimizing `{active_file}` with imported neighbor context...", expanded=True) as f_stat:
                        context_goals = build_neighbor_context(
                            target_rel_path=active_file,
                            working_tree=working_tree,
                            repo_map=st.session_state.proj_repo_map,
                            extra_goals=file_goals,
                            session_rules=st.session_state.get("proj_learned_rules", []),
                        )
                        opt_res = optimize_single_file(
                            filename=Path(active_file).name,
                            original_code=curr_code,
                            audit_report=audits_map[active_file],
                            extra_goals=context_goals,
                            project_context_files=working_tree,
                            target_rel_path=active_file,
                            status_callback=lambda m: f_stat.write(m),
                        )
                        st.session_state.proj_results[active_file] = opt_res
                        badge_txt = format_tier_badge(opt_res["approved"], opt_res.get("verification_tier", "none"))
                        f_stat.update(
                            label=badge_txt,
                            state="complete" if opt_res["approved"] else "error",
                        )

            # Show Audit Report for active file
            if active_file in audits_map:
                st.divider()
                st.subheader(f"📋 Audit & Limitations: `{active_file}`")
                st.markdown(audits_map[active_file])

            # Show Optimization Diff, Save & Advance Button, and Follow-Up Q&A
            if active_file in st.session_state.proj_results:
                res = st.session_state.proj_results[active_file]
                tier = res.get("verification_tier", "none")
                st.divider()
                st.subheader(f"🧪 Results for `{active_file}` — {format_tier_badge(res['approved'], tier)}")

                for log_line in res["logs"]:
                    st.write(log_line)

                t_diff, t_code, t_tests = st.tabs(["🔀 Unified Diff", "✅ Optimized Code", "🧪 Tests"])
                with t_diff:
                    st.code(res["diff"] or "No changes made.", language="diff")
                with t_code:
                    st.code(res["optimized_code"], language=syntax_lang)
                with t_tests:
                    if res["test_code"]:
                        st.code(res["test_code"], language=syntax_lang)
                    else:
                        st.info(f"Verification Tier `{tier}` (`{lang_name}`).")

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
                st.caption("Suggestions and corrections automatically rewrite the code above and save to Session Memory for future files.")

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
                    with st.status("Applying suggestion & re-verifying code...", expanded=True) as q_stat:
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
                        outcome = handle_code_followup(
                            filename=Path(active_file).name,
                            original_code=orig_code,
                            current_optimized_code=res["optimized_code"],
                            audit_report=audits_map[active_file],
                            user_message=effective_msg,
                            extra_context=neighbor_ctx,
                            status_callback=lambda m: q_stat.write(m),
                        )
                        if outcome["action"] == "answer":
                            qa_list.append({"role": "assistant", "content": outcome["answer"]})
                        else:
                            st.session_state.proj_results[active_file] = outcome
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
