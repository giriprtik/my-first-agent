"""
One OpenAI-compatible adapter for every provider. Each entry: base URL + default model.
Override any model with <PROVIDER>_MODEL in .env (model catalogues change often).
"""

from dataclasses import dataclass
from .config import env_any


@dataclass(frozen=True)
class ProviderSpec:
    base_url: str | None          # None = OpenAI's default endpoint
    default_model: str
    extra_headers: dict | None = None


PROVIDERS: dict[str, ProviderSpec] = {
    "gemini":     ProviderSpec("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.5-flash"),
    "openai":     ProviderSpec(None, "gpt-5-mini"),
    "openrouter": ProviderSpec("https://openrouter.ai/api/v1", "openrouter/free",   # OpenRouter picks any currently-free model
                               {"X-Title": "code-optimizer-agent"}),
    "groq":       ProviderSpec("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "nvidia":     ProviderSpec("https://integrate.api.nvidia.com/v1", "openai/gpt-oss-120b"),
    "cloudflare": ProviderSpec("https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
                               "@cf/openai/gpt-oss-120b"),
}


def resolve(provider: str) -> tuple[str | None, str, dict | None]:
    spec = PROVIDERS[provider]
    base_url = spec.base_url
    if provider == "cloudflare":
        account_id = env_any("CLOUDFLARE_ACCOUNT_ID", "CLOUDFARE_ACCOUNT_ID")
        if not account_id:
            raise RuntimeError("Cloudflare key found but CLOUDFLARE_ACCOUNT_ID is missing")
        base_url = base_url.format(account_id=account_id)
    model = env_any(f"{provider.upper()}_MODEL") or spec.default_model
    return base_url, model, spec.extra_headers
