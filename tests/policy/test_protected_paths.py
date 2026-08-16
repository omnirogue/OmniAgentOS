"""Hostile + floor tests for the protected-paths registry loader."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omniagentos.policy.protected_paths import (
    PROTECTED_PATHS_SCHEMA_VERSION,
    ProtectedPathsError,
    _clear_protected_paths_cache,
    _default_registry_path,
    _load_protected_paths,
    get_protected_paths,
    path_tier,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    _clear_protected_paths_cache()
    yield
    _clear_protected_paths_cache()


def _good_payload() -> dict:
    return yaml.safe_load(_default_registry_path().read_text(encoding="utf-8"))


def _write_registry(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "protected-paths.yaml"
    p.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return p


def test_default_registry_loads_and_classifies() -> None:
    reg = get_protected_paths()
    assert reg.version == PROTECTED_PATHS_SCHEMA_VERSION
    assert path_tier("configs/governance.yaml") == "P"
    assert path_tier("configs/protected-paths.yaml") == "P"
    assert path_tier("omniagentos/reliability/detector.py") == "S"
    assert path_tier("omniagentos/reliability/governance.py") == "P"  # P wins
    assert path_tier("notes/foo.txt") is None
    assert path_tier("/Users/x/Library/LaunchAgents/com.omniagentos.api.plist") == "P"
    assert path_tier(r"configs\governance.yaml") == "P"  # separator normalize


def test_missing_registry_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ProtectedPathsError, match="missing"):
        _load_protected_paths(missing)


def test_version_mutation_fails_closed(tmp_path: Path) -> None:
    payload = _good_payload()
    payload["version"] = 99
    path = _write_registry(tmp_path, payload)
    with pytest.raises(ProtectedPathsError, match="version"):
        _load_protected_paths(path)


def test_unknown_top_level_key_fails(tmp_path: Path) -> None:
    payload = _good_payload()
    payload["extra"] = True
    path = _write_registry(tmp_path, payload)
    with pytest.raises(ProtectedPathsError, match="unexpected top-level"):
        _load_protected_paths(path)


def test_duplicate_yaml_mapping_key_fails(tmp_path: Path) -> None:
    raw = _default_registry_path().read_text(encoding="utf-8")
    path = tmp_path / "duplicate-key.yaml"
    path.write_text(raw.replace("version: 1", "version: 1\nversion: 1", 1), encoding="utf-8")
    with pytest.raises(ProtectedPathsError, match="duplicate key"):
        _load_protected_paths(path)


def test_duplicate_nested_yaml_mapping_key_fails(tmp_path: Path) -> None:
    raw = _default_registry_path().read_text(encoding="utf-8")
    path = tmp_path / "duplicate-nested-key.yaml"
    path.write_text(
        raw.replace("tier_p:\n  files:", "tier_p:\n  files: []\n  files:", 1),
        encoding="utf-8",
    )
    with pytest.raises(ProtectedPathsError, match="duplicate key"):
        _load_protected_paths(path)


def test_non_string_yaml_mapping_key_fails(tmp_path: Path) -> None:
    raw = _default_registry_path().read_text(encoding="utf-8")
    path = tmp_path / "non-string-key.yaml"
    path.write_text(raw.replace("version: 1", "version: 1\n42: value", 1), encoding="utf-8")
    with pytest.raises(ProtectedPathsError, match="keys must be strings"):
        _load_protected_paths(path)


def test_duplicate_file_entry_fails(tmp_path: Path) -> None:
    payload = _good_payload()
    payload["tier_p"]["files"].append("configs/governance.yaml")
    path = _write_registry(tmp_path, payload)
    with pytest.raises(ProtectedPathsError, match="duplicate"):
        _load_protected_paths(path)


def test_cross_tier_file_ambiguity_fails(tmp_path: Path) -> None:
    payload = _good_payload()
    payload["tier_s"]["files"].append("configs/governance.yaml")
    path = _write_registry(tmp_path, payload)
    with pytest.raises(ProtectedPathsError, match="cross-tier file|demotion"):
        _load_protected_paths(path)


def test_floor_removal_fails(tmp_path: Path) -> None:
    payload = _good_payload()
    payload["tier_p"]["files"] = [
        f for f in payload["tier_p"]["files"] if f != "configs/governance.yaml"
    ]
    path = _write_registry(tmp_path, payload)
    with pytest.raises(ProtectedPathsError, match="floor removal"):
        _load_protected_paths(path)


def test_floor_demotion_to_tier_s_fails(tmp_path: Path) -> None:
    payload = _good_payload()
    # Move a floor Tier-P file into Tier S only.
    payload["tier_p"]["files"] = [
        f for f in payload["tier_p"]["files"] if f != "omniagentos/contracts.py"
    ]
    payload["tier_s"]["files"].append("omniagentos/contracts.py")
    path = _write_registry(tmp_path, payload)
    with pytest.raises(ProtectedPathsError, match="floor"):
        _load_protected_paths(path)


def test_traversal_entry_fails(tmp_path: Path) -> None:
    payload = _good_payload()
    payload["tier_p"]["files"].append("../secrets/token")
    path = _write_registry(tmp_path, payload)
    with pytest.raises(ProtectedPathsError, match="traversal"):
        _load_protected_paths(path)


def test_foreign_absolute_entry_fails(tmp_path: Path) -> None:
    payload = _good_payload()
    payload["tier_p"]["files"].append("/etc/passwd")
    path = _write_registry(tmp_path, payload)
    with pytest.raises(ProtectedPathsError, match="absolute|foreign"):
        _load_protected_paths(path)


def test_windows_absolute_entry_fails(tmp_path: Path) -> None:
    payload = _good_payload()
    payload["tier_p"]["files"].append(r"C:\Windows\System32\drivers\etc\hosts")
    path = _write_registry(tmp_path, payload)
    with pytest.raises(ProtectedPathsError, match="absolute|foreign"):
        _load_protected_paths(path)


def test_separator_confusion_normalizes_and_matches_floor(tmp_path: Path) -> None:
    payload = _good_payload()
    # Replace a floor entry with backslash spelling — must normalize to same path.
    files = payload["tier_p"]["files"]
    files = [r"configs\governance.yaml" if f == "configs/governance.yaml" else f for f in files]
    payload["tier_p"]["files"] = files
    path = _write_registry(tmp_path, payload)
    reg = _load_protected_paths(path)
    assert "configs/governance.yaml" in reg.tier_p_files
    assert reg.path_tier(r"configs\governance.yaml") == "P"


def test_registry_symlink_fails_without_touching_authority(tmp_path: Path) -> None:
    target = _write_registry(tmp_path, _good_payload())
    link = tmp_path / "registry-link.yaml"
    link.symlink_to(target)
    with pytest.raises(ProtectedPathsError, match="must not be a symlink"):
        _load_protected_paths(link)


def test_wrong_value_types_fail(tmp_path: Path) -> None:
    payload = _good_payload()
    payload["tier_p"]["files"] = "not-a-list"
    path = _write_registry(tmp_path, payload)
    with pytest.raises(ProtectedPathsError, match="must be a list"):
        _load_protected_paths(path)


def test_load_error_never_returns_empty_protection(tmp_path: Path) -> None:
    with pytest.raises(ProtectedPathsError):
        _load_protected_paths(tmp_path / "gone.yaml")
    # Default still loads full coverage when cache cleared after a failed custom load.
    _clear_protected_paths_cache()
    reg = get_protected_paths()
    assert len(reg.tier_p_files) >= 7
    assert len(reg.tier_s_files) >= 6


def test_duplicate_unknown_launchagent_key_fails(tmp_path: Path) -> None:
    payload = _good_payload()
    payload["tier_p"]["launchagents"]["extra"] = 1
    path = _write_registry(tmp_path, payload)
    with pytest.raises(ProtectedPathsError, match="unexpected launchagents"):
        _load_protected_paths(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path_contains", "prefix/Library/LaunchAgents/com.omniagentos."),
        ("basename_contains", "prefix-com.omniagentos."),
    ],
)
def test_launchagent_narrowing_fails(tmp_path: Path, field: str, value: str) -> None:
    payload = _good_payload()
    payload["tier_p"]["launchagents"][field] = value
    path = _write_registry(tmp_path, payload)
    with pytest.raises(ProtectedPathsError, match="floor demotion"):
        _load_protected_paths(path)


def test_registry_cannot_remove_its_own_tier_p_floor(tmp_path: Path) -> None:
    payload = _good_payload()
    payload["tier_p"]["files"].remove("configs/protected-paths.yaml")
    path = _write_registry(tmp_path, payload)
    with pytest.raises(ProtectedPathsError, match="floor removal"):
        _load_protected_paths(path)


def test_same_tier_file_directory_overlap_fails(tmp_path: Path) -> None:
    payload = _good_payload()
    payload["tier_p"]["files"].append("contracts/example.json")
    path = _write_registry(tmp_path, payload)
    with pytest.raises(ProtectedPathsError, match="overlapping tier_p"):
        _load_protected_paths(path)


def test_module_shadow_stems_present() -> None:
    reg = get_protected_paths()
    assert "governance" in reg.protected_p_modules
    assert "detector" in reg.protected_s_modules
