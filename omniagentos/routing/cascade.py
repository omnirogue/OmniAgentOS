"""Verification-gated router cascade (FrugalGPT arXiv:2305.05176 + RouteLLM
arXiv:2406.18665).

WHY: expensive-first routing burns budget on tasks a cheap tier could have
solved; cheap-first-with-no-verification silently ships cheap-tier failures
to the user. This module is the missing middle: run the CHEAP tier first,
let an OBJECTIVE VERIFIER (the caller's test/build harness -- never another
LLM's opinion) decide pass/fail, and escalate to the next, more expensive
tier ONLY on a VERIFIED failure -- carrying a one-paragraph Reflexion-style
reflection (arXiv:2303.11366, see ``omniagentos.selfimprove.reflexion``)
about why the cheap attempt failed, so the next tier isn't repeating the
same mistake blind.

Every attempt (win or loss) is appended to a JSONL trace file;
``omniagentos.routing.learn`` mines that trace to recommend which tier
FUTURE tasks of the same class should START at, closing the FrugalGPT loop
without any of this needing an LLM judge -- the verifier is deterministic
and the learner is pure arithmetic over recorded outcomes.

This module never resolves adapters or builds an ``AgentInput`` itself --
``work()`` is supplied by the composing caller (a later package wires it to
``omniagentos.adapters.registry.resolve_adapter`` + a real ``AgentInput``).
That keeps this module a pure orchestration/contract layer, testable with a
stub ``work()`` and zero network/CLI calls (see
``tests/routing/test_cascade.py``).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from omniagentos.routing.learn import read_traces, recommend_start_tier

logger = logging.getLogger(__name__)


def _repo_root() -> str:
    # omniagentos/routing/cascade.py -> omniagentos/routing -> omniagentos -> repo root
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_trace_path() -> str:
    """``OMNIAGENTOS_CASCADE_TRACE`` env override, else
    ``<repo_root>/var/routing/cascade/traces.jsonl`` (gitignored runtime
    scratch, same convention as the rest of ``var/``). Library code
    (``run_cascade``) always takes the explicit ``trace_path`` parameter --
    this default is resolved only by callers/config that want the
    repo-standard location, per the package brief.
    """
    override = os.environ.get("OMNIAGENTOS_CASCADE_TRACE")
    if override:
        return override
    return os.path.join(_repo_root(), "var", "routing", "cascade", "traces.jsonl")


def default_cascade_config_path() -> str:
    """``OMNIAGENTOS_CASCADE_CONFIG`` env override, else
    ``<repo_root>/configs/cascade.yaml`` (cwd-independent, same convention as
    ``omniagentos.contracts.default_policy_path``)."""
    override = os.environ.get("OMNIAGENTOS_CASCADE_CONFIG")
    if override:
        return override
    return os.path.join(_repo_root(), "configs", "cascade.yaml")


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CascadeTier:
    """One rung of the cascade ladder."""

    name: str
    adapter: str
    model: str | None = None
    effort: str | None = None
    cost_rank: float = 0.0


@dataclass(frozen=True)
class AttemptOutcome:
    """What a ``work()`` call hands back for ``verify()`` to judge."""

    output: object
    evidence: str


@dataclass(frozen=True)
class CascadeAttempt:
    """One tier's attempt within a single ``run_cascade`` call.

    ``reflection`` is the reflection this attempt's ``work()`` call was
    GIVEN (carried forward from the previous tier's failure) -- ``None`` for
    the first attempt in the run. ``error`` is set (and ``verified`` is
    always ``False``) when ``work()`` or ``verify()`` raised instead of
    returning a normal loss.
    """

    tier: CascadeTier
    verified: bool
    verify_detail: str
    reflection: str | None
    seconds: float
    error: str | None


@dataclass(frozen=True)
class CascadeResult:
    """Outcome of one ``run_cascade`` call across however much of the ladder
    it walked."""

    attempts: list[CascadeAttempt]
    final_output: object | None
    succeeded: bool
    task_class: str
    total_seconds: float


# ---------------------------------------------------------------------------
# Trace recording
# ---------------------------------------------------------------------------


def cascade_enabled() -> bool:
    """Whether the verification-gated tier escalation is wired into the product
    paths (currently the Orchestrator's ``_execute_task`` retry loop).

    OFF by default (``OMNIAGENTOS_CASCADE`` unset): with the flag unset every
    composing caller keeps its pre-cascade behavior byte-for-byte. Set
    ``OMNIAGENTOS_CASCADE=1`` to escalate on a verified failure and record a
    trace per attempt."""
    return os.environ.get("OMNIAGENTOS_CASCADE") == "1"


_DEFAULT_TRACE_MAX_BYTES = 5 * 1024 * 1024


def _rotate_if_needed(trace_path: str, max_bytes: int) -> None:
    """Size-based rotation: when ``trace_path`` exceeds ``max_bytes``, rename it to
    ``<trace_path>.1`` (overwriting any prior rotation -- exactly one is kept) and let
    the caller start a fresh file. Keeps the trace log from growing unbounded while
    :mod:`omniagentos.routing.learn` still sees the most recent window. ``max_bytes <=
    0`` disables rotation."""
    if max_bytes <= 0:
        return
    try:
        size = os.path.getsize(trace_path)
    except OSError:
        return
    if size > max_bytes:
        os.replace(trace_path, trace_path + ".1")


def record_trace(
    trace_path: str,
    *,
    task_class: str,
    tier_name: str,
    adapter: str | None,
    model: str | None,
    verified: bool,
    seconds: float,
    error: bool,
    escalated_from: str | None,
    max_bytes: int = _DEFAULT_TRACE_MAX_BYTES,
) -> None:
    """Append one JSONL trace row -- the SINGLE write path for cascade traces.

    Both the in-module ``run_cascade`` loop (via :func:`_append_trace`) and
    external composing callers (the Orchestrator's per-attempt win/loss trace)
    funnel through here, so the on-disk schema stays identical and
    :mod:`omniagentos.routing.learn` can mine either source uniformly. The trace is
    rotated (one ``.1`` backup kept) once it passes ``max_bytes`` so it never grows
    unbounded. Write failures are logged and swallowed -- a full disk or unwritable
    path must never break the caller (design invariant from the package brief)."""
    row = {
        "ts": time.time(),
        "task_class": task_class,
        "tier_name": tier_name,
        "adapter": adapter,
        "model": model,
        "verified": verified,
        "seconds": seconds,
        "error": error,
        "escalated_from": escalated_from,
    }
    try:
        parent = os.path.dirname(trace_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _rotate_if_needed(trace_path, max_bytes)
        with open(trace_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    except OSError as exc:
        logger.warning("cascade: failed to append trace to %s: %s", trace_path, exc)


def _append_trace(
    trace_path: str,
    *,
    task_class: str,
    tier: CascadeTier,
    verified: bool,
    seconds: float,
    error: bool,
    escalated_from: str | None,
) -> None:
    """Append one JSONL trace row for an in-cascade attempt (delegates to the
    shared :func:`record_trace` writer)."""
    record_trace(
        trace_path,
        task_class=task_class,
        tier_name=tier.name,
        adapter=tier.adapter,
        model=tier.model,
        verified=verified,
        seconds=seconds,
        error=error,
        escalated_from=escalated_from,
    )


# ---------------------------------------------------------------------------
# Cascade runner
# ---------------------------------------------------------------------------


def run_cascade(
    work: Callable[[CascadeTier, str | None], AttemptOutcome],
    verify: Callable[[AttemptOutcome], tuple[bool, str]],
    ladder: Sequence[CascadeTier],
    *,
    task_class: str,
    reflect: Callable[[str, str], str] | None = None,
    trace_path: str | None = None,
    start_tier: int | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CascadeResult:
    """Walk ``ladder`` from the cheapest viable tier, verifying each attempt
    with an objective ``verify()`` and escalating only on a verified
    failure. Never raises: a raising ``work()``/``verify()`` is recorded as
    an error/loss and the cascade continues to the next tier; a raising
    ``reflect()`` degrades to no reflection; a trace-write failure is logged
    and swallowed.

    ``start_tier``, when ``None``, defaults to
    ``learn.recommend_start_tier()`` over the trace history at
    ``trace_path`` for ``task_class`` (falling back to tier 0 when there is
    no history yet, or if resolving the recommendation itself raises).
    """
    resolved_trace_path = trace_path if trace_path is not None else default_trace_path()
    ladder = list(ladder)
    total_start = clock()

    if not ladder:
        return CascadeResult(
            attempts=[],
            final_output=None,
            succeeded=False,
            task_class=task_class,
            total_seconds=clock() - total_start,
        )

    if start_tier is None:
        start_tier = 0
        try:
            traces = read_traces(resolved_trace_path, task_class)
            if traces:
                start_tier = recommend_start_tier(traces, ladder)
        except Exception as exc:  # noqa: BLE001 -- learning a start tier is best-effort
            logger.warning("cascade: recommend_start_tier failed, starting at tier 0: %s", exc)
            start_tier = 0

    start_tier = max(0, min(start_tier, len(ladder) - 1))

    attempts: list[CascadeAttempt] = []
    reflection: str | None = None
    escalated_from: str | None = None

    for index in range(start_tier, len(ladder)):
        tier = ladder[index]
        attempt_start = clock()
        error: str | None = None
        verified = False
        verify_detail = ""
        outcome: AttemptOutcome | None = None

        try:
            outcome = work(tier, reflection)
        except Exception as exc:  # noqa: BLE001 -- work() is caller-supplied and untrusted
            error = str(exc)

        if outcome is not None and error is None:
            try:
                verified, verify_detail = verify(outcome)
            except Exception as exc:  # noqa: BLE001 -- verify() is caller-supplied and untrusted
                error = f"verify raised: {exc}"

        seconds = clock() - attempt_start

        attempts.append(
            CascadeAttempt(
                tier=tier,
                verified=verified,
                verify_detail=verify_detail,
                reflection=reflection,
                seconds=seconds,
                error=error,
            )
        )
        _append_trace(
            resolved_trace_path,
            task_class=task_class,
            tier=tier,
            verified=verified,
            seconds=seconds,
            error=error is not None,
            escalated_from=escalated_from,
        )

        if verified:
            return CascadeResult(
                attempts=attempts,
                final_output=outcome.output if outcome is not None else None,
                succeeded=True,
                task_class=task_class,
                total_seconds=clock() - total_start,
            )

        next_reflection: str | None = None
        if reflect is not None and outcome is not None and error is None:
            try:
                next_reflection = reflect(verify_detail, outcome.evidence)
            except Exception as exc:  # noqa: BLE001 -- reflection is best-effort
                logger.warning("cascade: reflect() raised for tier %s: %s", tier.name, exc)
                next_reflection = None

        reflection = next_reflection
        escalated_from = tier.name

    return CascadeResult(
        attempts=attempts,
        final_output=None,
        succeeded=False,
        task_class=task_class,
        total_seconds=clock() - total_start,
    )


# ---------------------------------------------------------------------------
# Ladder config loading
# ---------------------------------------------------------------------------


def load_ladder(path: str | None = None) -> list[CascadeTier]:
    """Load the cascade ladder from ``configs/cascade.yaml`` (or ``path``).

    ``pyyaml`` is already a repo dependency (see ``omniagentos.routing.config``
    / ``omniagentos.routing.account_pool``, which load ``configs/accounts.yaml``
    the same way), so this reads the YAML directly rather than hand-parsing.
    """
    resolved = path or default_cascade_config_path()
    raw: Any = yaml.safe_load(Path(resolved).read_text(encoding="utf-8")) or {}
    entries = raw.get("ladder") or []
    ladder: list[CascadeTier] = []
    for entry in entries:
        ladder.append(
            CascadeTier(
                name=str(entry["name"]),
                adapter=str(entry["adapter"]),
                model=entry.get("model"),
                effort=entry.get("effort"),
                cost_rank=float(entry.get("cost_rank", 0.0)),
            )
        )
    return ladder


__all__ = [
    "AttemptOutcome",
    "CascadeAttempt",
    "CascadeResult",
    "CascadeTier",
    "cascade_enabled",
    "default_cascade_config_path",
    "default_trace_path",
    "load_ladder",
    "record_trace",
    "run_cascade",
]
