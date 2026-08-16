#!/usr/bin/env python3
"""memcert benchmark runner (DESIGN devtasks/memcert/DESIGN.md §12).

Runs the certification vector (arms x models x axes x trials) against a set
of deterministically-generated fixture worlds and writes graded results plus
a summary + reports. Two adapters are supported: ``mock`` (deterministic,
offline, no network — used by the hermetic test suite) and ``openrouter``
(real spend through the in-repo policy-gated adapter registry).

Contract (DESIGN §12):
    One results.jsonl row per (item, arm, model, trial):
    {item_id, axis, level, arm, model, trial, raw_answer, parsed, verdict,
     score, latency_ms, cost_usd, tokens, error, ...}

World construction is INJECTABLE (``run(worlds=...)``): ``scripts/memcert/gen.py``
is a sibling module that may not exist yet at every point in this lane's
history, so callers (tests, or a future CLI wiring) can hand ``run()`` a
mapping of ``seed -> world`` directly instead of relying on the import. The
world only needs to satisfy the informal protocol:
    world.items(split: str) -> list[core.Item]
    world.write_fixtures(out_dir: Path) -> None

Same for the context builder (``scripts/memcert/arms.py::build_context``) and
the grader/summarizer (``scripts/memcert/grade.py::grade_rows``/``summarize``)
-- both are resolved from the sibling module automatically when it is
importable, and otherwise fall back to a self-contained default built on
``core.grade_item`` (see ``_default_grade_rows``/``_default_summarize``
below). Pass ``context_builder=``/``grader_fn=``/``summarizer_fn=`` to
override either resolution explicitly -- this is how the offline test drives
the harness without depending on ``gen.py``'s exact fixture-world shape.

CRITICAL LEAK GUARD: ``assert_no_answer_leak`` raises ``LeakGuardError`` if any
string fragment of an item's answer spec appears in the assembled SYSTEM
prompt. The USER prompt's ``context_block`` may legitimately contain it (a
transcript-based arm quoting the fact itself) — only the harness-authored
SYSTEM half is guarded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

try:  # imported as a package member: `scripts.memcert.run_bench`
    from . import core
except ImportError:  # pragma: no cover - exercised when run as a bare script
    # The Makefile / CLI invoke this file directly, so sys.path[0] is this
    # directory and there is no parent package for the relative import to
    # resolve against (same fallback idiom as scripts/northstar_cert/emit_gaps.py).
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import core  # type: ignore[no-redef]

# gen.py / arms.py / grade.py are sibling modules owned by other lanes of this
# same devtask; they may not exist yet. Resolve them best-effort so run_bench
# can be exercised offline (mock adapter + injected worlds/context builder)
# before they land, and pick them up automatically once they do.
gen_mod: Any = None
arms_mod: Any = None
grade_mod: Any = None
try:
    from . import arms as arms_mod  # type: ignore[no-redef]
except ImportError:
    try:
        import arms as arms_mod  # type: ignore[no-redef]
    except ImportError:
        arms_mod = None
try:
    from . import gen as gen_mod  # type: ignore[no-redef]
except ImportError:
    try:
        import gen as gen_mod  # type: ignore[no-redef]
    except ImportError:
        gen_mod = None
try:
    from . import grade as grade_mod  # type: ignore[no-redef]
except ImportError:
    try:
        import grade as grade_mod  # type: ignore[no-redef]
    except ImportError:
        grade_mod = None

EXIT_OK = 0
EXIT_BAR_FAILED = 1
EXIT_REFUSED = 2
EXIT_INSTRUMENT_FAILURE = 70

ROLE_LINE = (
    "You are being evaluated on your ability to recall and reason over the "
    "context/memory you are given. Answer only from that context."
)

_TERMINAL_ERROR_MARKERS = (
    "unauthorized",
    "forbidden",
    "401",
    "403",
    "429",
    "quota",
    "rate limit",
    "rate_limit",
    "insufficient_quota",
    "invalid api key",
    "invalid_api_key",
)

DEFAULT_BACKOFFS: tuple[float, ...] = (2.0, 8.0)
DEFAULT_MAX_ATTEMPTS = 3


class LeakGuardError(RuntimeError):
    """Raised when an item's answer spec is found in the assembled SYSTEM prompt."""


class WorldLike(Protocol):
    def items(self, split: str) -> list[Any]: ...

    def write_fixtures(self, out_dir: Path) -> None: ...


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _var_root() -> Path:
    base = os.environ.get("OMNIAGENTOS_VAR_DIR")
    if base:
        return Path(base)
    return _repo_root() / "var"


def _utc_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def default_out_dir(run_id: str | None = None) -> Path:
    return _var_root() / "memcert" / "runs" / (run_id or _utc_run_id())


# --------------------------------------------------------------------------
# leak guard
# --------------------------------------------------------------------------


def _leak_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        if value.strip():
            out.append(value)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            out.extend(_leak_strings(v))
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_leak_strings(v))
    elif value is not None:
        out.append(str(value))
    return out


def assert_no_answer_leak(system_prompt: str, spec: core.AnswerSpec) -> None:
    """Raise ``LeakGuardError`` if any answer-spec fragment leaks into SYSTEM.

    Only ``spec.value`` fragments of length >= 3 are checked (short fragments
    like single digits/letters would false-positive on ordinary prose).
    Aliases/stale_values are legitimately-known-elsewhere strings (they are
    NOT the graded answer itself) and are intentionally not checked here.
    Abstain items are exempt: their "value" is the ABSTAIN_TOKEN, which the
    shared answer protocol legitimately teaches in every SYSTEM prompt.
    """
    if spec.kind == "abstain":
        return
    for fragment in _leak_strings(spec.value):
        if fragment == core.ABSTAIN_TOKEN:
            continue
        if len(fragment) >= 3 and fragment in system_prompt:
            raise LeakGuardError(
                f"answer leak: spec.value fragment {fragment!r} found in the SYSTEM prompt"
            )


# --------------------------------------------------------------------------
# prompt assembly (DESIGN §12: identical scaffold across arms/models)
# --------------------------------------------------------------------------


def build_messages(item: Any, context: core.ArmContext, trial: int) -> tuple[str, str]:
    protocol = core.ACTION_PROTOCOL if item.answer_spec.kind == "params" else core.ANSWER_PROTOCOL
    system = ROLE_LINE + "\n" + protocol
    meta = context.meta if isinstance(context.meta, dict) else {}
    suffix = meta.get("system_prompt_suffix")
    if suffix:
        system = system + "\n" + str(suffix)
    # Tail reminder AFTER the question: on long contexts models drop the reply
    # format stated only up top (measured live 2026-08-12) — recency wins.
    # Identical for every arm/model, so comparisons stay symmetric.
    if item.answer_spec.kind == "params":
        tail = 'Reply with ONLY the JSON object {"tool": ..., "args": {...}} — no other text.'
    else:
        tail = (
            "Remember: reply with exactly one line 'ANSWER: <value>' "
            f"(or 'ANSWER: {core.ABSTAIN_TOKEN}' if absent) — no other text."
        )
    user = (
        (context.context_block or "")
        + f"\n\nQUESTION (id {item.item_id}, trial {trial}): {item.question}\n{tail}"
    )
    return system, user


# --------------------------------------------------------------------------
# adapters: call(model, system, user, wall_ms, **kwargs) -> dict
#   {text, cost_usd, tokens_in, tokens_out, error}
# --------------------------------------------------------------------------


def _mock_reply(rng: Any, is_action_kind: bool) -> str:
    roll = rng.random()
    if is_action_kind:
        if roll >= 0.90:  # 10% abstain
            return json.dumps({"tool": core.ABSTAIN_TOKEN, "args": {}})
        fake_tool = "tool_" + "".join(rng.choice("abcdefghijklmnop") for _ in range(6))
        fake_val = "".join(rng.choice("0123456789abcdef") for _ in range(8))
        # both the "correct-shaped wrong value" (60%) and "fabricated" (30%)
        # buckets are action-shaped JSON here; they differ only in how the
        # fake payload was generated, and both grade to a non-correct verdict
        # (the odds of colliding with the real answer are negligible).
        return json.dumps({"tool": fake_tool, "args": {"value": fake_val}})
    if roll >= 0.90:  # 10% abstain
        return f"ANSWER: {core.ABSTAIN_TOKEN}"
    if roll >= 0.60:  # 30% fabricated string
        fab = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(10))
        return f"ANSWER: {fab}"
    # 60% correct-shaped wrong value
    fake = "".join(rng.choice("0123456789") for _ in range(6))
    return f"ANSWER: {fake}"


def mock_adapter_call(
    model: str, system: str, user: str, wall_ms: int, **kwargs: Any
) -> dict[str, Any]:
    """Deterministic scripted mock adapter (DESIGN spec, run_bench brief).

    Never given access to answer specs — it only inspects the SYSTEM prompt's
    protocol marker to know whether an action (params) reply is expected, and
    draws its scripted reply from ``core.rng_for``.
    """
    item_id = str(kwargs.get("item_id", ""))
    arm = str(kwargs.get("arm", ""))
    trial = kwargs.get("trial", 0)
    seed = int(kwargs.get("seed", 0))
    is_action_kind = core.ACTION_PROTOCOL in system
    if model == "mock-oracle":
        # Cannot see answers either: always abstains, regardless of roll.
        text = json.dumps({"tool": core.ABSTAIN_TOKEN, "args": {}}) if is_action_kind else (
            f"ANSWER: {core.ABSTAIN_TOKEN}"
        )
        return {"text": text, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "error": None}
    rng = core.rng_for(seed, item_id + arm + model + str(trial))
    text = _mock_reply(rng, is_action_kind)
    return {"text": text, "cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "error": None}


def _openrouter_adapter_call(
    model: str, system: str, user: str, wall_ms: int, **kwargs: Any
) -> dict[str, Any]:
    """Real spend via the policy-gated OpenRouter adapter (llm-exec notes §1,§4).

    Drains ``AgentInput.metadata['cost_observations']`` into
    ``store.record_provider_call`` (stage='worker') so benchmark spend is
    visible in ``provider_call_usage`` — best-effort: a failure to persist the
    cost observation does not fail the call itself.
    """
    from omniagentos.adapters.registry import resolve_adapter
    from omniagentos.contracts import AgentInput, BudgetSpec

    adapter = resolve_adapter("api-openrouter")
    agent_input = AgentInput(
        run_id=str(kwargs.get("run_id", "memcert")),
        task_id=str(kwargs.get("task_id", "memcert-item")),
        prompt=user,
        model=model,
        metadata={"strict_model": True, "system_prompt": system},
        budget=BudgetSpec(wall_ms_max=wall_ms),
    )
    res = adapter.run(agent_input)
    usage = res.usage
    cost_usd = usage.cost_usd if usage is not None else None
    tokens_in = usage.input_tokens if usage is not None else None
    tokens_out = usage.output_tokens if usage is not None else None
    err = None
    if str(res.status) != "ok":
        err = res.error or f"status={res.status}"
    try:
        observations = agent_input.metadata.get("cost_observations") or []
        if observations:
            from omniagentos.contracts import default_db_path
            from omniagentos.db.store import SqliteStore

            store = SqliteStore(default_db_path())
            for obs in observations:
                store.record_provider_call(obs)
    except Exception:  # noqa: BLE001 - cost persistence is best-effort telemetry
        pass
    return {
        "text": res.output_text,
        "cost_usd": cost_usd,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "error": err,
    }


def _is_terminal_error(err: str) -> bool:
    low = err.lower()
    return any(marker in low for marker in _TERMINAL_ERROR_MARKERS)


def _call_with_retry(
    adapter_fn: Callable[..., dict[str, Any]],
    *,
    model: str,
    system: str,
    user: str,
    wall_ms: int,
    arm: str,
    parked: set[tuple[str, str]],
    parked_lock: Lock,
    max_attempts: int,
    backoffs: tuple[float, ...],
    call_kwargs: dict[str, Any],
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    with parked_lock:
        if (arm, model) in parked:
            return {
                "text": "",
                "cost_usd": None,
                "tokens_in": None,
                "tokens_out": None,
                "error": "parked: terminal error previously observed for this arm/model pair",
                "attempts": 0,
                "parked": True,
            }

    last: dict[str, Any] = {}
    attempts = 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            result = adapter_fn(model, system, user, wall_ms, **call_kwargs)
        except Exception as exc:  # noqa: BLE001 - adapter boundary, never crash the runner
            result = {
                "text": "",
                "cost_usd": None,
                "tokens_in": None,
                "tokens_out": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        last = result
        err = result.get("error")
        if not err:
            return {**result, "attempts": attempts}
        if _is_terminal_error(str(err)):
            with parked_lock:
                parked.add((arm, model))
            return {**result, "attempts": attempts, "terminal": True}
        if attempt < max_attempts:
            sleep_fn(backoffs[min(attempt - 1, len(backoffs) - 1)])
    return {**last, "attempts": attempts}


# --------------------------------------------------------------------------
# default grading / summarizing (used only when scripts/memcert/grade.py is
# unavailable and the caller doesn't inject grader_fn/summarizer_fn; when
# grade.py IS present it is the resolved default and its contract is
# authoritative: ``grade_rows(items_by_id, rows) -> list[dict]`` (items FIRST)
# and ``summarize(rows, bars=None, k=None, boot_seed=1, n_boot=2000) ->
# {"<axis>/<arm>/<model>": {axis, arm, model, n_rows, n_items, n_trials, mean,
# ci_lo, ci_hi, pass_k, verdicts}}`` -- a FLAT dict keyed by the
# "axis/arm/model" string, not a nested mapping. The fallbacks below mirror
# that exact shape (minus grade.py's proper cluster bootstrap) so report.py
# and run()'s bar-check never need to know which one produced the summary.
# Neither grader supplies ``parsed`` or ``cost_usd`` -- run() fills those in
# itself (parsing needs only the item's answer_spec KIND, never its VALUE;
# cost is harness telemetry, not a grading concern).
# --------------------------------------------------------------------------


def _default_grade_rows(
    items_by_id: dict[str, Any], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    graded = []
    for row in rows:
        item = items_by_id.get(row["item_id"])
        if item is None:
            raise KeyError(f"result row references unknown item_id: {row['item_id']!r}")
        verdict, score = core.grade_item(item.answer_spec, row.get("raw_answer") or "")
        new_row = dict(row)
        new_row["verdict"] = verdict
        new_row["score"] = score
        new_row.setdefault("axis", item.axis)
        new_row.setdefault("level", item.level)
        new_row.setdefault("cluster_id", item.cluster_id)
        graded.append(new_row)
    return graded


def _bootstrap_ci(
    scores: list[float], seed: int, name: str, resamples: int = 500
) -> tuple[float | None, float | None]:
    if not scores:
        return (None, None)
    if len(scores) == 1:
        return (scores[0], scores[0])
    rng = core.rng_for(seed, f"ci:{name}")
    n = len(scores)
    means = []
    for _ in range(resamples):
        means.append(sum(scores[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo_idx = int(0.025 * resamples)
    hi_idx = min(int(0.975 * resamples), resamples - 1)
    return (means[lo_idx], means[hi_idx])


def _default_summarize(
    rows: list[dict[str, Any]],
    bars: dict[str, float] | None = None,
    k: int | None = None,
    boot_seed: int = 1,
    n_boot: int = 2000,  # noqa: ARG001 - kept for signature parity with grade.summarize
) -> dict[str, Any]:
    """Fallback mirror of ``grade.summarize`` (non-clustered bootstrap)."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(str(r.get("axis", "")), str(r.get("arm", "")), str(r.get("model", "")))].append(r)

    out: dict[str, Any] = {}
    for (axis, arm, model), items in sorted(groups.items()):
        scores = [float(r["score"]) for r in items]
        n = len(scores)
        mean = sum(scores) / n if n else None
        ci_lo, ci_hi = _bootstrap_ci(scores, boot_seed, f"{axis}:{arm}:{model}")
        verdicts: dict[str, int] = defaultdict(int)
        for r in items:
            verdicts[str(r.get("verdict", ""))] += 1
        by_trial: dict[Any, list[float]] = defaultdict(list)
        for r in items:
            by_trial[r.get("trial", 0)].append(float(r["score"]))
        pass_k: bool | None = None
        if bars is not None and k is not None and axis in bars and mean is not None:
            bar = float(bars[axis])
            trial_means = {t: sum(v) / len(v) for t, v in by_trial.items()}
            pass_k = len(trial_means) >= k and all(tm >= bar for tm in trial_means.values())
        out[f"{axis}/{arm}/{model}"] = {
            "axis": axis,
            "arm": arm,
            "model": model,
            "n_rows": n,
            "n_items": len({r.get("item_id") for r in items}),
            "n_trials": len(by_trial),
            "mean": mean,
            "ci_lo": ci_lo,
            "ci_hi": ci_hi,
            "pass_k": pass_k,
            "verdicts": dict(verdicts),
        }
    return out


def _resolve_grader(
    grader_fn: Callable[..., list[dict[str, Any]]] | None,
) -> Callable[..., list[dict[str, Any]]]:
    if grader_fn is not None:
        return grader_fn
    if grade_mod is not None and hasattr(grade_mod, "grade_rows"):
        return grade_mod.grade_rows  # type: ignore[no-any-return]
    return _default_grade_rows


def _resolve_summarizer(
    summarizer_fn: Callable[..., dict[str, Any]] | None,
) -> Callable[..., dict[str, Any]]:
    if summarizer_fn is not None:
        return summarizer_fn
    if grade_mod is not None and hasattr(grade_mod, "summarize"):
        return grade_mod.summarize  # type: ignore[no-any-return]
    return _default_summarize


# --------------------------------------------------------------------------
# run()
# --------------------------------------------------------------------------


@dataclass
class RunResult:
    exit_code: int
    out_dir: Path
    summary: dict[str, Any] | None
    rows: list[dict[str, Any]] = field(default_factory=list)
    refused: bool = False


def _atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    os.replace(tmp, path)


def run(
    *,
    models: list[str],
    arms: list[str],
    axes: list[str] | None = None,
    trials: int = 3,
    split: str = "dev",
    seeds: list[int],
    scale: str = "S",
    out_dir: Path,
    adapter: str = "mock",
    budget_tokens: int = 24000,
    max_workers: int = 4,
    wall_ms: int = 90000,
    limit_items: int | None = None,
    worlds: dict[int, WorldLike] | None = None,
    context_builder: Callable[..., core.ArmContext] | None = None,
    grader_fn: Callable[..., list[dict[str, Any]]] | None = None,
    summarizer_fn: Callable[..., dict[str, Any]] | None = None,
    adapter_fn: Callable[..., dict[str, Any]] | None = None,
    bars: dict[str, float] | None = None,
    k_trials: int = 3,
    boot_seed: int = 1,
    n_boot: int = 2000,
    run_id: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoffs: tuple[float, ...] = DEFAULT_BACKOFFS,
) -> RunResult:
    out_dir = Path(out_dir)
    axes = list(axes) if axes else list(core.AXES)
    run_id = run_id or out_dir.name
    run_uuid = str(uuid.uuid4())

    summary_path = out_dir / "summary.json"
    if summary_path.exists():
        # Unchanged-input refusal (2026-08-06 gate-retry doctrine): a target
        # that already has a completed run is not re-run silently.
        return RunResult(exit_code=EXIT_REFUSED, out_dir=out_dir, summary=None, refused=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    fixtures_root = out_dir / "fixtures"
    results_path = out_dir / "results.jsonl"
    costs_path = out_dir / "costs.jsonl"

    if worlds is None:
        if gen_mod is None or not hasattr(gen_mod, "generate_world"):
            raise RuntimeError(
                "scripts.memcert.gen is not available; pass worlds={seed: world, ...} "
                "explicitly (offline/test callers must inject worlds)."
            )
        worlds = {seed: gen_mod.generate_world(seed, scale, split) for seed in seeds}

    if context_builder is None:
        if arms_mod is None or not hasattr(arms_mod, "build_context"):
            raise RuntimeError(
                "scripts.memcert.arms is not available; pass context_builder=... "
                "explicitly (offline/test callers must inject a context builder)."
            )
        context_builder = arms_mod.build_context

    grader = _resolve_grader(grader_fn)
    summarizer = _resolve_summarizer(summarizer_fn)

    # Grok review BLOCKER-2: on the cert split the raw seed IS the answer key
    # (grade-time re-derivation), so NO run artifact may carry it — dir names,
    # rows, and the manifest all use the seed-hash tag instead. Dev keeps raw
    # seeds for debuggability.
    def _seed_tag(seed: int) -> str:
        if split == "dev":
            return f"w{seed}"
        return "wh" + hashlib.sha256(str(seed).encode("utf-8")).hexdigest()[:10]

    tasks: list[tuple[int, Path, Any, str, str, int]] = []
    items_by_id: dict[str, Any] = {}
    for seed in seeds:
        world = worlds[seed]
        wdir = fixtures_root / _seed_tag(seed)
        world.write_fixtures(wdir)
        # gen.World fixes the split at generate time (items() takes no args);
        # injected test worlds may still accept items(split) — support both.
        try:
            raw_items = world.items()
        except TypeError:
            raw_items = world.items(split)
        world_items = [i for i in raw_items if i.axis in axes]
        world_items.sort(key=lambda i: i.item_id)
        if limit_items is not None:
            capped: list[Any] = []
            per_axis: dict[str, int] = defaultdict(int)
            for item in world_items:
                if per_axis[item.axis] >= limit_items:
                    continue
                per_axis[item.axis] += 1
                capped.append(item)
            world_items = capped
        for item in world_items:
            items_by_id[item.item_id] = item
            for arm in arms:
                for model in models:
                    for trial in range(trials):
                        tasks.append((seed, wdir, item, arm, model, trial))

    parked: set[tuple[str, str]] = set()
    parked_lock = Lock()
    results_lock = Lock()
    costs_lock = Lock()
    rows: list[dict[str, Any]] = []

    resolved_adapter_fn = adapter_fn
    if resolved_adapter_fn is None:
        if adapter == "mock":
            resolved_adapter_fn = mock_adapter_call
        elif adapter == "openrouter":
            resolved_adapter_fn = _openrouter_adapter_call
        else:
            raise ValueError(f"unknown adapter: {adapter!r}")

    def _execute(task: tuple[int, Path, Any, str, str, int]) -> dict[str, Any]:
        seed, wdir, item, arm, model, trial = task
        ctx_rng = core.rng_for(seed, f"context:{arm}:{item.item_id}")
        # Sol review MC-001: the arm under test must NEVER see the answer key.
        # Context builders receive a REDACTED item (empty answer spec); only
        # the runner/grader keep the real one. Arms need question/overrides/
        # scope, not answers.
        redacted_item = replace(
            item, answer_spec=core.AnswerSpec(kind=item.answer_spec.kind, value="")
        )
        context = context_builder(arm, wdir, redacted_item, budget_tokens, ctx_rng)
        system, user = build_messages(item, context, trial)
        assert_no_answer_leak(system, item.answer_spec)
        call_kwargs = {
            "run_id": run_id,
            "task_id": f"{item.item_id}:{arm}:{model}:{trial}",
            "seed": seed,
            "item_id": item.item_id,
            "arm": arm,
            "trial": trial,
        }
        t0 = time.monotonic()
        result = _call_with_retry(
            resolved_adapter_fn,  # type: ignore[arg-type]
            model=model,
            system=system,
            user=user,
            wall_ms=wall_ms,
            arm=arm,
            parked=parked,
            parked_lock=parked_lock,
            max_attempts=max_attempts,
            backoffs=backoffs,
            call_kwargs=call_kwargs,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        row = {
            "item_id": item.item_id,
            "axis": item.axis,
            "level": item.level,
            "split": item.split,
            "arm": arm,
            "model": model,
            "trial": trial,
            "seed": seed if split == "dev" else _seed_tag(seed),
            "cluster_id": item.cluster_id,
            "raw_answer": result.get("text") or "",
            "latency_ms": latency_ms,
            "cost_usd": result.get("cost_usd"),
            "tokens": {"in": result.get("tokens_in"), "out": result.get("tokens_out")},
            "error": result.get("error"),
            "arm_meta": context.meta if isinstance(context.meta, dict) else {},
            "run_uuid": run_uuid,
        }
        with results_lock:
            with results_path.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
        with costs_lock:
            with costs_path.open("a") as fh:
                fh.write(
                    json.dumps(
                        {
                            "item_id": item.item_id,
                            "arm": arm,
                            "model": model,
                            "trial": trial,
                            "seed": seed if split == "dev" else _seed_tag(seed),
                            "cost_usd": row["cost_usd"],
                            "tokens": row["tokens"],
                        }
                    )
                    + "\n"
                )
        return row

    start = time.monotonic()
    if tasks:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_execute, task) for task in tasks]
            for fut in as_completed(futures):
                rows.append(fut.result())
    wall_time_ms = int((time.monotonic() - start) * 1000)

    # Only successfully-answered rows are graded/scored: an adapter error means
    # the harness never observed an answer at all, so raw_answer=="" must
    # never be fed to core.grade_item as if it were a genuine (wrong/abstain)
    # reply -- that would silently launder instrument failures into the score
    # (2026-08-06 doctrine: "an instrument error must never be reported as a
    # candidate defect"). Error rows still get a results.jsonl row (verdict
    # "error", score None) so they are visible, just never averaged in.
    ok_rows = [r for r in rows if r.get("error") is None]
    error_rows = [r for r in rows if r.get("error") is not None]

    graded_ok = grader(items_by_id, ok_rows)
    for row in graded_ok:
        item = items_by_id[row["item_id"]]
        raw = row.get("raw_answer") or ""
        row["parsed"] = (
            core.extract_json_object(raw)
            if item.answer_spec.kind == "params"
            else core.extract_answer(raw)
        )

    graded_error = []
    for row in error_rows:
        new_row = dict(row)
        new_row["parsed"] = None
        new_row["verdict"] = "error"
        new_row["score"] = None
        graded_error.append(new_row)

    graded_rows = graded_ok + graded_error
    graded_rows.sort(key=lambda r: (r["item_id"], r["arm"], r["model"], r["trial"]))
    _atomic_write_jsonl(results_path, graded_rows)

    axes_summary = (
        summarizer(graded_ok, bars=bars, k=k_trials, boot_seed=boot_seed, n_boot=n_boot)
        if graded_ok
        else {}
    )

    # cost/spend is harness telemetry, not a grading concern -- merge it into
    # each summary cell from ALL rows (error or not: a call can still incur
    # cost before it fails).
    cost_by_key: dict[str, float] = defaultdict(float)
    for row in rows:
        cost_by_key[f"{row['axis']}/{row['arm']}/{row['model']}"] += float(row.get("cost_usd") or 0.0)
    for key, entry in axes_summary.items():
        entry["cost_usd"] = round(cost_by_key.get(key, 0.0), 6)

    total_cost = sum(cost_by_key.values())
    error_rows = [r for r in rows if r.get("error")]
    # Sol review MC-003: errors are never silently favorable. A run where more
    # than 20% of calls errored is an instrument failure regardless of how the
    # surviving rows scored.
    instrument_failure = (
        (not rows) or (not graded_ok) or (len(error_rows) / len(rows) > 0.20 if rows else True)
    )
    errors_by_key: dict[str, int] = defaultdict(int)
    for r in error_rows:
        errors_by_key[f"{r.get('axis')}/{r.get('arm')}/{r.get('model')}"] += 1

    bar_failed = False
    if bars:
        for key, entry in axes_summary.items():
            bar = bars.get(entry["axis"])
            if bar is None or entry["mean"] is None:
                continue
            if entry["mean"] < bar:
                bar_failed = True
            # Sol review MC-002: pass^k is a gate, not decoration — a measured
            # False fails the bar even when the pooled mean clears it.
            if entry.get("pass_k") is False:
                entry["pass_k_failed"] = True
                bar_failed = True
            # Sol review MC-003: a cell missing >5% of its calls to errors
            # cannot certify — a model must not pass by answering only its
            # easy subset while the rest time out.
            cell_errors = errors_by_key.get(key, 0)
            cell_total = (entry.get("n_rows") or 0) + cell_errors
            if cell_total and cell_errors / cell_total > 0.05:
                entry["error_degraded"] = True
                bar_failed = True
            # Grok review SHOULD-FIX-3: an axis where >90% of rows abstained is
            # a degenerate cell — all-abstain trivially "meets" a 0.0 bar with
            # zero memory ability. Degenerate cells fail their bar; absence of
            # ability is never favorable.
            verdicts = entry.get("verdicts") or {}
            cell_rows = entry.get("n_rows") or 0
            abstains = int(verdicts.get("abstain_miss", 0))
            if cell_rows and entry["axis"] != "E" and abstains / cell_rows > 0.9:
                entry["degenerate_abstain"] = True
                bar_failed = True
        # A cell whose EVERY call errored has no graded rows and therefore no
        # summary entry — it must fail loudly, not vanish (Sol MC-003).
        for key in errors_by_key:
            if key not in axes_summary:
                bar_failed = True

    if instrument_failure:
        exit_code = EXIT_INSTRUMENT_FAILURE
    elif bar_failed:
        exit_code = EXIT_BAR_FAILED
    else:
        exit_code = EXIT_OK

    summary = {
        "run_id": run_id,
        "manifest": {
            "models": models,
            "arms": arms,
            "axes": axes,
            "trials": trials,
            "split": split,
            "seeds": seeds if split == "dev" else [_seed_tag(s) for s in seeds],
            "scale": scale,
            "adapter": adapter,
            "budget_tokens": budget_tokens,
            "max_workers": max_workers,
            "wall_ms": wall_ms,
            "limit_items": limit_items,
            "k_trials": k_trials,
            "boot_seed": boot_seed,
            "n_boot": n_boot,
            "git_sha": os.environ.get("GIT_SHA"),
            "canary": core.canary_line(run_uuid),
            "run_uuid": run_uuid,
            "wall_time_ms": wall_time_ms,
            "total_cost_usd": total_cost,
        },
        "axes": axes_summary,
        "parked_pairs": sorted(f"{a}:{m}" for a, m in parked),
        "row_count": len(graded_rows),
        "error_count": sum(1 for r in graded_rows if r.get("error")),
    }
    with (out_dir / "summary.json.tmp").open("w") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
    os.replace(out_dir / "summary.json.tmp", summary_path)

    return RunResult(exit_code=exit_code, out_dir=out_dir, summary=summary, rows=graded_rows)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _split_csv(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


def _split_csv_int(value: str) -> list[int]:
    return [int(p.strip()) for p in value.split(",") if p.strip()]


def _load_bars(path: str) -> tuple[dict[str, float], int]:
    """Returns (bars, k_trials); k_trials defaults to 3 when the file omits it."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is present in .venv
        raise RuntimeError(f"--bars requires PyYAML to read {path!r}: {exc}") from exc
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    bars = data.get("bars", data)
    k = int(data.get("k_trials", 3)) if isinstance(data, dict) else 3
    return {str(k_): float(v) for k_, v in bars.items()}, k


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="memcert benchmark runner")
    p.add_argument("--models", required=True, help="comma-separated model ids")
    p.add_argument("--arms", required=True, help="comma-separated arm names")
    p.add_argument("--axes", default=",".join(core.AXES), help="comma-separated axis letters")
    p.add_argument("--trials", type=int, default=3)
    p.add_argument("--split", choices=core.SPLITS, default="dev")
    p.add_argument("--seeds", required=True, help="comma-separated ints, one world per seed")
    p.add_argument("--scale", default="S")
    p.add_argument("--out", default=None, help="default: var/memcert/runs/<utc-run-id>")
    p.add_argument("--adapter", choices=("openrouter", "mock"), default="mock")
    p.add_argument("--budget-tokens", type=int, default=24000)
    p.add_argument("--max-workers", type=int, default=4)
    p.add_argument("--wall-ms", type=int, default=90000)
    p.add_argument("--limit-items", type=int, default=None)
    p.add_argument("--bars", default=None, help="path to configs/memcert/bars.yaml (cert mode)")
    p.add_argument("--k-trials", type=int, default=None, help="default: k_trials from --bars, else 3")
    p.add_argument("--boot-seed", type=int, default=1)
    p.add_argument("--n-boot", type=int, default=2000)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    out_dir = Path(args.out) if args.out else default_out_dir()
    bars, bars_k = (_load_bars(args.bars) if args.bars else (None, 3))
    k_trials = args.k_trials if args.k_trials is not None else bars_k

    result = run(
        models=_split_csv(args.models),
        arms=_split_csv(args.arms),
        axes=_split_csv(args.axes),
        trials=args.trials,
        split=args.split,
        seeds=_split_csv_int(args.seeds),
        scale=args.scale,
        out_dir=out_dir,
        adapter=args.adapter,
        budget_tokens=args.budget_tokens,
        max_workers=args.max_workers,
        wall_ms=args.wall_ms,
        limit_items=args.limit_items,
        bars=bars,
        k_trials=k_trials,
        boot_seed=args.boot_seed,
        n_boot=args.n_boot,
    )

    if result.refused:
        print(f"REFUSED: {result.out_dir}/summary.json already exists (unchanged input)", file=sys.stderr)
        return result.exit_code

    assert result.summary is not None
    try:
        from . import report as report_mod
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import report as report_mod  # type: ignore[no-redef]

    (result.out_dir / "SUMMARY.md").write_text(report_mod.render_summary_md(result.summary))
    report_mod.write_junit(result.summary, result.out_dir / "junit.xml", bars=bars)

    print(f"memcert run {result.summary['run_id']}: exit={result.exit_code}")
    print(json.dumps(result.summary["manifest"], indent=2, sort_keys=True))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
