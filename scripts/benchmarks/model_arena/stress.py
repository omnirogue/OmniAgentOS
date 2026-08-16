"""Concurrency ramp: find each endpoint's saturation knee and peak throughput.

For every concurrency level N the harness fires N requests as close to
simultaneously as a thread barrier allows, then records:

  system_tps      aggregate generated tokens / batch wall time  (the headline)
  per_req_tps     mean single-stream speed as felt by one caller
  ttft p50/p95    queueing shows up here first
  errors / 429s   for a hosted API this *is* the parallelism ceiling

The ramp stops when errors spike or aggregate throughput stops improving, so the
knee is discovered rather than assumed. Prompts are salted per request because a
self-hosted vLLM server would otherwise serve them from its prefix cache and
report a throughput number that no real workload can reproduce.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .providers import Completion, Provider, stream_chat

# Cheap to prefill, and reliably runs to the token cap instead of stopping early.
STRESS_PROMPT = (
    "Batch {i} of an infrastructure load test. Write a numbered list of short, "
    "self-contained observations about distributed systems. Start at 1 and keep "
    "going without any summary or conclusion until you are cut off. Do not stop early."
)
STRESS_MAX_TOKENS = 256


@dataclass
class LevelResult:
    concurrency: int
    wall_s: float
    ok: int
    failed: int
    rate_limited: int
    system_tps: float
    per_req_tps: float
    ttft_p50: float | None
    ttft_p95: float | None
    latency_p50: float
    latency_p95: float
    total_tokens: int
    errors: list[str] = field(default_factory=list)

    def row(self) -> str:
        ttft = f"{self.ttft_p50:6.2f}" if self.ttft_p50 is not None else "   n/a"
        return (
            f"  N={self.concurrency:<4} ok={self.ok:<4} err={self.failed:<3} "
            f"429={self.rate_limited:<3} sys={self.system_tps:8.1f} tok/s  "
            f"per-req={self.per_req_tps:6.1f} tok/s  ttft_p50={ttft}s  "
            f"lat_p50={self.latency_p50:6.2f}s  lat_p95={self.latency_p95:6.2f}s"
        )


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def run_level(provider: Provider, concurrency: int, *, timeout: float = 300.0) -> LevelResult:
    """Fire `concurrency` requests simultaneously and measure the batch."""
    barrier = threading.Barrier(concurrency)
    results: list[Completion] = []
    lock = threading.Lock()

    def one(idx: int) -> None:
        barrier.wait()  # release all threads together
        comp = stream_chat(
            provider,
            STRESS_PROMPT.format(i=idx),
            max_tokens=STRESS_MAX_TOKENS,
            temperature=0.7,  # salt the sampling too; avoids identical-output caching
            timeout=timeout,
        )
        with lock:
            results.append(comp)

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(one, range(concurrency)))
    wall = time.perf_counter() - start

    good = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    rate_limited = sum(1 for r in bad if r.http_status == 429)
    tokens = sum((r.completion_tokens or 0) + (r.reasoning_tokens or 0) for r in good)
    ttfts = [r.ttft_s for r in good if r.ttft_s is not None]
    lats = [r.total_s for r in good]
    tps_samples = [r.output_tps for r in good if r.output_tps]

    return LevelResult(
        concurrency=concurrency,
        wall_s=wall,
        ok=len(good),
        failed=len(bad),
        rate_limited=rate_limited,
        system_tps=tokens / wall if wall > 0 else 0.0,
        per_req_tps=statistics.fmean(tps_samples) if tps_samples else 0.0,
        ttft_p50=_pct(ttfts, 0.50) if ttfts else None,
        ttft_p95=_pct(ttfts, 0.95) if ttfts else None,
        latency_p50=_pct(lats, 0.50),
        latency_p95=_pct(lats, 0.95),
        total_tokens=tokens,
        errors=[(r.error or "")[:160] for r in bad][:5],
    )


def ramp(
    provider: Provider,
    levels: list[int],
    *,
    settle_s: float = 5.0,
    timeout: float = 300.0,
    on_level: Callable[[LevelResult], None] | None = None,
) -> list[LevelResult]:
    """Walk the ramp, stopping at the knee or at the endpoint's ceiling."""
    out: list[LevelResult] = []
    best_tps = 0.0
    for n in levels:
        res = run_level(provider, n, timeout=timeout)
        out.append(res)
        if on_level:
            on_level(res)

        total = res.ok + res.failed
        err_rate = res.failed / total if total else 1.0

        if res.ok == 0:
            break  # endpoint is refusing everything at this level
        if err_rate > 0.25:
            break  # ceiling found: it is shedding load
        if res.system_tps < best_tps * 0.85 and n >= 4:
            break  # past the knee: adding clients now costs throughput
        best_tps = max(best_tps, res.system_tps)
        time.sleep(settle_s)  # let queues and rate-limit windows drain
    return out


def summarize(provider_name: str, results: list[LevelResult]) -> dict:
    """Pick out the peak-throughput level and where degradation began."""
    usable = [r for r in results if r.ok]
    if not usable:
        return {"provider": provider_name, "status": "no successful requests"}
    peak = max(usable, key=lambda r: r.system_tps)
    clean = [r for r in usable if r.failed == 0]
    return {
        "provider": provider_name,
        "peak_system_tps": round(peak.system_tps, 1),
        "peak_at_concurrency": peak.concurrency,
        "max_clean_concurrency": max((r.concurrency for r in clean), default=0),
        "single_stream_tps": round(usable[0].per_req_tps, 1),
        "scaling_factor": round(peak.system_tps / usable[0].system_tps, 2)
        if usable[0].system_tps
        else None,
        "ttft_p50_at_peak": round(peak.ttft_p50, 2) if peak.ttft_p50 is not None else None,
        "per_req_tps_at_peak": round(peak.per_req_tps, 1),
        "first_failure_at": next((r.concurrency for r in results if r.failed), None),
        "rate_limited_at": next((r.concurrency for r in results if r.rate_limited), None),
        "levels": [
            {
                "n": r.concurrency,
                "ok": r.ok,
                "failed": r.failed,
                "rate_limited": r.rate_limited,
                "system_tps": round(r.system_tps, 1),
                "per_req_tps": round(r.per_req_tps, 1),
                "ttft_p50": round(r.ttft_p50, 3) if r.ttft_p50 is not None else None,
                "ttft_p95": round(r.ttft_p95, 3) if r.ttft_p95 is not None else None,
                "latency_p50": round(r.latency_p50, 2),
                "latency_p95": round(r.latency_p95, 2),
                "wall_s": round(r.wall_s, 2),
                "tokens": r.total_tokens,
                "errors": r.errors,
            }
            for r in results
        ],
    }
