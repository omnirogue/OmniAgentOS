"""U-S2 half B: the rotation state machine.

Every credential in this file is a FIXTURE: a name generated in-test, a tmpdir
database, and dummy version identifiers. No real store, vault, or provider is
touched, and nothing secret-shaped is stored in the repository.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from omniagentos.connectors.secret_catalog import SecretCatalog, invalidate_cache
from omniagentos.db.store import SqliteStore
from omniagentos.security import secret_rotation
from omniagentos.security.secret_rotation import (
    PROVIDER_REVOKE_ARMED_VALUE,
    PROVIDER_REVOKE_ENV,
    ROTATION_STEPS,
    OperatorCeremonyAdapter,
    RotationEngine,
    RotationRefused,
    _provider_revoke_armed,
)

FIXTURE_NAME = "FIXTURE_ROTATION_TOKEN"
FIXTURE_CREDENTIAL_ID = f"cred:fixture:{FIXTURE_NAME}"


class _RecordingAdapter:
    """A fixture adapter. It holds no value and records what it was asked to do."""

    def __init__(self, *, probe_ok: bool = True, canary_ok: bool = True) -> None:
        self.probe_ok = probe_ok
        self.canary_ok = canary_ok
        self.staged: list[str] = []
        self.probed: list[str] = []
        self.canaried: list[tuple[str, str]] = []
        self.revoke_called = False
        self.revoked: list[str] = []

    def stage(self, credential_id: str, version_id: str) -> None:
        self.staged.append(version_id)

    def probe(self, credential_id: str, version_id: str) -> bool:
        self.probed.append(version_id)
        return self.probe_ok

    def canary(self, credential_id: str, old_version_id: str, new_version_id: str) -> bool:
        self.canaried.append((old_version_id, new_version_id))
        return self.canary_ok

    def revoke_at_provider(self, credential_id: str, version_id: str) -> None:
        # Set the flag BEFORE raising, so this records the call even if the
        # engine were to swallow the exception.
        self.revoke_called = True
        self.revoked.append(version_id)


class _TripwireAdapter(_RecordingAdapter):
    """Reaching the provider-revoke step at all is a test failure."""

    def revoke_at_provider(self, credential_id: str, version_id: str) -> None:
        super().revoke_at_provider(credential_id, version_id)
        raise AssertionError("provider revoke must be unreachable while its flag is off")


@pytest.fixture
def store(tmp_path: Path) -> Any:
    raw = SqliteStore(str(tmp_path / "rotation.sqlite3"))
    try:
        yield raw
    finally:
        raw.close()


@pytest.fixture
def catalog(store: SqliteStore) -> SecretCatalog:
    catalog = SecretCatalog(store)
    catalog.upsert(
        credential_id=FIXTURE_CREDENTIAL_ID,
        env_name=FIXTURE_NAME,
        state="active",
        provider_family="fixture",
        owner="human:owner",
    )
    return catalog


@pytest.fixture
def engine(store: SqliteStore) -> RotationEngine:
    return RotationEngine(store)


@pytest.fixture(autouse=True)
def _clean_cache_and_flag(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv(PROVIDER_REVOKE_ENV, raising=False)
    invalidate_cache()
    yield
    invalidate_cache()


def _seed_previous_version(engine: RotationEngine, catalog: SecretCatalog) -> str:
    """Establish an initial live version through the engine's own machinery."""
    version_id = "skv_fixture_previous"
    engine._create_version(FIXTURE_CREDENTIAL_ID, version_id)
    engine._point_active_at(FIXTURE_CREDENTIAL_ID, version_id, catalog_state="active")
    assert catalog.get(FIXTURE_NAME)["active_version_id"] == version_id
    return version_id


# --- the decisive happy path -----------------------------------------------


def test_a_rotation_runs_the_full_ladder_and_closes_a_receipt(
    engine: RotationEngine,
    catalog: SecretCatalog,
) -> None:
    """DECISIVE: stage -> probe -> canary -> switch, then a closed receipt."""
    previous = _seed_previous_version(engine, catalog)
    adapter = _TripwireAdapter()

    receipt = engine.rotate(FIXTURE_NAME, adapter, operator="human:owner")

    assert receipt.outcome == "succeeded"
    assert adapter.staged == [receipt.to_version_id]
    assert adapter.probed == [receipt.to_version_id]
    assert adapter.canaried == [(previous, receipt.to_version_id)]

    # The ladder ran in order, and every step it claims is a real recorded step.
    executed = [step["step"] for step in receipt.steps]
    assert executed == [
        "create_new",
        "write_only_stage",
        "capability_probe",
        "dual_version_canary",
        "atomic_active_pointer_switch",
        "cache_grant_invalidation",
        "provider_revoke",
        "encrypted_backup_expiry",
    ]
    assert set(executed) <= set(ROTATION_STEPS)

    # The pointer switched atomically and the old version retired behind it.
    row = catalog.get(FIXTURE_NAME)
    assert row is not None
    assert row["active_version_id"] == receipt.to_version_id
    assert row["state"] == "active"
    assert row["rotated_at"] != ""
    assert catalog.version(receipt.to_version_id)["state"] == "active"
    assert catalog.version(previous)["state"] == "retired"

    # Encrypted-backup expiry is bound to the VERSION ID, not a filename glob.
    assert catalog.version(previous)["backup_expires_at"] != ""

    # The receipt is closed, durable, and digested over the recorded events.
    assert receipt.receipt_digest != ""
    assert receipt.closed_at != ""
    stored = engine.rotations()
    assert len(stored) == 1
    assert stored[0]["outcome"] == "succeeded"
    assert stored[0]["step"] == "receipt_closure"
    assert stored[0]["receipt_digest"] == receipt.receipt_digest
    assert stored[0]["closed_at"] != ""


def test_the_first_rotation_bootstraps_a_version_without_a_predecessor(
    engine: RotationEngine,
    catalog: SecretCatalog,
) -> None:
    receipt = engine.rotate(FIXTURE_NAME, _TripwireAdapter(), operator="human:owner")
    assert receipt.outcome == "succeeded"
    assert receipt.from_version_id == ""
    assert receipt.provider_revoke_state == "not_attempted"
    assert catalog.get(FIXTURE_NAME)["active_version_id"] == receipt.to_version_id


# --- rollback ---------------------------------------------------------------


def test_a_failed_canary_rolls_back_to_the_previous_version(
    engine: RotationEngine,
    catalog: SecretCatalog,
) -> None:
    """DECISIVE: the pointer goes back and nothing was revoked."""
    previous = _seed_previous_version(engine, catalog)
    adapter = _TripwireAdapter(canary_ok=False)

    receipt = engine.rotate(FIXTURE_NAME, adapter, operator="human:owner")

    assert receipt.outcome == "rolled_back"
    assert receipt.rolled_back is True
    row = catalog.get(FIXTURE_NAME)
    assert row is not None
    assert row["active_version_id"] == previous
    assert row["state"] == "active"

    # The candidate is retired, NOT revoked: this program took no provider-side
    # action against it, and a false revocation record is a lie in the direction
    # that later licenses someone to stop worrying about the key.
    assert catalog.version(receipt.to_version_id)["state"] == "retired"
    assert catalog.version(receipt.to_version_id)["provider_revoked"] == 0
    assert adapter.revoke_called is False

    # The rollback still closes a receipt: a ceremony without one is
    # indistinguishable from a ceremony that never ran.
    assert receipt.receipt_digest != ""
    assert engine.rotations()[0]["outcome"] == "rolled_back"


def test_a_failed_canary_never_resurrects_a_revoked_previous_version(
    engine: RotationEngine,
    catalog: SecretCatalog,
) -> None:
    """DECISIVE COUNTERFEIT: fail closed rather than reinstate a dead key."""
    previous = _seed_previous_version(engine, catalog)
    engine._mark_revoked(previous)

    receipt = engine.rotate(FIXTURE_NAME, _TripwireAdapter(canary_ok=False), operator="human:owner")

    assert receipt.outcome == "rolled_back_no_active"
    row = catalog.get(FIXTURE_NAME)
    assert row is not None
    assert row["active_version_id"] == ""
    # Parked, not reactivated: the broker refuses this with
    # `credential_quarantined` while the metadata explaining why stays readable.
    assert row["state"] == "quarantined"
    assert row["owner"] == "human:owner"
    assert catalog.version(previous)["state"] == "revoked"

    refused = [step for step in receipt.steps if step["status"] == "refused"]
    assert any("revoked" in step["detail"] for step in refused)


def test_the_active_pointer_refuses_a_revoked_target_at_the_choke_point(
    engine: RotationEngine,
    catalog: SecretCatalog,
) -> None:
    """The rule is enforced in one place, so no future step can route around it."""
    previous = _seed_previous_version(engine, catalog)
    engine._mark_revoked(previous)

    with pytest.raises(RotationRefused) as refused:
        engine._point_active_at(FIXTURE_CREDENTIAL_ID, previous, catalog_state="active")
    assert refused.value.reason == "revoked_version_cannot_be_activated"

    sources = inspect.getsource(secret_rotation)
    tree = ast.parse(sources)
    pointer_writes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "UPDATE secret_catalog SET active_version_id = ?" in node.value
    ]
    # Exactly one statement in this module sets a non-empty active pointer, and
    # it lives in the method that refuses revoked targets.
    assert len(pointer_writes) == 1


def test_a_failed_probe_aborts_before_the_pointer_ever_moves(
    engine: RotationEngine,
    catalog: SecretCatalog,
) -> None:
    previous = _seed_previous_version(engine, catalog)
    adapter = _TripwireAdapter(probe_ok=False)

    receipt = engine.rotate(FIXTURE_NAME, adapter, operator="human:owner")

    assert receipt.outcome == "failed"
    assert adapter.canaried == []
    assert catalog.get(FIXTURE_NAME)["active_version_id"] == previous
    assert catalog.get(FIXTURE_NAME)["state"] == "active"
    assert "atomic_active_pointer_switch" not in [step["step"] for step in receipt.steps]


# --- the [OPERATOR] gate ---------------------------------------------------------


def test_provider_revoke_is_off_by_default_and_by_each_switch_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _provider_revoke_armed(False) is False
    assert _provider_revoke_armed(True) is False

    monkeypatch.setenv(PROVIDER_REVOKE_ENV, PROVIDER_REVOKE_ARMED_VALUE)
    assert _provider_revoke_armed(False) is False
    assert _provider_revoke_armed(True) is True

    # Habitual truthy spellings do NOT arm an irreversible action.
    for spelling in ("1", "true", "yes", "on", "enabled", "armed", "Armed", "ARMED=1"):
        monkeypatch.setenv(PROVIDER_REVOKE_ENV, spelling)
        assert _provider_revoke_armed(True) is False, f"{spelling!r} must not arm the step"

    # Surrounding whitespace is stripped -- that is env hygiene, not a spelling.
    monkeypatch.setenv(PROVIDER_REVOKE_ENV, "  ARMED  ")
    assert _provider_revoke_armed(True) is True


def test_the_provider_revoke_call_is_unreachable_while_its_flag_is_off(
    engine: RotationEngine,
    catalog: SecretCatalog,
) -> None:
    """COUNTERFEIT: the built step must be provably not executed."""
    _seed_previous_version(engine, catalog)
    adapter = _TripwireAdapter()

    receipt = engine.rotate(
        FIXTURE_NAME,
        adapter,
        operator="human:owner",
        # Even an explicit caller approval cannot arm it on its own.
        provider_revoke_operator_approval=True,
    )

    assert receipt.outcome == "succeeded"
    assert receipt.provider_revoke_state == "skipped_flag_off"
    assert adapter.revoke_called is False
    assert adapter.revoked == []
    assert catalog.version(receipt.from_version_id)["provider_revoked"] == 0
    assert catalog.version(receipt.from_version_id)["state"] == "retired"

    skipped = [step for step in receipt.steps if step["step"] == "provider_revoke"]
    assert len(skipped) == 1
    assert skipped[0]["status"] == "skipped"
    assert PROVIDER_REVOKE_ENV in skipped[0]["detail"]


def _mentions_gate(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id == "_provider_revoke_armed"
        for child in ast.walk(node)
    )


def test_there_is_exactly_one_provider_revoke_call_and_the_gate_encloses_it() -> None:
    """COUNTERFEIT: the gate is measured against the module's AST, not promised.

    A behavioural test alone proves the call did not happen on ONE path. This
    proves there is only one path it could happen on, and that the gate stands
    in front of it -- which is the claim the report makes.
    """
    tree = ast.parse(Path(secret_rotation.__file__).read_text(encoding="utf-8"))

    revoke_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "revoke_at_provider"
    ]
    assert len(revoke_calls) == 1, "more than one provider-revoke call site would defeat the gate"
    call = revoke_calls[0]

    # The call's enclosing function, found by containment rather than by name.
    enclosing = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(child is call for child in ast.walk(node))
    ]
    assert enclosing, "the provider-revoke call is not inside any function"
    function = min(enclosing, key=lambda node: len(list(ast.walk(node))))

    gates = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If) and _mentions_gate(node.test)
    ]
    assert len(gates) == 1, "exactly one _provider_revoke_armed gate must guard the call"
    gate = gates[0]

    inside_gate_body = any(
        child is call for statement in gate.body for child in ast.walk(statement)
    )
    if inside_gate_body:
        return  # positive form: `if armed: revoke(...)`

    # Negative form: `if not armed: <record>; return`. It must precede the call
    # and every branch of its body must leave the function.
    assert isinstance(gate.test, ast.UnaryOp) and isinstance(gate.test.op, ast.Not), (
        "a gate that neither encloses the call nor negates the check guards nothing"
    )
    assert gate.end_lineno is not None and gate.end_lineno < call.lineno
    assert isinstance(gate.body[-1], (ast.Return, ast.Raise)), (
        "the negative gate must return or raise before reaching the provider call"
    )
    assert not gate.orelse, "an else-branch on the negative gate is a second, unchecked path"


def test_the_step_is_built_and_runs_when_a_human_arms_both_switches(
    engine: RotationEngine,
    catalog: SecretCatalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate hides a real step, not an empty one."""
    previous = _seed_previous_version(engine, catalog)
    monkeypatch.setenv(PROVIDER_REVOKE_ENV, PROVIDER_REVOKE_ARMED_VALUE)
    adapter = _RecordingAdapter()

    receipt = engine.rotate(
        FIXTURE_NAME,
        adapter,
        operator="human:owner",
        provider_revoke_operator_approval=True,
    )

    assert receipt.provider_revoke_state == "completed"
    assert adapter.revoked == [previous]
    assert catalog.version(previous)["state"] == "revoked"
    assert catalog.version(previous)["provider_revoked"] == 1


# --- preconditions and hygiene ---------------------------------------------


@pytest.mark.parametrize("state", ["missing", "quarantined", "revoked", "retired", "rotating"])
def test_only_an_active_credential_can_start_a_rotation(
    engine: RotationEngine,
    catalog: SecretCatalog,
    state: str,
) -> None:
    catalog.set_state(FIXTURE_NAME, state, actor="human:owner", owner_ack=True)
    with pytest.raises(RotationRefused) as refused:
        engine.rotate(FIXTURE_NAME, _TripwireAdapter(), operator="human:owner")
    assert refused.value.reason == "credential_not_rotatable"


def test_rotating_an_unknown_name_is_refused(engine: RotationEngine) -> None:
    with pytest.raises(RotationRefused) as refused:
        engine.rotate("FIXTURE_NO_SUCH_NAME", _TripwireAdapter(), operator="human:owner")
    assert refused.value.reason == "unknown_credential"


def test_no_engine_or_adapter_signature_can_carry_a_credential_value() -> None:
    """The name-only guarantee is structural: there is nowhere to put a value."""
    forbidden = {"value", "secret", "token", "password", "material", "plaintext"}
    surfaces = [
        RotationEngine.rotate,
        secret_rotation.RotationAdapter.stage,
        secret_rotation.RotationAdapter.probe,
        secret_rotation.RotationAdapter.canary,
        secret_rotation.RotationAdapter.revoke_at_provider,
        OperatorCeremonyAdapter.stage,
        OperatorCeremonyAdapter.probe,
        OperatorCeremonyAdapter.canary,
    ]
    for surface in surfaces:
        parameters = set(inspect.signature(surface).parameters)
        assert not (parameters & forbidden), f"{surface.__qualname__} exposes a value parameter"

    # Stronger than parameter naming: no adapter method can hand a string BACK
    # into this process, so credential material has no return path either.
    for adapter_type in (secret_rotation.RotationAdapter, OperatorCeremonyAdapter):
        assert inspect.signature(adapter_type.stage).return_annotation == "None"
        assert inspect.signature(adapter_type.probe).return_annotation == "bool"
        assert inspect.signature(adapter_type.canary).return_annotation == "bool"
        assert inspect.signature(adapter_type.revoke_at_provider).return_annotation == "None"


def test_the_operator_ceremony_adapter_never_prompts_for_a_value() -> None:
    prompts: list[str] = []
    messages: list[str] = []
    adapter = OperatorCeremonyAdapter(
        prompt=lambda question: (prompts.append(question), "y")[1],
        out=messages.append,
    )
    adapter.stage(FIXTURE_CREDENTIAL_ID, "skv_fixture")
    assert adapter.probe(FIXTURE_CREDENTIAL_ID, "skv_fixture") is True
    assert adapter.canary(FIXTURE_CREDENTIAL_ID, "skv_old", "skv_fixture") is True

    # Every prompt is a yes/no question; none of them asks for key material.
    assert all(question.strip().endswith("[y/N]") for question in prompts)
    assert any("Do NOT paste the value here" in message for message in messages)


def test_an_unconfirmed_stage_aborts_rather_than_proceeding(
    engine: RotationEngine,
    catalog: SecretCatalog,
) -> None:
    previous = _seed_previous_version(engine, catalog)
    adapter = OperatorCeremonyAdapter(prompt=lambda _question: "n", out=lambda _message: None)

    receipt = engine.rotate(FIXTURE_NAME, adapter, operator="human:owner")

    assert receipt.outcome == "failed"
    assert catalog.get(FIXTURE_NAME)["active_version_id"] == previous
    staged = [step for step in receipt.steps if step["step"] == "write_only_stage"]
    # The adapter's refusal is recorded by TYPE only: an adapter exception can
    # quote the request it just made, and that request carried the key.
    assert staged[0]["status"] == "refused"
    assert staged[0]["detail"] == "RotationRefused"
