"""Model arena driver: stress ramp first, then the 20-test quality suite.

    python -m scripts.benchmarks.model_arena.run_arena stress
    python -m scripts.benchmarks.model_arena.run_arena quality
    python -m scripts.benchmarks.model_arena.run_arena all

Every completion is written to ``var/model-arena/<run>/raw.jsonl`` in full, so a
judge pass or a re-grade never has to re-spend contestant tokens.

The 20 tests are:
  Tier A (12)  the repo's existing objective coding arena — hidden pytest suites,
               PASS/FAIL from the pytest exit code, no judging
  Tier B (8)   retrieval, instruction-following, extraction, multi-step maths,
               SQL, regex, code review, hallucination resistance — all machine-graded

Providers run in parallel with each other (separate endpoints, so no shared
queue to distort latency) and strictly sequentially within a provider.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.benchmarks.model_arena.providers import (  # noqa: E402
    Completion,
    Provider,
    build_providers,
    stream_chat,  # noqa: E402
)
from scripts.benchmarks.model_arena.stress import ramp, summarize  # noqa: E402
from scripts.benchmarks.model_arena.tasks_b import TIER_B, TaskB  # noqa: E402

ARENA_ROOT = REPO / "var" / "e2e-bench" / "coding-arena"
RESULTS_ROOT = REPO / "var" / "model-arena"
REPORT_ROOT = REPO / "vault" / "benchmarks"

CODE_SYSTEM = (
    "You are a precise Python engineer. Return the complete contents of the single "
    "requested file inside one ```python fenced block. No prose outside the block."
)

# gemini-3.6-flash and grok-4.5 both spend hidden thinking tokens out of the same
# budget as the visible answer — measured: 241 thinking vs 11 visible tokens under a
# 256 cap. A tight cap would score them 0 for running out of room rather than for
# being wrong, so every task gets generous headroom.
MIN_ANSWER_TOKENS = 4096
CODE_MAX_TOKENS = 8000

# Some cheap reasoning models (Qwen3.5-9B, gpt-oss-20b) burn the entire budget on
# hidden reasoning and return `finish_reason: length` with zero visible tokens. That
# is budget starvation, not incapability, and scoring it 0 would be measuring the
# harness rather than the model — so retry once with a much larger allowance and
# record that it needed it.
RETRY_TOKEN_MULTIPLIER = 4
MAX_RETRY_TOKENS = 32000


def _call_with_budget_retry(
    provider: Provider, prompt: str, *, system: str | None, max_tokens: int
) -> tuple[Completion, bool]:
    """Return (completion, retried). Retries once when the budget was exhausted.

    Truncation counts as starvation even when some text did arrive: a code file cut
    off mid-function fails the hidden pytest suite exactly like an empty answer, so
    scoring it 0 would again measure the harness. minimax-m3 lost a coding task this
    way before the check stopped requiring an entirely empty completion.
    """
    comp = stream_chat(provider, prompt, system=system, max_tokens=max_tokens, temperature=0.0)
    hit_cap = comp.finish_reason == "length" or (comp.completion_tokens or 0) >= max_tokens
    starved = comp.http_status == 200 and hit_cap
    if not starved:
        return comp, False
    bigger = min(max_tokens * RETRY_TOKEN_MULTIPLIER, MAX_RETRY_TOKENS)
    if bigger <= max_tokens:
        return comp, False
    return (
        stream_chat(provider, prompt, system=system, max_tokens=bigger, temperature=0.0),
        True,
    )


# ---------------------------------------------------------------- Tier A


@dataclass
class CodingTask:
    task_id: str
    solution_file: str
    spec: str


def load_tier_a(tasks_root: Path = ARENA_ROOT / "tasks") -> list[CodingTask]:
    out = []
    for d in sorted(p for p in tasks_root.iterdir() if p.is_dir()):
        meta_f, spec_f = d / "meta.json", d / "spec.md"
        if not (meta_f.exists() and spec_f.exists()):
            continue
        meta = json.loads(meta_f.read_text())
        out.append(
            CodingTask(
                task_id=meta["task_id"],
                solution_file=meta["solution_file"],
                spec=spec_f.read_text(),
            )
        )
    return out


def extract_python(text: str) -> str:
    """Recover the module source from a model reply."""
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return text.strip() + "\n"  # unfenced: take it as-is and let pytest judge


def grade_tier_a(task: CodingTask, completion: Completion, timeout: int = 60) -> tuple[float, str]:
    if not completion.ok:
        return 0.0, f"no output ({completion.error})"
    sandbox = Path(tempfile.mkdtemp(prefix=f"arena-{task.task_id}-"))
    try:
        (sandbox / task.solution_file).write_text(extract_python(completion.text))
        proc = subprocess.run(
            [
                sys.executable,
                str(ARENA_ROOT / "grade.py"),
                "--solution",
                str(sandbox),
                "--task",
                task.task_id,
                "--tasks-root",
                str(ARENA_ROOT / "tasks"),
                "--timeout",
                str(timeout),
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 60,
        )
        try:
            verdict = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return 0.0, f"grader unparseable: {proc.stdout[:80]}{proc.stderr[:80]}"
        passed, collected = verdict.get("passed", 0), verdict.get("collected", 0)
        note = f"{verdict.get('verdict')} {passed}/{collected} tests" + (
            f" — {verdict['reason']}" if verdict.get("reason") else ""
        )
        return (1.0 if verdict.get("verdict") == "PASS" else 0.0), note
    except subprocess.TimeoutExpired:
        return 0.0, "grader timeout"
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


# ---------------------------------------------------------------- run record


def record(raw_path: Path, payload: dict, lock: threading.Lock) -> None:
    with lock:
        with raw_path.open("a") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def run_quality_for_provider(
    provider: Provider,
    tier_a: list[CodingTask],
    tier_b: list[TaskB],
    raw_path: Path,
    lock: threading.Lock,
    *,
    verbose: bool = True,
) -> list[dict]:
    """All 20 tests against one provider, sequentially."""
    rows: list[dict] = []

    for task in tier_a:
        comp, retried = _call_with_budget_retry(
            provider, task.spec, system=CODE_SYSTEM, max_tokens=CODE_MAX_TOKENS
        )
        score, note = grade_tier_a(task, comp)
        if retried:
            note += " [retried at a larger token budget]"
        row = {
            "budget_retry": retried,
            "provider": provider.name,
            "model": provider.model,
            "tier": "A",
            "task_id": task.task_id,
            "kind": "coding",
            "score": score,
            "note": note,
            **{k: v for k, v in comp.as_dict().items() if k not in {"text", "provider", "model"}},
        }
        rows.append(row)
        record(raw_path, {**row, "output": comp.text}, lock)
        if verbose:
            print(f"  [{provider.name:16}] A/{task.task_id:<18} {score:.2f}  {note}", flush=True)

    for tb in tier_b:
        comp, retried = _call_with_budget_retry(
            provider,
            tb.prompt,
            system=tb.system,
            max_tokens=max(tb.max_tokens, MIN_ANSWER_TOKENS),
        )
        if comp.ok:
            try:
                score, note = tb.grade(comp.text)
            except Exception as exc:  # noqa: BLE001 - a grader bug must not void the run
                score, note = 0.0, f"grader error: {type(exc).__name__}: {exc}"
        else:
            score, note = 0.0, f"no output ({comp.error})"
        if retried:
            note += " [retried at a larger token budget]"
        row = {
            "budget_retry": retried,
            "provider": provider.name,
            "model": provider.model,
            "tier": "B",
            "task_id": tb.task_id,
            "kind": tb.kind,
            "score": score,
            "note": note,
            **{k: v for k, v in comp.as_dict().items() if k not in {"text", "provider", "model"}},
        }
        rows.append(row)
        record(raw_path, {**row, "output": comp.text}, lock)
        if verbose:
            print(f"  [{provider.name:16}] B/{tb.task_id:<18} {score:.2f}  {note}", flush=True)

    return rows


# ---------------------------------------------------------------- reporting


def aggregate(rows: list[dict]) -> dict:
    by: dict[str, dict] = {}
    for r in rows:
        p = by.setdefault(
            r["provider"],
            {
                "provider": r["provider"],
                "model": r["model"],
                "n": 0,
                "score_sum": 0.0,
                "tier_a_pass": 0,
                "tier_a_n": 0,
                "tier_b_sum": 0.0,
                "tier_b_n": 0,
                "ttft": [],
                "total_s": [],
                "tps": [],
                "out_tokens": 0,
                "reasoning_tokens": 0,
                "failures": [],
            },
        )
        p["n"] += 1
        p["score_sum"] += r["score"]
        kind = p.setdefault("by_kind", {}).setdefault(r["kind"], {"sum": 0.0, "n": 0})
        kind["sum"] += r["score"]
        kind["n"] += 1
        if r.get("cost_usd"):
            p["cost_usd"] = p.get("cost_usd", 0.0) + r["cost_usd"]
        if r["tier"] == "A":
            p["tier_a_n"] += 1
            p["tier_a_pass"] += 1 if r["score"] >= 1.0 else 0
        else:
            p["tier_b_n"] += 1
            p["tier_b_sum"] += r["score"]
        if r.get("ttft_s") is not None:
            p["ttft"].append(r["ttft_s"])
        p["total_s"].append(r.get("total_s") or 0.0)
        if r.get("output_tps"):
            p["tps"].append(r["output_tps"])
        p["out_tokens"] += r.get("completion_tokens") or 0
        p["reasoning_tokens"] += r.get("reasoning_tokens") or 0
        if r["score"] < 0.5:
            p["failures"].append(f"{r['tier']}/{r['task_id']}")

    import statistics

    for p in by.values():
        p["quality_pct"] = round(100 * p["score_sum"] / p["n"], 1) if p["n"] else 0.0
        p["tier_a_pct"] = round(100 * p["tier_a_pass"] / p["tier_a_n"], 1) if p["tier_a_n"] else 0.0
        p["tier_b_pct"] = round(100 * p["tier_b_sum"] / p["tier_b_n"], 1) if p["tier_b_n"] else 0.0
        p["ttft_mean"] = round(statistics.fmean(p["ttft"]), 2) if p["ttft"] else None
        p["ttft_p95"] = (
            round(sorted(p["ttft"])[int(0.95 * (len(p["ttft"]) - 1))], 2) if p["ttft"] else None
        )
        p["latency_mean"] = round(statistics.fmean(p["total_s"]), 2) if p["total_s"] else None
        p["latency_total"] = round(sum(p["total_s"]), 1)
        p["tps_mean"] = round(statistics.fmean(p["tps"]), 1) if p["tps"] else None
        p["kind_pct"] = {
            k: round(100 * v["sum"] / v["n"], 0) for k, v in p.get("by_kind", {}).items()
        }
        if p.get("cost_usd"):
            p["cost_usd"] = round(p["cost_usd"], 5)
        for k in ("ttft", "total_s", "tps"):
            p.pop(k, None)
    return by


def write_report(run_dir: Path, run_id: str, stress: dict | None, quality: dict | None) -> Path:
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    path = REPORT_ROOT / f"model-arena-{run_id}.md"
    L: list[str] = [
        f"# Model arena — {run_id}",
        "",
        "qwen35 (Qwen3.5-122B-A10B Q4_K_M, self-hosted llama.cpp on a RunPod H200) "
        "vs grok-4.5 (xAI) vs gemini-3.6-flash (Google).",
        "",
        "Quality is machine-graded only — executed pytest suites, schema validation and "
        "exact matches. No LLM judging.",
        f"Raw completions: `{run_dir.relative_to(REPO)}/raw.jsonl`",
        "",
    ]

    if stress:
        L += [
            "## 1. Stress test — parallelism and throughput",
            "",
            "`system_tps` is aggregate generated tokens/sec across all in-flight requests;",
            "`per_req_tps` is what a single caller feels. Peak system_tps marks the saturation knee.",
            "",
            "| endpoint | peak system tok/s | at N | max clean N | 1-stream tok/s | scaling | ttft p50 @peak | per-req tok/s @peak | first failure |",
            "|---|--:|--:|--:|--:|--:|--:|--:|--:|",
        ]
        for name, s in stress.items():
            if "peak_system_tps" not in s:
                L.append(f"| {name} | — | — | — | — | — | — | — | {s.get('status', 'failed')} |")
                continue
            L.append(
                f"| {name} | {s['peak_system_tps']} | {s['peak_at_concurrency']} | "
                f"{s['max_clean_concurrency']} | {s['single_stream_tps']} | "
                f"{s['scaling_factor']}× | {s['ttft_p50_at_peak']}s | "
                f"{s['per_req_tps_at_peak']} | {s['first_failure_at'] or '—'} |"
            )
        L.append("")
        for name, s in stress.items():
            if "levels" not in s:
                continue
            L += [
                f"### {name} ramp",
                "",
                "| N | ok | err | 429 | system tok/s | per-req tok/s | ttft p50 | ttft p95 | lat p50 | lat p95 |",
                "|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
            ]
            for lv in s["levels"]:
                L.append(
                    f"| {lv['n']} | {lv['ok']} | {lv['failed']} | {lv['rate_limited']} | "
                    f"{lv['system_tps']} | {lv['per_req_tps']} | {lv['ttft_p50']} | "
                    f"{lv['ttft_p95']} | {lv['latency_p50']} | {lv['latency_p95']} |"
                )
            errs = [e for lv in s["levels"] for e in lv["errors"]]
            if errs:
                L += ["", "Sample errors:", ""] + [f"- `{e}`" for e in errs[:4]]
            L.append("")

    if quality:
        L += [
            "## 2. Quality — 20 machine-graded tests",
            "",
            "Tier A: 12 coding tasks with hidden pytest suites (binary PASS/FAIL).",
            "Tier B: 8 tests — retrieval, instruction-following, extraction, maths, SQL,",
            "regex, code review, hallucination resistance (fractional credit).",
            "",
            "| model | overall | Tier A pass | Tier B score | mean ttft | mean latency | mean tok/s | out tokens | reasoning tokens |",
            "|---|--:|--:|--:|--:|--:|--:|--:|--:|",
        ]
        for p in sorted(quality.values(), key=lambda x: -x["quality_pct"]):
            L.append(
                f"| {p['provider']} | **{p['quality_pct']}%** | "
                f"{p['tier_a_pass']}/{p['tier_a_n']} ({p['tier_a_pct']}%) | {p['tier_b_pct']}% | "
                f"{p['ttft_mean']}s | {p['latency_mean']}s | {p['tps_mean']} | "
                f"{p['out_tokens']} | {p['reasoning_tokens']} |"
            )
        L.append("")

        # Capability matrix: the actual "which cheap model for what" answer.
        kinds: list[str] = []
        for p in quality.values():
            for k in p.get("kind_pct") or {}:
                if k not in kinds:
                    kinds.append(k)
        if kinds:
            L += [
                "### What each model is good for",
                "",
                "Score per capability (%). `cost` is the whole 20-test suite for that model.",
                "",
                "| model | " + " | ".join(kinds) + " | cost |",
                "|---" * (len(kinds) + 2) + "|",
            ]
            for p in sorted(quality.values(), key=lambda x: -x["quality_pct"]):
                cells = []
                for k in kinds:
                    v = (p.get("kind_pct") or {}).get(k)
                    cells.append("—" if v is None else f"{v:.0f}")
                cost = f"${p['cost_usd']:.4f}" if p.get("cost_usd") else "—"
                L.append(f"| {p['provider']} | " + " | ".join(cells) + f" | {cost} |")
            L.append("")

        for p in quality.values():
            if p["failures"]:
                L.append(f"- **{p['provider']}** failed: {', '.join(p['failures'])}")
        L.append("")

    path.write_text("\n".join(L) + "\n")
    return path


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="qwen35 vs grok-4.5 vs gemini-3.6-flash")
    ap.add_argument("mode", choices=["stress", "quality", "all"])
    ap.add_argument("--qwen-base-url", default=None, help="override QWEN35_BASE_URL")
    ap.add_argument("--only", default=None, help="comma-separated provider names")
    ap.add_argument(
        "--only-tasks",
        default=None,
        help="comma-separated task ids, for re-running just the cases that need it. "
        "Appends to raw.jsonl; regrade keeps the newest row per provider+task.",
    )
    ap.add_argument("--levels", default="1,2,4,8,16,32,64", help="stress concurrency ramp")
    ap.add_argument(
        "--cloud-max-n",
        type=int,
        default=16,
        help="cap the ramp for hosted APIs (rate limits, spend)",
    )
    ap.add_argument("--run-id", default=None)
    ap.add_argument(
        "--qwen-thinking",
        action="store_true",
        help="add a 4th contestant: qwen35 with enable_thinking=true. The pod is served "
        "--reasoning off, so this is opt-in per request; the vault notes say it flips "
        "some multi-step answers from wrong to right at a real latency cost.",
    )
    ap.add_argument(
        "--together",
        action="store_true",
        help="benchmark the cheap Together.ai slate instead of qwen35/grok/gemini",
    )
    args = ap.parse_args(argv)

    if args.together:
        from scripts.benchmarks.model_arena.together import build_together_providers

        providers = build_together_providers()
    else:
        providers = build_providers(qwen_base_url=args.qwen_base_url)
    if args.qwen_thinking:
        base = next((p for p in providers if p.name == "qwen35"), None)
        if base:
            providers.append(
                Provider(
                    name="qwen35-thinking",
                    base_url=base.base_url,
                    api_key=base.api_key,
                    model=base.model,
                    extra_body={"chat_template_kwargs": {"enable_thinking": True}},
                )
            )
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        providers = [p for p in providers if p.name in want]
    missing = [p.name for p in providers if not p.api_key or not p.base_url]
    if missing:
        print(f"missing credentials/base url for: {missing}", file=sys.stderr)
        providers = [p for p in providers if p.api_key and p.base_url]
    if not providers:
        print("no usable providers", file=sys.stderr)
        return 1

    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_path = run_dir / "raw.jsonl"
    lock = threading.Lock()

    print(f"run {run_id} -> {run_dir}")
    for p in providers:
        print(f"  contestant: {p.name:16} model={p.model}  base={p.base_url}")
    print()

    stress_summary: dict | None = None
    quality_summary: dict | None = None

    if args.mode in ("stress", "all"):
        print("== stress: concurrency ramp ==", flush=True)
        all_levels = [int(x) for x in args.levels.split(",")]
        # Merge into any existing ramp data so re-running one provider (after a
        # fix, or to extend its ramp) does not discard the others' results.
        stress_path = run_dir / "stress.json"
        stress_summary = json.loads(stress_path.read_text()) if stress_path.exists() else {}
        for p in providers:
            is_local = "runpod" in p.base_url or "localhost" in p.base_url
            levels = all_levels if is_local else [n for n in all_levels if n <= args.cloud_max_n]
            print(f"-- {p.name} (levels {levels}) --", flush=True)
            res = ramp(p, levels, on_level=lambda r: print(r.row(), flush=True))
            stress_summary[p.name] = summarize(p.name, res)
        stress_path.write_text(json.dumps(stress_summary, indent=2))
        print(f"\nstress written -> {stress_path}\n", flush=True)

    if args.mode in ("quality", "all"):
        tier_a, tier_b = load_tier_a(), list(TIER_B)
        if args.only_tasks:
            wanted = {s.strip() for s in args.only_tasks.split(",")}
            tier_a = [t for t in tier_a if t.task_id in wanted]
            tier_b = [t for t in tier_b if t.task_id in wanted]
            unknown = wanted - {t.task_id for t in tier_a} - {t.task_id for t in tier_b}
            if unknown:
                print(f"unknown task ids: {sorted(unknown)}", file=sys.stderr)
        print(
            f"== quality: {len(tier_a)} coding + {len(tier_b)} reasoning = "
            f"{len(tier_a) + len(tier_b)} tests x {len(providers)} models ==",
            flush=True,
        )
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=len(providers)) as pool:
            futures = [
                pool.submit(run_quality_for_provider, p, tier_a, tier_b, raw_path, lock)
                for p in providers
            ]
            rows = [r for f in futures for r in f.result()]
        quality_summary = aggregate(rows)
        (run_dir / "quality.json").write_text(
            json.dumps({"rows": rows, "summary": quality_summary}, indent=2)
        )
        print(
            f"\nquality done in {time.perf_counter() - started:.0f}s -> {run_dir / 'quality.json'}",
            flush=True,
        )

    if stress_summary is None and (run_dir / "stress.json").exists():
        stress_summary = json.loads((run_dir / "stress.json").read_text())
    if quality_summary is None and (run_dir / "quality.json").exists():
        quality_summary = json.loads((run_dir / "quality.json").read_text())["summary"]

    report = write_report(run_dir, run_id, stress_summary, quality_summary)
    print(f"report -> {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
