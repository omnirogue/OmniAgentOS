"""Path algebra: normalization, containment, and — the load-bearing one — realms."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from omniagentos.scope import paths as sp
from omniagentos.scope.paths import (
    ScopePathError,
    clear_realm_cache,
    normalize_rel,
    overlap,
    private_workspace_bases,
    realm_of,
    register_private_base,
    rel_text,
    resolve_into_realm,
    safe_component,
    under,
)

DARWIN = sys.platform == "darwin"


@pytest.fixture(autouse=True)
def _isolated_realm_cache() -> Iterator[None]:
    """The git-toplevel memo is process-global; a stale entry would make a test
    that creates a repo mid-run see the pre-``git init`` answer."""
    clear_realm_cache()
    saved = list(sp._EXTRA_PRIVATE_BASES)
    yield
    sp._EXTRA_PRIVATE_BASES[:] = saved
    clear_realm_cache()


def _git_init(path: Path) -> None:
    subprocess.run(
        ("git", "-c", "init.defaultBranch=main", "init", "-q", str(path)),
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# normalize_rel / rel_text
# ---------------------------------------------------------------------------


class TestNormalizeRel:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("src/a.py", ("src", "a.py")),
            ("./a//b/", ("a", "b")),
            ("  src/a.py  ", ("src", "a.py")),
            ("a/x/../b", ("a", "b")),
            ("src\\a.py", ("src", "a.py")),  # backslashes fold, never one component
            (".github/workflows/ci.yml", (".github", "workflows", "ci.yml")),
            ("a/./b/./c", ("a", "b", "c")),
        ],
    )
    def test_normalizes(self, raw: str, expected: tuple[str, ...]) -> None:
        assert normalize_rel(raw) == expected

    @pytest.mark.parametrize("raw", [".", "./", "", "a/..", "./.", "a/b/../.."])
    def test_whole_realm_spellings_collapse_to_empty_tuple(self, raw: str) -> None:
        """THE invariant: '.' is (), so no downstream code needs a `parent_all` flag."""
        if raw == "":
            with pytest.raises(ScopePathError):
                normalize_rel(raw)
            return
        assert normalize_rel(raw) == ()

    @pytest.mark.parametrize(
        "raw",
        [
            "/etc/passwd",  # absolute
            "/",
            "~/.ssh/id_rsa",  # home
            "~",
            "C:/Windows/System32",  # drive letter
            "c:\\Windows",
            "z:rel/path",
            "../outside",  # ..-escape
            "a/../../outside",
            "..",
            "",  # empty
            "   ",
            "\t",
        ],
    )
    def test_rejects(self, raw: str) -> None:
        with pytest.raises(ScopePathError):
            normalize_rel(raw)

    def test_scope_path_error_is_a_value_error(self) -> None:
        """The five call sites this replaces caught ValueError; they still work."""
        assert issubclass(ScopePathError, ValueError)

    def test_rejects_embedded_nul(self) -> None:
        with pytest.raises(ScopePathError):
            normalize_rel("src/a\x00.py")

    def test_accepts_a_component_sequence_round_trip(self) -> None:
        assert normalize_rel(("src", "a.py")) == ("src", "a.py")
        assert normalize_rel([]) == ()

    def test_a_deep_escape_that_dips_back_in_is_still_rejected(self) -> None:
        with pytest.raises(ScopePathError):
            normalize_rel("a/../../a/b")


class TestRelText:
    @pytest.mark.parametrize(
        ("components", "expected"),
        [((), "."), (("a",), "a"), (("src", "a.py"), "src/a.py")],
    )
    def test_renders(self, components: tuple[str, ...], expected: str) -> None:
        assert rel_text(components) == expected

    @pytest.mark.parametrize("raw", ["src/a.py", ".", "./a//b/", ".github/ci.yml"])
    def test_round_trips_through_normalize(self, raw: str) -> None:
        components = normalize_rel(raw)
        assert normalize_rel(rel_text(components)) == components


# ---------------------------------------------------------------------------
# safe_component (lifted verbatim from swarm.spawn)
# ---------------------------------------------------------------------------


class TestSafeComponent:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("run_abc123", "run_abc123"),
            ("task-1.2", "task-1.2"),
            ("weird name!", "weird_name_"),
            ("  padded  ", "padded"),
        ],
    )
    def test_sanitizes(self, raw: str, expected: str) -> None:
        assert safe_component(raw) == expected

    @pytest.mark.parametrize("raw", ["..", ".", "a/b", "../escape", "/abs", "", "   ", "a/"])
    def test_rejects(self, raw: str) -> None:
        with pytest.raises(ValueError):
            safe_component(raw)

    def test_matches_the_swarm_spawn_re_export(self) -> None:
        from omniagentos.swarm.spawn import _safe_component

        assert _safe_component is safe_component


# ---------------------------------------------------------------------------
# under / overlap  (the scheduler bug lives here)
# ---------------------------------------------------------------------------


class TestUnderAndOverlap:
    @pytest.mark.parametrize(
        ("child", "parent", "expected"),
        [
            ((), (), True),  # whole realm is an ancestor of ITSELF
            (("src", "a"), (), True),  # ...and of everything
            ((), ("src",), False),  # but nothing is under a real path except itself
            (("src", "a"), ("src",), True),
            (("src",), ("src",), True),
            (("src",), ("src", "a"), False),
            (("github", "x"), (".github",), False),  # COMPONENT-wise, not str prefix
            ((".github", "x"), (".github",), True),
            (("srcx", "a"), ("src",), False),  # "src/" string prefix trap
        ],
    )
    def test_under(self, child: tuple[str, ...], parent: tuple[str, ...], expected: bool) -> None:
        assert under(child, parent) is expected

    def test_the_scheduler_bug_whole_realm_overlaps_everything(self) -> None:
        """SwarmScheduler._paths_overlap('.', 'src/a') returns False (verified at
        HEAD); the algebra here must return True with no special case."""
        assert overlap((), ("src", "a")) is True
        assert overlap(("src", "a"), ()) is True
        assert overlap((), ()) is True

    def test_upstream_scheduler_still_has_the_bug_and_this_module_does_not(self) -> None:
        """Pins the defect so the extraction is a behavior change someone CHOSE.

        Delete the ``legacy`` assertion when a later WP ports the scheduler onto
        ``scope.overlap`` — a failure here then means "the bug is fixed
        upstream", which is the outcome this pin exists to announce.
        """
        from omniagentos.swarm.scheduler import SwarmScheduler

        legacy = SwarmScheduler._paths_overlap(".", "src/a")
        assert legacy is False, "upstream bug fixed -- retire this pin"
        assert overlap(".", "src/a") is True

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            (("src", "a"), ("src", "a"), True),
            (("src", "a"), ("src", "b"), False),
            (("src",), ("src", "a"), True),
            (("src", "a"), ("src",), True),
            (("src",), ("tests",), False),
            (("srcx",), ("src",), False),
            ((".github",), ("github",), False),
        ],
    )
    def test_overlap_is_symmetric(
        self, a: tuple[str, ...], b: tuple[str, ...], expected: bool
    ) -> None:
        assert overlap(a, b) is expected
        assert overlap(b, a) is expected

    def test_accepts_raw_strings_too(self) -> None:
        assert overlap(".", "src/a") is True
        assert under("src/a", ".") is True
        assert under("github/x", ".github") is False

    def test_a_string_is_not_decomposed_into_characters(self) -> None:
        """str is a Sequence[str]; the coercion must check str FIRST."""
        assert normalize_rel("abc") == ("abc",)
        assert under("abc", "a") is False


# ---------------------------------------------------------------------------
# realm_of -- ORDER MATTERS
# ---------------------------------------------------------------------------


class TestRealmOfPrivateWorkspaces:
    def test_two_run_workspaces_in_this_repo_are_distinct_realms(self) -> None:
        """THE catastrophic case. var/runs/<id> is inside the OmniAgentOS git
        repo; if git toplevel were resolved first, every concurrent run's private
        workspace would fold into the OmniAgentOS realm and all runs would
        mutually conflict."""
        repo = sp._repo_root()
        a = realm_of(os.path.join(repo, "var", "runs", "run_aaa"))
        b = realm_of(os.path.join(repo, "var", "runs", "run_bbb"))
        repo_realm = realm_of(repo)

        assert a is not None and b is not None and repo_realm is not None
        assert a != b
        assert a != repo_realm
        assert b != repo_realm

    def test_everything_inside_one_run_workspace_shares_that_realm(self) -> None:
        repo = sp._repo_root()
        base = os.path.join(repo, "var", "runs", "run_aaa")
        assert realm_of(base) == realm_of(os.path.join(base, "deep", "nested", "file.py"))

    def test_intake_workspace_children_are_distinct_realms(self) -> None:
        repo = sp._repo_root()
        a = realm_of(os.path.join(repo, "var", "intake-workspace", "task_1"))
        b = realm_of(os.path.join(repo, "var", "intake-workspace", "task_2"))
        assert a is not None and a != b
        assert a != realm_of(repo)

    def test_worktrees_are_keyed_by_run_AND_task(self) -> None:
        """var/swarm/worktrees/<run_id>/<task_key> -- two components deep, so two
        tasks of the SAME run get separate realms (that is what a worktree is)."""
        repo = sp._repo_root()
        base = os.path.join(repo, "var", "swarm", "worktrees", "run_x")
        a = realm_of(os.path.join(base, "taskA"))
        b = realm_of(os.path.join(base, "taskB"))
        assert a is not None and b is not None and a != b
        assert realm_of(os.path.join(base, "taskA", "src", "x.py")) == a

    def test_a_sibling_of_a_private_base_is_not_swallowed(self) -> None:
        """Component-wise matching: var/runs-archive is NOT under var/runs."""
        repo = sp._repo_root()
        assert realm_of(os.path.join(repo, "var", "runs-archive", "x")) == realm_of(repo)

    def test_workspace_dir_env_override_is_honored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = tmp_path / "elsewhere" / "runs"
        (base / "run_a").mkdir(parents=True)
        (base / "run_b").mkdir(parents=True)
        monkeypatch.setenv("OMNIAGENTOS_WORKSPACE_DIR", str(base))
        assert realm_of(str(base / "run_a")) != realm_of(str(base / "run_b"))

    def test_registered_private_base_shields_a_custom_lane_root(self, tmp_path: Path) -> None:
        """A lane configured with a non-default var root declares it; without the
        declaration its worktrees would fold into the enclosing repo realm."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        custom = repo / "custom-var" / "worktrees"
        (custom / "run_x" / "taskA").mkdir(parents=True)
        (custom / "run_x" / "taskB").mkdir(parents=True)

        assert realm_of(str(custom / "run_x" / "taskA")) == realm_of(str(repo))

        register_private_base(str(custom), depth=2)
        clear_realm_cache()
        a = realm_of(str(custom / "run_x" / "taskA"))
        b = realm_of(str(custom / "run_x" / "taskB"))
        assert a is not None and b is not None and a != b
        assert a != realm_of(str(repo))

    def test_private_bases_are_folded_realpaths(self) -> None:
        for base, depth in private_workspace_bases():
            assert os.path.isabs(base)
            assert base == sp._casefold_path(base)
            assert depth >= 1


class TestRealmOfGitToplevel:
    def test_subproject_inside_a_parent_repo_shares_the_parent_realm(self, tmp_path: Path) -> None:
        """Migration 031 lets a subproject's root live inside its parent's root.
        Two realm strings for one physical tree is a MISSED conflict; git toplevel
        collapses them."""
        parent = tmp_path / "parent"
        parent.mkdir()
        _git_init(parent)
        child = parent / "packages" / "child"
        child.mkdir(parents=True)

        assert realm_of(str(child)) == realm_of(str(parent))

    def test_a_nested_repo_is_its_own_realm(self, tmp_path: Path) -> None:
        parent = tmp_path / "parent"
        parent.mkdir()
        _git_init(parent)
        nested = parent / "vendor" / "lib"
        nested.mkdir(parents=True)
        _git_init(nested)
        clear_realm_cache()

        assert realm_of(str(nested)) != realm_of(str(parent))

    def test_a_file_inside_a_repo_resolves_to_the_repo_realm(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        target = repo / "src" / "a.py"
        target.parent.mkdir()
        target.write_text("x", encoding="utf-8")

        assert realm_of(str(target)) == realm_of(str(repo))

    def test_a_not_yet_created_path_inside_a_repo_still_resolves(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        assert realm_of(str(repo / "does" / "not" / "exist")) == realm_of(str(repo))


class TestRealmOfFallbacks:
    def test_a_plain_directory_outside_any_repo_is_its_own_realm(self, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        realm = realm_of(str(plain))
        assert realm == sp._casefold_path(os.path.realpath(str(plain)))

    def test_symlinked_roots_fold_to_one_realm(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        assert realm_of(str(link)) == realm_of(str(real))

    @pytest.mark.skipif(not DARWIN, reason="case-fold only applies on macOS")
    def test_case_variants_fold_to_one_realm_on_macos(self, tmp_path: Path) -> None:
        plain = tmp_path / "MixedCase"
        plain.mkdir()
        assert realm_of(str(plain)) == realm_of(str(tmp_path / "mixedcase"))

    @pytest.mark.parametrize("raw", ["", "   ", "''"])
    def test_unusable_input_returns_none(self, raw: str) -> None:
        assert realm_of(raw) is None

    def test_memoization_does_not_shell_out_twice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        clear_realm_cache()
        realm_of(str(repo))  # warm

        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("git rev-parse was not memoized")

        monkeypatch.setattr(subprocess, "run", _boom)
        assert realm_of(str(repo)) is not None


# ---------------------------------------------------------------------------
# resolve_into_realm
# ---------------------------------------------------------------------------


class TestResolveIntoRealm:
    def test_migration_old_vs_new_on_legacy_matrix(self, tmp_path: Path) -> None:
        root = tmp_path / "realm"
        inside = root / "src"
        outside = tmp_path / "outside"
        inside.mkdir(parents=True)
        outside.mkdir()
        root_alias = tmp_path / "realm-alias"
        root_alias.symlink_to(root, target_is_directory=True)
        escape = root / "escape"
        escape.symlink_to(outside, target_is_directory=True)

        cases = (
            (inside, root),
            (inside / "missing.py", root),
            (inside, root_alias),
            (escape / "secret", root),
            (outside, root),
        )
        for candidate, realm in cases:
            realm_real = os.path.realpath(realm)
            resolved = os.path.realpath(candidate)
            folded_root = sp._casefold_path(realm_real)
            folded_candidate = sp._casefold_path(resolved)
            if os.path.commonpath([folded_root, folded_candidate]) == folded_root:
                old_parts: tuple[str, ...] | None = tuple(
                    Path(resolved).parts[len(Path(realm_real).parts) :]
                )
            else:
                old_parts = None
            assert resolve_into_realm(str(candidate), str(realm)) == old_parts

    def test_absolute_child_resolves_to_components(self, tmp_path: Path) -> None:
        root = tmp_path / "realm"
        (root / "src").mkdir(parents=True)
        (root / "src" / "a.py").write_text("x", encoding="utf-8")

        assert resolve_into_realm(str(root / "src" / "a.py"), str(root)) == ("src", "a.py")

    def test_the_realm_root_itself_is_the_empty_tuple(self, tmp_path: Path) -> None:
        root = tmp_path / "realm"
        root.mkdir()
        assert resolve_into_realm(str(root), str(root)) == ()

    def test_relative_candidate_resolves_against_the_realm(self, tmp_path: Path) -> None:
        root = tmp_path / "realm"
        (root / "src").mkdir(parents=True)
        assert resolve_into_realm("src/a.py", str(root)) == ("src", "a.py")

    def test_dot_dot_escape_fails_closed(self, tmp_path: Path) -> None:
        root = tmp_path / "realm"
        root.mkdir()
        (tmp_path / "outside").mkdir()
        assert resolve_into_realm("../outside/secret", str(root)) is None
        assert resolve_into_realm(str(tmp_path / "outside"), str(root)) is None

    def test_symlink_escape_fails_closed(self, tmp_path: Path) -> None:
        root = tmp_path / "realm"
        root.mkdir()
        secret = tmp_path / "outside"
        secret.mkdir()
        (root / "escape").symlink_to(secret, target_is_directory=True)

        assert resolve_into_realm(str(root / "escape" / "x"), str(root)) is None

    def test_home_path_is_not_silently_joined(self, tmp_path: Path) -> None:
        root = tmp_path / "realm"
        root.mkdir()
        assert resolve_into_realm("~/.ssh/id_rsa", str(root)) is None

    def test_symlinked_realm_spelling_still_resolves(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        (real / "src").mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        assert resolve_into_realm(str(link / "src"), str(real)) == ("src",)
        assert resolve_into_realm(str(real / "src"), str(link)) == ("src",)

    @pytest.mark.skipif(not DARWIN, reason="case-fold only applies on macOS")
    def test_case_variant_realm_still_contains_the_candidate(self, tmp_path: Path) -> None:
        root = tmp_path / "Realm"
        (root / "Src").mkdir(parents=True)
        folded_root = str(tmp_path / "realm")

        assert resolve_into_realm(str(root / "Src"), folded_root) == ("Src",)

    @pytest.mark.skipif(not DARWIN, reason="macOS firmlink spelling")
    def test_migration_unifies_home_firmlink_inode_spelling(self) -> None:
        home = Path(os.path.realpath(os.path.expanduser("~")))
        firmlink_home = Path(f"/System/Volumes/Data{home}")
        if not firmlink_home.exists() or not os.path.samestat(
            os.stat(home), os.stat(firmlink_home)
        ):
            pytest.skip("macOS /System/Volumes/Data home firmlink is unavailable")

        assert resolve_into_realm(
            str(firmlink_home / "Library" / "Messages"),
            str(home),
        ) == ("Library", "Messages")

    def test_case_is_preserved_in_the_returned_components(self, tmp_path: Path) -> None:
        root = tmp_path / "realm"
        (root / "MixedCase").mkdir(parents=True)
        assert resolve_into_realm(str(root / "MixedCase"), str(root)) == ("MixedCase",)

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_candidate_fails_closed(self, raw: str, tmp_path: Path) -> None:
        assert resolve_into_realm(raw, str(tmp_path)) is None

    def test_blank_realm_fails_closed(self, tmp_path: Path) -> None:
        assert resolve_into_realm(str(tmp_path / "x"), "") is None

    def test_a_sibling_with_a_shared_string_prefix_is_outside(self, tmp_path: Path) -> None:
        root = tmp_path / "realm"
        root.mkdir()
        (tmp_path / "realmx").mkdir()
        assert resolve_into_realm(str(tmp_path / "realmx" / "a"), str(root)) is None


class TestLayering:
    def test_scope_never_imports_upward(self) -> None:
        """scope/ sits UNDER swarm/runner/db; an upward import would reintroduce
        the cycle this package was extracted to break."""
        package = Path(sp.__file__).parent
        for module in sorted(package.glob("*.py")):
            source = module.read_text(encoding="utf-8")
            for banned in ("omniagentos.swarm", "omniagentos.runner", "omniagentos.db"):
                offenders = [
                    line
                    for line in source.splitlines()
                    if banned in line and line.lstrip().startswith(("import ", "from "))
                ]
                assert not offenders, f"{module.name} imports {banned}: {offenders}"
