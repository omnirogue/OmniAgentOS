from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.toolplane.manifest import CapabilityManifest, load_manifest
from omniagentos.toolplane.tools import dispatch


@pytest.fixture(autouse=True)
def _guard_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIAGENTOS_READBEFOREWRITE_ENABLED", raising=False)


def _manifest(tmp_path: Path) -> CapabilityManifest:
    root = tmp_path / "files"
    root.mkdir()
    return load_manifest(
        {
            "run_id": "run-read-before-write",
            "session_id": "session-read-before-write",
            "holder_generation": 1,
            "read_roots": [str(root)],
            "write_roots": [str(root)],
            "allowed_ops": ["read_file", "hash_file", "write_file", "edit_file"],
        }
    )


def test_new_file_write_without_prior_read_is_allowed(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    target = tmp_path / "files" / "new.txt"

    result = dispatch("write_file", manifest, {"path": str(target), "content": "new"})

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "new"


def test_existing_file_write_without_prior_read_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    target = tmp_path / "files" / "existing.txt"
    target.write_text("original", encoding="utf-8")

    result = dispatch("write_file", manifest, {"path": str(target), "content": "replacement"})

    assert result == {
        "ok": False,
        "error": "read_before_write",
        "detail": f"read {target} first; it changed since your last read",
    }
    assert target.read_text(encoding="utf-8") == "original"


def test_existing_file_read_then_write_is_allowed(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    target = tmp_path / "files" / "existing.txt"
    target.write_text("original", encoding="utf-8")

    assert dispatch("read_file", manifest, {"path": str(target)})["ok"] is True
    result = dispatch("write_file", manifest, {"path": str(target), "content": "replacement"})

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "replacement"
    assert (
        dispatch("write_file", manifest, {"path": str(target), "content": "blind second write"})[
            "error"
        ]
        == "read_before_write"
    )


def test_file_changed_between_read_and_write_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    target = tmp_path / "files" / "existing.txt"
    target.write_text("original", encoding="utf-8")
    dispatch("read_file", manifest, {"path": str(target)})
    target.write_text("concurrent change", encoding="utf-8")

    result = dispatch("write_file", manifest, {"path": str(target), "content": "replacement"})

    assert result["error"] == "read_before_write"
    assert result["detail"] == f"read {target} first; it changed since your last read"
    assert target.read_text(encoding="utf-8") == "concurrent change"


def test_guard_disabled_via_env_var_bypasses_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest(tmp_path)
    target = tmp_path / "files" / "existing.txt"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setenv("OMNIAGENTOS_READBEFOREWRITE_ENABLED", "false")

    result = dispatch("write_file", manifest, {"path": str(target), "content": "replacement"})

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "replacement"


def test_multiple_reads_use_the_last_observed_digest(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    target = tmp_path / "files" / "existing.txt"
    target.write_text("first", encoding="utf-8")
    dispatch("read_file", manifest, {"path": str(target)})
    target.write_text("second", encoding="utf-8")

    assert dispatch("read_file", manifest, {"path": str(target)})["ok"] is True
    result = dispatch("write_file", manifest, {"path": str(target), "content": "third"})

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "third"


def test_hash_file_read_allows_write(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    target = tmp_path / "files" / "existing.txt"
    target.write_text("original", encoding="utf-8")

    assert dispatch("hash_file", manifest, {"path": str(target)})["ok"] is True
    result = dispatch("write_file", manifest, {"path": str(target), "content": "replacement"})

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "replacement"


def test_edit_file_also_requires_a_matching_read(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    target = tmp_path / "files" / "existing.txt"
    target.write_text("old value", encoding="utf-8")

    rejected = dispatch(
        "edit_file", manifest, {"path": str(target), "old_str": "old", "new_str": "new"}
    )
    assert rejected["error"] == "read_before_write"

    dispatch("read_file", manifest, {"path": str(target)})
    allowed = dispatch(
        "edit_file", manifest, {"path": str(target), "old_str": "old", "new_str": "new"}
    )
    assert allowed["ok"] is True
    assert target.read_text(encoding="utf-8") == "new value"
