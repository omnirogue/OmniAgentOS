"""U-S2 half A: the name-only secret catalog and its resolution-time refusal.

Every credential-shaped string in this file is GENERATED HERE, at run time, from
literal fragments. Nothing secret-shaped is stored in the repository, which is
the SECRET_RE gate rule for every vault-touching item in this program.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omniagentos.connectors import (
    Capability,
    Connector,
    ConnectorRegistry,
    Group,
    HttpSpec,
    broker,
)
from omniagentos.connectors import secret_catalog as catalog_module
from omniagentos.connectors.broker import BrokerDenied
from omniagentos.connectors.secret_catalog import (
    CatalogRefused,
    SecretCatalog,
    invalidate_cache,
    resolution_denial,
)
from omniagentos.connectors.store import CapabilityStore
from omniagentos.contracts import ActionClass
from omniagentos.db.store import SqliteStore

DECLARED_PRESENT = "FIXTURE_ACCESS_TOKEN"
DECLARED_ABSENT = "FIXTURE_UNPROVISIONED_TOKEN"
ORPHAN_NAME = "FIXTURE_ORPHAN_LEGACY_TOKEN"


def _generated_secret_value() -> str:
    """Build a credential-shaped string at run time; never stored in the repo."""
    return "sk_live_" + ("A1b2C3d4" * 5)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    raw = SqliteStore(str(tmp_path / "catalog.sqlite3"))
    try:
        yield raw
    finally:
        raw.close()


@pytest.fixture
def catalog(store: SqliteStore) -> SecretCatalog:
    return SecretCatalog(store)


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    invalidate_cache()
    yield
    invalidate_cache()


def _fixture_registry() -> ConnectorRegistry:
    read_cap = Capability(
        id="fixture.read",
        connector="fixture",
        group="support",
        label="fixture read",
        action_class=ActionClass.READ_ONLY,
        http=HttpSpec(base_url="https://fixture.test", auth=f"bearer:{DECLARED_PRESENT}"),
    )
    return ConnectorRegistry(
        version=1,
        groups={"support": Group(label="Support")},
        connectors={
            "fixture": Connector(
                id="fixture",
                label="Fixture",
                group="support",
                env=[DECLARED_PRESENT, DECLARED_ABSENT],
                capabilities={"read": read_cap},
            )
        },
    )


def _broker_capability() -> Capability:
    return Capability(
        id="fixture.read",
        connector="fixture",
        group="support",
        label="fixture read",
        action_class=ActionClass.READ_ONLY,
        http=HttpSpec(
            base_url="https://fixture.test",
            methods=["GET"],
            auth=f"bearer:{DECLARED_PRESENT}",
        ),
    )


def _broker_registry(capability: Capability) -> SimpleNamespace:
    return SimpleNamespace(
        capability=lambda cap_id: capability,
        connectors={"fixture": SimpleNamespace(env=[DECLARED_PRESENT])},
        groups={"support": SimpleNamespace(danger=False)},
    )


def _point_resolution_at(store: SqliteStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_DB", store._db_path)
    invalidate_cache()


# --- population ------------------------------------------------------------


def test_sync_marks_declared_absent_missing_and_orphans_quarantined(
    catalog: SecretCatalog,
) -> None:
    """D-33: the declared-but-absent name is MARKED, never invented; orphans park."""
    summary = catalog.sync_from_registry(
        _fixture_registry(),
        {DECLARED_PRESENT: "present"},
        [ORPHAN_NAME],
    )

    assert sorted(summary.inserted) == sorted([DECLARED_PRESENT, DECLARED_ABSENT, ORPHAN_NAME])
    states = catalog.state_map()
    assert states == {
        DECLARED_PRESENT: "active",
        DECLARED_ABSENT: "missing",
        ORPHAN_NAME: "quarantined",
    }

    # "Marked, never invented": a missing credential gets a row and NO version,
    # so nothing downstream can mistake the mark for a provisioned key.
    missing_row = catalog.get(DECLARED_ABSENT)
    assert missing_row is not None
    assert missing_row["active_version_id"] == ""
    assert catalog.versions_for(str(missing_row["credential_id"])) == []

    # The catalog carries the S3-W3 field set, all of it name-only.
    active_row = catalog.get(DECLARED_PRESENT)
    assert active_row is not None
    assert active_row["credential_id"] == f"cred:fixture:{DECLARED_PRESENT}"
    assert active_row["provider_family"] == "fixture"
    assert active_row["risk_domain"] == "support"
    assert active_row["capability_refs"] == "fixture.read"
    assert active_row["effect_class"] == "read"
    assert active_row["last_used_at"] == ""


def test_sync_refuses_a_name_that_is_both_declared_and_archived(
    catalog: SecretCatalog,
) -> None:
    """The inventory invariant is upheld loudly, not patched over quietly."""
    with pytest.raises(CatalogRefused) as refused:
        catalog.sync_from_registry(
            _fixture_registry(),
            {DECLARED_PRESENT: "present"},
            [DECLARED_PRESENT],
        )
    assert refused.value.reason == "bucket_conflict"


def test_sync_never_unrevokes_and_never_disturbs_a_rotation(
    catalog: SecretCatalog,
) -> None:
    registry = _fixture_registry()
    environ = {DECLARED_PRESENT: "present", DECLARED_ABSENT: "present"}
    catalog.sync_from_registry(registry, environ, [])

    catalog.set_state(DECLARED_PRESENT, "revoked", actor="human:owner")
    catalog.set_state(DECLARED_ABSENT, "rotating", actor="human:owner")

    summary = catalog.sync_from_registry(registry, environ, [])

    assert sorted(summary.preserved) == sorted([DECLARED_PRESENT, DECLARED_ABSENT])
    assert catalog.state_map() == {
        DECLARED_PRESENT: "revoked",
        DECLARED_ABSENT: "rotating",
    }


def test_quarantine_lifts_only_when_the_name_becomes_registry_declared(
    catalog: SecretCatalog,
) -> None:
    """Repo absence parks a name; repo PRESENCE is the evidence that lifts it."""
    catalog.sync_from_registry(_fixture_registry(), {DECLARED_PRESENT: "x"}, [ORPHAN_NAME])
    assert catalog.state_map()[ORPHAN_NAME] == "quarantined"

    # Still undeclared, and now present in the environment: presence alone is
    # not evidence, so it stays parked.
    catalog.sync_from_registry(
        _fixture_registry(),
        {DECLARED_PRESENT: "x", ORPHAN_NAME: "x"},
        [ORPHAN_NAME],
    )
    assert catalog.state_map()[ORPHAN_NAME] == "quarantined"

    declared = _fixture_registry()
    connector = declared.connectors["fixture"]
    widened = ConnectorRegistry(
        version=1,
        groups=declared.groups,
        connectors={
            "fixture": Connector(
                id="fixture",
                label=connector.label,
                group=connector.group,
                env=[*connector.env, ORPHAN_NAME],
                capabilities=connector.capabilities,
            )
        },
    )
    catalog.sync_from_registry(widened, {DECLARED_PRESENT: "x", ORPHAN_NAME: "x"}, [])
    assert catalog.state_map()[ORPHAN_NAME] == "active"


# --- last_used comes only from the broker audit spine ----------------------


def test_last_used_is_populated_only_from_allowed_broker_calls(
    store: SqliteStore,
    catalog: SecretCatalog,
) -> None:
    catalog.sync_from_registry(_fixture_registry(), {DECLARED_PRESENT: "x"}, [ORPHAN_NAME])
    audit = CapabilityStore(store)

    # An intent row and a denial are attempts, not use. Only 'allowed' counts.
    for decision in ("intent", "denied", "allowed"):
        audit.log_call(
            "run_fixture",
            "lane:runner.step",
            "fixture.read",
            method="RESOLVE",
            allowed=decision == "allowed",
            decision=decision,
            connector="fixture",
            env_name=f"{DECLARED_PRESENT},{DECLARED_ABSENT}",
        )
    audit.log_call(
        "run_fixture",
        "lane:runner.step",
        "fixture.read",
        method="RESOLVE",
        allowed=False,
        decision="denied",
        connector="orphan",
        env_name=ORPHAN_NAME,
    )

    assert catalog.refresh_last_used() == 2

    rows = {str(row["env_name"]): row for row in catalog.rows()}
    assert rows[DECLARED_PRESENT]["last_used_at"] != ""
    assert rows[DECLARED_ABSENT]["last_used_at"] != ""
    # No allowed row ever named the orphan, so it keeps the empty marker. Empty
    # means "no broker evidence", NOT "never used" -- nothing may retire it on
    # this emptiness alone.
    assert rows[ORPHAN_NAME]["last_used_at"] == ""


def test_last_used_has_no_writer_outside_the_audit_spine(catalog: SecretCatalog) -> None:
    """There is deliberately no parameter through which a guess could be written."""
    import inspect

    assert "last_used" not in inspect.signature(SecretCatalog.upsert).parameters
    assert "last_used" not in inspect.signature(SecretCatalog.set_operator_fields).parameters


# --- the decisive resolution behaviour -------------------------------------


def test_quarantined_name_is_refused_while_its_metadata_stays_readable(
    store: SqliteStore,
    catalog: SecretCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DECISIVE: refusal on disposition, with the evidence about it preserved."""
    capability = _broker_capability()
    monkeypatch.setattr(broker, "load_registry", lambda: _broker_registry(capability))
    monkeypatch.setenv(DECLARED_PRESENT, "fixture-value")
    catalog.upsert(
        credential_id=f"cred:fixture:{DECLARED_PRESENT}",
        env_name=DECLARED_PRESENT,
        state="quarantined",
        provider_family="fixture",
        owner="human:owner",
    )
    _point_resolution_at(store, monkeypatch)

    audit = CapabilityStore(store)
    with pytest.raises(BrokerDenied) as denied:
        broker.resolve_for(capability, audit_store=audit)

    assert denied.value.reason == "credential_quarantined"
    assert denied.value.payload() == {
        "capability_id": DECLARED_PRESENT,
        "reason_code": "credential_quarantined",
        "next_action": (
            "a named owner must disposition this quarantined credential before it is used again"
        ),
    }

    # A policy refusal, not an availability report: the audit spine records it
    # as 'denied' rather than 'unavailable'.
    terminal = [row for row in audit.call_log() if row["decision"] != "intent"]
    assert [row["decision"] for row in terminal] == ["denied"]
    assert terminal[0]["reason_code"] == "credential_quarantined"

    # ... and the metadata that would let an operator decide what to do about it
    # is still fully readable while the credential itself is refused.
    row = catalog.get(DECLARED_PRESENT)
    assert row is not None
    assert row["state"] == "quarantined"
    assert row["owner"] == "human:owner"
    assert row["provider_family"] == "fixture"


def test_revoked_and_quarantined_are_distinguishable_from_every_earlier_code(
    store: SqliteStore,
    catalog: SecretCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two new codes carry remedies no existing code carries."""
    capability = _broker_capability()
    monkeypatch.setattr(broker, "load_registry", lambda: _broker_registry(capability))
    monkeypatch.setenv(DECLARED_PRESENT, "fixture-value")
    catalog.upsert(
        credential_id=f"cred:fixture:{DECLARED_PRESENT}",
        env_name=DECLARED_PRESENT,
        state="revoked",
    )
    _point_resolution_at(store, monkeypatch)

    with pytest.raises(BrokerDenied) as revoked:
        broker._resolve_secret(DECLARED_PRESENT, capability=capability)
    assert revoked.value.reason == "credential_revoked"

    catalog.set_state(DECLARED_PRESENT, "quarantined", actor="human:owner")
    invalidate_cache()
    with pytest.raises(BrokerDenied) as quarantined:
        broker._resolve_secret(DECLARED_PRESENT, capability=capability)
    assert quarantined.value.reason == "credential_quarantined"

    actions = broker._DENIAL_NEXT_ACTIONS
    assert actions["credential_revoked"] != actions["credential_quarantined"]
    # No two denial codes in the whole vocabulary share a next move: routing on
    # the code alone has to stay possible.
    assert len(set(actions.values())) == len(actions)


def test_a_name_with_no_catalog_row_resolves_exactly_as_before(
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = _broker_capability()
    monkeypatch.setattr(broker, "load_registry", lambda: _broker_registry(capability))
    monkeypatch.setenv(DECLARED_PRESENT, "fixture-value")
    _point_resolution_at(store, monkeypatch)

    assert broker._resolve_secret(DECLARED_PRESENT, capability=capability) == "fixture-value"


def test_a_catalog_fault_degrades_to_pre_catalog_behaviour_loudly(
    store: SqliteStore,
    catalog: SecretCatalog,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BALANCE RULE: a broken catalog must not refuse healthy names."""
    capability = _broker_capability()
    monkeypatch.setattr(broker, "load_registry", lambda: _broker_registry(capability))
    monkeypatch.setenv(DECLARED_PRESENT, "fixture-value")
    catalog.upsert(
        credential_id=f"cred:fixture:{DECLARED_PRESENT}",
        env_name=DECLARED_PRESENT,
        state="active",
    )
    _point_resolution_at(store, monkeypatch)

    def _broken(_db_path: str) -> dict[str, str]:
        raise RuntimeError("catalog table is corrupt")

    monkeypatch.setattr(catalog_module, "_load_state_map", _broken)
    invalidate_cache()

    with caplog.at_level(logging.ERROR, logger=catalog_module.__name__):
        assert broker._resolve_secret(DECLARED_PRESENT, capability=capability) == "fixture-value"

    assert any("secret_catalog_unavailable" in record.message for record in caplog.records)
    assert resolution_denial(DECLARED_PRESENT) == ""


def test_a_pre_migration_database_is_not_a_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An absent catalog table is the pre-catalog state, not an error."""
    import sqlite3

    plain = tmp_path / "no-catalog.sqlite3"
    sqlite3.connect(plain).close()
    monkeypatch.setenv("OMNIAGENTOS_DB", str(plain))
    invalidate_cache()

    with caplog.at_level(logging.ERROR, logger=catalog_module.__name__):
        assert resolution_denial(DECLARED_PRESENT) == ""
    assert not caplog.records


# --- counterfeits -----------------------------------------------------------


def test_an_owner_marked_shared_credential_is_never_auto_revoked_or_deleted(
    catalog: SecretCatalog,
) -> None:
    """COUNTERFEIT: repo-unreferenced is not proof of provider-side death."""
    catalog.sync_from_registry(_fixture_registry(), {DECLARED_PRESENT: "x"}, [ORPHAN_NAME])
    catalog.set_operator_fields(
        ORPHAN_NAME,
        owner="human:owner",
        shared_owner_marked=True,
        recovery_dependency="shared with the billing box outside this repo",
    )

    # Repeated unattended syncs cannot escalate a quarantine into a revocation
    # and cannot delete the row that records why it is parked.
    for _ in range(3):
        catalog.sync_from_registry(_fixture_registry(), {DECLARED_PRESENT: "x"}, [ORPHAN_NAME])

    row = catalog.get(ORPHAN_NAME)
    assert row is not None
    assert row["state"] == "quarantined"
    assert row["shared_owner_marked"] == 1
    assert row["recovery_dependency"] == "shared with the billing box outside this repo"

    # Even an explicit revocation is refused until a named owner acknowledges it.
    with pytest.raises(CatalogRefused) as refused:
        catalog.set_state(ORPHAN_NAME, "revoked", actor="job:secrets-sync")
    assert refused.value.reason == "shared_credential_requires_owner_ack"
    assert catalog.get(ORPHAN_NAME) is not None

    catalog.set_state(ORPHAN_NAME, "revoked", actor="human:owner", owner_ack=True)
    revoked = catalog.get(ORPHAN_NAME)
    assert revoked is not None and revoked["state"] == "revoked"


def test_a_value_shaped_string_is_refused_and_never_reaches_catalog_output(
    catalog: SecretCatalog,
) -> None:
    """COUNTERFEIT: a generated credential-shaped string cannot enter the catalog."""
    generated = _generated_secret_value()

    with pytest.raises(CatalogRefused) as refused:
        catalog.upsert(
            credential_id="cred:fixture:LEAK",
            env_name="FIXTURE_LEAK_TOKEN",
            state="active",
            disposition_note=f"rotated from {generated}",
        )
    assert refused.value.reason == "value_shaped_field"
    # The refusal itself must not echo what it refused.
    assert generated not in refused.value.detail
    assert generated not in str(refused.value)

    with pytest.raises(CatalogRefused):
        catalog.upsert(
            credential_id="cred:fixture:LEAK",
            env_name=generated,
            state="active",
        )

    catalog.sync_from_registry(_fixture_registry(), {DECLARED_PRESENT: generated}, [ORPHAN_NAME])
    report = catalog.report()
    rendered = repr(report) + repr(catalog.rows())
    assert generated not in rendered
    for fragment in ("sk_live_", "A1b2C3d4"):
        assert fragment not in rendered
    assert report["counts"]["active"] == 1
    assert report["counts"]["quarantined"] == 1


def test_the_quarantine_set_comes_from_the_inventory_report_itself(tmp_path: Path) -> None:
    """The ARCHIVE bucket arrives as inventory.py's own artifact, not retyped."""
    from omniagentos.connectors.inventory import _classify_names, write_report

    report = _classify_names(
        frozenset({ORPHAN_NAME, DECLARED_PRESENT}),
        frozenset({DECLARED_PRESENT}),
        frozenset({DECLARED_PRESENT}),
    )
    written = write_report(report, tmp_path)
    assert catalog_module._archive_names_from(str(written)) == [ORPHAN_NAME]

    # A newline list still works, and a report shaped like neither is refused
    # rather than silently read as an empty quarantine set.
    listed = tmp_path / "names.txt"
    listed.write_text(f"{ORPHAN_NAME}\n\n", encoding="utf-8")
    assert catalog_module._archive_names_from(str(listed)) == [ORPHAN_NAME]

    broken = tmp_path / "broken.json"
    broken.write_text('{"details": {}}', encoding="utf-8")
    with pytest.raises(CatalogRefused) as refused:
        catalog_module._archive_names_from(str(broken))
    assert refused.value.reason == "unusable_inventory_report"


def test_catalog_output_assertion_catches_a_forced_value(catalog: SecretCatalog) -> None:
    """The name-only assertion is a live check, not a comment on the schema."""
    generated = _generated_secret_value()
    with pytest.raises(CatalogRefused) as refused:
        catalog_module.assert_name_only({"names": {"active": [generated]}})
    assert refused.value.reason == "value_shaped_field"
    assert generated not in str(refused.value)
