"""Pure rendering helpers for app.py: they build data or HTML strings and never call Streamlit,
so they are unit-testable. Colours are semi-transparent so they read on Streamlit's light and dark themes."""
import difflib
import html
import ipaddress
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from file_optimizer import PIPELINE_STAGES

# ---------------------------------------------------------------- shared CSS

APP_CSS = """
<style>
.jb-stepper{display:flex;flex-wrap:wrap;gap:6px;align-items:stretch;margin:4px 0 12px}
.jb-iter{font-size:.78rem;opacity:.75;align-self:center;margin-right:4px;white-space:nowrap}
.jb-step{flex:1 1 110px;min-width:110px;border-radius:10px;padding:8px 10px;border:1px solid rgba(128,128,128,.25);
  background:rgba(128,128,128,.06)}
.jb-step .jb-step-top{display:flex;gap:6px;align-items:center;font-weight:600;font-size:.84rem}
.jb-step .jb-step-detail{font-size:.72rem;opacity:.75;margin-top:3px;overflow:hidden;text-overflow:ellipsis;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.jb-step.passed{border-color:rgba(22,163,74,.55);background:rgba(22,163,74,.10)}
.jb-step.failed{border-color:rgba(220,38,38,.6);background:rgba(220,38,38,.10)}
.jb-step.running{border-color:rgba(37,99,235,.6);background:rgba(37,99,235,.10)}
.jb-step.skipped{opacity:.55}
.jb-step.running .jb-icon{animation:jb-pulse 1s ease-in-out infinite}
@keyframes jb-pulse{50%{opacity:.25}}
.jb-sources{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px;margin:6px 0 4px}
.jb-source{display:block;border:1px solid rgba(128,128,128,.25);border-radius:10px;padding:8px 10px;
  text-decoration:none!important;color:inherit!important;background:rgba(128,128,128,.05)}
.jb-source:hover{border-color:rgba(37,99,235,.6);background:rgba(37,99,235,.08)}
.jb-source .jb-src-title{font-size:.83rem;font-weight:600;line-height:1.25;overflow:hidden;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.jb-source .jb-src-domain{font-size:.72rem;opacity:.65;margin-top:3px}
.jb-tree{font-size:.86rem}
.jb-tree .jb-folder{font-weight:600;opacity:.8;margin:8px 0 2px}
.jb-tree .jb-file{display:flex;gap:8px;align-items:baseline;padding:3px 0 3px 10px}
.jb-tree .jb-idx{font-variant-numeric:tabular-nums;opacity:.55;min-width:2.2em;text-align:right}
.jb-tree .jb-name{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.jb-tree .jb-deps{font-size:.72rem;opacity:.6}
.jb-badge{font-size:.7rem;font-weight:600;border-radius:999px;padding:1px 8px;white-space:nowrap;
  border:1px solid currentColor}
.jb-legend{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}
.jb-diff{max-height:620px;overflow:auto;border:1px solid rgba(128,128,128,.25);border-radius:10px}
.jb-diff table{border-collapse:collapse;width:100%;table-layout:fixed;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.78rem}
.jb-diff th{position:sticky;top:0;z-index:1;text-align:left;padding:6px 10px;font-family:inherit;font-weight:600;
  background:rgba(128,128,128,.22);backdrop-filter:blur(6px)}
.jb-diff td{padding:1px 8px;vertical-align:top}
.jb-diff td.jb-ln{text-align:right;opacity:.5;user-select:none;font-variant-numeric:tabular-nums}
.jb-diff td.jb-code{white-space:pre-wrap;word-break:break-word}
.jb-diff td.jb-del{background:rgba(220,38,38,.18)}
.jb-diff td.jb-add{background:rgba(22,163,74,.20)}
.jb-diff td.jb-empty{background:rgba(128,128,128,.07)}
.jb-diff tr.jb-gap td{text-align:center;opacity:.5;background:rgba(128,128,128,.08);padding:2px}
</style>
"""

# ---------------------------------------------------------------- pipeline stepper

_ICONS = {"pending": "○", "running": "◐", "passed": "✓", "failed": "✕", "skipped": "–"}
_LOOP_STAGES = ("coder", "gate", "regression", "verify", "review")


def new_stepper_state() -> dict:
    return {"iteration": "", "stages": {sid: {"state": "pending", "detail": ""} for sid, _ in PIPELINE_STAGES}}


def apply_progress(state: dict, stage: str, status: str, detail: str = "") -> dict:
    """Folds one optimize_single_file progress event into the stepper state (and returns it)."""
    if stage == "iteration":
        state["iteration"] = detail
        for sid in _LOOP_STAGES:  # a new Coder iteration starts these stages over
            state["stages"][sid] = {"state": "pending", "detail": ""}
    elif stage in state["stages"]:
        state["stages"][stage] = {"state": status if status in _ICONS else "pending", "detail": detail or ""}
    return state


def stepper_html(state: dict) -> str:
    parts = ['<div class="jb-stepper">']
    if state.get("iteration"):
        parts.append(f'<span class="jb-iter">Iteration {html.escape(state["iteration"])}</span>')
    for sid, label in PIPELINE_STAGES:
        step = state["stages"].get(sid, {"state": "pending", "detail": ""})
        status = step["state"]
        detail = html.escape(step["detail"][:140])
        parts.append(
            f'<div class="jb-step {status}" title="{detail}">'
            f'<div class="jb-step-top"><span class="jb-icon">{_ICONS.get(status, "○")}</span>{html.escape(label)}</div>'
            f'<div class="jb-step-detail">{detail or "&nbsp;"}</div></div>'
        )
    parts.append("</div>")
    return "".join(parts)


# ---------------------------------------------------------------- side-by-side diff

def side_by_side_diff_html(original: str, updated: str, from_label: str = "Original", to_label: str = "Optimized") -> str:
    """Two-column diff with 3 lines of context around each change; all file text is HTML-escaped."""
    if original == updated:
        return '<div class="jb-diff" style="padding:12px">No changes.</div>'
    old_lines, new_lines = original.splitlines(), updated.splitlines()

    def cell(num, text, kind):
        if num is None:
            return '<td class="jb-ln"></td><td class="jb-code jb-empty"></td>'
        return f'<td class="jb-ln">{num}</td><td class="jb-code {kind}">{html.escape(text) or "&nbsp;"}</td>'

    rows = []
    for g, group in enumerate(difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_grouped_opcodes(3)):
        if g:
            rows.append('<tr class="jb-gap"><td colspan="4">⋯</td></tr>')
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(i2 - i1):
                    rows.append(f"<tr>{cell(i1 + k + 1, old_lines[i1 + k], '')}{cell(j1 + k + 1, new_lines[j1 + k], '')}</tr>")
                continue
            left = [(i + 1, old_lines[i]) for i in range(i1, i2)]
            right = [(j + 1, new_lines[j]) for j in range(j1, j2)]
            for k in range(max(len(left), len(right))):
                l_num, l_txt = left[k] if k < len(left) else (None, "")
                r_num, r_txt = right[k] if k < len(right) else (None, "")
                rows.append(f"<tr>{cell(l_num, l_txt, 'jb-del')}{cell(r_num, r_txt, 'jb-add')}</tr>")
    head = (f'<thead><tr><th colspan="2">{html.escape(from_label)}</th>'
            f'<th colspan="2">{html.escape(to_label)}</th></tr></thead>')
    cols = '<colgroup><col style="width:3.6em"><col><col style="width:3.6em"><col></colgroup>'
    return f'<div class="jb-diff"><table>{cols}{head}<tbody>{"".join(rows)}</tbody></table></div>'


# ---------------------------------------------------------------- source cards

def _safe_http_url(url: str) -> str | None:
    trimmed = (url or "").strip()
    parsed = urlparse(trimmed)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    host = parsed.hostname
    if not host:
        return None
    if host.lower() in ("localhost", "127.0.0.1", "::1", "169.254.169.254", "metadata.google.internal"):
        return None
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return None
    except ValueError:
        pass
    return trimmed


def source_cards_html(sources: list[dict]) -> str:
    """Clickable cards for the agent's sources. Non-http(s) URLs are dropped (no javascript: links)."""
    cards = []
    for src in sources or []:
        url = _safe_http_url(str(src.get("url", "")))
        if not url:
            continue
        domain = urlparse(url).netloc.removeprefix("www.")
        title = str(src.get("title") or domain)
        cards.append(
            f'<a class="jb-source" href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">'
            f'<div class="jb-src-title">{html.escape(title)}</div>'
            f'<div class="jb-src-domain">{html.escape(domain)}</div></a>'
        )
    return f'<div class="jb-sources">{"".join(cards)}</div>' if cards else ""


# ---------------------------------------------------------------- project file tree

FILE_STATUSES = {
    "optimized": ("Optimized", "#16a34a"),
    "reviewed": ("Reviewed, no changes", "#2563eb"),
    "ready": ("Approved, not accepted", "#0d9488"),
    "needs_review": ("Needs review", "#d97706"),
    "audited": ("Audited", "#7c3aed"),
    "pending": ("Pending", "#6b7280"),
}


def file_status(path: str, completed: set, audits: dict, results: dict, working_tree: dict, original: dict) -> str:
    if path in completed:
        return "reviewed" if working_tree.get(path) == original.get(path) else "optimized"
    if path in results:
        return "ready" if results[path].get("approved") else "needs_review"
    if path in audits:
        return "audited"
    return "pending"


def _badge(status: str) -> str:
    label, color = FILE_STATUSES.get(status, ("Pending", "#6b7280"))
    return f'<span class="jb-badge" style="color:{html.escape(color, quote=True)}">{html.escape(label)}</span>'


def status_legend_html(counts: Counter) -> str:
    return '<div class="jb-legend">' + "".join(
        f'{_badge(s)}<span style="font-size:.75rem;opacity:.7;margin-right:6px">{counts.get(s, 0)}</span>'
        for s in FILE_STATUSES
    ) + "</div>"


def file_tree_html(ordered_files: list[str], statuses: dict[str, str], dep_map: dict[str, list[str]],
                   active: str | None = None) -> str:
    """Files grouped by folder, each with its position in dependency order, status badge and local imports."""
    position = {path: i for i, path in enumerate(ordered_files, start=1)}
    folders: dict[str, list[str]] = defaultdict(list)
    for path in ordered_files:
        norm_path = str(path).replace("\\", "/")
        parent_dir = str(PurePosixPath(norm_path).parent)
        folders[parent_dir].append(path)

    rows = ['<div class="jb-tree">']
    for folder in sorted(folders, key=lambda f: min(position[p] for p in folders[f])):
        rows.append(f'<div class="jb-folder">📁 {html.escape(folder if folder != "." else "(project root)")}</div>')
        for path in folders[folder]:
            deps = [Path(d).name for d in dep_map.get(path, [])]
            dep_txt = f'<span class="jb-deps">← {html.escape(", ".join(deps))}</span>' if deps else ""
            weight = "font-weight:700;" if path == active else ""
            pointer = " ◀" if path == active else ""
            rows.append(
                f'<div class="jb-file"><span class="jb-idx">#{position[path]}</span>'
                f'{_badge(statuses.get(path, "pending"))}'
                f'<span class="jb-name" style="{weight}">{html.escape(Path(path).name)}{pointer}</span>{dep_txt}</div>'
            )
    rows.append("</div>")
    return "".join(rows)


# ---------------------------------------------------------------- telemetry

def summarize_metrics(entries: list[dict]) -> dict:
    """Splits telemetry into model requests, tool calls and errors.
    A 'request' is a successful model call (lines without an error that are not tool calls)."""
    requests = []
    tools = []
    errors = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if "error" in e:
            errors.append(e)
        elif e.get("source") == "tool":
            tools.append(e)
        else:
            requests.append(e)

    req_latencies = [float(e["latency_sec"]) for e in requests if isinstance(e.get("latency_sec"), (int, float)) and e.get("latency_sec") is not None]
    total_latency = sum(req_latencies)
    avg_latency = (total_latency / len(req_latencies)) if req_latencies else 0.0

    total_tokens = sum(int(e["total_tokens"]) for e in requests if isinstance(e.get("total_tokens"), (int, float)) and e.get("total_tokens") is not None)

    by_source_counter = Counter()
    for e in requests:
        by_source_counter[e.get("source") or "unlabelled"] += 1

    tool_failures = sum(1 for e in tools if e.get("ok") is False)

    tools_by_name_counter = Counter()
    for e in tools:
        tools_by_name_counter[e.get("tool") or "?"] += 1

    errors_by_model_counter = Counter()
    for e in errors:
        errors_by_model_counter[e.get("model") or "?"] += 1

    last_error = errors[-1].get("error", "") if errors else ""

    token_series = [
        {"timestamp": e["timestamp"], "source": e.get("source") or "unlabelled", "tokens": int(e["total_tokens"])}
        for e in requests
        if e.get("timestamp") and isinstance(e.get("total_tokens"), (int, float)) and e.get("total_tokens") is not None
    ]

    return {
        "requests": len(requests),
        "avg_latency": avg_latency,
        "total_tokens": total_tokens,
        "by_source": dict(by_source_counter),
        "tool_calls": len(tools),
        "tool_failures": tool_failures,
        "tools_by_name": dict(tools_by_name_counter),
        "errors_by_model": dict(errors_by_model_counter),
        "last_error": last_error,
        "token_series": token_series,
    }