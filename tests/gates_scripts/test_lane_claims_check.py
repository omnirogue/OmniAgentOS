"""`scripts/gates/lane-claims-check.sh` — machine-checkable lane ownership.

devtasks/SESSION-CLAIMS.md is prose nothing reads, so nothing enforces it: on
2026-07-31 a lane edited paths inside the live ``dashboard/**`` claim and only a
human noticed, after the work was done. devtasks/lane-claims.yaml plus this
checker turn that into a mechanical answer.

The properties that matter, and that these tests pin:

* a lane is never blocked by its OWN claims (otherwise nobody would claim);
* touching another IN-FLIGHT lane's path is refused;
* a ``released`` claim blocks nobody (otherwise the ledger rots);
* unclaimed paths pass — this is an ownership check, not a review;
* the override is loud and recorded, never silent;
* a ledger that cannot be trusted (duplicate lane, bad status, unreadable ref)
  exits 2 — "could not run" is a refusal, not a pass.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GATES = REPO_ROOT / "scripts" / "gates"
CHECKER_SH = GATES / "lane-claims-check.sh"
CHECKER_PY = GATES / "lane_claims_check.py"
LEDGER = REPO_ROOT / "devtasks" / "lane-claims.yaml"
SESSION_CLAIMS = REPO_ROOT / "devtasks" / "SESSION-CLAIMS.md"


def _load_checker() -> ModuleType:
    """Import by file path — scripts/gates is not a package, and putting it on
    sys.path for the whole session would let its modules shadow real ones."""
    spec = importlib.util.spec_from_file_location("_lane_claims_check", CHECKER_PY)
    assert spec is not None and spec.loader is not None, CHECKER_PY
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves string annotations through sys.modules[__module__];
    # without this registration the decorator raises on import.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()
IN_FLIGHT = checker.IN_FLIGHT
LaneClaim = checker.LaneClaim
LedgerError = checker.LedgerError
_glob_to_regex = checker._glob_to_regex
find_violations = checker.find_violations
load_ledger = checker.load_ledger
parse_ledger = checker.parse_ledger
parse_name_status = checker.parse_name_status
validate_glob = checker.validate_glob


def _touched(*paths: str) -> list[tuple[str, str]]:
    """find_violations takes (path, how) pairs — renames touch two paths."""
    return [(path, "changed") for path in paths]

LEDGER_FIXTURE: dict[str, Any] = {
    "version": 1,
    "lanes": [
        {
            "lane": "t/ui-band1",
            "owner": "integration-discipline",
            "status": "in_flight",
            "claimed_at": "2026-07-29T15:00:00Z",
            "paths": ["dashboard/**"],
        },
        {
            "lane": "t/integration-package",
            "owner": "integration-discipline",
            "status": "in_flight",
            "claimed_at": "2026-07-29T15:00:00Z",
            "paths": ["omniagentos/integration/**", "scripts/merge-gate.sh"],
        },
        {
            "lane": "old-lane",
            "owner": "coordinating",
            "status": "released",
            "claimed_at": "2026-07-20",
            "paths": ["omniagentos/legacy/**"],
        },
        {
            "lane": "lane-coordination",
            "owner": "coordinating",
            "status": "in_flight",
            "claimed_at": "2026-07-31",
            "paths": ["tests/counterfeits/harness.py"],
        },
    ],
}


LEDGER_LANES: list[dict[str, Any]] = LEDGER_FIXTURE["lanes"]


def _claims() -> list[Any]:
    return parse_ledger(LEDGER_FIXTURE)


# --- ownership semantics ---------------------------------------------------


def test_lane_touching_its_own_claimed_path_passes() -> None:
    violations = find_violations(
        _claims(),
        lane_id="lane-coordination",
        changed_paths=_touched("tests/counterfeits/harness.py"),
    )
    assert violations == []


def test_lane_touching_another_in_flight_lanes_path_is_refused() -> None:
    violations = find_violations(
        _claims(),
        lane_id="lane-coordination",
        changed_paths=_touched("dashboard/src/App.tsx"),
    )
    assert len(violations) == 1
    assert violations[0].lane == "t/ui-band1"
    assert violations[0].glob == "dashboard/**"
    assert violations[0].path == "dashboard/src/App.tsx"


def test_released_claim_does_not_block() -> None:
    violations = find_violations(
        _claims(),
        lane_id="some-other-lane",
        changed_paths=_touched("omniagentos/legacy/thing.py"),
    )
    assert violations == []


def test_unclaimed_paths_pass() -> None:
    violations = find_violations(
        _claims(),
        lane_id="some-other-lane",
        changed_paths=_touched("omniagentos/nobody/owns/this.py", "README.md"),
    )
    assert violations == []


def test_one_path_claimed_by_two_in_flight_lanes_reports_both() -> None:
    """merge-gate.sh really is double-claimed today; hiding one is not an option."""
    claims = _claims() + [
        LaneClaim(
            lane="m5-probe-refusal",
            owner="coordinating",
            status=IN_FLIGHT,
            claimed_at="2026-07-29",
            paths=("scripts/merge-gate.sh",),
        )
    ]
    violations = find_violations(
        claims, lane_id="lane-coordination", changed_paths=_touched("scripts/merge-gate.sh")
    )
    assert {v.lane for v in violations} == {"t/integration-package", "m5-probe-refusal"}


# --- glob semantics --------------------------------------------------------


def test_double_star_crosses_slashes_and_single_star_does_not() -> None:
    subtree = _glob_to_regex("dashboard/**")
    assert subtree.match("dashboard/a.ts")
    assert subtree.match("dashboard/src/deep/a.ts")
    assert not subtree.match("dashboard")
    assert not subtree.match("dashboard-other/a.ts")

    segment = _glob_to_regex("omniagentos/*/models.py")
    assert segment.match("omniagentos/swarm/models.py")
    assert not segment.match("omniagentos/a/b/models.py")


def test_directory_claim_without_glob_matches_only_that_literal_path() -> None:
    """No implicit prefix matching — it would silently widen every claim."""
    exact = _glob_to_regex("scripts/merge-gate.sh")
    assert exact.match("scripts/merge-gate.sh")
    assert not exact.match("scripts/merge-gate.sh.bak")

    bare_dir = _glob_to_regex("omniagentos/integration")
    assert not bare_dir.match("omniagentos/integration/promote.py")


# --- ledger integrity ------------------------------------------------------


def test_shipped_ledger_parses_and_is_consistent() -> None:
    """Structural properties only.

    The pre-release private ledger pinned specific historical lane ids (e.g.
    a real crossed `dashboard/**` claim); this public tree ships only the
    checkable *mechanism* with a single inert example lane (see the
    "PUBLIC-RELEASE NOTE" in devtasks/lane-claims.yaml), so those specific
    ids no longer exist. What still matters, and what stays pinned here, is
    that whatever the ledger contains parses cleanly and is internally
    consistent -- unique lane ids, every claim carrying paths, a status from
    the closed vocabulary, and a claimed_at stamp.
    """
    claims = load_ledger(LEDGER)
    lanes = [claim.lane for claim in claims]
    assert lanes, "shipped ledger must declare at least one lane"
    assert len(lanes) == len(set(lanes))
    for claim in claims:
        assert claim.paths
        assert claim.status in {"in_flight", "released"}
        assert claim.claimed_at


def test_shipped_ledger_transcribes_every_in_flight_row_of_session_claims() -> None:
    """The ledger is a transcription; a dropped row is a claim nobody enforces.

    devtasks/SESSION-CLAIMS.md is the operator's live claim narrative --
    process content deliberately not shipped in this public-release tree
    (devtasks/ here carries only REACHABILITY-EXEMPT.txt and lane-claims.yaml).
    Without that source doc there is nothing to transcribe *against*, so the
    row-by-row fidelity check this test otherwise runs cannot be meaningfully
    evaluated -- restoring SESSION-CLAIMS.md would reintroduce exactly the
    operator process content the public cut intentionally removes. The
    mechanism itself (parsing, aliasing, glob semantics, end-to-end refusal)
    stays covered by the other tests in this module against synthetic
    fixtures, so nothing about the checker goes untested.

    What we CAN still assert without the source doc: an ledger with no
    narrative behind it must not silently claim any lane as `in_flight` --
    an unsourced in-flight claim is exactly the untranscribed-claim hazard
    this file exists to prevent, so the shipped example ledger must be
    all-`released`.
    """
    if not SESSION_CLAIMS.is_file():
        ledger_lanes = {
            claim.lane for claim in load_ledger(LEDGER) if claim.status == IN_FLIGHT
        }
        assert not ledger_lanes, (
            f"no {SESSION_CLAIMS.relative_to(REPO_ROOT)} to transcribe from, "
            f"yet these lanes claim in_flight anyway: {sorted(ledger_lanes)}"
        )
        return

    in_flight_rows = [
        line
        for line in SESSION_CLAIMS.read_text().splitlines()
        if line.startswith("|") and "IN FLIGHT" in line
    ]
    ledger_lanes = {claim.lane for claim in load_ledger(LEDGER) if claim.status == IN_FLIGHT}
    # Every source row's lane id must appear somewhere in the ledger.
    for row in in_flight_rows:
        assert any(f"`{lane}`" in row or f" {lane} " in row for lane in ledger_lanes), (
            f"no ledger entry for SESSION-CLAIMS row: {row}"
        )
    assert len(ledger_lanes) >= len(in_flight_rows)


def test_duplicate_lane_id_in_ledger_is_a_hard_error() -> None:
    data = {
        "lanes": [
            {"lane": "dup", "owner": "o", "status": "in_flight", "paths": ["a"]},
            {"lane": "dup", "owner": "o", "status": "in_flight", "paths": ["b"]},
        ]
    }
    with pytest.raises(LedgerError, match="duplicate lane id"):
        parse_ledger(data)


def test_unknown_status_is_a_hard_error() -> None:
    data = {"lanes": [{"lane": "x", "owner": "o", "status": "maybe", "paths": ["a"]}]}
    with pytest.raises(LedgerError, match="status 'maybe'"):
        parse_ledger(data)


def test_missing_paths_is_a_hard_error() -> None:
    data = {"lanes": [{"lane": "x", "owner": "o", "status": "in_flight"}]}
    with pytest.raises(LedgerError, match="'paths' must be a non-empty list"):
        parse_ledger(data)


# --- end to end through the shell entry point ------------------------------


def _write_ledger(path: Path) -> Path:
    path.write_text(yaml.safe_dump(LEDGER_FIXTURE, sort_keys=False), encoding="utf-8")
    return path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def candidate_repo(tmp_path: Path) -> Path:
    """A throwaway repo with `main` and a branch that edits dashboard/**."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "gate@omniagentos.local")
    _git(repo, "config", "user.name", "gate")
    (repo / "README.md").write_text("seed\n")
    (repo / "dashboard").mkdir()
    (repo / "dashboard" / "App.tsx").write_text("export const a = 1;\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    _git(repo, "checkout", "-q", "-b", "candidate")
    (repo / "dashboard" / "App.tsx").write_text("export const a = 2;\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "touch a claimed path")
    return repo


def _run_checker(
    repo: Path, ledger: Path, *, lane: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LANE_ID"] = lane
    env.pop("LANE_CLAIMS_OVERRIDE", None)
    env.update(env_extra or {})
    return subprocess.run(
        [
            str(CHECKER_SH),
            "main",
            "candidate",
            "--ledger",
            str(ledger),
            "--repo",
            str(repo),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_scripts_have_valid_syntax() -> None:
    assert os.access(CHECKER_SH, os.X_OK), f"{CHECKER_SH} is not executable"
    syntax = subprocess.run(["sh", "-n", str(CHECKER_SH)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr
    assert CHECKER_PY.is_file()


def test_end_to_end_refuses_a_crossed_claim(candidate_repo: Path, tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path / "ledger.yaml")
    result = _run_checker(candidate_repo, ledger, lane="lane-coordination")
    assert result.returncode == 1, f"{result.stdout}\n{result.stderr}"
    assert "dashboard/App.tsx" in result.stderr
    assert "t/ui-band1" in result.stderr


def test_end_to_end_owner_of_the_claim_is_not_blocked(
    candidate_repo: Path, tmp_path: Path
) -> None:
    ledger = _write_ledger(tmp_path / "ledger.yaml")
    result = _run_checker(candidate_repo, ledger, lane="t/ui-band1")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout


def test_end_to_end_override_passes_but_prints_a_loud_warning(
    candidate_repo: Path, tmp_path: Path
) -> None:
    ledger = _write_ledger(tmp_path / "ledger.yaml")
    result = _run_checker(
        candidate_repo,
        ledger,
        lane="lane-coordination",
        env_extra={"LANE_CLAIMS_OVERRIDE": "1"},
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "LANE_CLAIMS_OVERRIDE" in result.stderr
    assert "DELIBERATELY OVERRIDDEN" in result.stderr
    # The violations themselves stay visible — an override records, never hides.
    assert "t/ui-band1" in result.stderr


def test_end_to_end_missing_lane_id_cannot_run(candidate_repo: Path, tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path / "ledger.yaml")
    result = _run_checker(candidate_repo, ledger, lane="")
    assert result.returncode == 2
    assert "no acting lane" in result.stderr


def test_end_to_end_unreadable_ref_is_exit_2_not_a_pass(
    candidate_repo: Path, tmp_path: Path
) -> None:
    ledger = _write_ledger(tmp_path / "ledger.yaml")
    env = os.environ.copy()
    env["LANE_ID"] = "lane-coordination"
    result = subprocess.run(
        [
            str(CHECKER_SH),
            "main",
            "no-such-ref-zz99",
            "--ledger",
            str(ledger),
            "--repo",
            str(candidate_repo),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 2
    assert "REFUSED" in result.stderr


def test_checker_is_not_installed_as_a_git_hook() -> None:
    """core.hooksPath is repo-wide; an enforcing hook would break live sessions."""
    hooks_dir = REPO_ROOT / ".git" / "hooks"
    configured = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()
    installed = []
    if hooks_dir.is_dir():
        installed = [
            hook.name
            for hook in hooks_dir.iterdir()
            if hook.is_file() and "lane-claims" in hook.read_text(errors="ignore")
        ]
    assert not installed, f"lane-claims wired into git hooks: {installed}"
    if configured:
        hook_path = Path(configured)
        if not hook_path.is_absolute():
            hook_path = REPO_ROOT / hook_path
        if hook_path.is_dir():
            for hook in hook_path.iterdir():
                if hook.is_file():
                    assert "lane-claims" not in hook.read_text(errors="ignore"), hook


# ===========================================================================
# REWORK — Sol review. Every finding below is a place the gate FAILED OPEN,
# which is worse than no gate: it reads as protection.
# ===========================================================================


# --- MAJOR 3: renames bypassed the check ----------------------------------
# `git diff --name-only` reports ONLY a rename's destination, so `git mv
# <their file> <my dir>` moved a file OUT of a claimed tree and passed.


def test_parse_name_status_returns_both_sides_of_a_rename() -> None:
    output = "M\tREADME.md\nR100\tdashboard/App.tsx\tsandbox/App.tsx\nA\tnew.py\n"
    assert parse_name_status(output) == [
        ("README.md", "changed"),
        ("dashboard/App.tsx", "renamed from"),
        ("sandbox/App.tsx", "renamed to"),
        ("new.py", "changed"),
    ]


def test_parse_name_status_handles_copies_and_ignores_blank_lines() -> None:
    output = "\nC75\tdashboard/a.ts\tsandbox/a.ts\n\nD\tgone.py\n"
    assert parse_name_status(output) == [
        ("dashboard/a.ts", "copied from"),
        ("sandbox/a.ts", "copied to"),
        ("gone.py", "changed"),
    ]


def test_moving_a_file_out_of_a_claimed_tree_is_a_violation() -> None:
    """The destination is unclaimed; the SOURCE is what makes this a violation."""
    violations = find_violations(
        _claims(),
        lane_id="lane-coordination",
        changed_paths=[
            ("dashboard/App.tsx", "renamed from"),
            ("sandbox/App.tsx", "renamed to"),
        ],
    )
    assert [v.path for v in violations] == ["dashboard/App.tsx"]
    assert violations[0].lane == "t/ui-band1"
    assert violations[0].how == "renamed from"


@pytest.fixture
def rename_repo(tmp_path: Path) -> Path:
    """A repo whose candidate MOVES a claimed file into an unclaimed directory."""
    repo = tmp_path / "renamerepo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "gate@omniagentos.local")
    _git(repo, "config", "user.name", "gate")
    (repo / "dashboard").mkdir()
    # Long enough that git's rename detection scores it 100%.
    (repo / "dashboard" / "App.tsx").write_text("export const a = 1;\n" * 40)
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    _git(repo, "checkout", "-q", "-b", "candidate")
    (repo / "sandbox").mkdir()
    _git(repo, "mv", "dashboard/App.tsx", "sandbox/App.tsx")
    _git(repo, "commit", "-qm", "move a claimed file out of the claimed tree")
    return repo


def test_end_to_end_rename_out_of_a_claimed_tree_is_refused(
    rename_repo: Path, tmp_path: Path
) -> None:
    ledger = _write_ledger(tmp_path / "ledger.yaml")
    # Prove the old implementation's view really did hide this.
    name_only = subprocess.run(
        ["git", "diff", "--name-only", "main...candidate"],
        cwd=rename_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "dashboard/App.tsx" not in name_only, (
        "precondition: --name-only must hide the rename source"
    )

    result = _run_checker(rename_repo, ledger, lane="lane-coordination")
    assert result.returncode == 1, f"{result.stdout}\n{result.stderr}"
    assert "dashboard/App.tsx" in result.stderr
    assert "renamed from" in result.stderr


# --- MAJOR 4: an unknown lane id exited 0 ---------------------------------
# A typo in the lane name printed a note and passed with "0 changed path(s)",
# silently disabling the entire check.


def test_end_to_end_unknown_lane_id_refuses_with_exit_2(
    candidate_repo: Path, tmp_path: Path
) -> None:
    ledger = _write_ledger(tmp_path / "ledger.yaml")
    result = _run_checker(candidate_repo, ledger, lane="lane-coordinatoin")  # typo
    assert result.returncode == 2, f"{result.stdout}\n{result.stderr}"
    assert "has no entry" in result.stderr
    assert "Known lanes:" in result.stderr
    # It must NOT have reported a clean run.
    assert "OK —" not in result.stdout


def test_an_alias_resolves_to_its_lane(candidate_repo: Path, tmp_path: Path) -> None:
    """merge-gate passes a BRANCH name; an alias is how that stays fail-closed."""
    data = {
        "lanes": [
            dict(LEDGER_LANES[0]),
            {**LEDGER_LANES[3], "aliases": ["lane/lane-coordination"]},
        ]
    }
    ledger = tmp_path / "aliased.yaml"
    ledger.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    claims = parse_ledger(data)
    assert find_violations(
        claims, lane_id="lane/lane-coordination", changed_paths=_touched("dashboard/x.ts")
    )
    result = _run_checker(candidate_repo, ledger, lane="lane/lane-coordination")
    assert result.returncode == 1, f"{result.stdout}\n{result.stderr}"
    assert "dashboard/App.tsx" in result.stderr


def test_alias_colliding_with_a_lane_id_is_a_hard_error() -> None:
    data = {
        "lanes": [
            {"lane": "a", "owner": "o", "status": "in_flight", "paths": ["x"]},
            {"lane": "b", "owner": "o", "status": "in_flight", "paths": ["y"], "aliases": ["a"]},
        ]
    }
    with pytest.raises(LedgerError, match="collides with a lane id"):
        parse_ledger(data)


# --- MAJOR 5: the override accepted almost any value ----------------------
# `LANE_CLAIMS_OVERRIDE=False` reported all violations and still exited 0.


@pytest.mark.parametrize("value", ["False", "false", "no", "0", "true", "yes", "TRUE", "on", "2"])
def test_override_only_accepts_exactly_1(
    candidate_repo: Path, tmp_path: Path, value: str
) -> None:
    """`False` must never bypass; nothing but `1` may, and a typo is loud."""
    ledger = _write_ledger(tmp_path / "ledger.yaml")
    result = _run_checker(
        candidate_repo,
        ledger,
        lane="lane-coordination",
        env_extra={"LANE_CLAIMS_OVERRIDE": value},
    )
    assert result.returncode != 0, (
        f"LANE_CLAIMS_OVERRIDE={value!r} bypassed the check\n{result.stdout}\n{result.stderr}"
    )
    if value == "0":
        # The one unambiguous "off": enforce, so the real violation is exit 1.
        assert result.returncode == 1
    else:
        # Everything else — including the very plausible `False` — is a hard
        # error, so a misspelled override is loud rather than silently either
        # bypassing or being ignored.
        assert result.returncode == 2
        assert "is not understood" in result.stderr


def test_unset_override_enforces(candidate_repo: Path, tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path / "ledger.yaml")
    result = _run_checker(candidate_repo, ledger, lane="lane-coordination")
    assert result.returncode == 1


# --- MAJOR 7: invalid claim globs were accepted silently -------------------
# ('', '../dashboard/**', '/dashboard/**') parsed fine and protected NOTHING.


@pytest.mark.parametrize(
    ("glob", "why"),
    [
        ("", "blank"),
        ("   ", "blank"),
        ("/dashboard/**", "absolute"),
        ("../dashboard/**", "traversal"),
        ("dashboard/../../etc/**", "traversal"),
        ("./dashboard/**", "dot segment"),
        ("dashboard\\src\\**", "backslash"),
    ],
)
def test_invalid_claim_globs_are_rejected_at_parse_time(glob: str, why: str) -> None:
    """An unmatchable glob makes a lane believe it is covered when it is not."""
    with pytest.raises(LedgerError):
        validate_glob(glob, where="test")
    data = {"lanes": [{"lane": "x", "owner": "o", "status": "in_flight", "paths": [glob]}]}
    with pytest.raises(LedgerError):
        parse_ledger(data)


@pytest.mark.parametrize(
    "glob",
    ["dashboard/**", "scripts/merge-gate.sh", "omniagentos/*/models.py", "Makefile", "a/b?.py"],
)
def test_valid_claim_globs_are_accepted(glob: str) -> None:
    assert validate_glob(glob, where="test") == glob


def test_ledger_with_an_invalid_glob_refuses_end_to_end(
    candidate_repo: Path, tmp_path: Path
) -> None:
    ledger = tmp_path / "bad.yaml"
    ledger.write_text(
        yaml.safe_dump(
            {
                "lanes": [
                    {
                        "lane": "t/ui-band1",
                        "owner": "o",
                        "status": "in_flight",
                        "paths": ["../dashboard/**"],
                    },
                    LEDGER_LANES[3],
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    result = _run_checker(candidate_repo, ledger, lane="lane-coordination")
    assert result.returncode == 2
    assert "REFUSED" in result.stderr


# --- glob semantics, pinned explicitly (reviewer flagged them unspecified) --


@pytest.mark.parametrize(
    ("glob", "path", "expected"),
    [
        ("dashboard/**", "dashboard/src/x.ts", True),
        ("dashboard/**", "dashboard/x.ts", True),
        ("dashboard/**", "dashboard/a/b/c/d.ts", True),
        ("dashboard/**", "dashboard", False),
        ("dashboard/**", "dashboard-other/x.ts", False),
        ("a/b.py", "a/b.py", True),
        ("a/b.py", "a/bc.py", False),
        ("a/b.py", "a/b.pyc", False),
        ("a/b.py", "xa/b.py", False),
        ("omniagentos/*/models.py", "omniagentos/swarm/models.py", True),
        ("omniagentos/*/models.py", "omniagentos/a/b/models.py", False),
        ("Makefile", "Makefile", True),
        ("Makefile", "sub/Makefile", False),
    ],
)
def test_glob_semantics_are_pinned(glob: str, path: str, expected: bool) -> None:
    assert bool(_glob_to_regex(glob).match(path)) is expected


def test_shipped_ledger_header_records_that_it_is_not_authoritative() -> None:
    """Partial coverage must not read as full coverage (REACHABILITY-EXEMPT idiom)."""
    header = LEDGER.read_text()
    assert "NOT AUTHORITATIVE" in header
    assert "NOT TRANSCRIBED" in header
    assert "no IN-FLIGHT claim was crossed" in header


# --- REWORK round 2 (Sol): alias uniqueness ---------------------------------
# An alias that resolves to two lanes enforces only whichever `answers_to()`
# matched first, so the OTHER lane's claims are silently skipped for a caller
# using it — a bypass by ledger typo, the same fail-open class as the rest.


def _lane(lane: str, **extra: Any) -> dict[str, Any]:
    return {"lane": lane, "owner": "o", "status": "in_flight", "paths": ["a/**"], **extra}


def test_same_alias_in_two_lanes_is_a_hard_error() -> None:
    data = {
        "lanes": [
            _lane("lane-one", aliases=["shared-alias-probe"]),
            _lane("lane-two", aliases=["shared-alias-probe"]),
        ]
    }
    with pytest.raises(LedgerError) as excinfo:
        parse_ledger(data)
    message = str(excinfo.value)
    # BOTH lanes and the offending alias, or the operator cannot fix it.
    assert "shared-alias-probe" in message
    assert "lane-one" in message and "lanes[1]" in message
    assert "lanes[0]" in message


def test_alias_repeated_within_one_lane_is_a_hard_error() -> None:
    data = {"lanes": [_lane("lane-one", aliases=["dup-alias", "dup-alias"])]}
    with pytest.raises(LedgerError, match="alias 'dup-alias' is listed twice for 'lane-one'"):
        parse_ledger(data)


def test_alias_matching_a_later_lanes_id_is_a_hard_error() -> None:
    """The in-loop check only saw lanes declared SO FAR, so this ordering slipped.

    `LANE_ID=ml-wireB` resolved to `lane-coordination` first, leaving ml-wireB's
    own claims unenforced against that caller.
    """
    data = {
        "lanes": [
            _lane("lane-coordination", aliases=["ml-wireB"]),
            _lane("ml-wireB"),
        ]
    }
    with pytest.raises(LedgerError, match=r"alias 'ml-wireB' collides with a lane id"):
        parse_ledger(data)


def test_alias_matching_an_earlier_lanes_id_is_still_a_hard_error() -> None:
    """Regression floor: the ordering that already worked must keep working."""
    data = {
        "lanes": [
            _lane("ml-wireB"),
            _lane("lane-coordination", aliases=["ml-wireB"]),
        ]
    }
    with pytest.raises(LedgerError, match="collides with a lane id"):
        parse_ledger(data)


def test_no_alias_resolves_to_more_than_one_lane() -> None:
    """The property itself, stated once: resolution is unambiguous by construction."""
    claims = load_ledger(LEDGER)
    for claim in claims:
        for alias in claim.aliases:
            resolved = [other.lane for other in claims if other.answers_to(alias)]
            assert resolved == [claim.lane], f"alias {alias!r} resolves to {resolved}"


def test_the_shipped_ledger_still_parses_and_its_alias_still_resolves() -> None:
    """Do not regress the merge-gate snippet path (`LANE_ID=$BRANCH`).

    The private ledger's real regression floor pinned a specific historical
    lane id (`lane-coordination`); this public tree ships only the single
    inert `example-lane` (see devtasks/lane-claims.yaml), carrying a
    `lane/example-lane` alias for exactly this purpose, so the floor is
    pinned against that instead -- same mechanism, no operator content.
    """
    claims = load_ledger(LEDGER)
    resolved = [claim.lane for claim in claims if claim.answers_to("lane/example-lane")]
    assert resolved == ["example-lane"]
    assert [claim.lane for claim in claims if claim.answers_to("example-lane")] == [
        "example-lane"
    ]
