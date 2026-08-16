"""Dispatcher-only enforcement: who may EXECUTE, and who may only submit (D8a).

the operator's ruling 2026-08-13: personal machines are dispatchers — outgoing only.
They enqueue and they watch; execution happens on fleet workers. The surface is
queue authz, so these tests pin the properties that make it one rather than a
suggestion:

* role is SERVER-DERIVED — a machine that asks for ``role='worker'`` and is not
  on the allowlist is stored as a dispatcher. There is no path that elevates.
* the two refusal reasons stay DISTINCT, because they call for opposite operator
  actions: 'dispatcher-role-cannot-claim' is the design working, while
  'machine-not-allowlisted' is a row somebody needs to go look at.
* the flag defaults to LOG-ONLY and log-only changes nothing but the log — the
  existing fleet must be unaffected until an operator deliberately flips it.
* ``device_class`` is audit metadata and never reaches an authz decision. That
  one is asserted against the SOURCE as well as the behaviour, because the way
  an informational column becomes a permission is a later refactor that finds it
  convenient, not a decision anyone writes down.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

from omniagentos.workqueue import server as server_module
from omniagentos.workqueue import store as store_module
from omniagentos.workqueue.migrate import migrations_dir
from omniagentos.workqueue.server import create_app
from omniagentos.workqueue.store import (
    REFUSAL_DISPATCHER_ROLE,
    REFUSAL_NOT_ALLOWLISTED,
    WorkQueueStore,
)
from tests.workqueue.conftest import submit

TOKEN = "b6f0c8de0b0c4d9f8b2a1c3e5d7f9a1b"

#: The shipped allowlist in configs/workqueue.yaml. Duplicated here on purpose:
#: the point of the assertion below is to fail if the shipped list drifts from
#: the ruling, and a test that read the list and compared it with itself would
#: pass through any edit at all. Note 'acmeuni' is prose — the id is the long one.
#: The last two are live claiming workers present in wq_machines at seeding
#: time (2026-08-13) but absent from the original 5-box list; the allowlist must
#: be a SUPERSET of every beating worker or the enforce-flip idles one, so they
#: are pinned here too. This constant IS the ruling — edit it and the shipped
#: yaml together (that is what the assertion enforces).
SEEDED_ALLOWLIST = [
    "macstudio-a",
    "macstudio-b",
    "initech-roi-calculator",
    "acmeuniversityredditprompts",
    "initech-dev",
    "vps-00000000",
    "Initechs-Mac-mini",
]

#: A personal machine: real shape, deliberately not on the list.
LAPTOP = "owners-macbook-pro"


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def write_config(path, allowlist) -> None:
    """A minimal workqueue.yaml carrying just the key under test."""
    entries = "\n".join(f"  - {machine_id}" for machine_id in allowlist)
    path.write_text(f"worker_allowlist:\n{entries}\n" if entries else "worker_allowlist: []\n")


@pytest.fixture
def no_allowlist_ttl(monkeypatch):
    """Re-read the allowlist on every check, so a rewrite is visible immediately.

    The production TTL bounds how long a just-revoked machine keeps claiming; a
    test that waited it out would be asserting ``time.monotonic`` works.
    """
    monkeypatch.setattr(store_module, "WORKER_ALLOWLIST_TTL_S", 0.0)


@pytest.fixture
def enforce(monkeypatch):
    monkeypatch.setenv("WQ_ROLE_ENFORCE", "1")


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "workqueue.yaml"
    write_config(path, [])
    return path


@pytest.fixture
def roles_store(db_path, config_path, no_allowlist_ttl):
    store = WorkQueueStore(db_path, config_path=config_path)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def roles_client(roles_store, monkeypatch):
    monkeypatch.setenv("WQ_TOKEN", TOKEN)
    with TestClient(create_app(store=roles_store, reaper=False)) as client:
        yield client


def enroll(store, machine_id, **extra):
    store.enroll_machine(
        {
            "machine_id": machine_id,
            "hostname": f"{machine_id}.local",
            "os": "darwin",
            "labels": [],
            "max_concurrent": 1,
            **extra,
        }
    )


def row_for(store, machine_id) -> dict:
    return next(row for row in store.list_machines() if row["machine_id"] == machine_id)


# -- role derivation at enrollment ----------------------------------------


def test_a_non_allowlisted_machine_asking_for_worker_is_stored_as_a_dispatcher(
    roles_store, config_path
):
    """The elevation attempt, in its most direct form: just ask for it."""
    write_config(config_path, ["macstudio-a"])
    enroll(roles_store, LAPTOP, role="worker")

    assert row_for(roles_store, LAPTOP)["role"] == "dispatcher"
    # ...and it DID enroll. A dispatcher is a member of the pool that submits and
    # observes; refusing enrollment would only teach people to stop enrolling,
    # and an unenrolled machine is one the operator cannot see.
    assert row_for(roles_store, LAPTOP)["machine_id"] == LAPTOP


def test_an_allowlisted_machine_enrolls_as_a_worker(roles_store, config_path):
    write_config(config_path, ["macstudio-a"])
    enroll(roles_store, "macstudio-a")
    assert row_for(roles_store, "macstudio-a")["role"] == "worker"


def test_a_dispatcher_body_cannot_demote_an_allowlisted_worker(roles_store, config_path):
    """The body is ignored in BOTH directions — it is not consulted at all."""
    write_config(config_path, ["macstudio-a"])
    enroll(roles_store, "macstudio-a", role="dispatcher")
    assert row_for(roles_store, "macstudio-a")["role"] == "worker"


def test_re_enrollment_re_derives_the_role_from_the_current_allowlist(roles_store, config_path):
    """Editing the list + re-running enroll.sh IS the promotion/demotion path."""
    write_config(config_path, [])
    enroll(roles_store, "initech-dev")
    assert row_for(roles_store, "initech-dev")["role"] == "dispatcher"

    write_config(config_path, ["initech-dev"])
    enroll(roles_store, "initech-dev")
    assert row_for(roles_store, "initech-dev")["role"] == "worker"

    write_config(config_path, [])
    enroll(roles_store, "initech-dev")
    assert row_for(roles_store, "initech-dev")["role"] == "dispatcher"


def test_an_unreadable_config_grants_nothing(roles_store, config_path):
    """Fail CLOSED: a config the server cannot read is never a reason to trust."""
    config_path.write_text("worker_allowlist: [unclosed\n")
    enroll(roles_store, "macstudio-a")
    assert row_for(roles_store, "macstudio-a")["role"] == "dispatcher"

    config_path.unlink()
    assert roles_store.worker_allowlist() == frozenset()


def test_allowlist_matching_is_byte_identical(roles_store, config_path):
    """No case folding, no prefix match, no alias table.

    'acmeuni' is the prose alias the runbook and the fleet comment block use for
    acmeuniversityredditprompts. A lenient comparison here is exactly how a
    prose alias turns into a permission.
    """
    write_config(config_path, ["acmeuniversityredditprompts"])
    for near_miss in ("acmeuni", "ACMEUNIVERSITYREDDITPROMPTS", "acmeuniversityredditprompts-2"):
        enroll(roles_store, near_miss)
        assert row_for(roles_store, near_miss)["role"] == "dispatcher", near_miss
    enroll(roles_store, "acmeuniversityredditprompts")
    assert row_for(roles_store, "acmeuniversityredditprompts")["role"] == "worker"


# -- the two named refusals -----------------------------------------------


def test_a_dispatcher_claim_is_refused_by_role(roles_client, roles_store, config_path, enforce):
    write_config(config_path, ["macstudio-a"])
    unit_id = roles_store.enqueue(submit("role-1"))[0]
    enroll(roles_store, LAPTOP)  # not allowlisted ⇒ enrolled as a dispatcher
    assert row_for(roles_store, LAPTOP)["role"] == "dispatcher"

    response = roles_client.post(
        "/v1/claim",
        json={"machine_id": LAPTOP, "worker_id": "w1", "labels": []},
        headers=auth(),
    )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == REFUSAL_DISPATCHER_ROLE

    # Refused BEFORE the store was touched: no lease, no attempt row, no
    # generation bump. A refusal that still burned a candidate's attempt budget
    # would turn a misconfigured laptop into a way to exhaust the queue.
    unit = roles_store.get_unit(unit_id)
    assert unit["state"] == "queued"
    assert unit["attempt"] == 0
    assert unit["lease_owner"] is None
    assert unit["lease_generation"] == 0
    assert roles_store.list_attempts(unit_id) == []


def test_a_non_allowlisted_machine_claim_is_refused_by_the_allowlist(
    roles_client, roles_store, config_path, enforce
):
    """The OTHER reason: the row says 'worker', the list has never heard of it.

    This is the shape of every pre-002 row (the column DEFAULT is 'worker'), and
    it is the finding RUNBOOK §12's preflight exists to clear. It is also what
    revocation looks like: the machine was allowlisted when it enrolled, and the
    operator has since taken it off the list without touching the wq_machines row.
    """
    write_config(config_path, ["macstudio-b"])
    roles_store.enqueue(submit("role-2"))
    enroll(roles_store, "macstudio-b")
    assert row_for(roles_store, "macstudio-b")["role"] == "worker"

    write_config(config_path, [])  # revoked, no restart, no re-enrollment

    response = roles_client.post(
        "/v1/claim",
        json={"machine_id": "macstudio-b", "worker_id": "w1", "labels": []},
        headers=auth(),
    )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == REFUSAL_NOT_ALLOWLISTED
    # Still 'worker' on the row — the allowlist refused it, not the role column,
    # and the two reasons must not be reachable through each other.
    assert row_for(roles_store, "macstudio-b")["role"] == "worker"


def test_an_unenrolled_machine_is_not_allowlisted_rather_than_a_dispatcher(
    roles_client, roles_store, config_path, enforce
):
    """No row at all: there is no role to report, so the honest reason is the list."""
    write_config(config_path, [])
    roles_store.enqueue(submit("role-3"))
    response = roles_client.post(
        "/v1/claim",
        json={"machine_id": "ghost", "worker_id": "w1", "labels": []},
        headers=auth(),
    )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == REFUSAL_NOT_ALLOWLISTED


def test_the_two_reasons_are_distinct_strings():
    """Merging them would erase the only signal that separates two operator actions."""
    assert REFUSAL_DISPATCHER_ROLE != REFUSAL_NOT_ALLOWLISTED
    assert REFUSAL_DISPATCHER_ROLE == "dispatcher-role-cannot-claim"
    assert REFUSAL_NOT_ALLOWLISTED == "machine-not-allowlisted"


def test_a_role_refusal_is_403_not_401(roles_client, roles_store, config_path, enforce):
    """The token was fine. Conflating the two would send an operator token-hunting."""
    write_config(config_path, [])
    enroll(roles_store, LAPTOP)
    response = roles_client.post(
        "/v1/claim",
        json={"machine_id": LAPTOP, "worker_id": "w1", "labels": []},
        headers=auth(),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    # ...and the auth boundary is untouched: no token is still 401, not 403.
    assert (
        roles_client.post(
            "/v1/claim", json={"machine_id": LAPTOP, "worker_id": "w1", "labels": []}
        ).status_code
        == 401
    )


# -- the enforcement flag --------------------------------------------------


def test_log_only_is_the_default_and_the_claim_still_succeeds(
    roles_client, roles_store, config_path, monkeypatch, capsys
):
    """Flag unset ⇒ nothing changes but the log. This is the 72 h rehearsal."""
    monkeypatch.delenv("WQ_ROLE_ENFORCE", raising=False)
    write_config(config_path, [])
    unit_id = roles_store.enqueue(submit("log-only"))[0]
    enroll(roles_store, LAPTOP)

    response = roles_client.post(
        "/v1/claim",
        json={"machine_id": LAPTOP, "worker_id": "w1", "labels": []},
        headers=auth(),
    )
    assert response.status_code == 200, "log-only mode must not change the claim path"
    assert response.json()["unit"]["id"] == unit_id
    assert roles_store.get_unit(unit_id)["lease_owner"] == f"{LAPTOP}:w1"

    # ...and the would-deny is on the record, with the machine and the reason.
    logged = capsys.readouterr().err
    assert "wq-role-deny" in logged
    assert "mode=log-only" in logged
    assert LAPTOP in logged
    assert REFUSAL_DISPATCHER_ROLE in logged


def test_a_broken_stderr_never_fails_a_log_only_claim(
    roles_client, roles_store, config_path, monkeypatch
):
    """F5 (cross-lineage review): the would-deny log line is evidence, not a gate.

    Log-only mode promises a byte-identical claim path. If the ``print`` to a
    broken/full/closed stderr raised, the exception would rise through the claim
    handler and return 500 — turning a logging failure into a blocked claim for a
    legitimate worker. A broken stderr must lose the line, never the claim.
    """
    monkeypatch.delenv("WQ_ROLE_ENFORCE", raising=False)
    write_config(config_path, [])
    unit_id = roles_store.enqueue(submit("broken-stderr"))[0]
    enroll(roles_store, LAPTOP)

    class _BrokenStderr:
        def write(self, _data):
            raise OSError("stderr is closed")

        def flush(self):
            raise OSError("stderr is closed")

    monkeypatch.setattr(sys, "stderr", _BrokenStderr())
    response = roles_client.post(
        "/v1/claim",
        json={"machine_id": LAPTOP, "worker_id": "w1", "labels": []},
        headers=auth(),
    )
    assert response.status_code == 200, "a broken stderr must not fail a log-only claim"
    assert response.json()["unit"]["id"] == unit_id


@pytest.mark.parametrize("value", ["0", "", "true", "yes", "1 ", "01"])
def test_only_the_exact_string_1_enforces(
    roles_client, roles_store, config_path, monkeypatch, value
):
    """Generous truthiness is how a stray value in a launchd plist idles the pool."""
    monkeypatch.setenv("WQ_ROLE_ENFORCE", value)
    write_config(config_path, [])
    roles_store.enqueue(submit(f"flag-{value.strip() or 'empty'}"))
    enroll(roles_store, LAPTOP)

    response = roles_client.post(
        "/v1/claim",
        json={"machine_id": LAPTOP, "worker_id": "w1", "labels": []},
        headers=auth(),
    )
    assert response.status_code == 200, f"WQ_ROLE_ENFORCE={value!r} must not enforce"


def test_enforce_mode_logs_the_refusal_too(roles_client, roles_store, config_path, enforce, capsys):
    """One stream, two modes — an operator greps the same marker after the flip."""
    write_config(config_path, [])
    enroll(roles_store, LAPTOP)
    roles_client.post(
        "/v1/claim",
        json={"machine_id": LAPTOP, "worker_id": "w1", "labels": []},
        headers=auth(),
    )
    logged = capsys.readouterr().err
    assert "wq-role-deny" in logged and "mode=enforce" in logged


def test_an_allowed_machine_logs_nothing(roles_client, roles_store, config_path, enforce, capsys):
    write_config(config_path, ["macstudio-a"])
    roles_store.enqueue(submit("quiet"))
    enroll(roles_store, "macstudio-a")
    assert (
        roles_client.post(
            "/v1/claim",
            json={"machine_id": "macstudio-a", "worker_id": "w1", "labels": []},
            headers=auth(),
        ).status_code
        == 200
    )
    assert "wq-role-deny" not in capsys.readouterr().err


# -- the existing fleet ----------------------------------------------------


def test_the_shipped_allowlist_is_the_wave_1_seed(store):
    """Config drift is a silent fleet outage, so the shipped list is pinned."""
    assert sorted(store.worker_allowlist()) == sorted(SEEDED_ALLOWLIST)


def test_every_seeded_machine_can_still_claim_with_enforcement_on(store, monkeypatch, enforce):
    """The regression that matters: turning the flag on must not idle the fleet.

    Uses the REAL configs/workqueue.yaml (the ``store`` fixture's default config
    path), so this fails if the seed is edited without editing the ruling.
    """
    monkeypatch.setenv("WQ_TOKEN", TOKEN)
    for index, machine_id in enumerate(SEEDED_ALLOWLIST):
        store.enqueue(submit(f"fleet-{index}"))
        enroll(store, machine_id)
        assert row_for(store, machine_id)["role"] == "worker", machine_id

    with TestClient(create_app(store=store, reaper=False)) as client:
        for machine_id in SEEDED_ALLOWLIST:
            response = client.post(
                "/v1/claim",
                json={"machine_id": machine_id, "worker_id": "w1", "labels": []},
                headers=auth(),
            )
            assert response.status_code == 200, f"{machine_id} was refused: {response.text}"


# -- device_class grants nothing ------------------------------------------


def test_device_class_is_stored_and_returned(roles_store, config_path):
    write_config(config_path, [])
    enroll(roles_store, LAPTOP, device_class="personal-laptop")
    assert row_for(roles_store, LAPTOP)["device_class"] == "personal-laptop"


def test_device_class_cannot_buy_a_claim(roles_client, roles_store, config_path, enforce):
    """The self-declared string that most looks like a permission still is not one."""
    write_config(config_path, [])
    roles_store.enqueue(submit("dc"))
    enroll(roles_store, LAPTOP, device_class="fleet-worker")

    response = roles_client.post(
        "/v1/claim",
        json={"machine_id": LAPTOP, "worker_id": "w1", "labels": []},
        headers=auth(),
    )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == REFUSAL_DISPATCHER_ROLE


def test_device_class_and_role_are_independent(roles_store, config_path):
    """An allowlisted box keeps 'worker' whatever it calls itself, and vice versa."""
    write_config(config_path, ["initech-dev"])
    enroll(roles_store, "initech-dev", device_class="personal-laptop")
    assert row_for(roles_store, "initech-dev")["role"] == "worker"

    enroll(roles_store, LAPTOP, device_class="prod-host")
    assert row_for(roles_store, LAPTOP)["role"] == "dispatcher"


def _executable_source(obj) -> str:
    """``obj``'s code with comments and docstrings stripped.

    Prose about ``device_class`` is exactly what these functions SHOULD carry —
    the warning label is half the point of the column. A grep over raw source
    would therefore flag the warning itself, so the check runs over what actually
    EXECUTES: ``ast.unparse`` drops comments, and docstrings are removed here.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            if ast.get_docstring(node) is not None:
                node.body = node.body[1:]
    return ast.unparse(tree)


def test_no_authz_code_path_reads_device_class():
    """Asserted against the SOURCE, because behaviour cannot prove an absence.

    The three functions below are the whole role decision. If a later change
    finds it convenient to consult ``device_class`` in one of them, an
    operator-facing audit label silently becomes a self-declared permission —
    the machine supplies the value in its own enroll body. This test is the
    tripwire on that refactor.
    """
    for authz in (
        WorkQueueStore.claim_role_refusal,
        WorkQueueStore.role_for,
        WorkQueueStore.worker_allowlist,
    ):
        assert "device_class" not in _executable_source(authz), authz.__name__

    # ...and the transport that decides the enforcement MODE never sees it either.
    assert "device_class" not in _executable_source(server_module)


# -- migration behaviour ---------------------------------------------------


def _apply_001_only(db_path) -> None:
    """A queue DB as it looked before D8a: 001 applied, nothing else."""
    connection = sqlite3.connect(str(db_path))
    try:
        connection.executescript(
            (migrations_dir() / "001_workqueue.sql").read_text(encoding="utf-8")
        )
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at, checksum) VALUES (1, ?, ?)",
            ("2026-08-11T12:00:00Z", "pre-existing"),
        )
        connection.execute(
            "INSERT INTO wq_machines (machine_id, hostname, os, enrolled_at) VALUES (?, ?, ?, ?)",
            ("macstudio-a", "macstudio-a.local", "darwin", "2026-08-11T12:00:00Z"),
        )
        connection.commit()
    finally:
        connection.close()


def test_002_applies_to_an_existing_db_and_defaults_every_row_to_worker(db_path, config_path):
    """The no-outage property: an in-place upgrade demotes nobody.

    A migration that flipped the live fleet to 'dispatcher' would be an outage
    wearing a schema change's clothes. Demoting the rows that should not claim is
    the operator's preflight, deliberately, before the flag is flipped.
    """
    _apply_001_only(db_path)

    store = WorkQueueStore(db_path, config_path=config_path)
    try:
        row = row_for(store, "macstudio-a")
        assert row["role"] == "worker", "an existing row must keep claiming across the upgrade"
        assert row["device_class"] is None
    finally:
        store.close()


def test_a_pre_002_row_is_still_caught_by_the_allowlist(db_path, config_path, no_allowlist_ttl):
    """Which is why the allowlist check exists as well as the role column.

    Every pre-002 row carries the 'worker' DEFAULT regardless of who it is, so a
    role-only check would let each of them straight through.
    """
    _apply_001_only(db_path)
    write_config(config_path, [])

    store = WorkQueueStore(db_path, config_path=config_path)
    try:
        assert store.claim_role_refusal("macstudio-a") == REFUSAL_NOT_ALLOWLISTED
    finally:
        store.close()


def test_the_role_column_rejects_a_value_that_is_neither(db_path, config_path):
    """The CHECK constraint is the last line: no third role can be written."""
    store = WorkQueueStore(db_path, config_path=config_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                "INSERT INTO wq_machines (machine_id, hostname, os, enrolled_at, role) "
                "VALUES ('x', 'x', 'darwin', '2026-08-11T12:00:00Z', 'admin')"
            )
    finally:
        store.close()
