"""SQLite data access for bounded standing campaign grants.

The DAL composes over :class:`omniagentos.db.store.SqliteStore` so grant
validation, usage increments, and the corresponding audit action share the
same connection, writer lock, and transaction boundary.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from typing import Any, cast

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore

_OUTCOMES = frozenset({"ok", "failed", "broke_out", "refused"})


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        grants = cast("GrantsStore", args[0])
        with grants._store._lock:
            return method(*args, **kwargs)

    return wrapped


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _parse_json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _grant(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    target_set = _parse_json(out.pop("target_set_json", None), [])
    metadata = _parse_json(out.pop("metadata_json", None), {})
    # Preserve malformed target_set shapes so validation can fail closed rather
    # than silently coercing e.g. ``""`` / ``{}`` / ``0`` into an empty allow-all.
    if target_set is None:
        out["target_set"] = []
    else:
        out["target_set"] = target_set
    out["metadata"] = metadata if isinstance(metadata, dict) else {}
    return out


def _action(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return dict(row)


def _as_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_expired(expires_at: Any, now: str) -> bool:
    if not expires_at:
        return False
    expiry = _as_datetime(str(expires_at))
    moment = _as_datetime(now)
    # Invalid persisted bounds fail closed rather than creating an immortal grant.
    return expiry is None or moment is None or moment >= expiry


def is_grant_live(
    grant: dict[str, Any],
    *,
    capability: str | None = None,
    spend_usd: float = 0,
    now: str | None = None,
) -> tuple[bool, str]:
    """Check a grant's lifecycle and remaining bounds without consuming it.

    A live grant requires explicit human approval (``plan_approval_state`` ==
    ``approved``), a non-null expiry, and remaining action/spend headroom.
    Missing expiry is treated as not-live (fail closed) — immortal grants are
    refused rather than silently unbounded.
    """
    if grant.get("revoked_at"):
        return False, "revoked"
    if str(grant.get("plan_approval_state") or "") != "approved":
        return False, "not_approved"
    if not grant.get("expires_at"):
        return False, "no_expiry"
    if _is_expired(grant.get("expires_at"), now or utc_now_iso()):
        return False, "expired"
    if capability is not None and capability != grant.get("capability"):
        return False, "capability_mismatch"
    if capability is not None:
        # Local import: connectors.extractors imports connectors.broker, which
        # imports this package at module load time -- a module-level import
        # here would be circular. Defense-in-depth for a row that reaches
        # is_grant_live without the audience bound create_grant now requires
        # (a pre-existing / hand-edited row) -- never default a customer-
        # message grant to unbounded because the bound happens to be absent.
        from omniagentos.connectors.extractors import is_broadcast_capability

        if is_broadcast_capability(capability):
            meta = grant.get("metadata")
            meta = meta if isinstance(meta, dict) else {}
            if meta.get("max_recipients") is None or not meta.get("audience_snapshot_hash"):
                return False, "no_audience_bound"

    max_actions = grant.get("max_actions")
    if max_actions is None:
        return False, "no_max_actions"
    if int(grant.get("actions_used") or 0) >= int(max_actions):
        return False, "max_actions"

    requested_spend = float(spend_usd or 0)
    if requested_spend < 0:
        return False, "invalid_spend"
    max_spend = grant.get("max_spend_usd")
    if max_spend is None:
        return False, "no_max_spend"
    used = float(grant.get("spend_used_usd") or 0)
    maximum = float(max_spend)
    if used >= maximum or used + requested_spend > maximum:
        return False, "max_spend"
    return True, ""


@dataclass(frozen=True, slots=True)
class ConsumeResult:
    """Outcome of one atomic attempt to use a campaign grant."""

    ok: bool
    outcome: str
    reason: str = ""
    grant: dict[str, Any] | None = None
    action: dict[str, Any] | None = None


class GrantsStore:
    """Campaign-grants DAL composed over an already migrated ``SqliteStore``."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @property
    def _connection(self) -> sqlite3.Connection:
        """Resolve the calling thread's connection; never cache it."""
        return self._store._connection

    @_serialized
    def create_grant(
        self,
        capability: str,
        *,
        label: str = "",
        target_set: list[str] | None = None,
        project_id: str | None = None,
        approval_id: str | None = None,
        max_actions: int | None = None,
        max_spend_usd: float | None = None,
        max_recipients: int | None = None,
        expires_at: str | None = None,
        metadata: dict[str, Any] | None = None,
        plan_approval_state: str | None = None,
    ) -> dict[str, Any]:
        """Mint a bounded standing grant.

        Hard requirements (no silent defaults): ``approval_id``, ``max_actions``,
        ``max_spend_usd``, ``expires_at``, and a durable ``metadata.generation``
        pin must all be present. Every grant additionally requires an explicit
        ``metadata.action_class`` pin equal to the authoritative connector-registry
        class; this is especially load-bearing for hard-human and danger-group
        writes. Minting with an approval id records
        ``plan_approval_state="approved"`` so ``is_grant_live`` can require the
        same state. ``target_set`` must be a list (or omitted); non-list shapes
        are refused at mint rather than coerced to unrestricted.

        A customer-message capability (``gmail*.send``, ``piedpiper_*.conversation_send``,
        ``customerio_*.trigger_broadcast`` -- see
        :func:`omniagentos.connectors.extractors.is_broadcast_capability`)
        additionally requires a concrete, bounded audience: ``target_set`` must
        be non-empty (it IS the audience snapshot -- "no audience restriction"
        is not a legal state for a broadcast-capable grant), and
        ``max_recipients`` defaults to ``len(target_set)`` when omitted, or must
        be explicitly >= it. The resolved cap, an approval-time content hash of
        ``target_set``, and its count are recorded on ``metadata`` so the send
        path can re-check the live recipient surface against this exact
        snapshot before every send (``grants/validation.py``). This is a fail-
        closed requirement, not a default: an approved customer-message grant
        that cannot be bound this way is refused at mint, never minted
        unbounded.
        """
        if not approval_id or not str(approval_id).strip():
            raise ValueError("approval_id is required to mint a campaign grant")
        if max_actions is None or int(max_actions) < 1:
            raise ValueError("max_actions is required and must be >= 1")
        if max_spend_usd is None or float(max_spend_usd) < 0:
            raise ValueError("max_spend_usd is required and must be >= 0")
        if not expires_at or not str(expires_at).strip():
            raise ValueError("expires_at is required (ISO-8601); immortal grants refused")
        if target_set is not None and not isinstance(target_set, list):
            raise ValueError("target_set must be a list of strings (or omitted)")
        # Explicit approval_id means the human already approved the plan.
        state = plan_approval_state or "approved"
        if state != "approved":
            raise ValueError("plan_approval_state must be 'approved' when minting with approval_id")
        meta = dict(metadata or {})
        if "generation" not in meta:
            raise ValueError(
                "metadata.generation is required to mint a campaign grant "
                "(durable generation pin; omit is refused, not defaulted)"
            )
        try:
            meta["generation"] = int(meta["generation"])
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata.generation must be an integer durable pin") from exc
        try:
            # Local import avoids grants -> connectors -> broker -> grants at
            # module-import time while still binding every minted hard-human
            # grant to the authoritative registry classification.
            from omniagentos.connectors import load_registry

            authoritative_action_class = load_registry().capability(capability).action_class.value
        except Exception as exc:  # noqa: BLE001 -- an unknown/broken registry cannot mint proof.
            raise ValueError(
                f"cannot resolve authoritative action_class for capability {capability!r}"
            ) from exc
        pinned_action_class = meta.get("action_class")
        if not isinstance(pinned_action_class, str) or not pinned_action_class.strip():
            raise ValueError("metadata.action_class is required for a campaign grant")
        if pinned_action_class != authoritative_action_class:
            raise ValueError(
                "metadata.action_class must match the authoritative connector "
                f"class {authoritative_action_class!r}"
            )
        # Local import: connectors.extractors imports connectors.broker, which
        # imports this package at module load time -- a module-level import
        # here would be circular.
        from omniagentos.connectors.extractors import (
            audience_snapshot_hash,
            is_broadcast_capability,
        )

        if is_broadcast_capability(capability):
            resolved_targets = list(target_set) if target_set else []
            if not resolved_targets:
                raise ValueError(
                    "target_set (the audience snapshot) is required and must be "
                    f"non-empty to mint a {capability!r} grant; an approved "
                    "customer-message grant may not default to an unbounded audience"
                )
            if max_recipients is None:
                resolved_max_recipients = len(resolved_targets)
            else:
                resolved_max_recipients = int(max_recipients)
                if resolved_max_recipients < len(resolved_targets):
                    raise ValueError(
                        f"max_recipients ({resolved_max_recipients}) is smaller than "
                        f"target_set ({len(resolved_targets)} recipients); the cap must "
                        "cover the approved audience snapshot"
                    )
            if resolved_max_recipients < 1:
                raise ValueError("max_recipients must be >= 1")
            meta["max_recipients"] = resolved_max_recipients
            meta["audience_snapshot_hash"] = audience_snapshot_hash(resolved_targets)
            meta["audience_snapshot_count"] = len(resolved_targets)
        elif max_recipients is not None:
            raise ValueError(
                f"max_recipients is only meaningful for a customer-message capability; "
                f"{capability!r} is not broadcast-capable"
            )
        grant_id = new_id("gnt")
        self._store._write(
            "INSERT INTO campaign_grants "
            "(id, created_at, label, capability, target_set_json, project_id, approval_id, "
            "plan_approval_state, max_actions, max_spend_usd, expires_at, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                grant_id,
                utc_now_iso(),
                label,
                capability,
                _json(list(target_set) if target_set is not None else []),
                project_id,
                approval_id,
                state,
                int(max_actions),
                float(max_spend_usd),
                expires_at,
                _json(meta),
            ),
        )
        created = self._connection.execute(
            "SELECT * FROM campaign_grants WHERE id = ?", (grant_id,)
        ).fetchone()
        assert created is not None
        return _grant(created)

    @_serialized
    def get_grant(self, grant_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM campaign_grants WHERE id = ?", (grant_id,)
        ).fetchone()
        return None if row is None else _grant(row)

    @_serialized
    def revoke_grant(self, grant_id: str, reason: str = "") -> dict[str, Any] | None:
        changed = self._store._write_count(
            "UPDATE campaign_grants SET revoked_at = ?, revoke_reason = ? WHERE id = ?",
            (utc_now_iso(), reason, grant_id),
        )
        return self.get_grant(grant_id) if changed else None

    @_serialized
    def list_active_grants(
        self,
        *,
        capability: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["revoked_at IS NULL", "(expires_at IS NULL OR expires_at > ?)"]
        parameters: list[Any] = [utc_now_iso()]
        if capability is not None:
            clauses.append("capability = ?")
            parameters.append(capability)
        if project_id is not None:
            clauses.append("project_id = ?")
            parameters.append(project_id)
        rows = self._connection.execute(
            f"SELECT * FROM campaign_grants WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at ASC, id ASC",
            parameters,
        ).fetchall()
        return [_grant(row) for row in rows]

    def _insert_action(
        self,
        grant_id: str,
        capability: str,
        target: str | None,
        spend_usd: float,
        outcome: str,
        *,
        ref_type: str | None,
        ref_id: str | None,
        detail: str,
    ) -> dict[str, Any]:
        if outcome not in _OUTCOMES:
            raise ValueError(f"invalid grant action outcome: {outcome}")
        action_id = new_id("gac")
        self._connection.execute(
            "INSERT INTO grant_actions "
            "(id, created_at, grant_id, capability, target, spend_usd, outcome, "
            "ref_type, ref_id, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action_id,
                utc_now_iso(),
                grant_id,
                capability,
                target,
                float(spend_usd or 0),
                outcome,
                ref_type,
                ref_id,
                detail,
            ),
        )
        row = self._connection.execute(
            "SELECT * FROM grant_actions WHERE id = ?", (action_id,)
        ).fetchone()
        assert row is not None
        return _action(row)

    @_serialized
    def record_action(
        self,
        grant_id: str,
        capability: str,
        target: str | None,
        spend_usd: float,
        outcome: str,
        *,
        ref_type: str | None = None,
        ref_id: str | None = None,
        detail: str = "",
    ) -> dict[str, Any]:
        if outcome not in _OUTCOMES:
            raise ValueError(f"invalid grant action outcome: {outcome}")
        self._store._begin()
        try:
            created = self._insert_action(
                grant_id,
                capability,
                target,
                spend_usd,
                outcome,
                ref_type=ref_type,
                ref_id=ref_id,
                detail=detail,
            )
            self._store._commit()
            return created
        except BaseException:
            self._store._rollback()
            raise

    @_serialized
    def try_consume(
        self,
        grant_id: str,
        *,
        target: str | None = None,
        spend_usd: float = 0,
        capability: str | None = None,
        generation: int | None = None,
        scoped_args: dict[str, Any] | None = None,
        action_class: str | None = None,
        connector: str | None = None,
        tool: str | None = None,
        ref_type: str | None = None,
        ref_id: str | None = None,
        detail: str = "",
    ) -> ConsumeResult:
        """Atomically validate, consume bounds, and append the audit action."""
        # Local import keeps store free of a circular import at module load time.
        from omniagentos.grants.validation import validate_approval_token

        requested_spend = float(spend_usd or 0)
        self._store._begin()
        try:
            row = self._connection.execute(
                "SELECT * FROM campaign_grants WHERE id = ?", (grant_id,)
            ).fetchone()
            if row is None:
                self._store._commit()
                return ConsumeResult(False, "refused", "not_found")

            grant = _grant(row)
            requested_capability = capability or str(grant["capability"])

            # Full durable scope check (generation / target / pins / args) before
            # any bound is spent. validate_approval_token is pure-read against
            # the already-loaded row via a tiny adapter.
            class _RowStore:
                def get_grant(self, gid: str) -> dict[str, Any] | None:
                    return grant if gid == grant_id else None

            validation = validate_approval_token(
                grant_id,
                grant_store=_RowStore(),
                capability=requested_capability,
                action_class=action_class,
                connector=connector,
                tool=tool,
                target=target,
                generation=generation,
                scoped_args=scoped_args,
                spend_usd=requested_spend,
            )
            if not validation.ok:
                outcome = (
                    "broke_out"
                    if validation.reason in {"target_breakout", "target_ambiguous"}
                    else "refused"
                )
                action = self._insert_action(
                    grant_id,
                    requested_capability,
                    target,
                    requested_spend,
                    outcome,
                    ref_type=ref_type,
                    ref_id=ref_id,
                    detail=detail or validation.reason,
                )
                self._store._commit()
                return ConsumeResult(False, outcome, validation.reason, grant, action)

            self._connection.execute(
                "UPDATE campaign_grants SET actions_used = actions_used + 1, "
                "spend_used_usd = spend_used_usd + ? WHERE id = ?",
                (requested_spend, grant_id),
            )
            action = self._insert_action(
                grant_id,
                requested_capability,
                target,
                requested_spend,
                "ok",
                ref_type=ref_type,
                ref_id=ref_id,
                detail=detail,
            )
            updated_row = self._connection.execute(
                "SELECT * FROM campaign_grants WHERE id = ?", (grant_id,)
            ).fetchone()
            assert updated_row is not None
            updated = _grant(updated_row)
            self._store._commit()
            return ConsumeResult(True, "ok", grant=updated, action=action)
        except BaseException:
            self._store._rollback()
            raise


__all__ = ["ConsumeResult", "GrantsStore", "is_grant_live"]
