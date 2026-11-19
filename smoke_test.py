"""Live integration smoke test for the optimizer's model routing (REAL API calls, ~6 small requests, ~1 min).

    python smoke_test.py

Checks, end to end, with your real .env keys:
  1. the llm_router fallback pool loads (non-Gemini providers)
  2. ask_agent answers via Gemini normally (text + JSON mode)
  3. with every Gemini model forced into cooldown, ask_agent answers via the router (text + JSON mode)
  4. the heavy tier tries HEAVY_OPTIMIZER_MODELS first for large files
Nothing in .env is changed; forced cooldowns are cleared at the end.
"""
import json
import os
import sys
import time

os.environ["LLM_FALLBACK"] = "on"          # make sure a shell/test setting doesn't disable it

import file_optimizer as fo  # noqa: E402

results: list[tuple[str, bool, str]] = []


def last_model_entry() -> dict:
    """Most recent successful optimizer call in agent_metrics.jsonl."""
    try:
        lines = fo.METRICS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("source") == "optimizer" and "error" not in entry:
            return entry
    return {}


def check(name: str, fn) -> None:
    started = time.perf_counter()
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {str(e)[:200]}"
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  ({time.perf_counter() - started:.1f}s)\n      {detail}")


def all_gemini_models() -> list[str]:
    return list(dict.fromkeys(fo.HEAVY_OPTIMIZER_MODELS + fo.OPTIMIZER_MODELS))


# 1 ---------------------------------------------------------------------------------------------
def router_pool():
    status = fo.fallback_status()
    providers = [s["provider"] for s in status]
    if not providers:
        return False, "no fallback pool: is `openai` installed and is there a non-Gemini key in .env?"
    if "gemini" in providers:
        return False, f"Gemini should not be in the fallback pool: {providers}"
    return True, "providers in fallback order: " + ", ".join(f"{s['provider']}({s['state']})" for s in status)


# 2 ---------------------------------------------------------------------------------------------
def gemini_text():
    reply = fo.ask_agent("You are a test endpoint. Reply with exactly the word: pong", "ping")
    model = last_model_entry().get("model", "?")
    return model.startswith("gemini") and "pong" in reply.lower(), f"model={model} reply={reply[:60]!r}"


def gemini_json():
    reply = fo.ask_agent('Return {"approved": true, "reason": "smoke"} as JSON.', "go",
                         strip_fences=False, json_mode=True)
    data = json.loads(fo._strip_code_fences(reply))
    model = last_model_entry().get("model", "?")
    return model.startswith("gemini") and data.get("approved") is True, f"model={model} json={data}"


# 3 ---------------------------------------------------------------------------------------------
def force_gemini_down():
    for m in all_gemini_models():
        fo._set_cooldown(m, 600, "smoke test")


def router_text():
    force_gemini_down()
    reply = fo.ask_agent("You are a test endpoint. Reply with exactly the word: pong", "ping")
    model = last_model_entry().get("model", "?")
    return ":" in model and "pong" in reply.lower(), f"answered by {model} reply={reply[:60]!r}"


def router_json():
    force_gemini_down()
    reply = fo.ask_agent('Return {"approved": true, "reason": "smoke"} as JSON.', "go",
                         strip_fences=False, json_mode=True)
    data = json.loads(fo._strip_code_fences(reply))
    model = last_model_entry().get("model", "?")
    return ":" in model and data.get("approved") is True, f"answered by {model} json={data}"


# 4 ---------------------------------------------------------------------------------------------
def heavy_tier():
    fo._model_cooldown.clear(); fo._cooldown_reason.clear()
    if not fo.HEAVY_OPTIMIZER_MODELS:
        return True, "heavy tier disabled (OPTIMIZER_HEAVY_MODELS is empty) — skipped"
    with fo.use_heavy_models():
        order = fo.active_optimizer_models()
        fo.ask_agent("Reply with exactly: ok", "ok")
    model = last_model_entry().get("model", "?")
    heavy = fo.HEAVY_OPTIMIZER_MODELS[0]
    if model == heavy:
        return True, f"answered by heavy model {model}"
    cd = fo.model_cooldowns().get(heavy)
    if cd:   # heavy model busy -> correctly cooled down and fell back
        return True, f"{heavy} unavailable ({cd['reason']}), fell back to {model} as designed"
    return False, f"expected {heavy} first; order={order}, answered by {model}"


def large_file_detection():
    path = fo.PROJECT_ROOT / "agent.py"
    if not path.exists():
        return True, "agent.py not found — skipped"
    code = path.read_text(encoding="utf-8")
    lines = code.count("\n") + 1
    return fo.is_large_file(code) == (lines >= fo.LARGE_FILE_LINES), \
        f"agent.py: {lines} lines, threshold {fo.LARGE_FILE_LINES} -> large={fo.is_large_file(code)}"


if __name__ == "__main__":
    print(f"Light models: {fo.OPTIMIZER_MODELS}\nHeavy models: {fo.HEAVY_OPTIMIZER_MODELS}\n")
    try:
        check("1. fallback pool loads", router_pool)
        check("2a. Gemini answers (text)", gemini_text)
        check("2b. Gemini answers (JSON mode)", gemini_json)
        check("3a. Gemini down -> router answers (text)", router_text)
        check("3b. Gemini down -> router answers (JSON mode)", router_json)
        check("4a. large file detection", large_file_detection)
        check("4b. heavy tier tries heavy model first", heavy_tier)
    finally:
        fo._model_cooldown.clear(); fo._cooldown_reason.clear()   # never leave forced cooldowns behind
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} checks passed.")
    sys.exit(0 if passed == len(results) else 1)