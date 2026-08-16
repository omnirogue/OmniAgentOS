"""Self-tests for the counterfeit runner itself (W4-10 meta-guards).

1. A patch that does not apply is a hard error — never a skip or a green catch.
2. A deliberate surviving counterfeit (no real coverage) makes the gate red.
3. Collection errors are not masqueraded as catches.
4. ``corpus.d/`` fragments are discovered, ordered and runnable — and a
   duplicate id or a malformed fragment is a hard error, never a silent
   last-wins. Fragments exist so concurrent lanes stop conflicting on
   ``corpus.toml``; a loader that swallowed a collision would trade a visible
   textual conflict for an invisible lost counterfeit.
"""

from __future__ import annotations

import re
import shutil
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.counterfeits.harness import (
    CORPUS_DIR,
    REPO_ROOT,
    CounterfeitApplyError,
    CounterfeitCollectionError,
    CounterfeitDuplicateIdError,
    CounterfeitEntry,
    CounterfeitError,
    CounterfeitPatchEscapeError,
    CounterfeitSurvived,
    CounterfeitWrongFailure,
    assert_entry_caught,
    discover_fragments,
    evaluate_mutated,
    load_corpus,
    load_manifest,
    normalise_id,
    run_entry,
    run_pytest_nodes,
)

pytestmark = pytest.mark.counterfeit_gate

SELFTEST_MANIFEST = CORPUS_DIR / "selftest" / "broken.toml"
SURVIVOR_PATCH = CORPUS_DIR / "patches" / "deliberate-survivor-no-coverage.patch"


def test_unapplicable_patch_is_hard_error_not_skip() -> None:
    entries = load_manifest(SELFTEST_MANIFEST)
    assert len(entries) == 1
    result = run_entry(entries[0])
    assert result.status == "apply_error", result
    with pytest.raises(CounterfeitApplyError):
        assert_entry_caught(result)


def test_surviving_counterfeit_fails_the_gate_loudly() -> None:
    """Proof: an area with no real coverage cannot be reported as a pass.

    The deliberate-survivor patch only adds a comment marker that no test
    asserts on. Pointing must_fail at an unrelated green test yields SURVIVED,
    and assert_entry_caught must raise CounterfeitSurvived (gate red).
    """
    assert SURVIVOR_PATCH.is_file(), SURVIVOR_PATCH
    entry = CounterfeitEntry(
        id="deliberate-survivor-no-coverage",
        patch="patches/deliberate-survivor-no-coverage.patch",
        rationale=(
            "Comment-only mutation with no binding assertion — proves the gate "
            "goes red when a counterfeit survives."
        ),
        must_fail=("tests/swarm/test_summary.py::TestComputeMetricsGolden::test_golden_numbers",),
        failure_re=r"COUNTERFEIT_SURVIVOR|this pattern will never match xyzzy991",
        source=CORPUS_DIR / "corpus.toml",
    )
    result = run_entry(entry)
    assert result.status == "survived", (
        f"expected SURVIVED for uncovered counterfeit, got {result.status}: {result.detail}\n"
        f"{result.output_excerpt}"
    )
    with pytest.raises(CounterfeitSurvived, match="SURVIVED|decoration"):
        assert_entry_caught(result)


def test_collection_error_is_not_a_catch() -> None:
    """A missing node id must not be reported as 'caught' just because rc != 0."""
    entry = CounterfeitEntry(
        id="collection-miss",
        patch="patches/deliberate-survivor-no-coverage.patch",
        rationale="meta: collection failure must not count as a catch",
        must_fail=("tests/counterfeits/no_such_module.py::test_does_not_exist_abc123",),
        failure_re=r"anything",
        source=CORPUS_DIR / "corpus.toml",
    )
    result = run_entry(entry)
    assert result.status in {"collection_error", "apply_error", "survived", "wrong_failure"}
    # Apply still works (comment patch); collection must fail.
    if result.status != "apply_error":
        assert result.status == "collection_error", result
        with pytest.raises(CounterfeitCollectionError):
            assert_entry_caught(result)


def test_evaluate_mutated_rejects_unrelated_failure_without_failure_re() -> None:
    """rc != 0 without matching failure_re is wrong_failure, not caught."""
    # Real green test run, then fabricate a failed proc-like object via a
    # deliberate bad assertion path: run a node that fails for a known reason
    # and require a non-matching failure_re.
    from tests.counterfeits.harness import (
        apply_patch,
        destroy_scratch,
        materialise_scratch,
    )

    scratch = materialise_scratch(REPO_ROOT)
    try:
        apply_patch(scratch, CORPUS_DIR / "patches" / "path-startswith-containment.patch")
        proc = run_pytest_nodes(
            tree=scratch,
            nodes=[
                "tests/api/test_board_files.py::test_workspace_floor_refuses_string_prefix_without_path_ancestry",
            ],
            repo_root=REPO_ROOT,
            timeout=60,
        )
        entry = CounterfeitEntry(
            id="wrong-reason",
            patch="patches/path-startswith-containment.patch",
            rationale="meta",
            must_fail=(
                "tests/api/test_board_files.py::test_workspace_floor_refuses_string_prefix_without_path_ancestry",
            ),
            failure_re=r"this-string-will-not-appear-in-the-output-zz99",
            source=CORPUS_DIR / "corpus.toml",
        )
        result = evaluate_mutated(entry, proc)
        assert result.status == "wrong_failure", result
        with pytest.raises(CounterfeitWrongFailure):
            assert_entry_caught(result)
    finally:
        destroy_scratch(scratch)


# --- corpus.d/ fragment loader meta-guards ---------------------------------
#
# Concurrent lanes appending to one corpus.toml produced a textual conflict on
# every multi-lane rebase. Fragments remove the conflict; these tests prove the
# removal did not also remove the corpus's safety properties.

_BASE_MANIFEST = """\
[[counterfeit]]
id = "base-only"
patch = "patches/stand-in.patch"
rationale = "base manifest entry"
must_fail = ["tests/does_not_matter.py::test_placeholder"]
failure_re = "boom"
"""


def _fragment_toml(entry_id: str, *, patch: str = "patches/stand-in.patch") -> str:
    return (
        "[[counterfeit]]\n"
        f'id = "{entry_id}"\n'
        f'patch = "{patch}"\n'
        'rationale = "fragment entry"\n'
        'must_fail = ["tests/does_not_matter.py::test_placeholder"]\n'
        'failure_re = "boom"\n'
    )


def _corpus_root(tmp_path: Path) -> Path:
    """A minimal stand-in corpus: manifest + a patch file the loader can stat."""
    root = tmp_path / "counterfeits"
    (root / "patches").mkdir(parents=True)
    (root / "patches" / "stand-in.patch").write_text("(never applied by these tests)\n")
    (root / "corpus.toml").write_text(_BASE_MANIFEST)
    return root / "corpus.toml"


def test_absent_or_empty_fragment_dir_is_todays_behaviour_exactly(tmp_path: Path) -> None:
    """No fragments must mean no change at all — the back-compat guarantee."""
    manifest = _corpus_root(tmp_path)
    baseline = [entry.id for entry in load_manifest(manifest)]

    assert not (manifest.parent / "corpus.d").exists()
    assert discover_fragments(manifest.parent / "corpus.d") == []
    assert [entry.id for entry in load_corpus(manifest)] == baseline

    (manifest.parent / "corpus.d").mkdir()
    assert [entry.id for entry in load_corpus(manifest)] == baseline


def test_fragments_are_discovered_in_filename_order(tmp_path: Path) -> None:
    manifest = _corpus_root(tmp_path)
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    # Written out of order on purpose: ordering must come from the sort, not
    # from directory iteration order (which is filesystem-dependent).
    (fragments / "zeta-lane.toml").write_text(_fragment_toml("cf-zeta"))
    (fragments / "alpha-lane.toml").write_text(_fragment_toml("cf-alpha"))
    (fragments / "mid-lane.toml").write_text(_fragment_toml("cf-mid"))

    assert [p.name for p in discover_fragments(fragments)] == [
        "alpha-lane.toml",
        "mid-lane.toml",
        "zeta-lane.toml",
    ]
    assert [entry.id for entry in load_corpus(manifest)] == [
        "base-only",
        "cf-alpha",
        "cf-mid",
        "cf-zeta",
    ]


def test_non_toml_files_in_fragment_dir_are_ignored(tmp_path: Path) -> None:
    """The directory has to be able to exist in git without carrying a manifest."""
    manifest = _corpus_root(tmp_path)
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / ".gitkeep").write_text("")
    (fragments / "README.md").write_text("# how to add a fragment\n")
    (fragments / "notes.txt").write_text("not a manifest")
    (fragments / "my-lane.toml").write_text(_fragment_toml("cf-mine"))

    assert [p.name for p in discover_fragments(fragments)] == ["my-lane.toml"]
    assert [entry.id for entry in load_corpus(manifest)] == ["base-only", "cf-mine"]


def test_duplicate_id_between_manifest_and_fragment_is_hard_error(tmp_path: Path) -> None:
    """Silent last-wins would let one lane delete another lane's counterfeit."""
    manifest = _corpus_root(tmp_path)
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / "colliding-lane.toml").write_text(_fragment_toml("base-only"))

    with pytest.raises(CounterfeitDuplicateIdError) as excinfo:
        load_corpus(manifest)
    message = str(excinfo.value)
    assert "base-only" in message
    # Both sides must be named or the operator cannot resolve the collision.
    assert "corpus.toml" in message
    assert "colliding-lane.toml" in message


def test_duplicate_id_between_two_fragments_is_hard_error(tmp_path: Path) -> None:
    manifest = _corpus_root(tmp_path)
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / "lane-a.toml").write_text(_fragment_toml("cf-contested"))
    (fragments / "lane-b.toml").write_text(_fragment_toml("cf-contested"))

    with pytest.raises(CounterfeitDuplicateIdError) as excinfo:
        load_corpus(manifest)
    message = str(excinfo.value)
    assert "lane-a.toml" in message and "lane-b.toml" in message


def test_duplicate_id_within_one_fragment_is_hard_error(tmp_path: Path) -> None:
    manifest = _corpus_root(tmp_path)
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / "lane-a.toml").write_text(
        _fragment_toml("cf-twice") + "\n" + _fragment_toml("cf-twice")
    )

    with pytest.raises(CounterfeitDuplicateIdError, match="cf-twice"):
        load_corpus(manifest)


def test_malformed_fragment_is_hard_error_not_silently_skipped(tmp_path: Path) -> None:
    """Same seriousness the runner gives a patch that fails to apply."""
    manifest = _corpus_root(tmp_path)
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / "broken-lane.toml").write_text('[[counterfeit]\nid = "oops"\n')

    with pytest.raises(CounterfeitError, match="unreadable manifest"):
        load_corpus(manifest)


def test_fragment_missing_required_field_is_hard_error(tmp_path: Path) -> None:
    manifest = _corpus_root(tmp_path)
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / "partial-lane.toml").write_text('[[counterfeit]]\nid = "no-patch"\n')

    # The offending fragment must be named — with N lanes contributing, "some
    # entry is malformed" is not actionable.
    with pytest.raises(CounterfeitError, match=r"partial-lane\.toml: counterfeit\[0\] missing"):
        load_corpus(manifest)


def test_fragment_with_no_counterfeit_tables_is_hard_error(tmp_path: Path) -> None:
    manifest = _corpus_root(tmp_path)
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / "empty-lane.toml").write_text("# a lane stub that never got filled in\n")

    with pytest.raises(CounterfeitError, match="no \\[\\[counterfeit\\]\\] tables"):
        load_corpus(manifest)


def test_fragment_missing_patch_file_is_hard_error(tmp_path: Path) -> None:
    manifest = _corpus_root(tmp_path)
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / "lane-a.toml").write_text(_fragment_toml("cf-x", patch="patches/absent.patch"))

    with pytest.raises(CounterfeitError, match="patch missing"):
        load_corpus(manifest)


def test_fragment_patch_paths_resolve_against_the_corpus_root(tmp_path: Path) -> None:
    """`patches/foo.patch` must mean the same in a fragment and in corpus.toml."""
    manifest = _corpus_root(tmp_path)
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / "lane-a.toml").write_text(_fragment_toml("cf-x"))

    entry = next(e for e in load_corpus(manifest) if e.id == "cf-x")
    assert entry.source == fragments / "lane-a.toml"
    assert entry.patch_path == (manifest.parent / "patches" / "stand-in.patch").resolve()


def test_shipped_fragment_dir_exists_and_the_live_corpus_still_loads() -> None:
    """corpus.d/ is in git, and the real corpus is a superset of corpus.toml."""
    from tests.counterfeits.harness import DEFAULT_FRAGMENT_DIR, DEFAULT_MANIFEST

    assert DEFAULT_FRAGMENT_DIR.is_dir(), DEFAULT_FRAGMENT_DIR
    base_ids = [entry.id for entry in load_manifest(DEFAULT_MANIFEST)]
    corpus_ids = [entry.id for entry in load_corpus()]
    assert corpus_ids[: len(base_ids)] == base_ids
    assert len(corpus_ids) == len(set(corpus_ids))


def test_fragment_entry_is_runnable_end_to_end_and_is_caught(tmp_path: Path) -> None:
    """A fragment-sourced entry goes through apply → must_fail RED unchanged."""
    root = tmp_path / "counterfeits"
    (root / "patches").mkdir(parents=True)
    shutil.copy(
        CORPUS_DIR / "patches" / "path-startswith-containment.patch",
        root / "patches" / "path-startswith-containment.patch",
    )
    (root / "corpus.toml").write_text(_BASE_MANIFEST.replace("stand-in.patch", "real.patch"))
    (root / "patches" / "real.patch").write_text("(never applied)\n")
    fragments = root / "corpus.d"
    fragments.mkdir()
    (fragments / "lane-under-test.toml").write_text(
        "[[counterfeit]]\n"
        'id = "cf-fragment-sourced"\n'
        'patch = "patches/path-startswith-containment.patch"\n'
        'rationale = "same counterfeit as corpus.toml, reached through a fragment"\n'
        "must_fail = [\n"
        '  "tests/api/test_path_containment.py::test_string_prefix_counterfeit_fails_loudly",\n'
        "]\n"
        'failure_re = "string-prefix containment counterfeit|assert \\\\(\\\\) is None"\n'
    )

    entry = next(e for e in load_corpus(root / "corpus.toml") if e.id == "cf-fragment-sourced")
    result = run_entry(entry, repo_root=REPO_ROOT)
    assert result.status == "caught", f"{result.status}: {result.detail}\n{result.output_excerpt}"
    assert_entry_caught(result)


# --- REWORK: duplicate detection must be at least as loose as human error ---
#
# Sol review BLOCKER 1: `id=str(req("id"))` with exact-string comparison let
# ['base','Case','case'] and ["'id'","'id '"] all coexist. A duplicate check that
# only catches byte-exact repeats is decoration — the ids two racing lanes
# actually collide on differ by case or a stray space.


def _entry_toml(entry_id: str, *, patch: str = "patches/stand-in.patch") -> str:
    return (
        "[[counterfeit]]\n"
        f"id = {entry_id!r}\n"
        f'patch = "{patch}"\n'
        'rationale = "r"\n'
        'must_fail = ["tests/does_not_matter.py::test_placeholder"]\n'
        'failure_re = "boom"\n'
    )


@pytest.mark.parametrize(
    ("base_id", "fragment_id"),
    [
        ("base-only", "Base-Only"),  # case
        ("base-only", "BASE-ONLY"),
        ("base-only", "base-only "),  # trailing space
        ("base-only", " base-only"),  # leading space
    ],
)
def test_duplicate_id_across_files_is_caught_case_and_space_insensitively(
    tmp_path: Path, base_id: str, fragment_id: str
) -> None:
    manifest = _corpus_root(tmp_path)
    manifest.write_text(_entry_toml(base_id))
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / "other-lane.toml").write_text(_entry_toml(fragment_id))

    with pytest.raises(CounterfeitDuplicateIdError) as excinfo:
        load_corpus(manifest)
    message = str(excinfo.value)
    # BOTH files and BOTH original spellings, or the operator cannot fix it.
    assert "corpus.toml" in message and "other-lane.toml" in message
    assert repr(base_id) in message and repr(fragment_id) in message
    # ...and the table indices, same as the within-file message: "somewhere in
    # that file" makes a reader grep, which is the expensive part with N lanes.
    assert message.count("counterfeit[0]") == 2


@pytest.mark.parametrize(
    ("first", "second"),
    [("case", "Case"), ("cf-y", "cf-y "), ("cf-y", " cf-y")],
)
def test_duplicate_id_within_one_file_names_both_table_locations(
    tmp_path: Path, first: str, second: str
) -> None:
    manifest = _corpus_root(tmp_path)
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / "one-lane.toml").write_text(_entry_toml(first) + _entry_toml(second))

    with pytest.raises(CounterfeitDuplicateIdError) as excinfo:
        load_corpus(manifest)
    message = str(excinfo.value)
    assert "counterfeit[0]" in message and "counterfeit[1]" in message
    assert repr(first) in message and repr(second) in message
    assert "one-lane.toml" in message


def test_normalise_id_collapses_case_and_whitespace() -> None:
    assert normalise_id("cf-Foo") == normalise_id(" cf-foo ") == "cf-foo"
    # Sol's probe B: ids `id` and `id ` differ only by a trailing space.
    assert normalise_id("id") == normalise_id("id ") == "id"
    assert normalise_id("a  b") == normalise_id("a\tb") == "a b"
    # Distinct ids must stay distinct — normalisation must not over-merge.
    assert normalise_id("cf-a") != normalise_id("cf-b")
    assert normalise_id("cf-ab") != normalise_id("cf-a b")


def test_blank_id_is_rejected(tmp_path: Path) -> None:
    manifest = _corpus_root(tmp_path)
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / "blank-lane.toml").write_text(_entry_toml("   "))

    with pytest.raises(CounterfeitError, match="id is blank"):
        load_corpus(manifest)


def test_original_spelling_is_preserved_for_display() -> None:
    """Normalisation is for COLLISION detection only; the report shows the real id."""
    entry = CounterfeitEntry(
        id="CF-Mixed-Case",
        patch="patches/deliberate-survivor-no-coverage.patch",
        rationale="r",
        must_fail=("t::t",),
        failure_re="b",
        source=CORPUS_DIR / "corpus.toml",
    )
    assert entry.id == "CF-Mixed-Case"
    assert entry.normalised_id == "cf-mixed-case"


# --- REWORK: a manifest's `patch` is a filesystem read, so it is a
# containment surface (Sol review MAJOR 2). Probes accepted
# ../../pyproject.toml and /etc/passwd before this.


@pytest.mark.parametrize(
    ("label", "patch"),
    [
        ("traversal", "../../pyproject.toml"),
        ("traversal-into-repo", "../../../omniagentos/__init__.py"),
        ("absolute", "/etc/passwd"),
        ("blank", "   "),
        ("empty", ""),
    ],
)
def test_patch_outside_the_corpus_tree_is_refused(tmp_path: Path, label: str, patch: str) -> None:
    manifest = _corpus_root(tmp_path)
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / "escaping-lane.toml").write_text(_entry_toml(f"cf-{label}", patch=patch))

    with pytest.raises(CounterfeitPatchEscapeError):
        load_corpus(manifest)


def test_sibling_directory_with_the_root_as_a_string_prefix_is_refused(
    tmp_path: Path,
) -> None:
    """`<root>-evil` passes str.startswith and must still be refused."""
    manifest = _corpus_root(tmp_path)
    evil = Path(str(manifest.parent) + "-evil")
    evil.mkdir()
    (evil / "planted.patch").write_text("(outside the corpus)\n")
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / "sibling-lane.toml").write_text(
        _entry_toml("cf-sibling", patch=f"../{evil.name}/planted.patch")
    )

    # Sanity: the string-prefix test a naive implementation would use says yes.
    assert str(evil).startswith(str(manifest.parent))
    with pytest.raises(CounterfeitPatchEscapeError, match="outside the corpus tree"):
        load_corpus(manifest)


def test_patch_inside_the_corpus_tree_is_accepted(tmp_path: Path) -> None:
    """The containment check must not reject the legitimate case."""
    manifest = _corpus_root(tmp_path)
    (manifest.parent / "nested").mkdir()
    (manifest.parent / "nested" / "deep.patch").write_text("x\n")
    fragments = manifest.parent / "corpus.d"
    fragments.mkdir()
    (fragments / "ok-lane.toml").write_text(_entry_toml("cf-ok", patch="nested/deep.patch"))

    ids = [entry.id for entry in load_corpus(manifest)]
    assert ids == ["base-only", "cf-ok"]


def test_the_live_corpus_patches_are_all_contained() -> None:
    """Regression floor: every shipped entry passes the new containment check."""
    from tests.counterfeits.harness import DEFAULT_MANIFEST

    for entry in load_corpus():
        assert entry.patch_path.is_file(), entry.id
        assert entry.patch_root == (DEFAULT_MANIFEST.parent).resolve()


# --- pool / timeout / isolation meta-guards (cf-pool-0806 fix pass) ---------
#
# Sol review: uncapped linear timeout scaling broke verdict determinism;
# BrokenProcessPool was swallowed by a broad except; TMPDIR root was never
# created. These stay pure/fast — no full corpus run.


def test_effective_entry_timeout_is_fixed_across_pool_widths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Correctness bound must not grow with pool width (verdict determinism).

    A hang that scores survived at pool=1 must not flip to caught at pool=8
    solely because the timeout window grew. The bound is fixed at base for any
    workers in [1, 1000] when the operator has not set the env explicitly.
    """
    from tests.counterfeits.harness import (
        ENTRY_TIMEOUT_DEFAULT_SECONDS,
        ENTRY_TIMEOUT_ENV,
        POOL_WORKERS_ENV,
        effective_entry_timeout,
    )

    monkeypatch.delenv(ENTRY_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(POOL_WORKERS_ENV, raising=False)
    base = ENTRY_TIMEOUT_DEFAULT_SECONDS
    for width in (1, 4, 8, 100, 1000):
        got = effective_entry_timeout(workers=width)
        assert got == base, f"workers={width}: expected fixed {base}, got {got}"
        assert base <= got <= 2 * base


def test_effective_entry_timeout_respects_explicit_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.counterfeits.harness import ENTRY_TIMEOUT_ENV, effective_entry_timeout

    monkeypatch.setenv(ENTRY_TIMEOUT_ENV, "77.5")
    assert effective_entry_timeout(workers=1) == 77.5
    assert effective_entry_timeout(workers=8) == 77.5
    assert effective_entry_timeout(workers=100) == 77.5


def test_pool_workers_default_is_serial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.counterfeits.harness import (
        POOL_WORKERS_DEFAULT,
        POOL_WORKERS_ENV,
        pool_workers_default,
    )

    assert POOL_WORKERS_DEFAULT == 1
    monkeypatch.delenv(POOL_WORKERS_ENV, raising=False)
    assert pool_workers_default() == 1


def test_format_report_records_pool_width_and_timeout() -> None:
    """Durable receipt evidence lives in the report body, not only startup print."""
    from tests.counterfeits.harness import EntryResult, format_report

    entry = CounterfeitEntry(
        id="cf-report-meta",
        patch="patches/stand-in.patch",
        rationale="r",
        must_fail=("tests/x.py::t",),
        failure_re="boom",
        source=CORPUS_DIR / "corpus.toml",
    )
    result = EntryResult(entry=entry, status="caught", detail="caught for test")
    report = format_report([result], workers=1, entry_timeout=120.0)
    # Minor (cross-lineage review round 2, gpt-5.6-sol, of #253): substring
    # membership alone accepts an unannounced sixth field appended to the same
    # line (e.g. "...  other=0  unannounced_field=1" still contains this exact
    # five-field substring). format_report emits the totals line as its own
    # complete line (see harness.format_report), so anchoring to the whole line
    # tightens this back to the exact-token contract without depending on
    # anything format_report doesn't already guarantee.
    totals_line = r"(?m)^total=1  caught=1  survived=0  skipped_platform=0  other=0$"
    assert re.search(totals_line, report), report
    assert "pool_workers=1  entry_timeout=120.0s" in report


def test_isolated_runtime_env_creates_tmp_and_var_roots(tmp_path: Path) -> None:
    """TMPDIR isolation is a no-op if the candidate dir does not exist on disk."""
    from tests.counterfeits.harness import _apply_isolated_runtime_env

    tree = tmp_path / "scratch"
    tree.mkdir()
    env: dict[str, str] = {}
    _apply_isolated_runtime_env(env, tree)
    tmp_root = tree / "tmp"
    var_root = tree / "var" / "counterfeit-entry"
    assert tmp_root.is_dir(), "tmp_root must exist before pytest sees TMPDIR"
    assert var_root.is_dir()
    assert (var_root / "ledger").is_dir()
    assert (var_root / "vault").is_dir()
    assert env["TMPDIR"] == str(tmp_root)
    assert env["OMNIAGENTOS_DB"] == str(var_root / "state.sqlite3")


def test_run_entries_worker_error_never_disappears(monkeypatch: pytest.MonkeyPatch) -> None:
    """A future that raises becomes worker_error; result count stays complete."""
    from tests.counterfeits.harness import run_entries

    entry = CounterfeitEntry(
        id="cf-dead-worker",
        patch="patches/stand-in.patch",
        rationale="r",
        must_fail=("tests/x.py::t",),
        failure_re="boom",
        source=CORPUS_DIR / "corpus.toml",
    )

    def _boom_future(*_a: object, **_k: object) -> Future[object]:
        fut: Future[object] = Future()
        fut.set_exception(RuntimeError("forced-worker-crash"))
        return fut

    mock_executor = MagicMock()
    mock_executor.__enter__.return_value = mock_executor
    mock_executor.__exit__.return_value = False
    mock_executor.submit.side_effect = _boom_future

    # Force the pooled path and the ThreadPool fallback (ProcessPool init fails).
    monkeypatch.setattr(
        "tests.counterfeits.harness.ProcessPoolExecutor",
        MagicMock(side_effect=PermissionError("semlock blocked")),
    )
    monkeypatch.setattr(
        "tests.counterfeits.harness.ThreadPoolExecutor",
        MagicMock(return_value=mock_executor),
    )
    # as_completed yields our forced futures
    monkeypatch.setattr(
        "tests.counterfeits.harness.as_completed",
        lambda futures: list(futures),
    )

    results = run_entries([entry, entry], entry_timeout=5.0, workers=2)
    assert len(results) == 2
    for r in results:
        assert r.status == "worker_error", r
        assert "forced-worker-crash" in r.detail or "RuntimeError" in r.detail
        assert not r.ok


def test_run_entries_broken_process_pool_raises_counterfeit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BrokenProcessPool must hard-refuse the whole run, not one worker_error."""
    from tests.counterfeits.harness import run_entries

    entry = CounterfeitEntry(
        id="cf-broken-pool",
        patch="patches/stand-in.patch",
        rationale="r",
        must_fail=("tests/x.py::t",),
        failure_re="boom",
        source=CORPUS_DIR / "corpus.toml",
    )
    dead = Future()
    dead.set_exception(BrokenProcessPool("forced-dead-worker"))

    mock_executor = MagicMock()
    mock_executor.__enter__.return_value = mock_executor
    mock_executor.__exit__.return_value = False
    mock_executor.submit.return_value = dead

    monkeypatch.setattr(
        "tests.counterfeits.harness.ProcessPoolExecutor",
        MagicMock(return_value=mock_executor),
    )
    monkeypatch.setattr(
        "tests.counterfeits.harness.as_completed",
        lambda futures: list(futures),
    )

    with pytest.raises(CounterfeitError, match="process pool broke"):
        run_entries([entry], entry_timeout=5.0, workers=2)


def test_run_entries_threadpool_fallback_preserves_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ProcessPool is unavailable, ThreadPool path still returns N results."""
    from tests.counterfeits.harness import EntryResult, run_entries

    entry_a = CounterfeitEntry(
        id="cf-a",
        patch="patches/stand-in.patch",
        rationale="r",
        must_fail=("tests/x.py::t",),
        failure_re="boom",
        source=CORPUS_DIR / "corpus.toml",
    )
    entry_b = CounterfeitEntry(
        id="cf-b",
        patch="patches/stand-in.patch",
        rationale="r",
        must_fail=("tests/x.py::t",),
        failure_re="boom",
        source=CORPUS_DIR / "corpus.toml",
    )

    def fake_job(entry: CounterfeitEntry, repo_root: str | None, timeout: float) -> EntryResult:
        return EntryResult(entry=entry, status="caught", detail=f"fake ok t={timeout}")

    monkeypatch.setattr(
        "tests.counterfeits.harness.ProcessPoolExecutor",
        MagicMock(side_effect=PermissionError("semlock blocked")),
    )
    # Real ThreadPoolExecutor — exercises the fallback path without needing SemLock.
    monkeypatch.setattr("tests.counterfeits.harness._run_entry_job", fake_job)

    results = run_entries([entry_a, entry_b], entry_timeout=5.0, workers=2)
    assert len(results) == 2
    assert [r.entry.id for r in results] == ["cf-a", "cf-b"]
    assert all(r.status == "caught" for r in results)
