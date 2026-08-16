"""Coverage contract for the Phase-1 unified plan reconciliation artifact.

The Phase-1 plan of record (``devtasks/plans/phase1-unified-plan.json``) is
structurally statusless: it lists intended tasks but has no field that can say
which of them shipped. Every row therefore renders identically whether the work
landed, landed under a different carrier, or was never started.

This module pins the reconciliation artifact that supplies the missing status
field. The item IDs are DERIVED from the source plan -- never hardcoded -- so
the artifact cannot silently omit, duplicate, or invent an item.

The load-bearing rule is the anti-favourable-absence one: a row may only claim
that work shipped if it NAMES a carrier that actually exists in the tree. An
artifact that asserts "shipped" with no named carrier would recreate exactly
the false-completion failure it exists to prevent, so that is a test failure.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PLAN = REPO_ROOT / "devtasks" / "plans" / "phase1-unified-plan.json"
ARTIFACT = (
    REPO_ROOT
    / "devtasks"
    / "plans"
    / "phase1-unified-plan-reconciliation-2026-08-10.md"
)

# Closed status vocabulary. Anything outside this set is a drafting error, not
# a new category -- widening it is a deliberate edit, not an accident.
SHIPPED_STATUSES = frozenset({"shipped", "shipped-partial"})
ALL_STATUSES = SHIPPED_STATUSES | {"not-shipped"}

# The cell value that means "deliberately no carrier".
NONE_CELL = "--"

REQUIRED_COLUMNS = (
    "item",
    "status",
    "production_carrier",
    "test_carrier",
    "planned_verification",
    "delta",
)

REQUIRED_METADATA = (
    "source_plan_path",
    "source_plan_sha",
    "source_plan_blob_sha256",
    "measured_at_sha",
    "item_count",
    "corroborated_on_main",
    "historical_scope_status",
    "reconciliation_role",
    "remaining_work_entrypoint",
    "non_authority",
)

# Closed values for the authority metadata keys. These are the load-bearing
# claims that make the plan's retirement an enforced fact rather than prose
# that can drift out from under the metadata (or vice versa).
EXPECTED_HISTORICAL_SCOPE_STATUS = "immutable_intent_not_live_status"
EXPECTED_RECONCILIATION_ROLE = "measured_phase1_status_record"
EXPECTED_REMAINING_WORK_ENTRYPOINT = "pipeline/CONTRACT.md"
EXPECTED_NON_AUTHORITY = "devtasks/COMBINED-QUEUE.md"

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ITEM_ID_RE = re.compile(r"^([A-Z])(\d+)$")


# --------------------------------------------------------------------------
# Derivation from the source plan -- the IDs are never written down here.
# --------------------------------------------------------------------------


def _load_plan() -> dict:
    assert SOURCE_PLAN.is_file(), f"source plan of record missing: {SOURCE_PLAN}"
    return json.loads(SOURCE_PLAN.read_text(encoding="utf-8"))


def _derive_plan_items() -> list[str]:
    """Return every task ID in the source plan, in plan order.

    Derived by walking the plan's own lane/task structure so that adding or
    removing a task in the plan immediately changes what the artifact must
    cover.
    """
    plan = _load_plan()
    items: list[str] = []
    for lane_name, lane in plan["lanes"].items():
        for task in lane["tasks"]:
            item_id = task["id"]
            assert isinstance(item_id, str) and item_id, (
                f"lane {lane_name} has a task with a missing or non-string id"
            )
            items.append(item_id)
    return items


def _plan_blob_sha256() -> str:
    return hashlib.sha256(SOURCE_PLAN.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# Artifact parsing
# --------------------------------------------------------------------------


def _split_cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(set(cell) <= {"-", ":"} and cell for cell in cells)


def _parse_metadata(text: str) -> dict[str, str]:
    """Read the ``key: value`` metadata block fenced as ```yaml at the top."""
    match = re.search(r"```yaml\n(.*?)```", text, re.DOTALL)
    assert match, "artifact must open with a fenced ```yaml metadata block"
    metadata: dict[str, str] = {}
    for raw_line in match.group(1).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        assert ":" in line, f"malformed metadata line: {raw_line!r}"
        key, _, value = line.partition(":")
        metadata[key.strip()] = value.strip()
    return metadata


def _extract_section(text: str, heading: str) -> str:
    """Return the body of a top-level ``## <heading>`` section, or ``""``.

    Used so an authority claim can be checked against actual prose rather
    than only against the metadata block -- a metadata-only edit that never
    touches the prose (or a prose deletion that leaves the metadata intact)
    must both still fail.
    """
    pattern = rf"\n## {re.escape(heading)}\n(.*?)(?=\n## |\Z)"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------
# Git-object truth. Never fall back to the live worktree: a pin that is
# unavailable is an instrument error, not permission to trust dirty or
# untracked files.
# --------------------------------------------------------------------------


def _git_commit_exists(rev: str, repo_root: Path = REPO_ROOT) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{rev}^{{commit}}"],
        capture_output=True,
    )
    return result.returncode == 0


def _git_path_exists_at(rev: str, path: str, repo_root: Path = REPO_ROOT) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{rev}:{path}"],
        capture_output=True,
    )
    return result.returncode == 0


def _git_blob_bytes(
    rev: str, path: str, repo_root: Path = REPO_ROOT
) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "blob", f"{rev}:{path}"],
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else None


def _shipped_carrier_paths(rows: list[dict[str, str]]) -> list[str]:
    """Every unique production/test carrier named by a shipped(-partial) row."""
    paths: list[str] = []
    for row in rows:
        if row["status"] not in SHIPPED_STATUSES:
            continue
        for column in ("production_carrier", "test_carrier"):
            paths.extend(_carrier_paths(row[column]))
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _parse_coverage_rows(text: str) -> list[dict[str, str]]:
    """Parse the single coverage table keyed by the REQUIRED_COLUMNS header."""
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            header = None
            continue
        cells = _split_cells(line)
        if header is None:
            if tuple(cells) == REQUIRED_COLUMNS:
                header = cells
            continue
        if _is_separator_row(cells):
            continue
        assert len(cells) == len(header), (
            f"coverage row has {len(cells)} cells, expected {len(header)}: {line!r}"
        )
        rows.append(dict(zip(header, cells, strict=True)))
    return rows


@pytest.fixture(scope="module")
def artifact_text() -> str:
    if not ARTIFACT.is_file():
        pytest.skip(
            "reconciliation artifact not present in this tree: "
            f"{ARTIFACT.relative_to(REPO_ROOT)}. This artifact's whole job is to "
            "prove status claims against real commits reachable in THIS "
            "repository's git object store (source_plan_sha/measured_at_sha, "
            "checked via `git cat-file`/`git diff` against REPO_ROOT). A "
            "public-release cut is a scrubbed, single point-in-time export of a "
            "continuously-committed private estate -- it neither carries that "
            "estate's git history (the SHAs this document would pin are foreign "
            "to whatever history this export ends up with) nor, right now, has "
            "any git history of its own at all (`git -C <repo> cat-file` fails "
            "with 'not a git repository' here). Restoring the artifact's prose "
            "without a real, checkable history behind it would not honestly "
            "answer 'which of the planned items shipped' -- it would just move "
            "the favourable-absence hazard this document exists to prevent from "
            "'row is missing' to 'row cites an unverifiable pin', which is worse. "
            "This suite is therefore estate-bound and skipped rather than forced "
            "green; the plan-of-record's OWN structural well-formedness (no git "
            "needed) is still covered by test_source_plan_item_ids_are_well_formed "
            "and test_source_plan_items_are_unique above, and the git-truth "
            "predicates themselves are covered self-contained, against scratch "
            "repos, by the negative-control tests below."
        )
    return ARTIFACT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def metadata(artifact_text: str) -> dict[str, str]:
    return _parse_metadata(artifact_text)


@pytest.fixture(scope="module")
def rows(artifact_text: str) -> list[dict[str, str]]:
    parsed = _parse_coverage_rows(artifact_text)
    assert parsed, (
        "artifact contains no coverage table; expected a markdown table whose "
        f"header is exactly {' | '.join(REQUIRED_COLUMNS)}"
    )
    return parsed


# --------------------------------------------------------------------------
# The source plan itself must still have the shape the artifact reconciles.
# --------------------------------------------------------------------------


def test_source_plan_item_ids_are_well_formed() -> None:
    """Each lane numbers its tasks contiguously from 1, so IDs are derivable."""
    plan = _load_plan()
    for lane_name, lane in plan["lanes"].items():
        ids = [task["id"] for task in lane["tasks"]]
        for item_id in ids:
            match = _ITEM_ID_RE.match(item_id)
            assert match, f"item id {item_id!r} is not <LANE-LETTER><INDEX>"
            assert match.group(1) == lane_name, (
                f"item {item_id!r} is filed under lane {lane_name!r}"
            )
        indices = [int(_ITEM_ID_RE.match(i).group(2)) for i in ids]
        assert indices == list(range(1, len(ids) + 1)), (
            f"lane {lane_name} task indices are not contiguous from 1: {indices}"
        )


def test_source_plan_items_are_unique() -> None:
    items = _derive_plan_items()
    duplicates = {i for i in items if items.count(i) > 1}
    assert not duplicates, f"source plan repeats item ids: {sorted(duplicates)}"


# --------------------------------------------------------------------------
# Coverage: every planned item, exactly once, no strays.
# --------------------------------------------------------------------------


def test_artifact_covers_every_planned_item_exactly_once(
    rows: list[dict[str, str]],
) -> None:
    planned = _derive_plan_items()
    covered = [row["item"] for row in rows]

    missing = [item for item in planned if item not in covered]
    assert not missing, (
        f"artifact omits planned item(s) {missing}; a missing row is exactly "
        "the statuslessness this artifact exists to remove"
    )

    unknown = [item for item in covered if item not in planned]
    assert not unknown, (
        f"artifact covers item(s) {unknown} that the source plan does not list"
    )

    duplicated = sorted({item for item in covered if covered.count(item) > 1})
    assert not duplicated, f"artifact repeats item(s) {duplicated}"

    assert len(covered) == len(planned), (
        f"artifact has {len(covered)} rows for {len(planned)} planned items"
    )


def test_artifact_rows_follow_plan_order(rows: list[dict[str, str]]) -> None:
    assert [row["item"] for row in rows] == _derive_plan_items(), (
        "coverage rows must appear in source-plan order so the two documents "
        "can be read side by side"
    )


# --------------------------------------------------------------------------
# Status discipline and the anti-favourable-absence rule.
# --------------------------------------------------------------------------


def test_every_row_carries_a_known_status(rows: list[dict[str, str]]) -> None:
    for row in rows:
        assert row["status"] in ALL_STATUSES, (
            f"item {row['item']} has status {row['status']!r}; allowed: "
            f"{sorted(ALL_STATUSES)}"
        )


def test_shipped_rows_name_carriers_that_exist(rows: list[dict[str, str]]) -> None:
    """A 'shipped' claim must be backed by a file that is actually present.

    This is the anti-favourable-absence check: without it the artifact could
    assert completion for work that was never done, which is the precise
    failure mode it was written to prevent.

    A row may omit ``test_carrier`` only when the SOURCE PLAN named no test
    file for that item (it specified an inline verification command instead).
    That exemption is derived from the plan and cross-checked by
    ``test_planned_verification_matches_the_source_plan``, so an author cannot
    invoke it to paper over a test that is simply missing.
    """
    plan_has_test = _planned_test_files()
    for row in rows:
        if row["status"] not in SHIPPED_STATUSES:
            continue
        columns = ["production_carrier"]
        if plan_has_test[row["item"]]:
            columns.append("test_carrier")
        else:
            assert row["delta"] != NONE_CELL, (
                f"item {row['item']} planned no test file, so its delta must "
                "record how the shipped state was verified instead"
            )
        for column in columns:
            cell = row[column]
            assert cell and cell != NONE_CELL, (
                f"item {row['item']} claims status {row['status']!r} but names "
                f"no {column}; a completion claim without a carrier is a "
                "false-completion hazard"
            )
            paths = _carrier_paths(cell)
            assert paths, (
                f"item {row['item']} has an unparseable {column}: {cell!r}"
            )
            for path in paths:
                assert _git_path_exists_at("HEAD", path), (
                    f"item {row['item']} cites {column} {path!r}, which does "
                    "not exist as a Git object in committed HEAD; a dirty or "
                    "untracked worktree file must never satisfy this check"
                )


def test_not_shipped_rows_claim_no_carrier(rows: list[dict[str, str]]) -> None:
    for row in rows:
        if row["status"] != "not-shipped":
            continue
        for column in ("production_carrier", "test_carrier"):
            assert row[column] == NONE_CELL, (
                f"item {row['item']} is marked not-shipped but names a "
                f"{column} ({row[column]!r}); pick one"
            )


def test_every_row_records_a_delta(rows: list[dict[str, str]]) -> None:
    """The delta column explains how reality differs from the plan."""
    for row in rows:
        assert row["delta"], f"item {row['item']} has an empty delta cell"


# --------------------------------------------------------------------------
# Authority: the reconciliation must prove -- not merely assert -- that the
# source plan is retired and that this document (not COMBINED-QUEUE.md) is
# the measured status record. Both the metadata AND the Retirement prose
# must agree, so neither a metadata-only nor a prose-only edit can drift.
# --------------------------------------------------------------------------


def test_historical_plan_is_explicitly_retired_as_live_status(
    artifact_text: str, metadata: dict[str, str]
) -> None:
    assert metadata["historical_scope_status"] == EXPECTED_HISTORICAL_SCOPE_STATUS, (
        "historical_scope_status must declare the source plan as immutable "
        "intent, not live status"
    )
    assert metadata["reconciliation_role"] == EXPECTED_RECONCILIATION_ROLE, (
        "reconciliation_role must declare this document as the measured "
        "Phase-1 status record"
    )
    assert (
        metadata["remaining_work_entrypoint"] == EXPECTED_REMAINING_WORK_ENTRYPOINT
    ), "remaining_work_entrypoint must point at pipeline/CONTRACT.md"
    assert metadata["non_authority"] == EXPECTED_NON_AUTHORITY, (
        "non_authority must name devtasks/COMBINED-QUEUE.md as NOT an "
        "authority for Phase-1 status"
    )

    retirement = _extract_section(artifact_text, "Retirement")
    assert retirement, (
        "artifact must have a '## Retirement' section stating the plan's "
        "authority relationship to this reconciliation; structured metadata "
        "alone is cosmetically satisfiable without it"
    )
    assert "immutable" in retirement.lower(), (
        "Retirement prose must state the source plan records immutable "
        "intent, not live status"
    )
    assert "measured" in retirement.lower(), (
        "Retirement prose must state this document is the measured status "
        "record"
    )
    assert EXPECTED_REMAINING_WORK_ENTRYPOINT in retirement, (
        f"Retirement prose must name {EXPECTED_REMAINING_WORK_ENTRYPOINT!r} "
        "as where remaining work is tracked"
    )
    assert EXPECTED_NON_AUTHORITY in retirement, (
        f"Retirement prose must name {EXPECTED_NON_AUTHORITY!r} as NOT an "
        "authority for Phase-1 status"
    )


def _planned_test_files() -> dict[str, list[str]]:
    """Map each plan item to the test paths the plan itself claimed to own."""
    plan = _load_plan()
    planned_tests: dict[str, list[str]] = {}
    for lane in plan["lanes"].values():
        for task in lane["tasks"]:
            planned_tests[task["id"]] = [
                path for path in task["owned_paths"] if path.startswith("tests/")
            ]
    return planned_tests


def test_planned_verification_matches_the_source_plan(
    rows: list[dict[str, str]],
) -> None:
    """Each row must quote the verification path the plan actually named."""
    planned_tests = _planned_test_files()

    for row in rows:
        expected = planned_tests[row["item"]]
        cell = row["planned_verification"]
        if not expected:
            assert cell == NONE_CELL, (
                f"item {row['item']} planned no test file, so "
                f"planned_verification must be {NONE_CELL!r}, got {cell!r}"
            )
            continue
        cited = _carrier_paths(cell)
        assert cited == expected, (
            f"item {row['item']} cites planned_verification {cited} but the "
            f"source plan named {expected}"
        )


def _carrier_paths(cell: str) -> list[str]:
    """Extract repo-relative paths from a table cell.

    Paths are written as inline code spans so that prose in the same cell
    cannot be mistaken for a filename.
    """
    return re.findall(r"`([^`]+)`", cell)


# --------------------------------------------------------------------------
# Provenance: the artifact is pinned to the exact plan it reconciles.
# --------------------------------------------------------------------------


def test_metadata_block_is_complete(metadata: dict[str, str]) -> None:
    missing = [key for key in REQUIRED_METADATA if key not in metadata]
    assert not missing, f"artifact metadata is missing {missing}"


def test_source_plan_sha_is_a_full_commit_sha(metadata: dict[str, str]) -> None:
    for key in ("source_plan_sha", "measured_at_sha"):
        value = metadata[key]
        assert _SHA1_RE.match(value), (
            f"{key} must be a full 40-character commit sha, got {value!r}"
        )


def test_metadata_points_at_the_real_source_plan(metadata: dict[str, str]) -> None:
    declared = metadata["source_plan_path"]
    assert declared == str(SOURCE_PLAN.relative_to(REPO_ROOT)), (
        f"source_plan_path {declared!r} is not the plan this test reconciles"
    )


def test_artifact_is_pinned_to_the_current_plan_contents(
    metadata: dict[str, str],
) -> None:
    """If the plan is edited, this goes red and forces re-reconciliation.

    Content-pinning is what keeps the artifact from quietly going stale: a
    reconciliation of a plan that has since changed is worse than none.
    """
    declared = metadata["source_plan_blob_sha256"]
    assert _SHA256_RE.match(declared), (
        f"source_plan_blob_sha256 must be a sha256 hex digest, got {declared!r}"
    )
    actual = _plan_blob_sha256()
    assert declared == actual, (
        "the source plan has changed since this reconciliation was written "
        f"(recorded {declared}, actual {actual}); re-measure the items and "
        "update the artifact rather than editing this pin"
    )


def test_metadata_counts_match_the_table(
    metadata: dict[str, str], rows: list[dict[str, str]]
) -> None:
    assert int(metadata["item_count"]) == len(rows), (
        "item_count disagrees with the number of coverage rows"
    )
    corroborated = sum(1 for row in rows if row["status"] in SHIPPED_STATUSES)
    assert int(metadata["corroborated_on_main"]) == corroborated, (
        "corroborated_on_main disagrees with the rows marked shipped"
    )


def test_item_count_matches_the_source_plan(metadata: dict[str, str]) -> None:
    assert int(metadata["item_count"]) == len(_derive_plan_items()), (
        "item_count disagrees with the number of tasks in the source plan"
    )


def test_phase1_pins_resolve_to_commits_and_source_blob(
    metadata: dict[str, str],
) -> None:
    """Both SHAs must be real, resolvable commits, and the source-plan blob
    hash must match the committed Git object at BOTH the pin and HEAD.

    Regex-only validation (forty hex characters) accepts a syntactically
    valid commit that is not actually reachable, and reading only the
    worktree file lets an uncommitted edit manufacture agreement. Neither is
    acceptable on a self-governing planning authority surface.
    """
    for key in ("source_plan_sha", "measured_at_sha"):
        rev = metadata[key]
        assert _git_commit_exists(rev), (
            f"{key} {rev!r} does not resolve to a commit object reachable in "
            "this repository's history; an unavailable pin is an instrument "
            "error, never permission to fall back to the worktree"
        )

    source_plan_sha = metadata["source_plan_sha"]
    declared_blob = metadata["source_plan_blob_sha256"]
    relative_path = str(SOURCE_PLAN.relative_to(REPO_ROOT))

    pinned_bytes = _git_blob_bytes(source_plan_sha, relative_path)
    assert pinned_bytes is not None, (
        f"{relative_path!r} is not present in the tree at source_plan_sha "
        f"{source_plan_sha!r}"
    )
    assert hashlib.sha256(pinned_bytes).hexdigest() == declared_blob, (
        "source_plan_blob_sha256 does not match the committed plan object at "
        "source_plan_sha"
    )

    head_bytes = _git_blob_bytes("HEAD", relative_path)
    assert head_bytes is not None, f"{relative_path!r} is missing from committed HEAD"
    assert hashlib.sha256(head_bytes).hexdigest() == declared_blob, (
        "source_plan_blob_sha256 does not match the committed plan object at "
        "HEAD; a dirty or uncommitted source-plan edit must not manufacture "
        "a match"
    )


# --------------------------------------------------------------------------
# Carrier truth split across two trees: historical (measured_at_sha) and
# current (HEAD). Neither predicate may fall back to the live worktree.
# --------------------------------------------------------------------------


def test_shipped_carriers_existed_at_measured_tree(
    rows: list[dict[str, str]], metadata: dict[str, str]
) -> None:
    measured_at_sha = metadata["measured_at_sha"]
    assert _git_commit_exists(measured_at_sha), (
        f"measured_at_sha {measured_at_sha!r} does not resolve to a commit; "
        "an unavailable pin is an instrument error, never permission to "
        "check the worktree instead"
    )
    missing = [
        path
        for path in _shipped_carrier_paths(rows)
        if not _git_path_exists_at(measured_at_sha, path)
    ]
    assert not missing, (
        f"shipped carrier(s) {missing} do not exist as Git objects at "
        f"measured_at_sha {measured_at_sha!r}; historical truth must be "
        "checked against the pinned commit, never the live worktree"
    )


def test_shipped_carriers_still_exist_at_head(rows: list[dict[str, str]]) -> None:
    missing = [
        path for path in _shipped_carrier_paths(rows) if not _git_path_exists_at("HEAD", path)
    ]
    assert not missing, (
        f"shipped carrier(s) {missing} do not exist in committed HEAD; a "
        "dirty or untracked worktree file must never satisfy this check"
    )


# --------------------------------------------------------------------------
# Negative controls. These do not depend on the artifact fixtures -- they
# exercise the Git-truth predicates directly against known-bad inputs, so a
# regression in the predicates themselves is caught even if the artifact
# stays honest.
# --------------------------------------------------------------------------


def test_false_measured_pin_lacks_the_d3_carrier(tmp_path: Path) -> None:
    """A syntactically valid, resolvable commit that predates a carrier's
    creation must fail the historical-tree predicate -- proving the check
    reads Git history, not the live worktree where the file may be present.

    The private-repo original pinned a real historical commit
    (``8e16a995d936406f067a6e70f728e78e1b4d02a6``) against a real carrier path
    in REPO_ROOT's own history. Neither is meaningful in this public-release
    export: that SHA belongs to a different repository's object store, and
    REPO_ROOT itself currently has no git history at all (see the skip reason
    in the ``artifact_text`` fixture above). The same property -- a
    resolvable-but-too-early commit must not see a file that postdates it --
    is proven self-contained here instead, the same way the other negative
    controls below do.
    """
    scratch = tmp_path / "scratch-false-pin-repo"
    scratch.mkdir()
    _init_scratch_repo(scratch)

    (scratch / "seed.txt").write_text("seed\n")
    _run_scratch_git(scratch, "add", ".")
    _run_scratch_git(scratch, "commit", "-q", "-m", "before carrier")
    false_pin = _run_scratch_git(scratch, "rev-parse", "HEAD").stdout.strip()

    carrier_rel = "tests/scripts/reflection/test_install_reflection_label_guard.py"
    carrier_path = scratch / carrier_rel
    carrier_path.parent.mkdir(parents=True)
    carrier_path.write_text("# carrier added after the false pin\n")
    _run_scratch_git(scratch, "add", ".")
    _run_scratch_git(scratch, "commit", "-q", "-m", "add carrier")

    assert _git_commit_exists(false_pin, repo_root=scratch), (
        "false pin must itself be a real commit"
    )
    assert carrier_path.exists(), (
        "the carrier must exist in the live worktree for this control to "
        "be meaningful"
    )
    assert not _git_path_exists_at(false_pin, carrier_rel, repo_root=scratch), (
        f"{carrier_rel} unexpectedly exists at {false_pin}; the false-pin "
        "negative control assumption no longer holds"
    )


def test_unavailable_commit_fails_before_path_classification() -> None:
    zeroes = "0" * 40
    assert not _git_commit_exists(zeroes), "forty zeroes must never resolve as a commit"


def test_temporal_split_is_non_vacuous(tmp_path: Path) -> None:
    """A carrier present at the historical measurement tree but removed by
    HEAD must fail the current-freshness check while the historical check
    stays green -- proving the two predicates check different things.
    """
    scratch = tmp_path / "scratch-repo"
    scratch.mkdir()
    _init_scratch_repo(scratch)

    carrier_rel = "carrier.py"
    (scratch / carrier_rel).write_text("# carrier\n")
    _run_scratch_git(scratch, "add", ".")
    _run_scratch_git(scratch, "commit", "-q", "-m", "historical")
    historical_sha = _run_scratch_git(scratch, "rev-parse", "HEAD").stdout.strip()

    (scratch / carrier_rel).unlink()
    _run_scratch_git(scratch, "add", "-A")
    _run_scratch_git(scratch, "commit", "-q", "-m", "current")

    assert _git_path_exists_at(historical_sha, carrier_rel, repo_root=scratch), (
        "historical measurement check must stay green at the tree that "
        "still has the carrier"
    )
    assert not _git_path_exists_at("HEAD", carrier_rel, repo_root=scratch), (
        "current freshness check must go red once the carrier is removed "
        "from committed HEAD"
    )


def test_worktree_only_path_fails_at_both_trees(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch-repo-dirty"
    scratch.mkdir()
    _init_scratch_repo(scratch)
    (scratch / "committed.txt").write_text("x\n")
    _run_scratch_git(scratch, "add", ".")
    _run_scratch_git(scratch, "commit", "-q", "-m", "init")
    head_sha = _run_scratch_git(scratch, "rev-parse", "HEAD").stdout.strip()

    dirty_path = "untracked-carrier.py"
    (scratch / dirty_path).write_text("# manufactured only in the worktree\n")

    assert not _git_path_exists_at(head_sha, dirty_path, repo_root=scratch), (
        "a dirty worktree-only file must not satisfy the historical check"
    )
    assert not _git_path_exists_at("HEAD", dirty_path, repo_root=scratch), (
        "a dirty worktree-only file must not satisfy the current-HEAD check "
        "either"
    )


def test_dirty_source_plan_does_not_manufacture_a_match(tmp_path: Path) -> None:
    """An uncommitted edit to the source plan must not change what
    ``_git_blob_bytes`` reads at HEAD -- the read source is the Git object
    store, not the worktree file.
    """
    scratch = tmp_path / "scratch-source-plan-repo"
    scratch.mkdir()
    _init_scratch_repo(scratch)
    plan_rel = "plan.json"
    (scratch / plan_rel).write_text('{"a": 1}\n')
    _run_scratch_git(scratch, "add", ".")
    _run_scratch_git(scratch, "commit", "-q", "-m", "init")
    committed_blob = _git_blob_bytes("HEAD", plan_rel, repo_root=scratch)
    committed_hash = hashlib.sha256(committed_blob).hexdigest()

    (scratch / plan_rel).write_text('{"a": 2}\n')  # dirty, uncommitted edit

    head_blob = _git_blob_bytes("HEAD", plan_rel, repo_root=scratch)
    assert hashlib.sha256(head_blob).hexdigest() == committed_hash, (
        "the committed HEAD object must be unaffected by an uncommitted "
        "worktree edit"
    )


def _init_scratch_repo(path: Path) -> None:
    _run_scratch_git(path, "init", "-q")
    _run_scratch_git(path, "config", "user.email", "scratch@test.invalid")
    _run_scratch_git(path, "config", "user.name", "scratch")


def _run_scratch_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"scratch git command {args!r} failed: {result.stderr}"
    )
    return result
