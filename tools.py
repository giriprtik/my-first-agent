import os
import re
from pathlib import Path
from datetime import datetime

# Hardcoded sandbox directory inside your project (created on first save, not at import time)
SAFE_DIR = Path(__file__).parent / "agent_outputs"

MAX_NOTE_CHARS = 50_000
_DEFAULT_BASENAME = "note"


def save_research_note(filename: str, content: str) -> str:
    """
    Saves a Markdown research note inside the sandboxed agent_outputs folder.

    Args:
        filename: The desired name for the markdown file.
        content: The text content of the research note.

    Returns:
        A status message indicating success or access denial.

    Raises:
        ValueError: If filename or content is empty/whitespace, or if content exceeds 50,000 characters.
    """
    if not filename or not filename.strip():
        raise ValueError("Filename cannot be empty or whitespace.")

    if not content or not content.strip():
        raise ValueError("Content cannot be empty or whitespace.")

    if len(content) > MAX_NOTE_CHARS:
        raise ValueError("Content exceeds the maximum allowed length of 50,000 characters.")

    # Strip any folder paths (e.g., '../agent.py' becomes 'agent.py')
    clean_name = os.path.basename(filename).strip()

    # Split off an existing '.md' so the base can be sanitized; any other extension becomes part of the base.
    # (os.path.splitext(".md") returns (".md", ""), which used to save a file literally named "_md".)
    base = clean_name[: -len(".md")] if clean_name.endswith(".md") else clean_name

    # Sanitize base filename: allow only letters, numbers, underscores, and hyphens; collapse repeated underscores
    sanitized_base = re.sub(r"_+", "_", re.sub(r"[^a-zA-Z0-9_-]", "_", base))
    if not sanitized_base:
        sanitized_base = _DEFAULT_BASENAME

    # Force a safe .md extension so the agent cannot create executable .py or .sh scripts
    clean_name = sanitized_base + ".md"
    target_path = SAFE_DIR / clean_name

    # Prepend automatic metadata header if not already present
    final_content = content
    if not final_content.startswith("Saved at:"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        final_content = f"Saved at: {timestamp}\n\n{content}"

    SAFE_DIR.mkdir(parents=True, exist_ok=True)

    # Exclusive create ("x"): the existence check and the write are one atomic step,
    # so two concurrent saves of the same name can never overwrite each other.
    try:
        with open(target_path, "x", encoding="utf-8") as f:
            f.write(final_content)
    except FileExistsError:
        print(f"[SECURITY LOG] Blocked overwrite attempt on: {clean_name}")
        return f"ACCESS DENIED: '{clean_name}' already exists. Choose a different filename."

    print(f"[TOOL LOG] Safely saved note to: {target_path}")
    return f"Successfully saved note to agent_outputs/{clean_name}"
