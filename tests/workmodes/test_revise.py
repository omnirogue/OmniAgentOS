"""Revision loop foundation: scope safety and lineage."""

from __future__ import annotations

import pytest

from omniagentos.workmodes.revise import (
    RevisionError,
    create_revision_spec,
    revision_write_set,
)


def test_version_increments_lineage_and_mode_preserved() -> None:
    prior = {
        "mode": "content",
        "scope": "tsk_1",
        "lineage_id": "lin_abc",
        "version": 1,
        "write_set": ("outputs/script.md", "outputs/notes.md"),
        "read_set": ("brief.md",),
        "manifest_hash": "abc123",
    }
    rev = create_revision_spec(prior, "drop em-dashes")
    assert rev.version == 2
    assert rev.mode == "content"
    assert rev.scope == "tsk_1"
    assert rev.lineage_id == "lin_abc"
    assert rev.write_set == ("outputs/script.md", "outputs/notes.md")
    assert rev.read_set == ("brief.md",)
    assert rev.prior_manifest_hash == "abc123"
    assert rev.feedback == "drop em-dashes"


def test_write_set_never_widens() -> None:
    prior = {
        "mode": "code",
        "scope": "pkg",
        "lineage_id": "lin_x",
        "version": 2,
        "write_set": ("src/a.py",),
    }
    rev = create_revision_spec(
        prior,
        "fix tests",
        requested_write_set=("src/a.py", "src/b.py", "/etc/passwd"),
    )
    assert rev.write_set == ("src/a.py",)
    assert rev.version == 3


def test_write_set_unchanged_when_no_request() -> None:
    prior = {
        "mode": "report",
        "scope": "s",
        "version": 1,
        "write_set": ("a.md", "b.md"),
    }
    rev = create_revision_spec(prior, "tighten intro")
    assert rev.write_set == ("a.md", "b.md")
    # lineage derived deterministically when missing
    assert rev.lineage_id.startswith("lin_")


def test_read_set_may_grow() -> None:
    prior = {
        "mode": "code",
        "scope": "s",
        "version": 1,
        "write_set": ("a.py",),
        "read_set": ("a.py",),
    }
    rev = create_revision_spec(
        prior,
        "use helper",
        requested_read_set=("a.py", "helpers.py", "lib.py"),
    )
    assert rev.read_set == ("a.py", "helpers.py", "lib.py")


def test_empty_feedback_rejected() -> None:
    with pytest.raises(RevisionError):
        create_revision_spec(
            {"mode": "report", "scope": "s", "version": 1, "write_set": ("x",)},
            "  ",
        )


def test_revision_write_set_helper() -> None:
    assert revision_write_set(("a", "b", "c"), None) == ("a", "b", "c")
    assert revision_write_set(("a", "b", "c"), ("c", "a", "z")) == ("a", "c")
    assert revision_write_set(("a",), ("z",)) == ()


#: The three shapes ``_prior_write_set`` accepts, each carrying the SAME two
#: paths, one of them padded. Written as data rather than as three test bodies so
#: a fourth accepted shape has to be added here to pass -- a hand-written list of
#: the branches has the same failure mode as the inconsistency it is testing.
PRIOR_SHAPES: tuple[tuple[str, dict[str, object]], ...] = (
    ("write_set", {"write_set": ["outputs/report.md ", "outputs/notes.md"]}),
    (
        "entries",
        {"entries": [{"rel_path": "outputs/report.md "}, {"rel_path": "outputs/notes.md"}]},
    ),
    ("rel_paths", {"rel_paths": ["outputs/report.md ", "outputs/notes.md"]}),
)


@pytest.mark.parametrize(("shape", "carrier"), PRIOR_SHAPES, ids=[s for s, _ in PRIOR_SHAPES])
def test_padded_prior_path_is_not_an_illegal_widening(
    shape: str, carrier: dict[str, object]
) -> None:
    """Whitespace on a prior path is not a widening, whichever shape carries it.

    ``revision_write_set`` strips; every branch of ``_prior_write_set`` must strip
    the same way, or the membership test in ``create_revision_spec`` compares a
    stripped path against an unstripped set and raises "widened illegally" at a
    caller that requested nothing. The ``entries`` branch did exactly that, and
    it is the one branch no test covered.
    """
    prior = {"mode": "report", "scope": "q3", "version": 1, **carrier}
    rev = create_revision_spec(prior, "tighten it")
    assert rev.write_set == ("outputs/report.md", "outputs/notes.md"), shape


def test_object_prior_manifest() -> None:
    class Prior:
        mode = "image"
        scope = "hero"
        lineage_id = "lin_img"
        version = 4
        write_set = ("hero.png",)
        read_set = ()
        content_hash = "deadbeef"

    rev = create_revision_spec(Prior(), "more contrast")
    assert rev.version == 5
    assert rev.mode == "image"
    assert rev.prior_manifest_hash == "deadbeef"
