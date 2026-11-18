import os
import re
from pathlib import Path
from datetime import datetime

# Hardcoded sandbox directory inside your project
SAFE_DIR = Path(__file__).parent / "agent_outputs"
SAFE_DIR.mkdir(exist_ok=True)

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

    if len(content) > 50000:
        raise ValueError("Content exceeds the maximum allowed length of 50,000 characters.")

    # Strip any folder paths (e.g., '../agent.py' becomes 'agent.py')
    clean_name = os.path.basename(filename).strip()
    
    # Force a safe .md extension so the agent cannot create executable .py or .sh scripts
    if not clean_name.endswith(".md"):
        clean_name += ".md"

    # Separate extension to sanitize the base name while preserving '.md'
    base, ext = os.path.splitext(clean_name)
    
    # Sanitize base filename: allow only letters, numbers, underscores, and hyphens, replacing others with underscores
    sanitized_base = re.sub(r'[^a-zA-Z0-9_-]', '_', base)
    
    # Collapse multiple consecutive underscores into a single underscore for clean filenames
    sanitized_base = re.sub(r'_+', '_', sanitized_base)
    
    clean_name = sanitized_base + ext
        
    target_path = SAFE_DIR / clean_name

    # Block overwriting existing files
    if target_path.exists():
        print(f"[SECURITY LOG] Blocked overwrite attempt on: {clean_name}")
        return f"ACCESS DENIED: '{clean_name}' already exists. Choose a different filename."

    # Prepend automatic metadata header if not already present
    final_content = content
    if not final_content.startswith("Saved at:"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        final_content = f"Saved at: {timestamp}\n\n{content}"

    target_path.write_text(final_content, encoding="utf-8")
    print(f"[TOOL LOG] Safely saved note to: {target_path}")
    return f"Successfully saved note to agent_outputs/{clean_name}"