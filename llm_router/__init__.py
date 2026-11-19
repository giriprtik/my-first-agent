from .key_pool import ApiKey, KeyPool, NoHealthyKeyError
from .config import build_pool_from_env, DEFAULT_PRIORITY
from .providers import PROVIDERS
from .fallback import FallbackLLM

__all__ = ["ApiKey", "KeyPool", "NoHealthyKeyError", "build_pool_from_env",
           "DEFAULT_PRIORITY", "PROVIDERS", "FallbackLLM"]

try:  # optional: native Gemini wrapper, only if google-genai is installed
    from .gemini import RotatingGemini  # noqa: F401
    __all__.append("RotatingGemini")
except ImportError:
    pass
