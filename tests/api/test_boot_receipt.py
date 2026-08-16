"""Boot composition receipt — the registry, and the one GET that exposes it.

The point of the receipt is that a HALF-COMPOSED process stops being invisible.
So these tests care about exactly two things: a swallowed boot failure produces
a ``degraded`` row (and nothing else changes), and no absence is ever rendered
as health — ``skipped``/``disabled``/absent must never read as ``ok``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omniagentos.api.boot_receipt import (
    _MAX_DETAIL_CHARS,
    STATUS_DEGRADED,
    STATUS_DISABLED,
    STATUS_OK,
    STATUS_SKIPPED,
    BootReceipt,
    boot_receipt,
)
from omniagentos.api.main import app


@pytest.fixture
def receipt() -> BootReceipt:
    return BootReceipt()


# ------------------------------------------------------------- recording ----


def test_unmeasured_receipt_does_not_read_as_healthy(receipt: BootReceipt) -> None:
    """A process whose lifespan never ran must be DISTINGUISHABLE from a clean boot.

    ``degraded_count == 0`` is byte-identical between the two, so this asserts the
    fields that actually separate them. The earlier version of this test asserted
    the favourable value — it would have passed while the unmeasured case reported
    health, which is precisely how this class of bug survives a test suite.
    """
    snap = receipt.snapshot()
    assert snap["measured"] is False
    assert snap["status"] == "unmeasured"
    assert snap["steps"] == []
    assert snap["degraded"] == []
    assert snap["started_at"] is None
    assert snap["completed_at"] is None
    # The trap, stated: the old health predicate cannot tell these apart.
    assert snap["degraded_count"] == 0


def test_receipt_status_tracks_the_lifecycle(receipt: BootReceipt) -> None:
    assert receipt.snapshot()["status"] == "unmeasured"
    receipt.start()
    assert receipt.snapshot()["status"] == "composing"
    assert receipt.snapshot()["measured"] is True
    receipt.record_ok("swarm-resume")
    receipt.complete()
    assert receipt.snapshot()["status"] == STATUS_OK
    receipt.record_degraded("vault-index", RuntimeError("boom"))
    assert receipt.snapshot()["status"] == STATUS_DEGRADED


def test_degraded_records_exception_type_and_message(receipt: BootReceipt) -> None:
    receipt.start()
    receipt.record_degraded("vault-index", RuntimeError("playbook dir missing"), "vault index")
    snap = receipt.snapshot()

    (step,) = snap["steps"]
    assert step["subsystem"] == "vault-index"
    assert step["status"] == STATUS_DEGRADED
    assert step["error_type"] == "RuntimeError"
    assert "playbook dir missing" in step["detail"]
    assert step["recorded_at"].endswith("Z")
    assert snap["degraded"] == ["vault-index"]
    assert snap["degraded_count"] == 1


def test_statuses_are_distinct_and_none_of_them_is_ok(receipt: BootReceipt) -> None:
    """skipped / disabled are their own facts. Collapsing either into ok or into
    degraded is the failure this module exists to prevent."""
    receipt.record_ok("swarm-resume")
    receipt.record_skipped("routine-seed", "store unavailable")
    receipt.record_disabled("vault-index", "OMNIAGENTOS_INDEX_VAULT_ON_STARTUP")
    receipt.record_degraded("employee-seed", ValueError("boom"))

    by_name = {s["subsystem"]: s["status"] for s in receipt.snapshot()["steps"]}
    assert by_name == {
        "swarm-resume": STATUS_OK,
        "routine-seed": STATUS_SKIPPED,
        "vault-index": STATUS_DISABLED,
        "employee-seed": STATUS_DEGRADED,
    }
    counts = receipt.snapshot()["counts"]
    assert counts == {
        STATUS_OK: 1,
        STATUS_SKIPPED: 1,
        STATUS_DISABLED: 1,
        STATUS_DEGRADED: 1,
    }


def test_steps_keep_lifespan_order_and_a_retry_replaces_in_place(
    receipt: BootReceipt,
) -> None:
    receipt.record_degraded("swarm-resume", OSError("nope"))
    receipt.record_ok("routine-seed")
    receipt.record_ok("swarm-resume", "second attempt")

    steps = receipt.snapshot()["steps"]
    assert [s["subsystem"] for s in steps] == ["swarm-resume", "routine-seed"]
    assert steps[0]["status"] == STATUS_OK
    assert steps[0]["error_type"] is None
    assert receipt.snapshot()["degraded_count"] == 0


def test_start_clears_a_previous_run(receipt: BootReceipt) -> None:
    receipt.record_degraded("swarm-resume", OSError("nope"))
    receipt.start()
    snap = receipt.snapshot()
    assert snap["steps"] == []
    assert snap["started_at"] is not None
    assert snap["completed_at"] is None
    receipt.complete()
    assert receipt.snapshot()["completed_at"] is not None


def test_detail_is_bounded(receipt: BootReceipt) -> None:
    receipt.record_degraded("vault-index", RuntimeError("x" * 5000))
    detail = receipt.snapshot()["steps"][0]["detail"]
    assert len(detail) <= _MAX_DETAIL_CHARS


def test_recording_never_raises_even_on_a_hostile_exception(receipt: BootReceipt) -> None:
    """Recording runs INSIDE a startup ``except``. If it can raise, it converts a
    logged degradation into a failed boot — the exact outcome this replaces."""

    class Hostile(Exception):
        def __str__(self) -> str:
            raise ValueError("no string for you")

    receipt.record_degraded("swarm-resume", Hostile())
    step = receipt.snapshot()["steps"][0]
    assert step["status"] == STATUS_DEGRADED
    assert step["detail"]


class _HostileStr:
    """An object whose ``__str__`` raises — what a failing step may hand over."""

    def __str__(self) -> str:
        raise RuntimeError("hostile stringification")


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda r: r.record_ok("s", detail=_HostileStr()), id="record_ok"),
        pytest.param(lambda r: r.record_skipped("s", _HostileStr()), id="record_skipped"),
        pytest.param(lambda r: r.record_disabled("s", _HostileStr()), id="record_disabled"),
        pytest.param(lambda r: r.record("s", STATUS_OK, _HostileStr()), id="record"),
        pytest.param(lambda r: r.record_ok(_HostileStr()), id="hostile_subsystem"),
        pytest.param(
            lambda r: r.record_degraded("s", RuntimeError("x"), detail=_HostileStr()),
            id="record_degraded_detail",
        ),
    ],
)
def test_no_recorder_can_escape_on_hostile_string_work(
    receipt: BootReceipt, call: object
) -> None:
    """Every PUBLIC recorder guards its WHOLE body, not just the store.

    The string work (``_clip``, f-string interpolation) happens before anything
    touches the registry, so guarding only ``_put`` left ``record_ok(detail=obj)``
    able to raise straight out of a lifespan ``except`` block and crash boot
    (Class-A review F1, repro F1.py).
    """
    call(receipt)  # type: ignore[operator]  -- must not raise, that IS the assertion


def test_degraded_detail_is_redacted(receipt: BootReceipt) -> None:
    """Swallowed text is served over HTTP; credential shapes must not survive it."""
    secret = "sk-live-AAAABBBBCCCCDDDDEEEEFFFF11112222"
    receipt.record_degraded("vault-index", RuntimeError(f"bad note: api_key: {secret}"))
    assert secret not in receipt.snapshot()["steps"][0]["detail"]


def test_message_override_never_reads_the_exception_text(receipt: BootReceipt) -> None:
    """For an exception that AGGREGATES third-party text, the message is not read.

    Redaction catches credential shapes, not arbitrary relayed file content, so
    ``vault-index`` supplies its own allow-listed summary instead.
    """
    leaked = "line 3: password_hash = deadbeefcafebabe"
    receipt.record_degraded(
        "vault-index",
        RuntimeError(f"1 failed: skill-poisoned.md: ScannerError: {leaked}"),
        message_override="failing notes=1: skill-poisoned.md",
    )
    step = receipt.snapshot()["steps"][0]
    assert leaked not in step["detail"]
    assert "skill-poisoned.md" in step["detail"]
    assert step["error_type"] == "RuntimeError"


def test_vault_index_summary_relays_only_note_basenames() -> None:
    """The allow-list, directly: basenames + a count, and nothing else."""
    from omniagentos.api.main import _vault_index_failure_summary

    exc = RuntimeError(
        "indexed 2 playbook note(s); 2 failed: "
        "skill-a.md: ScannerError: while scanning, found 'api_key: sk-live-SECRETVALUE'; "
        "skill-b.md: ValueError: /Users/owner/private/vault/note.md is malformed"
    )
    summary = _vault_index_failure_summary(exc)
    assert "sk-live-SECRETVALUE" not in summary
    assert "/Users/owner/private" not in summary
    assert "ScannerError" not in summary
    assert "skill-a.md" in summary and "skill-b.md" in summary
    assert "failing notes=" in summary


def test_module_receipt_is_a_single_process_wide_instance() -> None:
    assert boot_receipt() is boot_receipt()


# ------------------------------------------------------------- lifespan -----


def test_lifespan_records_every_optional_subsystem(monkeypatch: pytest.MonkeyPatch) -> None:
    """With every flag off, the receipt still names all six swallow-site
    subsystems (plus the orch-resume thread) as ``disabled`` — a composition
    receipt that omits an unrun subsystem is the absence it exists to kill."""
    import asyncio

    from omniagentos.api import main as api_main

    for flag in (
        "OMNIAGENTOS_ORCH_RESUME_ON_STARTUP",
        "OMNIAGENTOS_SWARM_RESUME_ON_STARTUP",
        "OMNIAGENTOS_SEED_ROUTINES_ON_STARTUP",
        "OMNIAGENTOS_SEED_EMPLOYEES_ON_STARTUP",
        "OMNIAGENTOS_INDEX_VAULT_ON_STARTUP",
    ):
        monkeypatch.setenv(flag, "0")
    monkeypatch.setattr(api_main, "_assert_explicit_control_plane_db", lambda: None)
    monkeypatch.setattr(api_main, "_assert_migration_inventory", lambda: None)
    monkeypatch.setattr(api_main, "assert_startup_coherence", lambda: None)
    monkeypatch.setattr(api_main, "_mint_session_token_on_first_boot", lambda: None)
    monkeypatch.setattr(
        "omniagentos.swarm.scheduler.shutdown_default_schedulers", lambda: None, raising=False
    )

    async def drive() -> None:
        async with api_main.lifespan(api_main.app):
            pass

    asyncio.run(drive())

    snap = boot_receipt().snapshot()
    names = {s["subsystem"]: s["status"] for s in snap["steps"]}
    assert set(names) == {
        "orch-resume",
        "swarm-resume",
        "routine-seed-store",
        "routine-seed",
        "w3-health-monitor",
        "employee-seed",
        "vault-index",
    }
    assert set(names.values()) == {STATUS_DISABLED}
    assert snap["degraded_count"] == 0
    assert snap["started_at"] is not None
    assert snap["completed_at"] is not None


def _run_lifespan(monkeypatch: pytest.MonkeyPatch, **flags: str) -> dict[str, Any]:
    """Drive the real lifespan with the refusal guards stubbed; return the receipt."""
    import asyncio

    from omniagentos.api import main as api_main

    off = {
        "OMNIAGENTOS_ORCH_RESUME_ON_STARTUP": "0",
        "OMNIAGENTOS_SWARM_RESUME_ON_STARTUP": "0",
        "OMNIAGENTOS_SEED_ROUTINES_ON_STARTUP": "0",
        "OMNIAGENTOS_SEED_EMPLOYEES_ON_STARTUP": "0",
        "OMNIAGENTOS_INDEX_VAULT_ON_STARTUP": "0",
    }
    off.update(flags)
    for name, value in off.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(api_main, "_assert_explicit_control_plane_db", lambda: None)
    monkeypatch.setattr(api_main, "_assert_migration_inventory", lambda: None)
    monkeypatch.setattr(api_main, "assert_startup_coherence", lambda: None)
    monkeypatch.setattr(api_main, "_mint_session_token_on_first_boot", lambda: None)
    monkeypatch.setattr(
        "omniagentos.swarm.scheduler.shutdown_default_schedulers", lambda: None, raising=False
    )

    async def drive() -> None:
        async with api_main.lifespan(api_main.app):
            pass

    asyncio.run(drive())
    return boot_receipt().snapshot()


def _step(snap: dict[str, Any], subsystem: str) -> dict[str, Any]:
    return next(s for s in snap["steps"] if s["subsystem"] == subsystem)


def test_lifespan_records_a_real_swallowed_failure_as_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The integration half: make a lifespan step RAISE and prove the except block
    records it — and that boot still completes, unchanged (Class-A review F3)."""

    def boom(**_: object) -> dict[str, Any]:
        raise RuntimeError("swarm DAL is wedged")

    monkeypatch.setattr("omniagentos.swarm.scheduler.resume_stale_swarms", boom)

    snap = _run_lifespan(monkeypatch, OMNIAGENTOS_SWARM_RESUME_ON_STARTUP="1")

    step = _step(snap, "swarm-resume")
    assert step["status"] == STATUS_DEGRADED
    assert step["error_type"] == "RuntimeError"
    assert "swarm DAL is wedged" in step["detail"]
    assert snap["degraded"] == ["swarm-resume"]
    assert snap["status"] == STATUS_DEGRADED
    # Fail-open is unchanged: the lifespan still reached its completion mark.
    assert snap["completed_at"] is not None


def test_lifespan_swarm_resume_ok_detail_carries_no_filesystem_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HEALTHY path is a disclosure surface too: ``resume_stale_swarms``
    returns the absolute control-plane DB path in its summary (review A2)."""
    db_path = "/private/tmp/ocrit-secret-dir/swarm.sqlite3"

    def fake_resume(**_: object) -> dict[str, Any]:
        return {
            "db_path": db_path,
            "reconciled": {"a": 1},
            "resumed": ["run-abc"],
            "skipped_fresh": [],
            "errors": [],
            "candidates": 3,
        }

    monkeypatch.setattr("omniagentos.swarm.scheduler.resume_stale_swarms", fake_resume)

    snap = _run_lifespan(monkeypatch, OMNIAGENTOS_SWARM_RESUME_ON_STARTUP="1")

    step = _step(snap, "swarm-resume")
    assert step["status"] == STATUS_OK
    assert db_path not in step["detail"]
    assert "run-abc" not in step["detail"]
    assert "candidates=3" in step["detail"]
    assert "resumed=1" in step["detail"]


def test_lifespan_records_skipped_when_the_store_precondition_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store unavailable → the store step is degraded and the two seeds that
    depend on it are SKIPPED. Neither may read as ok, and neither may read as
    its own failure."""

    def no_store() -> object:
        raise RuntimeError("control-plane DB unavailable")

    monkeypatch.setattr("omniagentos.api.deps.get_store", no_store)

    snap = _run_lifespan(monkeypatch, OMNIAGENTOS_SEED_ROUTINES_ON_STARTUP="1")

    assert _step(snap, "routine-seed-store")["status"] == STATUS_DEGRADED
    assert _step(snap, "routine-seed")["status"] == STATUS_SKIPPED
    assert _step(snap, "w3-health-monitor")["status"] == STATUS_SKIPPED
    assert "store unavailable" in _step(snap, "routine-seed")["detail"]
    assert snap["counts"][STATUS_OK] == 0


def test_lifespan_vault_index_failure_relays_no_note_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``index_vault_playbook`` aggregates PyYAML text that echoes source lines."""
    secret = "sk-live-AAAABBBBCCCCDDDDEEEEFFFF11112222"

    def boom() -> int:
        raise RuntimeError(
            "indexed 0 playbook note(s); 1 failed: "
            f"skill-poisoned.md: ScannerError: while scanning: api_key: \"{secret}\""
        )

    monkeypatch.setattr("omniagentos.skills.sync_playbook_from_repo", lambda: 0)
    monkeypatch.setattr("omniagentos.skills.index_vault_playbook", boom)

    snap = _run_lifespan(monkeypatch, OMNIAGENTOS_INDEX_VAULT_ON_STARTUP="1")

    step = _step(snap, "vault-index")
    assert step["status"] == STATUS_DEGRADED
    assert step["error_type"] == "RuntimeError"
    assert secret not in step["detail"]
    assert "ScannerError" not in step["detail"]
    assert "skill-poisoned.md" in step["detail"]


# ------------------------------------------------------------- endpoint -----


def _get(path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(path, headers=headers or {})

    return asyncio.run(run())


#: The trusted-proxy assertion the dashboard attaches for a human principal.
_PRINCIPAL_HEADER = "X-Omni-Authenticated-Principal"


def test_boot_receipt_endpoint_requires_the_session_token() -> None:
    """The route carries its OWN ``Depends(_authorized)`` on top of the namespace
    gate, so it 401s even when the suite-wide ``require_session_token`` bypass is
    in force."""
    assert _get("/api/ops/boot-receipt").status_code == 401


@pytest.mark.real_auth
def test_boot_receipt_denies_the_raw_machine_principal(auth_headers: dict[str, str]) -> None:
    """A caller holding only the machine-wide bearer resolves to ``system`` and is
    403'd on every route that reads another principal's private machine state.

    The receipt is that content class — it reports this machine's own boot state,
    including swallowed exception text. Before ``("api", "ops")`` joined
    ``_GATED_READ_NAMESPACES`` the same caller read it 200 while being 403'd on
    ``/api/workfs/tree`` and ``/api/accounts`` (Class-A review A1).
    """
    response = _get("/api/ops/boot-receipt", auth_headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "system_principal_forbidden"


@pytest.mark.real_auth
def test_boot_receipt_admits_a_human_principal(auth_headers: dict[str, str]) -> None:
    """The same request WITH the proxy's principal assertion is admitted — the
    gate denies the machine bearer, not the operator."""
    boot_receipt().start()
    response = _get("/api/ops/boot-receipt", {**auth_headers, _PRINCIPAL_HEADER: "owner"})
    assert response.status_code == 200


def test_boot_receipt_endpoint_returns_the_registry(auth_headers: dict[str, str]) -> None:
    receipt = boot_receipt()
    receipt.start()
    receipt.record_degraded("vault-index", RuntimeError("index dir missing"))
    receipt.record_ok("swarm-resume", "reconciled=0 resumed=0")
    receipt.complete()

    response = _get("/api/ops/boot-receipt", auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == STATUS_DEGRADED
    assert body["measured"] is True
    assert body["degraded"] == ["vault-index"]
    assert body["degraded_count"] == 1
    assert {s["subsystem"] for s in body["steps"]} == {"vault-index", "swarm-resume"}
    assert body["completed_at"] is not None
