"""
Thread-safe pool of API keys you own. Rotates round-robin, skips keys that are
cooling down, applies exponential backoff, and can permanently disable a key
(e.g. revoked / invalid).
"""

import threading
import time
from dataclasses import dataclass


class NoHealthyKeyError(RuntimeError):
    def __init__(self, provider: str | None, retry_in_sec: float | None):
        self.provider = provider
        self.retry_in_sec = retry_in_sec
        hint = f" Next key frees up in ~{round(retry_in_sec)}s." if retry_in_sec else ""
        super().__init__(f"No healthy keys for provider={provider or 'any'}.{hint}")


@dataclass
class ApiKey:
    key: str
    provider: str
    label: str = ""
    cooldown_until: float = 0.0
    fail_count: int = 0
    disabled: bool = False

    def __post_init__(self):
        if not self.label:
            self.label = f"{self.provider}-{self.key[-4:]}"

    def __repr__(self) -> str:  # never leak the secret into logs / tracebacks
        return f"ApiKey(label={self.label!r}, provider={self.provider!r})"


class KeyPool:
    def __init__(self, keys: list[ApiKey], strategy: str = "round_robin"):
        """strategy: "round_robin" spreads load; "priority" always tries keys in list order."""
        if not keys:
            raise ValueError("KeyPool needs at least one ApiKey")
        if strategy not in ("round_robin", "priority"):
            raise ValueError("strategy must be 'round_robin' or 'priority'")
        self.keys = keys
        self.strategy = strategy
        self._idx = 0
        self._lock = threading.Lock()

    def _candidates(self, provider: str | None) -> list[ApiKey]:
        return [k for k in self.keys if not k.disabled and (provider is None or k.provider == provider)]

    def count(self, provider: str | None = None) -> int:
        return len(self._candidates(provider))

    def get_next_key(self, provider: str | None = None) -> ApiKey:
        with self._lock:
            now = time.time()
            n = len(self.keys)
            for step in range(n):
                k = self.keys[(self._idx + step) % n]
                if k.disabled or (provider and k.provider != provider):
                    continue
                if now >= k.cooldown_until:
                    if self.strategy == "round_robin":
                        self._idx = (self._idx + step + 1) % n
                    return k
            pool = self._candidates(provider)
            retry = min((k.cooldown_until - now for k in pool), default=None)
            raise NoHealthyKeyError(provider, retry)

    def healthy_keys(self, provider: str | None = None) -> list[ApiKey]:
        """All usable keys right now, in the order this call should try them."""
        with self._lock:
            now = time.time()
            n = len(self.keys)
            start = self._idx if self.strategy == "round_robin" else 0
            ordered = [self.keys[(start + i) % n] for i in range(n)]
            if self.strategy == "round_robin":
                self._idx = (self._idx + 1) % n
            return [k for k in ordered
                    if not k.disabled and now >= k.cooldown_until and (provider is None or k.provider == provider)]

    def next_retry_in(self, provider: str | None = None) -> float | None:
        now = time.time()
        return min((k.cooldown_until - now for k in self._candidates(provider)), default=None)

    def mark_rate_limited(self, key: ApiKey, base_backoff_sec: float = 60, max_backoff_sec: float = 3600):
        with self._lock:
            key.fail_count += 1
            delay = min(base_backoff_sec * (2 ** (key.fail_count - 1)), max_backoff_sec)
            key.cooldown_until = time.time() + delay

    def mark_success(self, key: ApiKey):
        with self._lock:
            key.fail_count = 0
            key.cooldown_until = 0.0

    def disable(self, key: ApiKey):
        with self._lock:
            key.disabled = True

    def status(self) -> list[dict]:
        now = time.time()
        return [
            {
                "label": k.label,
                "provider": k.provider,
                "state": "disabled" if k.disabled else ("ok" if now >= k.cooldown_until else "cooling"),
                "cooldown_sec": max(0, round(k.cooldown_until - now)),
                "fail_count": k.fail_count,
            }
            for k in self.keys
        ]
