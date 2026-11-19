"""Saves and restores the research chat so a conversation survives a page reload or an app restart.

Two things are stored together in one JSON file:
  - messages: what the UI shows (role, content, sources, tool steps)
  - history:  Gemini's own conversation history (types.Content), so the model keeps its context
"""
import json
import os
import tempfile
from pathlib import Path

from google.genai import types

STORE_VERSION = 1


def save_conversation(path: Path, messages: list[dict], history) -> None:
    """Writes the conversation atomically (temp file + rename), so a crash mid-write never leaves
    a half-written file that would lose the whole chat on the next load."""
    payload = {
        "version": STORE_VERSION,
        "messages": messages,
        "history": [
            content.model_dump(mode="json", exclude_none=True) if hasattr(content, "model_dump") else content
            for content in (history or [])
        ],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".chat-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def load_conversation(path: Path) -> tuple[list[dict], list[types.Content]]:
    """Returns (messages, history). A missing, unreadable or incompatible file yields an empty chat
    instead of an error, so a bad file can never stop the app from starting."""
    path = Path(path)
    if not path.exists():
        return [], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != STORE_VERSION:
            return [], []
        messages = [m for m in payload.get("messages", []) if isinstance(m, dict) and "role" in m and "content" in m]
        history = [types.Content.model_validate(item) for item in payload.get("history", [])]
        return messages, history
    except (OSError, ValueError, TypeError):
        return [], []


def clear_conversation(path: Path) -> None:
    Path(path).unlink(missing_ok=True)
