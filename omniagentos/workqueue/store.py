"""WorkQueueStore — the shared pool's only writer of truth.

Every guarantee this queue makes is implemented here, in SQLite, in one process
on the primary Mac. The server (``server.py``) and the HTTP client
(``client.py``) are transports over this surface and contain no logic of their
own.

Why TTL-expiry reclaim is CORRECT here, and why the next reader must not
"fix" it back to fail-closed (SPEC-shared-queue §3.4)
--------------------------------------------------------------------------
The estate's lane pool states the opposite rule, and is right about ITS
resource:

    "a lease recorded by a foreign host is never reclaimed, no matter its age.
     We have no local way to probe whether that pid is alive on the OTHER
     machine, and assuming 'can't see it locally' means 'dead' is exactly the
     fail-open bug being fixed here."

That is correct for a WORKTREE — a shared mutable directory on one filesystem
where two writers corrupt each other. It is WRONG for a work UNIT, and applying
it to a multi-machine pool would deadlock the pool the first time a Mac loses
power: nothing on this estate can ever prove a foreign pid is dead, so no lease
would ever be reclaimed and the queue would stall while looking healthy.

A unit is safe to reclaim on TTL because a unit is **re-executable by
reconstruction**: it is fully described by ``(repo_url, base_sha, brief,
agent_profile, owned_paths)`` and every execution starts from a fresh checkout
at ``base_sha``. The danger is not "two machines run it" — it is "two machines
both RECORD a result". That danger is closed by the fencing generation (every
result-writing statement carries the generation the worker believes it holds; a
mismatch means "you were adopted, stop") and by ``UNIQUE (unit_id, attempt)``,
**not** by proving death. Do not convert this to fail-closed reclaim.

Attempt accounting — the one place this design can silently break its promise
--------------------------------------------------------------------------
``claim`` increments ``wq_units.attempt`` unconditionally, because a claim that
decided nothing about the outcome must still be counted the moment it happens.
The promise that an instrument failure never consumes a candidate's attempt
budget is therefore kept at RESULT time, in the same fenced UPDATE that writes
the outcome: the six reversing outcomes in
``schema.NON_CONSUMING_OUTCOMES`` decrement it back, and the first three also
increment ``instrument_retries``. ``timeout`` is deliberately NOT one of them —
the unit's own ``timeout_s`` bounds the work, so exceeding it is evidence about
the candidate, not about the box.

Two attempt numbers, on purpose
--------------------------------------------------------------------------
``wq_units.attempt`` is the candidate's **budget counter** (it goes down again
on a reversing outcome). ``wq_attempts.attempt`` is the **execution ordinal**:
``MAX(attempt)+1`` for that unit, allocated inside the claim transaction and
never reused. They coincide whenever every execution consumed budget, which is
the common case. They must not be the same number, for two independent reasons:

1. If the ordinal were the budget counter, a unit that reversed its increment
   would re-allocate an ordinal it already used and hit ``UNIQUE(unit_id,
   attempt)`` on its NEXT claim — the five-instrument-error case in
   ``tests/workqueue/test_attempt_accounting.py`` would wedge the unit forever.
2. SPEC §6's double-execution query joins attempts on ``a.attempt < b.attempt``;
   it only means anything if ordinals are strictly increasing.

``UNIQUE (unit_id, attempt)`` keeps its full force as the double-record alarm:
allocation happens under ``BEGIN IMMEDIATE``, so a collision can only mean two
writers believed they held the write lock at once (the shared-network-filesystem
trap in SPEC §7). If it fires, the claim rolls back and returns ``None``.

Isolation: this module opens the queue DB with a LOCAL ``_connect`` clone (same
pragma set as ``omniagentos/db/store.py``). It deliberately does not import
``SqliteStore``, which is auto-migrated and would apply all 121 packaged
migrations into this file (SPEC §1.2).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omniagentos.workqueue.migrate import migrate_queue_connection
from omniagentos.workqueue.schema import (
    INSTRUMENT_BACKOFF_S,
    INSTRUMENT_RETRY_CAP,
    INSTRUMENT_RETRY_OUTCOMES,
    LEASE_TTL_S,
    NON_CONSUMING_OUTCOMES,
    REFUSAL_STORM_CAP,
    LeaseLost,
    Outcome,
    RiskClass,
    TerminalReason,
    UnitState,
    new_unit_id,
    utc_now_iso,
)

__all__ = ["WorkQueueStore", "LeaseLost"]

_SHA40 = re.compile(r"^[0-9a-f]{40}$")

#: Outcomes whose refusal-class must never overwrite the ORIGINAL cause in the
#: refusal ledger (§4.2, ``accurate-gate.py:342``).
_NON_OVERWRITING_CLASSES = frozenset({Outcome.UNCHANGED_RETRY.value, Outcome.STORM_PARKED.value})

_UNIT_JSON_COLUMNS = ("owned_paths", "labels")

#: How a unit with no ``submitted_by`` is reported. A name, not a blank, so an
#: unattributed backlog is something the operator SEES rather than something
#: that quietly falls out of the per-person block (§2 attribution).
UNATTRIBUTED = "(unattributed)"

#: The two machine roles (migration 002). A worker executes; a dispatcher only
#: submits and observes. the operator 2026-08-13: personal machines are dispatchers,
#: outgoing only — execution happens on fleet workers.
ROLE_WORKER = "worker"
ROLE_DISPATCHER = "dispatcher"

#: The two DISTINCT reasons a claim can be refused on role grounds. They are kept
#: apart on purpose: they call for opposite operator actions. The first says the
#: pool already knows this box is a dispatcher and it is behaving as designed;
#: the second says a row is claiming that nobody put on the allowlist — which,
#: before the enforcement flag is flipped, is exactly the preflight finding
#: RUNBOOK §12 asks the operator to go clean up.
REFUSAL_DISPATCHER_ROLE = "dispatcher-role-cannot-claim"
REFUSAL_NOT_ALLOWLISTED = "machine-not-allowlisted"

#: How long a read of ``configs/workqueue.yaml:worker_allowlist`` is reused before
#: the file is read again. The allowlist is checked at CLAIM time against the
#: CURRENT file, not against whatever was on disk when the server booted: adding a
#: machine must not need a server restart, and — far more important — REMOVING one
#: must take effect while the operator is still watching. 30 s is the bound on how
#: long a just-revoked machine can still claim; the alternative (re-reading on
#: every claim) puts a synchronous file read in the hot path of the busiest route.
#: ``store.config`` stays cached-for-the-process because it feeds one escalation
#: lookup; this one is authz and gets its own, deliberately shorter, lifetime.
WORKER_ALLOWLIST_TTL_S = 30.0

#: Live unit states → the ``offloads`` counter each one feeds. 'claimed' and
#: 'running' are one bucket: the difference is an internal step of the worker
#: lifecycle, and to the person waiting they are both "it is running somewhere".
_OFFLOAD_BUCKETS = {
    UnitState.QUEUED.value: "queued",
    UnitState.CLAIMED.value: "running",
    UnitState.RUNNING.value: "running",
    UnitState.REVIEW.value: "in_review",
}


# --------------------------------------------------------------------------
# connection + time helpers
# --------------------------------------------------------------------------


def _connect(db_path: str) -> sqlite3.Connection:
    """WAL + busy_timeout + foreign_keys + synchronous=NORMAL, on the queue DB.

    A local clone of ``omniagentos.db.store._connect`` on purpose: importing
    ``SqliteStore`` (which is what owns that helper's ecosystem) would
    auto-apply the packaged migration set into ``var/workqueue.sqlite3`` and
    recreate the exact ``122`` numbering collision the separate file exists to
    escape (SPEC §1.2).
    """
    if db_path != ":memory:":
        expanded = Path(db_path).expanduser()
        expanded.parent.mkdir(parents=True, exist_ok=True)
        db_path = str(expanded)
    connection = sqlite3.connect(
        db_path, isolation_level=None, timeout=5.0, check_same_thread=False
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection
    except BaseException:
        connection.close()
        raise


def _parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _shift(now_iso: str, seconds: float) -> str:
    return (_parse_iso(now_iso) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_s(then_iso: str | None, now_iso: str) -> float | None:
    if not then_iso:
        return None
    return (_parse_iso(now_iso) - _parse_iso(then_iso)).total_seconds()


def _json_list(value: Any) -> str:
    if value is None:
        return "[]"
    if isinstance(value, str):
        return value
    return json.dumps(list(value))


def _load_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _unit_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    data = _row_dict(row)
    if data is None:
        return None
    for column in _UNIT_JSON_COLUMNS:
        if column in data:
            data[column] = _load_list(data[column])
    return data


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "configs" / "workqueue.yaml"


# --------------------------------------------------------------------------


class WorkQueueStore:
    """Fenced, lease-based work queue over one SQLite file.

    Thread-safe: one connection guarded by an RLock. Cross-PROCESS safety comes
    from ``BEGIN IMMEDIATE`` plus ``busy_timeout``, not from the lock — the
    exclusivity test opens one store per thread precisely so it exercises the
    real thing.
    """

    def __init__(self, db_path: str | Path, *, config_path: str | Path | None = None) -> None:
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        self._connection = _connect(self._db_path)
        migrate_queue_connection(self._connection)
        self._config_path = Path(config_path) if config_path else _default_config_path()
        self._config: dict[str, Any] | None = None
        # Authz cache, separate from _config on purpose — see WORKER_ALLOWLIST_TTL_S.
        self._allowlist: frozenset[str] = frozenset()
        self._allowlist_read_at: float | None = None

    # -- plumbing ---------------------------------------------------------

    @property
    def db_path(self) -> str:
        return self._db_path

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> WorkQueueStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._connection.execute("COMMIT")

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.OperationalError:  # pragma: no cover - no transaction open
            pass

    @property
    def config(self) -> dict[str, Any]:
        """configs/workqueue.yaml, read once per store. Missing file is not fatal.

        The store consults it for exactly one decision (§4.6 lineage escalation);
        every other doctrine lives in code on purpose.
        """
        if self._config is None:
            data: dict[str, Any] = {}
            try:
                import yaml  # imported lazily: the store must work without PyYAML

                text = self._config_path.read_text(encoding="utf-8")
                loaded = yaml.safe_load(text)
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
            self._config = data
        return self._config

    # -- machine roles (D8a) ----------------------------------------------

    def worker_allowlist(self) -> frozenset[str]:
        """``configs/workqueue.yaml:worker_allowlist``, re-read every ``WORKER_ALLOWLIST_TTL_S``.

        The ONLY authority for which machine ids may execute. Ids are compared
        byte-for-byte against ``wq_machines.machine_id``: no case folding, no
        alias table, no prefix matching. 'acmeuni' is prose used in the runbook and
        the fleet comment block; the box's id is ``acmeuniversityredditprompts``,
        and a lenient comparison here is precisely how a prose alias becomes a
        permission.

        Fails CLOSED on every read problem — a missing file, unparseable YAML, a
        missing key, or a non-list value all yield the EMPTY set, which grants
        nothing. A config the server cannot read must never be the reason a
        machine is trusted. Non-``str`` entries are dropped for the same reason.
        """
        now = time.monotonic()
        last = self._allowlist_read_at
        if last is None or (now - last) >= WORKER_ALLOWLIST_TTL_S:
            entries: list[str] = []
            try:
                import yaml  # lazily, exactly as `config` does: PyYAML is optional

                loaded = yaml.safe_load(self._config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded.get("worker_allowlist")
                    if isinstance(raw, list):
                        entries = [item for item in raw if isinstance(item, str) and item.strip()]
            except Exception:
                entries = []
            self._allowlist = frozenset(entries)
            self._allowlist_read_at = now
        return self._allowlist

    def role_for(self, machine_id: str) -> str:
        """The role the SERVER derives for ``machine_id``. Never client-supplied.

        This is the whole enrollment rule: on the allowlist ⇒ ``'worker'``, and
        anything else ⇒ ``'dispatcher'``. There is no third answer and no path
        that takes a role from a request body, so a machine cannot elevate itself
        by asking — the worst a forged enroll can do is describe a dispatcher.
        """
        return ROLE_WORKER if machine_id in self.worker_allowlist() else ROLE_DISPATCHER

    def claim_role_refusal(self, machine_id: str) -> str | None:
        """Why ``machine_id`` may not claim, or ``None`` if it may.

        The DECISION lives here (the store is the only writer of truth and the
        only reader of the role column); whether a refusal becomes a 403 or a log
        line is the server's ``WQ_ROLE_ENFORCE`` call, because the enforcement
        MODE is an edge concern and the rollout needs to change it without
        touching this logic.

        Order matters. The stored role is checked FIRST because it is the fact
        the pool has already recorded about the box, so a machine enrolled as a
        dispatcher is told exactly that. The allowlist check then catches the
        rows the role column cannot speak for: every machine that enrolled before
        migration 002 carries the column DEFAULT ``'worker'`` regardless of who it
        is, so a legacy row would otherwise sail straight through. Those two cases
        want opposite operator actions, which is why they are two named reasons
        and not one.

        Reads the CURRENT allowlist (TTL-bounded, see ``worker_allowlist``), not a
        boot-time snapshot: revoking a machine must not require a server restart.
        ``device_class`` is deliberately not consulted — it is audit metadata, and
        nothing that decides anything may read it.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT role FROM wq_machines WHERE machine_id = ?", (machine_id,)
            ).fetchone()
        if row is not None and str(row["role"]) == ROLE_DISPATCHER:
            return REFUSAL_DISPATCHER_ROLE
        if machine_id not in self.worker_allowlist():
            # Covers both the unenrolled machine and the pre-002 row still
            # carrying the 'worker' default. A machine nobody put on the list
            # does not execute, whatever its row happens to say.
            return REFUSAL_NOT_ALLOWLISTED
        return None

    # -- enqueue ----------------------------------------------------------

    def enqueue(self, submit: dict[str, Any]) -> tuple[str, bool]:
        """Insert a unit. Returns ``(unit_id, deduped)``.

        ``idempotency_key`` is UNIQUE, so re-enqueueing the same key is a no-op
        that returns the existing id with ``deduped=True`` — never a duplicate
        unit and never an error.
        """
        for required in (
            "idempotency_key",
            "repo_url",
            "repo_slug",
            "base_sha",
            "branch",
            "owned_paths",
            "agent_profile",
            "acceptance_cmd",
            "risk_class",
        ):
            if not submit.get(required):
                raise ValueError(f"missing required field: {required}")
        base_sha = str(submit["base_sha"])
        if not _SHA40.fullmatch(base_sha):
            # A branch name here silently destroys the input fingerprint: two
            # machines resolving 'main' 90 seconds apart build different trees.
            raise ValueError(f"base_sha must be 40-hex, not a ref: {base_sha!r}")
        risk_class = str(submit["risk_class"])
        if risk_class not in {member.value for member in RiskClass}:
            raise ValueError(f"unknown risk_class: {risk_class!r}")
        owned_paths = _load_list(submit["owned_paths"])
        if not owned_paths:
            raise ValueError("owned_paths must list at least one repo-relative glob")

        now = utc_now_iso()
        key = str(submit["idempotency_key"])
        with self._lock:
            self._begin()
            try:
                existing = self._connection.execute(
                    "SELECT id FROM wq_units WHERE idempotency_key = ?", (key,)
                ).fetchone()
                if existing is not None:
                    self._commit()
                    return str(existing["id"]), True
                unit_id = str(submit.get("id") or new_unit_id())
                self._connection.execute(
                    """
                    INSERT INTO wq_units (
                        id, idempotency_key, created_at, updated_at,
                        repo_url, repo_slug, base_sha, base_ref, branch, owned_paths,
                        brief_inline, brief_path, submitted_by, agent_profile, timeout_s, labels,
                        risk_class, acceptance_cmd, acceptance_gate,
                        state, priority, max_attempts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        unit_id,
                        key,
                        now,
                        now,
                        str(submit["repo_url"]),
                        str(submit["repo_slug"]),
                        base_sha,
                        str(submit.get("base_ref") or "main"),
                        str(submit["branch"]),
                        _json_list(owned_paths),
                        submit.get("brief_inline"),
                        submit.get("brief_path"),
                        # Attribution is recorded, never inferred: a missing
                        # submitted_by stays empty and surfaces as
                        # '(unattributed)' in status(), because guessing an owner
                        # is worse than reporting that nobody claimed one.
                        str(submit.get("submitted_by") or ""),
                        str(submit["agent_profile"]),
                        int(submit.get("timeout_s") or 3600),
                        _json_list(submit.get("labels") or []),
                        risk_class,
                        str(submit["acceptance_cmd"]),
                        submit.get("acceptance_gate"),
                        UnitState.QUEUED.value,
                        int(submit.get("priority", 2)),
                        int(submit.get("max_attempts", 3)),
                    ),
                )
                self._commit()
                return unit_id, False
            except BaseException:
                self._rollback()
                raise

    # -- claim ------------------------------------------------------------

    def claim(
        self,
        machine_id: str,
        worker_id: str,
        labels: list[str] | None,
        now: str | None = None,
        reserved_for: str | None = None,
    ) -> dict[str, Any] | None:
        """Claim one unit under ``BEGIN IMMEDIATE`` (SPEC §3.1). ``None`` = nothing for you.

        Returns ``{"unit", "lease_generation", "lease_expires_at", "attempt"}``
        where ``attempt`` is the EXECUTION ORDINAL of the ``wq_attempts`` row
        written in this same transaction (see the module docstring — it is not
        ``unit["attempt"]``, which is the candidate's budget counter).

        Refuses to claim when the machine row says ``drain=1`` or its
        ``max_concurrent`` is already in flight. A machine with no enrolled row
        is NOT capacity-limited here: the store does not invent policy for a
        machine it has never met, and enrollment is where that policy is set.

        ``reserved_for`` is WORKER-DECLARED resource hygiene (never a server
        config-map): a worker that passes a non-empty owner string claims ONLY
        units whose ``submitted_by`` equals it. A worker self-restricting is
        always safe — it can only ever get FEWER units, never more — so a
        garbage value fails to the SAFE side (nothing claimed for a mismatched
        owner), not the fail-OPEN side the dropped config-map had. Absent, empty,
        blank, or non-``str`` ⇒ unrestricted, today's behaviour. NOT a security
        boundary: ``submitted_by`` is self-declared under the shared pool token,
        so a token-holder who submits AS the owner can still land work here — this
        prevents accidental contention, not deliberate forging (a hard boundary
        needs per-submitter auth tokens, out of scope).
        """
        now = now or utc_now_iso()
        # Coerce to the SAFE side: a non-str or blank owner restricts nothing.
        reserved_owner = reserved_for.strip() if isinstance(reserved_for, str) else ""
        owner = f"{machine_id}:{worker_id}"
        expires_at = _shift(now, LEASE_TTL_S)
        declared = set(labels or [])

        with self._lock:
            self._begin()
            try:
                machine = self._connection.execute(
                    "SELECT drain, max_concurrent FROM wq_machines WHERE machine_id = ?",
                    (machine_id,),
                ).fetchone()
                if machine is not None:
                    if int(machine["drain"]) == 1:
                        self._rollback()
                        return None
                    prefix = f"{machine_id}:"
                    in_flight = int(
                        self._connection.execute(
                            "SELECT COUNT(*) FROM wq_units "
                            "WHERE state IN ('claimed','running') AND lease_owner IS NOT NULL "
                            "AND substr(lease_owner, 1, ?) = ? "
                            "AND (lease_expires_at IS NULL OR lease_expires_at > ?)",
                            (len(prefix), prefix, now),
                        ).fetchone()[0]
                    )
                    if in_flight >= int(machine["max_concurrent"]):
                        self._rollback()
                        return None

                # A worker that DECLARED --reserved-for <owner> restricts its own
                # claim to that owner's units. There is no server-side config-map:
                # the owner arrives on the claim call itself, already coerced to
                # the safe side above (blank/non-str ⇒ no clause ⇒ unrestricted).
                # This can only ever REMOVE candidates, so a wrong owner starves
                # the worker rather than handing it someone else's work — the
                # fail-CLOSED direction. submitted_by is stored per unit already;
                # no new untrusted path reaches this query.
                reservation_sql = " AND submitted_by = ?" if reserved_owner else ""
                candidate_params: list[Any] = [now, now]
                if reserved_owner:
                    candidate_params.append(reserved_owner)

                candidates = self._connection.execute(
                    f"""
                    SELECT id, lease_generation, labels FROM wq_units
                     WHERE cancel_requested = 0
                       AND attempt < max_attempts
                       AND (not_before IS NULL OR not_before <= ?)
                       AND (   state = 'queued'
                            OR (state IN ('claimed','running')
                                AND (lease_expires_at IS NULL OR lease_expires_at <= ?)))
                       {reservation_sql}
                     ORDER BY priority ASC, created_at ASC, id ASC
                     LIMIT 20
                    """,
                    candidate_params,
                ).fetchall()

                chosen = None
                for candidate in candidates:
                    # Labels declare CAPABILITY: a machine must declare every
                    # label the unit asks for. Applied in Python over a bounded
                    # candidate set rather than in SQL, so the JSON stays opaque
                    # to the query planner.
                    if set(_load_list(candidate["labels"])) <= declared:
                        chosen = candidate
                        break
                if chosen is None:
                    self._rollback()
                    return None

                unit_id = str(chosen["id"])
                generation = int(chosen["lease_generation"])

                # Reclaiming an expired lease closes whatever the previous
                # holder left open, exactly as the reaper would. Without this,
                # a directly-reclaimed lease leaves a finished_at IS NULL row
                # behind and SPEC §6's double-execution alarm reads non-zero
                # forever for a unit that never double-executed.
                #
                # ...and, exactly as the reaper does, it REFUNDS the budget that
                # abandoned execution consumed. A worker that beats the 60 s
                # reaper to an expired lease must not be a cheaper way to burn a
                # candidate's attempts than the reaper is: leaving it standing
                # strands the unit at attempt == max_attempts in state 'queued',
                # which claim() itself then filters out forever.
                refund = max(
                    0,
                    self._connection.execute(
                        "UPDATE wq_attempts SET outcome = COALESCE(outcome, 'abandoned'), "
                        "finished_at = ? WHERE unit_id = ? AND finished_at IS NULL",
                        (now, unit_id),
                    ).rowcount,
                )

                cursor = self._connection.execute(
                    """
                    UPDATE wq_units
                       SET state = 'claimed',
                           lease_owner = ?,
                           lease_expires_at = ?,
                           lease_generation = lease_generation + 1,
                           attempt = MAX(attempt - ?, 0) + 1,
                           updated_at = ?
                     WHERE id = ? AND lease_generation = ?
                    """,
                    (owner, expires_at, refund, now, unit_id, generation),
                )
                if cursor.rowcount != 1:
                    # Lost the race under someone else's write lock. Take the
                    # next one on the next call rather than looping in here.
                    self._rollback()
                    return None

                ordinal = int(
                    self._connection.execute(
                        "SELECT COALESCE(MAX(attempt), 0) + 1 FROM wq_attempts WHERE unit_id = ?",
                        (unit_id,),
                    ).fetchone()[0]
                )
                try:
                    self._connection.execute(
                        """
                        INSERT INTO wq_attempts (
                            unit_id, attempt, lease_generation, machine_id, worker_id, started_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (unit_id, ordinal, generation + 1, machine_id, worker_id, now),
                    )
                except sqlite3.IntegrityError:
                    # UNIQUE(unit_id, attempt): someone else recorded this
                    # ordinal, so one of us was fenced out. Abort, do not retry.
                    self._rollback()
                    return None

                unit = _unit_dict(
                    self._connection.execute(
                        "SELECT * FROM wq_units WHERE id = ?", (unit_id,)
                    ).fetchone()
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise

        return {
            "unit": unit,
            "lease_generation": generation + 1,
            "lease_expires_at": expires_at,
            "attempt": ordinal,
        }

    # -- heartbeat --------------------------------------------------------

    def heartbeat(
        self,
        unit_id: str,
        owner: str,
        lease_generation: int,
        now: str | None = None,
    ) -> None:
        """Renew the lease, fenced on ``(lease_owner, lease_generation)``.

        ``rowcount != 1`` raises :class:`LeaseLost`: you were adopted. The
        worker must kill its child process tree immediately and write no result
        — a zombie that wakes up 20 minutes late writes nothing.
        """
        now = now or utc_now_iso()
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE wq_units SET lease_expires_at = ?, updated_at = ? "
                "WHERE id = ? AND lease_owner = ? AND lease_generation = ? "
                "AND state IN ('claimed','running')",
                (_shift(now, LEASE_TTL_S), now, unit_id, owner, int(lease_generation)),
            )
            if cursor.rowcount != 1:
                raise LeaseLost(
                    f"heartbeat for {unit_id} under generation {lease_generation} "
                    f"as {owner!r} matched no row: lease lost"
                )

    # -- result -----------------------------------------------------------

    def record_result(
        self,
        unit_id: str,
        owner: str,
        lease_generation: int,
        outcome: str,
        *,
        exit_code: int | None = None,
        gate_exit_code: int | None = None,
        input_key: str | None = None,
        retryable: int | None = None,
        remedy: str | None = None,
        head_sha: str | None = None,
        result_branch: str | None = None,
        result_sha: str | None = None,
        log_path: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Close an attempt, fenced on ``(lease_owner, lease_generation)``.

        Zero matching rows raises :class:`LeaseLost` and writes NOTHING.

        Returns ``{"unit": dict, "alert": dict | None}``. ``alert`` is non-None
        exactly when this call won the ``alerted_at`` compare-and-set, i.e.
        exactly once per park (§4.5) — AND the refusal ledger has not already
        announced a storm for this ``input_key`` (:meth:`_storm_already_announced`),
        because that storm and this park are ONE event. The CALLER sends it; the
        store performs no I/O beyond SQLite, which is what makes the guard
        idempotent under concurrent parks.
        """
        outcome_value = Outcome(outcome).value
        now = now or utc_now_iso()

        with self._lock:
            self._begin()
            try:
                row = self._connection.execute(
                    "SELECT * FROM wq_units WHERE id = ? AND lease_owner = ? "
                    "AND lease_generation = ? AND state IN ('claimed','running')",
                    (unit_id, owner, int(lease_generation)),
                ).fetchone()
                if row is None:
                    self._rollback()
                    raise LeaseLost(
                        f"result for {unit_id} under generation {lease_generation} "
                        f"as {owner!r} matched no row: lease lost"
                    )

                plan = self._plan_result(row, outcome_value, retryable, remedy, now)

                cursor = self._connection.execute(
                    """
                    UPDATE wq_units
                       SET state = ?,
                           attempt = ?,
                           instrument_retries = ?,
                           agent_profile = ?,
                           terminal_reason = ?,
                           park_remedy = ?,
                           not_before = ?,
                           finished_at = ?,
                           result_branch = COALESCE(?, result_branch),
                           result_sha = COALESCE(?, result_sha),
                           lease_owner = NULL,
                           lease_expires_at = NULL,
                           updated_at = ?
                     WHERE id = ? AND lease_owner = ? AND lease_generation = ?
                    """,
                    (
                        plan["state"],
                        plan["attempt"],
                        plan["instrument_retries"],
                        plan["agent_profile"],
                        plan["terminal_reason"],
                        plan["park_remedy"],
                        plan["not_before"],
                        plan["finished_at"],
                        result_branch,
                        result_sha,
                        now,
                        unit_id,
                        owner,
                        int(lease_generation),
                    ),
                )
                if cursor.rowcount != 1:  # pragma: no cover - same txn as the SELECT
                    self._rollback()
                    raise LeaseLost(f"fenced result update for {unit_id} matched no row")

                self._connection.execute(
                    """
                    UPDATE wq_attempts
                       SET finished_at = ?, outcome = ?, exit_code = ?, gate_exit_code = ?,
                           retryable = ?, remedy = ?, head_sha = ?, log_path = ?,
                           input_key = COALESCE(?, input_key)
                     WHERE unit_id = ? AND lease_generation = ? AND finished_at IS NULL
                    """,
                    (
                        now,
                        outcome_value,
                        exit_code,
                        gate_exit_code,
                        retryable,
                        remedy,
                        head_sha,
                        log_path,
                        input_key,
                        unit_id,
                        int(lease_generation),
                    ),
                )

                alert = None
                if plan["alertable"] and not self._storm_already_announced(input_key):
                    alert = self._claim_alert(unit_id, now)

                unit = _unit_dict(
                    self._connection.execute(
                        "SELECT * FROM wq_units WHERE id = ?", (unit_id,)
                    ).fetchone()
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise

        return {"unit": unit, "alert": alert}

    def _plan_result(
        self,
        row: sqlite3.Row,
        outcome: str,
        retryable: int | None,
        remedy: str | None,
        now: str,
    ) -> dict[str, Any]:
        """The whole §3.1/§4.5 state machine, as data. No I/O, no writes."""
        attempt = int(row["attempt"])
        retries = int(row["instrument_retries"])
        max_attempts = int(row["max_attempts"])
        profile = str(row["agent_profile"])
        unit_id = str(row["id"])

        plan: dict[str, Any] = {
            "state": UnitState.QUEUED.value,
            "attempt": attempt,
            "instrument_retries": retries,
            "agent_profile": profile,
            "terminal_reason": None,
            "park_remedy": None,
            "not_before": None,
            "finished_at": None,
            "alertable": False,
        }

        def park(reason: str | None, *, alertable: bool = True, finished: bool = True) -> None:
            plan["state"] = UnitState.PARKED.value
            plan["terminal_reason"] = reason
            plan["park_remedy"] = remedy
            plan["finished_at"] = now if finished else None
            plan["alertable"] = alertable

        if outcome in NON_CONSUMING_OUTCOMES:
            # The six reversing outcomes refund the claim-time increment. The
            # floor at 0 matters: a reap plus a late lease-lost report can both
            # land on the same claim, and a negative attempt violates the CHECK.
            plan["attempt"] = max(0, attempt - 1)

        if outcome == Outcome.PASS.value:
            sensitive = str(row["risk_class"]) == RiskClass.SENSITIVE.value
            # A sensitive unit NEVER auto-lands; it stops at review and takes no
            # terminal_reason, because it has not been accepted by anyone yet.
            plan["state"] = UnitState.REVIEW.value if sensitive else UnitState.DONE.value
            plan["terminal_reason"] = None if sensitive else TerminalReason.ACCEPTED.value
            plan["finished_at"] = now

        elif outcome in (Outcome.CANDIDATE_DEFECT.value, Outcome.TIMEOUT.value):
            # Both consume the attempt: a defect is a verdict about the code, and
            # a unit that blew its own timeout_s is evidence about the candidate,
            # not about the box (SPEC §3.1 lists six reversing outcomes; timeout
            # is not one of them).
            if attempt >= max_attempts:
                park(TerminalReason.ATTEMPTS_EXHAUSTED.value)
            else:
                plan["state"] = UnitState.QUEUED.value
                if outcome == Outcome.CANDIDATE_DEFECT.value:
                    plan["agent_profile"] = self._maybe_escalate(unit_id, profile)

        elif outcome in INSTRUMENT_RETRY_OUTCOMES:
            # §4.3: the fault lies with the box or the instrument, so the
            # candidate's budget is refunded and a SEPARATE counter carries it.
            proposed = retries + 1
            if proposed > INSTRUMENT_RETRY_CAP:
                # Cap, don't overshoot: the park must report 3, not 4 — the
                # counter is a measurement of the instrument, and the fourth
                # occurrence is the park itself (SPEC §3.1 test).
                plan["instrument_retries"] = INSTRUMENT_RETRY_CAP
                park(TerminalReason.TERMINAL_INSTRUMENT.value)
            else:
                plan["instrument_retries"] = proposed
                if retryable == 0:
                    # Terminal-at-1 classes (auth/suspension): re-running the
                    # same key cannot help, so park now with one alert.
                    park(TerminalReason.TERMINAL_INSTRUMENT.value)
                else:
                    backoff = INSTRUMENT_BACKOFF_S[min(proposed, len(INSTRUMENT_BACKOFF_S)) - 1]
                    plan["state"] = UnitState.QUEUED.value
                    plan["not_before"] = _shift(now, backoff)

        elif outcome == Outcome.UNCHANGED_RETRY.value:
            # SOFT park: terminal_reason stays NULL and nothing is alerted.
            # Re-queueing would busy-loop the ledger (the caller changed nothing,
            # so the key is the same and the answer would be the same), and
            # alerting on it would drown the one-alert guard in noise — the
            # storm park four refusals later is the event worth waking someone.
            park(None, alertable=False, finished=False)

        elif outcome == Outcome.STORM_PARKED.value:
            # §4.3's table says storm-parked consumes NO attempt, but it is not
            # one of §3.1's six reversing outcomes and so is not in
            # schema.NON_CONSUMING_OUTCOMES (a frozen file). Reverse it here
            # rather than widen that constant: nothing ran — the ledger refused
            # in 0.2 s — so nothing was spent, and a unit that reaches a human
            # with a phantom attempt against it misreports why it stopped.
            plan["attempt"] = max(0, attempt - 1)
            park(TerminalReason.STORM_PARKED.value)

        elif outcome in (Outcome.LEASE_LOST.value, Outcome.ABANDONED.value):
            plan["state"] = UnitState.QUEUED.value

        if plan["state"] == UnitState.QUEUED.value and int(row["cancel_requested"]) == 1:
            # A cancelled unit that requeues is unclaimable forever (claim skips
            # cancel_requested=1) and would read as healthy backlog. Terminate it
            # here instead — favourable absence is the failure mode to avoid.
            plan["state"] = UnitState.CANCELLED.value
            plan["terminal_reason"] = TerminalReason.CANCELLED.value
            plan["finished_at"] = now

        return plan

    def _maybe_escalate(self, unit_id: str, profile: str) -> str:
        """§4.6: on the SECOND candidate-defect, change the ACTION, not the tier.

        The retry is dispatched to a different LINEAGE via the lookup table in
        ``configs/workqueue.yaml:escalation``. Escalating effort on the same
        lineage was tested and killed on evidence across four lineages.
        """
        defects = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM wq_attempts WHERE unit_id = ? AND outcome = ?",
                (unit_id, Outcome.CANDIDATE_DEFECT.value),
            ).fetchone()[0]
        )
        # The current attempt row is closed AFTER this plan is computed, so the
        # second defect is the one where the stored count is exactly 1.
        if defects != 1:
            return profile
        table = self.config.get("escalation") or {}
        return str(table.get(profile, profile))

    def _storm_already_announced(self, input_key: str | None) -> bool:
        """Has the refusal ledger already woken someone about this exact input?

        ``refusal_record`` owns a CAS of its own (``wq_refusals.alerted_at``) and
        fires it at the cap, one round BEFORE the unit reaches its storm park —
        the caller sends that payload, so the storm is already announced by the
        time this park lands. Alerting again here would put two notifications on
        one event and break the ratio §6 reads to detect a park nobody was told
        about (``alerts_sent == parks``); :meth:`alerts` already dedups the pair
        in the other direction.

        The window is exactly "while an un-forgiven storm alert stands for this
        input": ``unpark`` deletes the refusal row, and ``refusal_clear`` deletes
        it on a pass, both of which re-arm the unit's own alert.
        """
        if not input_key:
            return False
        return (
            self._connection.execute(
                "SELECT 1 FROM wq_refusals WHERE input_key = ? AND alerted_at IS NOT NULL LIMIT 1",
                (input_key,),
            ).fetchone()
            is not None
        )

    def _claim_alert(self, unit_id: str, now: str) -> dict[str, Any] | None:
        """Flip ``wq_units.alerted_at`` once. Winner gets the payload to send."""
        cursor = self._connection.execute(
            "UPDATE wq_units SET alerted_at = ? WHERE id = ? AND alerted_at IS NULL",
            (now, unit_id),
        )
        if cursor.rowcount != 1:
            return None
        row = self._connection.execute("SELECT * FROM wq_units WHERE id = ?", (unit_id,)).fetchone()
        unit = _unit_dict(row) or {}
        return {
            "kind": "unit-park",
            "unit_id": unit_id,
            "state": unit.get("state"),
            "terminal_reason": unit.get("terminal_reason"),
            "remedy": unit.get("park_remedy"),
            "repo_slug": unit.get("repo_slug"),
            "branch": unit.get("branch"),
            "attempt": unit.get("attempt"),
            "instrument_retries": unit.get("instrument_retries"),
            "alerted_at": now,
            "message": (
                f"workqueue park: {unit_id} {unit.get('terminal_reason') or 'soft-park'}"
                f" — {unit.get('park_remedy') or 'no remedy recorded'}"
            ),
        }

    # -- reaper -----------------------------------------------------------

    def reap_expired(self, now: str | None = None) -> int:
        """Requeue units whose lease expired. Returns how many were reclaimed.

        Closes the open ``wq_attempts`` row as ``abandoned`` so the
        double-execution alarm stays honest, and REFUNDS the attempt budget that
        execution consumed at claim time — exactly what ``record_result`` does
        for the ``abandoned`` outcome, and for the same reason: an execution that
        died with the box produced no verdict about the candidate.

        Refunding is not a nicety. ``claim`` increments ``attempt``
        unconditionally, so a reaper that left it standing burned one budget unit
        per dead worker; after ``max_attempts`` reaps the unit sits at
        ``state='queued'`` with ``attempt == max_attempts``, which claim's own
        ``attempt < max_attempts`` filter excludes FOREVER. That is a silent
        zombie that reads as healthy backlog — the favourable-absence failure
        this queue exists to prevent.

        ``instrument_retries`` is untouched (abandoned refunds ``attempt`` only),
        and the refund is clamped at zero: a reap and a late lease-lost report can
        both land on the same claim, and a negative ``attempt`` violates the CHECK.
        """
        now = now or utc_now_iso()
        with self._lock:
            self._begin()
            try:
                rows = self._connection.execute(
                    "SELECT id, cancel_requested FROM wq_units "
                    "WHERE state IN ('claimed','running') "
                    "AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
                    (now,),
                ).fetchall()
                for row in rows:
                    unit_id = str(row["id"])
                    # One refund per execution actually abandoned: if there is no
                    # open attempt row nothing was spent, so nothing is given back.
                    refund = max(
                        0,
                        self._connection.execute(
                            "UPDATE wq_attempts SET outcome = COALESCE(outcome, 'abandoned'), "
                            "finished_at = ? WHERE unit_id = ? AND finished_at IS NULL",
                            (now, unit_id),
                        ).rowcount,
                    )
                    if int(row["cancel_requested"]) == 1:
                        self._connection.execute(
                            "UPDATE wq_units SET state = 'cancelled', terminal_reason = 'cancelled', "
                            "attempt = MAX(attempt - ?, 0), finished_at = ?, lease_owner = NULL, "
                            "lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                            (refund, now, now, unit_id),
                        )
                    else:
                        self._connection.execute(
                            "UPDATE wq_units SET state = 'queued', attempt = MAX(attempt - ?, 0), "
                            "lease_owner = NULL, lease_expires_at = NULL, updated_at = ? "
                            "WHERE id = ?",
                            (refund, now, unit_id),
                        )
                self._commit()
                return len(rows)
            except BaseException:
                self._rollback()
                raise

    # -- park / unpark / cancel -------------------------------------------

    def park(
        self,
        unit_id: str,
        terminal_reason: str | None,
        remedy: str | None,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        """Park a unit administratively. Returns the alert payload iff CAS won."""
        if terminal_reason is not None:
            terminal_reason = TerminalReason(terminal_reason).value
        now = now or utc_now_iso()
        with self._lock:
            self._begin()
            try:
                row = self._connection.execute(
                    "SELECT id FROM wq_units WHERE id = ?", (unit_id,)
                ).fetchone()
                if row is None:
                    self._rollback()
                    return None
                self._connection.execute(
                    "UPDATE wq_units SET state = 'parked', terminal_reason = ?, park_remedy = ?, "
                    "finished_at = COALESCE(finished_at, ?), lease_owner = NULL, "
                    "lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                    (terminal_reason, remedy, now if terminal_reason else None, now, unit_id),
                )
                alert = self._claim_alert(unit_id, now) if terminal_reason else None
                self._commit()
                return alert
            except BaseException:
                self._rollback()
                raise

    def requeue(self, unit_id: str) -> None:
        """Put a SOFT-parked unit back in the queue. The ledger keeps its memory.

        This is the OTHER half of ``unpark`` and the distinction is the whole
        anti-retry contract (§4.5). A soft park (``state='parked'`` with
        ``terminal_reason IS NULL``) is what an ``unchanged-retry`` leaves behind:
        nothing ran, nothing is broken, and the caller is free to put the unit
        back — but the refusal ledger must not forget, because the count it is
        keeping is the ONLY thing that can reach the storm cap.

        Re-queueing through ``unpark`` instead is a live busy-loop: unpark
        DELETES the refusal row for the unit's last ``input_key`` and zeroes
        ``attempt``, so the very next claim runs the gate for real, refuses,
        records count=1 again, and the pair alternates candidate-defect /
        unchanged-retry forever. ``count`` oscillates 1↔2, the cap of 5 is
        unreachable, and no storm park and no alert ever happen — the failure is
        SILENT, which is exactly the shape this queue exists to prevent.

        So this method touches ``wq_refusals`` not at all, and leaves ``attempt``,
        ``instrument_retries`` and ``alerted_at`` exactly as it found them. It
        refuses a TERMINAL park outright: that one needs a human saying what
        changed, which is ``unpark(unit_id, because)``.

        A unit with ``cancel_requested = 1`` is CANCELLED rather than re-queued —
        see :meth:`_cancel_now`. Re-queueing it would produce a row that is
        'queued' and permanently unclaimable.
        """
        now = utc_now_iso()
        with self._lock:
            self._begin()
            try:
                row = self._connection.execute(
                    "SELECT state, terminal_reason, cancel_requested FROM wq_units WHERE id = ?",
                    (unit_id,),
                ).fetchone()
                if row is None:
                    self._rollback()
                    raise KeyError(f"unknown unit: {unit_id}")
                if str(row["state"]) != UnitState.PARKED.value:
                    self._rollback()
                    raise ValueError(f"unit {unit_id} is not parked (state={row['state']})")
                if int(row["cancel_requested"]) == 1:
                    # Cancelled beats re-queued. See _cancel_now: 'queued' with
                    # cancel_requested=1 is unclaimable backlog, not work.
                    self._cancel_now(unit_id, now)
                    self._commit()
                    return
                if row["terminal_reason"] is not None:
                    self._rollback()
                    raise ValueError(
                        f"unit {unit_id} is parked as {row['terminal_reason']} — TERMINAL. "
                        "Requeue is for soft parks only; say what changed: "
                        f"wq unpark {unit_id} --because '<what you fixed>'"
                    )
                self._connection.execute(
                    "UPDATE wq_units SET state = 'queued', park_remedy = NULL, "
                    "not_before = NULL, finished_at = NULL, lease_owner = NULL, "
                    "lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                    (now, unit_id),
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def unpark(self, unit_id: str, because: str) -> None:
        """Human unpark — the AMNESTY, and the only thing that forgives the ledger.

        ``because`` is mandatory: there is no automatic unpark. Putting a soft
        park back without forgetting anything is :meth:`requeue`; this method is
        what a human runs on a TERMINAL park (and, explicitly, on a soft one too
        when they mean the amnesty).

        Clears the park AND the counters that would otherwise re-park the unit on
        its first step: an unpark that leaves ``attempt >= max_attempts`` or
        ``instrument_retries`` at its cap produces a unit that is 'queued' and
        permanently unclaimable, which reads as healthy backlog. That is exactly
        the favourable-absence failure this queue exists to prevent.

        The refusal row for the unit's LAST ``input_key`` is deleted, and only
        that one: unparking is a statement about this input, not an amnesty.

        One thing an unpark may NOT do is resurrect cancelled work: a unit with
        ``cancel_requested = 1`` is cancelled here instead (:meth:`_cancel_now`).
        """
        if not because or not because.strip():
            raise ValueError("unpark requires a non-empty --because")
        now = utc_now_iso()
        with self._lock:
            self._begin()
            try:
                row = self._connection.execute(
                    "SELECT state, cancel_requested FROM wq_units WHERE id = ?", (unit_id,)
                ).fetchone()
                if row is None:
                    self._rollback()
                    raise KeyError(f"unknown unit: {unit_id}")
                if str(row["state"]) != UnitState.PARKED.value:
                    self._rollback()
                    raise ValueError(f"unit {unit_id} is not parked (state={row['state']})")
                if int(row["cancel_requested"]) == 1:
                    # A cancel outranks the amnesty: the unit terminates instead
                    # of returning to a queue that can never claim it. The refusal
                    # row is deliberately NOT forgiven here — the amnesty is a
                    # statement about an input that is about to be re-run, and
                    # this unit will not run. The ledger can only refuse harder.
                    self._cancel_now(unit_id, now)
                    self._commit()
                    return
                last_key = self._connection.execute(
                    "SELECT input_key FROM wq_attempts WHERE unit_id = ? AND input_key IS NOT NULL "
                    "ORDER BY attempt DESC LIMIT 1",
                    (unit_id,),
                ).fetchone()
                if last_key is not None:
                    self._connection.execute(
                        "DELETE FROM wq_refusals WHERE input_key = ?", (last_key["input_key"],)
                    )
                self._connection.execute(
                    "UPDATE wq_units SET state = 'queued', terminal_reason = NULL, "
                    "park_remedy = NULL, not_before = NULL, alerted_at = NULL, finished_at = NULL, "
                    "attempt = 0, instrument_retries = 0, lease_owner = NULL, "
                    "lease_expires_at = NULL, updated_at = ? WHERE id = ?",
                    (now, unit_id),
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def cancel(self, unit_id: str) -> None:
        """Request cancellation. A unit that is not running is cancelled at once.

        An in-flight unit keeps its lease — nothing is force-reclaimed, because
        breaking a live lease is the fail-open behaviour §3.4 avoids. The worker
        sees ``cancel_requested`` and the result path terminates it.

        'Not running' is ``queued`` OR ``parked``. Leaving a parked unit at
        ``state='parked', cancel_requested=1`` was a live zombie factory: the
        next ``unpark``/``requeue`` forced it back to 'queued' without clearing
        the flag, and ``claim`` filters ``cancel_requested = 0`` — a unit that is
        queued, unclaimable forever, and indistinguishable from healthy backlog.
        A parked unit is not executing, so cancelling it is immediate and safe.
        """
        now = utc_now_iso()
        with self._lock:
            self._begin()
            try:
                self._connection.execute(
                    "UPDATE wq_units SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                    (now, unit_id),
                )
                self._connection.execute(
                    "UPDATE wq_units SET state = 'cancelled', terminal_reason = 'cancelled', "
                    "finished_at = COALESCE(finished_at, ?), updated_at = ? "
                    "WHERE id = ? AND state IN ('queued','parked')",
                    (now, now, unit_id),
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def _cancel_now(self, unit_id: str, now: str) -> None:
        """Terminate a unit that carries an outstanding ``cancel_requested``.

        Used by ``unpark`` and ``requeue``: a human asked for this unit to stop,
        and returning it to 'queued' would neither honour that nor produce
        runnable work — ``claim`` skips ``cancel_requested = 1``, so the unit
        would sit in the backlog unclaimable and unexplained. The cancel wins;
        resurrecting cancelled work is not something an unpark gets to do.

        Same write :meth:`cancel` makes, plus the lease fields these two verbs
        clear anyway. ``park_remedy`` and ``alerted_at`` are LEFT ALONE: both are
        history of something that really happened, and this unit has no future
        park for the alert guard to re-arm.
        """
        self._connection.execute(
            "UPDATE wq_units SET state = 'cancelled', terminal_reason = 'cancelled', "
            "finished_at = COALESCE(finished_at, ?), lease_owner = NULL, "
            "lease_expires_at = NULL, updated_at = ? WHERE id = ?",
            (now, now, unit_id),
        )

    # -- reads ------------------------------------------------------------

    def get_unit(self, unit_id: str) -> dict[str, Any] | None:
        with self._lock:
            return _unit_dict(
                self._connection.execute(
                    "SELECT * FROM wq_units WHERE id = ?", (unit_id,)
                ).fetchone()
            )

    def list_attempts(self, unit_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM wq_attempts WHERE unit_id = ? ORDER BY attempt ASC", (unit_id,)
            ).fetchall()
        return [row for row in (_row_dict(r) for r in rows) if row is not None]

    # -- machines ---------------------------------------------------------

    def enroll_machine(self, m: dict[str, Any]) -> None:
        """UPSERT a machine row. ``drain`` and ``enrolled_at`` survive re-enrollment.

        ``role`` is SERVER-DERIVED (D8a): whatever ``m`` says about it is ignored
        and :meth:`role_for` decides, so ``role='worker'`` in a request body from
        a machine nobody allowlisted stores ``'dispatcher'``. Enrollment still
        succeeds — a dispatcher is a first-class member of the pool that submits
        and observes — because refusing to enroll would only teach people to stop
        enrolling, and an unenrolled machine is one the operator cannot see.

        Re-enrollment RE-DERIVES the role rather than preserving it, which is what
        makes editing the allowlist plus a re-run of ``enroll.sh`` the promotion
        and demotion path. ``device_class`` is stored verbatim as audit metadata
        and grants nothing.
        """
        machine_id = str(m["machine_id"])
        role = self.role_for(machine_id)
        now = utc_now_iso()
        with self._lock:
            self._begin()
            try:
                self._connection.execute(
                    """
                    INSERT INTO wq_machines (
                        machine_id, hostname, os, ssh_target, labels, max_concurrent,
                        enrolled_at, agent_version, notes, ncpu, perf_cores, mem_gb,
                        ceiling_fraction, role, device_class
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(machine_id) DO UPDATE SET
                        hostname = excluded.hostname,
                        os = excluded.os,
                        ssh_target = excluded.ssh_target,
                        labels = excluded.labels,
                        max_concurrent = excluded.max_concurrent,
                        agent_version = excluded.agent_version,
                        notes = excluded.notes,
                        ncpu = COALESCE(excluded.ncpu, wq_machines.ncpu),
                        perf_cores = COALESCE(excluded.perf_cores, wq_machines.perf_cores),
                        mem_gb = COALESCE(excluded.mem_gb, wq_machines.mem_gb),
                        ceiling_fraction = excluded.ceiling_fraction,
                        role = excluded.role,
                        device_class = COALESCE(excluded.device_class, wq_machines.device_class)
                    """,
                    (
                        machine_id,
                        str(m.get("hostname") or m["machine_id"]),
                        str(m.get("os") or "darwin"),
                        m.get("ssh_target"),
                        _json_list(m.get("labels") or []),
                        int(m.get("max_concurrent", 1)),
                        now,
                        m.get("agent_version"),
                        m.get("notes"),
                        m.get("ncpu"),
                        m.get("perf_cores"),
                        m.get("mem_gb"),
                        float(m.get("ceiling_fraction") or 0.75),
                        # NOT m.get("role"): the body's opinion is discarded above.
                        role,
                        m.get("device_class"),
                    ),
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise

    def machine_beat(
        self, machine_id: str, beat: dict[str, Any], now: str | None = None
    ) -> dict[str, Any]:
        """Record telemetry + liveness. Returns ``{"drain": 0|1}``.

        The beat is how the pool learns every box's cores and load (the operator,
        2026-08-11) and how a drained machine finds out — the response is the
        only channel that tells a worker to stop claiming.
        """
        now = now or utc_now_iso()
        with self._lock:
            self._begin()
            try:
                row = self._connection.execute(
                    "SELECT drain FROM wq_machines WHERE machine_id = ?", (machine_id,)
                ).fetchone()
                if row is None:
                    self._rollback()
                    return {"drain": 0}
                self._connection.execute(
                    "UPDATE wq_machines SET last_seen_at = ?, telemetry_at = ?, "
                    "last_load1 = COALESCE(?, last_load1), last_load5 = COALESCE(?, last_load5), "
                    "last_mem_free_gb = COALESCE(?, last_mem_free_gb) WHERE machine_id = ?",
                    (
                        now,
                        now,
                        beat.get("load1"),
                        beat.get("load5"),
                        beat.get("mem_free_gb"),
                        machine_id,
                    ),
                )
                worker_id = beat.get("worker_id")
                if worker_id:
                    self._connection.execute(
                        """
                        INSERT INTO wq_workers (
                            worker_id, machine_id, pid, started_at, last_beat_at, current_unit_id
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(worker_id) DO UPDATE SET
                            last_beat_at = excluded.last_beat_at,
                            current_unit_id = excluded.current_unit_id
                        """,
                        (
                            str(worker_id),
                            machine_id,
                            int(beat.get("pid") or 0),
                            now,
                            now,
                            beat.get("current_unit_id"),
                        ),
                    )
                self._commit()
                return {"drain": int(row["drain"])}
            except BaseException:
                self._rollback()
                raise

    def set_drain(self, machine_id: str, drain: bool) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE wq_machines SET drain = ? WHERE machine_id = ?",
                (1 if drain else 0, machine_id),
            )

    def list_machines(self) -> list[dict[str, Any]]:
        now = utc_now_iso()
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM wq_machines ORDER BY machine_id ASC"
            ).fetchall()
            machines: list[dict[str, Any]] = []
            for row in rows:
                data = _row_dict(row) or {}
                data["labels"] = _load_list(data.get("labels"))
                data["in_flight"] = self._in_flight(str(data["machine_id"]), now)
                machines.append(data)
        return machines

    def _in_flight(self, machine_id: str, now: str) -> int:
        prefix = f"{machine_id}:"
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM wq_units WHERE state IN ('claimed','running') "
                "AND lease_owner IS NOT NULL AND substr(lease_owner, 1, ?) = ? "
                "AND (lease_expires_at IS NULL OR lease_expires_at > ?)",
                (len(prefix), prefix, now),
            ).fetchone()[0]
        )

    # -- refusal ledger (§4.2) --------------------------------------------

    def refusal_check(self, input_key: str, gate: str) -> dict[str, Any] | None:
        with self._lock:
            return _row_dict(
                self._connection.execute(
                    "SELECT * FROM wq_refusals WHERE input_key = ? AND gate = ?",
                    (input_key, gate),
                ).fetchone()
            )

    def refusal_record(
        self,
        input_key: str,
        gate: str,
        refusal_class: str,
        retryable: int,
        remedy: str,
        unit_id: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Count one refusal for ``(input_key, gate)``.

        The ORIGINAL cause is never overwritten by ``unchanged-retry`` or
        ``storm-parked`` (``accurate-gate.py:342``) — and neither are its
        ``retryable``/``remedy``, which belong to that class. Otherwise the
        ledger would forget what actually broke the moment it starts refusing
        cheaply, and the remedy text would degrade to "you already asked".

        At ``count >= REFUSAL_STORM_CAP`` the row is parked and the alert CAS
        fires once. The returned row carries ``alert`` (payload or None); the
        caller sends it and decides whether to storm-park the unit itself.
        """
        now = now or utc_now_iso()
        with self._lock:
            self._begin()
            try:
                existing = self._connection.execute(
                    "SELECT * FROM wq_refusals WHERE input_key = ? AND gate = ?",
                    (input_key, gate),
                ).fetchone()
                if existing is None:
                    count = 1
                    self._connection.execute(
                        """
                        INSERT INTO wq_refusals (
                            input_key, gate, count, first_seen_at, last_seen_at,
                            refusal_class, retryable, remedy, last_unit_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            input_key,
                            gate,
                            count,
                            now,
                            now,
                            refusal_class,
                            int(retryable),
                            remedy,
                            unit_id,
                        ),
                    )
                else:
                    count = int(existing["count"]) + 1
                    keep = refusal_class in _NON_OVERWRITING_CLASSES
                    self._connection.execute(
                        "UPDATE wq_refusals SET count = ?, last_seen_at = ?, refusal_class = ?, "
                        "retryable = ?, remedy = ?, last_unit_id = COALESCE(?, last_unit_id) "
                        "WHERE input_key = ? AND gate = ?",
                        (
                            count,
                            now,
                            existing["refusal_class"] if keep else refusal_class,
                            int(existing["retryable"]) if keep else int(retryable),
                            existing["remedy"] if keep else remedy,
                            unit_id,
                            input_key,
                            gate,
                        ),
                    )

                alert = None
                if count >= REFUSAL_STORM_CAP:
                    self._connection.execute(
                        "UPDATE wq_refusals SET parked_at = COALESCE(parked_at, ?) "
                        "WHERE input_key = ? AND gate = ?",
                        (now, input_key, gate),
                    )
                    cursor = self._connection.execute(
                        "UPDATE wq_refusals SET alerted_at = ? "
                        "WHERE input_key = ? AND gate = ? AND alerted_at IS NULL",
                        (now, input_key, gate),
                    )
                    if cursor.rowcount == 1:
                        alert = {
                            "kind": "refusal-storm",
                            "input_key": input_key,
                            "gate": gate,
                            "count": count,
                            "unit_id": unit_id,
                            "alerted_at": now,
                        }

                row = _row_dict(
                    self._connection.execute(
                        "SELECT * FROM wq_refusals WHERE input_key = ? AND gate = ?",
                        (input_key, gate),
                    ).fetchone()
                )
                self._commit()
            except BaseException:
                self._rollback()
                raise

        assert row is not None
        if alert is not None:
            row["alert"] = alert
            row["alert"]["refusal_class"] = row["refusal_class"]
            row["alert"]["remedy"] = row["remedy"]
        else:
            row["alert"] = None
        return row

    def refusal_clear(self, input_key: str, gate: str) -> None:
        """A pass DELETES the row. The ledger has no cached-pass path — it can
        only ever refuse HARDER, never admit something on stale evidence."""
        with self._lock:
            self._connection.execute(
                "DELETE FROM wq_refusals WHERE input_key = ? AND gate = ?", (input_key, gate)
            )

    # -- alerts + status ---------------------------------------------------

    def alerts(self) -> list[dict[str, Any]]:
        """Every alert this store has authorised, exactly once each.

        A storm park flips two guards (the unit's and the refusal row's) for one
        event, so a refusal alert whose ``last_unit_id`` already alerted is not
        counted twice — 'alerts sent vs parks' must read 1:1 (§6).
        """
        with self._lock:
            unit_rows = self._connection.execute(
                "SELECT id, state, terminal_reason, park_remedy, alerted_at FROM wq_units "
                "WHERE alerted_at IS NOT NULL ORDER BY alerted_at ASC, id ASC"
            ).fetchall()
            refusal_rows = self._connection.execute(
                "SELECT * FROM wq_refusals WHERE alerted_at IS NOT NULL "
                "ORDER BY alerted_at ASC, input_key ASC"
            ).fetchall()

        alerted_units = {str(row["id"]) for row in unit_rows}
        out: list[dict[str, Any]] = [
            {
                "kind": "unit-park",
                "unit_id": str(row["id"]),
                "state": row["state"],
                "terminal_reason": row["terminal_reason"],
                "remedy": row["park_remedy"],
                "alerted_at": row["alerted_at"],
            }
            for row in unit_rows
        ]
        for row in refusal_rows:
            if row["last_unit_id"] and str(row["last_unit_id"]) in alerted_units:
                continue
            out.append(
                {
                    "kind": "refusal-storm",
                    "input_key": row["input_key"],
                    "gate": row["gate"],
                    "count": row["count"],
                    "refusal_class": row["refusal_class"],
                    "remedy": row["remedy"],
                    "unit_id": row["last_unit_id"],
                    "alerted_at": row["alerted_at"],
                }
            )
        out.sort(key=lambda item: str(item.get("alerted_at") or ""))
        return out

    def status(self, now: str | None = None) -> dict[str, Any]:
        """The whole observability payload (§6) — ``GET /v1/status`` verbatim.

        Every number here answers a question a human asked: is work sitting while
        capacity idles (oldest_unclaimed_s), is the pool re-running instead of
        producing (attempts per completion), is the instrument the problem
        (instrument share), and can anything run at all (unclaimable).

        Two fields carry a carve-out worth reading before changing them:

        ``parks`` counts TERMINAL parks only (``state='parked' AND
        terminal_reason IS NOT NULL``), which is narrower than
        ``depth['parked']``. §6 requires ``alerts_sent == parks`` to read 1:1,
        and a SOFT park (``unchanged-retry``: ``terminal_reason`` NULL, alertable
        False by design) announces nothing — counting it here would make a
        healthy pool report a permanent alert deficit and train the operator to
        ignore the one ratio that detects a park nobody was told about. Soft
        parks stay visible in ``depth['parked']``.

        ``offloads`` answers the operator's question directly (ROUTING-DECISIONS §2): who
        currently has work in the pool, and on whose box it is running.
        """
        now = now or utc_now_iso()
        cutoff_1h = _shift(now, -3600)
        cutoff_6h = _shift(now, -6 * 3600)
        cutoff_24h = _shift(now, -24 * 3600)

        with self._lock:
            depth = {
                state.value: 0
                for state in (
                    UnitState.QUEUED,
                    UnitState.CLAIMED,
                    UnitState.RUNNING,
                    UnitState.REVIEW,
                    UnitState.DONE,
                    UnitState.PARKED,
                    UnitState.CANCELLED,
                )
            }
            for row in self._connection.execute(
                "SELECT state, COUNT(*) AS n FROM wq_units GROUP BY state"
            ).fetchall():
                depth[str(row["state"])] = int(row["n"])

            oldest_row = self._connection.execute(
                "SELECT MIN(created_at) AS oldest FROM wq_units "
                "WHERE state = 'queued' AND cancel_requested = 0"
            ).fetchone()
            oldest_unclaimed_s = _age_s(oldest_row["oldest"] if oldest_row else None, now)

            machine_rows = self._connection.execute(
                "SELECT * FROM wq_machines ORDER BY machine_id ASC"
            ).fetchall()
            machines: list[dict[str, Any]] = []
            capable: list[set[str]] = []
            total_cores = total_perf = total_slots = 0
            for row in machine_rows:
                machine_id = str(row["machine_id"])
                labels = _load_list(row["labels"])
                if int(row["drain"]) == 0:
                    capable.append(set(labels))
                    total_cores += int(row["ncpu"] or 0)
                    total_perf += int(row["perf_cores"] or 0)
                    total_slots += int(row["max_concurrent"])
                done_1h = self._passes(machine_id, cutoff_1h)
                done_6h = self._passes(machine_id, cutoff_6h)
                finished_6h = int(
                    self._connection.execute(
                        "SELECT COUNT(*) FROM wq_attempts WHERE machine_id = ? "
                        "AND finished_at IS NOT NULL AND finished_at >= ?",
                        (machine_id, cutoff_6h),
                    ).fetchone()[0]
                )
                machines.append(
                    {
                        "machine_id": machine_id,
                        "hostname": row["hostname"],
                        "os": row["os"],
                        "labels": labels,
                        "max_concurrent": int(row["max_concurrent"]),
                        "drain": int(row["drain"]),
                        "in_flight": self._in_flight(machine_id, now),
                        "last_seen_at": row["last_seen_at"],
                        "last_seen_age_s": _age_s(row["last_seen_at"], now),
                        "ncpu": row["ncpu"],
                        "perf_cores": row["perf_cores"],
                        "mem_gb": row["mem_gb"],
                        "load1": row["last_load1"],
                        "load5": row["last_load5"],
                        "mem_free_gb": row["last_mem_free_gb"],
                        "ceiling_fraction": row["ceiling_fraction"],
                        "done_1h": done_1h,
                        "done_6h": done_6h,
                        "attempts_per_completion": (
                            round(finished_6h / done_6h, 2) if done_6h else None
                        ),
                    }
                )

            unclaimable: list[dict[str, Any]] = []
            for row in self._connection.execute(
                "SELECT id, labels FROM wq_units WHERE state = 'queued' AND cancel_requested = 0"
            ).fetchall():
                wanted = set(_load_list(row["labels"]))
                if not wanted:
                    continue
                if not any(wanted <= declared for declared in capable):
                    unclaimable.append(
                        {
                            "unit_id": str(row["id"]),
                            "labels": sorted(wanted),
                            "reason": TerminalReason.UNCLAIMABLE.value,
                        }
                    )

            refusals_24h: dict[str, Any] = {}
            for row in self._connection.execute(
                "SELECT outcome, COUNT(*) AS n FROM wq_attempts "
                "WHERE outcome IS NOT NULL AND outcome != 'pass' AND finished_at >= ? "
                "GROUP BY outcome",
                (cutoff_24h,),
            ).fetchall():
                refusals_24h[str(row["outcome"])] = int(row["n"])
            total_refusals = sum(refusals_24h.values())
            instrument = sum(refusals_24h.get(name.value, 0) for name in INSTRUMENT_RETRY_OUTCOMES)
            refusals_24h["total"] = total_refusals
            refusals_24h["instrument_share"] = (
                round(instrument / total_refusals, 3) if total_refusals else 0.0
            )

            lease_reclaims_1h = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM wq_attempts WHERE outcome = 'abandoned' "
                    "AND finished_at >= ?",
                    (cutoff_1h,),
                ).fetchone()[0]
            )
            double_executions = int(
                self._connection.execute(
                    "SELECT COUNT(DISTINCT a.unit_id) FROM wq_attempts a "
                    "JOIN wq_attempts b ON a.unit_id = b.unit_id AND a.attempt < b.attempt "
                    "WHERE a.finished_at IS NULL OR a.finished_at > b.started_at"
                ).fetchone()[0]
            )
            in_flight = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM wq_units WHERE state IN ('claimed','running')"
                ).fetchone()[0]
            )
            workers = int(self._connection.execute("SELECT COUNT(*) FROM wq_workers").fetchone()[0])
            # TERMINAL parks only — see the docstring. depth['parked'] keeps the
            # soft parks; this number is the one paired with alerts_sent.
            terminal_parks = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM wq_units WHERE state = ? AND terminal_reason IS NOT NULL",
                    (UnitState.PARKED.value,),
                ).fetchone()[0]
            )
            offloads = self._offloads()

        alerts = self.alerts()
        return {
            "now": now,
            "machines": machines,
            "workers": workers,
            "depth": depth,
            "oldest_unclaimed_s": oldest_unclaimed_s,
            "unclaimable": unclaimable,
            "refusals_24h": refusals_24h,
            "lease_reclaims_1h": lease_reclaims_1h,
            "double_executions": double_executions,
            "alerts_sent": len(alerts),
            "parks": terminal_parks,
            "offloads": offloads,
            "capacity": {
                "total_cores": total_cores,
                "total_perf_cores": total_perf,
                "total_slots": total_slots,
                "free_slots": max(0, total_slots - in_flight),
                "in_flight": in_flight,
            },
        }

    def _offloads(self) -> list[dict[str, Any]]:
        """Per-person in-flight/pending counts, with the boxes they are running on.

        "We should be aware when one of us is offloading or has a pending job"
        (the operator, 2026-08-11). Only the four LIVE states are counted: a finished or
        parked unit is not something the person is currently waiting on, and
        rolling them in would make every person's line grow forever.

        Caller holds ``self._lock``.
        """
        rows = self._connection.execute(
            "SELECT submitted_by, state, lease_owner FROM wq_units "
            "WHERE state IN ('queued','claimed','running','review')"
        ).fetchall()
        people: dict[str, dict[str, Any]] = {}
        for row in rows:
            person = str(row["submitted_by"] or "").strip() or UNATTRIBUTED
            entry = people.setdefault(
                person,
                {"person": person, "queued": 0, "running": 0, "in_review": 0, "machines": []},
            )
            entry[_OFFLOAD_BUCKETS[str(row["state"])]] += 1
            owner = row["lease_owner"]
            if owner:
                # lease_owner is '<machine_id>:<worker_id>' and worker_id is
                # itself '<machine_id>:<pid>:<nonce>' — the FIRST field is the
                # box, and that is the only part a human wants to read.
                machine = str(owner).split(":", 1)[0]
                if machine and machine not in entry["machines"]:
                    entry["machines"].append(machine)
        for entry in people.values():
            entry["machines"].sort()
        return sorted(people.values(), key=lambda entry: str(entry["person"]))

    def _passes(self, machine_id: str, cutoff: str) -> int:
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM wq_attempts WHERE machine_id = ? AND outcome = 'pass' "
                "AND finished_at >= ?",
                (machine_id, cutoff),
            ).fetchone()[0]
        )
