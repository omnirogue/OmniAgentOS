"""Cognitive Budget Manager — LIVE progressive escalation (no cost objective)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from omniagentos.cognitive_txn import atomic, discarded
from omniagentos.contracts import default_db_path, new_id, utc_now_iso
from omniagentos.db.migrate import migrate_connection
from omniagentos.db.store import _connect, _row, _rows

# Escalation ladder (plan §7 / table 2)
RUNGS: list[dict[str, Any]] = [
    {
        "rung": 0,
        "name": "mechanical",
        "model_role": "none",
        "reasoning_effort": "minimal",
        "parallel_candidates": 1,
        "verification_rung": "mechanical",
    },
    {
        "rung": 1,
        "name": "fast",
        "model_role": "fast_implementer",
        "reasoning_effort": "low",
        "parallel_candidates": 1,
        "verification_rung": "mechanical_plus_independent",
    },
    {
        "rung": 2,
        "name": "strengthened",
        "model_role": "fast_implementer",
        "reasoning_effort": "high",
        "parallel_candidates": 1,
        "verification_rung": "mechanical_plus_independent",
        "max_repairs": 1,
    },
    {
        "rung": 3,
        "name": "diverse",
        "model_role": "standard_implementer",
        "reasoning_effort": "medium",
        "parallel_candidates": 2,
        "verification_rung": "single_reviewer",
    },
    {
        "rung": 4,
        "name": "specialist",
        "model_role": "specialist",
        "reasoning_effort": "high",
        "parallel_candidates": 1,
        "verification_rung": "specialist_plus_verifier",
    },
    {
        "rung": 5,
        "name": "expert_swarm",
        "model_role": "strong_planner",
        "reasoning_effort": "xhigh",
        "parallel_candidates": 3,
        "verification_rung": "panel",
    },
    {
        "rung": 6,
        "name": "break_glass",
        "model_role": "human_exception",
        "reasoning_effort": "high",
        "parallel_candidates": 1,
        "verification_rung": "human_gate",
    },
]

DEFAULT_ESCALATION_PATH = [
    "increase_effort",
    "switch_model_family",
    "add_parallel_candidate",
    "specialist_review",
    "expert_panel",
]

# Role → preferred provider lineage for multi-model routing hints
ROLE_PROVIDER_HINTS: dict[str, list[str]] = {
    "fast_implementer": ["codex", "gemini", "kimi", "claude"],
    "standard_implementer": ["claude", "codex", "grok"],
    "specialist": ["claude", "grok"],
    "strong_planner": ["claude", "grok"],
    # Reached via graph_runtime NodeSpec.model_role, not via RUNGS (do not delete)
    "verifier": ["grok", "claude", "gemini"],
    "strong_synthesizer": ["claude", "grok"],
    "none": [],
    "human_exception": [],
}


def provider_hints_for_model_role(model_role: str | None) -> list[str]:
    """Return a new list of preferred provider routing hints for a model role.

    Always returns a new list to avoid mutating the shared configuration map.
    """
    if not model_role:
        return []
    return list(ROLE_PROVIDER_HINTS.get(model_role, []))


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return default


# --- M-47: CBM-owned durable close outbox -------------------------------------
#
# This table is owned by the CBM service, not by a shared numbered migration:
# the close outbox is an implementation detail of ``close_allocation`` delivery,
# and claiming a global migration number here would collide with other lanes
# that own `omniagentos/db/migrations/**`.

_OUTBOX_TABLE = "cbm_close_outbox"

_OUTBOX_DDL = f"""
CREATE TABLE IF NOT EXISTS {_OUTBOX_TABLE} (
    allocation_id              TEXT PRIMARY KEY,
    outcome_id                 TEXT NOT NULL,
    decision_payload_json      TEXT NOT NULL,
    outcome_payload_json       TEXT NOT NULL,
    log_decision_at            TEXT,
    attach_outcome_at          TEXT,
    log_decision_claim_owner   TEXT,
    log_decision_claimed_at    TEXT,
    attach_outcome_claim_owner TEXT,
    attach_outcome_claimed_at  TEXT,
    attempts                   INTEGER NOT NULL DEFAULT 0,
    last_error                 TEXT,
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL
)
"""

# Ordered learning emissions required before a close may report success.
LEARNING_STEPS: tuple[str, ...] = ("log_decision", "attach_outcome")

# Each step's delivery progress is a separate durable column, so a failure in a
# later step never causes an earlier step to be repeated on a normal retry.
_STEP_COLUMNS: dict[str, str] = {
    "log_decision": "log_decision_at",
    "attach_outcome": "attach_outcome_at",
}

# Mappings for claim owner and timestamp columns per step
_CLAIM_OWNER_COLUMNS: dict[str, str] = {
    "log_decision": "log_decision_claim_owner",
    "attach_outcome": "attach_outcome_claim_owner",
}

_CLAIMED_AT_COLUMNS: dict[str, str] = {
    "log_decision": "log_decision_claimed_at",
    "attach_outcome": "attach_outcome_claimed_at",
}

_OUTBOX_COLUMNS: tuple[str, ...] = (
    "allocation_id",
    "outcome_id",
    "decision_payload_json",
    "outcome_payload_json",
    "log_decision_at",
    "attach_outcome_at",
    "log_decision_claim_owner",
    "log_decision_claimed_at",
    "attach_outcome_claim_owner",
    "attach_outcome_claimed_at",
    "attempts",
    "last_error",
    "created_at",
    "updated_at",
)


def _is_claim_stale(claimed_at_iso: str | None, stale_seconds: float = 30.0) -> bool:
    if not claimed_at_iso:
        return True
    try:
        s = str(claimed_at_iso).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        return (now - dt).total_seconds() >= stale_seconds
    except Exception:
        return True


# --- M-47: canonical durable learning sink ------------------------------------
#
# ``omniagentos.learning.api`` defaults to a process-global in-memory list. An
# outbox step marked delivered against that list is a false durability claim:
# the row is gone at process exit while ``log_decision_at`` stays set, so the
# event is never re-emitted. The sink below is a real file whose identity is
# derived from the runtime database, so a fresh service over the same database
# resolves the same file and the delivered/pending split survives a restart.

_LEARNING_SINK_ENV = "OMNIAGENTOS_CBM_LEARNING_LOG"
_LEARNING_SINK_NAME = "cbm_close_learning.jsonl"


def _database_file(connection: sqlite3.Connection) -> Path | None:
    """Path of the connection's main database, or ``None`` for in-memory."""
    try:
        rows = connection.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        name = row[1] if not isinstance(row, sqlite3.Row) else row["name"]
        filename = row[2] if not isinstance(row, sqlite3.Row) else row["file"]
        if str(name) == "main" and filename:
            return Path(str(filename))
    return None


def _resolve_learning_sink(connection: sqlite3.Connection) -> Path:
    """Canonical durable sink for this service's runtime.

    Resolution is deterministic and depends only on durable inputs, so a
    restarted service appends to the same file rather than starting a new log.
    """
    override = os.environ.get(_LEARNING_SINK_ENV)
    if override:
        return Path(override)
    database = _database_file(connection)
    root = database.parent if database is not None else Path(default_db_path()).parent
    return root / "learning" / _LEARNING_SINK_NAME


def _fsync_sink(path: Path) -> None:
    """Force the appended learning bytes to stable storage.

    ``learning.api._record`` closes the file but never fsyncs, so the record is
    only in the page cache when it returns. Marking the outbox step delivered at
    that point lets the durable marker outrun the durable bytes. Any failure
    propagates: the step stays pending rather than being claimed as delivered.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    # The directory entry matters only the first time the file is created; a
    # platform that cannot fsync a directory (Windows) is not a delivery
    # failure, so this stays best-effort while the file fsync above does not.
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


class CbmCloseRetryableError(RuntimeError):
    """A close did not fully succeed and is safe/expected to retry (M-47).

    Never returned as ``{"closed": True}`` — callers must treat this as an
    explicit failure, not success. The attributes describe exactly how far the
    close actually got:

    - ``durable_closed`` — ``False`` when nothing was committed (the outcome,
      allocation closure, role stats and outbox row all rolled back). ``True``
      when the business close is durable but learning emission is incomplete.
    - ``learning_pending`` — the learning steps still undelivered, in order.
    - ``outcome_id`` — the durable outcome id when one exists, so a retry
      reconstructs rather than fabricates.
    """

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        allocation_id: str | None = None,
        durable_closed: bool = False,
        learning_pending: Any = (),
        outcome_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.allocation_id = allocation_id
        self.durable_closed = bool(durable_closed)
        self.learning_pending = [str(s) for s in learning_pending]
        self.outcome_id = outcome_id


class CognitiveBudgetService:
    """Authoritative allocation + escalation. LIVE by default."""

    def __init__(self, database: str | Path | sqlite3.Connection | None = None) -> None:
        self._lock = RLock()
        if isinstance(database, sqlite3.Connection):
            self._connection = database
            self._connection.row_factory = sqlite3.Row
            self._owns = False
            migrate_connection(self._connection)
        else:
            self._connection = _connect(str(database or default_db_path()))
            self._owns = True
            migrate_connection(self._connection)
        self._learning_sink = _resolve_learning_sink(self._connection)
        self._ensure_close_outbox()

    @property
    def learning_sink(self) -> Path:
        """Durable file every close learning emission is appended to (M-47)."""
        return self._learning_sink

    def _ensure_close_outbox(self) -> None:
        """Create and validate the CBM-owned durable close outbox (M-47).

        Fails closed: if a ``cbm_close_outbox`` table already exists without the
        exact columns this service depends on, closing must not proceed with a
        silently degraded delivery record.
        """
        self._connection.execute(_OUTBOX_DDL)
        present = {
            str(r["name"])
            for r in self._connection.execute(
                f"PRAGMA table_info({_OUTBOX_TABLE})"  # noqa: S608 - fixed literal
            ).fetchall()
        }
        claim_cols = [
            ("log_decision_claim_owner", "TEXT"),
            ("log_decision_claimed_at", "TEXT"),
            ("attach_outcome_claim_owner", "TEXT"),
            ("attach_outcome_claimed_at", "TEXT"),
        ]
        for col_name, col_type in claim_cols:
            if col_name not in present:
                self._connection.execute(
                    f"ALTER TABLE {_OUTBOX_TABLE} ADD COLUMN {col_name} {col_type}"  # noqa: S608
                )
                present.add(col_name)

        missing = [c for c in _OUTBOX_COLUMNS if c not in present]
        if missing:
            raise RuntimeError(
                f"{_OUTBOX_TABLE} schema mismatch; missing columns: {', '.join(missing)}"
            )

    def close(self) -> None:
        if self._owns:
            self._connection.close()
            self._owns = False

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "live": True,
            "rungs": len(RUNGS),
            "default_start_rung": 1,
            "objective": "quality+ETAR",
            "cost_objective": False,
            "role_provider_hints": ROLE_PROVIDER_HINTS,
        }

    def allocate(
        self,
        *,
        task_id: str | None = None,
        stage: str = "execution",
        required_quality: float = 0.9,
        risk_class: str = "reversible_internal",
        novelty: str = "low",
        difficulty: str = "medium",
        fingerprint: dict[str, Any] | None = None,
        force_rung: int | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """START FAST unless risk/novelty forces a higher rung.

        ``commit`` is accepted for compatibility and no longer calls
        ``Connection.commit()``. The allocation is a single INSERT, which is
        already atomic; and an explicit commit here would have committed an
        *enclosing* scope, which is precisely what ``recommend_rung``'s
        discard-only probe must never do (M-40).
        """
        rung = 1  # fast-first
        if force_rung is not None:
            rung = max(0, min(6, force_rung))
        else:
            if risk_class in {"irreversible", "bounded_external"} and novelty == "high":
                rung = 4
            elif risk_class == "irreversible":
                rung = 3
            elif novelty == "high" or difficulty == "high":
                rung = 2
            elif stage in {"planning", "synthesis"} and difficulty != "low":
                rung = 2
            elif stage == "verification":
                rung = 0  # mechanical first

        base = dict(RUNGS[rung])
        # Production evidence priors from role stats
        role = str(base["model_role"])
        stats = self._role_stats(role)
        predicted_quality = 0.92
        predicted_etar = 120.0
        if stats and stats["samples"] >= 3:
            predicted_quality = stats["accepts"] / max(1, stats["samples"])
            predicted_etar = stats["sum_wall_s"] / max(1, stats["samples"])

        rationale = [
            f"start_rung={rung} ({base['name']})",
            f"stage={stage}",
            f"risk={risk_class}",
            f"novelty={novelty}",
            "policy=fast_first_verify_escalate",
        ]
        if stats and stats["samples"]:
            rationale.append(f"role_samples={stats['samples']} accept_rate={predicted_quality:.2f}")

        now = utc_now_iso()
        alloc_id = new_id("cba")
        fp = fingerprint or {
            "risk": risk_class,
            "novelty": novelty,
            "difficulty": difficulty,
            "stage": stage,
        }
        row = {
            "id": alloc_id,
            "task_id": task_id,
            "stage": stage,
            "required_quality": required_quality,
            "model_role": role,
            "reasoning_effort": base["reasoning_effort"],
            "parallel_candidates": int(base.get("parallel_candidates") or 1),
            "context_mode": "targeted",
            "verification_rung": base["verification_rung"],
            "max_repairs": int(base.get("max_repairs") or 1),
            "max_replans": 1,
            "hedge_after_p95_seconds": 180 if rung <= 2 else 300,
            "escalation_path": list(DEFAULT_ESCALATION_PATH),
            "stop_rule": "acceptance_gate_passed",
            "evidence_required": ["tests", "diff_scope", "reviewer_report"]
            if risk_class != "read_only"
            else ["tests"],
            "rationale": rationale,
            "predicted_quality": predicted_quality,
            "predicted_etar_s": predicted_etar,
            "rung": rung,
            "status": "active",
            "fingerprint": fp,
            "provider_hints": provider_hints_for_model_role(role),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._connection.execute(
                "INSERT INTO cbm_allocations "
                "(id, task_id, stage, required_quality, model_role, reasoning_effort, "
                "parallel_candidates, context_mode, verification_rung, max_repairs, "
                "max_replans, hedge_after_p95_seconds, escalation_path_json, stop_rule, "
                "evidence_required_json, rationale_json, predicted_quality, predicted_etar_s, "
                "rung, status, fingerprint_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    alloc_id,
                    task_id,
                    stage,
                    required_quality,
                    role,
                    row["reasoning_effort"],
                    row["parallel_candidates"],
                    row["context_mode"],
                    row["verification_rung"],
                    row["max_repairs"],
                    row["max_replans"],
                    row["hedge_after_p95_seconds"],
                    _j(row["escalation_path"]),
                    row["stop_rule"],
                    _j(row["evidence_required"]),
                    _j(rationale),
                    predicted_quality,
                    predicted_etar,
                    rung,
                    "active",
                    _j(fp),
                    now,
                    now,
                ),
            )
        return row

    def escalate(
        self,
        allocation_id: str,
        *,
        trigger_code: str,
        evidence: list[str] | None = None,
        next_gate: str | None = None,
    ) -> dict[str, Any]:
        """Material escalation — never identical retry."""
        with self._lock:
            row = _row(
                self._connection.execute(
                    "SELECT * FROM cbm_allocations WHERE id = ?", (allocation_id,)
                ).fetchone()
            )
        if row is None:
            raise KeyError(allocation_id)
        status = str(row.get("status") or "")
        if status in {"closed", "contracted"}:
            raise ValueError(f"cannot escalate {status} allocation")
        from_rung = int(row["rung"])
        to_rung = min(6, from_rung + 1)
        if to_rung == from_rung:
            raise ValueError("already at break-glass rung")
        # Map trigger → preferred step
        if trigger_code in {"repeated_failure", "identical_error"}:
            to_rung = min(6, max(to_rung, 3))  # force diverse
        if trigger_code in {"high_risk", "irreversible"}:
            to_rung = min(6, max(to_rung, 4))
        if trigger_code in {"novelty", "no_evaluator"}:
            to_rung = min(6, max(to_rung, 5))

        base = dict(RUNGS[to_rung])
        now = utc_now_iso()
        esc_id = new_id("cbe")
        changed = {
            "model_role": base["model_role"],
            "reasoning_effort": base["reasoning_effort"],
            "parallel_candidates": base["parallel_candidates"],
            "verification_rung": base["verification_rung"],
            "rung": to_rung,
        }
        # M-40: the escalation audit row and the allocation mutation are one
        # write. As two autocommit statements a failure between them either
        # recorded an escalation that never took effect, or moved the
        # allocation to a new rung with no audit trail explaining why.
        with self._lock, atomic(self._connection, "cbm_escalate"):
            self._connection.execute(
                "INSERT INTO cbm_escalations "
                "(id, allocation_id, task_id, from_rung, to_rung, trigger_code, "
                "evidence_json, changed_fields_json, next_gate, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    esc_id,
                    allocation_id,
                    row.get("task_id"),
                    from_rung,
                    to_rung,
                    trigger_code,
                    _j(evidence or []),
                    _j(changed),
                    next_gate or "re_verify",
                    now,
                ),
            )
            self._connection.execute(
                "UPDATE cbm_allocations SET rung=?, model_role=?, reasoning_effort=?, "
                "parallel_candidates=?, verification_rung=?, status='escalated', "
                "updated_at=? WHERE id=?",
                (
                    to_rung,
                    base["model_role"],
                    base["reasoning_effort"],
                    base["parallel_candidates"],
                    base["verification_rung"],
                    now,
                    allocation_id,
                ),
            )
        return {
            "escalation_id": esc_id,
            "allocation_id": allocation_id,
            "from_rung": from_rung,
            "to_rung": to_rung,
            "trigger_code": trigger_code,
            "changed": changed,
            "provider_hints": provider_hints_for_model_role(str(base["model_role"])),
            "next_gate": next_gate or "re_verify",
        }

    def contract(self, allocation_id: str, *, reason: str = "gate_passed") -> dict[str, Any]:
        """Cancel redundant work / release strong models after acceptance."""
        now = utc_now_iso()
        # M-40: the status guard and the mutation share one transaction, so the
        # allocation cannot be closed by another writer between the check and
        # the update. BEGIN IMMEDIATE takes the write lock before the read, so
        # this never fails a read-to-write upgrade.
        with self._lock, atomic(self._connection, "cbm_contract"):
            row = _row(
                self._connection.execute(
                    "SELECT * FROM cbm_allocations WHERE id = ?", (allocation_id,)
                ).fetchone()
            )
            if row is None:
                raise KeyError(allocation_id)
            if str(row.get("status") or "") == "closed":
                raise ValueError("cannot contract a closed allocation")
            self._connection.execute(
                "UPDATE cbm_allocations SET status='contracted', "
                "parallel_candidates=1, updated_at=? WHERE id=?",
                (now, allocation_id),
            )
        return {
            "allocation_id": allocation_id,
            "status": "contracted",
            "reason": reason,
            "action": "cancel_redundant_candidates_release_verifiers",
        }

    def decide_gate(
        self,
        allocation_id: str,
        *,
        passed: bool,
        measured_quality: float | None = None,
        trigger_code: str = "gate_failure",
        evidence: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fast-first control: contract on accepted quality; escalate only on evidence.

        Stops immediately once measured quality clears the required threshold.
        At break-glass with a failed gate, returns UNKNOWN rather than looping.
        """
        row = self.get_allocation(allocation_id)
        if row is None:
            raise KeyError(allocation_id)
        if row.get("status") in {"closed", "contracted"}:
            return {
                "allocation_id": allocation_id,
                "decision": "noop",
                "status": row.get("status"),
                "reason": "already_terminal",
            }
        required = float(row.get("required_quality") or 0.9)
        quality = (
            float(measured_quality) if measured_quality is not None else (1.0 if passed else 0.0)
        )
        if passed and quality >= required:
            out = self.contract(allocation_id, reason="acceptance_gate_passed")
            return {
                "allocation_id": allocation_id,
                "decision": "contract",
                "reason": "quality_threshold_met",
                "measured_quality": quality,
                "required_quality": required,
                **out,
            }
        # Failed gate or quality below threshold
        if int(row.get("rung") or 0) >= 6:
            now = utc_now_iso()
            with self._lock:
                self._connection.execute(
                    "UPDATE cbm_allocations SET status='closed', updated_at=? WHERE id=?",
                    (now, allocation_id),
                )
            return {
                "allocation_id": allocation_id,
                "decision": "unknown",
                "reason": "break_glass_exhausted_no_accepted_candidate",
                "measured_quality": quality,
                "required_quality": required,
                "status": "closed",
            }
        esc = self.escalate(
            allocation_id,
            trigger_code=trigger_code if not passed else "quality_below_threshold",
            evidence=evidence
            or [
                f"passed={passed}",
                f"measured_quality={quality}",
                f"required_quality={required}",
            ],
            next_gate="re_verify",
        )
        return {
            "allocation_id": allocation_id,
            "decision": "escalate",
            "reason": "gate_failed_or_quality_short",
            "measured_quality": quality,
            "required_quality": required,
            **esc,
        }

    def recommend_rung(
        self,
        *,
        stage: str = "execution",
        required_quality: float = 0.9,
        risk_class: str = "reversible_internal",
        novelty: str = "low",
        difficulty: str = "medium",
    ) -> dict[str, Any]:
        """ETAR-aware start-rung recommendation (no allocation written).

        M-40: the probe allocation is written inside a scope that is always
        discarded, so recommending never leaves an ``active`` allocation nobody
        asked for. The scope nests as a savepoint when a caller already holds a
        transaction, so the discard rolls back the probe and nothing else.
        """
        with self._lock, discarded(self._connection, "cbm_recommend_probe"):
            probe = self.allocate(
                task_id=None,
                stage=stage,
                required_quality=required_quality,
                risk_class=risk_class,
                novelty=novelty,
                difficulty=difficulty,
                commit=False,
            )
            predicted = float(probe.get("predicted_quality") or 0.92)
            rung = int(probe["rung"])
            if predicted < required_quality and rung < 6:
                rung = min(6, rung + 1)

        baseline = {
            "recommended_rung": rung,
            "rung_name": RUNGS[rung]["name"],
            "predicted_quality": predicted,
            "predicted_etar_s": probe.get("predicted_etar_s"),
            "required_quality": required_quality,
            "etar_adjusted": predicted < required_quality,
            "model_role": RUNGS[rung]["model_role"],
            "reasoning_effort": RUNGS[rung]["reasoning_effort"],
            "parallel_candidates": RUNGS[rung].get("parallel_candidates", 1),
            "verification_rung": RUNGS[rung]["verification_rung"],
            "provider_hints": provider_hints_for_model_role(str(RUNGS[rung]["model_role"])),
        }
        # D1: optionally overlay promoted model_assignment champions (lazy lab.runtime).
        try:
            from omniagentos.cbm.champion_routing import apply_champion_to_recommendation

            return apply_champion_to_recommendation(
                baseline,
                task_class=stage,
            )
        except Exception:  # noqa: BLE001 — champion path must never break baseline
            return baseline

    def close_allocation(
        self,
        allocation_id: str,
        *,
        first_pass_accepted: bool,
        wall_seconds: float | None = None,
        repair_count: int = 0,
        verifier_score: float | None = None,
        production_outcome: str = "healthy",
    ) -> dict[str, Any]:
        """Close an allocation: outcome + role stats + learning emission (M-47).

        A close is only reported as ``closed: True`` once the durable business
        close *and* every required learning emission have succeeded.

        Durability model:

        - The outcome row, the allocation status flip, the role-stat mutation
          and the learning outbox row (payloads + per-step delivery state) are
          written in one explicit transaction. The connection runs in autocommit
          mode, so ``commit()``/``rollback()`` alone would *not* have been
          atomic; the explicit scope is what makes it all-or-nothing.
        - Learning emissions go to a canonical durable file
          (``learning_sink``), derived from the runtime database, and are
          fsynced before the matching outbox step is marked delivered. The
          learning API's default in-memory log is not durable, so delivering
          there and marking the step done was a false durability claim: the
          record vanished with the process while the marker survived, and the
          event was never re-emitted.
        - If that transaction fails, nothing is committed and
          ``CbmCloseRetryableError(durable_closed=False)`` is raised.
        - If the business close commits but a learning emission fails, the close
          is *not* reported as successful: ``CbmCloseRetryableError`` is raised
          with ``durable_closed=True`` and the truthful ``learning_pending``
          list. Retrying reconstructs identifiers and payloads from the durable
          outbox, retries only the pending steps, and never writes a second
          outcome or role-stat mutation.

        Delivery guarantee — stated honestly: this is durable **at-least-once**,
        not exactly-once. ``omniagentos.learning.api`` exposes no idempotency
        key and no "already recorded" response, so the append to the sink and
        our durable delivery marker cannot be committed together. The exact
        crash window is: after the appended record is fsynced and before the
        corresponding ``UPDATE cbm_close_outbox SET <step>_at`` commits. A
        process death inside that window causes exactly that one step to be
        re-emitted on the next retry. Exactly-once here requires an idempotency
        key on the learning API; that is a cross-lane contract change and is
        recorded as a handoff rather than faked.
        """
        with self._lock:
            row = _row(
                self._connection.execute(
                    "SELECT * FROM cbm_allocations WHERE id = ?", (allocation_id,)
                ).fetchone()
            )
            if row is None:
                raise KeyError(allocation_id)
            record = self._resume_close(allocation_id, row)
            idempotent = record is not None
            if record is None:
                try:
                    record = self._durable_close(
                        allocation_id,
                        row,
                        first_pass_accepted=first_pass_accepted,
                        wall_seconds=wall_seconds,
                        repair_count=repair_count,
                        verifier_score=verifier_score,
                        production_outcome=production_outcome,
                    )
                except CbmCloseRetryableError:
                    record = self._resume_close(allocation_id, row)
                    if record is None:
                        raise
                    idempotent = True
            return self._deliver_close_learning(record, idempotent=idempotent)

    def _resume_close(self, allocation_id: str, row: dict[str, Any]) -> dict[str, Any] | None:
        """Return the durable close record to resume from, or ``None`` (M-47).

        ``None`` means no durable close exists yet and the business close must
        run. Any returned record is authoritative: its outcome id and learning
        payloads are reused verbatim so a retry never fabricates new ones.
        """
        outbox = _row(
            self._connection.execute(
                f"SELECT * FROM {_OUTBOX_TABLE} WHERE allocation_id = ?",  # noqa: S608
                (allocation_id,),
            ).fetchone()
        )
        if outbox is not None:
            return outbox
        if row.get("status") != "closed":
            return None
        outcome = _row(
            self._connection.execute(
                "SELECT * FROM cbm_outcomes WHERE allocation_id = ? "
                "ORDER BY created_at, id LIMIT 1",
                (allocation_id,),
            ).fetchone()
        )
        if outcome is None:
            # Closed with no outcome: a genuinely incomplete close. Recover by
            # running the business close, which is what the caller asked for.
            return None
        # Closed before this outbox existed. Reconstruct the delivery record
        # from durable state rather than inventing a new outcome id. Delivery
        # state is recorded as undelivered because it is genuinely unknown for
        # pre-outbox closes; at-least-once re-emission is the truthful default.
        return self._backfill_outbox(row, outcome)

    def _close_payloads(
        self,
        allocation_id: str,
        row: dict[str, Any],
        *,
        outcome_id: str,
        first_pass_accepted: bool,
        wall_seconds: float | None,
        repair_count: int,
        production_outcome: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        decision = {
            "decision_id": allocation_id,
            "kind": "cbm_allocation_close",
            "rung": row.get("rung"),
            "model_role": row.get("model_role"),
            "first_pass_accepted": bool(first_pass_accepted),
        }
        outcome = {
            "outcome_id": outcome_id,
            "first_pass_accepted": bool(first_pass_accepted),
            "wall_seconds": wall_seconds,
            "repair_count": repair_count,
            "production_outcome": production_outcome,
        }
        return decision, outcome

    def _backfill_outbox(self, row: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
        allocation_id = str(row["id"])
        outcome_id = str(outcome["id"])
        now = utc_now_iso()
        decision, outcome_payload = self._close_payloads(
            allocation_id,
            row,
            outcome_id=outcome_id,
            first_pass_accepted=bool(outcome.get("first_pass_accepted")),
            wall_seconds=outcome.get("wall_seconds"),
            repair_count=int(outcome.get("repair_count") or 0),
            production_outcome=str(outcome.get("production_outcome") or "healthy"),
        )
        with atomic(self._connection, "cbm_backfill_outbox"):
            self._connection.execute(
                f"INSERT INTO {_OUTBOX_TABLE} "  # noqa: S608
                "(allocation_id, outcome_id, decision_payload_json, outcome_payload_json, "
                "log_decision_at, attach_outcome_at, attempts, last_error, created_at, "
                "updated_at) VALUES (?,?,?,?,NULL,NULL,0,NULL,?,?)",
                (allocation_id, outcome_id, _j(decision), _j(outcome_payload), now, now),
            )
        return {
            "allocation_id": allocation_id,
            "outcome_id": outcome_id,
            "decision_payload_json": _j(decision),
            "outcome_payload_json": _j(outcome_payload),
            "log_decision_at": None,
            "attach_outcome_at": None,
        }

    def _durable_close(
        self,
        allocation_id: str,
        row: dict[str, Any],
        *,
        first_pass_accepted: bool,
        wall_seconds: float | None,
        repair_count: int,
        verifier_score: float | None,
        production_outcome: str,
    ) -> dict[str, Any]:
        """Commit outcome + closure + role stats + outbox atomically (M-47)."""
        esc_n = self._connection.execute(
            "SELECT COUNT(*) AS n FROM cbm_escalations WHERE allocation_id = ?",
            (allocation_id,),
        ).fetchone()
        escalations = int(esc_n["n"]) if esc_n else 0
        now = utc_now_iso()
        oid = new_id("cbo")
        decision_payload, outcome_payload = self._close_payloads(
            allocation_id,
            row,
            outcome_id=oid,
            first_pass_accepted=first_pass_accepted,
            wall_seconds=wall_seconds,
            repair_count=repair_count,
            production_outcome=production_outcome,
        )
        # The connection is in autocommit mode, so an explicit transaction —
        # not commit() — is what makes these four writes all-or-nothing.
        try:
            with atomic(self._connection, "cbm_close"):
                self._connection.execute(
                    "INSERT INTO cbm_outcomes "
                    "(id, allocation_id, task_id, fingerprint_json, model_role, "
                    "first_pass_accepted, wall_seconds, repair_count, verifier_score, "
                    "escalations, escaped_defect, production_outcome, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)",
                    (
                        oid,
                        allocation_id,
                        row.get("task_id"),
                        row.get("fingerprint_json") or "{}",
                        row.get("model_role"),
                        1 if first_pass_accepted else 0,
                        wall_seconds,
                        repair_count,
                        verifier_score,
                        escalations,
                        production_outcome,
                        now,
                    ),
                )
                self._connection.execute(
                    "UPDATE cbm_allocations SET status='closed', updated_at=? WHERE id=?",
                    (now, allocation_id),
                )
                # Update role leaderboard
                role = str(row.get("model_role") or "")
                self._connection.execute(
                    "INSERT INTO cbm_role_stats (role, fingerprint_key, samples, accepts, "
                    "sum_wall_s, sum_repairs, updated_at) VALUES (?,?,1,?,?,?,?) "
                    "ON CONFLICT(role, fingerprint_key) DO UPDATE SET "
                    "samples = samples + 1, "
                    "accepts = accepts + excluded.accepts, "
                    "sum_wall_s = sum_wall_s + excluded.sum_wall_s, "
                    "sum_repairs = sum_repairs + excluded.sum_repairs, "
                    "updated_at = excluded.updated_at",
                    (
                        role,
                        "",
                        1 if first_pass_accepted else 0,
                        float(wall_seconds or 0),
                        repair_count,
                        now,
                    ),
                )
                # Pending learning work becomes durable with the business close,
                # so a crash after commit still leaves an exact replayable record.
                self._connection.execute(
                    f"INSERT INTO {_OUTBOX_TABLE} "  # noqa: S608
                    "(allocation_id, outcome_id, decision_payload_json, "
                    "outcome_payload_json, log_decision_at, attach_outcome_at, attempts, "
                    "last_error, created_at, updated_at) "
                    "VALUES (?,?,?,?,NULL,NULL,0,NULL,?,?)",
                    (
                        allocation_id,
                        oid,
                        _j(decision_payload),
                        _j(outcome_payload),
                        now,
                        now,
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            raise CbmCloseRetryableError(
                f"close_allocation store failed for {allocation_id}; "
                f"nothing was committed, retryable: {exc}",
                allocation_id=allocation_id,
                durable_closed=False,
                learning_pending=LEARNING_STEPS,
            ) from exc
        return {
            "allocation_id": allocation_id,
            "outcome_id": oid,
            "decision_payload_json": _j(decision_payload),
            "outcome_payload_json": _j(outcome_payload),
            "log_decision_at": None,
            "attach_outcome_at": None,
        }

    def _claim_step(
        self, allocation_id: str, step: str, owner_id: str, stale_seconds: float = 30.0
    ) -> bool:
        """Atomically claim a step for emitting if not already delivered or actively claimed (M-47)."""
        delivered_col = _STEP_COLUMNS[step]
        owner_col = _CLAIM_OWNER_COLUMNS[step]
        claimed_at_col = _CLAIMED_AT_COLUMNS[step]
        now = utc_now_iso()

        with atomic(self._connection, f"cbm_claim_{step}"):
            row = _row(
                self._connection.execute(
                    f"SELECT {delivered_col}, {owner_col}, {claimed_at_col} "  # noqa: S608
                    f"FROM {_OUTBOX_TABLE} WHERE allocation_id = ?",
                    (allocation_id,),
                ).fetchone()
            )
            if row is None or row.get(delivered_col) is not None:
                return False

            cur_owner = row.get(owner_col)
            cur_claimed_at = row.get(claimed_at_col)

            if cur_owner == owner_id:
                self._connection.execute(
                    f"UPDATE {_OUTBOX_TABLE} SET {claimed_at_col} = ?, updated_at = ? "  # noqa: S608
                    f"WHERE allocation_id = ? AND {owner_col} = ?",
                    (now, now, allocation_id, owner_id),
                )
                return True

            if cur_owner is not None and not _is_claim_stale(cur_claimed_at, stale_seconds):
                return False

            cur = self._connection.execute(
                f"UPDATE {_OUTBOX_TABLE} "  # noqa: S608
                f"SET {owner_col} = ?, {claimed_at_col} = ?, updated_at = ? "
                f"WHERE allocation_id = ? AND {delivered_col} IS NULL AND "
                f"({owner_col} IS NULL OR {owner_col} = ? OR {claimed_at_col} IS NULL OR {claimed_at_col} = ?)",
                (owner_id, now, now, allocation_id, cur_owner, cur_claimed_at),
            )
            return cur.rowcount > 0

    def _unclaim_step(self, allocation_id: str, step: str, owner_id: str) -> None:
        """Release a claim after a transient emission failure so retries proceed immediately (M-47)."""
        owner_col = _CLAIM_OWNER_COLUMNS[step]
        claimed_at_col = _CLAIMED_AT_COLUMNS[step]
        now = utc_now_iso()
        try:
            with atomic(self._connection, f"cbm_unclaim_{step}"):
                self._connection.execute(
                    f"UPDATE {_OUTBOX_TABLE} SET {owner_col} = NULL, {claimed_at_col} = NULL, "  # noqa: S608
                    f"updated_at = ? WHERE allocation_id = ? AND {owner_col} = ?",
                    (now, allocation_id, owner_id),
                )
        except sqlite3.Error:
            pass

    def _deliver_close_learning(
        self, record: dict[str, Any], *, idempotent: bool
    ) -> dict[str, Any]:
        """Drive pending learning emissions using claim-before-emit (M-47).

        Raises ``CbmCloseRetryableError`` instead of swallowing a failure, so a
        close with incomplete learning is never reported as success.

        Only the process that successfully claims a step is permitted to append
        and fsync to the canonical learning sink. Concurrent processes seeing an
        active claim wait or yield without duplicating emissions. Stale claims from
        crashed processes are reclaimed after timeout.
        """
        allocation_id = str(record["allocation_id"])
        outcome_id = str(record["outcome_id"])
        owner_id = f"cbm_{os.getpid()}_{new_id('claim')}"
        stale_seconds = float(os.environ.get("OMNIAGENTOS_CBM_STALE_CLAIM_TIMEOUT_S", "30.0"))

        from omniagentos.learning.api import attach_outcome, log_decision

        sink = self._learning_sink
        decision_payload = _loads(record.get("decision_payload_json"), {})
        outcome_payload = _loads(record.get("outcome_payload_json"), {})

        for _attempt in range(100):
            with self._lock:
                cur_row = _row(
                    self._connection.execute(
                        f"SELECT * FROM {_OUTBOX_TABLE} WHERE allocation_id = ?",  # noqa: S608
                        (allocation_id,),
                    ).fetchone()
                )
            if cur_row is None:
                break

            pending = [s for s in LEARNING_STEPS if not cur_row.get(_STEP_COLUMNS[s])]
            if not pending:
                break

            step = pending[0]
            with self._lock:
                claimed = self._claim_step(
                    allocation_id, step, owner_id, stale_seconds=stale_seconds
                )

            if not claimed:
                time.sleep(0.05)
                continue

            try:
                if step == "log_decision":
                    log_decision(decision_payload, path=sink)
                else:
                    attach_outcome(allocation_id, outcome_payload, path=sink)
                _fsync_sink(sink)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._unclaim_step(allocation_id, step, owner_id)
                    self._record_delivery_error(allocation_id, step, exc)
                with self._lock:
                    fresh_row = _row(
                        self._connection.execute(
                            f"SELECT * FROM {_OUTBOX_TABLE} WHERE allocation_id = ?",  # noqa: S608
                            (allocation_id,),
                        ).fetchone()
                    )
                remaining_pending = (
                    [s for s in LEARNING_STEPS if not fresh_row.get(_STEP_COLUMNS[s])]
                    if fresh_row
                    else pending
                )
                raise CbmCloseRetryableError(
                    f"cbm close for {allocation_id} is durably closed but learning "
                    f"emission {step!r} failed; retry completes only the pending "
                    f"steps: {exc}",
                    allocation_id=allocation_id,
                    outcome_id=outcome_id,
                    durable_closed=True,
                    learning_pending=remaining_pending,
                ) from exc

            with self._lock:
                marked = self._mark_delivered(allocation_id, step, owner_id)
                if not marked:
                    self._unclaim_step(allocation_id, step, owner_id)
                    fresh_row = _row(
                        self._connection.execute(
                            f"SELECT * FROM {_OUTBOX_TABLE} WHERE allocation_id = ?",  # noqa: S608
                            (allocation_id,),
                        ).fetchone()
                    )
                    remaining_pending = (
                        [s for s in LEARNING_STEPS if not fresh_row.get(_STEP_COLUMNS[s])]
                        if fresh_row
                        else list(LEARNING_STEPS)
                    )
                    raise CbmCloseRetryableError(
                        f"cbm close for {allocation_id} learning emission {step!r} completed but "
                        f"marking delivered failed (claim owner mismatch or step already updated)",
                        allocation_id=allocation_id,
                        outcome_id=outcome_id,
                        durable_closed=True,
                        learning_pending=remaining_pending,
                    )

        with self._lock:
            final_row = _row(
                self._connection.execute(
                    f"SELECT * FROM {_OUTBOX_TABLE} WHERE allocation_id = ?",  # noqa: S608
                    (allocation_id,),
                ).fetchone()
            )

        if final_row is None:
            raise CbmCloseRetryableError(
                f"cbm close for {allocation_id} is durably closed but outbox row is missing",
                allocation_id=allocation_id,
                outcome_id=outcome_id,
                durable_closed=True,
                learning_pending=list(LEARNING_STEPS),
            )

        final_pending = [s for s in LEARNING_STEPS if not final_row.get(_STEP_COLUMNS[s])]
        if final_pending:
            raise CbmCloseRetryableError(
                f"cbm close for {allocation_id} is durably closed but learning delivery "
                f"is incomplete; pending steps: {final_pending}",
                allocation_id=allocation_id,
                outcome_id=outcome_id,
                durable_closed=True,
                learning_pending=final_pending,
            )

        result = {
            "outcome_id": outcome_id,
            "allocation_id": allocation_id,
            "closed": True,
            "learning_delivered": True,
        }
        if idempotent:
            result["idempotent"] = True
        return result

    def _mark_delivered(self, allocation_id: str, step: str, owner_id: str | None = None) -> bool:
        column = _STEP_COLUMNS[step]
        owner_col = _CLAIM_OWNER_COLUMNS[step]
        claimed_at_col = _CLAIMED_AT_COLUMNS[step]
        now = utc_now_iso()
        with atomic(self._connection, "cbm_mark_delivered"):
            if owner_id:
                cur = self._connection.execute(
                    f"UPDATE {_OUTBOX_TABLE} SET {column} = ?, {owner_col} = NULL, "  # noqa: S608
                    f"{claimed_at_col} = NULL, last_error = NULL, updated_at = ? "
                    f"WHERE allocation_id = ? AND {column} IS NULL AND {owner_col} = ?",
                    (now, now, allocation_id, owner_id),
                )
            else:
                cur = self._connection.execute(
                    f"UPDATE {_OUTBOX_TABLE} SET {column} = ?, {owner_col} = NULL, "  # noqa: S608
                    f"{claimed_at_col} = NULL, last_error = NULL, updated_at = ? "
                    f"WHERE allocation_id = ? AND {column} IS NULL",
                    (now, now, allocation_id),
                )
            return cur.rowcount > 0

    def _record_delivery_error(self, allocation_id: str, step: str, exc: Exception) -> None:
        """Best-effort failure telemetry. Never converts failure into success.

        The authoritative pending state is the NULL delivery columns, not this
        row, so a failure to record telemetry must not mask the real error the
        caller is about to receive.
        """
        now = utc_now_iso()
        try:
            with atomic(self._connection, "cbm_delivery_error"):
                self._connection.execute(
                    f"UPDATE {_OUTBOX_TABLE} SET attempts = attempts + 1, "  # noqa: S608
                    "last_error = ?, updated_at = ? WHERE allocation_id = ?",
                    (f"{step}: {exc}"[:500], now, allocation_id),
                )
        except sqlite3.Error:
            return

    def get_allocation(self, allocation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = _row(
                self._connection.execute(
                    "SELECT * FROM cbm_allocations WHERE id = ?", (allocation_id,)
                ).fetchone()
            )
        if row is None:
            return None
        return self._alloc_dict(row)

    def list_escalations(self, allocation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM cbm_escalations WHERE allocation_id = ? ORDER BY created_at",
                    (allocation_id,),
                ).fetchall()
            )
        return rows

    def leaderboard(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM cbm_role_stats ORDER BY samples DESC, role"
                ).fetchall()
            )
        out = []
        for r in rows:
            samples = int(r["samples"] or 0)
            accepts = int(r["accepts"] or 0)
            out.append(
                {
                    "role": r["role"],
                    "samples": samples,
                    "accept_rate": (accepts / samples) if samples else 0.0,
                    "avg_wall_s": (float(r["sum_wall_s"] or 0) / samples) if samples else 0.0,
                    "avg_repairs": (int(r["sum_repairs"] or 0) / samples) if samples else 0.0,
                    "provider_hints": provider_hints_for_model_role(str(r["role"])),
                }
            )
        return out

    def _role_stats(self, role: str) -> dict[str, Any] | None:
        with self._lock:
            row = _row(
                self._connection.execute(
                    "SELECT * FROM cbm_role_stats WHERE role = ? AND fingerprint_key = ''",
                    (role,),
                ).fetchone()
            )
        return row

    @staticmethod
    def _alloc_dict(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "task_id": row.get("task_id"),
            "stage": row.get("stage"),
            "required_quality": row.get("required_quality"),
            "model_role": row.get("model_role"),
            "reasoning_effort": row.get("reasoning_effort"),
            "parallel_candidates": row.get("parallel_candidates"),
            "context_mode": row.get("context_mode"),
            "verification_rung": row.get("verification_rung"),
            "max_repairs": row.get("max_repairs"),
            "max_replans": row.get("max_replans"),
            "hedge_after_p95_seconds": row.get("hedge_after_p95_seconds"),
            "escalation_path": _loads(row.get("escalation_path_json"), []),
            "stop_rule": row.get("stop_rule"),
            "evidence_required": _loads(row.get("evidence_required_json"), []),
            "rationale": _loads(row.get("rationale_json"), []),
            "predicted_quality": row.get("predicted_quality"),
            "predicted_etar_s": row.get("predicted_etar_s"),
            "rung": row.get("rung"),
            "status": row.get("status"),
            "fingerprint": _loads(row.get("fingerprint_json"), {}),
            "provider_hints": provider_hints_for_model_role(str(row.get("model_role") or "")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }
