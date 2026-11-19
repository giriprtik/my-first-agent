"""Pure rendering helpers for app.py: they build data or HTML strings and never call Streamlit,
so they are unit-testable. Colours are semi-transparent so they read on Streamlit's light and dark themes."""
import difflib
import html
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
.jb-findcount{font-size:.68rem;font-weight:700;border-radius:999px;padding:0 7px;white-space:nowrap}
.jb-scanners{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:8px;margin:4px 0 10px}
.jb-scanner{border:1px solid rgba(128,128,128,.25);border-radius:10px;padding:8px 10px;background:rgba(128,128,128,.05)}
.jb-scanner .jb-sc-top{display:flex;justify-content:space-between;gap:6px;font-weight:600;font-size:.84rem}
.jb-scanner .jb-sc-detail{font-size:.72rem;opacity:.75;margin-top:3px;word-break:break-word}
.jb-scanner.ok{border-color:rgba(22,163,74,.5)}
.jb-scanner.missing{border-style:dashed;opacity:.85}
.jb-scanner.error,.jb-scanner.timeout{border-color:rgba(220,38,38,.6);background:rgba(220,38,38,.08)}
.jb-sc-extra{font-size:.74rem;opacity:.8;margin:-4px 0 8px;line-height:1.5}
.jb-sc-extra code{font-size:.7rem}
.jb-sev{display:flex;flex-wrap:wrap;gap:6px;margin:2px 0 8px}
.jb-recheck{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px;margin:4px 0 10px}
.jb-recheck .jb-col{border:1px solid rgba(128,128,128,.25);border-radius:10px;padding:8px 10px}
.jb-recheck .jb-col h5{margin:0 0 4px;font-size:.82rem}
.jb-recheck .jb-col div{font-size:.74rem;line-height:1.35;margin:2px 0;word-break:break-word}
.jb-recheck .good{border-color:rgba(22,163,74,.55);background:rgba(22,163,74,.07)}
.jb-recheck .bad{border-color:rgba(220,38,38,.6);background:rgba(220,38,38,.07)}
.jb-recheck .warn{border-color:rgba(217,119,6,.6);background:rgba(217,119,6,.07)}
.jb-sev span{font-size:.75rem;font-weight:600;border-radius:999px;padding:2px 10px;border:1px solid currentColor}
</style>
"""

# ---------------------------------------------------------------- pipeline stepper

_ICONS = {"pending": "○", "running": "◐", "passed": "✓", "failed": "✕", "skipped": "–"}
_LOOP_STAGES = ("coder", "gate", "regression", "verify", "rescan", "review")


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
    parsed = urlparse((url or "").strip())
    return url.strip() if parsed.scheme in ("http", "https") and parsed.netloc else None


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
    label, color = FILE_STATUSES[status]
    return f'<span class="jb-badge" style="color:{color}">{html.escape(label)}</span>'


def status_legend_html(counts: Counter) -> str:
    return '<div class="jb-legend">' + "".join(
        f'{_badge(s)}<span style="font-size:.75rem;opacity:.7;margin-right:6px">{counts.get(s, 0)}</span>'
        for s in FILE_STATUSES
    ) + "</div>"


def file_tree_html(ordered_files: list[str], statuses: dict[str, str], dep_map: dict[str, list[str]],
                   active: str | None = None, finding_counts: dict[str, tuple[int, str]] | None = None) -> str:
    """Files grouped by folder, each with its position in dependency order, status badge and local imports.

    finding_counts maps a path to (number of scanner findings, worst severity) and adds a coloured counter."""
    finding_counts = finding_counts or {}
    position = {path: i for i, path in enumerate(ordered_files, start=1)}
    folders: dict[str, list[str]] = defaultdict(list)
    for path in ordered_files:
        folders[str(PurePosixPath(path).parent)].append(path)

    rows = ['<div class="jb-tree">']
    for folder in sorted(folders, key=lambda f: min(position[p] for p in folders[f])):
        rows.append(f'<div class="jb-folder">📁 {html.escape(folder if folder != "." else "(project root)")}</div>')
        for path in folders[folder]:
            deps = [Path(d).name for d in dep_map.get(path, [])]
            dep_txt = f'<span class="jb-deps">← {html.escape(", ".join(deps))}</span>' if deps else ""
            weight = "font-weight:700;" if path == active else ""
            pointer = " ◀" if path == active else ""
            count_txt = ""
            if finding_counts.get(path):
                n, worst = finding_counts[path]
                color = SEVERITY_COLORS.get(worst, "#6b7280")
                count_txt = (f'<span class="jb-findcount" style="color:{color};border:1px solid {color}" '
                             f'title="{n} scanner finding(s), worst: {html.escape(worst)}">🛡 {n}</span>')
            rows.append(
                f'<div class="jb-file"><span class="jb-idx">#{position[path]}</span>'
                f'{_badge(statuses.get(path, "pending"))}'
                f'<span class="jb-name" style="{weight}">{html.escape(Path(path).name)}{pointer}</span>{count_txt}{dep_txt}</div>'
            )
    rows.append("</div>")
    return "".join(rows)


# ---------------------------------------------------------------- security scanners

SEVERITY_COLORS = {"critical": "#dc2626", "high": "#ea580c", "medium": "#ca8a04", "low": "#2563eb", "info": "#6b7280"}
_SCANNER_STATE = {"ok": "✓ ran", "missing": "not installed", "error": "✕ failed", "timeout": "⏱ timed out"}


def scanner_status_html(tools: dict[str, dict], labels: dict[str, str] | None = None) -> str:
    """One card per scanner that ran (or failed); tools that were not needed or not installed go on one line each."""
    labels = labels or {}
    cards, missing, skipped = [], [], []
    for name, info in tools.items():
        status = info.get("status", "error")
        label = html.escape(labels.get(name, name))
        if status == "missing":
            missing.append(f"<b>{label}</b> <code>{html.escape(info.get('detail', ''))}</code>")
            continue
        if status == "skipped":
            skipped.append(f"{label} ({html.escape(info.get('detail', ''))})")
            continue
        detail = (f"{info.get('count', 0)} finding(s) in {info.get('seconds', 0)}s" if status == "ok"
                  else info.get("detail", "") or status)
        if info.get("note"):
            detail += f" · {info['note']}"
        cards.append(
            f'<div class="jb-scanner {html.escape(status)}"><div class="jb-sc-top">'
            f'<span>{label}</span><span>{html.escape(_SCANNER_STATE.get(status, status))}</span></div>'
            f'<div class="jb-sc-detail">{html.escape(detail[:300])}</div></div>'
        )
    out = f'<div class="jb-scanners">{"".join(cards)}</div>' if cards else ""
    if missing:
        out += '<div class="jb-sc-extra">Not installed: ' + " · ".join(missing) + "</div>"
    if skipped:
        out += '<div class="jb-sc-extra">Not needed for this code: ' + ", ".join(skipped) + "</div>"
    return out


def severity_summary_html(counts: dict[str, int]) -> str:
    chips = [f'<span style="color:{SEVERITY_COLORS[s]}">{s.capitalize()} {counts.get(s, 0)}</span>'
             for s in SEVERITY_COLORS if counts.get(s)]
    return f'<div class="jb-sev">{"".join(chips) or "<span>No findings</span>"}</div>'


def finding_counts_by_file(findings) -> dict[str, tuple[int, str]]:
    """{path: (count, worst severity)} for the file tree."""
    order = list(SEVERITY_COLORS)
    out: dict[str, tuple[int, str]] = {}
    for f in findings:
        n, worst = out.get(f.file, (0, "info"))
        if order.index(f.severity) < order.index(worst):
            worst = f.severity
        out[f.file] = (n + 1, worst)
    return out


def recheck_html(check: dict | None) -> str:
    """Resolved / still present / new findings after the optimizer's scanner re-check."""
    if not check:
        return ""
    if check.get("status") != "ok":
        return (f'<div class="jb-recheck"><div class="jb-col"><h5>🛡 Scanner re-check skipped</h5>'
                f'<div>{html.escape(str(check.get("detail", ""))[:300])}</div></div></div>')

    def items(findings, empty):
        rows = [f'<div><b>{html.escape(f.severity)}</b> · {html.escape(f.rule_id.rsplit(".", 1)[-1])}'
                f'{" · line " + str(f.line) if f.line else ""}</div>' for f in findings[:12]]
        if len(findings) > 12:
            rows.append(f"<div>… and {len(findings) - 12} more</div>")
        return "".join(rows) or f'<div style="opacity:.6">{empty}</div>'

    remaining, new = check["remaining"], check["introduced"]
    other = [f for f in check["still_present"] if f not in remaining]
    cols = [
        ("good", f"✓ Resolved ({len(check['resolved'])})", items(check["resolved"], "None")),
        ("bad" if remaining else "good", f"Must-fix still present ({len(remaining)}/{check['must_fix_count']})",
         items(remaining, "None — all must-fix findings are gone")),
        ("bad" if check["blocking_introduced"] else ("warn" if new else "good"), f"New findings ({len(new)})",
         items(new, "None introduced")),
    ]
    if other:
        cols.append(("warn", f"Not required, still present ({len(other)})", items(other, "")))
    if check.get("suppressions_added"):
        cols.append(("bad", "Suppression comments added", f"<div>{check['suppressions_added']} — not allowed</div>"))
    return '<div class="jb-recheck">' + "".join(
        f'<div class="jb-col {cls}"><h5>{html.escape(title)}</h5>{body}</div>' for cls, title, body in cols) + "</div>"


# ---------------------------------------------------------------- telemetry

def summarize_metrics(entries: list[dict]) -> dict:
    """Splits telemetry into model requests, tool calls and errors.
    A 'request' is a successful model call (lines without an error that are not tool calls)."""
    requests = [e for e in entries if "error" not in e and e.get("source") != "tool"]
    tools = [e for e in entries if e.get("source") == "tool"]
    errors = [e for e in entries if "error" in e]
    return {
        "requests": len(requests),
        "avg_latency": (sum(e.get("latency_sec") or 0 for e in requests) / len(requests)) if requests else 0.0,
        "total_tokens": sum(e.get("total_tokens") or 0 for e in requests),
        "by_source": dict(Counter(e.get("source") or "unlabelled" for e in requests)),
        "tool_calls": len(tools),
        "tool_failures": sum(1 for e in tools if e.get("ok") is False),
        "tools_by_name": dict(Counter(e.get("tool") or "?" for e in tools)),
        "errors_by_model": dict(Counter(e.get("model") or "?" for e in errors)),
        "last_error": errors[-1].get("error", "") if errors else "",
        "token_series": [
            {"timestamp": e["timestamp"], "source": e.get("source") or "unlabelled", "tokens": e.get("total_tokens") or 0}
            for e in requests if e.get("timestamp")
        ],
    }
