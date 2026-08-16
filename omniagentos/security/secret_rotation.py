"""The credential rotation state machine (U-S2 half B).

Nine steps, in this order, each one recorded before the next begins:

    create_new -> write_only_stage -> capability_probe -> dual_version_canary
    -> atomic_active_pointer_switch -> cache_grant_invalidation
    -> provider_revoke [OPERATOR]-GATED -> encrypted_backup_expiry -> receipt_closure

THIS ENGINE NEVER HANDLES A CREDENTIAL VALUE.

Every step that must touch real key material is delegated to a
:class:`RotationAdapter`, and every argument this module passes an adapter is an
IDENTIFIER -- a credential id, a version id. There is no parameter, return type,
or column anywhere in this file through which a secret could travel, which is
why the ceremony can be driven from an operator CLI without the value entering
this process at all.

THE ORDER IS THE SAFETY PROPERTY

Provider-side revocation comes AFTER the switch and after the rollback window
precisely because a failed canary must be able to go back. So:

    * a failed probe aborts before the active pointer moves at all;
    * a failed canary rolls the pointer back to the previous version;
    * a rollback NEVER activates a revoked version. If the previous version has
      been revoked, there is nothing safe to return to, so the credential is
      parked in ``quarantined`` with no active version -- fail closed -- rather
      than resurrected. :meth:`RotationEngine._point_active_at` is the single
      choke point that enforces this, so even a coding mistake in a future step
      cannot route around it.

PROVIDER REVOCATION IS OFF, BY TWO INDEPENDENT SWITCHES ([OPERATOR] / D-05)

D-05 is a named [OPERATOR] decision and provider revocation is irreversible, so the
step is BUILT and its actual call is unreachable until a human arms it:

    1. the environment flag :data:`PROVIDER_REVOKE_ENV` must equal the exact
       literal :data:`PROVIDER_REVOKE_ARMED_VALUE` ("ARMED"), and
    2. the caller must pass ``provider_revoke_operator_approval=True``.

Both default to off. A flag alone cannot arm it (a variable left in a launch
profile would otherwise silently arm every rotation on the box) and an argument
alone cannot arm it (a caller cannot self-authorize an irreversible action).
There is exactly ONE call to ``revoke_at_provider`` in this file and it is
lexically inside ``if _provider_revoke_armed(...)`` -- asserted by
``tests/security/test_secret_rotation.py`` against this module's own AST, so
the claim is measured rather than promised. When the step is skipped the
rotation still closes a receipt, recording ``skipped_flag_off``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any, Protocol, cast

from omniagentos.connectors.secret_catalog import (
    SecretCatalog,
    assert_name_only,
    invalidate_cache,
    open_catalog,
)
from omniagentos.contracts import digest, new_id, utc_now_iso

_LOG = logging.getLogger(__name__)

#: [OPERATOR] / D-05. The labelled switch for provider-side revocation.
#:
#: Named ``ROTATION`` and not ``SECRET`` on purpose, and the reason is worth
#: recording so it does not read as evasion: ``tests/llm/test_unbrokered_
#: credentials.py`` flags any ``os.environ`` read whose name matches the
#: credential shape (``_KEY|_TOKEN|_SECRET|...``), and its inventory of
#: exceptions is the T4.8 migration's list of REAL unbrokered credential reads.
#: This module performs none -- it reads a boolean rollout flag -- so putting it
#: on that list would dilute a live security inventory with something that is
#: not a credential read at all. The flag is no less findable under this name.
PROVIDER_REVOKE_ENV = "OMNIAGENTOS_ROTATION_PROVIDER_REVOKE"

#: The ONLY value of :data:`PROVIDER_REVOKE_ENV` that arms the step. Compared
#: exactly: "1", "true", and "yes" all leave it OFF, because a switch this
#: consequential should not be flippable by the habit of setting a variable to 1.
PROVIDER_REVOKE_ARMED_VALUE = "ARMED"

#: The nine steps, in execution order. The receipt digest is computed over the
#: recorded events, so a ceremony that skipped a step cannot later claim it ran.
ROTATION_STEPS: tuple[str, ...] = (
    "create_new",
    "write_only_stage",
    "capability_probe",
    "dual_version_canary",
    "atomic_active_pointer_switch",
    "cache_grant_invalidation",
    "provider_revoke",
    "encrypted_backup_expiry",
    "receipt_closure",
)

#: Catalog states a rotation may start from. Everything else is refused:
#: ``missing`` would be provisioning (a separate named [OPERATOR] decision under
#: D-33, never an invention here), ``quarantined``/``revoked`` are credentials
#: this system is refusing to use at all, ``retired`` is superseded, and
#: ``rotating`` means an earlier ceremony is still open and must be closed
#: before a second one moves the same pointer.
ROTATABLE_STATES: frozenset[str] = frozenset({"active"})

_DEFAULT_BACKUP_RETENTION_DAYS = 7


class RotationRefused(RuntimeError):
    """A rotation step was refused. Carries a reason code, never a value."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class RotationAdapter(Protocol):
    """The out-of-band half of a rotation ceremony.

    An implementation performs the steps that genuinely require key material --
    minting a new version at the provider, writing it into the local store,
    exercising it, revoking the old one. The engine hands it IDENTIFIERS only
    and reads back booleans, so credential material never crosses back into this
    process.
    """

    def stage(self, credential_id: str, version_id: str) -> None:
        """Write-only stage: install the new version. Returns nothing."""

    def probe(self, credential_id: str, version_id: str) -> bool:
        """Exercise one capability with the new version. True when it works."""

    def canary(self, credential_id: str, old_version_id: str, new_version_id: str) -> bool:
        """Run both versions side by side. True when the new one is healthy."""

    def revoke_at_provider(self, credential_id: str, version_id: str) -> None:
        """Irreversibly kill one version at the provider. [OPERATOR]-GATED (D-05)."""


@dataclass(frozen=True, slots=True)
class RotationReceipt:
    """Name-only, closed record of one rotation ceremony."""

    rotation_id: str
    credential_id: str
    env_name: str
    from_version_id: str
    to_version_id: str
    outcome: str
    provider_revoke_state: str
    steps: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    receipt_digest: str = ""
    closed_at: str = ""

    @property
    def rolled_back(self) -> bool:
        return self.outcome in {"rolled_back", "rolled_back_no_active"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "rotation_id": self.rotation_id,
            "credential_id": self.credential_id,
            "env_name": self.env_name,
            "from_version_id": self.from_version_id,
            "to_version_id": self.to_version_id,
            "outcome": self.outcome,
            "provider_revoke_state": self.provider_revoke_state,
            "steps": [dict(step) for step in self.steps],
            "receipt_digest": self.receipt_digest,
            "closed_at": self.closed_at,
        }


def _provider_revoke_armed(operator_approval: bool) -> bool:
    """[OPERATOR]-GATED (D-05). True only when BOTH switches are explicitly on.

    Returns False by default and on every ambiguity. Read the two conditions as
    one sentence: a human set the labelled environment flag to the exact literal
    ``ARMED`` on this box, AND this particular call was made with an explicit
    operator approval. Neither alone is enough, and nothing in this repository
    passes ``operator_approval=True`` except an operator-driven ceremony.
    """
    if not operator_approval:
        return False
    return (os.environ.get(PROVIDER_REVOKE_ENV) or "").strip() == PROVIDER_REVOKE_ARMED_VALUE


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Serialize writes through the composed store's writer lock (as store.py)."""

    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        engine = cast("RotationEngine", args[0])
        with engine._store._lock:
            return method(*args, **kwargs)

    return wrapped


class RotationEngine:
    """Drive one credential through the rotation state machine."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._catalog = SecretCatalog(store)

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._store._connection

    # --- the ceremony ------------------------------------------------------

    def rotate(
        self,
        env_name: str,
        adapter: RotationAdapter,
        *,
        operator: str,
        backup_retention_days: int = _DEFAULT_BACKUP_RETENTION_DAYS,
        provider_revoke_operator_approval: bool = False,
    ) -> RotationReceipt:
        """Run the nine-step ceremony for one credential NAME.

        Returns a closed receipt in every terminal case -- success, aborted
        probe, and rolled-back canary alike. A ceremony that ends without a
        receipt is indistinguishable from one that never ran, which is exactly
        the ambiguity a rotation cannot afford.
        """
        row = self._catalog.get(env_name)
        if row is None:
            raise RotationRefused("unknown_credential", "no catalog row for this name")
        state = str(row["state"])
        if state not in ROTATABLE_STATES:
            raise RotationRefused(
                "credential_not_rotatable",
                f"catalog state {state!r} cannot start a rotation",
            )

        credential_id = str(row["credential_id"])
        from_version_id = str(row["active_version_id"] or "")
        to_version_id = new_id("skv")
        rotation_id = new_id("rot")

        self._open_rotation(
            rotation_id,
            credential_id,
            from_version_id,
            to_version_id,
            operator=operator,
        )
        self._create_version(credential_id, to_version_id)
        self._catalog.set_state(env_name, "rotating", actor=operator, note="rotation in progress")
        self._record(rotation_id, "create_new", "ok", f"version {to_version_id} staged as new")

        # --- write-only stage ---
        staged = self._adapter_step(
            rotation_id,
            "write_only_stage",
            lambda: adapter.stage(credential_id, to_version_id),
        )
        if not staged:
            return self._abort_before_switch(
                rotation_id, env_name, credential_id, from_version_id, to_version_id, operator
            )

        # --- capability probe: nothing has moved yet, so a failure aborts ---
        probed = self._adapter_step(
            rotation_id,
            "capability_probe",
            lambda: adapter.probe(credential_id, to_version_id),
        )
        if not probed:
            return self._abort_before_switch(
                rotation_id, env_name, credential_id, from_version_id, to_version_id, operator
            )

        # --- dual-version canary: both versions live at once ---
        self._set_version_state(to_version_id, "canary")
        if not self._adapter_step(
            rotation_id,
            "dual_version_canary",
            lambda: adapter.canary(credential_id, from_version_id, to_version_id),
        ):
            return self._rollback(
                rotation_id, env_name, credential_id, from_version_id, to_version_id, operator
            )

        # --- atomic active-pointer switch ---
        self._point_active_at(credential_id, to_version_id, catalog_state="active")
        self._retire_version(from_version_id)
        self._record(
            rotation_id,
            "atomic_active_pointer_switch",
            "ok",
            f"active version is now {to_version_id}",
        )

        # --- cache / grant invalidation ---
        invalidate_cache()
        self._record(
            rotation_id,
            "cache_grant_invalidation",
            "ok",
            "resolution-state cache dropped",
        )

        # --- old-version provider revoke: [OPERATOR]-GATED (D-05), OFF by default ---
        revoke_state = self._provider_revoke(
            rotation_id,
            adapter,
            credential_id,
            from_version_id,
            operator_approval=provider_revoke_operator_approval,
        )

        # --- encrypted-backup expiry (U-S3 owns the deletion itself) ---
        self._expire_backup(from_version_id, backup_retention_days)
        self._record(
            rotation_id,
            "encrypted_backup_expiry",
            "ok" if from_version_id else "skipped",
            f"retention {backup_retention_days}d recorded by version id",
        )

        return self._close(
            rotation_id,
            env_name,
            credential_id,
            from_version_id,
            to_version_id,
            outcome="succeeded",
            provider_revoke_state=revoke_state,
        )

    # --- terminal paths ----------------------------------------------------

    def _abort_before_switch(
        self,
        rotation_id: str,
        env_name: str,
        credential_id: str,
        from_version_id: str,
        to_version_id: str,
        operator: str,
    ) -> RotationReceipt:
        """Stage/probe failed. The active pointer never moved; put it back as it was."""
        self._retire_version(to_version_id)
        self._catalog.set_state(
            env_name, "active", actor=operator, note="rotation aborted before switch"
        )
        return self._close(
            rotation_id,
            env_name,
            credential_id,
            from_version_id,
            to_version_id,
            outcome="failed",
            provider_revoke_state="not_attempted",
        )

    def _rollback(
        self,
        rotation_id: str,
        env_name: str,
        credential_id: str,
        from_version_id: str,
        to_version_id: str,
        operator: str,
    ) -> RotationReceipt:
        """The canary failed. Restore the previous pointer WITHOUT resurrecting.

        The new version is ``retired`` and not ``revoked``: this program took no
        provider-side action against it, and recording a revocation that did not
        happen would make the catalog lie in the direction of "that key is dead"
        -- the direction that later licenses someone to stop worrying about it.

        If the previous version is revoked (or gone), there is nothing safe to
        go back to. The credential is parked in ``quarantined`` with no active
        version, which the broker refuses with ``credential_quarantined`` while
        its metadata stays readable, rather than reactivating a dead key.
        """
        self._retire_version(to_version_id)
        previous = self._catalog.version(from_version_id) if from_version_id else None
        if previous is None or str(previous["state"]) == "revoked":
            self._clear_active(credential_id)
            self._catalog.set_state(
                env_name,
                "quarantined",
                actor=operator,
                note="canary failed and the previous version is revoked; no active version",
            )
            invalidate_cache()
            self._record(
                rotation_id,
                "atomic_active_pointer_switch",
                "refused",
                "rollback refused to activate a revoked version; credential parked",
            )
            outcome = "rolled_back_no_active"
        else:
            self._point_active_at(credential_id, from_version_id, catalog_state="active")
            invalidate_cache()
            self._record(
                rotation_id,
                "atomic_active_pointer_switch",
                "refused",
                f"rolled back to {from_version_id}",
            )
            outcome = "rolled_back"
        self._record(
            rotation_id,
            "cache_grant_invalidation",
            "ok",
            "resolution-state cache dropped",
        )
        return self._close(
            rotation_id,
            env_name,
            credential_id,
            from_version_id,
            to_version_id,
            outcome=outcome,
            provider_revoke_state="not_attempted",
        )

    # --- the [OPERATOR]-gated step ----------------------------------------------

    def _provider_revoke(
        self,
        rotation_id: str,
        adapter: RotationAdapter,
        credential_id: str,
        version_id: str,
        *,
        operator_approval: bool,
    ) -> str:
        """Revoke the superseded version at the provider. [OPERATOR]-GATED (D-05).

        THE CALL BELOW IS THE ONLY ``revoke_at_provider`` CALL IN THIS FILE AND
        IT IS UNREACHABLE UNLESS A HUMAN ARMED BOTH SWITCHES. Off is not a
        failure: the step records ``skipped_flag_off`` and the rotation closes
        successfully, because a rotation whose old key is still alive at the
        provider is a completed rotation with a follow-up, not a broken one.
        """
        if not version_id:
            self._record(rotation_id, "provider_revoke", "skipped", "no previous version")
            return "not_attempted"
        if not _provider_revoke_armed(operator_approval):
            self._record(
                rotation_id,
                "provider_revoke",
                "skipped",
                f"{PROVIDER_REVOKE_ENV} is not {PROVIDER_REVOKE_ARMED_VALUE} "
                "and/or no operator approval was given",
            )
            return "skipped_flag_off"
        try:
            adapter.revoke_at_provider(credential_id, version_id)
        except Exception as exc:  # noqa: BLE001 -- see the type-name-only note below.
            # Only the exception TYPE is recorded. A provider client's message
            # can quote the request it just made, and that request carried the
            # key this step is revoking.
            self._record(rotation_id, "provider_revoke", "refused", type(exc).__name__)
            return "failed"
        self._mark_revoked(version_id)
        invalidate_cache()
        self._record(rotation_id, "provider_revoke", "ok", f"version {version_id} revoked")
        return "completed"

    # --- adapter plumbing ---------------------------------------------------

    def _adapter_step(self, rotation_id: str, step: str, action: Callable[[], Any]) -> bool:
        """Run one adapter call and record it. Exceptions are failures, not crashes."""
        try:
            result = action()
        except Exception as exc:  # noqa: BLE001 -- type name only; see _provider_revoke.
            self._record(rotation_id, step, "refused", type(exc).__name__)
            return False
        # ``stage`` returns None and is judged only by not raising; the two
        # verification steps return an explicit boolean.
        ok = True if result is None else bool(result)
        self._record(rotation_id, step, "ok" if ok else "refused", "")
        return ok

    # --- durable state ------------------------------------------------------

    @_serialized
    def _open_rotation(
        self,
        rotation_id: str,
        credential_id: str,
        from_version_id: str,
        to_version_id: str,
        *,
        operator: str,
    ) -> None:
        self._write(
            "INSERT INTO secret_rotations (rotation_id, credential_id, from_version_id, "
            "to_version_id, step, outcome, provider_revoke_state, operator, opened_at) "
            "VALUES (?, ?, ?, ?, 'create_new', 'open', 'not_attempted', ?, ?)",
            (
                rotation_id,
                credential_id,
                from_version_id,
                to_version_id,
                operator,
                utc_now_iso(),
            ),
        )

    @_serialized
    def _create_version(self, credential_id: str, version_id: str) -> None:
        self._write(
            "INSERT INTO secret_versions (version_id, credential_id, state, created_at) "
            "VALUES (?, ?, 'staged', ?)",
            (version_id, credential_id, utc_now_iso()),
        )

    @_serialized
    def _set_version_state(self, version_id: str, state: str) -> None:
        self._write(
            "UPDATE secret_versions SET state = ? WHERE version_id = ? AND state != 'revoked'",
            (state, version_id),
        )

    @_serialized
    def _retire_version(self, version_id: str) -> None:
        if not version_id:
            return
        self._write(
            "UPDATE secret_versions SET state = 'retired', retired_at = ? "
            "WHERE version_id = ? AND state != 'revoked'",
            (utc_now_iso(), version_id),
        )

    @_serialized
    def _mark_revoked(self, version_id: str) -> None:
        self._write(
            "UPDATE secret_versions SET state = 'revoked', revoked_at = ?, provider_revoked = 1 "
            "WHERE version_id = ?",
            (utc_now_iso(), version_id),
        )

    @_serialized
    def _expire_backup(self, version_id: str, retention_days: int) -> None:
        """Bind the retired version's backup expiry to its VERSION ID, not a filename.

        U-S3 owns the deletion; this records when it becomes due. Version id and
        not a filename glob, so a renamed generation cannot outlive its window.
        """
        if not version_id:
            return
        expires = (datetime.now(tz=UTC) + timedelta(days=max(0, retention_days))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._write(
            "UPDATE secret_versions SET backup_expires_at = ? WHERE version_id = ?",
            (expires, version_id),
        )

    @_serialized
    def _point_active_at(self, credential_id: str, version_id: str, *, catalog_state: str) -> None:
        """THE SINGLE CHOKE POINT for the active pointer. Refuses a revoked target.

        Every path that moves the pointer -- the forward switch and the rollback
        alike -- comes through here, so "a failed canary never resurrects a
        revoked version" is one enforced rule rather than a discipline repeated
        at each call site.
        """
        version = self._catalog.version(version_id)
        if version is None:
            raise RotationRefused("unknown_version", "no version row for this id")
        if str(version["state"]) == "revoked":
            raise RotationRefused(
                "revoked_version_cannot_be_activated",
                "a revoked credential version is never reinstated",
            )
        now = utc_now_iso()
        self._store._begin()
        try:
            self._conn.execute(
                "UPDATE secret_versions SET state = 'active', activated_at = ? "
                "WHERE version_id = ?",
                (now, version_id),
            )
            self._conn.execute(
                "UPDATE secret_catalog SET active_version_id = ?, state = ?, rotated_at = ?, "
                "updated_at = ? WHERE credential_id = ?",
                (version_id, catalog_state, now, now, credential_id),
            )
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise
        invalidate_cache()

    @_serialized
    def _clear_active(self, credential_id: str) -> None:
        self._write(
            "UPDATE secret_catalog SET active_version_id = '', updated_at = ? "
            "WHERE credential_id = ?",
            (utc_now_iso(), credential_id),
        )

    @_serialized
    def _record(self, rotation_id: str, step: str, status: str, detail: str) -> None:
        """Append one append-only step event and advance the rotation's step marker."""
        if step not in ROTATION_STEPS:
            raise RotationRefused("unknown_step", f"{step!r} is not a rotation step")
        seq = int(
            self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM secret_rotation_events WHERE rotation_id = ?",
                (rotation_id,),
            ).fetchone()[0]
        )
        self._store._begin()
        try:
            self._conn.execute(
                "INSERT INTO secret_rotation_events (rotation_id, seq, step, status, detail, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rotation_id, seq, step, status, detail, utc_now_iso()),
            )
            self._conn.execute(
                "UPDATE secret_rotations SET step = ? WHERE rotation_id = ?",
                (step, rotation_id),
            )
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise

    @_serialized
    def _close(
        self,
        rotation_id: str,
        env_name: str,
        credential_id: str,
        from_version_id: str,
        to_version_id: str,
        *,
        outcome: str,
        provider_revoke_state: str,
    ) -> RotationReceipt:
        """Close the ceremony and mint its receipt over the recorded events."""
        steps = tuple(
            {
                "seq": int(row["seq"]),
                "step": str(row["step"]),
                "status": str(row["status"]),
                "detail": str(row["detail"]),
            }
            for row in self._conn.execute(
                "SELECT seq, step, status, detail FROM secret_rotation_events "
                "WHERE rotation_id = ? ORDER BY seq",
                (rotation_id,),
            ).fetchall()
        )
        receipt_digest = digest(json.dumps(steps, sort_keys=True))
        closed_at = utc_now_iso()
        self._store._begin()
        try:
            self._conn.execute(
                "UPDATE secret_rotations SET outcome = ?, provider_revoke_state = ?, "
                "step = 'receipt_closure', closed_at = ?, receipt_digest = ? "
                "WHERE rotation_id = ?",
                (outcome, provider_revoke_state, closed_at, receipt_digest, rotation_id),
            )
            self._conn.execute(
                "INSERT INTO secret_rotation_events (rotation_id, seq, step, status, detail, ts) "
                "VALUES (?, ?, 'receipt_closure', 'ok', ?, ?)",
                (rotation_id, len(steps) + 1, outcome, closed_at),
            )
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise
        receipt = RotationReceipt(
            rotation_id=rotation_id,
            credential_id=credential_id,
            env_name=env_name,
            from_version_id=from_version_id,
            to_version_id=to_version_id,
            outcome=outcome,
            provider_revoke_state=provider_revoke_state,
            steps=steps,
            receipt_digest=receipt_digest,
            closed_at=closed_at,
        )
        assert_name_only(receipt.as_dict(), where="rotation receipt")
        _LOG.info(
            "secret_rotation closed rotation_id=%s env_name=%s outcome=%s provider_revoke=%s",
            rotation_id,
            env_name,
            outcome,
            provider_revoke_state,
        )
        return receipt

    def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        self._store._begin()
        try:
            self._conn.execute(sql, params)
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise

    # --- operator reads -----------------------------------------------------

    @_serialized
    def rotations(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recent ceremonies, newest first. Name-only, checked before it returns."""
        rows = [
            dict(row)
            for row in self._conn.execute(
                "SELECT r.*, c.env_name FROM secret_rotations r "
                "JOIN secret_catalog c ON c.credential_id = r.credential_id "
                "ORDER BY r.opened_at DESC, r.rotation_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]
        assert_name_only(rows, where="rotation status")
        return rows


class OperatorCeremonyAdapter:
    """Drive a rotation from a terminal, with a human doing the out-of-band work.

    This adapter is the honest shape of a rotation in this estate today: there
    is no provider automation to trust, so the operator mints and installs the
    new version themselves and this process only sequences, verifies, and
    receipts the ceremony. It NEVER prompts for a credential value and has
    nowhere to put one -- every prompt is a yes/no question about work the
    operator has already done elsewhere.
    """

    def __init__(
        self,
        *,
        prompt: Callable[[str], str] = input,
        out: Callable[[str], None] = print,
    ) -> None:
        self._prompt = prompt
        self._out = out

    def _confirm(self, question: str) -> bool:
        return self._prompt(f"{question} [y/N] ").strip().lower() in {"y", "yes"}

    def stage(self, credential_id: str, version_id: str) -> None:
        self._out(
            f"Stage the new version for {credential_id} in the provider console and in the "
            f"local store, recording it as {version_id}. Do NOT paste the value here."
        )
        if not self._confirm("Has the new version been staged?"):
            raise RotationRefused("stage_not_confirmed")

    def probe(self, credential_id: str, version_id: str) -> bool:
        self._out(f"Exercise one read capability of {credential_id} using {version_id}.")
        return self._confirm("Did the probe succeed?")

    def canary(self, credential_id: str, old_version_id: str, new_version_id: str) -> bool:
        self._out(
            f"Run {credential_id} with {old_version_id} and {new_version_id} both live, "
            "then judge the new one."
        )
        return self._confirm("Is the new version healthy?")

    def revoke_at_provider(self, credential_id: str, version_id: str) -> None:
        self._out(f"Revoke {version_id} of {credential_id} at the provider. IRREVERSIBLE.")
        if not self._confirm("Has the old version been revoked at the provider?"):
            raise RotationRefused("provider_revoke_not_confirmed")


def main(argv: list[str] | None = None) -> int:
    """Operator entry point: ``python -m omniagentos.security.secret_rotation``.

    ``rotate`` runs the ceremony with :class:`OperatorCeremonyAdapter`, so the
    human performs each external step and this process sequences and receipts
    it. ``--arm-provider-revoke`` supplies only ONE of the two switches; the
    step stays off unless ``OMNIAGENTOS_ROTATION_PROVIDER_REVOKE=ARMED`` is also
    set in the environment by a human who meant it.
    """
    parser = argparse.ArgumentParser(description="Credential rotation ceremony (U-S2).")
    parser.add_argument("command", choices=("rotate", "status"))
    parser.add_argument("--env-name", default="", help="the credential NAME to rotate")
    parser.add_argument("--operator", default="", help="canonical human:<name> spelling")
    parser.add_argument("--db", default=None)
    parser.add_argument("--backup-retention-days", type=int, default=_DEFAULT_BACKUP_RETENTION_DAYS)
    parser.add_argument(
        "--arm-provider-revoke",
        action="store_true",
        help=(
            "operator approval for the [OPERATOR]-gated provider revoke (D-05). One of two "
            f"switches; {PROVIDER_REVOKE_ENV}={PROVIDER_REVOKE_ARMED_VALUE} is the other."
        ),
    )
    args = parser.parse_args(argv)

    with open_catalog(args.db) as catalog:
        engine = RotationEngine(catalog._store)
        if args.command == "status":
            print(json.dumps(engine.rotations(), indent=2))
            return 0
        if not args.env_name or not args.operator:
            parser.error("rotate requires --env-name and --operator")
        receipt = engine.rotate(
            args.env_name,
            OperatorCeremonyAdapter(),
            operator=args.operator,
            backup_retention_days=args.backup_retention_days,
            provider_revoke_operator_approval=args.arm_provider_revoke,
        )
        print(json.dumps(receipt.as_dict(), indent=2))
    # Anything short of a completed switch is a non-zero exit: an aborted probe
    # and a rolled-back canary are both "the credential was NOT rotated".
    return 0 if receipt.outcome == "succeeded" else 1


if __name__ == "__main__":  # pragma: no cover - exercised by module invocation
    raise SystemExit(main())


__all__ = [
    "PROVIDER_REVOKE_ARMED_VALUE",
    "PROVIDER_REVOKE_ENV",
    "ROTATABLE_STATES",
    "ROTATION_STEPS",
    "OperatorCeremonyAdapter",
    "RotationAdapter",
    "RotationEngine",
    "RotationReceipt",
    "RotationRefused",
    "main",
]
