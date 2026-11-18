import ast
import io
import os
import re
import shutil
import zipfile
from pathlib import Path
from file_optimizer import (
    audit_code,
    optimize_single_file,
    extract_public_interface,
    get_language_info,
    LANGUAGE_PROFILES,
)

PROJECT_ROOT = Path(__file__).parent
PROJECT_SANDBOX = PROJECT_ROOT / "workspace_project"

IGNORED_DIRS = {
    ".git", ".svn", ".hg", "__pycache__", "node_modules", "venv", ".venv",
    "env", ".env", "dist", "build", ".next", ".idea", ".vscode", "target",
    "workspace", "workspace_project", "agent_outputs"
}

IGNORED_FILES = {".DS_Store", "package-lock.json", "yarn.lock", "poetry.lock"}
MAX_FILE_BYTES = 200_000  # Skip giant generated/minified files over 200 KB


def is_supported_project_file(rel_path: Path) -> bool:
    """Checks if a file inside the zip is a valid code/config file worth auditing."""
    if any(part in IGNORED_DIRS or part.startswith(".") for part in rel_path.parts[:-1]):
        return False
    if rel_path.name in IGNORED_FILES or rel_path.name.startswith("."):
        return False
    if rel_path.name.lower() == "dockerfile":
        return True
    return rel_path.suffix.lower() in LANGUAGE_PROFILES


def extract_zip_to_sandbox(zip_bytes: bytes) -> list[str]:
    """Safely extracts a zip archive into workspace_project/ (blocking Zip Slip attacks) and returns relative file paths."""
    if PROJECT_SANDBOX.exists():
        shutil.rmtree(PROJECT_SANDBOX)
    PROJECT_SANDBOX.mkdir(parents=True, exist_ok=True)

    extracted_files = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            # Prevent Zip Slip path traversal
            target_path = (PROJECT_SANDBOX / info.filename).resolve()
            if not str(target_path).startswith(str(PROJECT_SANDBOX.resolve())):
                continue

            rel_path = target_path.relative_to(PROJECT_SANDBOX)
            if not is_supported_project_file(rel_path):
                continue
            if info.file_size > MAX_FILE_BYTES:
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)
            raw_data = zf.read(info.filename)
            try:
                text_data = raw_data.decode("utf-8")
            except UnicodeDecodeError:
                continue  # Skip binary files safely

            target_path.write_text(text_data, encoding="utf-8")
            extracted_files.append(rel_path.as_posix())

    return sorted(extracted_files)


def _extract_imports(rel_path: str, code: str) -> set[str]:
    """Finds local module names imported by a file to build a lightweight dependency graph."""
    ext = Path(rel_path).suffix.lower()
    imports = set()

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


def build_repo_map_and_order(rel_files: list[str]) -> tuple[str, list[str]]:
    """Builds a compact token-saving Repository Map and sorts files from leaves -> entrypoints."""
    stems_to_paths = {Path(p).stem: p for p in rel_files}
    deps: dict[str, set[str]] = {p: set() for p in rel_files}
    map_lines = []

    for rel_path in rel_files:
        full_path = PROJECT_SANDBOX / rel_path
        code = full_path.read_text(encoding="utf-8")
        public_api = extract_public_interface(Path(rel_path).name, code)
        imported_stems = _extract_imports(rel_path, code)

        for stem in imported_stems:
            if stem in stems_to_paths and stems_to_paths[stem] != rel_path:
                deps[rel_path].add(stems_to_paths[stem])

        compact_sig = " | ".join(line.strip("- ").strip() for line in public_api.splitlines()[:6])
        map_lines.append(f"- `{rel_path}`: {compact_sig}")

    # Topological sort: files with 0 local dependencies come first
    ordered = []
    visited = set()

    def visit(node: str):
        if node in visited:
            return
        visited.add(node)
        for dep in sorted(deps[node]):
            visit(dep)
        ordered.append(node)

    for f in rel_files:
        visit(f)

    repo_map_str = "\n".join(map_lines)
    return repo_map_str, ordered


def optimize_project_files(
    selected_files: list[str],
    repo_map: str,
    extra_goals: str = "",
    audit_only: bool = False,
    status_callback=None,
) -> dict:
    """Audits and optionally optimizes each selected project file one by one inside workspace_project/."""
    def log(msg: str):
        if status_callback:
            status_callback(msg)

    results = {}
    workspace_dir = PROJECT_ROOT / "workspace"
    workspace_dir.mkdir(exist_ok=True)

    for idx, rel_path in enumerate(selected_files, start=1):
        full_path = PROJECT_SANDBOX / rel_path
        original_code = full_path.read_text(encoding="utf-8")
        lang_name, _, _ = get_language_info(Path(rel_path).name)

        log(f"[{idx}/{len(selected_files)}] 🔍 Auditing `{rel_path}` ({lang_name})...")
        audit_report = audit_code(rel_path, original_code)

        if audit_only:
            results[rel_path] = {
                "audit_report": audit_report,
                "approved": None,
                "optimized_code": original_code,
                "diff": "",
                "logs": ["Audit-only mode: no code changes made."],
            }
            continue

        log(f"[{idx}/{len(selected_files)}] ⚡ Optimizing & verifying `{rel_path}`...")

        # Stage all other sandbox project files into workspace/ so cross-file imports resolve
        for other_rel in selected_files:
            if other_rel != rel_path:
                src_other = PROJECT_SANDBOX / other_rel
                if src_other.exists():
                    shutil.copy2(src_other, workspace_dir / Path(other_rel).name)

        project_context_goals = (
            f"PROJECT REPOSITORY MAP (DO NOT BREAK SIGNATURES USED BY OTHER FILES):\n{repo_map}\n\n"
            f"USER GOALS:\n{extra_goals or 'Refactor and improve code quality, security, error handling, and performance based on the Audit Report. You MUST apply concrete improvements.'}"
        )

        opt_res = optimize_single_file(
            filename=Path(rel_path).name,
            original_code=original_code,
            audit_report=audit_report,
            extra_goals=project_context_goals,
            status_callback=lambda m: log(f"   ↳ `{rel_path}`: {m}"),
        )

        if opt_res["approved"]:
            full_path.write_text(opt_res["optimized_code"], encoding="utf-8")

        results[rel_path] = {
            "audit_report": audit_report,
            "approved": opt_res["approved"],
            "optimized_code": opt_res["optimized_code"],
            "diff": opt_res["diff"],
            "logs": opt_res["logs"],
        }

    return results



def package_sandbox_as_zip(results: dict) -> bytes:
    """Bundles the entire optimized workspace_project/ plus a full AUDIT_REPORT.md into a downloadable .zip."""
    # Write a combined markdown audit & limitations report inside the project root
    report_sections = ["# Project Security, Optimization & Limitations Report\n"]
    for rel_path, data in results.items():
        status_badge = "AUDIT ONLY" if data["approved"] is None else ("APPROVED" if data["approved"] else "NEEDS REVIEW")
        report_sections.append(f"## `{rel_path}` [{status_badge}]\n\n{data['audit_report']}\n\n---\n")

    (PROJECT_SANDBOX / "PROJECT_AUDIT_REPORT.md").write_text("\n".join(report_sections), encoding="utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in PROJECT_SANDBOX.rglob("*"):
            if file_path.is_file():
                zf.write(file_path, arcname=file_path.relative_to(PROJECT_SANDBOX).as_posix())
    buf.seek(0)
    return buf.getvalue()
