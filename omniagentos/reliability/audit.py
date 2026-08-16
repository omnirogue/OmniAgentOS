"""Scheduled reliability audit entry points — the §11 orchestration loops.

Four cadences, each running under an audit-row lifecycle
(``queued → running → completed|failed``):

  * :func:`watch` (every 600 s) — detector sweep (high-water mark) → safe recovery
    claims → critical alerts → pipeline monitoring-window checks/auto-rollback →
    stale-stage reclaim (§5b.6).
  * :func:`twice_daily` (06:30 + 18:30) — full sweep + degradation scan → analyzer
    proposals (memory-first) → sandbox → judges → pipeline mode-routing → department
    reviews + CTO daily pass → vault report → grouped notifications.
  * :func:`daily_summary` (08:05) — consolidated improvement summary (info notice).
  * :func:`weekly_architecture` (Sun 09:00) — CTO deep review + scorecard trends +
    archdocs staleness → ``doc_stale`` finding.

Design invariants:
  * Every audit runs under its row lifecycle; a stage failure is CAUGHT and recorded
    in ``stats_json['errors']`` — one failing stage never aborts the audit, and one
    failing audit never aborts the scheduling loop.
  * All collaborators (detector, recovery, analyzer, sandbox+judge pipeline,
    department/CTO reviews, notifier) are injectable so the loop is testable with mock
    adapters and never touches a live LLM CLI in tests. LLM-dependent stages run only
    when their collaborator is supplied.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from omniagentos.contracts import utc_now_iso
from omniagentos.reliability.contracts import ReliabilityStore
from omniagentos.reliability.report import write_audit_report
from omniagentos.reliability.taxonomy import (
    EventStatus,
    FailureClass,
    ImprovementStatus,
    Severity,
)

# Stages that can be reclaimed as "stale" and the terminal they retreat to.
_STALE_STAGE_SECONDS = 1800  # 30 min without progress ⇒ stale (bounded retry ⇒ failed)
_ANALYZER_EVENT_BUDGET = 25  # cap analyzer calls per audit (token budget guard)
_WATCH_INTERVAL_SECONDS = 600
# First-run bootstrap lookback: the very first watch must look BACK, not start at
# now(), or pre-existing failures present on deploy day are silently never seen.
_WATCH_BOOTSTRAP_HOURS = 24.0
# Env gate for the LLM-dependent stages (analyzer proposals, sandbox/judge pipeline).
# '1' (default) → construct real production collaborators; '0' → skip them cleanly.
_LLM_ENV = "OMNIAGENTOS_RELIABILITY_LLM"
# Optional reminder cadence for critical events that stay open. Unset/0 (the
# default) = alert exactly once per event; the twice-daily grouped digest is
# what reports the standing open-event count.
_REALERT_ENV = "OMNIAGENTOS_RELIABILITY_REALERT_HOURS"


def _iso_ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(hours: float) -> tuple[str, str]:
    return _iso_ago(hours), utc_now_iso()


# --- Injectable collaborators (defaults degrade gracefully) -----------------


@dataclass
class AuditDeps:
    """Everything an audit stage might need; all optional, all injectable."""

    vault_dir: str | None = None
    repo_root: str | None = None
    detect_fn: Callable[..., list[Any]] | None = None
    recover_fn: Callable[[ReliabilityStore], dict[str, Any]] | None = None
    analyze_fn: Callable[..., str] | None = None
    memory_search: Callable[[str], Any] | None = None
    adapter_fn: Callable[..., Any] | None = None  # analyzer-style: (prompt) -> dict
    company_adapter_fn: Callable[..., Any] | None = (
        None  # company-style: (harness, prompt, **) -> AgentResult
    )
    pipeline: Any | None = None
    sandbox_runner: Any | None = None  # explicit C-03 collaborator
    judge_panel: Any | None = None  # explicit C-03 collaborator
    departments_fn: Callable[..., dict[str, Any]] | None = None
    cto_daily_fn: Callable[..., dict[str, Any]] | None = None
    cto_weekly_fn: Callable[..., dict[str, Any]] | None = None
    staleness_fn: Callable[..., Any] | None = None
    notifier: Callable[..., Any] | None = None
    window_hours: float | None = None
    # Test seam only: skip L09/L03 gates when exercising a fully injected path.
    force_activation: bool = False

    @classmethod
    def from_kwargs(cls, kwargs: dict[str, Any]) -> AuditDeps:
        known: dict[str, Any] = {f: kwargs.get(f) for f in cls.__dataclass_fields__}
        return cls(**known)


def _default_notifier(store: ReliabilityStore) -> Callable[..., Any]:
    """Build a notifier that raises on failure so the audit can record it (M-46).

    Callers must use :func:`_safe_notify` (or equivalent) so a notification failure
    is durable and operator-visible without aborting the main audit stages.
    """
    conn = getattr(store, "_connection", None)

    def _notify(
        *,
        kind: str,
        title: str,
        severity: str = "info",
        ref_type: str | None = None,
        ref_id: str | None = None,
        dedupe: bool = True,
        payload: dict[str, Any] | None = None,
    ) -> None:
        from omniagentos.notifications.service import (
            NotificationPersistenceError,
            record_notification_result,
        )

        result = record_notification_result(
            kind=kind,
            title=title,
            severity=severity,
            ref_type=ref_type,
            ref_id=ref_id,
            payload=payload or {},
            connection=conn,
            push=False,
            dedupe=dedupe,
        )
        if result.status == "failed":
            raise NotificationPersistenceError(result.error or "notification persistence failed")

    return _notify


def _persist_notify_failure(
    store: ReliabilityStore,
    *,
    audit_id: str,
    error: str,
    kind: str,
    title: str,
) -> bool:
    """Try to persist notification degradation without notifying recursively.

    Returns whether the dedicated reliability-state row committed. The audit
    result remains degraded either way, so failure of this fallback is explicit
    in ``stats_json`` rather than being swallowed as a second false success.
    """
    conn = getattr(store, "_connection", None)
    if conn is None:
        return False
    try:
        payload = {
            "audit_id": audit_id,
            "error": error,
            "kind": kind,
            "title": title,
            "at": utc_now_iso(),
        }
        key = f"notify_failure:{audit_id}:{payload['at']}"
        now = utc_now_iso()
        conn.execute(
            """
            INSERT INTO reliability_state (key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json = ?, updated_at = ?
            """,
            (key, json.dumps(payload), now, json.dumps(payload), now),
        )
        # Optional commit — store may own the connection lifecycle.
        try:
            conn.commit()
        except Exception:  # noqa: BLE001
            return False
        return True
    except Exception:  # noqa: BLE001 - reported by the bool result, never recursive
        return False


def _safe_notify(run: _Run, **kwargs: Any) -> bool:
    """Fire a notification; failures are durable + visible, never silent (M-46).

    Returns True on success. On failure records ``run.errors``, marks the audit
    stats degraded, and persists a reliability_state row — without converting the
    main audit outcome into a false clean success.
    """
    notify = run.deps.notifier or _default_notifier(run.store)
    try:
        notify(**kwargs)
        return True
    except Exception as exc:  # noqa: BLE001
        failure: dict[str, Any] = {
            "stage": "notify",
            "error": str(exc),
            "kind": kwargs.get("kind"),
            "title": kwargs.get("title"),
        }
        run.errors.append(failure)
        run.stats["degraded"] = True
        run.stats["notify_failures"] = int(run.stats.get("notify_failures") or 0) + 1
        durable = _persist_notify_failure(
            run.store,
            audit_id=run.audit_id,
            error=str(exc),
            kind=str(kwargs.get("kind") or ""),
            title=str(kwargs.get("title") or ""),
        )
        failure["durable"] = durable
        if not durable:
            run.stats["notify_failure_persistence_failed"] = (
                int(run.stats.get("notify_failure_persistence_failed") or 0) + 1
            )
        return False


def _try_build_sandbox_runner(repo_root: str) -> Any:
    """Construct the production sandbox adapter when available (owned by L05)."""
    try:
        from omniagentos.reliability.sandbox import PipelineSandboxRunner

        return PipelineSandboxRunner()
    except Exception:  # noqa: BLE001
        return None


def _try_build_judge_panel(store: ReliabilityStore) -> Any:
    """Construct the production judge panel when available."""
    try:
        from omniagentos.reliability.judges import JudgePanel

        return JudgePanel(store)
    except Exception:  # noqa: BLE001
        return None


def _hydrate_production_deps(store: ReliabilityStore, deps: AuditDeps) -> AuditDeps:
    """Fill in the real pipeline + analyzer collaborators for production runs.

    Without this, a production ``watch()``/``twice_daily()`` invoked with no injected
    collaborators had NO pipeline — so sandbox/judges/monitoring/auto-rollback never
    ran (codex #5 / C-03). Gated by ``OMNIAGENTOS_RELIABILITY_LLM``: '0' skips the LLM
    stages cleanly, '1' (default) constructs the pipeline.

    Production **sandbox/judge activation** remains gated on L09 containment and the
    L03 fenced-state contract (C-03). Collaborators are only attached when those
    seams report ready (or ``force_activation`` is set for tests). Absent
    collaborators produce a visible degraded activation assessment rather than a
    silent proposed-forever path.
    """
    if os.environ.get(_LLM_ENV, "1") == "0":
        return deps

    from omniagentos.reliability.pipeline import (
        ImprovementPipeline,
        assess_pipeline_activation,
    )

    sandbox = deps.sandbox_runner
    judges = deps.judge_panel
    # Production may construct real collaborators only when dependency gates pass
    # (or tests force-activate). Never bypass L09/L03 by default.
    pre_assessment = assess_pipeline_activation(
        store=store,
        sandbox_runner=object(),  # probe dependency gates only
        judge_panel=object(),
        force_activation=deps.force_activation,
    )
    dependency_gates_ok = (
        not any(r.startswith("l09_") or r.startswith("l03_") for r in pre_assessment.reasons)
        or deps.force_activation
    )

    if dependency_gates_ok:
        if sandbox is None:
            sandbox = _try_build_sandbox_runner(deps.repo_root or os.getcwd())
            deps.sandbox_runner = sandbox
        if judges is None:
            judges = _try_build_judge_panel(store)
            deps.judge_panel = judges

    if deps.pipeline is None:
        try:
            deps.pipeline = ImprovementPipeline(
                store,
                repo_root=deps.repo_root or os.getcwd(),
                sandbox_runner=sandbox,
                judge_panel=judges,
                force_activation=deps.force_activation,
            )
        except Exception:  # noqa: BLE001 - a missing pipeline degrades to "no pipeline"
            deps.pipeline = None
    else:
        # Attach collaborators onto an injected pipeline shell when possible.
        if getattr(deps.pipeline, "sandbox_runner", None) is None and sandbox is not None:
            try:
                deps.pipeline.sandbox_runner = sandbox
            except Exception:  # noqa: BLE001
                pass
        if getattr(deps.pipeline, "judge_panel", None) is None and judges is not None:
            try:
                deps.pipeline.judge_panel = judges
            except Exception:  # noqa: BLE001
                pass
        if deps.force_activation and hasattr(deps.pipeline, "force_activation"):
            deps.pipeline.force_activation = True

    if deps.analyze_fn is None and deps.adapter_fn is None:
        try:
            from omniagentos.reliability.analyzer import _default_adapter

            deps.adapter_fn = _default_adapter
        except Exception:  # noqa: BLE001
            pass
    return deps


# --- Watch cursor management (bootstrap lookback + first_seen + hold-on-error) ---


def _read_watch_state(store: ReliabilityStore) -> dict[str, Any] | None:
    """Read the raw ``watch_cursor`` reliability_state value_json, or None if unset."""
    conn = getattr(store, "_connection", None)
    if conn is None:
        return None
    row = conn.execute(
        "SELECT value_json FROM reliability_state WHERE key = 'watch_cursor'"
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value_json"] or "{}")
    except (ValueError, TypeError):
        return {}


def _load_watch_cursor(store: ReliabilityStore) -> str:
    """Cursor for the detector window.

    First run (no persisted cursor row) bootstraps to ``now - 24h`` so pre-existing
    failures on deploy day ARE seen — never ``now()``, which would skip them (§5b.5).
    """
    state = _read_watch_state(store)
    if state and state.get("cursor"):
        return str(state["cursor"])
    return _iso_ago(_WATCH_BOOTSTRAP_HOURS)


def _persist_watch_cursor(store: ReliabilityStore, cursor: str) -> None:
    """Persist the cursor + a set-once ``first_seen`` stamp (steward dead-man reads it).

    Written directly (the frozen store's ``advance_watch_cursor`` only stores
    ``{"cursor": ...}`` and would drop ``first_seen``). ``updated_at`` is always
    refreshed so the reliability dead-man rule sees the watch is alive even when the
    cursor is being HELD after an error.
    """
    conn = getattr(store, "_connection", None)
    if conn is None:
        # No raw connection (unusual store impl): fall back to the frozen contract.
        store.advance_watch_cursor(cursor)
        return
    state = _read_watch_state(store) or {}
    value = {"cursor": cursor, "first_seen": state.get("first_seen") or utc_now_iso()}
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO reliability_state (key, value_json, updated_at)
        VALUES ('watch_cursor', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value_json = ?, updated_at = ?
        """,
        (json.dumps(value), now, json.dumps(value), now),
    )


def _finalize_watch_cursor(run: _Run, loaded_cursor: str, high_water: str) -> None:
    """Advance the cursor ONLY on a clean window; otherwise HOLD it and alert (codex #3).

    Advancing past a window whose detection/insertion errored would silently skip the
    failures that never got recorded. On any stage error we keep the loaded cursor
    (so the window is re-scanned next cycle — inserts are idempotent), record the last
    error, and fire a warning alert so the skip is never silent.
    """
    if run.errors:
        run.stats["cursor_held"] = True
        run.stats["last_error"] = run.errors[-1]
        _persist_watch_cursor(run.store, loaded_cursor)  # keep cursor; refresh liveness
        _safe_notify(
            run,
            kind="alert",
            title=f"{run.kind} cursor held: {len(run.errors)} stage error(s), window not advanced",
            severity="warning",
            ref_type="reliability_audit",
            ref_id=run.audit_id,
            dedupe=True,
            payload={"errors": run.errors[:10], "held_at": loaded_cursor},
        )
        return
    try:
        _persist_watch_cursor(run.store, high_water)
    except Exception as exc:  # noqa: BLE001
        run.errors.append({"stage": "advance_cursor", "error": str(exc)})


# --- Audit run scaffolding --------------------------------------------------


@dataclass
class _Run:
    """Live state for one audit: its row + accumulating stats and errors."""

    store: ReliabilityStore
    kind: str
    deps: AuditDeps
    audit_id: str = ""
    window_start: str = ""
    window_end: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)

    def stage(self, name: str, fn: Callable[[], Any]) -> Any:
        """Run one stage; a failure is recorded, never propagated (§11)."""
        try:
            result = fn()
            if isinstance(result, dict):
                self.stats[name] = result
            return result
        except Exception as exc:  # noqa: BLE001 - one stage never aborts the audit
            self.errors.append({"stage": name, "error": str(exc)})
            return None


def _begin(
    store: ReliabilityStore,
    kind: str,
    deps: AuditDeps,
    window_hours: float,
    audit_id: str | None = None,
) -> _Run:
    # Honor a pre-created queued audit row (CLI ``--audit-id``): START that row rather
    # than mint a new one, so the row the caller is tracking is the one that runs.
    if audit_id:
        existing = store.get_audit(audit_id)
        if existing is not None:
            start = existing.window_start or _iso_ago(window_hours)
            end = existing.window_end or utc_now_iso()
        else:
            start, end = _window(window_hours)
        store.start_audit(audit_id)
        return _Run(
            store=store, kind=kind, deps=deps, audit_id=audit_id, window_start=start, window_end=end
        )
    start, end = _window(window_hours)
    audit_id = store.create_audit(kind, start, end)
    store.start_audit(audit_id)
    return _Run(
        store=store, kind=kind, deps=deps, audit_id=audit_id, window_start=start, window_end=end
    )


def _finish(run: _Run, findings: int, note_path: str | None) -> dict[str, Any]:
    run.stats["errors"] = run.errors
    run.store.complete_audit(
        run.audit_id, stats_json=run.stats, findings=findings, report_note_path=note_path
    )
    return {
        "audit_id": run.audit_id,
        "stats_json": run.stats,
        "findings_count": findings,
        "report_note_path": note_path,
    }


def _report(run: _Run, findings: int, events: list[Any]) -> str | None:
    vault = run.deps.vault_dir
    if not vault:
        return None
    try:
        return write_audit_report(vault, run.audit_id, run.kind, run.stats, events)
    except Exception as exc:  # noqa: BLE001 - vault write is best-effort
        run.errors.append({"stage": "report", "error": str(exc)})
        return None


# --- Shared stages ----------------------------------------------------------


def _detect_and_persist(run: _Run, since: str) -> dict[str, Any]:
    """Detector sweep up to a high-water mark; persist events idempotently (§5b.5).

    Rule and insert errors are surfaced onto ``run.errors`` so the caller HOLDS the
    watch cursor rather than advancing past failures that were never recorded (codex #3).
    ``insert_reliability_event`` uses ``INSERT OR IGNORE``, so a duplicate
    occurrence_key does not raise — any exception here is a genuine write error,
    not idempotent dedup.
    """
    deps = run.deps
    rule_errors: list[dict[str, str]] = []
    if deps.detect_fn is None:
        from omniagentos.reliability.detector import detect_failures

        candidates = detect_failures(run.store, since, run.window_end, errors=rule_errors) or []
    else:
        candidates = deps.detect_fn(run.store, since, run.window_end) or []
    for err in rule_errors:
        run.errors.append({"stage": "detect_rule", **err})
    inserted = 0
    insert_errors = 0
    for c in candidates:
        try:
            run.store.insert_reliability_event(
                failure_class=c.failure_class,
                severity=c.severity,
                signature=c.signature,
                occurrence_key=c.occurrence_key,
                source=c.source,
                ref_type=c.ref_type,
                ref_id=c.ref_id,
                evidence_json=c.evidence_json,
            )
            inserted += 1
        except Exception as exc:  # noqa: BLE001 - a real write error must hold the cursor
            insert_errors += 1
            run.errors.append({"stage": "insert", "error": str(exc)})
    return {
        "candidates": len(candidates),
        "inserted": inserted,
        "insert_errors": insert_errors,
        "rule_errors": len(rule_errors),
    }


def _recover(run: _Run) -> dict[str, Any]:
    recover_fn = run.deps.recover_fn
    if recover_fn is None:
        from omniagentos.reliability.recovery import run_recovery_cycle

        recover_fn = run_recovery_cycle
    return recover_fn(run.store)


def _realert_cutoff() -> str | None:
    """Timestamp before which an already-alerted critical event may alert again.

    ``None`` (the default) means never re-alert: one notification per event.
    Set ``OMNIAGENTOS_RELIABILITY_REALERT_HOURS`` to a positive number to get a
    bounded reminder cadence for events that stay open.
    """
    raw = (os.environ.get(_REALERT_ENV) or "").strip()
    if not raw:
        return None
    try:
        hours = float(raw)
    except ValueError:
        return None
    if hours <= 0:
        return None
    return _iso_ago(hours)


def _critical_alerts(run: _Run) -> dict[str, Any]:
    """Alert ONCE per open critical event (critical is cooldown-exempt, m5).

    Cooldown-exempt means a critical never waits behind the generic notification
    cooldown — it does NOT mean it re-fires forever. Recovery only allow-lists
    rate_limit/timeout, so classes like session_error/account_disabled sit at
    'open' until a human acts; re-listing every open critical each 600 s cycle
    turned 43 stale events into 246 notifications/hour indefinitely. The
    ``alerted_at`` stamp (migration 053) bounds it to one alert per event, plus
    an optional re-alert cadence; ongoing volume is carried by the twice-daily
    grouped digest, which reports the open-event count.
    """
    cutoff = _realert_cutoff()
    fired = 0
    stamp_errors = 0
    events = run.store.list_events_awaiting_alert(
        severity=Severity.CRITICAL.value,
        status=EventStatus.OPEN.value,
        realert_before=cutoff,
        limit=200,
    )
    # F7: per-event cap — alert once, then at most once per realert window, max M.
    max_alerts = int(os.environ.get("OMNIAGENTOS_RELIABILITY_MAX_ALERTS_PER_EVENT", "3"))
    skipped_capped = 0
    for ev in events:
        recovery = dict(ev.recovery_json or {}) if isinstance(ev.recovery_json, dict) else {}
        alert_count = int(recovery.get("alert_count") or 0)
        if alert_count >= max_alerts:
            skipped_capped += 1
            try:
                run.store.mark_event_alerted(ev.id)
            except Exception:  # noqa: BLE001
                pass
            continue
        if _safe_notify(
            run,
            kind="alert",
            title=f"Critical failure: {ev.failure_class}",
            severity="critical",
            ref_type="reliability_event",
            ref_id=ev.id,
            # Critical stays cooldown-exempt, but per-event cap above bounds storms.
            dedupe=False,
            payload={
                "failure_class": ev.failure_class,
                "source": ev.source,
                "alert_count": alert_count + 1,
            },
        ):
            fired += 1
        try:
            run.store.mark_event_alerted(ev.id)
            recovery["alert_count"] = alert_count + 1
            if hasattr(run.store, "update_event_recovery"):
                run.store.update_event_recovery(ev.id, recovery)
        except Exception as exc:  # noqa: BLE001 - a failed stamp must not abort the sweep
            stamp_errors += 1
            run.errors.append({"stage": "critical_alerts_stamp", "error": str(exc)})
    result: dict[str, Any] = {
        "alerts_fired": fired,
        "realert_cutoff": cutoff,
        "skipped_capped": skipped_capped,
        "max_alerts_per_event": max_alerts,
    }
    if stamp_errors:
        result["stamp_errors"] = stamp_errors
    return result


def _monitoring_checks(run: _Run) -> dict[str, Any]:
    """Evaluate each in-flight observation window; the pipeline auto-rolls-back on regression (M1)."""
    pipeline = run.deps.pipeline
    if pipeline is None:
        return {"checked": 0}
    checked = 0
    outcomes: dict[str, str] = {}
    for imp in run.store.list_improvements(status=ImprovementStatus.MONITORING.value, limit=200):
        try:
            outcomes[imp.id] = pipeline.monitor_tick(imp.id)
            checked += 1
        except Exception as exc:  # noqa: BLE001 - one improvement never aborts the sweep
            run.errors.append({"stage": "monitor", "error": f"{imp.id}: {exc}"})
    return {"checked": checked, "outcomes": outcomes}


def _reclaim_stale(run: _Run) -> dict[str, Any]:
    """Reclaim stale stages + apply lease (§5b.6/3). Bounded retries ⇒ failed + notify."""
    store = run.store
    pipeline = run.deps.pipeline
    cutoff = _iso_ago(_STALE_STAGE_SECONDS / 3600.0)
    reclaimed = 0

    # Zombie apply lease (fencing token invalidates the holder).
    try:
        if store.reclaim_stale_lease("reliability:apply", threshold_seconds=_STALE_STAGE_SECONDS):
            reclaimed += 1
    except Exception:  # noqa: BLE001 - best-effort
        pass

    for status in (ImprovementStatus.TESTING.value, ImprovementStatus.JUDGING.value):
        for imp in store.list_improvements(status=status, limit=200):
            started = imp.stage_started_at or ""
            if started and started >= cutoff:
                continue
            try:
                store.transition_improvement(
                    imp.id,
                    status,
                    ImprovementStatus.FAILED.value,
                    actor="audit:reclaim",
                    detail_json={"reason": "stale_stage", "stage": status},
                )
                _safe_notify(
                    run,
                    kind="escalation",
                    title=f"Reclaimed stale {status} stage",
                    severity="warning",
                    ref_type="improvement",
                    ref_id=imp.id,
                    dedupe=True,
                )
                reclaimed += 1
            except Exception:  # noqa: BLE001 - CAS conflict means someone else moved it
                continue

    # Applying is resumable-idempotent — reconcile rather than fail (§5b.2, M2).
    if pipeline is not None:
        for imp in store.list_improvements(status=ImprovementStatus.APPLYING.value, limit=50):
            try:
                pipeline.reconcile_apply(imp.id)
                reclaimed += 1
            except Exception as exc:  # noqa: BLE001
                run.errors.append({"stage": "reclaim_applying", "error": f"{imp.id}: {exc}"})

    return {"reclaimed": reclaimed}


# --- Analyzer + pipeline decision (twice-daily) -----------------------------


def _propose_and_decide(run: _Run) -> dict[str, Any]:
    """Analyzer proposals (memory-first) then sandbox→judge→mode-routing per proposal.

    Absent sandbox/judge collaborators or unmet L09/L03 gates block activation and
    surface a degraded result (C-03) — proposals are never silently stranded without
    an operator-visible reason.
    """
    deps = run.deps
    store = run.store
    proposed: list[str] = []
    decisions: dict[str, str] = {}
    blocked: list[str] = []

    analyze_fn = deps.analyze_fn
    if analyze_fn is None and deps.adapter_fn is not None:
        from omniagentos.reliability.analyzer import analyze_event

        analyze_fn = analyze_event
    memory_search = deps.memory_search
    if memory_search is None:
        from omniagentos.reliability.memory import search_improvements

        def memory_search(sig: str) -> Any:
            return search_improvements(store, signature=sig)

    # Only propose when an analyzer collaborator is available (no live-CLI in tests).
    if analyze_fn is not None:
        open_events = store.list_events(status=EventStatus.OPEN.value, limit=_ANALYZER_EVENT_BUDGET)
        for ev in open_events:
            try:
                kwargs: dict[str, Any] = {"memory_search": memory_search, "origin": "audit"}
                if deps.adapter_fn is not None:
                    kwargs["adapter_fn"] = deps.adapter_fn
                if deps.repo_root:
                    kwargs["repo_dir"] = deps.repo_root
                imp_id = analyze_fn(store, ev.id, **kwargs)
                if imp_id:
                    proposed.append(imp_id)
            except Exception as exc:  # noqa: BLE001 - one event never aborts the batch
                run.errors.append({"stage": "analyze", "error": f"{ev.id}: {exc}"})

    activation: dict[str, Any] = {
        "active": False,
        "degraded": True,
        "reasons": ["pipeline_absent"],
    }
    if deps.pipeline is not None:
        try:
            from omniagentos.reliability.pipeline import assess_pipeline_activation

            status: Any = (
                deps.pipeline.activation_status()
                if hasattr(deps.pipeline, "activation_status")
                else (
                    assess_pipeline_activation(
                        store=store,
                        sandbox_runner=getattr(
                            deps.pipeline, "sandbox_runner", deps.sandbox_runner
                        ),
                        judge_panel=getattr(deps.pipeline, "judge_panel", deps.judge_panel),
                        force_activation=deps.force_activation,
                    )
                )
            )
            activation = status.as_dict() if hasattr(status, "as_dict") else dict(status)
        except Exception as exc:  # noqa: BLE001
            activation = {
                "active": False,
                "degraded": True,
                "reasons": [f"activation_check_failed:{exc}"],
            }

        if not activation.get("active"):
            run.stats["degraded"] = True
            for imp in store.list_improvements(status=ImprovementStatus.PROPOSED.value, limit=200):
                blocked.append(imp.id)
                decisions[imp.id] = "degraded"
            if blocked:
                _safe_notify(
                    run,
                    kind="escalation",
                    title=(
                        "Sandbox/judge activation blocked: "
                        + ",".join(activation.get("reasons") or ["unknown"])
                    ),
                    severity="warning",
                    ref_type="reliability_audit",
                    ref_id=run.audit_id,
                    dedupe=True,
                    payload={"activation": activation, "blocked_improvements": blocked[:50]},
                )
        else:
            for imp in store.list_improvements(status=ImprovementStatus.PROPOSED.value, limit=200):
                try:
                    result = deps.pipeline.sandbox_and_judge(imp.id)
                    decisions[imp.id] = getattr(result, "status", "?")
                    if getattr(result, "status", None) == "degraded":
                        run.stats["degraded"] = True
                        blocked.append(imp.id)
                except Exception as exc:  # noqa: BLE001 - one proposal never aborts the sweep
                    run.errors.append({"stage": "decide", "error": f"{imp.id}: {exc}"})
    else:
        run.stats["degraded"] = True

    return {
        "proposed": proposed,
        "decisions": decisions,
        "activation": activation,
        "blocked": blocked,
    }


def _degradation_scan(run: _Run) -> dict[str, Any]:
    """Retry/cost/latency trend vs the immediately-prior window (raw runs SQL)."""
    conn = getattr(run.store, "_connection", None)
    if conn is None:
        return {"skipped": True}
    width = float(run.deps.window_hours or 12.0)
    cur_start, cur_end = run.window_start, run.window_end
    prior_start = _iso_ago(width * 2)

    def _agg(start: str, end: str) -> dict[str, float]:
        row = conn.execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN state='failed' OR error IS NOT NULL THEN 1 ELSE 0 END) AS failures, "
            "SUM(MAX(attempt-1,0)) AS retries, "
            "AVG(wall_ms) AS latency, SUM(COALESCE(cost_usd,0)) AS cost "
            "FROM runs WHERE COALESCE(finished_at, updated_at) >= ? AND COALESCE(finished_at, updated_at) < ?",
            (start, end),
        ).fetchone()
        return {
            "n": float(row["n"] or 0),
            "failures": float(row["failures"] or 0),
            "retries": float(row["retries"] or 0),
            "latency": float(row["latency"] or 0),
            "cost": float(row["cost"] or 0),
        }

    cur = _agg(cur_start, cur_end)
    prior = _agg(prior_start, cur_start)
    findings: list[dict[str, Any]] = []
    checks = [
        ("retries", FailureClass.RETRY_SPIKE.value),
        ("cost", FailureClass.COST_SPIKE.value),
        ("latency", FailureClass.LATENCY_REGRESSION.value),
    ]
    for metric, klass in checks:
        if prior[metric] > 0 and cur[metric] > prior[metric] * 1.5:
            findings.append(
                {"metric": metric, "prior": prior[metric], "current": cur[metric], "class": klass}
            )
            try:
                sig = f"degradation|{metric}"
                run.store.insert_reliability_event(
                    failure_class=klass,
                    severity=Severity.WARNING.value,
                    signature=sig,
                    occurrence_key=f"{sig}|{cur_end[:13]}",
                    source="audit:degradation_scan",
                    evidence_json={
                        "metric": metric,
                        "prior": prior[metric],
                        "current": cur[metric],
                    },
                )
            except Exception:  # noqa: BLE001 - idempotent insert
                pass
    return {"current": cur, "prior": prior, "findings": findings}


def _departments(run: _Run) -> dict[str, Any]:
    """Department manager reviews (origin ``department`` proposals).

    A distinct, independently-isolated stage from the CTO pass (codex #12): a crash in
    the department sweep must NOT prevent the CTO daily pass from running, and vice
    versa. Runs ONLY when a company-style ``(harness, prompt, **) -> AgentResult``
    adapter is supplied, which keeps the audit from ever falling through to a live CLI
    in tests while W10 passes the real company adapter in production.
    """
    deps = run.deps
    if deps.company_adapter_fn is None:
        return {"skipped": "no_company_adapter"}
    departments_fn = deps.departments_fn
    if departments_fn is None:
        from omniagentos.orgdims.company_departments import run_department_reviews

        departments_fn = run_department_reviews
    return {"departments": departments_fn(run.store, deps.company_adapter_fn)}


def _cto_daily(run: _Run) -> dict[str, Any]:
    """CTO daily pass (origin ``cto`` proposals) — isolated from the department sweep."""
    deps = run.deps
    if deps.company_adapter_fn is None:
        return {"skipped": "no_company_adapter"}
    cto_daily_fn = deps.cto_daily_fn
    if cto_daily_fn is None:
        from omniagentos.orgdims.company_cto import daily_review

        cto_daily_fn = daily_review
    return {"cto_daily": cto_daily_fn(run.store, deps.company_adapter_fn)}


def _grouped_notifications(run: _Run, open_events: int) -> dict[str, Any]:
    """One grouped info notice for the batch (criticals already alerted immediately)."""
    queued = len(
        run.store.list_improvements(status=ImprovementStatus.AWAITING_HUMAN.value, limit=200)
    )
    ok = _safe_notify(
        run,
        kind="info",
        title=f"{run.kind} audit: {open_events} open events, {queued} awaiting approval",
        severity="info",
        ref_type="reliability_audit",
        ref_id=run.audit_id,
        dedupe=False,
        payload={"open_events": open_events, "awaiting_human": queued},
    )
    return {"awaiting_human": queued, "notified": ok}


# --- Public entry points ----------------------------------------------------


def _run_watch(
    store: ReliabilityStore, deps: AuditDeps, audit_id: str | None = None
) -> dict[str, Any]:
    run = _begin(store, "watch", deps, window_hours=deps.window_hours or 1.0, audit_id=audit_id)
    high_water = run.window_end
    cursor = _load_watch_cursor(store)

    run.stage("detect", lambda: _detect_and_persist(run, cursor))
    run.stage("recover", lambda: _recover(run))
    run.stage("critical_alerts", lambda: _critical_alerts(run))
    run.stage("auto_close", lambda: _auto_close_events(run))
    run.stage("monitoring", lambda: _monitoring_checks(run))
    run.stage("reclaim", lambda: _reclaim_stale(run))

    # Advance the cursor ONLY on a clean window; otherwise hold + alert (codex #3).
    _finalize_watch_cursor(run, cursor, high_water)

    open_events = store.list_events(status=EventStatus.OPEN.value, limit=1000)
    note = _report(run, len(open_events), open_events)
    return _finish(run, len(open_events), note)


def _auto_close_events(run: _Run) -> dict[str, Any]:
    """F2: close open events whose signature has been quiet for N hours."""
    hours = float(os.environ.get("OMNIAGENTOS_RELIABILITY_AUTO_CLOSE_HOURS", "24"))
    if not hasattr(run.store, "auto_close_stale_events"):
        return {"skipped": "no_auto_close"}
    try:
        return run.store.auto_close_stale_events(quiet_hours=hours)
    except Exception as exc:  # noqa: BLE001
        run.errors.append({"stage": "auto_close", "error": str(exc)})
        return {"error": str(exc)}


def watch(
    store: ReliabilityStore, once: bool = False, audit_id: str | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Detect → recover → alert → monitor → reclaim, every 600 s (§11)."""
    deps = _hydrate_production_deps(store, AuditDeps.from_kwargs(kwargs))
    result = _run_watch(store, deps, audit_id=audit_id)
    if not once:
        while True:  # pragma: no cover - the scheduling loop is launchd's job in prod
            time.sleep(_WATCH_INTERVAL_SECONDS)
            try:
                _run_watch(store, deps)
            except Exception:  # noqa: BLE001 - one failed cycle never kills the loop
                continue
    return result


def _run_twice_daily(
    store: ReliabilityStore, deps: AuditDeps, audit_id: str | None = None
) -> dict[str, Any]:
    hours = deps.window_hours or 12.0
    run = _begin(store, "twice_daily", deps, window_hours=hours, audit_id=audit_id)
    high_water = run.window_end
    cursor = _load_watch_cursor(store)

    run.stage("detect", lambda: _detect_and_persist(run, cursor))
    run.stage("recover", lambda: _recover(run))
    run.stage("degradation", lambda: _degradation_scan(run))
    run.stage("critical_alerts", lambda: _critical_alerts(run))
    # Two independently-isolated stages (codex #12): one crashing never skips the other.
    run.stage("departments", lambda: _departments(run))
    run.stage("cto_daily", lambda: _cto_daily(run))
    run.stage("propose_decide", lambda: _propose_and_decide(run))
    run.stage("monitoring", lambda: _monitoring_checks(run))
    run.stage("reclaim", lambda: _reclaim_stale(run))

    # Advance the shared watch cursor ONLY on a clean window; otherwise hold + alert.
    _finalize_watch_cursor(run, cursor, high_water)

    open_events = store.list_events(status=EventStatus.OPEN.value, limit=1000)
    run.stage("notify", lambda: _grouped_notifications(run, len(open_events)))
    note = _report(run, len(open_events), open_events)
    return _finish(run, len(open_events), note)


def twice_daily(
    store: ReliabilityStore, once: bool = False, audit_id: str | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Full sweep + degradation + proposals → sandbox → judges → route → report (§11)."""
    deps = _hydrate_production_deps(store, AuditDeps.from_kwargs(kwargs))
    result = _run_twice_daily(store, deps, audit_id=audit_id)
    if not once:
        while True:  # pragma: no cover - launchd owns the cadence in prod
            time.sleep(_WATCH_INTERVAL_SECONDS)
            try:
                _run_twice_daily(store, deps)
            except Exception:  # noqa: BLE001
                continue
    return result


def daily_summary(
    store: ReliabilityStore, once: bool = False, audit_id: str | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Consolidated daily improvement summary (info notice + vault section, §11)."""
    deps = AuditDeps.from_kwargs(kwargs)
    run = _begin(
        store, "daily_summary", deps, window_hours=deps.window_hours or 24.0, audit_id=audit_id
    )

    def _summarize() -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for imp in store.list_improvements(limit=500):
            by_status[imp.status] = by_status.get(imp.status, 0) + 1
        return {"by_status": by_status, "total": sum(by_status.values())}

    summary = run.stage("summarize", _summarize) or {"by_status": {}, "total": 0}
    run.stage(
        "notify",
        lambda: {
            "notified": _safe_notify(
                run,
                kind="info",
                title=f"Daily improvement summary: {summary['total']} improvements",
                severity="info",
                ref_type="reliability_audit",
                ref_id=run.audit_id,
                dedupe=False,
                payload=summary,
            )
        },
    )
    findings = int(summary.get("total", 0))
    note = _report(run, findings, [])
    return _finish(run, findings, note)


def weekly_architecture(
    store: ReliabilityStore, once: bool = False, audit_id: str | None = None, **kwargs: Any
) -> dict[str, Any]:
    """CTO deep review + scorecard trends + archdocs staleness → doc_stale (§11)."""
    deps = AuditDeps.from_kwargs(kwargs)
    run = _begin(
        store,
        "weekly_architecture",
        deps,
        window_hours=deps.window_hours or 168.0,
        audit_id=audit_id,
    )

    if deps.company_adapter_fn is not None:
        cto_weekly_fn = deps.cto_weekly_fn
        if cto_weekly_fn is None:
            from omniagentos.orgdims.company_cto import weekly_review

            cto_weekly_fn = weekly_review
        run.stage(
            "cto_weekly",
            lambda: {
                "result": cto_weekly_fn(store, deps.company_adapter_fn, vault_dir=deps.vault_dir)
            },
        )

    run.stage("scorecards", lambda: {"week": len(store.list_scorecards(window="week", limit=200))})
    run.stage("staleness", lambda: _staleness_check(run))

    open_events = store.list_events(status=EventStatus.OPEN.value, limit=1000)
    note = _report(run, len(open_events), open_events)
    return _finish(run, len(open_events), note)


def _staleness_check(run: _Run) -> dict[str, Any]:
    """Archdocs staleness ⇒ a ``doc_stale`` event (⇒ later L1 docs-refresh improvement)."""
    staleness_fn = run.deps.staleness_fn
    if staleness_fn is None:
        try:
            from omniagentos.archdocs.staleness import check_staleness  # type: ignore

            staleness_fn = check_staleness
        except Exception:  # noqa: BLE001 - archdocs may not be present
            return {"skipped": "no_staleness_fn"}
    report = staleness_fn()
    stale = bool(
        getattr(report, "stale", None) if not isinstance(report, dict) else report.get("stale")
    )
    if stale:
        sig = "doc_stale|archdocs"
        try:
            run.store.insert_reliability_event(
                failure_class=FailureClass.DOC_STALE.value,
                severity=Severity.INFO.value,
                signature=sig,
                occurrence_key=f"{sig}|{run.window_end[:10]}",
                source="audit:staleness",
                evidence_json={"report": report if isinstance(report, dict) else str(report)},
            )
        except Exception:  # noqa: BLE001
            pass
    return {"stale": stale}


# Legacy alias retained for callers/tests that used the old flat entry point.
def run_audit(store: ReliabilityStore, kind: str, **kwargs: Any) -> dict[str, Any]:
    dispatch = {
        "watch": watch,
        "twice_daily": twice_daily,
        "daily_summary": daily_summary,
        "weekly_architecture": weekly_architecture,
    }
    fn = dispatch.get(kind, twice_daily)
    kwargs.setdefault("once", True)
    return fn(store, **kwargs)


__all__ = [
    "AuditDeps",
    "watch",
    "twice_daily",
    "daily_summary",
    "weekly_architecture",
    "run_audit",
]
