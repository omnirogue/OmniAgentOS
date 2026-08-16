"""``omniagentos.swarm.optimize`` — WP8: the twice-daily swarm optimizer.

Run via ``python -m omniagentos.swarm.optimize`` (by hand, or the render-not-load
launchd template in ``scripts/swarm/``, staggered 15 minutes off the
selfimprove-curator's 3:30/15:30 slots — see ``scripts/swarm/launchd.py``).

WHAT THIS DOES: mines finished (``completed``/``failed``/``cancelled``) swarm
runs since the last watermark for recurring bottlenecks, per-provider success
rates/latencies, sizing (target-concurrency vs utilization) observations, and
observed tier win rates — then:

1. **Appends** a dated recommendations section to ``vault/swarm/playbook.md``
   (mechanical aggregation, always; ONE optional Kimi narrative
   subsection that degrades silently on ANY LLM failure — mirrors
   ``omniagentos.swarm.summary``'s ``_narrative_section``). A human reads and
   applies these; nothing here is executed automatically.
2. **Best-effort** ingests one knowledge fact per pass (discipline ``"swarm"``,
   source ``curator`` — this module IS a curator, over swarm runs instead of
   the run ledger).
3. **Refreshes** ``var/swarm/learned.json`` wholesale: an OBSERVED-DATA-ONLY
   snapshot (per-provider success rate + median attempt latency, and
   observed-good concurrency by plan size), bounded to sane ranges. The
   scheduler's router MAY consult this as advisory input with hard bounds —
   see the plan doc's WP8 bullet — but nothing in this repo writes it back
   into ``configs/swarm.yaml`` or any other config, ever. That edit, if any,
   is a human's call after reading the playbook.

WATERMARK (idempotency): ``var/swarm/optimize-state.json`` records the
``(finished_at, rowid)`` pair of the last run this module has already
folded into the playbook + knowledge fact. A pass with nothing new since the
watermark writes nothing to the playbook and leaves the watermark untouched
— running this twice in a row (by hand, or because two launchd slots somehow
both fired) can never double-append a playbook section. ``learned.json`` is
a full recomputed snapshot every pass regardless (harmless to refresh with
identical numbers when nothing changed) — it is advisory current-state, not
an incremental log.

NEVER touches config. Recommendations are always human-applied.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.contracts import (
    NoteType,
    VaultFrontmatter,
    default_db_path,
    default_vault_dir,
)
from omniagentos.routing.cascade import default_trace_path
from omniagentos.swarm.contracts import (
    ACTION_PROVIDER_SWITCHED,
    ACTION_RATE_LIMIT,
    ACTION_RATE_LIMIT_STALL,
    ACTION_TASK_SPLIT,
)
from omniagentos.swarm.costgreen import (
    cost_to_green_by_task,
    effort_stats_as_dict,
    summarize_by_effort,
)
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.spawn import default_swarm_var_root
from omniagentos.vault.errors import VaultError
from omniagentos.vault.frontmatter import render_frontmatter, split_frontmatter
from omniagentos.vault.sections import extract_section

LOG = logging.getLogger(__name__)

FableRunner = Callable[..., dict[str, Any] | None]
KnowledgeIngestFn = Callable[[str, str], None]

_DEFAULT_BATCH_LIMIT = 200  # runs processed per pass (mirrors selfimprove.curator's scan limit)
_DEFAULT_HISTORY_LIMIT = 500  # runs kept in the learned.json rolling snapshot
# The optimizer's learned "observed-good concurrency" recommendation is clamped
# here. Kept equal to swarm.scheduler.MAX_SLOTS / planner.TARGET_N_HARD_CEILING:
# a lower ceiling would make the playbook permanently unable to recommend the
# widths the fleet can now actually run (fleet-scale-200: 10 -> 20).
_MAX_CONCURRENCY_CEILING = 20
_GOOD_UTILIZATION_THRESHOLD = 0.6
# The heading swarm.summary.write_summary uses in a RUN's OWN vault note --
# what _collect_narrative_excerpts mines those notes for. One level higher
# (##, not ###) than this module's own playbook subsection heading below,
# because it lives in a different document with a different heading scheme.
_RUN_NARRATIVE_HEADINGS = (
    "## Improvement opportunities (Kimi)",
    "## Improvement opportunities (Fable)",  # historical sections
)

# This module's own optional-narrative subsection heading inside a dated
# playbook section -- one level below the section's own "##" heading, in
# line with every other "###" subsection _render_playbook_section writes.
_PLAYBOOK_NARRATIVE_HEADING = "### Improvement opportunities (Kimi)"

# (label, min_tasks, max_tasks-or-None) — plan-size buckets for the playbook.
# Open-ended on the right so a 30-task plan still lands in "large".
_PLAN_SIZE_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("small (1-3 tasks)", 1, 3),
    ("medium (4-8 tasks)", 4, 8),
    ("large (9+ tasks)", 9, None),
)

# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def default_state_path() -> str:
    return str(default_swarm_var_root() / "optimize-state.json")


def default_learned_path() -> str:
    return str(default_swarm_var_root() / "learned.json")


def default_playbook_path(vault_dir: str | None = None) -> str:
    return os.path.join(vault_dir or default_vault_dir(), "swarm", "playbook.md")


# ---------------------------------------------------------------------------
# small parsing / io helpers (defensive: every field here is untrusted DB/disk text)
# ---------------------------------------------------------------------------


def _parse_json(value: Any, *, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default
    return default if parsed is None else parsed


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return "?"


def _atomic_write(path: Path, content: str) -> None:
    """tmp + fsync + rename — the plan doc's durable-write idiom, reused here
    because ``playbook.md``/``learned.json`` may be read by other tooling
    (dashboard, router) concurrently with this twice-daily writer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_state(path: str | Path) -> dict[str, Any]:
    """Read the watermark file; a missing/corrupt file means "from the
    beginning" — never an error (mirrors ``routing.learn.read_traces``'s
    "missing file = no history yet" contract)."""
    resolved = Path(path)
    if not resolved.is_file():
        return {}
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_state(path: str | Path, state: Mapping[str, Any]) -> None:
    _atomic_write(Path(path), json.dumps(dict(state), indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# aggregation — providers (success rate + median attempt latency)
# ---------------------------------------------------------------------------


def _aggregate_providers(
    dal: SwarmDal, runs: Sequence[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Per-provider attempts/successes/success_rate/median_attempt_minutes
    across every ``swarm_attempts`` row of the given runs. Bounded to [0, 1]
    for success_rate by construction (successes <= attempts)."""
    attempts_by_provider: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        for attempt in dal.attempts_for_run(str(run["id"])):
            provider = str(attempt.get("provider") or "unknown")
            attempts_by_provider[provider].append(attempt)

    result: dict[str, dict[str, Any]] = {}
    for provider, attempts in attempts_by_provider.items():
        total = len(attempts)
        successes = sum(1 for a in attempts if str(a.get("end_reason") or "") == "completed")
        durations: list[float] = []
        for attempt in attempts:
            started = _parse_iso(attempt.get("started_at"))
            ended = _parse_iso(attempt.get("ended_at"))
            if started is not None and ended is not None and ended >= started:
                durations.append((ended - started).total_seconds() / 60.0)
        median_minutes = round(statistics.median(durations), 3) if durations else None
        result[provider] = {
            "attempts": total,
            "successes": successes,
            "success_rate": round(_clamp(successes / total if total else 0.0, 0.0, 1.0), 4),
            "median_attempt_minutes": median_minutes,
        }
    return result


# ---------------------------------------------------------------------------
# aggregation — recurring bottlenecks (raw swarm.event rows)
# ---------------------------------------------------------------------------


def _aggregate_events(dal: SwarmDal, runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Recurring-bottleneck tallies straight off ``swarm.event`` rows — the
    detail WP7's per-run ``metrics_json`` does not carry (task_split counts,
    which providers a switch moved between, which provider got rate-limited)."""
    stall_count = 0
    stall_seconds = 0.0
    split_count = 0
    switch_count = 0
    switch_pairs: Counter[tuple[str, str]] = Counter()
    rate_limit_by_provider: Counter[str] = Counter()

    for run in runs:
        for event in dal.events_for_run(str(run["id"])):
            action = event.get("action")
            payload = event.get("payload") or {}
            if action == ACTION_RATE_LIMIT_STALL:
                stall_count += 1
                try:
                    stall_seconds += float(payload.get("seconds") or 0.0)
                except (TypeError, ValueError):
                    pass
            elif action == ACTION_TASK_SPLIT:
                split_count += 1
            elif action == ACTION_PROVIDER_SWITCHED:
                switch_count += 1
                frm = str(payload.get("from_provider") or "?")
                to = str(payload.get("to_provider") or "?")
                switch_pairs[(frm, to)] += 1
            elif action == ACTION_RATE_LIMIT:
                rate_limit_by_provider[str(payload.get("provider") or "?")] += 1

    return {
        "stall_count": stall_count,
        "stall_seconds": round(stall_seconds, 1),
        "split_count": split_count,
        "switch_count": switch_count,
        "switch_pairs": sorted(switch_pairs.items()),
        "rate_limit_by_provider": sorted(rate_limit_by_provider.items()),
    }


# ---------------------------------------------------------------------------
# aggregation — sizing (target concurrency vs utilization, by plan size)
# ---------------------------------------------------------------------------


def _bucket_for(tasks_total: int) -> str | None:
    for label, lo, hi in _PLAN_SIZE_BUCKETS:
        if tasks_total >= lo and (hi is None or tasks_total <= hi):
            return label
    return None


def _aggregate_sizing(runs: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Bucket runs by plan size (``metrics_json.tasks_total``) and report the
    median observed ``mean_target_n``/``utilization``, plus an "observed-good"
    concurrency: the median ``mean_target_n`` among runs in the bucket that
    cleared ``_GOOD_UTILIZATION_THRESHOLD`` utilization (falling back to the
    measured bucket's overall median target_n when no run cleared the bar).
    Runs whose utilization is ``None`` remain counted but never become sizing
    exemplars; their median utilization and recommendation stay unknown.
    Clamped to [1, _MAX_CONCURRENCY_CEILING] — the same ceiling
    ``configs/swarm.yaml``'s ``max_concurrent_swarms``/the scheduler's
    ``target_n`` clamp already enforce.
    """
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        metrics = _parse_json(run.get("metrics_json"), default={})
        if not isinstance(metrics, dict) or not metrics:
            continue
        tasks_total = int(metrics.get("tasks_total") or 0)
        label = _bucket_for(tasks_total)
        if label is None:
            continue
        buckets[label].append(metrics)

    result: dict[str, dict[str, Any]] = {}
    for label, metrics_list in buckets.items():
        target_ns = [
            float(value)
            for metrics in metrics_list
            if (value := metrics.get("mean_target_n")) is not None
        ]
        measured = [
            (float(target_n), _clamp(float(utilization), 0.0, 1.0))
            for metrics in metrics_list
            if (target_n := metrics.get("mean_target_n")) is not None
            and (utilization := metrics.get("utilization")) is not None
        ]
        utils = [utilization for _, utilization in measured]
        good_ns = [
            target_n
            for target_n, utilization in measured
            if utilization >= _GOOD_UTILIZATION_THRESHOLD
        ] or [target_n for target_n, _ in measured]
        observed_good = int(round(statistics.median(good_ns))) if good_ns else None
        result[label] = {
            "runs": len(metrics_list),
            "median_target_n": round(statistics.median(target_ns), 2) if target_ns else 0.0,
            "median_utilization": round(statistics.median(utils), 4) if utils else None,
            "observed_good_concurrency": (
                int(_clamp(observed_good, 1, _MAX_CONCURRENCY_CEILING))
                if observed_good is not None
                else None
            ),
        }
    return result


# ---------------------------------------------------------------------------
# aggregation — win rate by router-decided effort (048; playbook + learned.json
# advisory data for the future learned adjustment behind
# SwarmRouter._decide_effort — never auto-applied)
# ---------------------------------------------------------------------------

# Learner semantics (mirrors swarm.router's sample mining): completed is a
# win; review_denied/timeout mean the attempt was too weak; everything else
# (rate_limited/killed/crashed/auth_failed) is capacity or infra noise, not
# evidence about the effort level.
_EFFORT_WIN_REASONS = frozenset({"completed"})
_EFFORT_LOSS_REASONS = frozenset({"review_denied", "timeout"})
# Bucket for attempts with no recorded effort (pre-048 rows, or providers
# running at session default — e.g. the claude bridge has no effort knob).
_EFFORT_DEFAULT_BUCKET = "default"


def _aggregate_efforts(
    dal: SwarmDal, runs: Sequence[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Per-effort attempts/wins/win_rate across every ended ``swarm_attempts``
    row of the given runs, learner semantics (see the constants above)."""
    outcomes_by_effort: dict[str, list[bool]] = defaultdict(list)
    for run in runs:
        for attempt in dal.attempts_for_run(str(run["id"])):
            end_reason = str(attempt.get("end_reason") or "")
            if end_reason in _EFFORT_WIN_REASONS:
                win = True
            elif end_reason in _EFFORT_LOSS_REASONS:
                win = False
            else:
                continue
            effort = str(attempt.get("effort") or "") or _EFFORT_DEFAULT_BUCKET
            outcomes_by_effort[effort].append(win)

    result: dict[str, dict[str, Any]] = {}
    for effort, wins in outcomes_by_effort.items():
        total = len(wins)
        won = sum(wins)
        result[effort] = {
            "attempts": total,
            "wins": won,
            "win_rate": round(_clamp(won / total if total else 0.0, 0.0, 1.0), 4),
        }
    return result


def _attempts_with_usage_for_run(dal: SwarmDal, run_id: str) -> list[dict[str, Any]]:
    """Prefer the joined usage view; fall back to raw attempts on older DALs."""
    reader = getattr(dal, "attempts_with_usage", None)
    if callable(reader):
        return list(reader(run_id))
    return list(dal.attempts_for_run(run_id))


def _aggregate_cost_to_green(
    dal: SwarmDal, runs: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Cost-to-green by starting effort across runs, with confidence bands.

    Surfaces the costgreen rates (and their measured/partial/sparse confidence)
    into the playbook and learned.json so a sparse comparison is never
    indistinguishable from a fully measured one. Unknown per-green rates stay
    explicit JSON nulls — callers must not treat null as 0.0.
    """
    records = []
    for run in runs:
        rows = _attempts_with_usage_for_run(dal, str(run["id"]))
        records.extend(cost_to_green_by_task(rows))
    return [effort_stats_as_dict(stats) for stats in summarize_by_effort(records)]


def _fmt_optional_number(value: Any) -> str:
    """Render a per-green rate for the playbook. None is 'unknown', never '0'."""
    if value is None:
        return "unknown"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    return f"{float(value):.4g}"


# ---------------------------------------------------------------------------
# aggregation — observed tier win rates (playbook "routing hints" only; this
# module never feeds these back into routing — swarm.router owns the learner)
# ---------------------------------------------------------------------------


def _aggregate_tiers(db_path: str | None) -> dict[str, dict[str, Any]]:
    from omniagentos.swarm.router import default_samples_loader, swarm_task_class

    try:
        samples = default_samples_loader(db_path)
    except Exception:  # noqa: BLE001 -- an observability read must never break the pass.
        LOG.debug("swarm optimizer: tier sample read failed", exc_info=True)
        return {}

    by_class: dict[str, list[bool]] = defaultdict(list)
    for sample in samples:
        cls = swarm_task_class(str(sample.get("complexity") or "standard"))
        by_class[cls].append(bool(sample.get("win")))

    result: dict[str, dict[str, Any]] = {}
    for cls, wins in by_class.items():
        total = len(wins)
        won = sum(wins)
        result[cls] = {
            "attempts": total,
            "wins": won,
            "win_rate": round(_clamp(won / total if total else 0.0, 0.0, 1.0), 4),
        }
    return result


def _read_swarm_cascade_traces(trace_path: str | None = None) -> dict[str, dict[str, int]]:
    """Best-effort read of ``var/routing/cascade/traces.jsonl`` rows whose
    ``task_class`` is a swarm class (``swarm:<complexity>``) — populated today
    only if a future wiring routes swarm attempts through
    ``routing.cascade.run_cascade`` (the router currently mines
    ``swarm_attempts`` directly, see ``_aggregate_tiers`` above). An
    empty/missing file is the normal case, not an error — mirrors
    ``routing.learn.read_traces``."""
    path = trace_path or default_trace_path()
    result: dict[str, dict[str, int]] = {}
    if not os.path.exists(path):
        return result
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                task_class = str(row.get("task_class") or "")
                if not task_class.startswith("swarm:"):
                    continue
                bucket = result.setdefault(task_class, {"attempts": 0, "wins": 0})
                bucket["attempts"] += 1
                if row.get("win"):
                    bucket["wins"] += 1
    except OSError:
        LOG.debug("swarm optimizer: cascade trace read failed for %s", path, exc_info=True)
        return {}
    return result


# ---------------------------------------------------------------------------
# prior-run narrative excerpts (mined from each run's own vault summary note)
# ---------------------------------------------------------------------------


def _collect_narrative_excerpts(runs: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """Pull an already-written improvement section from each run summary.

    The Fable heading remains readable for historical notes; new summaries use
    Kimi.
    """
    excerpts: list[tuple[str, str]] = []
    for run in runs:
        note_path = run.get("summary_note_path")
        if not note_path:
            continue
        try:
            content = Path(str(note_path)).read_text(encoding="utf-8")
        except OSError:
            continue
        section = next(
            (
                found
                for heading in _RUN_NARRATIVE_HEADINGS
                if (found := extract_section(content, heading=heading))
            ),
            "",
        )
        if not section:
            continue
        body = "\n".join(section.splitlines()[1:]).strip()
        if body:
            excerpts.append((str(run.get("id") or ""), body))
    return excerpts


# ---------------------------------------------------------------------------
# optional Kimi narrative (degrades silently — mirrors swarm.summary's
# _narrative_section, same effort/turns/wall_ms envelope)
# ---------------------------------------------------------------------------


def _narrative_section(
    *,
    runs_analyzed: int,
    providers: Mapping[str, Any],
    bottlenecks: Mapping[str, Any],
    sizing: Mapping[str, Any],
    tier_win_rates: Mapping[str, Any],
    effort_win_rates: Mapping[str, Any],
    fable_runner: FableRunner | None,
) -> str | None:
    runner = fable_runner
    if runner is None:
        from omniagentos.improvement_chain import run_kimi_json

        runner = run_kimi_json

    bottleneck_summary = {
        k: v for k, v in bottlenecks.items() if k not in ("switch_pairs", "rate_limit_by_provider")
    }
    prompt = "\n".join(
        [
            "You are reviewing recent OmniAgentOS swarm runs (a twice-daily",
            "optimizer pass) for improvement opportunities. Be specific and",
            "concise (3-6 bullet points).",
            "",
            f"Runs analyzed this pass: {runs_analyzed}",
            f"Provider success rates: {json.dumps(providers, sort_keys=True)}",
            f"Bottlenecks: {json.dumps(bottleneck_summary, sort_keys=True)}",
            f"Sizing observations by plan size: {json.dumps(sizing, sort_keys=True)}",
            f"Observed tier win rates: {json.dumps(tier_win_rates, sort_keys=True)}",
            f"Observed effort win rates: {json.dumps(effort_win_rates, sort_keys=True)}",
            "",
            "Write 3-6 markdown bullet points: concrete routing/sizing",
            "recommendations a human could consider applying to",
            "configs/swarm.yaml. These are NEVER auto-applied -- say so only if",
            "directly relevant, don't pad with disclaimers.",
            'Return JSON: {"narrative": "...markdown bullet list..."}',
        ]
    )
    schema = {
        "type": "object",
        "required": ["narrative"],
        "properties": {"narrative": {"type": "string"}},
    }
    try:
        raw = runner(prompt, schema, effort="medium", max_turns=3, wall_ms=180_000)
    except Exception:  # noqa: BLE001 -- degrade to mechanical-only on ANY LLM failure.
        LOG.warning("swarm optimizer: narrative pass failed", exc_info=True)
        return None
    if not isinstance(raw, dict):
        return None
    narrative = raw.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        return None
    return narrative.strip()


# ---------------------------------------------------------------------------
# playbook.md — dated section render + append (append-only, preserves prior
# content; NOT the write_note()/merge_human_section() regenerate-in-place
# idiom, since this file is a growing log, not a re-rendered-from-DB note)
# ---------------------------------------------------------------------------


def _render_playbook_section(
    *,
    now: datetime,
    runs_analyzed: int,
    window_start: str | None,
    window_end: str | None,
    providers: Mapping[str, Mapping[str, Any]],
    bottlenecks: Mapping[str, Any],
    sizing: Mapping[str, Mapping[str, Any]],
    tier_win_rates: Mapping[str, Mapping[str, Any]],
    effort_win_rates: Mapping[str, Mapping[str, Any]],
    cascade_trace_tiers: Mapping[str, Mapping[str, Any]],
    narrative_excerpts: Sequence[tuple[str, str]],
    fable_narrative: str | None,
    cost_to_green: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    heading = now.strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [f"## {heading} -- Swarm optimizer pass", ""]

    window_bit = ""
    if window_start and window_end:
        window_bit = f" (finished between {window_start} and {window_end})"
    lines.append(f"**Runs analyzed:** {runs_analyzed}{window_bit}")
    lines.append("")

    lines.append("### Provider success rates & latency")
    lines.append("")
    if providers:
        lines.append("| Provider | Attempts | Successes | Success rate | Median attempt (min) |")
        lines.append("|---|---|---|---|---|")
        for provider, stats in sorted(providers.items()):
            median = stats.get("median_attempt_minutes")
            median_str = f"{median:.2f}" if isinstance(median, (int, float)) else "?"
            lines.append(
                f"| {provider} | {stats['attempts']} | {stats['successes']} | "
                f"{_fmt_pct(stats['success_rate'])} | {median_str} |"
            )
    else:
        lines.append("_No attempts recorded in this window._")
    lines.append("")

    lines.append("### Recurring bottlenecks")
    lines.append("")
    lines.append(
        f"- Rate-limit stalls: {bottlenecks.get('stall_count', 0)} event(s), "
        f"{bottlenecks.get('stall_seconds', 0):.0f}s total cooldown wait"
    )
    lines.append(f"- Task splits: {bottlenecks.get('split_count', 0)}")
    switch_pairs = bottlenecks.get("switch_pairs") or []
    switch_desc = (
        "; ".join(f"{frm} -> {to} x{count}" for (frm, to), count in switch_pairs) or "none"
    )
    lines.append(f"- Provider switches: {bottlenecks.get('switch_count', 0)} ({switch_desc})")
    rl_pairs = bottlenecks.get("rate_limit_by_provider") or []
    rl_desc = "; ".join(f"{provider} x{count}" for provider, count in rl_pairs) or "none"
    lines.append(f"- Rate-limit hits by provider: {rl_desc}")
    lines.append("")

    lines.append("### Sizing observations (target concurrency vs utilization)")
    lines.append("")
    if sizing:
        lines.append(
            "| Plan size | Runs | Median target_n | Median utilization | "
            "Observed-good concurrency |"
        )
        lines.append("|---|---|---|---|---|")
        for label, stats in sizing.items():
            observed_good = stats["observed_good_concurrency"]
            observed_good_text = str(observed_good) if observed_good is not None else "?"
            lines.append(
                f"| {label} | {stats['runs']} | {stats['median_target_n']:.2f} | "
                f"{_fmt_pct(stats['median_utilization'])} | {observed_good_text} |"
            )
    else:
        lines.append("_No sized runs in this window (metrics_json absent/empty)._")
    lines.append("")

    lines.append("### Routing hints (observed win rates -- advisory only, human-applied)")
    lines.append("")
    if tier_win_rates:
        lines.append("| Task class | Attempts | Wins | Win rate |")
        lines.append("|---|---|---|---|")
        for cls, stats in sorted(tier_win_rates.items()):
            lines.append(
                f"| {cls} | {stats['attempts']} | {stats['wins']} | {_fmt_pct(stats['win_rate'])} |"
            )
    else:
        lines.append("_No completed/denied/timed-out attempts recorded yet._")
    if cascade_trace_tiers:
        lines.append("")
        lines.append("Supplementary cascade-trace evidence:")
        for cls, stats in sorted(cascade_trace_tiers.items()):
            attempts = int(stats.get("attempts") or 0)
            wins = int(stats.get("wins") or 0)
            rate = wins / attempts if attempts else 0.0
            lines.append(f"- {cls}: {wins}/{attempts} ({_fmt_pct(rate)})")
    lines.append("")

    lines.append("### Effort win rates (router-decided reasoning effort -- advisory only)")
    lines.append("")
    if effort_win_rates:
        lines.append("| Effort | Attempts | Wins | Win rate |")
        lines.append("|---|---|---|---|")
        for effort, stats in sorted(effort_win_rates.items()):
            lines.append(
                f"| {effort} | {stats['attempts']} | {stats['wins']} | "
                f"{_fmt_pct(stats['win_rate'])} |"
            )
        lines.append("")
        lines.append(
            "_`default` = attempts with no recorded effort (pre-048 rows, or "
            "providers running at session default). Evidence for future "
            "`router.effort_by_tier`/`router.effort_overrides` edits -- "
            "human-applied only._"
        )
    else:
        lines.append("_No effort-attributable attempts recorded yet._")
    lines.append("")

    lines.append(
        "### Cost-to-green by starting effort "
        "(retries + escalations charged to the start -- advisory only)"
    )
    lines.append("")
    if cost_to_green:
        lines.append(
            "| Effort | Tasks | Green | Cost/green | Tokens/green | Wall ms/green | Confidence |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for row in cost_to_green:
            effort_label = row.get("effort")
            if effort_label is None or effort_label == "":
                effort_label = "default"
            # Explicit None checks — bare truthiness would treat measured 0.0
            # (a free CLI report) the same as unknown.
            cost_cell = _fmt_optional_number(row.get("cost_per_green"))
            tokens_cell = _fmt_optional_number(row.get("tokens_per_green"))
            wall_cell = _fmt_optional_number(row.get("wall_ms_per_green"))
            confidence = row.get("confidence")
            confidence_cell = confidence if isinstance(confidence, str) else "unknown"
            lines.append(
                f"| {effort_label} | {int(row.get('tasks') or 0)} | "
                f"{int(row.get('green') or 0)} | {cost_cell} | {tokens_cell} | "
                f"{wall_cell} | {confidence_cell} |"
            )
        lines.append("")
        lines.append(
            "_`unknown` means that dimension was not fully measured (absent, "
            "unparseable, or tokens-only for cost). Confidence is measured/"
            "partial/sparse from how many task chains had complete cost data; "
            "tokens and wall gate their own rates. Human-applied only — never "
            "auto-writes config._"
        )
    else:
        lines.append("_No board-task attempt chains recorded yet._")
    lines.append("")

    if narrative_excerpts:
        lines.append("### Prior run narrative excerpts")
        lines.append("")
        for run_id, excerpt in narrative_excerpts:
            first_line = excerpt.splitlines()[0] if excerpt else ""
            lines.append(f"- `{run_id}`: {first_line}")
        lines.append("")

    if fable_narrative:
        lines.append(_PLAYBOOK_NARRATIVE_HEADING)
        lines.append("")
        lines.append(fable_narrative)
        lines.append("")

    lines.append(
        "_Advisory only -- see `var/swarm/learned.json` for the machine-readable "
        "observed-data snapshot. Nothing here is auto-applied to `configs/swarm.yaml`; "
        "a human reads this and edits config by hand._"
    )
    return "\n".join(lines).rstrip("\n") + "\n"


def _load_existing_playbook(path: Path) -> tuple[VaultFrontmatter | None, str]:
    if not path.is_file():
        return None, ""
    content = path.read_text(encoding="utf-8")
    try:
        fm, body = split_frontmatter(content)
    except VaultError:
        return None, content  # pre-existing non-frontmatter file: keep it, no fm
    return fm, body


def append_playbook_section(playbook_path: str | Path, section_md: str, *, now_iso: str) -> str:
    """Append ``section_md`` to ``playbook.md``, preserving every byte already
    on disk. Creates the file (with frontmatter + intro) on first write."""
    path = Path(playbook_path)
    fm, body = _load_existing_playbook(path)
    if fm is None:
        fm = VaultFrontmatter(
            id="swarm-optimizer",
            type=NoteType.PLAYBOOK,
            discipline="swarm",
            created=now_iso,
            source_run=None,
            confidence=None,
            status="active",
            supersedes=None,
        )
        body = (
            "\n# Swarm optimizer playbook\n\n"
            "[[Home]]\n\n"
            "Twice-daily mechanical recommendations mined from finished swarm runs "
            "(`omniagentos.swarm.optimize`). Every section below is appended, never "
            "rewritten -- human-applied only. Nothing here is auto-edited into "
            "`configs/swarm.yaml` or any other config.\n"
        )
    new_body = body.rstrip("\n") + "\n\n" + section_md.rstrip("\n") + "\n"
    content = render_frontmatter(fm) + new_body
    _atomic_write(path, content)
    return str(path.resolve())


# ---------------------------------------------------------------------------
# learned.json — observed-data-only snapshot (bounds enforced by the
# aggregation helpers above: success_rate/utilization in [0,1],
# observed_good_concurrency in [1, _MAX_CONCURRENCY_CEILING])
# ---------------------------------------------------------------------------


def _build_learned(
    *,
    now_iso: str,
    since_finished_at: str | None,
    runs_analyzed_total: int,
    providers: Mapping[str, Mapping[str, Any]],
    sizing: Mapping[str, Mapping[str, Any]],
    efforts: Mapping[str, Mapping[str, Any]],
    cost_to_green: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": now_iso,
        "since_finished_at": since_finished_at,
        "runs_analyzed_total": runs_analyzed_total,
        "providers": dict(providers),
        "concurrency_by_plan_size": dict(sizing),
        # Win rate by router-decided effort (048) — the observed-data input a
        # future learned adjustment behind SwarmRouter._decide_effort reads.
        "win_rate_by_effort": dict(efforts),
        # Cost-to-green by starting effort, with confidence. Unknown rates are
        # JSON null (never 0.0). See swarm.costgreen.
        "cost_to_green_by_effort": list(cost_to_green or ()),
    }


# ---------------------------------------------------------------------------
# best-effort knowledge ingest (mirrors swarm.summary._safe_knowledge_ingest)
# ---------------------------------------------------------------------------


def _safe_knowledge_ingest(
    *,
    runs_analyzed: int,
    providers: Mapping[str, Mapping[str, Any]],
    bottlenecks: Mapping[str, Any],
    ingest_fn: KnowledgeIngestFn | None,
) -> bool:
    provider_bits = (
        ", ".join(
            f"{provider} {_fmt_pct(stats['success_rate'])} ({stats['attempts']} attempts)"
            for provider, stats in sorted(providers.items())
        )
        or "no attempts recorded"
    )
    content = (
        f"Swarm optimizer pass analyzed {runs_analyzed} run(s): {provider_bits}. "
        f"Rate-limit stalls: {bottlenecks.get('stall_count', 0)}, "
        f"task splits: {bottlenecks.get('split_count', 0)}, "
        f"provider switches: {bottlenecks.get('switch_count', 0)}. "
        "See vault/swarm/playbook.md for the full recommendations."
    )
    if ingest_fn is not None:
        try:
            ingest_fn("swarm-optimizer", content)
            return True
        except Exception:  # noqa: BLE001 -- best-effort, even for an injected fake.
            LOG.debug("swarm optimizer: knowledge ingest (injected) failed", exc_info=True)
            return False

    # Merge-order-safe: an un-migrated/absent knowledge package must never
    # fail an optimizer pass (mirrors knowledge.runner_hook / swarm.summary).
    try:
        from omniagentos.knowledge import ingest as knowledge_ingest_module
        from omniagentos.knowledge.contracts import EpisodeSource
        from omniagentos.knowledge.recall import _get_store
    except Exception:  # noqa: BLE001
        return False
    try:
        knowledge_ingest_module.ingest_episode(
            _get_store(),
            source=EpisodeSource.CURATOR.value,
            content=content,
            source_ref="swarm-optimizer",
            discipline="swarm",
        )
        return True
    except Exception:  # noqa: BLE001
        LOG.debug("swarm optimizer: knowledge ingest failed", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# result + entrypoint
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OptimizeResult:
    runs_analyzed: int = 0
    watermark_advanced: bool = False
    playbook_written: bool = False
    learned_written: bool = False
    narrative_included: bool = False
    knowledge_ingested: bool = False
    state_path: str | None = None
    learned_path: str | None = None
    playbook_path: str | None = None
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs_analyzed": self.runs_analyzed,
            "watermark_advanced": self.watermark_advanced,
            "playbook_written": self.playbook_written,
            "learned_written": self.learned_written,
            "narrative_included": self.narrative_included,
            "knowledge_ingested": self.knowledge_ingested,
            "state_path": self.state_path,
            "learned_path": self.learned_path,
            "playbook_path": self.playbook_path,
            "errors": self.errors,
        }


def run_optimize(
    *,
    dal: SwarmDal | None = None,
    db_path: str | None = None,
    state_path: str | None = None,
    learned_path: str | None = None,
    playbook_path: str | None = None,
    vault_dir: str | None = None,
    fable_runner: FableRunner | None = None,
    knowledge_ingest: KnowledgeIngestFn | None = None,
    trace_path: str | None = None,
    batch_limit: int = _DEFAULT_BATCH_LIMIT,
    history_limit: int = _DEFAULT_HISTORY_LIMIT,
    now: Callable[[], datetime] | None = None,
) -> OptimizeResult:
    """Run one optimizer pass. Never raises for optional steps (narrative,
    knowledge ingest, individual file writes are all best-effort and recorded
    in ``result.errors``); a genuine DB-read failure propagates, since this is
    a standalone scheduled job, not anyone's critical path — a broken pass
    should be loud in its own launchd log, not silently swallowed.
    """
    clock = now or (lambda: datetime.now(UTC))
    resolved_db_path = db_path or default_db_path()
    owns_dal = dal is None
    dal = dal or SwarmDal(resolved_db_path)

    resolved_state_path = Path(state_path or default_state_path())
    resolved_learned_path = Path(learned_path or default_learned_path())
    resolved_playbook_path = Path(playbook_path or default_playbook_path(vault_dir))

    result = OptimizeResult(
        state_path=str(resolved_state_path),
        learned_path=str(resolved_learned_path),
        playbook_path=str(resolved_playbook_path),
    )

    try:
        state = read_state(resolved_state_path)
        since_finished_at = state.get("since_finished_at")
        since_rowid = int(state.get("since_rowid") or 0)

        new_runs = dal.finished_runs_since(since_finished_at, since_rowid)
        if batch_limit > 0:
            new_runs = new_runs[:batch_limit]
        result.runs_analyzed = len(new_runs)

        now_dt = clock()
        now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        # --- learned.json: always a full, bounded, recomputed snapshot ---
        all_runs = dal.finished_runs_since(None, 0)
        if history_limit > 0:
            all_runs = all_runs[-history_limit:]
        providers_snapshot = _aggregate_providers(dal, all_runs)
        sizing_snapshot = _aggregate_sizing(all_runs)
        efforts_snapshot = _aggregate_efforts(dal, all_runs)
        cost_to_green_snapshot = _aggregate_cost_to_green(dal, all_runs)
        learned_payload = _build_learned(
            now_iso=now_iso,
            since_finished_at=since_finished_at,
            runs_analyzed_total=len(all_runs),
            providers=providers_snapshot,
            sizing=sizing_snapshot,
            efforts=efforts_snapshot,
            cost_to_green=cost_to_green_snapshot,
        )
        try:
            _atomic_write(
                resolved_learned_path,
                json.dumps(learned_payload, indent=2, sort_keys=True) + "\n",
            )
            result.learned_written = True
        except OSError:
            LOG.warning("swarm optimizer: failed writing learned.json", exc_info=True)
            result.errors["learned"] = "write failed"

        if not new_runs:
            return result  # naturally idempotent: nothing new, watermark untouched

        providers_delta = _aggregate_providers(dal, new_runs)
        bottlenecks_delta = _aggregate_events(dal, new_runs)
        sizing_delta = _aggregate_sizing(new_runs)
        efforts_delta = _aggregate_efforts(dal, new_runs)
        cost_to_green_delta = _aggregate_cost_to_green(dal, new_runs)
        tier_win_rates = _aggregate_tiers(resolved_db_path)
        cascade_trace_tiers = _read_swarm_cascade_traces(trace_path)
        narrative_excerpts = _collect_narrative_excerpts(new_runs)

        fable_narrative = _narrative_section(
            runs_analyzed=len(new_runs),
            providers=providers_delta,
            bottlenecks=bottlenecks_delta,
            sizing=sizing_delta,
            tier_win_rates=tier_win_rates,
            effort_win_rates=efforts_delta,
            fable_runner=fable_runner,
        )
        result.narrative_included = fable_narrative is not None

        window_start = str(new_runs[0].get("finished_at") or "") or None
        window_end = str(new_runs[-1].get("finished_at") or "") or None
        section_md = _render_playbook_section(
            now=now_dt,
            runs_analyzed=len(new_runs),
            window_start=window_start,
            window_end=window_end,
            providers=providers_delta,
            bottlenecks=bottlenecks_delta,
            sizing=sizing_delta,
            tier_win_rates=tier_win_rates,
            effort_win_rates=efforts_delta,
            cascade_trace_tiers=cascade_trace_tiers,
            narrative_excerpts=narrative_excerpts,
            fable_narrative=fable_narrative,
            cost_to_green=cost_to_green_delta,
        )
        try:
            append_playbook_section(resolved_playbook_path, section_md, now_iso=now_iso)
            result.playbook_written = True
        except OSError:
            LOG.warning("swarm optimizer: failed writing playbook", exc_info=True)
            result.errors["playbook"] = "write failed"

        result.knowledge_ingested = _safe_knowledge_ingest(
            runs_analyzed=len(new_runs),
            providers=providers_delta,
            bottlenecks=bottlenecks_delta,
            ingest_fn=knowledge_ingest,
        )

        last_run = new_runs[-1]
        new_state = {
            "since_finished_at": str(last_run.get("finished_at")),
            "since_rowid": int(last_run.get("_rowid") or 0),
            "updated_at": now_iso,
            "runs_processed_total": int(state.get("runs_processed_total") or 0) + len(new_runs),
        }
        write_state(resolved_state_path, new_state)
        result.watermark_advanced = True
        return result
    finally:
        if owns_dal:
            dal.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omniagentos.swarm.optimize",
        description=(
            "Twice-daily swarm optimizer: mines finished swarm runs since the last "
            "watermark for recurring bottlenecks, per-provider success rates/"
            "latencies, sizing observations, and routing hints. Appends a dated "
            "section to vault/swarm/playbook.md (human-applied only), best-effort "
            "ingests one knowledge fact, and refreshes var/swarm/learned.json "
            "(observed data only -- never auto-edits config)."
        ),
    )
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--state-path", default=None)
    parser.add_argument("--learned-path", default=None)
    parser.add_argument("--playbook-path", default=None)
    parser.add_argument("--vault-dir", default=None)
    parser.add_argument("--batch-limit", type=int, default=_DEFAULT_BATCH_LIMIT)
    parser.add_argument("--history-limit", type=int, default=_DEFAULT_HISTORY_LIMIT)
    args = parser.parse_args(argv)

    result = run_optimize(
        db_path=args.db_path,
        state_path=args.state_path,
        learned_path=args.learned_path,
        playbook_path=args.playbook_path,
        vault_dir=args.vault_dir,
        batch_limit=args.batch_limit,
        history_limit=args.history_limit,
    )
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "OptimizeResult",
    "append_playbook_section",
    "default_learned_path",
    "default_playbook_path",
    "default_state_path",
    "main",
    "read_state",
    "run_optimize",
    "write_state",
]
