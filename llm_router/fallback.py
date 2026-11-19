"""
FallbackLLM: one chat() call, tries every healthy key/provider in priority order.

Uses the OpenAI chat-completions format everywhere (messages, tools, tool_calls),
so your agent code is provider-agnostic. Returns the SDK ChatCompletion, with
`.router_provider` and `.router_key` attributes added so you can log who answered.
"""

import threading
import openai
from openai import OpenAI

from .key_pool import KeyPool, ApiKey, NoHealthyKeyError
from .providers import PROVIDERS, resolve


def _short(e: Exception, n: int = 160) -> str:
    return str(e).replace("\n", " ")[:n]


class FallbackLLM:
    def __init__(self, pool: KeyPool, timeout: float = 60, log=print):
        self.pool = pool
        self.timeout = timeout
        self.log = log
        self._clients: dict[str, tuple[OpenAI, str]] = {}
        self._lock = threading.Lock()
        for k in pool.keys:
            if k.provider not in PROVIDERS:
                pool.disable(k)

    def _client(self, key: ApiKey) -> tuple[OpenAI, str]:
        with self._lock:
            if key.label not in self._clients:
                base_url, model, headers = resolve(key.provider)
                client = OpenAI(api_key=key.key, base_url=base_url, default_headers=headers,
                                timeout=self.timeout, max_retries=0)   # we do the retrying
                self._clients[key.label] = (client, model)
            return self._clients[key.label]

    def model_for(self, key: ApiKey) -> str:
        return resolve(key.provider)[1]

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             provider: str | None = None, model: str | None = None, **kwargs):
        candidates = self.pool.healthy_keys(provider)
        if not candidates:
            raise NoHealthyKeyError(provider, self.pool.next_retry_in(provider))

        errors_seen = []
        for key in candidates:
            try:
                client, default_model = self._client(key)
                params = {"model": model or default_model, "messages": messages, **kwargs}
                if tools:
                    params["tools"] = tools
                resp = client.chat.completions.create(**params)
                self.pool.mark_success(key)
                resp.router_provider, resp.router_key = key.provider, key.label
                return resp

            except openai.RateLimitError as e:
                if "insufficient_quota" in str(e) or getattr(e, "code", None) == "insufficient_quota":
                    self.pool.disable(key)        # out of credits: won't fix itself in 60s
                    errors_seen.append(f"{key.label}: no credits/quota left -> disabled")
                else:
                    self.pool.mark_rate_limited(key, base_backoff_sec=60)
                    errors_seen.append(f"{key.label}: rate-limited (429) -> cooling down")
            except (openai.InternalServerError, openai.APIConnectionError) as e:   # 5xx, timeouts, network
                self.pool.mark_rate_limited(key, base_backoff_sec=10)
                errors_seen.append(f"{key.label}: {type(e).__name__} -> cooling 10s")
            except (openai.AuthenticationError, openai.PermissionDeniedError):
                self.pool.disable(key)
                errors_seen.append(f"{key.label}: key rejected (401/403) -> disabled")
            except openai.APIStatusError as e:
                if e.status_code in (404, 410):
                    # model retired / unknown: retrying every call just wastes time, so disable
                    self.pool.disable(key)
                    errors_seen.append(f"{key.label}: model '{params['model']}' not available ({e.status_code}) "
                                       f"-> disabled. Set {key.provider.upper()}_MODEL in .env "
                                       f"(run: python -m llm_router.check)")
                else:
                    # 400/422 etc: often request-specific (tools unsupported, context too long): skip this call only
                    errors_seen.append(f"{key.label}: {e.status_code} {_short(e)}")
            except RuntimeError as e:   # e.g. missing Cloudflare account id
                self.pool.disable(key)
                errors_seen.append(f"{key.label}: {e}")
            self.log(f"[router] {errors_seen[-1]}")

        raise RuntimeError("All providers failed:\n  " + "\n  ".join(errors_seen))
