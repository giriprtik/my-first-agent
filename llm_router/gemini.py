"""
Drop-in replacement for `client.models.generate_content(...)` (google-genai SDK)
that rotates across every GEMINI_API_KEY_<N> in the pool.

It returns the SDK's own response object, so system instructions, multi-turn
`contents`, tools / function calls and JSON output all work unchanged.
"""

import threading
from google import genai
from google.genai import errors

from .key_pool import KeyPool, ApiKey

_TRANSIENT = {500, 502, 503, 504}   # model overloaded etc. -> short cooldown, try next key
_BAD_KEY = {401, 403}               # revoked / no permission -> disable key


class RotatingGemini:
    def __init__(self, pool: KeyPool, model: str = "gemini-2.5-flash", provider: str = "gemini", log=print):
        self.pool = pool
        self.model = model
        self.provider = provider
        self.log = log
        self._clients: dict[str, genai.Client] = {}
        self._clients_lock = threading.Lock()

    def _client(self, key: ApiKey) -> genai.Client:
        # one client per key; no global genai.configure() so concurrent sessions can't clobber each other
        with self._clients_lock:
            if key.label not in self._clients:
                self._clients[key.label] = genai.Client(api_key=key.key)
            return self._clients[key.label]

    def generate_content(self, contents, config=None, model: str | None = None, max_attempts: int | None = None):
        attempts = max_attempts or max(2, self.pool.count(self.provider) * 2)
        last_err: Exception | None = None
        for _ in range(attempts):
            key = self.pool.get_next_key(self.provider)   # raises NoHealthyKeyError when all are cooling
            try:
                resp = self._client(key).models.generate_content(
                    model=model or self.model, contents=contents, config=config
                )
                self.pool.mark_success(key)
                return resp
            except errors.APIError as e:
                last_err = e
                msg = str(e)
                if e.code == 429:
                    self.pool.mark_rate_limited(key, base_backoff_sec=60)
                    self.log(f"[router] {key.label} rate-limited -> rotating")
                elif e.code in _TRANSIENT:
                    self.pool.mark_rate_limited(key, base_backoff_sec=5)
                    self.log(f"[router] {key.label} got {e.code} -> rotating")
                elif e.code in _BAD_KEY or "API_KEY_INVALID" in msg:
                    self.pool.disable(key)
                    self.log(f"[router] {key.label} invalid/forbidden -> disabled")
                else:
                    raise  # real request error (bad prompt, bad schema): don't burn other keys on it
        raise RuntimeError(f"Gemini call failed after {attempts} attempts: {last_err}")
