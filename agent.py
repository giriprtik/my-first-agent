import os
import re
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv
from google import genai
from google.genai import types
from tavily import TavilyClient
from tools import save_research_note  # Import your sandboxed tool

# 1. Load environment variables
load_dotenv()

METRICS_PATH = Path(__file__).parent / "agent_metrics.jsonl"

# Clients are created on first use, so this module (and app.py, which imports it) can be imported
# and tested without API keys. Tests can still replace them with patch.object(agent, "gemini_client").
gemini_client = None
tavily_client = None

DEFAULT_TOOL_BUDGET = 6      # tool calls allowed per question in the streaming (UI) loop
MAX_PAGE_CHARS = 8000        # read_page returns at most this much page text to the model


def _gemini():
    global gemini_client
    if gemini_client is None:
        gemini_client = genai.Client(
            http_options=types.HttpOptions(
                timeout=30000,  # milliseconds
                retry_options=types.HttpRetryOptions(attempts=1),
            )
        )
    return gemini_client


def _tavily():
    global tavily_client
    if tavily_client is None:
        tavily_client = TavilyClient(api_key=os.environ.get("TAVILY_API_KEY", ""))
    return tavily_client


def _format_results(response: dict) -> str:
    results = []
    for item in response.get("results", []) or []:
        if not isinstance(item, dict) or not (item.get("title") or item.get("url") or item.get("content")):
            continue
        results.append(
            f"Title: {item.get('title', '')}\nURL: {item.get('url', '')}\nContent: {item.get('content', '')}\n"
        )
    return "\n---\n".join(results)


# 2. Define the Web Research Tools
def search_web(query: str) -> str:
    """Searches the live web for recent news, facts, and current events."""
    print(f"[TOOL LOG] Searching web for: '{query}'")
    response = _tavily().search(query=query, max_results=3)
    return _format_results(response)


def deep_search(query: str) -> str:
    """Thorough web search for complex, comparative or multi-part questions. Slower than search_web.
    Returns a short synthesized answer followed by up to 5 sources with their URLs."""
    print(f"[TOOL LOG] Deep search for: '{query}'")
    response = _tavily().search(query=query, search_depth="advanced", include_answer=True, max_results=5)
    answer = (response.get("answer") or "").strip()
    results = _format_results(response)
    if answer and results:
        return f"Summary answer: {answer}\n\n---\n{results}"
    if answer:
        return f"Summary answer: {answer}"
    return results


def read_page(url: str) -> str:
    """Reads the main text of one web page, for example a source URL returned by a search,
    when the search snippet is not enough to answer accurately. Accepts full http(s) URLs only."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "Tool error: read_page only accepts full http(s) URLs, e.g. https://example.com/article."
    print(f"[TOOL LOG] Reading page: {url}")
    response = _tavily().extract(urls=[url], format="markdown")
    for item in response.get("results", []) or []:
        content = (item.get("raw_content") or "").strip() if isinstance(item, dict) else ""
        if content:
            if len(content) > MAX_PAGE_CHARS:
                content = (
                    content[:MAX_PAGE_CHARS]
                    + f"\n\n[Truncated: showing the first {MAX_PAGE_CHARS} of {len(content)} characters.]"
                )
            return f"URL: {item.get('url') or url}\n\n{content}"
    return f"Could not read {url}: the page returned no readable text."


# 3. Configure the Agent's Brain & Guidelines
system_prompt = """
You are a precise Research Assistant with restricted file-saving access.
- Use `search_web` whenever the user asks about recent events, live data, or facts you are unsure about.
- Do NOT use `search_web` for simple greetings, basic math, or follow-up questions answerable from chat history.
- Only use `save_research_note` when the user explicitly asks you to save, write, or export a note/file.
- Never attempt to overwrite existing files or access directories outside your sandbox.
- Always summarize findings in 3 concise bullet points and include source URLs.
- If the search returns no useful data, say "I could not find verified information on that."
"""

# Used by the streaming loop, which also offers deep_search and read_page and has a per-question tool budget
AGENT_SYSTEM_PROMPT = system_prompt + """
- Use `deep_search` instead of `search_web` for complex, comparative or multi-part questions.
- Use `read_page` on a source URL when a snippet is too thin to answer accurately. Read at most 2 pages per question.
- You have a limited number of tool calls per question. When a tool reports the budget is exhausted, answer with what you have.
- When you save a note, the user confirms the save in the interface; tell them it is ready to confirm.
"""

MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-4-hydrogen",
    "gemini-3.7-flash"
]

_models_lock = threading.Lock()
_validated_models: list[str] | None = None
_unavailable_models: list[str] = []


def active_models() -> list[str]:
    """MODELS filtered to the ones the API actually serves, checked once per process with models.list().

    Falls back to the full MODELS list (without caching) if the check itself fails, e.g. offline,
    so a listing error never blocks chatting."""
    global _validated_models, _unavailable_models
    with _models_lock:
        if _validated_models is not None:
            return list(_validated_models)
        try:
            available = {
                str(getattr(m, "name", "") or "").removeprefix("models/") for m in _gemini().models.list()
            }
        except Exception as e:
            print(f"[WARNING] Could not validate model names: {e}")
            return list(MODELS)
        valid = [m for m in MODELS if m in available]
        if not valid:
            print("[WARNING] None of MODELS appear in models.list(); using MODELS unchanged.")
            return list(MODELS)
        _unavailable_models = [m for m in MODELS if m not in available]
        if _unavailable_models:
            print(f"[WARNING] Skipping models the API does not list: {', '.join(_unavailable_models)}")
        _validated_models = valid
        return list(valid)


def model_status() -> dict:
    """For the UI: which configured models are in use, and which were dropped as unavailable."""
    active = active_models()
    return {"active": active, "unavailable": list(_unavailable_models), "validated": _validated_models is not None}


def cached_model_status() -> dict:
    """Like model_status() but never calls the API: reports only what an earlier check found.
    Safe to call on every page render."""
    with _models_lock:
        validated = _validated_models is not None
        return {
            "active": list(_validated_models) if validated else list(MODELS),
            "unavailable": list(_unavailable_models),
            "validated": validated,
        }


def create_agent_session(model_name: str, history=None, manual_tools: bool = False):
    """Creates a chat. manual_tools=True disables the SDK's automatic function calling so stream_reply()
    can run tools itself (budget, confirmation, live progress) and adds deep_search/read_page."""
    if manual_tools:
        chat = _gemini().chats.create(
            model=model_name,
            history=history,
            config=types.GenerateContentConfig(
                system_instruction=AGENT_SYSTEM_PROMPT,
                tools=[search_web, deep_search, read_page, save_research_note],
                temperature=0.2,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        chat._jb_manual_tools = True
        return chat
    return _gemini().chats.create(
        model=model_name,
        history=history,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[search_web, save_research_note],  # Both tools attached here
            temperature=0.2,
        ),
    )


def _append_metrics(entry: dict) -> None:
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **entry}
    try:
        with open(METRICS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        print(f"[WARNING] Could not write telemetry: {e}")


def _log_metrics(model_name: str, latency: float, response) -> None:
    """Prints and appends one telemetry line. Never raises: a logging failure must not count as a model failure."""
    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = (getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
    candidates_tokens = (getattr(usage, "candidates_token_count", 0) or 0) if usage else 0
    total_tokens = (getattr(usage, "total_token_count", 0) or 0) if usage else 0

    print(f"[METRICS] Model: {model_name} | Latency: {latency:.2f}s | Tokens: {total_tokens} (in: {prompt_tokens}, out: {candidates_tokens})")

    _append_metrics({
        "source": "chat",
        "model": model_name,
        "latency_sec": latency,
        "prompt_tokens": prompt_tokens,
        "candidates_tokens": candidates_tokens,
        "total_tokens": total_tokens,
    })


def _log_error(model_name: str, error: Exception) -> None:
    _append_metrics({"source": "chat", "model": model_name, "error": f"{type(error).__name__}: {error}"[:300]})


def send_with_fallback(chat, prompt: str):
    for model_name in MODELS:
        print(f"[STATUS] Trying {model_name}...")
        try:
            if getattr(chat, "_model", None) != model_name:
                chat = create_agent_session(model_name, history=chat.get_history())

            start_time = time.perf_counter()
            response = chat.send_message(prompt)
            latency = time.perf_counter() - start_time
            text = response.text
        except Exception as e:
            print(f"[WARNING] {model_name} failed: {e}")
            _log_error(model_name, e)
            continue

        # Outside the try: once a model has answered, nothing below may trigger a retry on another model,
        # because the retry would replay the prompt and repeat its tool calls (searches, note saves).
        _log_metrics(model_name, latency, response)
        return chat, text
    raise RuntimeError("All models failed. See warnings above.")


# --- Streaming agent loop (used by the Streamlit chat) -------------------------------------------

_RESEARCH_TOOLS = {"search_web": search_web, "deep_search": deep_search, "read_page": read_page}
_SOURCE_PATTERN = re.compile(r"Title: (?P<title>[^\n]*)\nURL: (?P<url>\S+)")


def _extract_sources(tool_name: str, args: dict, output: str) -> list[dict]:
    if tool_name == "read_page":
        url = str(args.get("url", "")).strip()
        return [{"title": urlparse(url).netloc or url, "url": url}] if url.startswith("http") else []
    return [
        {"title": m.group("title").strip() or m.group("url"), "url": m.group("url")}
        for m in _SOURCE_PATTERN.finditer(output or "")
    ]


def _iter_parts(chunk):
    candidates = getattr(chunk, "candidates", None) or []
    if candidates:
        content = getattr(candidates[0], "content", None)
        yield from (getattr(content, "parts", None) or [])


def _open_stream(chat, message):
    """Tries each validated model until one starts streaming. Only the FIRST chunk is awaited inside the
    fallback, so a model is never retried after it has produced output (no duplicated tool calls)."""
    last_error = None
    history = chat.get_history() if hasattr(chat, "get_history") else []
    for model_name in active_models():
        try:
            if getattr(chat, "_model", None) != model_name or not getattr(chat, "_jb_manual_tools", False):
                chat = create_agent_session(model_name, history=history, manual_tools=True)
            stream = iter(chat.send_message_stream(message))
            first = next(stream, None)
            return chat, model_name, first, stream
        except Exception as e:
            print(f"[WARNING] {model_name} failed: {e}")
            _log_error(model_name, e)
            last_error = e
    raise RuntimeError(f"All models failed (last error: {last_error})")


def stream_reply(chat, prompt: str, confirm_saves: bool = True, tool_budget: int = DEFAULT_TOOL_BUDGET):
    """Runs one user question through the agent and yields progress events as they happen.

    Events (dicts with a "type" key):
      {"type": "model", "model": name}                       model that is answering
      {"type": "text", "delta": str}                         streamed answer text
      {"type": "tool_call", "name": str, "args": dict}
      {"type": "tool_result", "name": str, "ok": bool, "summary": str, "sources": [...]}
      {"type": "tool_skipped", "name": str, "reason": str}   over the tool budget
      {"type": "pending_save", "filename": str, "content": str}  needs the user's confirmation
      {"type": "done", "chat", "text", "sources", "model", "pending_saves", "tools_used"}

    With confirm_saves=True, save_research_note is NOT executed: it is reported as pending_save and the
    UI saves it only after the user clicks to confirm. Raises RuntimeError if no model can answer.
    """
    message = prompt
    chat, model_name, first_chunk, stream = _open_stream(chat, message)
    yield {"type": "model", "model": model_name}

    tools_used = 0
    text_parts: list[str] = []
    sources: list[dict] = []
    seen_urls: set[str] = set()
    pending_saves: list[dict] = []

    for turn in range(tool_budget + 2):
        if turn > 0:
            stream = iter(chat.send_message_stream(message))
            first_chunk = next(stream, None)

        started = time.perf_counter()
        last_chunk = None
        calls = []
        chunks = ([first_chunk] if first_chunk is not None else [])
        for chunk in _chain(chunks, stream):
            last_chunk = chunk
            for part in _iter_parts(chunk):
                if getattr(part, "function_call", None):
                    calls.append(part.function_call)
                elif getattr(part, "text", None) and not getattr(part, "thought", False):
                    text_parts.append(part.text)
                    yield {"type": "text", "delta": part.text}
        _log_metrics(model_name, time.perf_counter() - started, last_chunk)

        if not calls:
            break

        responses = []
        for call in calls:
            name = call.name or ""
            args = dict(call.args or {})
            if tools_used >= tool_budget:
                output = (
                    f"Tool budget of {tool_budget} calls for this question is exhausted. "
                    "Answer now using the information you already have."
                )
                yield {"type": "tool_skipped", "name": name, "reason": "tool budget exhausted"}
            else:
                tools_used += 1
                yield {"type": "tool_call", "name": name, "args": args}
                output, ok, found = _run_tool(name, args, confirm_saves, pending_saves)
                if name == "save_research_note" and confirm_saves and ok:
                    yield {"type": "pending_save", **pending_saves[-1]}
                for src in found:
                    if src["url"] not in seen_urls:
                        seen_urls.add(src["url"])
                        sources.append(src)
                summary = (output or "").strip().splitlines()[0][:160] if (output or "").strip() else "(no output)"
                yield {"type": "tool_result", "name": name, "ok": ok, "summary": summary, "sources": found}
            responses.append(types.Part.from_function_response(name=name, response={"result": output}))
        message = responses

    yield {
        "type": "done",
        "chat": chat,
        "text": "".join(text_parts),
        "sources": sources,
        "model": model_name,
        "pending_saves": pending_saves,
        "tools_used": tools_used,
    }


def _chain(first: list, rest):
    yield from first
    yield from rest


def _run_tool(name: str, args: dict, confirm_saves: bool, pending_saves: list) -> tuple[str, bool, list]:
    """Executes one tool call. Returns (output for the model, ok, sources found)."""
    started = time.perf_counter()
    ok = True
    sources: list[dict] = []
    try:
        if name in _RESEARCH_TOOLS:
            output = _RESEARCH_TOOLS[name](**args)
            sources = _extract_sources(name, args, output)
            ok = not output.startswith(("Tool error:", "Could not read"))
        elif name == "save_research_note":
            filename = str(args.get("filename", "")).strip()
            content = str(args.get("content", ""))
            if confirm_saves:
                if not filename or not content.strip():
                    output, ok = "Tool error: a note needs a filename and non-empty content.", False
                else:
                    pending_saves.append({"filename": filename, "content": content})
                    output = (
                        f"The note '{filename}' is prepared and waiting for the user to confirm saving it "
                        "in the interface. Tell the user it is ready and that they can click Save to confirm."
                    )
            else:
                output = save_research_note(filename, content)
                ok = output.startswith("Successfully")
        else:
            output, ok = f"Tool error: unknown tool '{name}'.", False
    except Exception as e:
        output, ok = f"Tool error: {type(e).__name__}: {e}", False
    _append_metrics({"source": "tool", "tool": name, "ok": ok, "latency_sec": round(time.perf_counter() - started, 4)})
    return output, ok, sources


def run_agent(user_prompt: str) -> str:
    chat = create_agent_session(MODELS[0])
    _, text = send_with_fallback(chat, user_prompt)
    return text


# 4. Interactive Terminal Chat Loop
if __name__ == "__main__":
    chat = create_agent_session(MODELS[0])
    print("Research Agent Ready! Type 'exit' or 'quit' to stop.\n")

    while True:
        user_input = input("You: ").strip()

        if not user_input:
            continue
        if user_input.lower() in ["exit", "quit"]:
            print("Goodbye!")
            break

        try:
            chat, reply = send_with_fallback(chat, user_input)
            print(f"\nAgent:\n{reply}\n")
        except Exception as e:
            print(f"\n[ERROR] {e}\n")
