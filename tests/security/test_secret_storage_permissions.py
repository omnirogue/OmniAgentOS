"""Counterfeit-resistant tests for secret-store permission containment."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from omniagentos.connectors import Capability, HttpSpec, broker
from omniagentos.connectors.broker import BrokerDenied
from omniagentos.contracts import ActionClass
from omniagentos.security import secret_storage
from omniagentos.security.secret_storage import (
    PermissionViolation,
    StoragePermissionGuard,
    register_permission_guard,
)


@pytest.fixture(autouse=True)
def _reset_permission_guard() -> None:
    """Keep the process-global optional guard isolated between tests."""
    register_permission_guard(None)
    yield
    register_permission_guard(None)


#: U-R4 made credential resolution capability-addressed: a caller names a
#: capability, never a bare environment variable, and the private resolver
#: refuses any name its connector does not declare. The guard therefore has to
#: be exercised THROUGH that shape -- these fixtures build the smallest
#: capability that legitimately owns ``TEST_ENV_NAME``.
_GUARD_CAP = Capability(
    id="guardfixture.read",
    connector="guardfixture",
    group="support",
    label="guardfixture.read",
    action_class=ActionClass.READ_ONLY,
    http=HttpSpec(base_url="https://guardfixture.test"),
)


def _pin_registry(monkeypatch: pytest.MonkeyPatch, *env_names: str) -> None:
    """Declare ``env_names`` as the fixture connector's complete credential scope."""
    monkeypatch.setattr(
        broker,
        "load_registry",
        lambda: SimpleNamespace(connectors={"guardfixture": SimpleNamespace(env=list(env_names))}),
    )


def _resolve(env_name: str = "TEST_ENV_NAME") -> str:
    return broker._resolve_secret(env_name, capability=_GUARD_CAP)


def test_overly_permissive_file_marks_credential_unavailable(
    tmpdir: pytest.TempPathFactory,
) -> None:
    """A 0644 fixture is refused without exposing its content or path."""
    store_dir = Path(str(tmpdir)) / "store"
    store_dir.mkdir(mode=0o700)
    store_file = store_dir / "connections.env"
    store_file.write_text("DUMMY_VALUE=secret123")
    os.chmod(store_file, 0o644)

    guard = StoragePermissionGuard()

    with pytest.raises(PermissionViolation) as exc_info:
        guard.check_store_access(str(store_file), "TEST_ENV_NAME")

    assert exc_info.value.reason == "file_too_permissive"
    assert "secret123" not in str(exc_info.value)
    assert str(store_file) not in str(exc_info.value)


def test_symlink_escape_refused_without_echo(tmpdir: pytest.TempPathFactory) -> None:
    """A link leaving the protected store is refused without content hints."""
    store_dir = Path(str(tmpdir)) / "store"
    store_dir.mkdir(mode=0o700)
    external_dir = Path(str(tmpdir)) / "external"
    external_dir.mkdir()
    external_file = external_dir / "leaked.env"
    external_file.write_text("LEAKED=value456")
    os.chmod(external_file, 0o600)
    symlink = store_dir / "evil_link.env"
    symlink.symlink_to(external_file)

    with pytest.raises(PermissionViolation) as exc_info:
        StoragePermissionGuard().check_store_access(str(symlink), "TEST_ENV_NAME")

    assert exc_info.value.reason == "symlink_escape"
    assert "value456" not in str(exc_info.value)
    assert "leaked" not in str(exc_info.value).lower()


@pytest.mark.parametrize("decision", [None, ()])
def test_unknown_or_nondescendant_inode_containment_fails_closed(
    decision: tuple[str, ...] | None,
    tmpdir: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown containment or the root itself is an escape refusal, never admission."""
    store_dir = Path(str(tmpdir)) / "store"
    store_dir.mkdir(mode=0o700)
    store_file = store_dir / "connections.env"
    store_file.write_text("VALID=secret")
    os.chmod(store_file, 0o600)
    monkeypatch.setattr(secret_storage, "inode_relative_parts_anchored", lambda *_args: decision)

    with pytest.raises(PermissionViolation) as exc_info:
        StoragePermissionGuard().check_store_access(str(store_file), "TEST_ENV_NAME")

    assert exc_info.value.reason == "symlink_escape"
    assert str(store_file) not in str(exc_info.value)


def test_missing_file_under_missing_store_directory_remains_file_missing(
    tmpdir: pytest.TempPathFactory,
) -> None:
    """Anchored containment preserves the typed absence result for a missing root."""
    store_file = Path(str(tmpdir)) / "missing-store" / "connections.env"

    with pytest.raises(PermissionViolation) as exc_info:
        StoragePermissionGuard().check_store_access(str(store_file), "TEST_ENV_NAME")

    assert exc_info.value.reason == "file_missing"
    assert str(store_file) not in str(exc_info.value)


def test_store_leaf_resolving_to_store_root_is_refused(
    tmpdir: pytest.TempPathFactory,
) -> None:
    """A would-be file aliasing its protected directory is not a strict descendant."""
    store_dir = Path(str(tmpdir)) / "store"
    store_dir.mkdir(mode=0o700)
    store_file = store_dir / "connections.env"
    store_file.symlink_to(".", target_is_directory=True)

    with pytest.raises(PermissionViolation) as exc_info:
        StoragePermissionGuard().check_store_access(str(store_file), "TEST_ENV_NAME")

    assert exc_info.value.reason == "symlink_escape"


def test_symlinked_store_directory_alias_is_accepted(
    tmpdir: pytest.TempPathFactory,
) -> None:
    """A configured directory alias remains valid when its file stays beneath it."""
    real_store = Path(str(tmpdir)) / "real-store"
    real_store.mkdir(mode=0o700)
    store_file = real_store / "connections.env"
    store_file.write_text("VALID=secret")
    os.chmod(store_file, 0o600)
    alias = Path(str(tmpdir)) / "store-alias"
    alias.symlink_to(real_store, target_is_directory=True)

    StoragePermissionGuard().check_store_access(str(alias / "connections.env"), "TEST_ENV_NAME")


def test_lexical_commonpath_cannot_counterfeit_inode_containment(
    tmpdir: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forged lexical-commonpath verdict cannot admit an outward symlink."""
    store_dir = Path(str(tmpdir)) / "store"
    store_dir.mkdir(mode=0o700)
    external_dir = Path(str(tmpdir)) / "external"
    external_dir.mkdir()
    external_file = external_dir / "leaked.env"
    external_file.write_text("LEAKED=value")
    os.chmod(external_file, 0o600)
    symlink = store_dir / "evil_link.env"
    symlink.symlink_to(external_file)
    monkeypatch.setattr(os.path, "commonpath", lambda _paths: str(store_dir))

    with pytest.raises(PermissionViolation) as exc_info:
        StoragePermissionGuard().check_store_access(str(symlink), "TEST_ENV_NAME")

    assert exc_info.value.reason == "symlink_escape"


def test_invalid_store_path_refuses_without_echo() -> None:
    """Path-normalization errors fail closed without exposing the supplied spelling."""
    invalid_path = "private\x00store.env"

    with pytest.raises(PermissionViolation) as exc_info:
        StoragePermissionGuard().check_store_access(invalid_path, "TEST_ENV_NAME")

    assert exc_info.value.reason == "symlink_escape"
    assert "private" not in str(exc_info.value)


def test_overly_permissive_directory_refused(tmpdir: pytest.TempPathFactory) -> None:
    """A store directory whose group or others can access it is refused."""
    store_dir = Path(str(tmpdir)) / "store"
    store_dir.mkdir(mode=0o777)
    os.chmod(store_dir, 0o777)
    store_file = store_dir / "connections.env"
    store_file.write_text("TEST=value789")
    os.chmod(store_file, 0o600)

    with pytest.raises(PermissionViolation) as exc_info:
        StoragePermissionGuard().check_store_access(str(store_file), "TEST_ENV_NAME")

    assert exc_info.value.reason == "dir_too_permissive"
    assert "value789" not in str(exc_info.value)


def test_valid_permissions_accepted(tmpdir: pytest.TempPathFactory) -> None:
    """A 0700 directory containing a 0600 file passes silently."""
    store_dir = Path(str(tmpdir)) / "store"
    store_dir.mkdir(mode=0o700)
    store_file = store_dir / "connections.env"
    store_file.write_text("VALID=secret")
    os.chmod(store_file, 0o600)

    StoragePermissionGuard().check_store_access(str(store_file), "TEST_ENV_NAME")


def test_valid_relative_store_path_is_accepted(
    tmpdir: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Relative paths use their containing store directory as the protected root."""
    store_dir = Path(str(tmpdir)) / "store"
    store_dir.mkdir(mode=0o700)
    store_file = store_dir / "connections.env"
    store_file.write_text("VALID=secret")
    os.chmod(store_file, 0o600)
    monkeypatch.chdir(store_dir)

    StoragePermissionGuard().check_store_access("connections.env", "TEST_ENV_NAME")


def test_missing_file_raises_file_missing(tmpdir: pytest.TempPathFactory) -> None:
    """A non-existent store file receives the typed missing-file refusal."""
    store_path = str(Path(str(tmpdir)) / "nonexistent.env")

    with pytest.raises(PermissionViolation) as exc_info:
        StoragePermissionGuard().check_store_access(store_path, "TEST_ENV_NAME")

    assert exc_info.value.reason == "file_missing"


def test_broker_denies_a_permissive_store_before_secret_resolution(
    tmpdir: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broker converts a guard refusal to credential_unavailable safely."""
    store_dir = Path(str(tmpdir)) / "store"
    store_dir.mkdir(mode=0o700)
    store_file = store_dir / "connections.env"
    store_file.write_text("DUMMY_VALUE=secret123")
    os.chmod(store_file, 0o644)
    _pin_registry(monkeypatch, "TEST_ENV_NAME")
    monkeypatch.setattr(broker, "_get_store_path_for_env", lambda _env: str(store_file))
    monkeypatch.setenv("TEST_ENV_NAME", "test-credential-123")
    monkeypatch.setenv(broker.STORE_PERMISSION_GUARD_ENV, "enforce")
    register_permission_guard(StoragePermissionGuard())

    with pytest.raises(BrokerDenied) as exc_info:
        _resolve()

    assert exc_info.value.reason == "credential_unavailable"
    assert exc_info.value.cap_id == "TEST_ENV_NAME"
    assert exc_info.value.detail == "Store access denied: file_too_permissive"
    assert "secret123" not in str(exc_info.value)
    assert str(store_file) not in str(exc_info.value)
    # U-R3 coexistence: a guard refusal keeps its OWN next-action and never
    # borrows the provisioning codes' remedy.
    assert exc_info.value.next_action == (
        "operator must repair secret-store permissions for this credential"
    )
    assert exc_info.value.next_action != broker._DENIAL_NEXT_ACTIONS["credential_missing"]
    assert exc_info.value.next_action != broker._DENIAL_NEXT_ACTIONS["capability_unprovisioned"]


# --- rollout rungs ---------------------------------------------------------
#
# U-S1 shipped the guard registered only from its own tests, so production
# resolution never consulted it. Arming it is only safe with a rung control,
# because the store MAPPING is still a U-S3 placeholder and a mapping mistake in
# enforce mode refuses every outbound call on the box.


def test_guard_defaults_to_shadow_and_does_not_refuse(
    tmpdir: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default rung observes and reports; the credential still resolves."""
    store_dir = Path(str(tmpdir)) / "store"
    store_dir.mkdir(mode=0o700)
    store_file = store_dir / "connections.env"
    store_file.write_text("DUMMY_VALUE=secret123")
    os.chmod(store_file, 0o644)
    _pin_registry(monkeypatch, "TEST_ENV_NAME")
    monkeypatch.setattr(broker, "_get_store_path_for_env", lambda _env: str(store_file))
    monkeypatch.setenv("TEST_ENV_NAME", "test-credential-123")
    monkeypatch.delenv(broker.STORE_PERMISSION_GUARD_ENV, raising=False)

    assert broker._store_permission_guard_mode() == "shadow"
    assert _resolve() == "test-credential-123"


def test_guard_off_rung_never_consults_the_store(
    tmpdir: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``off`` means off: the guard is not even asked."""
    _pin_registry(monkeypatch, "TEST_ENV_NAME")
    monkeypatch.setenv(broker.STORE_PERMISSION_GUARD_ENV, "off")
    monkeypatch.setenv("TEST_ENV_NAME", "test-credential-123")

    def _explode(_env: str) -> str:  # pragma: no cover -- must not be called
        raise AssertionError("the store must not be consulted when the guard is off")

    monkeypatch.setattr(broker, "_get_store_path_for_env", _explode)

    assert _resolve() == "test-credential-123"


def test_an_unmapped_store_passes_through_and_is_not_a_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No local store maps this credential -> nothing was checked, nothing refused.

    The pre-fix mapping named ``~/.config/omni/connections.env`` for EVERY
    credential, so on any machine without that file the guard turned a mapping
    failure into ``credential_unavailable`` for 100% of outbound calls. "We did
    not check" must never be spelled the same way as "we refused".
    """
    _pin_registry(monkeypatch, "TEST_ENV_NAME")
    monkeypatch.setenv(broker.STORE_PERMISSION_GUARD_ENV, "enforce")
    monkeypatch.setenv("TEST_ENV_NAME", "test-credential-123")
    monkeypatch.setattr(broker, "_get_store_path_for_env", lambda _env: None)

    assert _resolve() == "test-credential-123"


def test_an_absent_credential_is_still_credential_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The denial reasons stay distinct: unmapped store != absent value.

    U-R3 added two provisioning codes either side of this one, so the pin is
    now three-way: a permission refusal is ``credential_unavailable``, ONE
    absent name beside a provisioned sibling is ``credential_missing``, and a
    connector with nothing provisioned at all is ``capability_unprovisioned``.
    """
    _pin_registry(monkeypatch, "TEST_ENV_NAME", "TEST_ENV_SIBLING")
    monkeypatch.setenv(broker.STORE_PERMISSION_GUARD_ENV, "enforce")
    monkeypatch.delenv("TEST_ENV_NAME", raising=False)
    monkeypatch.setenv("TEST_ENV_SIBLING", "sibling-is-provisioned")
    monkeypatch.setattr(broker, "_get_store_path_for_env", lambda _env: None)

    with pytest.raises(BrokerDenied) as exc_info:
        _resolve()
    assert exc_info.value.reason == "credential_missing"
    assert exc_info.value.cap_id == "TEST_ENV_NAME"


def test_a_wholly_unprovisioned_connector_is_not_credential_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing provisioned is an operator gap, not a transient absence."""
    _pin_registry(monkeypatch, "TEST_ENV_NAME", "TEST_ENV_SIBLING")
    monkeypatch.setenv(broker.STORE_PERMISSION_GUARD_ENV, "enforce")
    monkeypatch.delenv("TEST_ENV_NAME", raising=False)
    monkeypatch.delenv("TEST_ENV_SIBLING", raising=False)
    monkeypatch.setattr(broker, "_get_store_path_for_env", lambda _env: None)

    with pytest.raises(BrokerDenied) as exc_info:
        _resolve()
    assert exc_info.value.reason == "capability_unprovisioned"
    assert exc_info.value.cap_id == "guardfixture.read"


def test_store_owned_by_another_account_is_refused(
    tmpdir: pytest.TempPathFactory,
) -> None:
    """Mode alone is not containment; a store this process does not own is not trusted."""
    store_dir = Path(str(tmpdir)) / "store"
    store_dir.mkdir(mode=0o700)
    store_file = store_dir / "connections.env"
    store_file.write_text("VALID=secret")
    os.chmod(store_file, 0o600)

    real_stat = os.stat

    def _foreign_owner(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if os.path.realpath(path) == os.path.realpath(str(store_file)):
            fields = list(result)
            fields[4] = os.geteuid() + 4242  # st_uid
            return os.stat_result(fields)
        return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "stat", _foreign_owner)
        with pytest.raises(PermissionViolation) as exc_info:
            StoragePermissionGuard().check_store_access(str(store_file), "TEST_ENV_NAME")

    assert exc_info.value.reason == "wrong_owner"
