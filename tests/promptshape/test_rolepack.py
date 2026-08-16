"""Tests for role pack assembly, validation, and caching."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omniagentos.promptshape.rolepack import JOB_ROLES, clear_role_pack_cache, role_pack


def _create_fake_root(tmp_path: Path, base_content: str = "BASE") -> Path:
    """Helper to build a fake root under tmp_path with universal-base.md."""
    vault_dir = tmp_path / "vault" / "prompts"
    roles_dir = vault_dir / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)

    base_file = vault_dir / "universal-base.md"
    base_file.write_text(base_content, encoding="utf-8")
    return tmp_path


def _write_fake_role(root: Path, role_name: str, content: str) -> None:
    """Helper to write a role markdown file under a fake root."""
    role_file = root / "vault" / "prompts" / "roles" / f"{role_name}.md"
    role_file.write_text(content, encoding="utf-8")


def test_byte_identical_across_n_calls(tmp_path: Path) -> None:
    """Assert every rendered .text is byte-identical across N >= 5 calls,

    including a call after clear_role_pack_cache() so identity is proven
    from disk, not from a cached object. Assert on .encode('utf-8') bytes.
    """
    root = _create_fake_root(tmp_path, "UNIVERSAL_BASE")
    _write_fake_role(root, "implementer", "ROLE_TEXT")

    clear_role_pack_cache()
    results: list[bytes] = []

    # 5 calls, clearing cache before the last one
    for i in range(5):
        if i == 4:
            clear_role_pack_cache()
        seg = role_pack("implementer", root=root)
        assert seg is not None
        results.append(seg.text.encode("utf-8"))

    # Assert all encoded bytes are identical to the first
    first = results[0]
    for idx, r in enumerate(results):
        assert r == first, f"Mismatch at call {idx}"


def test_typo_role_rejected_with_log(caplog: pytest.LogCaptureFixture) -> None:
    """Assert typo roles are rejected with a warning and return None (against real checkout root)."""
    import logging
    with caplog.at_level(logging.WARNING):
        # 1. First typo: "implementor"
        caplog.clear()
        seg = role_pack("implementor")
        assert seg is None
        assert "implementor" in caplog.text
        assert "expected one of" in caplog.text
        assert "single path component" not in caplog.text

        # 2. Second typo: "reviewers"
        caplog.clear()
        seg2 = role_pack("reviewers")
        assert seg2 is None
        assert "reviewers" in caplog.text
        assert "expected one of" in caplog.text
        assert "single path component" not in caplog.text


def test_unknown_role_returns_none(tmp_path: Path) -> None:
    """An unknown role returns None (rejection comes from the allowlist)."""
    root = _create_fake_root(tmp_path, "BASE")
    seg = role_pack("no-such-role", root=root)
    assert seg is None


def test_path_traversal_attempt_rejected(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Path traversal attempts must be rejected and return None.

    This is a revert-tested guard: deleting the single-component check in
    rolepack.py MUST make this test fail. It covers both relative and absolute
    path traversal escape mechanisms with real, existing target files outside
    the allowed directories.
    """
    import logging
    root = _create_fake_root(tmp_path, "BASE")
    sentinel_content = "SECRET_SENTINEL_DATA"

    # 1. Relative attack target: sentinel at tmp_path/"secret.md"
    rel_file = tmp_path / "secret.md"
    rel_file.write_text(sentinel_content, encoding="utf-8")

    # 2. Absolute attack target: sentinel at an absolute path outside root
    abs_dir = tmp_path.parent / f"outside_root_{tmp_path.name}"
    abs_dir.mkdir(parents=True, exist_ok=True)
    abs_file = abs_dir / "secret.md"
    abs_file.write_text(sentinel_content, encoding="utf-8")

    # 3. Cheap cases targets: "..", ".", "", "roles\\planner"
    roles_dir = tmp_path / "vault" / "prompts" / "roles"
    (roles_dir / "..md").write_text(sentinel_content, encoding="utf-8")
    (roles_dir / ".md").write_text(sentinel_content, encoding="utf-8")
    (roles_dir / "roles\\planner.md").write_text(sentinel_content, encoding="utf-8")

    # Every hostile input names a file that GENUINELY EXISTS and WOULD be read if the guard is removed
    forbidden_inputs = [
        "../../../secret",                 # relative attack (resolves to tmp_path/"secret.md")
        str(abs_file.with_suffix("")),    # absolute attack (resolves to abs_file)
        "..",                              # resolves to roles_dir/"..md"
        ".",                               # resolves to roles_dir/".md"
        "",                                # resolves to roles_dir/".md"
        "roles\\planner",                  # resolves to roles_dir/"roles\\planner.md"
    ]

    with caplog.at_level(logging.WARNING):
        for job_role in forbidden_inputs:
            caplog.clear()
            seg = role_pack(job_role, root=root)
            assert seg is None
            # Guarded check to ensure that if the guard is bypassed, we can assert leaks
            if seg is not None:
                assert sentinel_content not in seg.text

            # Assert that the traversal warning was emitted, not the allowlist warning
            assert "single path component" in caplog.text
            assert "expected one of" not in caplog.text

    # Cover non-str inputs
    invalid_types: list[object] = [123, None, [], {}]
    for invalid_type in invalid_types:
        seg = role_pack(invalid_type, root=root)  # type: ignore[arg-type]
        assert seg is None
        if seg is not None:
            assert sentinel_content not in seg.text


def test_missing_file_returns_none_not_exception(tmp_path: Path) -> None:
    """Missing file -> None not exception: a root where universal-base.md exists

    but the role .md does not, AND a root where neither exists.
    """
    # 1. Root where universal-base.md exists but the role .md does not
    root_only_base = tmp_path / "root_only_base"
    _create_fake_root(root_only_base, "BASE")
    seg1 = role_pack("implementer", root=root_only_base)
    assert seg1 is None

    # 2. Root where neither exists
    root_empty = tmp_path / "root_empty"
    seg2 = role_pack("implementer", root=root_empty)
    assert seg2 is None


def test_all_seven_job_roles_resolve_non_none() -> None:
    """All 14 JOB_ROLES resolve non-None against the real checkout root.

    Each must be a Segment with kind == "stable" and non-empty text
    that contains the universal-base text.
    """
    clear_role_pack_cache()
    # Read the real universal base directly to compare
    real_root = Path(__file__).resolve().parents[2]
    base_file = real_root / "vault" / "prompts" / "universal-base.md"
    assert base_file.exists(), f"Real universal-base.md missing at: {base_file}"
    base_text = base_file.read_text(encoding="utf-8").strip()

    for role in JOB_ROLES:
        seg = role_pack(role)
        assert seg is not None, f"Role {role!r} failed to load from real checkout"
        assert seg.kind == "stable"
        assert seg.text, f"Role {role!r} resolved with empty text"
        assert base_text in seg.text, f"Role {role!r} did not contain the universal-base text"


def test_cache_invalidation_on_mtime_change(tmp_path: Path) -> None:
    """Test cache invalidation on mtime change of either base or role file."""
    root = _create_fake_root(tmp_path, "BASE")
    _write_fake_role(root, "implementer", "ROLE_CONTENT")

    clear_role_pack_cache()

    # Initial call to cache
    seg1 = role_pack("implementer", root=root)
    assert seg1 is not None
    assert "BASE" in seg1.text
    assert "ROLE_CONTENT" in seg1.text

    # 1. Update role file, bump mtime, and check invalidation
    role_file = root / "vault" / "prompts" / "roles" / "implementer.md"
    role_file.write_text("NEW_ROLE_CONTENT", encoding="utf-8")

    # Bump mtime
    stat = role_file.stat()
    new_mtime = (stat.st_mtime_ns // 1_000_000_000) + 10
    os.utime(role_file, (new_mtime, new_mtime))

    seg2 = role_pack("implementer", root=root)
    assert seg2 is not None
    assert "NEW_ROLE_CONTENT" in seg2.text

    # 2. Update base file, bump mtime, and check invalidation
    base_file = root / "vault" / "prompts" / "universal-base.md"
    base_file.write_text("NEW_BASE", encoding="utf-8")

    base_stat = base_file.stat()
    new_base_mtime = (base_stat.st_mtime_ns // 1_000_000_000) + 10
    os.utime(base_file, (new_base_mtime, new_base_mtime))

    seg3 = role_pack("implementer", root=root)
    assert seg3 is not None
    assert "NEW_BASE" in seg3.text
    assert "NEW_ROLE_CONTENT" in seg3.text
