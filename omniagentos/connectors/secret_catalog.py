"""The name-only secret catalog (U-S2 / S3-W3).

WHAT THIS IS FOR

Before this module the only question the credential path could answer was "is
the value in the environment?". That is a presence check, not a disposition:
it cannot tell a name the operator has deliberately parked from a name nobody
has ever looked at, and it cannot refuse a credential that should no longer be
used while keeping the evidence about it readable. This catalog is the missing
half -- one row per credential NAME with a STATE, and a resolution-time consult
so the state is enforced rather than merely recorded.

THE NAME-ONLY RULE IS STRUCTURAL, NOT A CONVENTION

Migration 109 gives no table here a value, ciphertext, or fingerprint column,
and every write passes through :func:`_refuse_value_shaped`. A catalog that
could hold a value would eventually hold one, and a dump of the credential
inventory would become a dump of the credentials.

THE SIX STATES

    missing      -- declared by the registry, absent from the environment. A
                    MARK, never an invention (D-33): the row exists so the gap
                    is visible; provisioning the name is a separate named [OPERATOR]
                    decision and nothing here writes a placeholder value.
    quarantined  -- present in the vault, referenced by nothing in the repo.
                    Refused for resolution, PRESERVED for review. Repo absence
                    is not proof that provider-side use is dead, so this state
                    deliberately does not delete, revoke, or expire anything.
    active       -- resolvable.
    rotating     -- a rotation ceremony holds this credential (see
                    ``omniagentos/security/secret_rotation.py``).
    retired      -- superseded and no longer resolved, but still recoverable.
    revoked      -- dead at the provider. Terminal: a revoked credential is
                    never reinstated, only replaced.

THE BALANCE RULE

A catalog FAULT must never take down credential resolution for healthy names.
:func:`resolution_denial` therefore fails OPEN on every error path -- unreadable
database, absent table, corrupt row -- with a loud log, and refuses only on a
row it actually read and understood. The catalog is authority over disposition,
not over existence: a name with no row resolves exactly as it did before this
module existed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Any, cast

from omniagentos.contracts import default_db_path, utc_now_iso

_LOG = logging.getLogger(__name__)

#: Every state a catalog row may hold, in lifecycle order.
CATALOG_STATES: tuple[str, ...] = (
    "missing",
    "quarantined",
    "active",
    "rotating",
    "retired",
    "revoked",
)

#: States whose rows are refused at resolution, mapped to their denial code.
#:
#: They are two codes and not one because the two refusals have DIFFERENT and
#: incompatible remedies, and a caller routes on the code alone:
#:
#:   * ``credential_quarantined`` is reversible. The name may be perfectly
#:     readable; we are declining to use it until a named owner dispositions it.
#:     Reinstating it is a legitimate outcome.
#:   * ``credential_revoked`` is terminal. The version is dead at the provider,
#:     and "reinstate it" is exactly the action that must never happen -- the
#:     same rule that makes a failed canary refuse to resurrect a revoked
#:     version. Collapsing the two would put "try turning it back on" on the
#:     remedy path of a credential that cannot come back.
REFUSING_STATES: dict[str, str] = {
    "quarantined": "credential_quarantined",
    "revoked": "credential_revoked",
}

#: Seconds a resolved state map is trusted before it is re-read. The credential
#: path calls into here on every resolution, so an uncached read would put a
#: SQLite open on the hot path; a rotation invalidates the cache explicitly
#: through :func:`invalidate_cache` rather than waiting this out.
_CACHE_TTL_S = 30.0

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Shape tripwires for material that must never appear in a catalog field or in
#: catalog output. This is a TRIPWIRE, not a proof: it catches the credential
#: shapes this estate actually issues plus obviously high-entropy mixed-case
#: tokens, and it deliberately does not try to recognise every possible secret.
#: The structural guarantee is migration 109's lack of a value column; this is
#: the second layer that keeps a well-meaning writer from smuggling one into a
#: note field.
_VALUE_SHAPED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@"),
    # Long mixed-case high-entropy run. Requires upper AND lower AND digit so
    # that SCREAMING_ENV_NAMES and the lowercase-hex ids minted by
    # ``contracts.new_id`` cannot trip it -- a tripwire that fires on the
    # module's own identifiers would be turned off within a week.
    re.compile(
        r"(?=[A-Za-z0-9+/=_-]*[a-z])(?=[A-Za-z0-9+/=_-]*[A-Z])"
        r"(?=[A-Za-z0-9+/=_-]*[0-9])[A-Za-z0-9+/=_-]{32,}"
    ),
)

_state_cache: dict[str, tuple[float, dict[str, str]]] = {}


class CatalogRefused(RuntimeError):
    """A catalog write was refused. Never carries the offending value."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SyncSummary:
    """Name-only result of one catalog sync."""

    inserted: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": list(self.inserted),
            "updated": list(self.updated),
            "unchanged": list(self.unchanged),
            "preserved": list(self.preserved),
            "counts": {
                "inserted": len(self.inserted),
                "updated": len(self.updated),
                "unchanged": len(self.unchanged),
                "preserved": len(self.preserved),
            },
        }


def _refuse_value_shaped(field: str, text: str) -> None:
    """Refuse a catalog field whose content looks like credential material."""
    for pattern in _VALUE_SHAPED_PATTERNS:
        if pattern.search(text):
            # The offending text is NOT echoed: a refusal that quotes the value
            # writes it into the log it was trying to keep it out of.
            raise CatalogRefused(
                "value_shaped_field",
                f"{field} matched a credential-shape tripwire and was refused",
            )


def assert_name_only(payload: Any, *, where: str = "catalog output") -> None:
    """Raise :class:`CatalogRefused` if ``payload`` renders any value-shaped text.

    Applied to everything this module returns to an operator surface, so the
    name-only guarantee is checked at the boundary rather than assumed from the
    schema. It is exported because the rotation module renders receipts through
    the same rule.
    """
    _refuse_value_shaped(where, json.dumps(payload, sort_keys=True, default=str))


def _validated_text(field: str, value: object) -> str:
    text = "" if value is None else str(value)
    _refuse_value_shaped(field, text)
    return text


def _validated_env_name(env_name: str) -> str:
    if not _ENV_NAME_RE.fullmatch(env_name):
        # Length only: a rejected "name" that is really a value must not be
        # echoed into the exception that refused it.
        raise CatalogRefused(
            "invalid_env_name",
            f"<{len(env_name)}-character string> is not an environment-variable name",
        )
    _refuse_value_shaped("env_name", env_name)
    return env_name


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Serialize access through the composed store's writer lock (as store.py)."""

    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        catalog = cast("SecretCatalog", args[0])
        with catalog._store._lock:
            return method(*args, **kwargs)

    return wrapped


def _effect_class(action_classes: Iterable[str]) -> str:
    classes = set(action_classes)
    if not classes:
        return "unknown"
    reads = {"read_only"}
    if classes <= reads:
        return "read"
    if classes.isdisjoint(reads):
        return "write"
    return "read_write"


class SecretCatalog:
    """Read and write the name-only credential catalog.

    Composes an open :class:`~omniagentos.db.store.SqliteStore`, matching
    ``connectors/store.py``. Every method here is metadata-only by construction:
    there is no parameter on this class that accepts a credential value.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._store._connection

    # --- reads -------------------------------------------------------------

    @_serialized
    def get(self, env_name: str) -> dict[str, Any] | None:
        """One catalog row by env NAME, or ``None``."""
        row = self._conn.execute(
            "SELECT * FROM secret_catalog WHERE env_name = ?", (env_name,)
        ).fetchone()
        return dict(row) if row is not None else None

    @_serialized
    def rows(self, *, state: str | None = None) -> list[dict[str, Any]]:
        """Every catalog row, optionally filtered by state, ordered by name."""
        if state is None:
            found = self._conn.execute("SELECT * FROM secret_catalog ORDER BY env_name").fetchall()
        else:
            found = self._conn.execute(
                "SELECT * FROM secret_catalog WHERE state = ? ORDER BY env_name", (state,)
            ).fetchall()
        return [dict(row) for row in found]

    @_serialized
    def state_map(self) -> dict[str, str]:
        """``{env_name: state}`` for every row."""
        return {
            str(row["env_name"]): str(row["state"])
            for row in self._conn.execute("SELECT env_name, state FROM secret_catalog").fetchall()
        }

    @_serialized
    def report(self) -> dict[str, Any]:
        """Operator-facing, name-only summary. Checked before it is returned."""
        rows = self.rows()
        # Built as named locals rather than by mutating through the payload
        # dict: a heterogeneous literal widens to dict[str, object], and every
        # write below then has to be cast back. Same shape, same key order.
        counts: dict[str, int] = {state: 0 for state in CATALOG_STATES}
        names: dict[str, list[str]] = {state: [] for state in CATALOG_STATES}
        no_broker_evidence_of_use: list[str] = []
        for row in rows:
            state = str(row["state"])
            counts[state] = counts.get(state, 0) + 1
            names.setdefault(state, []).append(str(row["env_name"]))
            if not str(row["last_used_at"]):
                no_broker_evidence_of_use.append(str(row["env_name"]))
        payload: dict[str, Any] = {
            "generated_at": utc_now_iso(),
            "total": len(rows),
            "counts": counts,
            "names": names,
            "no_broker_evidence_of_use": no_broker_evidence_of_use,
        }
        assert_name_only(payload, where="catalog report")
        return payload

    @_serialized
    def version(self, version_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM secret_versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    @_serialized
    def versions_for(self, credential_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT * FROM secret_versions WHERE credential_id = ? ORDER BY created_at, "
                "version_id",
                (credential_id,),
            ).fetchall()
        ]

    # --- writes ------------------------------------------------------------

    @_serialized
    def upsert(
        self,
        *,
        credential_id: str,
        env_name: str,
        state: str,
        provider_family: str = "",
        risk_domain: str = "",
        store_shard: str = "",
        capability_refs: str = "",
        effect_class: str = "unknown",
        owner: str = "",
        recovery_dependency: str = "",
        shared_owner_marked: bool = False,
        disposition_note: str = "",
        rotation_due_at: str = "",
    ) -> None:
        """Insert or refresh one row. Operator-owned fields are set only on insert.

        ``last_used_at`` has no parameter on purpose: the only writer for it is
        :meth:`refresh_last_used`, reading the broker audit spine. A method that
        accepted it here would be an invitation to fill the column with a guess.
        """
        if state not in CATALOG_STATES:
            raise CatalogRefused("unknown_state", f"{state!r} is not a catalog state")
        fields = {
            "credential_id": _validated_text("credential_id", credential_id),
            "env_name": _validated_env_name(env_name),
            "provider_family": _validated_text("provider_family", provider_family),
            "risk_domain": _validated_text("risk_domain", risk_domain),
            "store_shard": _validated_text("store_shard", store_shard),
            "capability_refs": _validated_text("capability_refs", capability_refs),
            "effect_class": effect_class,
            "owner": _validated_text("owner", owner),
            "recovery_dependency": _validated_text("recovery_dependency", recovery_dependency),
            "disposition_note": _validated_text("disposition_note", disposition_note),
            "rotation_due_at": _validated_text("rotation_due_at", rotation_due_at),
        }
        now = utc_now_iso()
        self._store._begin()
        try:
            self._conn.execute(
                "INSERT INTO secret_catalog (credential_id, env_name, provider_family, "
                "risk_domain, store_shard, capability_refs, effect_class, state, owner, "
                "recovery_dependency, shared_owner_marked, disposition_note, rotation_due_at, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(env_name) DO UPDATE SET "
                "provider_family = excluded.provider_family, "
                "risk_domain = excluded.risk_domain, "
                "store_shard = excluded.store_shard, "
                "capability_refs = excluded.capability_refs, "
                "effect_class = excluded.effect_class, "
                "state = excluded.state, "
                "updated_at = excluded.updated_at",
                (
                    fields["credential_id"],
                    fields["env_name"],
                    fields["provider_family"],
                    fields["risk_domain"],
                    fields["store_shard"],
                    fields["capability_refs"],
                    fields["effect_class"],
                    state,
                    fields["owner"],
                    fields["recovery_dependency"],
                    1 if shared_owner_marked else 0,
                    fields["disposition_note"],
                    fields["rotation_due_at"],
                    now,
                    now,
                ),
            )
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise
        invalidate_cache()

    @_serialized
    def set_operator_fields(
        self,
        env_name: str,
        *,
        owner: str | None = None,
        shared_owner_marked: bool | None = None,
        recovery_dependency: str | None = None,
        rotation_due_at: str | None = None,
        disposition_note: str | None = None,
    ) -> None:
        """Record the human judgement a sync pass must never overwrite."""
        updates: dict[str, Any] = {}
        if owner is not None:
            updates["owner"] = _validated_text("owner", owner)
        if shared_owner_marked is not None:
            updates["shared_owner_marked"] = 1 if shared_owner_marked else 0
        if recovery_dependency is not None:
            updates["recovery_dependency"] = _validated_text(
                "recovery_dependency", recovery_dependency
            )
        if rotation_due_at is not None:
            updates["rotation_due_at"] = _validated_text("rotation_due_at", rotation_due_at)
        if disposition_note is not None:
            updates["disposition_note"] = _validated_text("disposition_note", disposition_note)
        if not updates:
            return
        assignments = ", ".join(f"{column} = ?" for column in updates)
        self._store._begin()
        try:
            self._conn.execute(
                f"UPDATE secret_catalog SET {assignments}, updated_at = ? WHERE env_name = ?",
                (*updates.values(), utc_now_iso(), env_name),
            )
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise

    @_serialized
    def set_state(
        self,
        env_name: str,
        state: str,
        *,
        actor: str,
        note: str = "",
        owner_ack: bool = False,
    ) -> None:
        """Move one credential to ``state``.

        ``owner_ack`` is required to revoke a credential the operator has marked
        as shared with a system outside this repo. That marking is the operator
        saying "repo absence proves nothing about this one", and the counterfeit
        this guard exists for is an automated pass reading repo-unreferenced as
        dead and revoking a live shared key. No sync path passes ``owner_ack``;
        only a human at a terminal does.
        """
        if state not in CATALOG_STATES:
            raise CatalogRefused("unknown_state", f"{state!r} is not a catalog state")
        row = self.get(env_name)
        if row is None:
            raise CatalogRefused("unknown_credential", "no catalog row for this name")
        if state == "revoked" and int(row["shared_owner_marked"] or 0) and not owner_ack:
            raise CatalogRefused(
                "shared_credential_requires_owner_ack",
                "this credential is operator-marked as shared; revocation needs a named owner",
            )
        note_text = _validated_text("disposition_note", note)
        self._store._begin()
        try:
            self._conn.execute(
                "UPDATE secret_catalog SET state = ?, disposition_note = ?, updated_at = ? "
                "WHERE env_name = ?",
                (
                    state,
                    note_text or str(row["disposition_note"] or ""),
                    utc_now_iso(),
                    env_name,
                ),
            )
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise
        _LOG.info(
            "secret_catalog state change env_name=%s from=%s to=%s actor=%s",
            env_name,
            row["state"],
            state,
            actor,
        )
        invalidate_cache()

    @_serialized
    def sync_from_registry(
        self,
        registry: Any,
        environ: Mapping[str, object],
        quarantine_names: Iterable[str],
        *,
        store_shard: str = "",
    ) -> SyncSummary:
        """Reconcile the catalog against the registry, the environment, and ARCHIVE.

        Three inputs, three dispositions:

        * a registry-declared name present in ``environ`` -> ``active``;
        * a registry-declared name absent from ``environ`` -> ``missing`` (D-33:
          marked, never invented -- no placeholder value, no version row);
        * an ``ARCHIVE``-bucket orphan from ``connectors/inventory.py`` ->
          ``quarantined``, refused for resolution and preserved for review.

        This pass NEVER deletes a row, never writes ``revoked``, and never
        overwrites an operator-owned field. Those three restrictions are what
        make it safe to run unattended against a vault whose orphans may still
        be live somewhere outside this repo.
        """
        quarantine = {str(name) for name in quarantine_names}
        declared = self._declared_metadata(registry)
        overlap = sorted(quarantine & set(declared))
        if overlap:
            # inventory.py guarantees a name lands in exactly one bucket and that
            # a repo-referenced name is never ARCHIVE. Both are violated here, so
            # picking a winner would bury an inventory defect under a quiet
            # heuristic. Refuse and make the operator look.
            raise CatalogRefused(
                "bucket_conflict",
                f"{len(overlap)} name(s) are both registry-declared and ARCHIVE-quarantined: "
                + ", ".join(overlap),
            )

        inserted: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
        preserved: list[str] = []

        for env_name in sorted(set(declared) | quarantine):
            meta = declared.get(env_name)
            if meta is None:
                desired_state = "quarantined"
                credential_id = f"cred:orphan:{env_name}"
                provider_family = ""
                risk_domain = "unclassified"
                capability_refs = ""
                effect_class = "unknown"
            else:
                present = env_name in environ and str(environ[env_name] or "") != ""
                desired_state = "active" if present else "missing"
                credential_id = f"cred:{meta['connector']}:{env_name}"
                provider_family = meta["connector"]
                risk_domain = meta["group"]
                capability_refs = ",".join(meta["capabilities"])
                effect_class = meta["effect_class"]

            existing = self.get(env_name)
            if existing is not None:
                keep = self._preserved_state(
                    str(existing["state"]), desired_state, meta is not None
                )
                if keep is not None:
                    preserved.append(env_name)
                    continue
            self.upsert(
                credential_id=credential_id,
                env_name=env_name,
                state=desired_state,
                provider_family=provider_family,
                risk_domain=risk_domain,
                store_shard=store_shard,
                capability_refs=capability_refs,
                effect_class=effect_class,
            )
            if existing is None:
                inserted.append(env_name)
            elif str(existing["state"]) != desired_state:
                updated.append(env_name)
            else:
                unchanged.append(env_name)

        return SyncSummary(
            inserted=tuple(inserted),
            updated=tuple(updated),
            unchanged=tuple(unchanged),
            preserved=tuple(preserved),
        )

    @staticmethod
    def _preserved_state(current: str, desired: str, is_declared: bool) -> str | None:
        """Return the state to KEEP, or ``None`` to let the sync write ``desired``.

        Three rules, each for a different failure this pass could otherwise
        cause:

        * ``rotating`` is held by the rotation state machine. A sync that reset
          it mid-ceremony would move the active pointer out from under a canary.
        * ``revoked`` and ``retired`` are operator dispositions. Re-deriving
          ``active`` from "the value happens to be in the environment" would
          silently un-revoke a key, which is the single worst thing this module
          could do.
        * ``quarantined`` is reversible, but only by evidence: the name becoming
          registry-declared IS the review (a declared name is repo-referenced by
          definition, and the inventory invariant says a repo-referenced name is
          never ARCHIVE). An undeclared name stays quarantined no matter what
          the environment says.
        """
        if current in {"rotating", "revoked", "retired"}:
            return current
        if current == "quarantined" and not is_declared:
            return current
        return None

    @staticmethod
    def _declared_metadata(registry: Any) -> dict[str, dict[str, Any]]:
        """Map every registry-declared env NAME to its connector metadata.

        A name declared by more than one connector is owned by the first in
        sorted order and accumulates the union of their capability references,
        so the credential id is deterministic across runs rather than dependent
        on dict ordering.
        """
        out: dict[str, dict[str, Any]] = {}
        for connector_key in sorted(registry.connectors):
            connector = registry.connectors[connector_key]
            cap_ids = sorted(cap.id for cap in connector.capabilities.values())
            action_classes = [
                cap.action_class.value
                if hasattr(cap.action_class, "value")
                else str(cap.action_class)
                for cap in connector.capabilities.values()
            ]
            for env_name in connector.env:
                entry = out.get(env_name)
                if entry is None:
                    out[env_name] = {
                        "connector": connector_key,
                        "group": connector.group,
                        "capabilities": cap_ids,
                        "effect_class": _effect_class(action_classes),
                    }
                else:
                    entry["capabilities"] = sorted(set(entry["capabilities"]) | set(cap_ids))
        return out

    @_serialized
    def refresh_last_used(self) -> int:
        """Fill ``last_used_at`` from the broker audit spine. Returns rows touched.

        ``broker_calls`` is the ONLY source. A resolution that the broker allowed
        is the only evidence this system has that a credential was actually read;
        an ``intent`` row means an attempt, and a denial means no value was
        touched at all, so neither counts as use. A name with no allowed row
        keeps an empty ``last_used_at``, which means "no broker evidence" and
        NOT "never used" -- nothing may retire a credential on that emptiness.
        """
        try:
            rows = self._conn.execute(
                "SELECT env_name, MAX(ts) AS last_ts FROM broker_calls "
                "WHERE decision = 'allowed' AND env_name != '' GROUP BY env_name"
            ).fetchall()
        except sqlite3.OperationalError:
            _LOG.warning("secret_catalog: broker_calls unavailable; last_used left unchanged")
            return 0

        # The audit spine records a connector's whole declared name scope in one
        # comma-joined column, so one row is evidence for several names.
        latest: dict[str, str] = {}
        for row in rows:
            stamp = str(row["last_ts"] or "")
            if not stamp:
                continue
            for name in str(row["env_name"]).split(","):
                name = name.strip()
                if name and stamp > latest.get(name, ""):
                    latest[name] = stamp
        if not latest:
            return 0

        touched = 0
        self._store._begin()
        try:
            for name, stamp in sorted(latest.items()):
                cursor = self._conn.execute(
                    "UPDATE secret_catalog SET last_used_at = ?, updated_at = ? "
                    "WHERE env_name = ? AND last_used_at < ?",
                    (stamp, utc_now_iso(), name, stamp),
                )
                touched += cursor.rowcount if cursor.rowcount > 0 else 0
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise
        return touched


@contextmanager
def open_catalog(db_path: str | None = None) -> Iterator[SecretCatalog]:
    """Open a short-lived, migrated catalog handle on the control-plane DB."""
    from omniagentos.db.store import SqliteStore

    store = SqliteStore(db_path or default_db_path())
    try:
        yield SecretCatalog(store)
    finally:
        store.close()


def invalidate_cache() -> None:
    """Drop the resolution-state cache.

    Called by the rotation state machine's cache/grant invalidation step so a
    pointer switch or a revocation takes effect on the credential path at once
    rather than at the end of a TTL.
    """
    _state_cache.clear()


def _load_state_map(db_path: str) -> dict[str, str]:
    """Read ``{env_name: state}`` with one bare SELECT and no migration.

    Deliberately NOT via :class:`SqliteStore`: the credential path must not be
    able to trigger a schema migration, and a database that predates migration
    109 has no catalog -- which is a pre-catalog state, not a fault, and must
    resolve exactly as it did before.

    Also deliberately not a ``mode=ro`` URI. A read-only connection to a WAL
    database needs the ``-shm`` file to already exist, so the moment no writer
    is attached the catalog would fail to open and every consult would take the
    fail-open path -- a security control that silently switches itself off is
    worse than one that is absent. This connection issues nothing but SELECT.
    """
    if not os.path.exists(db_path):
        return {}
    connection = sqlite3.connect(db_path, timeout=0.5)
    try:
        rows = connection.execute("SELECT env_name, state FROM secret_catalog").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {}
        raise
    finally:
        connection.close()
    return {str(name): str(state) for name, state in rows}


def _cached_state_map() -> dict[str, str]:
    db_path = default_db_path()
    cached = _state_cache.get(db_path)
    now = time.monotonic()
    if cached is not None and now < cached[0]:
        return cached[1]
    resolved = _load_state_map(db_path)
    _state_cache[db_path] = (now + _CACHE_TTL_S, resolved)
    return resolved


def resolution_denial(env_name: str) -> str:
    """Return the denial code for ``env_name``, or ``""`` to allow resolution.

    THE FAIL-OPEN IS DELIBERATE AND IS THE BALANCE RULE. This function is on the
    live credential path for every brokered call in the estate. A catalog fault
    -- unreadable file, locked database, corrupt row, a schema this build does
    not know -- must degrade to the behaviour that existed before the catalog,
    with a loud log, because the alternative is that one bad row on the
    control-plane DB refuses every outbound credential use on the box. Only a
    row that was READ and UNDERSTOOD refuses, and a name with no row is not this
    module's business.
    """
    try:
        state = _cached_state_map().get(env_name, "")
    except Exception as exc:  # noqa: BLE001 -- see the fail-open note above.
        _LOG.error(
            "secret_catalog_unavailable env_name=%s error=%s; credential resolution "
            "continues on pre-catalog behaviour",
            env_name,
            type(exc).__name__,
        )
        return ""
    return REFUSING_STATES.get(state, "")


def _archive_names_from(path: str) -> list[str]:
    """Read the ARCHIVE bucket from an inventory report written by ``inventory.py``.

    The report is the artifact the pipeline actually produces (``make
    secrets-inventory``), and taking it whole is the point: the ARCHIVE bucket
    is the output of the four-way classifier, including its
    repo-referenced-never-ARCHIVE invariant. A hand-typed name list would let
    somebody quarantine a live credential with a typo, so it is accepted only as
    an explicit fallback for a machine with no report on it.
    """
    raw = open(path, encoding="utf-8").read()
    try:
        payload = json.loads(raw)
    except ValueError:
        return [line.strip() for line in raw.splitlines() if line.strip()]
    names = payload.get("details", {}).get("ARCHIVE")
    if not isinstance(names, list):
        raise CatalogRefused(
            "unusable_inventory_report",
            "the report has no details.ARCHIVE list; refusing to guess the bucket",
        )
    return [str(name) for name in names]


def main(argv: list[str] | None = None) -> int:
    """Operator entry point: ``python -m omniagentos.connectors.secret_catalog``.

    ``sync`` reconciles the catalog against the registry, this process's
    environment, and the ARCHIVE bucket of an inventory report produced by
    ``connectors/inventory.py``. ``report`` prints name-only counts and states.
    Both refuse to print anything that trips the value-shape tripwire.
    """
    parser = argparse.ArgumentParser(description="Name-only secret catalog (U-S2).")
    parser.add_argument("command", choices=("sync", "report", "refresh-last-used"))
    parser.add_argument("--db", default=None, help="control-plane database path")
    parser.add_argument(
        "--inventory-report",
        type=str,
        default="",
        help=(
            "path to a var/secrets-inventory-*.json report; its ARCHIVE bucket becomes "
            "the quarantined set (a newline-separated name list is also accepted)"
        ),
    )
    args = parser.parse_args(argv)

    from omniagentos.connectors import load_registry

    quarantine: list[str] = []
    if args.inventory_report:
        quarantine = _archive_names_from(args.inventory_report)

    with open_catalog(args.db) as catalog:
        if args.command == "sync":
            summary = catalog.sync_from_registry(load_registry(), os.environ, quarantine)
            payload = summary.as_dict()
            assert_name_only(payload, where="catalog sync summary")
            print(json.dumps(payload, indent=2))
        elif args.command == "refresh-last-used":
            print(json.dumps({"rows_touched": catalog.refresh_last_used()}, indent=2))
        else:
            print(json.dumps(catalog.report(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by module invocation
    raise SystemExit(main())


__all__ = [
    "CATALOG_STATES",
    "REFUSING_STATES",
    "CatalogRefused",
    "SecretCatalog",
    "SyncSummary",
    "assert_name_only",
    "invalidate_cache",
    "main",
    "open_catalog",
    "resolution_denial",
]
