"""
Builds a KeyPool from environment variables.

Accepted names (both styles work, mix freely):
    GEMINI_API_KEY          GEMINI_API_KEY_2   GEMINI_API_KEY_3 ...
Provider-specific extras (not keys, read by the adapters):
    CLOUDFLARE_ACCOUNT_ID   (CLOUDFARE_ACCOUNT_ID also accepted)
    <PROVIDER>_MODEL        override the default model, e.g. GROQ_MODEL=...
"""

import os
import re
from dotenv import load_dotenv
from .key_pool import ApiKey, KeyPool

_PATTERN = re.compile(r"^([A-Z]+)_API_KEY(?:_(\d+))?$")
_ALIASES = {"cloudfare": "cloudflare"}  # tolerate the common misspelling

# Default fallback order: strongest / most reliable first, Cloudflare last.
DEFAULT_PRIORITY = ["gemini", "openai", "openrouter", "groq", "nvidia", "cloudflare"]
KNOWN_PROVIDERS = set(DEFAULT_PRIORITY)   # other *_API_KEY vars (e.g. TAVILY) are ignored


def build_pool_from_env(
    dotenv_path: str | None = None,
    providers: set[str] | None = None,
    priority: list[str] | None = None,
    strategy: str = "priority",
) -> KeyPool:
    load_dotenv(dotenv_path)
    order = priority or [p.strip() for p in os.getenv("LLM_PRIORITY", "").split(",") if p.strip()] or DEFAULT_PRIORITY
    rank = {p: i for i, p in enumerate(order)}

    keys = []
    for env_name, value in os.environ.items():
        m = _PATTERN.match(env_name)
        if not (m and value.strip()):
            continue
        provider = _ALIASES.get(m.group(1).lower(), m.group(1).lower())
        if provider not in (providers or KNOWN_PROVIDERS):
            continue
        keys.append(ApiKey(key=value.strip(), provider=provider, label=env_name))
    if not keys:
        raise RuntimeError("No API keys found. Add e.g. GEMINI_API_KEY=... to .env")

    keys.sort(key=lambda k: (rank.get(k.provider, len(rank)), k.label))
    return KeyPool(keys, strategy=strategy)


def env_any(*names: str) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v and v.strip():
            return v.strip()
    return None
