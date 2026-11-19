"""Saves and restores the research chat so a conversation survives a page reload or an app restart.

Two things are stored together in one JSON file:
  - messages: what the UI shows (role, content, sources, tool steps)
  - history:  Gemini's own conversation history (types.Content), so the model keeps its context
"""
import json
import os
import tempfile
import fcntl
import logging
import shutil
from pathlib import Path

from google.genai import types

STORE_VERSION = 1
MAX_CONVERSATION_FILE_SIZE = 10 * 1024 * 1024  # 10 MB limit to prevent DoS via memory exhaustion

logger = logging.getLogger(__name__)


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
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
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
        if path.stat().st_size > MAX_CONVERSATION_FILE_SIZE:
            logger.error("Conversation file exceeds maximum allowed size: %s", path)
            return [], []

        with open(path, "r", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            payload = json.load(f)

        if not isinstance(payload, dict):
            return [], []

        if payload.get("version") != STORE_VERSION:
            backup_path = path.with_suffix(path.suffix + ".bak")
            try:
                shutil.move(path, backup_path)
                logger.warning("Incompatible conversation version detected. Backed up to %s", backup_path)
            except Exception as backup_err:
                logger.error("Failed to backup incompatible conversation file: %s", backup_err)
            return [], []

        messages = [m for m in payload.get("messages", []) if isinstance(m, dict) and "role" in m and "content" in m]
        history = []
        for item in payload.get("history", []):
            try:
                history.append(types.Content.model_validate(item))
            except Exception as val_err:
                logger.error("Failed to validate history item: %s", val_err)
                raise

        return messages, history
    except (OSError, ValueError, TypeError) as err:
        logger.error("Error loading conversation from %s: %s", path, err)
        return [], []


def clear_conversation(path: Path) -> None:
    path = Path(path)
    if path.exists():
        try:
            with open(path, "w", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except (OSError, IOError):
            pass
    path.unlink(missing_ok=True)