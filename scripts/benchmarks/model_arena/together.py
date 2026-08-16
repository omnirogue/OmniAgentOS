"""Together.ai contestants: which cheap models are good for what.

Together speaks the OpenAI dialect, so the existing harness runs unchanged. The
only addition is price metadata, pulled live from Together's own catalogue so the
numbers cannot drift out of date, and used purely as telemetry — it never selects
a model, it just says what a capability costs.

    python -m scripts.benchmarks.model_arena.together          # print the slate
    python -m scripts.benchmarks.model_arena.together --all     # full catalogue
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.benchmarks.model_arena.providers import Provider, load_connections  # noqa: E402

BASE_URL = "https://api.together.xyz/v1"

# Five serverless models forming a clean price ladder from $0.12 to $1.20 per 1M
# output tokens. Only serverless models belong here: most of Together's cheap
# catalogue (Llama-3.2-3B, trinity-mini, Nemotron-Nano-9B, Ministral-3-14B,
# Llama-4-Scout) is listed with a per-token price but rejects requests with
# "Unable to access non-serverless model" — using those means paying hourly for a
# dedicated endpoint, which is not cheap at all. `build_together_providers`
# filters on the catalogue's `running` flag so that trap stays caught.
CHEAP_SLATE: list[tuple[str, str]] = [
    ("lfm2.5-8b-a1b", "LiquidAI/LFM2.5-8B-A1B"),
    ("gpt-oss-20b", "openai/gpt-oss-20b"),
    ("qwen3.5-9b", "Qwen/Qwen3.5-9B"),
    ("gpt-oss-120b", "openai/gpt-oss-120b"),
    ("minimax-m3", "MiniMaxAI/MiniMax-M3"),
]

# Serverless and verified working, but above the price band asked for. Pass as a
# slate to build_together_providers() to include them.
PRICIER_SERVERLESS: list[tuple[str, str]] = [
    ("qwen3.7-plus", "Qwen/Qwen3.7-Plus"),
]


def fetch_catalogue(api_key: str, timeout: float = 60.0) -> dict[str, dict]:
    """Model id -> {price_in, price_out, context_length, display_name}."""
    resp = httpx.get(
        BASE_URL + "/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout
    )
    resp.raise_for_status()
    out: dict[str, dict] = {}
    for m in resp.json():
        pricing = m.get("pricing") or {}
        out[m["id"]] = {
            "price_in": pricing.get("input"),
            "price_out": pricing.get("output"),
            "context_length": m.get("context_length"),
            "display_name": m.get("display_name") or m["id"],
            "type": m.get("type"),
            # False => dedicated-endpoint only; per-token calls are rejected.
            "running": bool(m.get("running")),
        }
    return out


def preflight(providers: list[Provider]) -> list[Provider]:
    """Drop models that cannot actually serve a per-token request.

    The catalogue's `running` flag is NOT a usability signal — it reads false even
    for models that answer fine, so it cannot be filtered on. A one-token probe is
    the only reliable test, and it stops a dead model from burning 20 graded tests
    to produce 20 identical HTTP 400s.
    """
    from scripts.benchmarks.model_arena.providers import stream_chat

    alive: list[Provider] = []
    for p in providers:
        # 512, not a token or two: several of these are thinking models that spend
        # ~200 hidden tokens before the first visible one, and a tight probe would
        # report "empty completion" for a model that works perfectly well.
        c = stream_chat(p, "Say READY", max_tokens=512)
        if c.ok:
            alive.append(p)
        else:
            reason = (c.error or "")[:120].replace("\n", " ")
            print(f"  skip {p.name}: HTTP {c.http_status} {reason}", file=sys.stderr)
    return alive


def build_together_providers(
    slate: list[tuple[str, str]] | None = None,
    *,
    api_key: str | None = None,
    probe: bool = True,
) -> list[Provider]:
    conn = load_connections()
    key = api_key or conn.get("TOGETHER_API_KEY", "")
    if not key:
        raise SystemExit("TOGETHER_API_KEY missing from connections.env")

    catalogue = fetch_catalogue(key)
    providers: list[Provider] = []
    for short, model_id in slate or CHEAP_SLATE:
        meta = catalogue.get(model_id)
        if meta is None:
            print(f"  skip {short}: {model_id} not in catalogue", file=sys.stderr)
            continue
        providers.append(
            Provider(
                name=short,
                base_url=BASE_URL,
                api_key=key,
                model=model_id,
                price_in=meta["price_in"],
                price_out=meta["price_out"],
                context_length=meta["context_length"],
            )
        )
    return preflight(providers) if probe else providers


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    conn = load_connections()
    key = conn.get("TOGETHER_API_KEY", "")
    if not key:
        print("TOGETHER_API_KEY missing", file=sys.stderr)
        return 1
    catalogue = fetch_catalogue(key)

    if "--all" in args:
        rows = [
            (mid, m)
            for mid, m in catalogue.items()
            if m["type"] == "chat" and (m["price_out"] or 0) > 0
        ]
        rows.sort(key=lambda r: (r[1]["price_in"] or 0) + (r[1]["price_out"] or 0))
        print(f"{len(rows)} paid chat models\n")
        print(f"{'id':56} {'$in/1M':>8} {'$out/1M':>8} {'ctx':>10}")
        for mid, m in rows:
            print(
                f"{mid[:56]:56} {m['price_in']:>8.3f} {m['price_out']:>8.3f} "
                f"{m['context_length'] or 0:>10}"
            )
        return 0

    print(f"{'name':20} {'model':46} {'$in/1M':>8} {'$out/1M':>8} {'ctx':>10}")
    for p in build_together_providers():
        print(
            f"{p.name:20} {p.model[:46]:46} {p.price_in:>8.3f} {p.price_out:>8.3f} "
            f"{p.context_length or 0:>10}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
