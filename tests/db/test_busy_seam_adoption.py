"""HANDOFF/L14-M33 seam adoption: swarm/dal.py and reliability/store.py.

Before this lane, ``rg -l execute_write_transaction`` outside ``tests/`` and
the HANDOFF doc found only ``busy.py`` itself -- zero production adopters.
These tests fail on that tree (neither store touches ``omniagentos.db.busy``
at all, so the shared ``BUSY_RETRY_METRICS`` counters never move) and pass
once ``SwarmDal`` and ``SqliteReliabilityStore`` route their writes through
``omniagentos.db.busy.execute_write_transaction`` / ``run_with_busy_retry``.

A metrics-count assertion is used instead of a source-text grep on purpose:
grepping for the string ``execute_write_transaction`` would pass on a
decorative/no-op import, and a test that merely calls a store method and
expects no exception passes identically before and after adoption (SQLite's
own ``busy_timeout`` already retries transparently at the C layer for a
short hold, so a naive "does a lock-contended write succeed" test proves
nothing -- see BUILDER_BRIEF's red-first warning). Watching the shared
process-wide counters increment is a real, mechanically-verifiable signature
of "this call went through the busy.py seam", not a coincidence of timing.
"""

from __future__ import annotations

import ast
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.db.busy import (
    BUSY_RETRY_METRICS,
    busy_retry_metrics_snapshot,
    reset_busy_retry_metrics,
)
from omniagentos.db.migrate import migrate_connection
from omniagentos.reliability.store import SqliteReliabilityStore
from omniagentos.swarm.dal import SwarmDal
from tests.support.db_template import migrated_db


@pytest.fixture
def swarm_db_path(tmp_path: Path) -> str:
    return migrated_db(CollabStore, tmp_path / "swarm-dal-seam.db")


@pytest.fixture
def reliability_store(tmp_path: Path) -> SqliteReliabilityStore:
    db = tmp_path / "reliability-seam.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    migrate_connection(conn)
    conn.close()
    return SqliteReliabilityStore(str(db))


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    reset_busy_retry_metrics()
    yield
    reset_busy_retry_metrics()


def test_swarm_dal_write_routes_through_busy_seam(swarm_db_path: str) -> None:
    """A SwarmDal write must be observable in the shared busy.py metrics.

    On the pre-adoption tree ``SwarmDal`` never imports ``omniagentos.db.busy``
    at all, so ``BUSY_RETRY_METRICS.attempts`` stays 0 after this call -- this
    assertion FAILS on that tree. Post-adoption, ``_serialized`` retries the
    call via ``run_with_busy_retry`` (and several methods additionally route
    through ``execute_write_transaction`` via ``SwarmDal._write_txn``), so at
    least one attempt is always recorded.
    """
    before = busy_retry_metrics_snapshot()
    dal = SwarmDal(swarm_db_path)
    try:
        run = dal.create_run(working_dir="/tmp/ws", goal="seam adoption", source="test")
        assert run["id"].startswith("swr_")
    finally:
        dal.close()
    after = busy_retry_metrics_snapshot()
    assert after["attempts"] > before["attempts"], (
        "SwarmDal.create_run did not record an attempt in "
        "omniagentos.db.busy.BUSY_RETRY_METRICS -- swarm/dal.py is not "
        "routing its writes through the shared busy-retry seam"
    )
    assert after["successes"] > before["successes"]


def test_swarm_dal_second_write_method_also_routes_through_seam(swarm_db_path: str) -> None:
    """A second, independent write path (not just create_run) must adopt too."""
    dal = SwarmDal(swarm_db_path)
    try:
        run = dal.create_run(working_dir="/tmp/ws", goal="seam adoption 2", source="test")
        reset_busy_retry_metrics()
        activated = dal.try_activate_run(run["id"])
        assert activated is True
    finally:
        dal.close()
    snap = busy_retry_metrics_snapshot()
    assert snap["attempts"] >= 1
    assert snap["successes"] >= 1


def test_reliability_store_write_routes_through_busy_seam(
    reliability_store: SqliteReliabilityStore,
) -> None:
    """A reliability-store write must be observable in the shared busy.py metrics.

    On the pre-adoption tree, ``_execute_with_retry`` runs its own private
    ``_retry_on_busy`` loop and never touches ``omniagentos.db.busy`` --
    ``BUSY_RETRY_METRICS.attempts`` stays 0 and this assertion FAILS. Post
    adoption, ``_execute_with_retry`` calls ``execute_write_transaction``
    directly, so every write (and every method routed through it) records at
    least one attempt.
    """
    before = busy_retry_metrics_snapshot()
    event_id = reliability_store.insert_reliability_event(
        failure_class="test_failure",
        severity="low",
        signature="seam-adoption-sig",
        occurrence_key="seam-adoption-key",
        source="test",
    )
    assert event_id.startswith("evt_")
    after = busy_retry_metrics_snapshot()
    assert after["attempts"] > before["attempts"], (
        "SqliteReliabilityStore.insert_reliability_event did not record an "
        "attempt in omniagentos.db.busy.BUSY_RETRY_METRICS -- "
        "reliability/store.py is not routing its writes through the shared "
        "busy-retry seam"
    )
    assert after["successes"] > before["successes"]


def _file_conn(path: Path, *, timeout: float) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def test_falsifier_lock_holding_writer_does_not_kill_swarm_dal_write(
    swarm_db_path: str,
) -> None:
    """Proposal falsifier: "A deliberate lock-holding writer still kills a
    swarm/dal write with 'database is locked' after adoption."

    Exercises the REAL production method (``SwarmDal.try_activate_run``, one
    of the methods converted onto ``execute_write_transaction`` via
    ``SwarmDal._write_txn``) under genuine multi-connection contention, not a
    call into ``omniagentos.db.busy`` directly.

    Forces contention to be resolved at the PYTHON level (not SQLite's own
    ``busy_timeout``, which would otherwise silently absorb a short hold and
    prove nothing about adoption either way -- see BUILDER_BRIEF's red-first
    warning) by dropping ``SwarmDal``'s connection timeout to 0, then holds
    ``BEGIN IMMEDIATE`` on a second connection long enough that only the
    Python-level retry loop can resolve it. If ``try_activate_run`` is routed
    through the shared seam it retries until the holder releases and
    succeeds without ever raising ``OperationalError``; the shared
    ``BUSY_RETRY_METRICS`` prove a real retry -- not luck -- resolved it.
    """
    dal = SwarmDal(swarm_db_path)
    try:
        run = dal.create_run(working_dir="/tmp/ws", goal="falsifier", source="test")
        run_id = str(run["id"])

        # Force every OperationalError to surface immediately to Python so the
        # busy.py seam's own retry loop -- not sqlite3's internal busy_timeout
        # wait -- is what resolves the contention.
        dal._connection.execute("PRAGMA busy_timeout=0")

        holder = _file_conn(Path(swarm_db_path), timeout=0.0)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE swarm_runs SET goal = goal WHERE 1=0")

        released = threading.Event()

        def _release_after_delay() -> None:
            time.sleep(0.2)
            holder.commit()
            released.set()

        threading.Thread(target=_release_after_delay, daemon=True).start()

        reset_busy_retry_metrics()
        activated = dal.try_activate_run(run_id)

        assert activated is True
        assert released.is_set()
        snap = BUSY_RETRY_METRICS.snapshot()
        assert snap["retries"] >= 1, (
            "no contention was actually exercised -- the falsifier proves "
            "nothing without at least one observed retry"
        )
        assert snap["successes"] >= 1
        holder.close()
    finally:
        dal.close()


# ---------------------------------------------------------------------------
# HANDOFF/L14-M33 plan step 2: mechanical bypass ratchet.
#
# Adopting the seam in three stores is worth little if the next store lands a
# fresh hand-rolled BEGIN/commit. There is no ruff rule for "wrote SQLite
# without the retry seam", so the check is an AST scan with a FROZEN inventory:
# a module that opens or commits its own transaction outside
# ``omniagentos.db.busy`` must already be on the list, and adopting the seam
# must take it OFF the list. Both directions fail loudly, which is the point --
# the list is a ratchet, and its length is the honest size of the remaining
# clone family (64 modules, ~300 sites), not a claim that adoption is finished.
#
# Scope note, deliberately narrow: this scan flags a module, not a defect.
# ``db/store.py`` and ``db/busy.py`` are ON the list because they *implement*
# the primitives; migration modules are on it because they run before any
# concurrency exists. Their presence is what makes the scan trustworthy -- it
# has no hand-tuned exclusions to hide behind.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _REPO_ROOT / "omniagentos"

_BEGIN_HELPER = "_begin"


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _direct_write_sites(tree: ast.AST) -> list[tuple[int, str]]:
    """Return ``(lineno, kind)`` for every direct transaction-control site.

    Three kinds, matching the three shapes this codebase actually uses:

    * ``begin-literal``  -- ``conn.execute("BEGIN IMMEDIATE")``
    * ``begin-helper``   -- ``self._begin()`` / ``self._store._begin()``
    * ``commit``         -- ``conn.commit()`` (the autocommit-style stores,
      e.g. ``csi/store.py``, which never open an explicit transaction at all
      and are therefore invisible to a BEGIN-only scan)
    """
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node.func)
        if name in {"execute", "executescript"} and node.args:
            first = node.args[0]
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value.strip().upper().startswith("BEGIN")
            ):
                sites.append((node.lineno, "begin-literal"))
        elif name == _BEGIN_HELPER:
            sites.append((node.lineno, "begin-helper"))
        elif name == "commit" and not node.args and not node.keywords:
            sites.append((node.lineno, "commit"))
    return sites


def scan_direct_write_bypasses(package_root: Path) -> dict[str, list[tuple[int, str]]]:
    """Map ``omniagentos/<module>.py`` -> its direct transaction-control sites."""
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(package_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken tree is not our error
            continue
        sites = _direct_write_sites(tree)
        if sites:
            found[str(path.relative_to(package_root.parent))] = sites
    return found


#: Frozen inventory of modules that still control transactions themselves
#: instead of going through ``omniagentos.db.busy``. Enumerated mechanically on
#: 2026-08-13 at the HANDOFF/L14-M33 adoption of swarm/dal + reliability/store.
#: DO NOT append to this list to make a test pass -- route the write through the
#: seam instead. Appending is a deliberate act that needs a reason in the diff.
_KNOWN_BYPASS_MODULES = frozenset(
    {
        "omniagentos/accounts/service.py",
        "omniagentos/api/routes/memlife.py",
        "omniagentos/banking/store.py",
        "omniagentos/budget/simulation.py",
        "omniagentos/chats/store.py",
        "omniagentos/cognitive_txn.py",
        "omniagentos/collab/store.py",
        "omniagentos/connectors/secret_catalog.py",
        "omniagentos/connectors/store.py",
        "omniagentos/conversations/store.py",
        "omniagentos/csi/approval.py",
        "omniagentos/csi/store.py",
        "omniagentos/db/busy.py",
        "omniagentos/db/migrate.py",
        "omniagentos/db/store.py",
        "omniagentos/filesearch/catalog.py",
        # fleetcap adopted the seam outright (per-row execute_write_transaction
        # units) — the waiver main carried for it is removed; the ratchet
        # tightens. It still writes only the external FLEETCAP_DB, now with
        # busy retry instead of hand-rolled commits.
        "omniagentos/formation/telemetry.py",
        "omniagentos/gates/engine.py",
        "omniagentos/grants/store.py",
        "omniagentos/improve/budget.py",
        "omniagentos/intake/orchestrations.py",
        "omniagentos/intake/service.py",
        "omniagentos/interactions/store.py",
        "omniagentos/knowledge/migrate.py",
        "omniagentos/knowledge/store.py",
        "omniagentos/lab/db/store.py",
        "omniagentos/lab/eval/grader.py",
        "omniagentos/longhaul/engine.py",
        "omniagentos/longhaul/store.py",
        "omniagentos/maintenance/cache_gc.py",
        "omniagentos/memlife/backfill.py",
        "omniagentos/memory/store.py",
        "omniagentos/notifications/cap_retention.py",
        "omniagentos/notifications/dal.py",
        "omniagentos/projects/portfolio.py",
        "omniagentos/projects/store.py",
        "omniagentos/provision/capability_policy.py",
        "omniagentos/provision/capability_requests.py",
        "omniagentos/pulse/store.py",
        "omniagentos/reflection/harvest.py",
        "omniagentos/reflection/runner.py",
        "omniagentos/reliability/audit.py",
        "omniagentos/reliability/pipeline.py",
        "omniagentos/reliability/store.py",
        "omniagentos/reliability/worker_drive.py",
        "omniagentos/repomap/service.py",
        "omniagentos/revenue/store.py",
        "omniagentos/routing/limit_state.py",
        "omniagentos/scheduler/loop_budget.py",
        "omniagentos/scheduler/store.py",
        "omniagentos/scope/locks.py",
        "omniagentos/security/secret_rotation.py",
        "omniagentos/sessions/dal.py",
        "omniagentos/sessions/lifecycle_capture.py",
        "omniagentos/sessions/scope_wiring.py",
        "omniagentos/skills/__init__.py",
        # skill-usage telemetry (#429) landed with two direct commit sites; the
        # writer is deliberately best-effort (faults logged, never raised), and
        # seam adoption belongs to the owning lane. Entry unblocks every PR's
        # merge-gate meanwhile — main itself was red on this ratchet without it.
        "omniagentos/skills/usage.py",
        "omniagentos/steward/store.py",
        "omniagentos/swarm/dal.py",
        "omniagentos/swarm/insession.py",
        "omniagentos/taskcontract/store.py",
        "omniagentos/vaultgraph/graph.py",
        "omniagentos/workmodes/manifest.py",
        "omniagentos/workqueue/migrate.py",
        "omniagentos/workqueue/store.py",
    }
)


def test_no_new_module_bypasses_the_busy_seam() -> None:
    """A module not on the frozen list must not hand-roll transaction control."""
    scanned = set(scan_direct_write_bypasses(_PACKAGE_ROOT))
    new = sorted(scanned - _KNOWN_BYPASS_MODULES)
    assert not new, (
        "new direct-write bypass(es) of omniagentos.db.busy:\n  "
        + "\n  ".join(new)
        + "\n\nRoute the write through omniagentos.db.busy.execute_write_transaction "
        "(or a store method that does). Only add to _KNOWN_BYPASS_MODULES with a "
        "reason -- the list is a ratchet, not a waiver."
    )


def test_bypass_ratchet_is_tightened_when_a_store_adopts_the_seam() -> None:
    """The frozen list must not outlive the bypasses it records.

    Without this direction the list silently becomes a stale monument: a store
    could adopt the seam and the inventory would keep reporting 64 outstanding
    modules, so nobody could tell adoption progress from adoption theatre.
    """
    scanned = set(scan_direct_write_bypasses(_PACKAGE_ROOT))
    stale = sorted(_KNOWN_BYPASS_MODULES - scanned)
    assert not stale, (
        "these modules no longer bypass the seam (or no longer exist) -- remove "
        "them from _KNOWN_BYPASS_MODULES:\n  " + "\n  ".join(stale)
    )


def test_the_ratchet_actually_catches_a_new_bypass(tmp_path: Path) -> None:
    """Red-first proof for the ratchet itself, on synthetic sources.

    A ratchet that cannot fail is worse than no ratchet: it reads as a passing
    guard forever. Each of the three bypass shapes is fed to the scanner in a
    throwaway package (never by mutating the real tree), and a seam-using module
    is fed alongside them to show the scanner is not simply flagging everything.
    """
    pkg = tmp_path / "omniagentos"
    pkg.mkdir()
    (pkg / "sneaky_begin.py").write_text(
        'def w(conn):\n    conn.execute("BEGIN IMMEDIATE")\n', encoding="utf-8"
    )
    (pkg / "sneaky_helper.py").write_text(
        "class S:\n    def w(self):\n        self._begin()\n", encoding="utf-8"
    )
    (pkg / "sneaky_commit.py").write_text(
        'def w(conn):\n    conn.execute("INSERT INTO t VALUES (1)")\n    conn.commit()\n',
        encoding="utf-8",
    )
    (pkg / "well_behaved.py").write_text(
        "from omniagentos.db.busy import execute_write_transaction\n\n\n"
        "def w(conn):\n"
        '    return execute_write_transaction(conn, lambda c: c.execute("INSERT INTO t VALUES (1)"), op="ok")\n',
        encoding="utf-8",
    )

    scanned = scan_direct_write_bypasses(pkg)

    assert set(scanned) == {
        "omniagentos/sneaky_begin.py",
        "omniagentos/sneaky_helper.py",
        "omniagentos/sneaky_commit.py",
    }, f"scanner mis-detected the synthetic bypasses: {sorted(scanned)}"
    assert scanned["omniagentos/sneaky_begin.py"] == [(2, "begin-literal")]
    assert scanned["omniagentos/sneaky_helper.py"] == [(3, "begin-helper")]
    assert scanned["omniagentos/sneaky_commit.py"] == [(3, "commit")]

    # And the same scanner, run against the real package, must flag any of these
    # module names had they landed for real -- i.e. the frozen list would reject them.
    assert not (set(scanned) & _KNOWN_BYPASS_MODULES)


# ---------------------------------------------------------------------------
# Adoption completeness inside the two stores this lane owns. The recurring
# defect here is INCOMPLETE PROPAGATION: converting a handful of write methods,
# documenting "adopted", and leaving siblings on the old path. These two tests
# are per-METHOD, so a new write method that skips the seam fails even though
# the module-level ratchet above stays green.
# ---------------------------------------------------------------------------

_WRITE_VERBS = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE"})


def _class_def(module_path: Path, name: str) -> ast.ClassDef:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {module_path}")


def _writes_in(node: ast.AST) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call) or not isinstance(sub.func, ast.Attribute):
            continue
        if sub.func.attr in {"execute", "executemany"} and sub.args:
            first = sub.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                head = first.value.strip().upper().split()
                if head and head[0] in _WRITE_VERBS:
                    out.append((sub.lineno, head[0]))
        elif sub.func.attr == _BEGIN_HELPER:
            out.append((sub.lineno, "BEGIN"))
    return out


def test_every_swarm_dal_write_method_is_under_the_retry_seam() -> None:
    """Every ``SwarmDal`` method that writes must carry ``@_serialized``.

    ``@_serialized`` is what routes the whole call through
    ``run_with_busy_retry``, so it -- not the choice between ``_begin()`` and
    ``_write_txn`` -- is what makes a write retry-protected. The 18 methods this
    lane did not convert to ``execute_write_transaction`` are still covered,
    and this test is the mechanical statement of that claim.
    """
    dal = _class_def(_PACKAGE_ROOT / "swarm" / "dal.py", "SwarmDal")
    unprotected: list[str] = []
    for method in dal.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        writes = _writes_in(method)
        if not writes:
            continue
        decorators = {
            d.id if isinstance(d, ast.Name) else getattr(d, "attr", None)
            for d in method.decorator_list
        }
        if "_serialized" not in decorators:
            unprotected.append(f"{method.name} (line {method.lineno}) writes at {writes[:3]}")
    assert not unprotected, (
        "SwarmDal write method(s) outside the busy-retry seam -- add @_serialized:\n  "
        + "\n  ".join(unprotected)
    )


def test_every_reliability_store_write_routes_through_execute_with_retry() -> None:
    """``_execute_with_retry`` is the reliability store's single seam chokepoint.

    A method that writes must either go through it or be a body helper that
    receives the caller's ``connection`` (the hash-chain appenders), which is
    already inside the caller's retried transaction.
    """
    store = _class_def(_PACKAGE_ROOT / "reliability" / "store.py", "SqliteReliabilityStore")
    unrouted: list[str] = []
    for method in store.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        writes = _writes_in(method)
        if not writes:
            continue
        source = ast.dump(method)
        takes_connection = any(
            arg.arg == "connection" for arg in method.args.args + method.args.kwonlyargs
        )
        if "_execute_with_retry" not in source and not takes_connection:
            unrouted.append(f"{method.name} (line {method.lineno}) writes at {writes[:3]}")
    assert not unrouted, (
        "SqliteReliabilityStore write method(s) bypassing _execute_with_retry:\n  "
        + "\n  ".join(unrouted)
    )


# ---------------------------------------------------------------------------
# Behavioural contracts the adoption must NOT quietly change.
# ---------------------------------------------------------------------------


def test_swarm_dal_keeps_its_five_attempt_budget() -> None:
    """Adoption must not inherit the shared default's wider budget.

    B05's lease/busy controls are specified at five attempts. Passing
    ``DEFAULT_BUSY_RETRY_POLICY`` (ten) instead of the dal's own policy doubles
    how long the scheduler waits before contention becomes visible, and it also
    splits the budget between converted (10) and unconverted (5) write methods.
    """
    from omniagentos.db.busy import DEFAULT_BUSY_RETRY_POLICY
    from omniagentos.swarm.dal import _SWARM_DAL_BUSY_POLICY

    assert _SWARM_DAL_BUSY_POLICY.max_attempts == 5
    assert _SWARM_DAL_BUSY_POLICY.max_attempts != DEFAULT_BUSY_RETRY_POLICY.max_attempts
    assert _SWARM_DAL_BUSY_POLICY.jitter_ms > 0, (
        "the old fixed 0.05*2**n backoff synchronised concurrent writers; "
        "jitter is the one intended behaviour change"
    )


def test_write_txn_refuses_to_run_inside_an_open_transaction(swarm_db_path: str) -> None:
    """The seam must not turn a nested-transaction crash into silent data loss.

    ``execute_write_transaction`` rolls back any residual transaction before its
    ``BEGIN``. Called from inside an outer transaction that is what it would
    discard -- whereas the ``self._begin()`` shape it replaced raised. This pins
    the loud failure so a future caller cannot lose an outer unit of work.
    """
    dal = SwarmDal(swarm_db_path)
    try:
        dal._begin()
        dal._connection.execute(
            "INSERT INTO swarm_runs (id, working_dir, goal, status, created_at, updated_at) "
            "VALUES ('swr_nested_probe', '/tmp/ws', 'outer work', 'queued', '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z')"
        )
        with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
            dal._write_txn(
                lambda conn: conn.execute(
                    "UPDATE swarm_runs SET goal = 'clobbered' WHERE id = 'swr_nested_probe'"
                ),
                op="swarm_dal.nesting_probe",
            )
        # The outer transaction is intact: still open, still holding its row.
        assert dal._connection.in_transaction
        row = dal._connection.execute(
            "SELECT goal FROM swarm_runs WHERE id = 'swr_nested_probe'"
        ).fetchone()
        assert row is not None and row["goal"] == "outer work"
        dal._rollback()
    finally:
        dal.close()


def test_falsifier_lock_holding_writer_does_not_kill_reliability_store_write(
    tmp_path: Path,
) -> None:
    """The falsifier, applied to the second adopted store.

    The salvaged pass only exercised ``swarm/dal.py`` under real contention.
    ``reliability/store.py`` swapped an entire private retry decorator for the
    shared seam, which is the larger change of the two, and it had no
    contention test at all.
    """
    db = tmp_path / "reliability-falsifier.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    migrate_connection(conn)
    conn.commit()
    conn.close()

    store = SqliteReliabilityStore(str(db))
    store._connection.execute("PRAGMA busy_timeout=0")

    holder = _file_conn(db, timeout=0.0)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE reliability_events SET alerted_at = alerted_at WHERE 1=0")

    released = threading.Event()

    def _release_after_delay() -> None:
        time.sleep(0.2)
        holder.commit()
        released.set()

    threading.Thread(target=_release_after_delay, daemon=True).start()

    reset_busy_retry_metrics()
    event_id = store.insert_reliability_event(
        failure_class="test_failure",
        severity="low",
        signature="falsifier-reliability",
        occurrence_key="falsifier-reliability-key",
        source="test",
    )

    assert event_id.startswith("evt_")
    assert released.is_set()
    snap = BUSY_RETRY_METRICS.snapshot()
    assert snap["retries"] >= 1, (
        "no contention was actually exercised -- the falsifier proves nothing "
        "without at least one observed retry"
    )
    holder.close()
