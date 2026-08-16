"""Persisted TaskContract store with ledger identity (M-16).

Self-contained tables (CREATE IF NOT EXISTS) so L16 does not depend on an L14
migration and does not touch L18 cognitive storage. The authoritative identity
is ``contract_hash`` (lease-excluded SHA-256 of the TaskContract intent).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from threading import RLock
from typing import Any

from omniagentos.contracts import default_db_path, new_id, utc_now_iso
from omniagentos.db.store import _connect, _row, _rows
from omniagentos.taskcontract.models import TaskContract, TaskContractError
from omniagentos.taskcontract.transitions import (
    ContractState,
    assert_supported_lane,
    validate_transition,
)

__all__ = ["TaskContractRecord", "TaskContractStore"]

# DDL mirrored by omniagentos/db/migrations/086_control_plane.sql (same tables
# and indexes, including IF NOT EXISTS). Keep both sides in lockstep.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_contracts (
    id              TEXT PRIMARY KEY,
    contract_hash   TEXT NOT NULL,
    state           TEXT NOT NULL,
    lane            TEXT NOT NULL,
    ref_id          TEXT,
    task_id         TEXT,
    lineage_id      TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    body_json       TEXT NOT NULL,
    metadata_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(contract_hash, lane, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_task_contracts_hash ON task_contracts(contract_hash);
CREATE INDEX IF NOT EXISTS idx_task_contracts_ref ON task_contracts(lane, ref_id, state);
CREATE INDEX IF NOT EXISTS idx_task_contracts_task ON task_contracts(task_id, updated_at);

CREATE TABLE IF NOT EXISTS task_contract_transitions (
    id              TEXT PRIMARY KEY,
    contract_id     TEXT NOT NULL,
    contract_hash   TEXT NOT NULL,
    from_state      TEXT NOT NULL,
    to_state        TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    actor           TEXT NOT NULL DEFAULT 'system',
    evidence_json   TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_contract_tx_contract
    ON task_contract_transitions(contract_id, created_at);
"""


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


class TaskContractRecord:
    """Hydrated persisted contract row."""

    __slots__ = (
        "id",
        "contract_hash",
        "state",
        "lane",
        "ref_id",
        "task_id",
        "lineage_id",
        "version",
        "body",
        "metadata",
        "created_at",
        "updated_at",
    )

    def __init__(self, row: Mapping[str, Any]) -> None:
        self.id = str(row["id"])
        self.contract_hash = str(row["contract_hash"])
        self.state = str(row["state"])
        self.lane = str(row["lane"])
        self.ref_id = row.get("ref_id")
        self.task_id = row.get("task_id")
        self.lineage_id = row.get("lineage_id")
        self.version = int(row.get("version") or 1)
        body_raw = row.get("body_json") or "{}"
        meta_raw = row.get("metadata_json") or "{}"
        self.body = json.loads(body_raw) if isinstance(body_raw, str) else dict(body_raw or {})
        self.metadata = json.loads(meta_raw) if isinstance(meta_raw, str) else dict(meta_raw or {})
        self.created_at = str(row.get("created_at") or "")
        self.updated_at = str(row.get("updated_at") or "")

    def contract(self) -> TaskContract:
        return TaskContract.from_dict(self.body)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contract_hash": self.contract_hash,
            "state": self.state,
            "lane": self.lane,
            "ref_id": self.ref_id,
            "task_id": self.task_id,
            "lineage_id": self.lineage_id,
            "version": self.version,
            "body": dict(self.body),
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TaskContractStore:
    """Authoritative persistence + transition validation for TaskContracts."""

    def __init__(self, database: str | Path | sqlite3.Connection | None = None) -> None:
        self._lock = RLock()
        if isinstance(database, sqlite3.Connection):
            self._connection = database
            self._connection.row_factory = sqlite3.Row
            self._owns = False
        else:
            self._connection = _connect(str(database or default_db_path()))
            self._owns = True
        self._ensure_schema()

    def close(self) -> None:
        if self._owns:
            self._connection.close()
            self._owns = False

    def _ensure_schema(self) -> None:
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.commit()

    def adopt(
        self,
        contract: TaskContract | Mapping[str, Any],
        *,
        lane: str,
        ref_id: str | None = None,
        actor: str = "system",
        metadata: Mapping[str, Any] | None = None,
        initial_state: str | ContractState = ContractState.ADOPTED,
    ) -> TaskContractRecord:
        """Validate, hash, and persist a contract as adopted for a lane/ref.

        Idempotent on ``(contract_hash, lane, ref_id)``: re-adoption of the same
        intent returns the existing row without weakening state.
        """
        lane_n = assert_supported_lane(lane)
        tc = contract if isinstance(contract, TaskContract) else TaskContract.from_dict(contract)
        digest = tc.contract_hash()
        body = tc.to_dict()
        # Ledger identity must match recomputation from stored body.
        if TaskContract.from_dict(body).contract_hash() != digest:
            raise TaskContractError("contract body does not re-hash to its ledger identity")

        try:
            state = ContractState(str(initial_state))
        except ValueError as exc:
            raise TaskContractError(f"invalid initial state: {initial_state!r}") from exc
        if state not in {ContractState.DRAFT, ContractState.ADOPTED}:
            raise TaskContractError("adopt initial_state must be draft or adopted")

        now = utc_now_iso()
        with self._lock:
            existing = _row(
                self._connection.execute(
                    "SELECT * FROM task_contracts WHERE contract_hash = ? AND lane = ? "
                    "AND IFNULL(ref_id, '') = IFNULL(?, '')",
                    (digest, lane_n, ref_id),
                ).fetchone()
            )
            if existing is not None:
                return TaskContractRecord(existing)

            cid = new_id("tct")
            meta = dict(metadata or {})
            self._connection.execute(
                "INSERT INTO task_contracts "
                "(id, contract_hash, state, lane, ref_id, task_id, lineage_id, version, "
                "body_json, metadata_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    digest,
                    state.value,
                    lane_n,
                    ref_id,
                    tc.task_id,
                    tc.lineage_id,
                    int(tc.version),
                    _j(body),
                    _j(meta),
                    now,
                    now,
                ),
            )
            self._connection.execute(
                "INSERT INTO task_contract_transitions "
                "(id, contract_id, contract_hash, from_state, to_state, reason, actor, "
                "evidence_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    new_id("tcx"),
                    cid,
                    digest,
                    ContractState.DRAFT.value,
                    state.value,
                    "adopt",
                    actor,
                    _j({"lane": lane_n, "ref_id": ref_id}),
                    now,
                ),
            )
            self._connection.commit()
            row = _row(
                self._connection.execute(
                    "SELECT * FROM task_contracts WHERE id = ?", (cid,)
                ).fetchone()
            )
            assert row is not None
            return TaskContractRecord(row)

    def transition(
        self,
        contract_id: str,
        to_state: str | ContractState,
        *,
        reason: str = "",
        actor: str = "system",
        evidence: Mapping[str, Any] | None = None,
        expected_hash: str | None = None,
    ) -> TaskContractRecord:
        """Apply a validated state transition; fail closed on illegal edges."""
        with self._lock:
            row = _row(
                self._connection.execute(
                    "SELECT * FROM task_contracts WHERE id = ?", (contract_id,)
                ).fetchone()
            )
            if row is None:
                raise KeyError(contract_id)
            current = str(row["state"])
            digest = str(row["contract_hash"])
            # Identity check against stored hash.
            body = json.loads(row["body_json"] or "{}")
            recomputed = TaskContract.from_dict(body).contract_hash()
            if recomputed != digest:
                raise TaskContractError(
                    "stored contract body no longer matches ledger contract_hash"
                )
            dst = validate_transition(
                current,
                to_state,
                contract_hash=digest,
                expected_hash=expected_hash if expected_hash is not None else digest,
            )
            if current == dst.value:
                return TaskContractRecord(row)

            now = utc_now_iso()
            self._connection.execute(
                "UPDATE task_contracts SET state = ?, updated_at = ? WHERE id = ?",
                (dst.value, now, contract_id),
            )
            self._connection.execute(
                "INSERT INTO task_contract_transitions "
                "(id, contract_id, contract_hash, from_state, to_state, reason, actor, "
                "evidence_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    new_id("tcx"),
                    contract_id,
                    digest,
                    current,
                    dst.value,
                    str(reason or ""),
                    actor,
                    _j(dict(evidence or {})),
                    now,
                ),
            )
            self._connection.commit()
            updated = _row(
                self._connection.execute(
                    "SELECT * FROM task_contracts WHERE id = ?", (contract_id,)
                ).fetchone()
            )
            assert updated is not None
            return TaskContractRecord(updated)

    def get(self, contract_id: str) -> TaskContractRecord | None:
        with self._lock:
            row = _row(
                self._connection.execute(
                    "SELECT * FROM task_contracts WHERE id = ?", (contract_id,)
                ).fetchone()
            )
        return TaskContractRecord(row) if row else None

    def get_by_hash(
        self, contract_hash: str, *, lane: str | None = None, ref_id: str | None = None
    ) -> TaskContractRecord | None:
        with self._lock:
            if lane is not None:
                row = _row(
                    self._connection.execute(
                        "SELECT * FROM task_contracts WHERE contract_hash = ? AND lane = ? "
                        "AND IFNULL(ref_id, '') = IFNULL(?, '') ORDER BY updated_at DESC LIMIT 1",
                        (contract_hash, assert_supported_lane(lane), ref_id),
                    ).fetchone()
                )
            else:
                row = _row(
                    self._connection.execute(
                        "SELECT * FROM task_contracts WHERE contract_hash = ? "
                        "ORDER BY updated_at DESC LIMIT 1",
                        (contract_hash,),
                    ).fetchone()
                )
        return TaskContractRecord(row) if row else None

    def list_for_ref(self, lane: str, ref_id: str) -> list[TaskContractRecord]:
        lane_n = assert_supported_lane(lane)
        with self._lock:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM task_contracts WHERE lane = ? AND ref_id = ? "
                    "ORDER BY updated_at DESC",
                    (lane_n, ref_id),
                ).fetchall()
            )
        return [TaskContractRecord(r) for r in rows]

    def list_transitions(self, contract_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = _rows(
                self._connection.execute(
                    "SELECT * FROM task_contract_transitions WHERE contract_id = ? "
                    "ORDER BY created_at ASC",
                    (contract_id,),
                ).fetchall()
            )
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "id": r["id"],
                    "contract_id": r["contract_id"],
                    "contract_hash": r["contract_hash"],
                    "from_state": r["from_state"],
                    "to_state": r["to_state"],
                    "reason": r["reason"],
                    "actor": r["actor"],
                    "evidence": json.loads(r["evidence_json"] or "{}"),
                    "created_at": r["created_at"],
                }
            )
        return out

    def assert_hash_identity(self, contract_id: str) -> str:
        """Re-hash stored body and fail closed on drift."""
        rec = self.get(contract_id)
        if rec is None:
            raise KeyError(contract_id)
        recomputed = rec.contract().contract_hash()
        if recomputed != rec.contract_hash:
            raise TaskContractError(
                f"ledger identity broken for {contract_id}: "
                f"stored={rec.contract_hash} recomputed={recomputed}"
            )
        return recomputed
