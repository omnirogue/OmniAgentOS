"""The default gate workspace: routines must settle on evidence, or on nothing.

``default_gate_workspace()`` is opt-in, and nothing configured it. Every routine
settlement on this installation recorded ``gate_evidence_unavailable`` — 254/254
of them — so the objective gate chain, the signing key, the whole evidence
apparatus, has never rendered a verdict about a real routine run.

Three halves, and the last two are what keep it safe:

1. WITH the workspace configured, settlement produces a REAL verdict. Not a
   verdict-shaped absence: ``gate_passed`` is a boolean and ``stop_reason`` is
   ``gate_passed``/``gate_failed``, decided by re-running the routine's declared
   verifier at the pinned commit.
2. WITHOUT it, behaviour is byte-for-byte the a6c0cc7e shape — ``gate_passed``
   and ``accepted`` are NULL and the run is EXCLUDED from the acceptance floor.
   The same NULL bucket now also holds every case where the workspace itself
   could not be executed against (missing, not a checkout, dirty), because
   ``GateWorkspaceUnusable`` splits those causes out of the refusal class at the
   raise site. Absent evidence must stay absent, never become evidence of
   failure — a workspace that goes dirty between configuration and settlement
   judged nothing, and counting it as a rejection would auto-pause a healthy
   routine.
3. But absence must not become a HIDING PLACE either. The gate never executes in
   the configured workspace: that directory is only the source of the pin, and
   every run is graded in its own throwaway tree. Without that, one
   side-effecting verifier could dirty the shared checkout, be condemned once,
   and leave every later run permanently unjudgeable — and a routine that is
   never judged is a routine that can never be auto-paused.

The shell half pins the same asymmetry at the configuration seam, where the
value is decided: ``scripts/launch-env.sh`` probes the workspace and exports it
only when a gate run would actually succeed against it.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.contracts import RunState
from omniagentos.db.store import SqliteStore
from omniagentos.policy import load_policy
from omniagentos.scheduler.gate_evidence import GateExecutionInfraError, GateWorkspaceUnusable
from omniagentos.scheduler.gate_runner import _ephemeral_run_tree, default_gate_workspace
from omniagentos.scheduler.routines_settle import settle_pending
from omniagentos.scheduler.routines_tick import tick
from omniagentos.scheduler.store import RoutinesStore, _count_settled_runs
from tests.routines.conftest import valid_routine_payload
from tests.support.db_template import make_store

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCH_ENV = REPO_ROOT / "scripts" / "launch-env.sh"

_SUITE = """
def test_one(): assert True
def test_two(): assert True
"""


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=str(root),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(root), check=True, capture_output=True
    )
    subprocess.run(["git", "add", "--all"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=str(root), check=True, capture_output=True)


def _worktree_paths(source: Path) -> list[str]:
    """Every worktree *source* registered beyond its own checkout."""
    listing = subprocess.run(
        ["git", "-C", str(source), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    roots = [line.split(" ", 1)[1] for line in listing.splitlines() if line.startswith("worktree ")]
    # Resolved: git reports /var/folders/..., the caller holds /private/var/... .
    return [str(Path(root).resolve()) for root in roots if Path(root).resolve() != source.resolve()]


def _gate_workspace(root: Path) -> Path:
    """A clean, git-backed checkout with a real (passing) verifier in it."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "suite").mkdir(parents=True, exist_ok=True)
    (root / "suite" / "test_suite.py").write_text(_SUITE, encoding="utf-8")
    (root / ".gitignore").write_text(".pytest_cache\n__pycache__\nvar\n", encoding="utf-8")
    _git_init(root)
    return root


def _seed_terminal_routine_run(db: SqliteStore) -> None:
    """One active routine with a pytest gate, fired, and its run completed."""
    routines = RoutinesStore(db)
    routine = routines.create_routine(
        valid_routine_payload(
            name="gate-workspace-probe",
            trigger_config={"cron": "* * * * *"},
            task_template={"title": "do it", "harness": "cli-grok"},
            gate_type="test_command",
            gate_config={"command": "pytest suite", "expected_exit_code": 0},
        )
    )
    tick(db, load_policy(), now=NOW)
    routine_run = routines.list_runs(routine["id"])[0]
    db.update_run(
        routine_run["run_id"],
        {"state": RunState.COMPLETED.value, "finished_at": "2026-01-01T09:00:00Z"},
    )


def _settle(db: SqliteStore) -> dict[str, object]:
    """Settle at the REAL wall clock, evaluated now and not at import.

    Gate evidence is timestamped and expires, and `evidence_rejections` rejects
    evidence dated more than 60s in the future. A frozen `now`, or one captured
    at module import and used minutes later under xdist, therefore reads
    freshly-minted evidence as future-dated and reports a gate FAILURE — the
    exact false unfavourable this file exists to keep out of the acceptance
    floor. Only the tick is frozen; it decides cron due-ness, not staleness.
    """
    return settle_pending(db, now=datetime.now(UTC))


@pytest.fixture
def _isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Evidence store and signing key under tmp; no inherited workspace."""
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("OMNIAGENTOS_GATE_WORKSPACE", raising=False)


@pytest.mark.usefixtures("_isolated_runtime")
def test_configured_workspace_settles_on_a_real_gate_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: OMNIAGENTOS_GATE_WORKSPACE set -> an actual verdict.

    Nothing about the gate chain is injected here — no workspace argument, no
    runner, no evidence store. ``settle_pending`` resolves all three from the
    environment exactly as the five-minute tick does, so this test fails if the
    default resolution path breaks for any reason, not just if the settlement
    branch does.
    """
    workspace = _gate_workspace(tmp_path / "gate-workspace")
    monkeypatch.setenv("OMNIAGENTOS_GATE_WORKSPACE", str(workspace))
    assert default_gate_workspace() == workspace

    db = make_store(SqliteStore, tmp_path / "test.db")
    _seed_terminal_routine_run(db)

    settled = _settle(db)["settled"][0]

    assert settled["stop_reason"] != "gate_evidence_unavailable", (
        "the routine settled without judging its evidence — this is the 254/254 state"
    )
    # The notes carry every rejection reason, so a red here names its own cause.
    assert settled["stop_reason"] == "gate_passed", settled["notes"]
    assert settled["gate_passed"] == 1
    assert settled["accepted"] == 1


@pytest.mark.usefixtures("_isolated_runtime")
def test_unconfigured_workspace_keeps_the_null_null_shape(tmp_path: Path) -> None:
    """Unconfigured is UNJUDGED, not failed: gate_passed/accepted stay NULL.

    a6c0cc7e made this bucket NULL so ungateable runs are excluded from the
    acceptance floor rather than counted against it. Regressing it to 0/0 would
    auto-pause every routine on an installation that simply has no workspace.
    """
    db = make_store(SqliteStore, tmp_path / "test.db")
    _seed_terminal_routine_run(db)

    assert default_gate_workspace() is None
    settled = _settle(db)["settled"][0]

    assert settled["stop_reason"] == "gate_evidence_unavailable"
    assert settled["gate_passed"] is None
    assert settled["accepted"] is None


@pytest.mark.usefixtures("_isolated_runtime")
def test_a_failing_verifier_is_reported_as_a_failure_not_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured workspace must be able to say NO as well as yes.

    A "real verdict" that can only come back favourable is not a gate. The
    workspace here holds a verifier that genuinely fails.
    """
    workspace = tmp_path / "gate-workspace"
    workspace.mkdir(parents=True)
    (workspace / "suite").mkdir()
    (workspace / "suite" / "test_suite.py").write_text(
        "def test_one(): assert False\n", encoding="utf-8"
    )
    (workspace / ".gitignore").write_text(".pytest_cache\n__pycache__\nvar\n", encoding="utf-8")
    _git_init(workspace)
    monkeypatch.setenv("OMNIAGENTOS_GATE_WORKSPACE", str(workspace))

    db = make_store(SqliteStore, tmp_path / "test.db")
    _seed_terminal_routine_run(db)

    settled = _settle(db)["settled"][0]
    assert settled["stop_reason"] == "gate_failed"
    assert settled["gate_passed"] == 0


# --- the TOCTOU seat: the workspace can go bad AFTER it was proven good -----
#
# The configuration probe runs once, when the job spawns. The gate runs minutes
# or hours later. In between, a concurrent merge, a `checkout --detach`, an
# editor, or a corrupt index can make the workspace unusable. Narrowing that
# window cannot close it; what makes the race harmless is classifying the CAUSE
# at the raise site, so "the workspace was unusable" settles as absence
# (NULL/NULL, excluded from the acceptance floor) and never as failure.


@pytest.mark.usefixtures("_isolated_runtime")
def test_a_workspace_dirtied_after_the_probe_settles_null_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clean when configured, dirty when the gate runs -> unavailable, not failed."""
    workspace = _gate_workspace(tmp_path / "gate-workspace")
    monkeypatch.setenv("OMNIAGENTOS_GATE_WORKSPACE", str(workspace))
    assert default_gate_workspace() == workspace  # the probe's view: clean

    db = make_store(SqliteStore, tmp_path / "test.db")
    _seed_terminal_routine_run(db)

    # ...and now somebody else writes in it, exactly as a merge in flight would.
    (workspace / "someone-elses-merge.txt").write_text("in flight\n", encoding="utf-8")

    settled = _settle(db)["settled"][0]

    assert settled["gate_passed"] is None, (
        "a workspace that went dirty judged NOTHING; recording gate_passed=0 "
        "invents a failure and counts it toward the 50% auto-pause floor"
    )
    assert settled["accepted"] is None
    assert settled["stop_reason"] == "gate_evidence_unavailable"
    assert settled["stop_reason"] != "gate_refused"


@pytest.mark.usefixtures("_isolated_runtime")
def test_a_workspace_that_stops_being_a_checkout_settles_null_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other environment cause: the directory is there, the checkout is not."""
    workspace = _gate_workspace(tmp_path / "gate-workspace")
    monkeypatch.setenv("OMNIAGENTOS_GATE_WORKSPACE", str(workspace))
    db = make_store(SqliteStore, tmp_path / "test.db")
    _seed_terminal_routine_run(db)

    shutil.rmtree(workspace / ".git")

    settled = _settle(db)["settled"][0]
    assert settled["gate_passed"] is None
    assert settled["stop_reason"] == "gate_evidence_unavailable"


@pytest.mark.usefixtures("_isolated_runtime")
def test_a_gate_naming_a_missing_target_is_still_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boundary of the carve-out — and the reason it is a carve-out.

    Only refusals about the WORKSPACE are forgiven. A gate that names a target
    which does not exist is a fact about the CANDIDATE, and gate_runner's module
    docstring is explicit that it is "the exact fail-open this chain exists to
    close": a verifier pointed at nothing must never be able to pass, and must
    never be able to duck the acceptance floor either. Blanket-forgiving every
    refusal would have re-opened it.
    """
    workspace = _gate_workspace(tmp_path / "gate-workspace")
    monkeypatch.setenv("OMNIAGENTOS_GATE_WORKSPACE", str(workspace))

    db = make_store(SqliteStore, tmp_path / "test.db")
    routines = RoutinesStore(db)
    routine = routines.create_routine(
        valid_routine_payload(
            name="gate-names-nothing",
            trigger_config={"cron": "* * * * *"},
            task_template={"title": "do it", "harness": "cli-grok"},
            gate_type="test_command",
            gate_config={"command": "pytest suite/test_absent.py", "expected_exit_code": 0},
        )
    )
    tick(db, load_policy(), now=NOW)
    routine_run = routines.list_runs(routine["id"])[0]
    db.update_run(
        routine_run["run_id"],
        {"state": RunState.COMPLETED.value, "finished_at": "2026-01-01T09:00:00Z"},
    )

    settled = _settle(db)["settled"][0]
    assert settled["stop_reason"] == "gate_refused"
    assert settled["gate_passed"] == 0, "a gate naming nothing must not be forgiven"


@pytest.mark.usefixtures("_isolated_runtime")
def test_doctrine_an_unusable_workspace_never_enters_the_acceptance_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DOCTRINE: a broken PIN SOURCE is not a denominator.

    The acceptance floor auto-pauses a routine whose settled runs fall below 50%
    accepted. `_count_settled_runs` IS that denominator, so the invariant is
    asserted there and not merely on one row's columns: while the workspace
    cannot be executed against, the routine's settled count stays 0 and it stays
    active. Regressing the classification turns a week of concurrent merges into
    an auto-paused fleet.

    WHAT THIS PINS, AND WHAT IT DELIBERATELY DOES NOT
    -------------------------------------------------
    Read on its own, "the gate cannot run, so nothing is ever settled" is also
    the shape of an ATTACK: a routine whose verifier poisons the checkout would
    be unjudgeable forever and therefore unpausable forever. That is why the
    gate no longer executes in this directory at all. The configured workspace
    is only the SOURCE OF THE PIN; every run is graded in its own throwaway tree,
    so nothing a verifier does can put the workspace into this state.

    The dirt below is therefore what it looks like: somebody OUTSIDE the gate —
    a merge in flight, an operator, a half-finished checkout — touching the pin
    source. Perpetual NULL now requires the pin source itself to be broken,
    which no run can cause and which the health sentinel can see (the rows are
    gate_passed IS NULL AND stop_reason='gate_evidence_unavailable'). The
    companion test, test_a_self_dirtying_verifier_condemns_only_its_own_run,
    pins the half this one must not be read as licensing.
    """
    workspace = _gate_workspace(tmp_path / "gate-workspace")
    monkeypatch.setenv("OMNIAGENTOS_GATE_WORKSPACE", str(workspace))
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    (workspace / "someone-elses-merge.txt").write_text("in flight\n", encoding="utf-8")

    db = make_store(SqliteStore, tmp_path / "test.db")
    routines = RoutinesStore(db)
    routine = routines.create_routine(
        valid_routine_payload(
            name="floor-doctrine",
            trigger_config={"cron": "* * * * *"},
            task_template={"title": "do it", "harness": "cli-grok"},
            gate_type="test_command",
            gate_config={"command": "pytest suite", "expected_exit_code": 0},
        )
    )

    for minute in range(1, 4):
        moment = NOW + timedelta(minutes=minute)
        assert tick(db, load_policy(), now=moment)["fired"], "routine must fire"
        routine_run = routines.list_runs(routine["id"])[0]
        db.update_run(
            routine_run["run_id"],
            {
                "state": RunState.COMPLETED.value,
                "finished_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        _settle(db)

    settled_runs, settled_accepted = _count_settled_runs(db._connection, routine["id"], limit=100)
    assert settled_runs == 0, "unusable-workspace runs must never be counted as settled"
    assert settled_accepted == 0

    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["status"] == "active", "the routine must not auto-pause on a dirty workspace"
    assert updated["auto_pause_reason"] == ""


# --- the execution surface: one throwaway tree per run ----------------------
#
# A verifier is planner-authored code that this repo executes. Run it twice in
# the same persistent checkout and one side-effecting verifier buys PERMANENT
# immunity from auto-pause: its own run is condemned via workspace_tree_clean,
# and every later run trips the pre-execution dirty check, which is (correctly)
# absence — so the routine can never accumulate the settled failures that
# auto-pause requires. Confirmed misbehaviour plus perpetual NULL, forever, with
# no operational alarm. Ephemeral run trees remove step two.


_SELF_DIRTYING_SUITE = """
from pathlib import Path


def test_writes_into_the_tree_it_runs_in():
    Path("poison.txt").write_text("side effect\\n", encoding="utf-8")
    assert True
"""


@pytest.mark.usefixtures("_isolated_runtime")
def test_a_self_dirtying_verifier_condemns_only_its_own_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blocker, end to end: misbehaviour is punished and does not persist.

    Run 1 dirties the tree it runs in. That is a fact about the RUN, so it stays
    condemning — gate_passed=0, in the denominator. Run 2 gets a brand new tree
    and is judged on its own merits rather than inheriting run 1's mess as
    "unjudgeable". Both runs count, so the routine remains auto-pausable, which
    is the property the attack was trying to destroy.
    """
    workspace = tmp_path / "gate-workspace"
    workspace.mkdir(parents=True)
    (workspace / "suite").mkdir()
    (workspace / "suite" / "test_suite.py").write_text(_SELF_DIRTYING_SUITE, encoding="utf-8")
    (workspace / ".gitignore").write_text(".pytest_cache\n__pycache__\nvar\n", encoding="utf-8")
    _git_init(workspace)
    monkeypatch.setenv("OMNIAGENTOS_GATE_WORKSPACE", str(workspace))
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)

    db = make_store(SqliteStore, tmp_path / "test.db")
    routines = RoutinesStore(db)
    routine = routines.create_routine(
        valid_routine_payload(
            name="self-dirtying",
            trigger_config={"cron": "* * * * *"},
            task_template={"title": "do it", "harness": "cli-grok"},
            gate_type="test_command",
            gate_config={"command": "pytest suite", "expected_exit_code": 0},
        )
    )

    outcomes = []
    for minute in (1, 2):
        moment = NOW + timedelta(minutes=minute)
        assert tick(db, load_policy(), now=moment)["fired"], "routine must fire"
        routine_run = routines.list_runs(routine["id"])[0]
        db.update_run(
            routine_run["run_id"],
            {
                "state": RunState.COMPLETED.value,
                "finished_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        outcomes.append(_settle(db)["settled"][0])

    # The tests themselves PASS; what condemns each run is that it wrote into
    # the tree it was graded in.
    for index, settled in enumerate(outcomes, start=1):
        assert settled["gate_passed"] == 0, f"run {index}: a self-dirtying verifier is a failure"
        assert settled["stop_reason"] == "gate_failed"
        assert settled["stop_reason"] != "gate_evidence_unavailable", (
            f"run {index}: poisoning must not make later runs UNJUDGEABLE — that is "
            "the permanent-immunity bug"
        )

    # The pin source never saw the write at all.
    status = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.stdout == "", "the verifier must not be able to touch the pin source"
    assert not (workspace / "poison.txt").exists()

    # And the routine stayed auto-pausable: both runs are in the denominator.
    settled_runs, settled_accepted = _count_settled_runs(db._connection, routine["id"], limit=100)
    assert settled_runs == 2, "a misbehaving routine must accumulate settled failures"
    assert settled_accepted == 0


def test_the_run_tree_is_destroyed_on_the_success_path(tmp_path: Path) -> None:
    source = _gate_workspace(tmp_path / "gate-workspace")
    sha = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    with _ephemeral_run_tree(source, sha) as tree:
        assert tree.is_dir()
        assert (tree / "suite" / "test_suite.py").is_file()
        assert _worktree_paths(source) == [str(tree.resolve())]

    assert not tree.exists()
    assert _worktree_paths(source) == [], "a leaked worktree registration is a real cost"


def test_the_run_tree_is_destroyed_when_the_body_raises(tmp_path: Path) -> None:
    """The path that actually matters: a timeout, a kill, an infra error."""
    source = _gate_workspace(tmp_path / "gate-workspace")
    sha = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    escaped: Path | None = None
    with pytest.raises(GateExecutionInfraError):
        with _ephemeral_run_tree(source, sha) as tree:
            escaped = tree
            assert tree.is_dir()
            raise GateExecutionInfraError("process group survived SIGKILL")

    assert escaped is not None
    assert not escaped.exists()
    assert _worktree_paths(source) == []


def test_a_pin_source_that_cannot_produce_a_tree_is_unusable_not_a_failure(
    tmp_path: Path,
) -> None:
    """The one remaining route to perpetual NULL, and it is an environment fact."""
    source = _gate_workspace(tmp_path / "gate-workspace")
    absent = "0" * 40

    with pytest.raises(GateWorkspaceUnusable):
        with _ephemeral_run_tree(source, absent):
            pass  # pragma: no cover -- materialization must fail first

    assert _worktree_paths(source) == []


def test_the_run_tree_is_not_created_beside_the_named_worktrees(tmp_path: Path) -> None:
    """Hygiene: throwaway trees live in TMPDIR, never in the repo's parent.

    This box already carries ~30 named worktrees next to the checkout. A gate
    tree materialised there is one somebody eventually mistakes for a lane.
    """
    source = _gate_workspace(tmp_path / "gate-workspace")
    sha = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    with _ephemeral_run_tree(source, sha) as tree:
        resolved = tree.resolve()
        assert source.resolve().parent not in resolved.parents
        assert resolved.is_relative_to(Path(tempfile.gettempdir()).resolve())


# --- the configuration seam: scripts/launch-env.sh --------------------------


def _sourced_gate_workspace(repo_root: Path, preset: str | None = None) -> str:
    """Source the copied launch-env.sh and report what it exported."""
    preset_line = f'export OMNIAGENTOS_GATE_WORKSPACE="{preset}"\n' if preset else ""
    script = (
        "set -eu\n"
        "unset OMNIAGENTOS_GATE_WORKSPACE OMNIAGENTOS_LAUNCH_ENV_LOADED || true\n"
        f"{preset_line}"
        f'. "{repo_root / "scripts" / "launch-env.sh"}"\n'
        'printf "%s\\n" "${OMNIAGENTOS_GATE_WORKSPACE:-<unset>}"\n'
    )
    return subprocess.check_output(["bash", "-c", script], text=True).strip().splitlines()[-1]


@pytest.fixture
def _fake_checkout(tmp_path: Path) -> Path:
    """A stand-in repo root holding a real copy of launch-env.sh."""
    repo = tmp_path / "OmniAgentOS"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(LAUNCH_ENV, repo / "scripts" / "launch-env.sh")
    return repo


def test_launch_env_leaves_the_workspace_unset_when_there_is_none(_fake_checkout: Path) -> None:
    assert _sourced_gate_workspace(_fake_checkout) == "<unset>"


def test_launch_env_exports_a_clean_sibling_gate_checkout(_fake_checkout: Path) -> None:
    workspace = _gate_workspace(Path(f"{_fake_checkout}-gate"))
    assert _sourced_gate_workspace(_fake_checkout) == str(workspace)


def test_launch_env_refuses_a_dirty_gate_checkout(_fake_checkout: Path) -> None:
    """A pin source that yields no verdicts must not be published as one.

    PytestGateRunner refuses a dirty workspace, and that refusal is classified
    as absence — the run settles NULL and leaves the acceptance floor. So the
    cost of exporting a dirty checkout is not false failures, it is SILENCE:
    every tick produces a run nobody judged, which is indistinguishable from the
    unconfigured 254/254 state this variable exists to end, except that it looks
    configured. One untracked file is enough for the runner to refuse, so one
    untracked file must be enough here.
    """
    workspace = _gate_workspace(Path(f"{_fake_checkout}-gate"))
    (workspace / "stray.txt").write_text("someone else is writing here\n", encoding="utf-8")

    assert _sourced_gate_workspace(_fake_checkout) == "<unset>"

    (workspace / "stray.txt").unlink()
    (workspace / "suite" / "test_suite.py").write_text("def test_one(): assert 0\n", "utf-8")
    assert _sourced_gate_workspace(_fake_checkout) == "<unset>", (
        "a tracked-but-modified tree is refused by the runner too"
    )


def test_launch_env_refuses_a_checkout_whose_git_status_fails(_fake_checkout: Path) -> None:
    """Empty stdout from a FAILING git is not a clean tree.

    A corrupt index makes `git status` exit 128 while writing nothing to stdout,
    and `git rev-parse HEAD` still succeeds because it only reads refs. A probe
    that tests output emptiness alone therefore reads the most broken possible
    checkout as the cleanest one and exports it — and the runner then refuses it.
    The exit status has to be checked too, exactly as the runner checks
    `returncode != 0 or stdout.strip()`.
    """
    workspace = _gate_workspace(Path(f"{_fake_checkout}-gate"))
    (workspace / ".git" / "index").write_bytes(b"CORRUPT-NOT-AN-INDEX")

    # Precondition: this is the shape the naive probe would misread.
    status = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert status.returncode != 0
    assert status.stdout == ""

    assert _sourced_gate_workspace(_fake_checkout) == "<unset>"


def test_launch_env_refuses_a_gate_directory_that_is_not_a_checkout(_fake_checkout: Path) -> None:
    sibling = Path(f"{_fake_checkout}-gate")
    (sibling / "suite").mkdir(parents=True)
    (sibling / "suite" / "test_suite.py").write_text(_SUITE, encoding="utf-8")

    assert _sourced_gate_workspace(_fake_checkout) == "<unset>"


def test_launch_env_honours_an_operator_preset_unprobed(_fake_checkout: Path) -> None:
    """A preset is an operator decision and wins, as every other knob here does."""
    _gate_workspace(Path(f"{_fake_checkout}-gate"))
    chosen = "/somewhere/an/operator/picked"
    assert _sourced_gate_workspace(_fake_checkout, preset=chosen) == chosen
