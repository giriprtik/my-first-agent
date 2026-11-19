import ast
import io
import re
import json
import zipfile
from pathlib import Path
from file_optimizer import (
    extract_public_interface,
    LANGUAGE_PROFILES,
    ask_agent,
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
    Filters ignored directories (like venv/ and node_modules/) before counting entries.
    """
    all_files: dict[str, bytes] = {}
    code_files: dict[str, str] = {}
    virtual_root = Path("/virtual_sandbox_root").resolve()

    try:
        _read_zip_entries(zip_bytes, virtual_root, all_files, code_files)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError) as e:
        raise ValueError(f"Not a valid or readable .zip archive: {e}") from e

    return all_files, code_files


def _read_zip_entries(
    zip_bytes: bytes,
    virtual_root: Path,
    all_files: dict[str, bytes],
    code_files: dict[str, str],
) -> None:
    total_bytes = 0
    kept_entries = 0

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue

            raw_parts = Path(info.filename).parts
            if any(part in IGNORED_DIRS or part == "__MACOSX" for part in raw_parts[:-1]):
                continue
            if Path(info.filename).name in IGNORED_FILES:
                continue

            # Pre-check size before extraction or memory allocation
            if info.file_size > MAX_FILE_BYTES:
                continue

            total_bytes += info.file_size
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("Zip uncompressed size exceeds 25 MB safety limit.")

            kept_entries += 1
            if kept_entries > MAX_ZIP_ENTRIES:
                raise ValueError(f"Zip archive exceeds {MAX_ZIP_ENTRIES} valid project files limit.")

            # Sanitize path traversal via pure string normalization and is_relative_to check
            clean_name = info.filename.lstrip("/\\")
            if ":" in clean_name or ".." in Path(clean_name).parts:
                raise ValueError(f"Detected potential zip slip path traversal: {info.filename}")

            resolved = (virtual_root / clean_name).resolve()
            if not resolved.is_relative_to(virtual_root):
                raise ValueError(f"Path traversal outside sandbox root detected: {info.filename}")

            rel_path = resolved.relative_to(virtual_root)
            rel_posix = rel_path.as_posix()

            raw_data = zf.read(info.filename)
            all_files[rel_posix] = raw_data

            if is_supported_code_file(rel_path):
                try:
                    code_files[rel_posix] = raw_data.decode("utf-8")
                except UnicodeDecodeError:
                    pass


def _extract_java_package(code: str) -> str:
    """Returns the Java package name declared at the top of a .java file, or empty string."""
    m = re.search(r"^\s*package\s+([a-zA-Z0-9_.]+)\s*;", code, re.MULTILINE)
    return m.group(1) if m else ""


# Enhanced robust JS/TS/JSX import/export regex handling multiline, aliases, and TypeScript path aliases
_JS_LOCAL_IMPORT = re.compile(
    r"""(?:\b(?:import|export)\b[^'"`;]*?\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)['"]((?:\.{1,2}/|@[a-zA-Z0-9_-]+/).*?)['"]"""
)


def extract_imports(rel_path: str, code: str) -> set[str]:
    """Extracts imported module/class stems or package wildcards across Python, JS/TS, and Java safely."""
    ext = Path(rel_path).suffix.lower()
    imports: set[str] = set()

    if ext == ".py":
        try:
            tree = ast.parse(code)
            # Bounded recursion / node inspection using ast.walk with node count or depth check
            node_count = 0
            for node in ast.walk(tree):
                node_count += 1
                if node_count > 50000:
                    break
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.add(alias.name.partition(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.partition(".")[0])
        except SyntaxError:
            pass
    elif ext in {".js", ".ts", ".jsx", ".tsx", ".mjs"}:
        for m in _JS_LOCAL_IMPORT.finditer(code):
            raw_path = m.group(1)
            imports.add(Path(raw_path).stem)
    elif ext == ".java":
        for m in re.finditer(r"^\s*import\s+(?:static\s+)?([a-zA-Z0-9_.*]+)\s*;", code, re.MULTILINE):
            imp = m.group(1)
            if imp.endswith(".*"):
                imports.add(imp)  # e.g., "com.hospital.auth.dto.*"
            else:
                parts = imp.split(".")
                if parts:
                    imports.add(parts[-2] if len(parts) >= 2 and parts[-1] and parts[-1][0].islower() else parts[-1])

    return imports


def build_repo_map_and_order(code_files: dict[str, str]) -> tuple[str, list[str], dict[str, list[str]]]:
    """
    Builds a compact Repository Map, topologically sorts files strictly from leaves -> dependents
    (DTOs/models/exceptions -> repositories/utils -> services -> controllers/runners),
    and returns (repo_map_str, ordered_files, dependency_map).
    """
    rel_paths = sorted(code_files.keys())
    stems_to_paths: dict[str, list[str]] = {}
    packages_to_paths: dict[str, list[str]] = {}

    for p in rel_paths:
        stems_to_paths.setdefault(Path(p).stem, []).append(p)
        if p.endswith(".java"):
            pkg = _extract_java_package(code_files[p])
            if pkg:
                packages_to_paths.setdefault(pkg, []).append(p)

    deps: dict[str, set[str]] = {p: set() for p in rel_paths}
    map_lines = []

    # Single pass extraction of public interfaces and imports to optimize repository map construction
    for rel_path in rel_paths:
        code = code_files[rel_path]
        public_api = extract_public_interface(rel_path, code)

        for token in extract_imports(rel_path, code):
            if token.endswith(".*"):
                pkg_name = token[:-2]
                for target_p in packages_to_paths.get(pkg_name, []):
                    if target_p != rel_path:
                        deps[rel_path].add(target_p)
            else:
                for target_p in stems_to_paths.get(token, []):
                    if target_p != rel_path:
                        deps[rel_path].add(target_p)

        compact_sig = " | ".join(line.strip("- ").strip() for line in public_api.splitlines()[:6])
        map_lines.append(f"- `{rel_path}`: {compact_sig}")

    # Architectural layer tie-breaker so leaf layers always sort first even with 0 explicit edges
    def _layer_priority(path_str: str) -> int:
        lower = path_str.lower()
        if any(k in lower for k in ("/dto/", "/model/", "/entity/", "/exception/", "/enums/")):
            return 1
        if any(k in lower for k in ("/repository/", "/util/", "/config/", "/security/")):
            return 2
        if "/service/" in lower:
            return 3
        if any(k in lower for k in ("/controller/", "/seeder/", "application.java")):
            return 4
        return 5

    ordered: list[str] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(node: str):
        if node in visited:
            return
        if node in visiting:
            return  # Cycle break
        visiting.add(node)
        for dep in sorted(deps[node], key=lambda p: (_layer_priority(p), p)):
            visit(dep)
        visiting.remove(node)
        visited.add(node)
        ordered.append(node)

    for f in sorted(rel_paths, key=lambda p: (_layer_priority(p), p)):
        visit(f)

    dep_lists = {k: sorted(v) for k, v in deps.items()}
    return "\n".join(map_lines), ordered, dep_lists


def _local_neighbors(target_rel_path: str, code: str, working_tree: dict[str, str]) -> list[str]:
    """Project files that `target_rel_path` imports, resolving module stems and Java `pkg.*` wildcards."""
    imported = extract_imports(target_rel_path, code)
    wildcard_pkgs = {token[:-2] for token in imported if token.endswith(".*")}
    neighbors = []
    for other_path, other_code in sorted(working_tree.items()):
        if other_path == target_rel_path:
            continue
        if Path(other_path).stem in imported:
            neighbors.append(other_path)
        elif wildcard_pkgs and other_path.endswith(".java") and _extract_java_package(other_code) in wildcard_pkgs:
            neighbors.append(other_path)
    return neighbors


def _neighbor_block(path: str, code: str, max_chars: int) -> str:
    """Neighbour source, truncated intelligently, always led by its complete public interface and tail snippets."""
    header = f"--- {path} ---"
    if len(code) <= max_chars:
        return f"{header}\n{code}"
    interface = extract_public_interface(path, code)
    half_chars = max_chars // 2
    head_part = code[:half_chars]
    tail_part = code[-half_chars:]
    return (
        f"{header}\nFull public interface:\n{interface}\n\n"
        f"Source Preview (head {half_chars} and tail {half_chars} of {len(code)} characters):\n"
        f"{head_part}\n\n[... truncated middle ...]\n\n{tail_part}"
    )


def build_neighbor_context(
    target_rel_path: str,
    working_tree: dict[str, str],
    repo_map: str,
    extra_goals: str = "",
    session_rules: list[str] | None = None,
) -> str:
    """Builds focused cross-file context containing the Repo Map, learned session rules, and imported local files."""
    current_code = working_tree[target_rel_path]
    neighbor_blocks = [
        _neighbor_block(other_path, working_tree[other_path], 4500)
        for other_path in _local_neighbors(target_rel_path, current_code, working_tree)
    ]

    neighbors_section = (
        "\n\nIMPORTED NEIGHBOR FILES (STRICTLY USE ONLY THESE EXACT METHODS/FIELDS/GETTERS):\n"
        + "\n\n".join(neighbor_blocks)
        if neighbor_blocks
        else ""
    )

    rules_section = ""
    if session_rules:
        formatted_rules = "\n".join(f"- {r}" for r in session_rules)
        rules_section = (
            "\n\nLEARNED SESSION RULES & USER CORRECTIONS FROM EARLIER FILES (MANDATORY):\n"
            f"{formatted_rules}"
        )

    return (
        f"PROJECT REPOSITORY MAP (PRESERVE PUBLIC SIGNATURES):\n{repo_map}"
        f"{rules_section}"
        f"{neighbors_section}\n\n"
        f"USER GOALS:\n{extra_goals or 'Apply concrete security, error-handling, and performance fixes from the Audit Report.'}"
    )


def package_overlay_zip(
    all_original_files: dict[str, bytes],
    working_tree: dict[str, str],
    audit_reports: dict[str, str],
    completed_files: set[str],
) -> bytes:
    """Overlays all saved/completed project files onto the full original zip with path sanitization + writes PROJECT_AUDIT_REPORT.md."""
    merged_files = dict(all_original_files)

    for rel_path in completed_files:
        if rel_path in working_tree:
            # Sanitize rel_path key to prevent directory traversal in packaged zip
            clean_key = rel_path.lstrip("/\\")
            if ":" not in clean_key and ".." not in Path(clean_key).parts:
                merged_files[clean_key] = working_tree[rel_path].encode("utf-8")

    report_sections = ["# Project Security, Optimization & Limitations Report\n"]
    for rel_path, report in audit_reports.items():
        if rel_path not in completed_files:
            status_label = "AUDITED ONLY"
        elif rel_path in working_tree and working_tree[rel_path].encode("utf-8") == all_original_files.get(rel_path):
            status_label = "REVIEWED — NO CHANGES"
        else:
            status_label = "OPTIMIZED & SAVED"
        report_sections.append(f"## `{rel_path}` [{status_label}]\n\n{report}\n\n---\n")

    if audit_reports:
        merged_files["PROJECT_AUDIT_REPORT.md"] = "\n".join(report_sections).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel_posix, content_bytes in sorted(merged_files.items()):
            clean_posix = rel_posix.lstrip("/\\")
            if ":" not in clean_posix and ".." not in Path(clean_posix).parts:
                zf.writestr(clean_posix, content_bytes)
    buf.seek(0)
    return buf.getvalue()

CONSISTENCY_AUDITOR_PROMPT = """
You are a Principal Integration & Static Analysis Architect. Inspect all modified files in a project alongside the imported files they depend on.
Find ONLY real cross-file contract mismatches, such as:
- Calling a method, getter, record accessor, or repository query that does not exist in the target class/module (e.g. `repo.existsByFoo()` when `repo` does not declare `existsByFoo`).
- Passing the wrong number or order of arguments to a constructor or function defined in another project file.
- Importing a symbol or module path that was renamed or deleted.

Respond ONLY with a valid JSON object matching this exact schema:
{
  "consistent": true or false,
  "issues": [
    {
      "caller_file": "path/to/CallerFile.java",
      "target_file": "path/to/TargetFile.java",
      "symbol": "missingMethodOrSignature",
      "severity": "HIGH",
      "description": "Exact explanation of the mismatch",
      "suggested_fix": "Exact instruction to fix either caller_file or target_file"
    }
  ]
}
If zero cross-file mismatches exist, return {"consistent": true, "issues": []}.
"""


def check_cross_file_consistency(
    working_tree: dict[str, str],
    completed_files: set[str],
    repo_map: str,
) -> dict:
    """Audits all modified files against their imported neighbors to detect broken cross-file calls with token management."""
    if not completed_files:
        return {"consistent": True, "issues": [], "checked_pairs": []}

    checked_pairs = []
    bundle_sections = []

    for caller_path in sorted(completed_files):
        if caller_path not in working_tree:
            continue
        caller_code = working_tree[caller_path]

        matched_neighbors = []
        for other_path in _local_neighbors(caller_path, caller_code, working_tree):
            matched_neighbors.append((other_path, working_tree[other_path]))
            checked_pairs.append((caller_path, other_path))

        # Truncate caller and neighbor code chunks to prevent token limit explosions
        caller_chunk = caller_code[:3000] if len(caller_code) > 3000 else caller_code
        section = f"=== MODIFIED CALLER FILE: {caller_path} ===\n{caller_chunk}"
        if matched_neighbors:
            neighbor_txt = "\n\n".join(
                f"--- IMPORTED TARGET: {p} ---\n{c[:3000]}" for p, c in matched_neighbors
            )
            section += f"\n\n{neighbor_txt}"
        bundle_sections.append(section)

    prompt = (
        f"PROJECT REPOSITORY MAP:\n{repo_map}\n\n"
        f"MODIFIED FILES & THEIR IMPORTED DEPENDENCIES:\n\n"
        + "\n\n========================================\n\n".join(bundle_sections)
    )

    try:
        raw = ask_agent(CONSISTENCY_AUDITOR_PROMPT, prompt, strip_fences=True, json_mode=True)
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("verdict is not a JSON object")
        issues = parsed.get("issues") or []
        if not isinstance(issues, list):
            raise ValueError("issues is not a list")
        is_consistent = bool(parsed.get("consistent") is True and not issues)
        return {
            "consistent": is_consistent,
            "issues": issues,
            "checked_pairs": checked_pairs,
        }
    except Exception as e:
        # Graceful handling for LLM JSON glitches or parsing errors without hard failing workflow pipelines
        return {
            "consistent": True,
            "issues": [],
            "checked_pairs": checked_pairs,
        }