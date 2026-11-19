"""
Diagnose every provider in your .env:
    python -m llm_router.check            # test each provider with its configured model
    python -m llm_router.check --models   # also list chat models each key can use
"""

import sys
import openai
from .config import build_pool_from_env
from .fallback import FallbackLLM

SKIP_WORDS = ("embed", "whisper", "tts", "image", "audio", "rerank", "guard", "vision", "moderation",
              "transcribe", "veo", "imagen", "live", "robotics", "omni", "banana")


def list_models(client, limit=25):
    try:
        ids = sorted(m.id for m in client.models.list())
    except Exception as e:
        return [f"(can't list models: {type(e).__name__})"]
    ids = [i.removeprefix("models/") for i in ids if not any(w in i.lower() for w in SKIP_WORDS)]
    return ids[:limit] + ([f"... +{len(ids) - limit} more"] if len(ids) > limit else [])


def main():
    show_models = "--models" in sys.argv
    pool = build_pool_from_env()
    llm = FallbackLLM(pool, log=lambda *_: None)
    print(f"{'KEY':<22} {'MODEL':<38} RESULT")
    print("-" * 90)
    ok = 0
    for key in pool.keys:
        try:
            client, model = llm._client(key)
        except RuntimeError as e:
            print(f"{key.label:<22} {'-':<38} CONFIG ERROR: {e}")
            continue
        try:
            r = client.chat.completions.create(model=model, max_tokens=300,
                                               messages=[{"role": "user", "content": "Reply with just: ok"}])
            text = (r.choices[0].message.content or "").strip().replace("\n", " ")[:25]
            print(f"{key.label:<22} {model:<38} OK  ({text!r})")
            ok += 1
        except openai.APIStatusError as e:
            body = str(e).replace("\n", " ")
            hint = ""
            if e.status_code in (404, 410):
                hint = f"  -> model gone; set {key.provider.upper()}_MODEL"
            elif "insufficient_quota" in body:
                hint = "  -> account has no credits (add billing or drop this provider)"
            elif e.status_code in (401, 403):
                hint = "  -> key invalid or lacks permission"
            print(f"{key.label:<22} {model:<38} FAIL {e.status_code}{hint}")
            print(f"{'':<22} {body[:140]}")
        except Exception as e:
            print(f"{key.label:<22} {model:<38} FAIL {type(e).__name__}: {str(e)[:100]}")
        if show_models:
            for mid in list_models(client):
                print(f"{'':<26}· {mid}")
    print("-" * 90)
    print(f"{ok}/{len(pool.keys)} providers working.")


if __name__ == "__main__":
    main()
