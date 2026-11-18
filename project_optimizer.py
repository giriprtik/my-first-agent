import ast
import io
import re
import zipfile
from pathlib import Path
from file_optimizer import (
    audit_code,
    optimize_single_file,
    extract_public_interface,
    get_language_info,
    LANGUAGE_PROFILES,
)

IGNORED_DIRS = {
    ".git", ".svn", ".hg", "__pycache__", "node_modules", "venv", ".venv",
    "env", ".env", "dist", "build", ".next", ".idea", ".vscode", "target",
    "workspace", "workspace_project", "agent_outputs"
}

IGNORED_FILES = {".DS_Store", "package-lock.json", "yarn.lock", "poetry.lock"}
MAX_ZIP_ENTRIES = 2000
MAX_TOTAL_BYTES = 25_000_000  # 25 MB uncompressed cap
MAX_FILE_BYTES = 200_000


def is_supported_code_file(rel_path: Path) -> bool:
    """Checks if a relative path inside a zip is a supported code/config file."""
    if any(part in IGNORED_DIRS or part.startswith(".") for part in rel_path.parts[:-1]):
        return False
    if rel_path.name in IGNORED_FILES or rel_path.name.startswith("."):
        return False
    if rel_path.name.lower() == "dockerfile":
        return True
    return rel_path.suffix.lower() in LANGUAGE_PROFILES


def parse_zip_in_memory(zip_bytes: bytes) -> tuple[dict[str, bytes], dict[str, str]]:
    """
    Safely parses a zip archive in memory with path-aware zip-slip checks and size caps.
    Returns (all_clean_binary_files, supported_utf8_code_files).
    """
    all_files: dict[str, bytes] = {}
    code_files: dict[str, str] = {}
    virtual_root = Path("/virtual_sandbox_root")
    total_bytes = 0

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise ValueError(f"Zip archive exceeds {MAX_ZIP_ENTRIES} entries limit.")

        for info in infos:
            if info.is_dir():
                continue

            # Filter junk directories BEFORE resolving paths
            raw_parts = Path(info.filename).parts
            if any(part in IGNORED_DIRS or part == "__MACOSX" for part in raw_parts[:-1]):
                continue
            if Path(info.filename).name in IGNORED_FILES:
                continue

            resolved = (virtual_root / info.filename).resolve()
            if not resolved.is_relative_to(virtual_root):
                continue

            rel_path = resolved.relative_to(virtual_root)
            rel_posix = rel_path.as_posix()

            total_bytes += info.file_size
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("Zip uncompressed size exceeds 25 MB safety limit.")

            raw_data = zf.read(info.filename)
            all_files[rel_posix] = raw_data

            if is_supported_code_file(rel_path) and info.file_size <= MAX_FILE_BYTES:
                try:
                    code_files[rel_posix] = raw_data.decode("utf-8")
                except UnicodeDecodeError:
                    pass

    return all_files, code_files


def _extract_imports(rel_path: str, code: str) -> set[str]:
    """Extracts imported module stems safely."""
    ext = Path(rel_path).suffix.lower()
    imports: set[str] = set()

    if ext == ".py":
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
        except SyntaxError:
            pass
    elif ext in {".js", ".ts", ".jsx", ".tsx", ".mjs"}:
        for m in re.finditer(r"""(?:import|require)\s*(?:.*?\sfrom\s*)?['"]\.\/([^'"]+)['"]""", code):
            imports.add(Path(m.group(1)).stem)

    return imports


def build_repo_map_and_order(code_files: dict[str, str]) -> tuple[str, list[str]]:
    """Builds a compact Repository Map and topologically sorts files (leaves first)."""
    rel_paths = sorted(code_files.keys())
    stems_to_paths = {Path(p).stem: p for p in rel_paths}
    deps: dict[str, set[str]] = {p: set() for p in rel_paths}
    map_lines = []

    for rel_path in rel_paths:
        code = code_files[rel_path]
        public_api = extract_public_interface(rel_path, code)
        for stem in _extract_imports(rel_path, code):
            if stem in stems_to_paths and stems_to_paths[stem] != rel_path:
                deps[rel_path].add(stems_to_paths[stem])

        compact_sig = " | ".join(line.strip("- ").strip() for line in public_api.splitlines()[:6])
        map_lines.append(f"- `{rel_path}`: {compact_sig}")

    ordered: list[str] = []
    visited: set[str] = set()

    def visit(node: str):
        if node in visited:
            return
        visited.add(node)
        for dep in sorted(deps[node]):
            visit(dep)
        ordered.append(node)

    for f in rel_paths:
        visit(f)

    return "\n".join(map_lines), ordered


def optimize_project_files(
    selected_files: list[str],
    code_files: dict[str, str],
    repo_map: str,
    extra_goals: str = "",
    audit_only: bool = False,
    status_callback=None,
) -> dict:
    """Audits and optimizes selected project files in isolated temp sandboxes with per-file error isolation."""
    def log(msg: str):
        if status_callback:
            status_callback(msg)

    working_tree = dict(code_files)
    results = {}

    for idx, rel_path in enumerate(selected_files, start=1):
        original_code = working_tree[rel_path]
        lang_name, _, _ = get_language_info(rel_path)

        try:
            log(f"[{idx}/{len(selected_files)}] 🔍 Auditing `{rel_path}` ({lang_name})...")
            audit_report = audit_code(rel_path, original_code)

            if audit_only:
                results[rel_path] = {
                    "audit_report": audit_report,
                    "approved": None,
                    "verification_tier": "none",
                    "optimized_code": original_code,
                    "diff": "",
                    "logs": ["Audit-only mode: no code changes made."],
                }
                continue

            log(f"[{idx}/{len(selected_files)}] ⚡ Optimizing & verifying `{rel_path}`...")
            project_goals = (
                f"PROJECT REPOSITORY MAP (PRESERVE PUBLIC SIGNATURES):\n{repo_map}\n\n"
                f"USER GOALS:\n{extra_goals or 'Apply concrete security, error-handling, and performance fixes from the Audit Report.'}"
            )

            opt_res = optimize_single_file(
                filename=Path(rel_path).name,
                original_code=original_code,
                audit_report=audit_report,
                extra_goals=project_goals,
                project_context_files=working_tree,
                target_rel_path=rel_path,
                status_callback=lambda m: log(f"   ↳ `{rel_path}`: {m}"),
            )

            if opt_res["approved"]:
                working_tree[rel_path] = opt_res["optimized_code"]

            results[rel_path] = {
                "audit_report": audit_report,
                "approved": opt_res["approved"],
                "verification_tier": opt_res.get("verification_tier", "none"),
                "optimized_code": opt_res["optimized_code"],
                "diff": opt_res["diff"],
                "logs": opt_res["logs"],
            }

        except Exception as e:
            log(f"❌ [{idx}/{len(selected_files)}] Error processing `{rel_path}`: {e}")
            results[rel_path] = {
                "audit_report": f"Error during processing: {e}",
                "approved": False,
                "verification_tier": "none",
                "optimized_code": original_code,
                "diff": "",
                "logs": [f"❌ Failed with error: {e}"],
            }

    return results


def package_overlay_zip(all_original_files: dict[str, bytes], results: dict) -> bytes:
    """Overlays approved optimized files onto the complete original project archive (preserving README, assets, etc.)."""
    merged_files = dict(all_original_files)

    report_sections = ["# Project Security, Optimization & Limitations Report\n"]
    for rel_path, data in results.items():
        tier = data.get("verification_tier", "none")
        badge = "AUDIT ONLY" if data["approved"] is None else (f"APPROVED ({tier})" if data["approved"] else "NEEDS REVIEW")
        report_sections.append(f"## `{rel_path}` [{badge}]\n\n{data['audit_report']}\n\n---\n")

        if data.get("approved") and data.get("optimized_code"):
            merged_files[rel_path] = data["optimized_code"].encode("utf-8")

    merged_files["PROJECT_AUDIT_REPORT.md"] = "\n".join(report_sections).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel_posix, content_bytes in sorted(merged_files.items()):
            zf.writestr(rel_posix, content_bytes)
    buf.seek(0)
    return buf.getvalue()
