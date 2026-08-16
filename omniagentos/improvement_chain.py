"""Ordered review chain for suggestions emitted by improvement loops.

The chain is intentionally asymmetric:

* Kimi is the volume analyst and produces a structured draft.
* Opus 5 at X High receives a confined writable staging directory and MUST
  directly edit the plan file there.
* Fable receives the resulting plan read-only and records the final review.

Only the configured plan target is copied out of the staging directory. This
gives Opus real editing ability without granting an unattended reviewer write
access to the rest of the repository.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omniagentos.adapters.registry import resolve_adapter
from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    AgentUsage,
    BudgetSpec,
    ResultStatus,
    new_id,
)
from omniagentos.longhaul.limits import classify_limit_text
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.runner import sandbox
from omniagentos.runtime_paths import resolve_var_root
from omniagentos.steward.notify import send_slack

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "configs" / "loop_models.yaml"
DEFAULT_ARTIFACT_ROOT = ROOT / "var" / "loop-review"
FABLE_REVIEW_START = "<!-- FABLE FINAL REVIEW START -->"
FABLE_REVIEW_END = "<!-- FABLE FINAL REVIEW END -->"

# Terminal-error classification (X-B1 / plan-2 B3).
#
# ``classify_limit_text`` (omniagentos.longhaul.limits) is the ONE shared
# definition this module reuses for the four RECOGNISED outcome names
# (auth_error/quota_exhausted/transient_rate_limit/overloaded). It is the
# same function ``provider_exec``'s doctor-outcome classifier (lane/
# provider-truth) delegates recognised text to, so a shared-table match can
# never come out contradictory between the two -- that classifier's full
# vocabulary is wider (it also returns ``ok``/``harness_error``/
# ``unavailable``, none of which this module needs: an ``ok`` never reaches
# here, and any other unrecognised text is just ``None`` on both sides,
# never treated as favourable). Importing it here (rather than re-deriving
# the phrase tables) is what keeps recognised outcomes compatible.
#
# Of the four classes, auth_error and quota_exhausted are TERMINAL: retrying
# through account rotation cannot fix an expired credential or a suspended/
# out-of-balance org (the Moonshot suspension is the live example). A
# terminal failure PARKS -- it stops being retried this tick and is recorded
# so operators/dashboards can query why -- and pages exactly once per
# component/signature/day, never once per tick. transient_rate_limit and
# overloaded remain retryable; an unclassified (``None``) error is NEVER
# treated as success or as terminal -- it just fails the stage as before.
_TERMINAL_OUTCOMES = frozenset({"auth_error", "quota_exhausted"})

_HARNESS_PROVIDER = {
    "cli-kimi": "kimi",
    "cli-claude": "claude",
    "cli-codex": "codex",
    "cli-grok": "grok",
    "cli-gemini": "gemini",
}


def _provider_for_harness(harness: str) -> str:
    return _HARNESS_PROVIDER.get(harness, harness)


# Vendor tokens that identify a LINEAGE wherever they appear -- in a harness key
# or in a model id. The harness table above only covers the five ``cli-*`` keys,
# so ``api-kimi-k3`` (a real registry key, omniagentos/adapters/registry.py)
# resolved to itself and compared UNEQUAL to ``kimi``: a fallback pinned to it
# would have passed a same-lineage check while being the same vendor, which is
# the same outage twice and defeats the operator constraint outright.
_LINEAGE_TOKENS: tuple[tuple[str, str], ...] = (
    ("kimi", "kimi"),
    ("moonshot", "kimi"),
    ("claude", "claude"),
    ("anthropic", "claude"),
    ("opus", "claude"),
    ("sonnet", "claude"),
    ("haiku", "claude"),
    ("fable", "claude"),
    ("codex", "codex"),
    ("gpt", "codex"),
    ("openai", "codex"),
    ("gemini", "gemini"),
    ("google", "gemini"),
    ("grok", "grok"),
    ("xai", "grok"),
    ("qwen", "qwen"),
    ("o1", "codex"),
    ("o3", "codex"),
    ("o4", "codex"),
)

# Tokens are matched on PUNCTUATION BOUNDARIES, never as bare substrings: a
# substring test reads ``community/octopus-v2`` as Claude (it contains "opus")
# and would refuse a perfectly valid cross-lineage fallback. Splitting on
# non-alphanumerics keeps ``moonshot-ai/kimi-k3`` -> {moonshot, ai, kimi, k3}
# and ``claude-opus-5`` -> {claude, opus, 5} matching, while ``octopus`` stays
# its own token and matches nothing.
_LINEAGE_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _lineage_for_stage(stage: ModelStage) -> str:
    """Best-effort vendor lineage of *stage*, from its harness AND its model.

    Deliberately checks BOTH: a harness key alone misses ``api-*`` adapters
    (``api-kimi-k3`` is a real registry key) and any future alias, while a model
    id alone misses a bare alias like ``fable``. Unknown text falls back to the
    harness-provider string, which is never treated as matching anything else.
    """
    tokens = {
        token
        for token in _LINEAGE_TOKEN_SPLIT.split(f"{stage.harness} {stage.model}".lower())
        if token
    }
    for token, lineage in _LINEAGE_TOKENS:
        if token in tokens:
            return lineage
    return _provider_for_harness(stage.harness)


# ``classify_limit_text``'s provider tables cover gemini/kimi/grok (the
# longhaul CLI providers); "claude" is not one of them because CLAUDE ACCOUNT
# ROTATION is an improvement_chain concept, not a longhaul one. These are
# small, ADDITIVE-ONLY local extensions for phrasings this chain's own
# reviewer found live-plausible (an expired OAuth session, a monthly spend
# cap, an outright suspended org) that the shared table has no reason to
# carry. They never override or contradict a shared-table match -- they are
# consulted only when ``classify_limit_text`` itself returns ``None`` -- so
# they cannot desync the vocabulary; they only widen recognition within it.
_CLAUDE_EXTRA_TERMINAL_PATTERNS: dict[str, tuple[str, ...]] = {
    "auth_error": (
        "oauth session expired",
        "failed to authenticate",
        "organization suspended",
        "org suspended",
    ),
    "quota_exhausted": (
        "monthly spend limit",
        "spend limit",
    ),
}


def classify_chain_error(provider: str, error_text: str) -> str | None:
    """Classify a stage failure using the shared limit-text vocabulary.

    Returns ``'auth_error' | 'quota_exhausted' | 'transient_rate_limit' |
    'overloaded'``, or ``None`` when the text matches no known failure shape.
    ``None`` is not a favourable result: callers must keep treating the stage
    as failed, it simply carries no park/retry verdict.
    """
    outcome = classify_limit_text(provider, error_text)
    if outcome is not None:
        return outcome
    if provider == "claude":
        lowered = error_text.lower()
        for outcome_name, patterns in _CLAUDE_EXTRA_TERMINAL_PATTERNS.items():
            if any(pattern in lowered for pattern in patterns):
                return outcome_name
    return None


def is_terminal_outcome(outcome: str | None) -> bool:
    """Return whether a classified outcome is TERMINAL (park, never rotate)."""
    return outcome in _TERMINAL_OUTCOMES


def _loop_review_root() -> Path:
    # Package anchor (env_keys=(), environ={}): this module's sibling artifact
    # tree (DEFAULT_ARTIFACT_ROOT) is anchored here, and following
    # OMNIAGENTOS_VAR (runtime under the launcher) would strand existing
    # parked/alerted state — a previously-parked component would silently read
    # as unparked (review B1, 2026-08-14). Tests monkeypatch THIS function.
    return resolve_var_root(env_keys=(), environ={}, leaf=("loop-review",))


def _parked_state_path() -> Path:
    return _loop_review_root() / "parked.json"


def _parked_lock_path() -> Path:
    return _loop_review_root() / ".parked.lock"


def _with_parked_lock(mutate: Callable[[], Any]) -> Any:
    """Serialize every read-modify-write of ``parked.json`` (park AND clear)
    behind one stable, cross-process ``flock`` -- same idiom as the alert
    lock: blocking ``LOCK_EX``, released but never unlinked, so every caller
    (this process, another tick's fresh process, a concurrent writer for a
    DIFFERENT component) keeps contending for the same file instead of
    racing an unlocked read/modify/``os.replace`` and losing an update.
    """
    lock_path = _parked_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            return mutate()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _alert_state_path() -> Path:
    return _loop_review_root() / "alerted.json"


def _read_json_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_state(path: Path, data: Mapping[str, Any]) -> None:
    _atomic_write(path, json.dumps(dict(data), indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def _dedup_key(component: str, signature: str, day: str) -> str:
    return f"{component}:{signature}:{day}"


def _alert_lock_path() -> Path:
    return _loop_review_root() / ".alert.lock"


def _alert_once(
    *,
    component: str,
    signature: str,
    day: str,
    message: str,
    sender: Callable[[str], Any] | None = None,
) -> bool:
    """Send exactly one alert per ``component:signature:day``.

    Returns ``True`` iff this call actually delivered the alert AND recorded
    the dedup key. Two invariants, both load-bearing:

    * DELIVERY-GATED PERSISTENCE (``omniagentos.scheduler.loop_jobs``'s
      paging precedent): the dedup key is written ONLY when the sender
      reports ``ok`` (default ``False`` for an unrecognised/legacy return
      value -- "unknown is never favourable" applies to delivery too). A
      failed send (missing webhook, timeout, HTTP error) leaves the key
      unwritten so the NEXT tick retries; recording an undelivered page
      would silently drop it forever.
    * LOCK-PROTECTED read/check/send/write: an independent caller (the swarm
      optimizer/summary loops both bind ``run_kimi_json`` as their default
      runner and can run concurrently with each other and with
      ``fable-curator.sh``) must never observe an absent key, send its own
      duplicate alert, and race the write. The whole sequence holds a
      blocking ``flock`` on a stable lockfile; the lock is released, never
      unlinked, so every caller keeps contending for the same file.
    """
    key = _dedup_key(component, signature, day)
    lock_path = _alert_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = _read_json_state(_alert_state_path())
            if state.get(key):
                return False
            notifier = sender or (
                lambda text: send_slack(text, webhook_env="OPS_ALERT_SLACK_WEBHOOK_URL")
            )
            result = notifier(message)
            delivered = bool(getattr(result, "ok", False))
            if not delivered:
                return False
            state[key] = True
            _write_json_state(_alert_state_path(), state)
            return True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def park_chain_failure(
    *,
    component: str,
    outcome: str,
    detail: str,
    now: Callable[[], datetime] | None = None,
    sender: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Persist a park record for a TERMINAL failure and alert-once.

    The classified ``outcome`` doubles as the stable per-component
    "signature" in the ``component:signature:day`` dedup idiom: it is a
    small, fixed vocabulary (unlike raw error text, which varies run to run
    and would defeat dedup).
    """
    clock = now or (lambda: datetime.now(UTC))
    ts = clock()
    day = ts.strftime("%Y-%m-%d")
    record = {
        "component": component,
        "outcome": outcome,
        "detail": detail[-2000:],
        "parked_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dedup_key": _dedup_key(component, outcome, day),
    }

    def _mutate() -> None:
        state = _read_json_state(_parked_state_path())
        state[component] = record
        _write_json_state(_parked_state_path(), state)

    _with_parked_lock(_mutate)
    _alert_once(
        component=component,
        signature=outcome,
        day=day,
        message=(
            f"OmniAgentOS improvement_chain PARKED ({component}): {outcome} -- {detail[:500]}"
        ),
        sender=sender,
    )
    return record


def get_parked_reason(component: str) -> dict[str, Any] | None:
    """Query the persisted, honest reason a component is parked, if any."""
    record = _read_json_state(_parked_state_path()).get(component)
    return record if isinstance(record, dict) else None


def clear_parked_reason(component: str) -> None:
    """Operator/recovery hook: clear a park record once the cause is resolved."""

    def _mutate() -> None:
        state = _read_json_state(_parked_state_path())
        if component in state:
            del state[component]
            _write_json_state(_parked_state_path(), state)

    _with_parked_lock(_mutate)


def _maybe_recovery_candidate(stage_component: str, account_components: list[str]) -> bool:
    """Return whether a RECOVERY ATTEMPT should proceed this call: the
    stage is parked AND at least one configured account is NOT parked.

    ROUND-6 DESIGN (retain, never delete-then-maybe-recreate): this is
    READ-ONLY -- it never mutates ``parked.json``. The previous design
    (round 5) deleted the stage record here and only recreated it once the
    per-account loop finished, which left a real window (a concurrent
    config-error call, or a crash with no ``finally``) where every account
    was parked but the stage record was momentarily absent. Retaining the
    stage record through the WHOLE recovery attempt makes the invariant
    hold BY CONSTRUCTION at every instant: "every configured account is
    parked" implies "the stage record exists", because it is never removed
    except at the single confirmed-recovery site
    (:func:`_clear_stage_park_on_recovery`), which only fires once a
    specific account is known to be NOT parked -- so there is no window
    where the invariant's antecedent is true and its consequent is false.
    """
    if get_parked_reason(stage_component) is None:
        return False
    return any(get_parked_reason(component) is None for component in account_components)


def _clear_stage_park_on_recovery(stage_component: str, account_components: list[str]) -> None:
    """Clear the stage-level park once recovery is CONFIRMED, but ONLY after
    an atomic re-verify that at least one account is still unparked.

    Called right after a specific account returns a successful or
    definitively non-terminal result (never after a terminal re-park, which
    leaves the account -- and possibly the whole stage -- still parked).

    ROUND-7: the previous "safe to call unconditionally" claim was the bug.
    Two overlapping recovery callers can both pass the pre-call candidate
    and per-account checks, enter ``run_once``, then diverge: caller A
    returns terminal and parks the final unparked account, while caller B
    returns OK / definitive non-terminal and would clear the stage. A bare
    unconditional delete (``clear_parked_reason(stage_component)``) has no
    current-state check, so B can wipe the stage key after A has parked
    every account -- recreating the exact "all accounts parked, no stage
    record" gap this delta was meant to close.

    The fix is re-verify-then-delete inside ONE ``_with_parked_lock`` hold
    over a SINGLE ``parked.json`` snapshot: read state once, check that at
    least one of ``account_components`` is absent from that snapshot, and
    only then delete ``stage_component`` and write back. If every account is
    parked in that same snapshot, leave the stage key alone -- the
    invariant "every account parked ⇒ stage record present" still needs it.
    Concurrent terminal parks also take ``_with_parked_lock`` (via
    ``park_chain_failure``), so check and delete serialize with those
    writers: there is no window between "account still unparked" and
    "delete stage" that a peer can slip a final account-park into.
    """

    def _mutate() -> None:
        state = _read_json_state(_parked_state_path())
        # Same snapshot for the unparked check AND the conditional delete:
        # do not re-read via get_parked_reason / clear_parked_reason.
        if not any(component not in state for component in account_components):
            return
        if stage_component in state:
            del state[stage_component]
            _write_json_state(_parked_state_path(), state)

    _with_parked_lock(_mutate)


def _retry_parked_alert(
    component: str,
    record: Mapping[str, Any],
    *,
    now: Callable[[], datetime] | None = None,
    sender: Callable[[str], Any] | None = None,
) -> bool:
    """Every parked fast path calls this BEFORE short-circuiting.

    If the original park alert never delivered (the webhook was down, timed
    out, or errored), this retries it from the persisted ``outcome``/
    ``detail`` -- it is exactly :func:`_alert_once`, just re-invoked from the
    stored record instead of from a fresh failure. Once a send SUCCEEDS the
    once-per-``component:signature:day`` dedup still applies (the key gets
    recorded then, same as any other alert), so a working webhook stops the
    retries after exactly one delivered page; a still-broken webhook keeps
    retrying every parked call, same as a fresh terminal failure would.
    """
    clock = now or (lambda: datetime.now(UTC))
    day = clock().strftime("%Y-%m-%d")
    outcome = str(record.get("outcome") or "")
    detail = str(record.get("detail") or "")
    return _alert_once(
        component=component,
        signature=outcome,
        day=day,
        message=(
            f"OmniAgentOS improvement_chain PARKED ({component}): {outcome} -- {detail[:500]}"
        ),
        sender=sender,
    )


class ChainParked(RuntimeError):
    """Raised when a component is currently parked -- the call is refused
    WITHOUT making the provider call, until :func:`clear_parked_reason` runs.
    """

    def __init__(self, component: str, record: Mapping[str, Any]) -> None:
        super().__init__(
            f"{component} is parked ({record.get('outcome')} since "
            f"{record.get('parked_at')}): {record.get('detail')}"
        )
        self.component = component
        self.record = dict(record)


def _log_parked_skip(component: str, record: Mapping[str, Any]) -> None:
    print(
        f"improvement_chain: {component} is PARKED ({record.get('outcome')} since "
        f"{record.get('parked_at')}) -- refusing the provider call until "
        "clear_parked_reason() is invoked",
        file=sys.stderr,
    )


def _raise_if_parked(component: str) -> None:
    """Enforcement point: consult persisted parked state before any provider
    call this component would otherwise make, and short-circuit it."""
    record = get_parked_reason(component)
    if record is not None:
        _log_parked_skip(component, record)
        _retry_parked_alert(component, record)
        raise ChainParked(component, record)


@dataclass(frozen=True)
class ModelStage:
    harness: str
    model: str
    effort: str | None
    can_edit_plans: bool


@dataclass(frozen=True)
class ChainConfig:
    primary: ModelStage
    # OPTIONAL second-lineage proposer (R0-2 item 4). ``None`` keeps the
    # historical single-stage shape so an older config still loads.
    primary_fallback: ModelStage | None
    plan_editor: ModelStage
    final_reviewer: ModelStage
    plan_target: Path
    plan_max_bytes: int
    improvement_log_lines: int
    playbook_max_chars: int
    existing_plan_max_chars: int
    latest_curator_reports: int
    latest_reflection_reports: int
    latest_backlog_digests: int
    primary_wall_ms: int
    editor_wall_ms: int
    reviewer_wall_ms: int


@dataclass(frozen=True)
class SourceSnapshot:
    name: str
    path: str
    content: str


@dataclass(frozen=True)
class ChainResult:
    run_id: str
    plan_path: str
    artifact_dir: str
    verdict: str
    models: dict[str, dict[str, Any]]
    sources: list[str]


JsonRunner = Callable[
    [ModelStage, str, dict[str, Any], Path, int],
    dict[str, Any],
]
PlanEditor = Callable[
    [ModelStage, str, Path, str, int],
    str,
]


def _stage(raw: Any, name: str) -> ModelStage:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{name} must be a mapping")
    harness = str(raw.get("harness") or "").strip()
    model = str(raw.get("model") or "").strip()
    effort_raw = raw.get("effort")
    effort = str(effort_raw).strip().lower() if effort_raw is not None else None
    if not harness or not model:
        raise ValueError(f"{name} requires harness and model")
    if effort not in {None, "low", "medium", "high", "xhigh", "max"}:
        raise ValueError(f"{name}.effort has unsupported value {effort!r}")
    return ModelStage(
        harness=harness,
        model=model,
        effort=effort,
        can_edit_plans=bool(raw.get("can_edit_plans", False)),
    )


def _positive_int(raw: Any, default: int, name: str) -> int:
    value = default if raw is None else int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def load_config(path: str | Path = DEFAULT_CONFIG_PATH, *, root: Path | None = None) -> ChainConfig:
    """Load and validate the central loop model policy."""
    root = root or ROOT
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ValueError("loop model config must be a mapping")

    primary = _stage(raw.get("primary"), "primary")
    editor = _stage(raw.get("plan_editor"), "plan_editor")
    reviewer = _stage(raw.get("final_reviewer"), "final_reviewer")
    if primary.can_edit_plans:
        raise ValueError("primary Kimi stage must be read-only")

    # A single-vendor dependency in the one loop that improves the system is
    # the wrong shape regardless of any billing pause, so a fallback is
    # supported -- but only a REAL one. A fallback on the same harness is the
    # same outage twice, and a writable one would smuggle plan-edit rights into
    # a read-only proposer seat.
    fallback_raw = raw.get("primary_fallback")
    primary_fallback = None
    if fallback_raw is not None:
        primary_fallback = _stage(fallback_raw, "primary_fallback")
        if primary_fallback.can_edit_plans:
            raise ValueError("primary_fallback must be read-only")
        primary_lineage = _lineage_for_stage(primary)
        if _lineage_for_stage(primary_fallback) == primary_lineage:
            raise ValueError(
                "primary_fallback must be a different lineage from primary "
                f"(both resolve to lineage {primary_lineage!r})"
            )

    if not editor.can_edit_plans:
        raise ValueError("plan_editor must have can_edit_plans=true")
    if reviewer.can_edit_plans:
        raise ValueError("final_reviewer must be read-only")

    plan = raw.get("plan") or {}
    if not isinstance(plan, Mapping):
        raise ValueError("plan must be a mapping")
    target_raw = str(plan.get("target") or "").strip()
    target_rel = Path(target_raw)
    if (
        not target_raw
        or target_rel.is_absolute()
        or ".." in target_rel.parts
        or target_rel.suffix.lower() != ".md"
    ):
        raise ValueError("plan.target must be a repository-relative Markdown path")
    target = (root / target_rel).resolve()
    resolved_root = root.resolve()
    if inode_relative_parts_anchored(target, resolved_root) is None:
        raise ValueError("plan.target resolves outside the repository")

    sources = raw.get("sources") or {}
    budgets = raw.get("budgets") or {}
    if not isinstance(sources, Mapping) or not isinstance(budgets, Mapping):
        raise ValueError("sources and budgets must be mappings")

    return ChainConfig(
        primary=primary,
        primary_fallback=primary_fallback,
        plan_editor=editor,
        final_reviewer=reviewer,
        plan_target=target,
        plan_max_bytes=_positive_int(plan.get("max_bytes"), 262_144, "plan.max_bytes"),
        improvement_log_lines=_positive_int(
            sources.get("improvement_log_lines"), 100, "sources.improvement_log_lines"
        ),
        playbook_max_chars=_positive_int(
            sources.get("playbook_max_chars"), 100_000, "sources.playbook_max_chars"
        ),
        existing_plan_max_chars=_positive_int(
            sources.get("existing_plan_max_chars"),
            100_000,
            "sources.existing_plan_max_chars",
        ),
        latest_curator_reports=_positive_int(
            sources.get("latest_curator_reports"), 3, "sources.latest_curator_reports"
        ),
        latest_reflection_reports=_positive_int(
            sources.get("latest_reflection_reports"), 3, "sources.latest_reflection_reports"
        ),
        latest_backlog_digests=_positive_int(
            sources.get("latest_backlog_digests"), 3, "sources.latest_backlog_digests"
        ),
        primary_wall_ms=_positive_int(
            budgets.get("primary_wall_ms"), 420_000, "budgets.primary_wall_ms"
        ),
        editor_wall_ms=_positive_int(
            budgets.get("editor_wall_ms"), 900_000, "budgets.editor_wall_ms"
        ),
        reviewer_wall_ms=_positive_int(
            budgets.get("reviewer_wall_ms"), 420_000, "budgets.reviewer_wall_ms"
        ),
    )


def _tail_lines(path: Path, count: int) -> str:
    if not path.is_file():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:])


def _tail_chars(path: Path, count: int) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-count:]


def _latest(pattern: str, count: int) -> list[Path]:
    paths = [path for path in ROOT.glob(pattern) if path.is_file()]
    return sorted(paths, key=lambda item: (item.stat().st_mtime_ns, str(item)))[-count:]


def _latest_external(directory: Path, pattern: str, count: int) -> list[Path]:
    if not directory.is_dir():
        return []
    paths = [path for path in directory.glob(pattern) if path.is_file()]
    return sorted(paths, key=lambda item: (item.stat().st_mtime_ns, str(item)))[-count:]


def collect_sources(config: ChainConfig) -> list[SourceSnapshot]:
    """Collect existing loop suggestions and the current actionable plan."""
    snapshots: list[SourceSnapshot] = []

    def add(name: str, path: Path, content: str) -> None:
        if content.strip():
            snapshots.append(SourceSnapshot(name=name, path=str(path), content=content))

    improvement_log = ROOT / "var" / "improvement-log.jsonl"
    add(
        "Improvement log",
        improvement_log,
        _tail_lines(improvement_log, config.improvement_log_lines),
    )

    playbook = ROOT / "vault" / "swarm" / "playbook.md"
    add("Swarm optimizer playbook", playbook, _tail_chars(playbook, config.playbook_max_chars))

    for report in _latest_external(
        Path.home() / ".claude" / "curator-reports",
        "*.md",
        config.latest_curator_reports,
    ):
        add(f"Curator report {report.name}", report, _tail_chars(report, 50_000))

    for report in _latest(
        "var/reflection/*/*.md",
        config.latest_reflection_reports,
    ):
        add(
            f"Reflection report {report.parent.name}/{report.name}",
            report,
            _tail_chars(report, 50_000),
        )

    for digest in _latest("var/backlog/digest-*.md", config.latest_backlog_digests):
        add(f"Backlog digest {digest.name}", digest, _tail_chars(digest, 30_000))

    add(
        "Current actionable plan",
        config.plan_target,
        _tail_chars(config.plan_target, config.existing_plan_max_chars),
    )
    return snapshots


def _source_bundle(sources: list[SourceSnapshot]) -> str:
    if not sources:
        return "No prior loop suggestions were found."
    parts: list[str] = []
    for source in sources:
        parts.append(
            f"## SOURCE: {source.name}\nPath: {source.path}\n\n"
            f"<source-data>\n{source.content}\n</source-data>"
        )
    return "\n\n".join(parts)


def _model_metadata(stage: ModelStage, *, writable: bool) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "source": "loop-improvement-chain",
        "sandbox": {"level": "workspace_write" if writable else "read_only"},
    }
    if stage.effort:
        metadata["reasoning_effort"] = stage.effort
    # Kimi headless mode is force-auto. This is an explicit operator-selected
    # lane, but it still may run only when the outer macOS sandbox is available.
    if stage.harness == "cli-kimi":
        metadata["cli_unattended_elevated"] = True
    return metadata


def _account_retryable(result: Any) -> bool:
    if result.status == ResultStatus.OK:
        return False
    error = str(result.error or "").lower()
    return any(
        marker in error
        for marker in (
            "oauth",
            "authenticate",
            "rate limit",
            "spend limit",
            "usage limit",
            "account pool exhausted",
        )
    )


def _account_component(stage: ModelStage, account_id: str) -> str:
    return f"claude_account.{stage.model}.{account_id}"


def _stage_component(stage: ModelStage) -> str:
    return f"claude_stage.{stage.model}"


def _all_accounts_parked_result(stage: ModelStage, accounts: list[Any]) -> AgentResult:
    """Every configured account is currently parked: make ZERO provider
    calls. Refresh/create the STAGE-level park (keyed only on ``stage.model``,
    not any one account) from the MOST RECENTLY parked account's own record
    (compared by ``parked_at``'s UTC ISO timestamp, which sorts correctly as
    a plain string), so THIS gate -- ``_run_adapter`` -- is the single lowest
    entry point every ``cli-claude`` caller shares (``run_stage_json``/
    ``run_plan_editor``, and therefore ``reflection/fable_gate.py``'s direct
    ``run_stage_json`` default too) without that caller needing its own
    parking logic.
    """
    stage_component = _stage_component(stage)
    records = [
        record
        for record in (
            get_parked_reason(_account_component(stage, account.id)) for account in accounts
        )
        if record is not None
    ]
    most_recent = max(records, key=lambda record: str(record.get("parked_at") or ""), default=None)
    outcome = str(most_recent.get("outcome")) if most_recent is not None else "quota_exhausted"
    detail = (
        str(most_recent.get("detail"))
        if most_recent is not None
        else "every configured account is parked"
    )
    already = get_parked_reason(stage_component)
    if already is None:
        park_chain_failure(
            component=stage_component,
            outcome=outcome,
            detail=f"all {len(accounts)} configured accounts are parked -- {detail}",
        )
        already = get_parked_reason(stage_component)
        assert already is not None
    else:
        _log_parked_skip(stage_component, already)
        _retry_parked_alert(stage_component, already)
    return AgentResult(
        status=ResultStatus.ERROR,
        usage=AgentUsage(wall_ms=0),
        error=(
            f"{stage_component} parked ({already.get('outcome')}): all configured "
            "accounts are currently parked -- refusing the provider call"
        ),
    )


def _stage_parked_short_circuit(stage_component: str) -> AgentResult | None:
    """If ``stage_component`` is parked, refuse with ZERO provider calls and
    retry its alert; return ``None`` when it is not parked (the caller must
    then fall through to its normal, un-gated fallback).

    This is what makes ``claude_stage.<model>`` AUTHORITATIVE (round-4
    blocker): every branch in :func:`_run_adapter` that would otherwise call
    ``adapter.run(agent_input)`` WITHOUT having successfully inspected
    per-account state -- an adapter with no ``_run_once``, an unreadable
    accounts config, or zero configured accounts -- consults this FIRST.
    Only a successfully-inspected config (real accounts, each individually
    queryable) may proceed to the per-account loop below, which has its own,
    already-covered parking checks.
    """
    record = get_parked_reason(stage_component)
    if record is None:
        return None
    _log_parked_skip(stage_component, record)
    _retry_parked_alert(stage_component, record)
    return AgentResult(
        status=ResultStatus.ERROR,
        usage=AgentUsage(wall_ms=0),
        error=(
            f"{stage_component} parked ({record.get('outcome')}): account state "
            "could not be (re)inspected -- refusing the provider call"
        ),
    )


def _run_adapter(stage: ModelStage, agent_input: AgentInput) -> Any:
    """Run one stage, trying configured Claude accounts on account faults.

    TERMINAL semantics (X-B1 finding #2): rotating to a DIFFERENT account on
    an auth/quota failure is legitimate failover (a second account can be
    healthy while the first's credential expired or its own quota is spent),
    so this does NOT abandon the whole stage the way ``ChainParked`` does at
    the chain level. Instead each account is its own park-not-rotate unit:
    a terminal classification parks THAT account (keyed on
    ``stage.model``+account id) so subsequent calls stop retrying a
    known-broken credential every tick, alerts exactly once, and the loop
    still fails over to the next configured account in this same call --
    it never silently disappears as an unclassified "success masked the
    error" the way plain string-matched rotation did before this fix.

    When EVERY configured account is parked, this is the single lowest entry
    point for all ``cli-claude`` callers (``run_stage_json``/
    ``run_plan_editor``, and therefore also ``reflection/fable_gate.py``'s
    direct ``run_stage_json`` default), so it refuses with zero provider
    calls and a stage-level park rather than silently falling back to the
    default pool (round-2 finding: that fallback let an exhausted stage
    reach the provider anyway, ungated, every single invocation).

    ROUND-4: ``claude_stage.<model>`` is now AUTHORITATIVE across every
    fallback, not just the "config loaded fine but every account is parked"
    path -- a park record stops an adapter-shape mismatch, an unreadable
    accounts config, or an empty account list from reaching the provider
    too.

    ROUND-6 DESIGN (RETAIN, not delete-then-maybe-recreate): the stage
    record is now NEVER deleted speculatively. Once account configuration is
    successfully inspected, if the stage is parked and at least one account
    is not, the per-account loop is allowed to run (the stage short-circuit
    below is skipped for THIS call only) so the unparked account gets a real
    attempt -- but the stage record itself stays on disk the entire time.
    It is cleared ONLY at :func:`_clear_stage_park_on_recovery`, called the
    instant a specific account is CONFIRMED not parked (an OK result, or a
    definitive non-terminal failure). This makes "every configured account
    is parked" implies "the stage record exists" hold BY CONSTRUCTION at
    every instant -- a concurrent caller or a crash mid-recovery-attempt
    always sees a stage record present whenever it would matter, because
    there is no window where it was deleted in anticipation of a recovery
    outcome that has not yet been confirmed (the round-5 design's gap).
    """
    adapter = resolve_adapter(stage.harness)
    if stage.harness != "cli-claude":
        return adapter.run(agent_input)

    stage_component = _stage_component(stage)

    # ClaudeAdapter's public pool rotates rate limits, but an expired OAuth
    # account is a plain error. Iterate the versioned account configuration
    # directly so this chain also works from a fresh worktree whose runtime
    # account database has not been migrated yet.
    run_once = getattr(adapter, "_run_once", None)
    if not callable(run_once):
        blocked = _stage_parked_short_circuit(stage_component)
        return blocked if blocked is not None else adapter.run(agent_input)
    try:
        from omniagentos.routing.config import load_accounts_config

        provider = load_accounts_config().providers.get("claude")
        accounts = sorted(
            (account for account in (provider.accounts if provider else []) if account.enabled),
            key=lambda account: (account.priority, account.id),
        )
    except Exception:
        blocked = _stage_parked_short_circuit(stage_component)
        return blocked if blocked is not None else adapter.run(agent_input)
    if not accounts:
        blocked = _stage_parked_short_circuit(stage_component)
        return blocked if blocked is not None else adapter.run(agent_input)

    account_components = [_account_component(stage, account.id) for account in accounts]

    # Account configuration is now successfully inspected (uninspectable
    # cases were already handled by the three fallback branches above, each
    # already gated on the RETAINED stage record). If a recovery candidate
    # exists (stage parked AND at least one account unparked), fall through
    # into the per-account loop WITHOUT short-circuiting -- its own
    # per-account parked-check already lets exactly the unparked account(s)
    # through, while every already-parked account is still skipped with
    # zero calls. If there is no recovery candidate (stage not parked at
    # all, or parked with every account ALSO parked/still-parked), the
    # short-circuit below applies exactly as before -- zero provider calls,
    # retry the alert.
    if not _maybe_recovery_candidate(stage_component, account_components):
        blocked = _stage_parked_short_circuit(stage_component)
        if blocked is not None:
            return blocked

    result = None
    for account, component in zip(accounts, account_components, strict=True):
        parked = get_parked_reason(component)
        if parked is not None:
            _log_parked_skip(component, parked)
            _retry_parked_alert(component, parked)
            continue
        candidate = run_once(
            agent_input,
            env_overrides={"CLAUDE_CONFIG_DIR": account.resolved_config_dir()},
        )
        result = candidate
        if candidate.status == ResultStatus.OK:
            _clear_stage_park_on_recovery(stage_component, account_components)
            return candidate
        outcome = classify_chain_error("claude", str(candidate.error or ""))
        if is_terminal_outcome(outcome):
            assert outcome is not None
            park_chain_failure(
                component=component, outcome=outcome, detail=str(candidate.error or "")
            )
            continue
        if not _account_retryable(candidate):
            _clear_stage_park_on_recovery(stage_component, account_components)
            return candidate

    # Every account either was already parked, or just got (re-)parked this
    # call: recreate/retain the stage-level park regardless of whether any
    # account was attempted THIS call (round-5 fix, unchanged) -- this is
    # ALSO the recovery-then-re-fail path (a just-recovered account is
    # retried for real, fails terminal again, and re-parks itself
    # individually): the stage record was never removed in the first place
    # under the round-6 design, so ``_all_accounts_parked_result`` here just
    # finds it already present and retry-alerts, rather than needing to
    # recreate it from a gap.
    if all(get_parked_reason(component) is not None for component in account_components):
        return _all_accounts_parked_result(stage, accounts)
    assert result is not None
    return result


class StageFailure(RuntimeError):
    """A structured-stage or plan-editor failure, carrying the raw provider detail.

    ``detail`` is the raw error text (never the formatted message) so callers
    can classify it with :func:`classify_chain_error` without re-parsing the
    exception string.
    """

    def __init__(self, stage: ModelStage, detail: str) -> None:
        super().__init__(f"{stage.model} structured stage failed: {detail}")
        self.stage = stage
        self.detail = detail


def _handle_stage_failure(component: str, exc: StageFailure) -> str | None:
    """Classify a stage failure and park+alert-once if it is TERMINAL.

    Returns the classified outcome (or ``None``); never raises. An
    unclassified error is left for the caller to propagate/handle as before
    -- it is never treated as terminal nor as success.
    """
    outcome = classify_chain_error(_provider_for_harness(exc.stage.harness), exc.detail)
    if is_terminal_outcome(outcome):
        assert outcome is not None
        park_chain_failure(component=component, outcome=outcome, detail=exc.detail)
    return outcome


# Explicit output-token ceiling for read-only structured stages. F1 (2026-08-12
# Class-A review): the proposer set no ``tokens_max``, so the spend guard reserved
# only its optimistic default and the outbound request carried no ``max_tokens`` —
# a paid OpenRouter fallback could then bill above the reservation and be settled
# after the fact. A structured proposal/verdict never needs more than this, so an
# explicit bound gives the guard a real ceiling to reserve AND to send on the wire
# (bounded, so a call cannot overshoot its reservation). Generous vs. the free
# primary's 32k max; the paid fallbacks clamp it to their own priced maxima.
STAGE_MAX_OUTPUT_TOKENS = 16384


def run_stage_json(
    stage: ModelStage,
    prompt: str,
    schema: dict[str, Any],
    working_dir: Path,
    wall_ms: int,
) -> dict[str, Any]:
    """Run a read-only structured stage through the configured adapter."""
    if stage.harness == "cli-kimi" and not sandbox.sandbox_available():
        raise RuntimeError("Kimi loop stage refused: outer macOS sandbox is unavailable")
    response = _run_adapter(
        stage,
        AgentInput(
            run_id=new_id("loop"),
            task_id=new_id("tsk"),
            prompt=prompt,
            working_dir=str(working_dir),
            model=stage.model,
            output_schema=schema,
            tools_allowed=[],
            budget=BudgetSpec(wall_ms_max=wall_ms, max_turns=4, tokens_max=STAGE_MAX_OUTPUT_TOKENS),
            metadata=_model_metadata(stage, writable=False),
        ),
    )
    if response.status != ResultStatus.OK or not isinstance(response.output_json, dict):
        raise StageFailure(stage, str(response.error or response.status))
    return response.output_json


def run_plan_editor(
    stage: ModelStage,
    prompt: str,
    workspace: Path,
    plan_name: str,
    wall_ms: int,
) -> str:
    """Give Opus a confined workspace and require a direct plan-file edit."""
    before = (workspace / plan_name).read_text(encoding="utf-8")
    response = _run_adapter(
        stage,
        AgentInput(
            run_id=new_id("loop"),
            task_id=new_id("tsk"),
            prompt=prompt,
            working_dir=str(workspace),
            model=stage.model,
            tools_allowed=["Read", "Write", "Edit", "Glob", "Grep"],
            budget=BudgetSpec(wall_ms_max=wall_ms, max_turns=12),
            metadata=_model_metadata(stage, writable=True),
        ),
    )
    if response.status != ResultStatus.OK:
        raise StageFailure(stage, str(response.error or response.status))
    after = (workspace / plan_name).read_text(encoding="utf-8")
    if after == before:
        raise RuntimeError(
            f"{stage.model} returned without editing {plan_name}; refusing review-only output"
        )
    return response.output_text


@dataclass(frozen=True)
class StageAttempt:
    """One proposer attempt and WHY it ended the way it did.

    ``failure_kind`` is the MECHANISM (how the attempt ended); ``outcome`` is
    the shared four-class provider classification (``auth_error`` /
    ``quota_exhausted`` / ``transient_rate_limit`` / ``overloaded``) or ``None``
    when the text matches no known shape. The two are deliberately separate:
    collapsing them is precisely the defect this type exists to fix -- park, a
    StageFailure, a sandbox refusal and an adapter-resolution failure all used
    to arrive as one bare ``None`` and get reported as a spawn failure.
    """

    role: str
    component: str
    harness: str
    model: str
    failure_kind: str | None = None
    outcome: str | None = None
    detail: str | None = None

    def describe(self) -> str:
        if self.failure_kind is None:
            return f"{self.role}({self.harness}/{self.model}): ok"
        return (
            f"{self.role}({self.harness}/{self.model}): "
            f"{self.failure_kind}/{self.outcome or 'unclassified'}: "
            f"{(self.detail or '').strip()[:400]}"
        )


@dataclass(frozen=True)
class KimiJsonResult:
    """The structured outcome of the proposer seam.

    ``output is None`` means every attempt failed, and ``attempts`` says which
    ones and why -- so a caller can record the CLASSIFIED cause instead of
    inventing a mechanical one.
    """

    output: dict[str, Any] | None
    attempts: tuple[StageAttempt, ...] = ()

    @property
    def ok(self) -> bool:
        return self.output is not None

    @property
    def used_fallback(self) -> bool:
        return self.ok and any(
            a.role == "fallback" and a.failure_kind is None for a in self.attempts
        )

    @property
    def failures(self) -> tuple[StageAttempt, ...]:
        return tuple(a for a in self.attempts if a.failure_kind is not None)

    @property
    def failure_kind(self) -> str | None:
        failures = self.failures
        return failures[0].failure_kind if failures else None

    @property
    def outcome(self) -> str | None:
        """The FIRST classified cause across attempts.

        First, not last: it is the one that has to be remedied, and it is the
        one the old code buried under "spawn/CLI failure".
        """
        for attempt in self.failures:
            if attempt.outcome is not None:
                return attempt.outcome
        return None

    def describe(self) -> str:
        if not self.attempts:
            return "no attempts were made"
        return "; ".join(attempt.describe() for attempt in self.attempts)


def _stage_workspace(stage: ModelStage) -> Path:
    workspace = (
        DEFAULT_ARTIFACT_ROOT / "model-workspaces" / (stage.harness.removeprefix("cli-") or "loop")
    )
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _attempt_stage_json(
    *,
    role: str,
    component: str,
    stage: ModelStage,
    prompt: str,
    schema: dict[str, Any],
    wall_ms: int,
) -> tuple[dict[str, Any] | None, StageAttempt]:
    """Run one stage and return ``(output, attempt)`` -- never raising.

    Every terminating condition is NAMED. ``park_chain_failure`` still fires
    for terminal outcomes via ``_handle_stage_failure``, so the park/alert-once
    behaviour is unchanged; what is new is that the caller learns which one
    happened.
    """
    parked = get_parked_reason(component)
    if parked is not None:
        _log_parked_skip(component, parked)
        _retry_parked_alert(component, parked)
        return None, StageAttempt(
            role=role,
            component=component,
            harness=stage.harness,
            model=stage.model,
            failure_kind="parked",
            outcome=str(parked.get("outcome")) or None,
            detail=str(parked.get("detail") or ""),
        )
    try:
        output = run_stage_json(stage, prompt, schema, _stage_workspace(stage), wall_ms)
    except StageFailure as exc:
        outcome = _handle_stage_failure(component, exc)
        return None, StageAttempt(
            role=role,
            component=component,
            harness=stage.harness,
            model=stage.model,
            failure_kind="stage_failure",
            outcome=outcome,
            detail=exc.detail,
        )
    except Exception as exc:
        # A non-StageFailure exception (e.g. the sandbox_available() refusal,
        # or any other adapter-resolution failure) still carries provider
        # text worth classifying -- swallowing it unclassified was the exact
        # silent-failure gap X-B1 exists to close.
        detail = f"{type(exc).__name__}: {exc}"
        outcome = _handle_stage_failure(component, StageFailure(stage, detail))
        return None, StageAttempt(
            role=role,
            component=component,
            harness=stage.harness,
            model=stage.model,
            failure_kind="adapter_error",
            outcome=outcome,
            detail=detail,
        )
    return output, StageAttempt(
        role=role,
        component=component,
        harness=stage.harness,
        model=stage.model,
    )


def run_kimi_json_result(
    prompt: str,
    schema: dict[str, Any],
    *,
    effort: str | None = None,
    max_turns: int = 3,
    wall_ms: int = 180_000,
    allow_fallback: bool = False,
) -> KimiJsonResult:
    """Shared proposer seam, reporting WHY it failed instead of just ``None``.

    ``allow_fallback`` is OPT-IN and defaults to ``False``: ``swarm.optimize``
    and ``swarm.summary`` bind this module's legacy ``run_kimi_json`` as their
    default runner, and quietly re-routing those loops to another vendor is not
    this lane's call to make. The reflection proposer -- the seam that carries
    11 of the 12 genuine failures -- opts in explicitly.

    ``max_turns`` and ``effort`` are accepted for call compatibility with the
    previous Fable-shaped runner seam. Kimi's CLI has no turn cap flag and this
    stage's reasoning effort is fixed by ``configs/loop_models.yaml`` rather
    than by the caller, so both are ignored; the wall cap is enforced.
    """
    del max_turns, effort
    try:
        config = load_config()
    except Exception as exc:
        # A config-load failure before any stage is known is not a provider
        # failure at all, so it is neither classified nor parked -- but it is
        # still NAMED, rather than reported as a spawn failure.
        return KimiJsonResult(
            output=None,
            attempts=(
                StageAttempt(
                    role="primary",
                    component="run_kimi_json.primary",
                    harness="?",
                    model="?",
                    failure_kind="config_error",
                    detail=f"{type(exc).__name__}: {exc}",
                ),
            ),
        )

    output, attempt = _attempt_stage_json(
        role="primary",
        component="run_kimi_json.primary",
        stage=config.primary,
        prompt=prompt,
        schema=schema,
        wall_ms=wall_ms,
    )
    attempts = [attempt]
    if output is not None:
        return KimiJsonResult(output=output, attempts=tuple(attempts))

    if not allow_fallback:
        return KimiJsonResult(output=None, attempts=tuple(attempts))
    # Read the fallback ONLY on the opt-in path: the default path must not
    # depend on a field older configs (and existing test doubles) do not carry.
    fallback_stage = getattr(config, "primary_fallback", None)
    if fallback_stage is None:
        return KimiJsonResult(output=None, attempts=tuple(attempts))

    output, fallback_attempt = _attempt_stage_json(
        role="fallback",
        component="run_kimi_json.fallback",
        stage=fallback_stage,
        prompt=prompt,
        schema=schema,
        wall_ms=wall_ms,
    )
    attempts.append(fallback_attempt)
    return KimiJsonResult(output=output, attempts=tuple(attempts))


def run_kimi_json(
    prompt: str,
    schema: dict[str, Any],
    *,
    effort: str | None = None,
    max_turns: int = 3,
    wall_ms: int = 180_000,
) -> dict[str, Any] | None:
    """Legacy ``dict | None`` seam, retained for ``swarm.optimize``/``swarm.summary``.

    New callers should use :func:`run_kimi_json_result`, which reports the
    classified cause of a failure instead of collapsing every class to ``None``.
    """
    return run_kimi_json_result(
        prompt,
        schema,
        effort=effort,
        max_turns=max_turns,
        wall_ms=wall_ms,
    ).output


def _seed_plan(draft: str, existing: str) -> str:
    if existing.strip():
        return _strip_fable_review(existing)
    return (
        "# Loop Improvement Plan\n\n"
        "> Managed by the Kimi → Opus 5 X High → Fable review chain.\n\n"
        "## Kimi draft\n\n"
        f"{draft.strip()}\n"
    )


def _strip_fable_review(text: str) -> str:
    pattern = re.compile(
        re.escape(FABLE_REVIEW_START) + r".*?" + re.escape(FABLE_REVIEW_END),
        re.DOTALL,
    )
    return pattern.sub("", text).rstrip() + "\n"


def _render_fable_review(review: Mapping[str, Any]) -> str:
    verdict = str(review.get("verdict") or "needs_revision")
    summary = str(review.get("summary") or "").strip()
    strengths = [str(item).strip() for item in review.get("strengths") or [] if str(item).strip()]
    required = [
        str(item).strip() for item in review.get("required_changes") or [] if str(item).strip()
    ]
    lines = [
        FABLE_REVIEW_START,
        "## Fable final review",
        "",
        f"**Verdict:** `{verdict}`",
        "",
        summary or "No review summary was returned.",
    ]
    if strengths:
        lines.extend(["", "### Strengths", "", *[f"- {item}" for item in strengths]])
    if required:
        lines.extend(["", "### Required changes", "", *[f"- {item}" for item in required]])
    lines.extend(["", FABLE_REVIEW_END, ""])
    return "\n".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def run_chain(
    *,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    artifact_root: str | Path = DEFAULT_ARTIFACT_ROOT,
    json_runner: JsonRunner = run_stage_json,
    plan_editor: PlanEditor = run_plan_editor,
    now: Callable[[], datetime] | None = None,
) -> ChainResult:
    """Run Kimi -> Opus editor -> Fable reviewer and install the plan."""
    config = load_config(config_path)
    clock = now or (lambda: datetime.now(UTC))
    started = clock()
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    artifact_dir = Path(artifact_root) / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)

    sources = collect_sources(config)
    operational_context = (
        "## CURRENT CHAIN STATE (authoritative)\n\n"
        "- This branch replaces the historical single-session Fable curator with "
        "the Kimi → confined Opus 5 X High editor → read-only Fable chain.\n"
        "- The historical request to grant the curator write access under "
        "`~/.claude/**` is resolved by replacement and is now obsolete. The plan "
        "MUST NOT request that permission expansion or restoration of the old "
        "curator write path.\n"
        "- Opus may edit only the staged copy of the configured plan target. Kimi "
        "and Fable remain read-only.\n"
        "- Treat any earlier Fable review notes embedded in the current plan as "
        "feedback to disposition explicitly during this pass.\n"
    )
    bundle = operational_context + "\n" + _source_bundle(sources)
    (artifact_dir / "evidence.md").write_text(bundle + "\n", encoding="utf-8")

    draft_schema = {
        "type": "object",
        "required": ["draft_plan", "source_findings", "deferred"],
        "properties": {
            "draft_plan": {"type": "string"},
            "source_findings": {"type": "array", "items": {"type": "string"}},
            "deferred": {"type": "array", "items": {"type": "string"}},
        },
    }
    kimi_prompt = (
        "You are the primary analyst for OmniAgentOS improvement loops. "
        "Synthesize the source material below into an evidence-backed actionable "
        "plan draft. Reconcile contradictions, do not blindly accept model "
        "recommendations, preserve source/run identifiers, identify verification "
        "commands, and defer claims whose samples are too thin. Do not edit files. "
        "Return the requested structured object.\n\n" + bundle
    )
    _raise_if_parked("improvement_chain.primary")
    try:
        draft_result = json_runner(
            config.primary,
            kimi_prompt,
            draft_schema,
            artifact_dir,
            config.primary_wall_ms,
        )
    except StageFailure as exc:
        _handle_stage_failure("improvement_chain.primary", exc)
        raise
    draft = str(draft_result.get("draft_plan") or "").strip()
    if not draft:
        raise RuntimeError("Kimi returned an empty draft plan")
    (artifact_dir / "kimi-draft.json").write_text(
        json.dumps(draft_result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    workspace = artifact_dir / "opus-workspace"
    workspace.mkdir()
    plan_name = config.plan_target.name
    existing = (
        config.plan_target.read_text(encoding="utf-8", errors="replace")
        if config.plan_target.is_file()
        else ""
    )
    (workspace / plan_name).write_text(_seed_plan(draft, existing), encoding="utf-8")
    (workspace / "KIMI-DRAFT.md").write_text(draft + "\n", encoding="utf-8")
    (workspace / "EVIDENCE.md").write_text(bundle + "\n", encoding="utf-8")

    editor_prompt = (
        f"You are the plan editor. Use Claude Opus 5 at X High effort. Open "
        f"`{plan_name}` and DIRECTLY EDIT that file in this workspace; a prose "
        "review in your response is not completion. Read `KIMI-DRAFT.md` and "
        "`EVIDENCE.md`. Improve the plan by deduplicating suggestions, resolving "
        "conflicts against evidence, turning vague ideas into ordered tasks with "
        "owners/paths/acceptance checks, retaining already-completed context, and "
        "marking uncertain recommendations as experiments. Do not edit either "
        "evidence file. Do not create code or change configs. Leave a complete, "
        "standalone Markdown plan in the target file."
    )
    _raise_if_parked("improvement_chain.plan_editor")
    try:
        editor_summary = plan_editor(
            config.plan_editor,
            editor_prompt,
            workspace,
            plan_name,
            config.editor_wall_ms,
        )
    except StageFailure as exc:
        _handle_stage_failure("improvement_chain.plan_editor", exc)
        raise
    edited = (workspace / plan_name).read_text(encoding="utf-8", errors="strict")
    encoded = edited.encode("utf-8")
    if not edited.lstrip().startswith("#") or not edited.strip():
        raise RuntimeError("Opus-edited plan is not a non-empty Markdown document")
    if len(encoded) > config.plan_max_bytes:
        raise RuntimeError(
            f"Opus-edited plan is {len(encoded)} bytes; cap is {config.plan_max_bytes}"
        )
    (artifact_dir / "opus-editor-summary.txt").write_text(
        editor_summary.strip() + "\n",
        encoding="utf-8",
    )
    (artifact_dir / "opus-edited-plan.md").write_text(edited, encoding="utf-8")

    review_schema = {
        "type": "object",
        "required": ["verdict", "summary", "strengths", "required_changes"],
        "properties": {
            "verdict": {"type": "string", "enum": ["approved", "needs_revision"]},
            "summary": {"type": "string"},
            "strengths": {"type": "array", "items": {"type": "string"}},
            "required_changes": {"type": "array", "items": {"type": "string"}},
        },
    }
    review_prompt = (
        "You are the final Fable reviewer. Review the Opus-edited actionable plan "
        "below against the accumulated loop evidence. Check that recommendations "
        "are supported, conflicts are resolved, tasks are editable/actionable, "
        "verification is concrete, risky config changes remain gated, and no "
        "important existing suggestion was silently lost. You are read-only: "
        "return the structured final review.\n\n"
        f"## OPUS-EDITED PLAN\n\n{edited}\n\n"
        f"## SOURCE EVIDENCE\n\n{bundle}"
    )
    _raise_if_parked("improvement_chain.final_reviewer")
    try:
        review = json_runner(
            config.final_reviewer,
            review_prompt,
            review_schema,
            artifact_dir,
            config.reviewer_wall_ms,
        )
    except StageFailure as exc:
        _handle_stage_failure("improvement_chain.final_reviewer", exc)
        raise
    verdict = str(review.get("verdict") or "needs_revision")
    if verdict not in {"approved", "needs_revision"}:
        raise RuntimeError(f"Fable returned invalid verdict {verdict!r}")
    (artifact_dir / "fable-review.json").write_text(
        json.dumps(review, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    final_plan = _strip_fable_review(edited).rstrip() + "\n\n" + _render_fable_review(review)
    if len(final_plan.encode("utf-8")) > config.plan_max_bytes:
        raise RuntimeError("final reviewed plan exceeds configured size cap")
    _atomic_write(config.plan_target, final_plan)
    (artifact_dir / "final-plan.md").write_text(final_plan, encoding="utf-8")

    result = ChainResult(
        run_id=run_id,
        plan_path=str(config.plan_target),
        artifact_dir=str(artifact_dir),
        verdict=verdict,
        models={
            "primary": asdict(config.primary),
            "plan_editor": asdict(config.plan_editor),
            "final_reviewer": asdict(config.final_reviewer),
        },
        sources=[source.path for source in sources],
    )
    (artifact_dir / "result.json").write_text(
        json.dumps(asdict(result), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    improvement_log = ROOT / "var" / "improvement-log.jsonl"
    improvement_log.parent.mkdir(parents=True, exist_ok=True)
    with improvement_log.open("a", encoding="utf-8") as log:
        log.write(
            json.dumps(
                {
                    "ts": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "improver": "loop-plan-review",
                    "changes": [
                        {
                            "path": str(config.plan_target),
                            "kind": "plan",
                            "summary": (
                                "Kimi draft refined by Opus 5 X High and "
                                f"reviewed by Fable ({verdict})"
                            ),
                        }
                    ],
                    "notes": f"artifact_dir={artifact_dir}",
                    "report_path": str(config.plan_target),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Kimi -> Opus 5 X High plan edit -> Fable review"
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    args = parser.parse_args(argv)

    artifact_root = Path(args.artifact_root)
    artifact_root.mkdir(parents=True, exist_ok=True)
    lock_path = artifact_root / ".lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(json.dumps({"status": "skipped", "reason": "review chain already running"}))
            return 0
        result = run_chain(config_path=args.config, artifact_root=artifact_root)
    print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ChainConfig",
    "ChainParked",
    "ChainResult",
    "ModelStage",
    "SourceSnapshot",
    "StageFailure",
    "classify_chain_error",
    "clear_parked_reason",
    "collect_sources",
    "get_parked_reason",
    "is_terminal_outcome",
    "load_config",
    "main",
    "park_chain_failure",
    "run_chain",
    "run_kimi_json",
    "run_plan_editor",
    "run_stage_json",
]
