import os

# Keep tests offline: never let ask_agent fall back to real OpenRouter/Groq/... keys from .env.
# (test_llm_fallback.py turns it back on with mocks.)
os.environ.setdefault("LLM_FALLBACK", "off")
