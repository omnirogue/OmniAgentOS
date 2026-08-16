"""The conflict matrix, exhaustively: 4 kind-pairs x 4 path relationships = 16 cells.

Plus the two properties the rest of Phase 3 depends on: whole-realm (``()``)
behaves as an ancestor of everything including itself, and blocker selection is
deterministic so two racing claimants queue behind the SAME lock.
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from omniagentos.scope.conflict import (
    CONFLICT_REASONS,
    ScopeConflict,
    conflicts_with,
    find_conflicts,
    first_conflict,
)
from omniagentos.scope.model import COMMIT_PURPOSE, ScopeClaim
from omniagentos.scope.paths import ScopePathError

REALM = "/realm/one"
OTHER_REALM = "/realm/two"


def claim(
    path: str,
    kind: str = "file",
    *,
    realm: str = REALM,
    owner_group: str = "",
    purpose: str = "edit",
    lock_id: str = "",
) -> ScopeClaim:
    return ScopeClaim.for_path(
        realm,
        path,
        kind=kind,  # type: ignore[arg-type]
        owner_group=owner_group,
        purpose=purpose,
        lock_id=lock_id,
    )


# ---------------------------------------------------------------------------
# The 16 cells
# ---------------------------------------------------------------------------

# (candidate_path, candidate_kind, held_path, held_kind, expected_reason)
#
# Four path relationships per kind-pair:
#   equal            src/a      vs  src/a
#   candidate deeper src/a/b    vs  src/a
#   held deeper      src/a      vs  src/a/b
#   disjoint         src/a      vs  src/z
MATRIX: list[tuple[str, str, str, str, str | None]] = [
    # --- candidate=file, held=file : collide ONLY on the same exact path -----
    ("src/a", "file", "src/a", "file", "same_file"),
    ("src/a/b", "file", "src/a", "file", None),
    ("src/a", "file", "src/a/b", "file", None),
    ("src/a", "file", "src/z", "file", None),
    # --- candidate=file, held=root : collide when candidate is UNDER held ----
    ("src/a", "file", "src/a", "root", "candidate_under_held"),
    ("src/a/b", "file", "src/a", "root", "candidate_under_held"),
    ("src/a", "file", "src/a/b", "root", None),
    ("src/a", "file", "src/z", "root", None),
    # --- candidate=root, held=file : collide when held is UNDER candidate ----
    ("src/a", "root", "src/a", "file", "held_under_candidate"),
    ("src/a/b", "root", "src/a", "file", None),
    ("src/a", "root", "src/a/b", "file", "held_under_candidate"),
    ("src/a", "root", "src/z", "file", None),
    # --- candidate=root, held=root : collide when the subtrees OVERLAP -------
    ("src/a", "root", "src/a", "root", "root_overlap"),
    ("src/a/b", "root", "src/a", "root", "root_overlap"),
    ("src/a", "root", "src/a/b", "root", "root_overlap"),
    ("src/a", "root", "src/z", "root", None),
]


class TestMatrix:
    def test_the_table_covers_every_cell(self) -> None:
        assert len(MATRIX) == 16
        pairs = {(row[1], row[3]) for row in MATRIX}
        assert pairs == {("file", "file"), ("file", "root"), ("root", "file"), ("root", "root")}

    @pytest.mark.parametrize(("c_path", "c_kind", "h_path", "h_kind", "expected"), MATRIX)
    def test_cell(
        self, c_path: str, c_kind: str, h_path: str, h_kind: str, expected: str | None
    ) -> None:
        assert conflicts_with(claim(c_path, c_kind), claim(h_path, h_kind)) == expected

    @pytest.mark.parametrize(("c_path", "c_kind", "h_path", "h_kind", "expected"), MATRIX)
    def test_never_collides_across_realms(
        self, c_path: str, c_kind: str, h_path: str, h_kind: str, expected: str | None
    ) -> None:
        """A realm IS the conflict universe -- two run workspaces must never block
        each other however identical their relative paths are."""
        candidate = claim(c_path, c_kind, realm=REALM)
        held = claim(h_path, h_kind, realm=OTHER_REALM)
        assert conflicts_with(candidate, held) is None

    def test_component_wise_never_string_prefix(self) -> None:
        """`github/x` is not inside `.github`, and `srcx` is not inside `src`."""
        assert conflicts_with(claim("github/x"), claim(".github", "root")) is None
        assert conflicts_with(claim("srcx/a"), claim("src", "root")) is None
        assert conflicts_with(claim(".github/x"), claim(".github", "root")) == (
            "candidate_under_held"
        )

    def test_every_reason_the_matrix_can_return_is_declared(self) -> None:
        produced = {row[4] for row in MATRIX if row[4] is not None}
        assert produced == set(CONFLICT_REASONS)


class TestWholeRealm:
    """`()` is an ancestor of everything INCLUDING ITSELF."""

    @pytest.mark.parametrize("held_path", [".", "src", "src/a/b"])
    @pytest.mark.parametrize("held_kind", ["file", "root"])
    def test_a_whole_realm_root_blocks_every_held_claim(
        self, held_path: str, held_kind: str
    ) -> None:
        if held_path == "." and held_kind == "file":
            pytest.skip("the whole realm cannot be a file")
        candidate = claim(".", "root")
        assert conflicts_with(candidate, claim(held_path, held_kind)) is not None

    @pytest.mark.parametrize("candidate_path", [".", "src", "src/a/b"])
    @pytest.mark.parametrize("candidate_kind", ["file", "root"])
    def test_a_held_whole_realm_root_blocks_every_candidate(
        self, candidate_path: str, candidate_kind: str
    ) -> None:
        if candidate_path == "." and candidate_kind == "file":
            pytest.skip("the whole realm cannot be a file")
        held = claim(".", "root")
        assert conflicts_with(claim(candidate_path, candidate_kind), held) is not None

    def test_whole_realm_collides_with_itself(self) -> None:
        assert conflicts_with(claim(".", "root"), claim(".", "root")) == "root_overlap"

    def test_the_dot_spellings_are_all_the_same_claim(self) -> None:
        """The scheduler's `parent_all` flag disappears: `.` normalizes to `()`,
        so there is nothing left to special-case."""
        for spelling in (".", "./", "a/.."):
            assert claim(spelling, "root").components == ()
            assert conflicts_with(claim(spelling, "root"), claim("src/a")) == (
                "held_under_candidate"
            )


# ---------------------------------------------------------------------------
# Same-owner relaxation (near-dead by construction; the guard is the point)
# ---------------------------------------------------------------------------


class TestSameOwnerRelaxation:
    @pytest.mark.parametrize(("c_path", "c_kind", "h_path", "h_kind", "expected"), MATRIX)
    def test_the_relaxation_cannot_flip_any_matrix_cell(
        self, c_path: str, c_kind: str, h_path: str, h_kind: str, expected: str | None
    ) -> None:
        """Under today's matrix two DISTINCT files never collide in the first
        place, so the relaxation is provably a no-op. Asserted, not assumed --
        the day file/file semantics widen, this test is the alarm."""
        same = conflicts_with(
            claim(c_path, c_kind, owner_group="grp"),
            claim(h_path, h_kind, owner_group="grp"),
        )
        different = conflicts_with(
            claim(c_path, c_kind, owner_group="grp-a"),
            claim(h_path, h_kind, owner_group="grp-b"),
        )
        assert same == different == expected

    def test_a_root_claim_is_never_relaxed_even_for_a_co_owner(self) -> None:
        held_root = claim("src", "root", owner_group="grp")
        assert conflicts_with(claim("src/a", owner_group="grp"), held_root) is not None
        assert conflicts_with(claim("src/a", "root", owner_group="grp"), held_root) is not None
        assert (
            conflicts_with(
                claim("src", "root", owner_group="grp"), claim("src/a", owner_group="grp")
            )
            is not None
        )

    def test_the_same_file_is_never_relaxed(self) -> None:
        assert (
            conflicts_with(claim("src/a", owner_group="grp"), claim("src/a", owner_group="grp"))
            == "same_file"
        )

    def test_a_held_commit_is_never_relaxed(self) -> None:
        held = claim("src/a", owner_group="grp", purpose=COMMIT_PURPOSE)
        assert conflicts_with(claim("src/a", owner_group="grp"), held) == "same_file"

    def test_an_empty_owner_group_is_not_an_owner(self) -> None:
        """Two unowned claims must not accidentally count as co-owned."""
        assert conflicts_with(claim("src/a"), claim("src/a")) == "same_file"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestOrdering:
    def _held(self) -> list[ScopeClaim]:
        return [
            claim("src/a/b/c.py", lock_id="lock-deep"),
            claim("src/a", "root", lock_id="lock-mid"),
            claim(".", "root", lock_id="lock-realm"),
            claim("src/a/b", "root", lock_id="lock-midlow"),
        ]

    def test_broadest_blocker_wins(self) -> None:
        candidate = claim("src/a/b/c.py")
        found = find_conflicts(candidate, self._held())
        assert [c.held.lock_id for c in found] == [
            "lock-realm",  # depth 0
            "lock-mid",  # depth 2
            "lock-midlow",  # depth 3
            "lock-deep",  # depth 4
        ]
        blocker = first_conflict(candidate, self._held())
        assert blocker is not None and blocker.held.lock_id == "lock-realm"

    def test_first_conflict_matches_find_conflicts_head(self) -> None:
        candidate = claim("src/a/b/c.py")
        for order in itertools.permutations(self._held()):
            found = find_conflicts(candidate, order)
            head = first_conflict(candidate, order)
            assert head is not None
            assert head == found[0]

    def test_order_of_held_locks_never_changes_the_answer(self) -> None:
        """Two racing claimants iterate the lock table in whatever order the DB
        hands them; they must still name the SAME blocker."""
        candidate = claim("src/a/b/c.py")
        answers = {
            tuple(c.held.lock_id for c in find_conflicts(candidate, order))
            for order in itertools.permutations(self._held())
        }
        assert len(answers) == 1

    def test_equal_depth_ties_break_on_lock_id(self) -> None:
        candidate = claim("src", "root")
        held = [
            claim("src/z", lock_id="lock-z"),
            claim("src/a", lock_id="lock-a"),
            claim("src/m", lock_id="lock-m"),
        ]
        assert [c.held.lock_id for c in find_conflicts(candidate, held)] == [
            "lock-a",
            "lock-m",
            "lock-z",
        ]

    def test_equal_depth_and_lock_id_still_totally_ordered(self) -> None:
        """Lock ids should be unique, but a total order must not depend on it."""
        candidate = claim("src", "root")
        held = [claim("src/z", lock_id=""), claim("src/a", lock_id="")]
        assert [c.held.path_text for c in find_conflicts(candidate, held)] == [
            "src/a",
            "src/z",
        ]

    def test_no_conflict_returns_none_and_empty(self) -> None:
        candidate = claim("docs/readme.md")
        held = [claim("src/a", "root", lock_id="l1"), claim("tests/x.py", lock_id="l2")]
        assert first_conflict(candidate, held) is None
        assert find_conflicts(candidate, held) == []

    def test_held_in_another_realm_is_skipped(self) -> None:
        candidate = claim("src/a")
        held = [
            claim("src/a", realm=OTHER_REALM, lock_id="other"),
            claim("src/a", realm=REALM, lock_id="mine"),
        ]
        blocker = first_conflict(candidate, held)
        assert blocker is not None and blocker.held.lock_id == "mine"

    def test_conflict_describes_itself(self) -> None:
        blocker = first_conflict(claim("src/a"), [claim("src", "root", lock_id="l1")])
        assert blocker is not None
        text = blocker.describe()
        assert "l1" in text and "candidate_under_held" in text

    def test_conflicts_are_hashable_values(self) -> None:
        one = ScopeConflict(claim("src/a"), claim("src", "root"), "candidate_under_held")
        two = ScopeConflict(claim("src/a"), claim("src", "root"), "candidate_under_held")
        assert one == two
        assert len({one, two}) == 1


# ---------------------------------------------------------------------------
# Model invariants
# ---------------------------------------------------------------------------


class TestScopeClaim:
    def test_dot_is_forced_to_a_root(self) -> None:
        """`.` with a file-ish default is the common planner spelling for
        "this task owns everything"; it becomes the whole-realm ROOT."""
        built = ScopeClaim.for_path(REALM, ".", kind="file")
        assert built.components == () and built.kind == "root"
        assert built.is_whole_realm and built.is_root

    def test_a_file_claim_on_the_whole_realm_is_rejected(self) -> None:
        with pytest.raises(ScopePathError):
            ScopeClaim(realm=REALM, components=(), kind="file")

    @pytest.mark.parametrize("bad", ["/abs/path", "~/x", "../escape", "", "C:/x"])
    def test_bad_paths_are_rejected(self, bad: str) -> None:
        with pytest.raises(ScopePathError):
            ScopeClaim.for_path(REALM, bad)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"realm": "", "components": ("a",)},
            {"realm": "   ", "components": ("a",)},
            {"realm": REALM, "components": ("a",), "kind": "glob"},
            {"realm": REALM, "components": ("a", ""), "kind": "file"},
            {"realm": REALM, "components": ("a", ".."), "kind": "file"},
            {"realm": REALM, "components": ("a/b",), "kind": "file"},
            {"realm": REALM, "components": ["a"], "kind": "file"},
            {"realm": REALM, "components": ("a",), "kind": "file", "purpose": ""},
        ],
    )
    def test_invariants(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(ScopePathError):
            ScopeClaim(**kwargs)  # type: ignore[arg-type]

    def test_is_frozen_and_hashable(self) -> None:
        built = claim("src/a")
        assert {built, claim("src/a")} == {built}
        with pytest.raises(dataclasses.FrozenInstanceError):
            built.kind = "root"  # type: ignore[misc]

    def test_path_text_and_depth(self) -> None:
        assert claim(".", "root").path_text == "."
        assert claim(".", "root").depth == 0
        assert claim("src/a.py").path_text == "src/a.py"
        assert claim("src/a.py").depth == 2

    def test_str_marks_a_subtree(self) -> None:
        assert str(claim("src", "root")).endswith("src/**")
        assert str(claim("src/a.py")).endswith("src/a.py")
