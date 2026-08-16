"""safe_slug / ensure_safe_write_target / open_no_follow
(omniagentos/selfimprove/paths.py) — path-safety adversarial tests (F3, F4, F8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.selfimprove.paths import (
    PathEscapesRootError,
    ensure_safe_write_target,
    open_no_follow,
    safe_slug,
)

# ---------------------------------------------------------------------------
# F4: safe_slug must be injective (no deterministic collisions)
# ---------------------------------------------------------------------------


def test_safe_slug_hashed_form_of_unsafe_value_does_not_collide_with_that_literal_slug() -> None:
    # Before F4: safe_slug("foo!") sanitized+hashed to some "foo-<digest>",
    # and that exact string, fed back into safe_slug, returned unchanged
    # (already "safe") — two different project/skill identifiers ended up
    # sharing one storage location.
    hashed = safe_slug("foo!")
    assert safe_slug(hashed) != hashed


def test_safe_slug_is_injective_across_a_batch_of_distinct_inputs() -> None:
    values = [
        "note",
        "",  # handled separately below (blank is rejected, not "note")
        "foo",
        "foo!",
        "foo-c0e0aaaea050",
        "Foo",
        "foo ",
        "../../etc/passwd",
        "a/b/c",
        "weird/project name!!",
        "code-changes",
    ]
    values = [v for v in values if v.strip()]
    slugs = [safe_slug(v) for v in values]
    assert len(slugs) == len(set(slugs)), f"collision among slugs: {slugs}"


def test_safe_slug_same_input_is_deterministic() -> None:
    assert safe_slug("some-project") == safe_slug("some-project")


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_safe_slug_rejects_blank_identifiers(blank: str) -> None:
    with pytest.raises(ValueError, match="non-blank"):
        safe_slug(blank)


def test_safe_slug_note_and_literal_note_no_longer_collide_with_blank() -> None:
    # "note" was the previous fallback for blank input; both a blank
    # identifier (now rejected outright) and the literal string "note" must
    # never resolve to the same directory.
    assert safe_slug("note") != "note"


def test_safe_slug_handles_unicode_and_control_characters_without_raising() -> None:
    weird = "caf\xe9\x00\x07☃"  # café + NUL + BEL + snowman
    slug = safe_slug(weird)
    assert slug  # produced something
    assert "/" not in slug and "\\" not in slug
    assert "\x00" not in slug


def test_safe_slug_traversal_payload_never_contains_dotdot_or_slash() -> None:
    slug = safe_slug("../../etc/passwd")
    assert ".." not in slug
    assert "/" not in slug


# ---------------------------------------------------------------------------
# F3: pre-existing symlinks must not redirect writes outside the root
# ---------------------------------------------------------------------------


def test_ensure_safe_write_target_allows_a_normal_nested_path(tmp_path: Path) -> None:
    target = tmp_path / "proj" / "CONSTRAINTS.md"
    ensure_safe_write_target(tmp_path, target)  # must not raise


def test_ensure_safe_write_target_rejects_symlinked_parent_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    root = tmp_path / "constraints"
    root.mkdir()
    (root / "proj").symlink_to(outside, target_is_directory=True)
    target = root / "proj" / "CONSTRAINTS.md"

    with pytest.raises(PathEscapesRootError):
        ensure_safe_write_target(root, target)
    assert not (outside / "CONSTRAINTS.md").exists()


def test_ensure_safe_write_target_rejects_symlinked_leaf_file(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-file.md"
    outside.write_text("do not touch\n", encoding="utf-8")
    root = tmp_path / "skills"
    (root / "my-skill").mkdir(parents=True)
    (root / "my-skill" / "SKILL.md").symlink_to(outside)
    target = root / "my-skill" / "SKILL.md"

    with pytest.raises(PathEscapesRootError):
        ensure_safe_write_target(root, target)
    assert outside.read_text(encoding="utf-8") == "do not touch\n"


def test_open_no_follow_refuses_to_open_a_symlinked_leaf(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-file2.md"
    outside.write_text("original\n", encoding="utf-8")
    link = tmp_path / "CONSTRAINTS.md"
    link.symlink_to(outside)

    with pytest.raises(OSError):
        with open_no_follow(link, "a+", encoding="utf-8") as handle:
            handle.write("pwned\n")

    assert outside.read_text(encoding="utf-8") == "original\n"


def test_open_no_follow_writes_normally_when_no_symlink_involved(tmp_path: Path) -> None:
    path = tmp_path / "CONSTRAINTS.md"
    with open_no_follow(path, "w", encoding="utf-8") as handle:
        handle.write("hello\n")
    assert path.read_text(encoding="utf-8") == "hello\n"
