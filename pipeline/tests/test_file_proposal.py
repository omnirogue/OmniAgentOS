"""Pins bridge/file_proposal.py — the write-time gate on a proposal's `paths`.

The defect this file exists to keep closed: a proposal with an empty `paths`
used to be written happily by every machine in the system and then refused by
the Implementer, because `paths` is what its parallel-landing conflict graph is
built from. 13 of 36 queued proposals carried an empty one.

Every test here asserts the SAME two things about a refusal: the exit code, and
that **nothing was written**. A gate that refuses in its output but leaves a
degraded artifact on disk is the failure mode being prevented, not a partial
success.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG / "bridge"))

import file_proposal  # noqa: E402
from canonical import content_id  # noqa: E402

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def queue(tmp_path: Path) -> Path:
    q = tmp_path / "loopqueue"
    for sub in ("proposals", "rejected", "parked"):
        (q / sub).mkdir(parents=True)
    (q / "ledger.jsonl").touch()
    return q


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A checkout with two real files and one real directory."""
    r = tmp_path / "repo"
    (r / "bridge").mkdir(parents=True)
    (r / "bridge" / "governor.py").write_text("# real\n")
    (r / "CONTRACT.md").write_text("# real\n")
    (r / "tests").mkdir()
    return r


def draft(**over) -> dict:
    payload = {
        "problem": "an observable thing is wrong",
        "implementation_plan": "1. do the thing",
    }
    payload.update(over.pop("payload", {}))
    art = {
        "contract": "v1.1",
        "kind": "proposal",
        "title": "a plan",
        "created_at": "2026-08-08T00:00:00Z",
        "producer": {"role": "planner", "actor": "test"},
        "paths": ["bridge/governor.py"],
        "payload": payload,
    }
    art.update(over)
    art["id"] = content_id(art["payload"])
    return art


def run(art: dict, tmp_path: Path, queue: Path, root: Path, *extra) -> tuple[int, str]:
    """Run the tool in-process and return (exit code, stderr)."""
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(art))
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code = file_proposal.main([str(path), "--queue", str(queue),
                                   "--root", f"repo={root}", *extra])
    return code, err.getvalue()


def written(queue: Path) -> list[Path]:
    return sorted((queue / "proposals").glob("*.json"))


# --------------------------------------------------------------------------
# the core defect: paths
# --------------------------------------------------------------------------

def test_empty_paths_is_refused_and_nothing_is_written(tmp_path, queue, root):
    code, err = run(draft(paths=[]), tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "paths.present" in err
    assert "WRONG schedule" in err
    assert written(queue) == [], "a refused proposal must never reach proposals/"


def test_absent_paths_is_refused(tmp_path, queue, root):
    art = draft()
    del art["paths"]
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "paths.present" in err
    assert written(queue) == []


def test_refusal_names_its_own_remedy(tmp_path, queue, root):
    """A refusal that does not say how to fix itself sends the next agent to
    debug the wrong thing."""
    _, err = run(draft(paths=[]), tmp_path, queue, root)
    assert "REMEDY:" in err
    assert "--derive" in err


@pytest.mark.parametrize("bad,expected", [
    ("/Users/owner/repo/bridge/governor.py", "absolute"),
    ("~/repo/bridge/governor.py", "absolute"),
    ("../outside/thing.py", ".."),
    ("bridge/", "directory"),
    ("bridge/*.py", "glob"),
    (".", "repo root"),
    ("./", "directory"),
])
def test_path_shapes_that_cannot_be_graph_nodes_are_refused(bad, expected,
                                                            tmp_path, queue, root):
    code, err = run(draft(paths=[bad]), tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "paths.shape" in err
    assert expected in err
    assert written(queue) == []


def test_a_path_that_names_nothing_is_refused(tmp_path, queue, root):
    """The pre-rename-name trap: rejection sha256:d12b0fdc named
    PROMPT-repair-loop.md long after commit 0000000 renamed it."""
    code, err = run(draft(paths=["PROMPT-repair-loop.md"]), tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "paths.reality" in err
    assert "PROMPT-repair-loop.md" in err
    assert written(queue) == []


def test_a_real_repo_root_dotfile_is_accepted(tmp_path, queue, root, monkeypatch):
    monkeypatch.setattr(file_proposal, "default_roots", lambda: {})
    (root / ".gitignore").write_text("*.pyc\n")
    code, err = run(draft(paths=[".gitignore"]), tmp_path, queue, root)
    assert code == file_proposal.EXIT_WRITTEN, err
    assert "paths.reality" not in err
    assert "lanes." not in err
    assert "more than one root" not in err
    files = written(queue)
    assert len(files) == 1
    body = json.loads(files[0].read_text())
    assert body["paths"] == [".gitignore"]


def test_a_nonexistent_dotfile_is_still_refused_by_its_real_name(tmp_path, queue, root):
    code, err = run(draft(paths=[".env.does-not-exist"]), tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "paths.reality" in err
    assert ".env.does-not-exist" in err
    assert written(queue) == []


def test_a_new_file_is_allowed_when_declared(tmp_path, queue, root):
    """`paths` means every file TOUCHED, which includes files created. Refusing
    those would refuse most real plans — the reality check must not become a
    'file must already exist' check."""
    art = draft(paths=["tests/test_new_thing.py"],
                payload={"new_paths": ["tests/test_new_thing.py"]})
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_WRITTEN, err
    assert len(written(queue)) == 1
    body = json.loads(written(queue)[0].read_text())
    assert body["paths_resolved"]["tests/test_new_thing.py"] == "new"


def test_a_new_dotfile_declared_in_new_paths_is_accepted(tmp_path, queue, root):
    art = draft(paths=[".tl-new-file"], payload={"new_paths": [".tl-new-file"]})
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_WRITTEN, err
    assert len(written(queue)) == 1
    body = json.loads(written(queue)[0].read_text())
    assert body["paths_resolved"][".tl-new-file"] == "new"


def test_new_paths_must_also_appear_in_paths(tmp_path, queue, root):
    art = draft(payload={"new_paths": ["tests/orphan.py"]})
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "paths.new_paths" in err
    assert written(queue) == []


def test_nested_roots_get_one_canonical_spelling(tmp_path, queue, root):
    """ThreeLoops lives inside ~/Ops, so one file has two repo-relative
    names. The graph keys on the string, so two spellings never collide — the
    dangerous direction. Only the deepest root's spelling is accepted."""
    outer = root.parent
    (outer / "marker").write_text("x")  # make `outer` a plausible root
    code, err = run(draft(paths=["repo/bridge/governor.py"]), tmp_path, queue, root,
                    "--root", f"outer={outer}")
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "paths.spelling" in err
    assert "'bridge/governor.py'" in err, "the refusal must name the spelling to use"
    assert written(queue) == []


def test_the_canonical_spelling_is_accepted(tmp_path, queue, root):
    outer = root.parent
    code, err = run(draft(paths=["bridge/governor.py"]), tmp_path, queue, root,
                    "--root", f"outer={outer}")
    assert code == file_proposal.EXIT_WRITTEN, err


def test_default_roots_cover_the_surface_people_actually_edit(monkeypatch):
    """Ten of the thirteen empty-`paths` proposals were host-ops work whose files
    live outside any git repo. With no root covering them, a planner's only
    options were an empty `paths` or a lie."""
    roots = file_proposal.default_roots()
    home = Path.home()
    for expected in (home / "Work" / "Ops", home / "Library" / "LaunchAgents"):
        if expected.is_dir():
            assert expected.resolve() in roots.values(), f"{expected} is not addressable"


def test_converged_pipeline_is_not_a_second_nested_repo_root(tmp_path, monkeypatch):
    repo = tmp_path / "OmniAgentOS"
    target = repo / "pipeline" / "bridge" / "governor.py"
    target.parent.mkdir(parents=True)
    target.write_text("# real\n")
    monkeypatch.setenv("LOOP_WORKDIR", str(repo))
    roots = file_proposal.default_roots()
    assert repo.resolve() in roots.values()
    assert (repo / "pipeline").resolve() not in roots.values()


# --------------------------------------------------------------------------
# off-repo: a KNOWN root is not necessarily a LANDABLE one
# --------------------------------------------------------------------------
#
# The surviving half of the undrainable-WIP class: `paths.reality` (above)
# already refuses a path that exists in NO known root. It never refused a
# path that exists ONLY in a known-but-not-landable root — e.g. a file that
# is real, under ~/Ops, but nothing this Implementer merges ever reaches
# it. That draft used to file cleanly, get admitted, and charge wip_cap
# forever with no way to drain. These tests pin the fix at the point that
# matters: a path resolving ONLY under a non-landable root is refused; a path
# resolving under the landable repo (workdir, or a caller-declared --root) is
# not.

def test_off_repo_path_is_refused_and_nothing_is_written(tmp_path, queue, root,
                                                          monkeypatch):
    """probe B from the proposal: paths=['ThreeLoops/bridge/janitor.py']
    resolving solely under a non-landable root must REFUSE, not file."""
    ops = tmp_path / "ops-checkout"
    (ops / "ThreeLoops" / "bridge").mkdir(parents=True)
    (ops / "ThreeLoops" / "bridge" / "janitor.py").write_text("# real\n")
    monkeypatch.setattr(file_proposal, "default_roots", lambda: {"Ops": ops.resolve()})
    code, err = run(draft(paths=["ThreeLoops/bridge/janitor.py"]), tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "paths.off_repo" in err
    assert "ThreeLoops/bridge/janitor.py" in err, "the refusal must name the offending path"
    assert "Ops" in err, "the refusal must name the root it actually resolved under"
    assert written(queue) == []


def test_off_repo_refusal_names_the_landable_root_and_the_root_flag_remedy(
        tmp_path, queue, root, monkeypatch):
    ops = tmp_path / "ops-checkout"
    (ops / "ThreeLoops").mkdir(parents=True)
    (ops / "ThreeLoops" / "janitor.py").write_text("# real\n")
    monkeypatch.setattr(file_proposal, "default_roots", lambda: {"Ops": ops.resolve()})
    _, err = run(draft(paths=["ThreeLoops/janitor.py"]), tmp_path, queue, root)
    assert "REMEDY:" in err
    assert "--root" in err


def test_off_repo_refused_path_is_never_silently_normalised_or_dropped(
        tmp_path, queue, root, monkeypatch):
    """A refusal must be loud, not a silent drop — the artifact must not
    exist, and the process must exit non-zero, not just warn."""
    ops = tmp_path / "ops-checkout"
    ops.mkdir()
    (ops / "janitor.py").write_text("# real\n")
    monkeypatch.setattr(file_proposal, "default_roots", lambda: {"Ops": ops.resolve()})
    code, err = run(draft(paths=["janitor.py"]), tmp_path, queue, root)
    assert code != file_proposal.EXIT_WRITTEN
    assert "NOT WRITTEN" in err
    assert written(queue) == []


def test_check_parity_off_repo_refusal_reports_the_same_way_and_writes_nothing(
        tmp_path, queue, root, monkeypatch):
    """--check must not diverge from the real run: same refusal, same exit
    code, still nothing written (--check never writes anyway, but a filing
    tool that only refuses on the REAL run is the favourable-absence trap
    moved one step earlier)."""
    ops = tmp_path / "ops-checkout"
    (ops / "ThreeLoops" / "bridge").mkdir(parents=True)
    (ops / "ThreeLoops" / "bridge" / "janitor.py").write_text("# real\n")
    monkeypatch.setattr(file_proposal, "default_roots", lambda: {"Ops": ops.resolve()})
    art = draft(paths=["ThreeLoops/bridge/janitor.py"])
    code_check, err_check = run(art, tmp_path, queue, root, "--check")
    code_real, err_real = run(art, tmp_path, queue, root)
    assert code_check == code_real == file_proposal.EXIT_REFUSED_FIXABLE
    assert "paths.off_repo" in err_check
    assert "paths.off_repo" in err_real
    assert written(queue) == []


def test_landable_root_workdir_paths_file_without_a_root_flag(tmp_path, queue, monkeypatch):
    """The repo the queue lives in (LOOP_WORKDIR) is landable BY DEFAULT — no
    --root needed. This is the positive case the off-repo refusal must never
    catch."""
    workdir = tmp_path / "workdir-repo"
    (workdir / "bridge").mkdir(parents=True)
    (workdir / "bridge" / "governor.py").write_text("# real\n")
    monkeypatch.setattr(file_proposal, "default_roots",
                        lambda: {"workdir-repo": workdir.resolve()})
    monkeypatch.setenv("LOOP_WORKDIR", str(workdir))
    art = draft(paths=["bridge/governor.py"])
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(art))
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code = file_proposal.main([str(draft_path), "--queue", str(queue)])
    assert code == file_proposal.EXIT_WRITTEN, err.getvalue()
    assert "paths.off_repo" not in err.getvalue()
    assert len(written(queue)) == 1


def test_landable_root_extra_root_flag_checkout_still_files(tmp_path, queue, root):
    """A caller who names an extra checkout via --root is declaring it
    landable explicitly — that legitimate multi-root filing must keep
    working exactly as before this fix."""
    code, err = run(draft(paths=["bridge/governor.py"]), tmp_path, queue, root)
    assert code == file_proposal.EXIT_WRITTEN, err
    assert "paths.off_repo" not in err
    assert len(written(queue)) == 1


def test_paths_reality_regression_neither_resolves_still_refuses(tmp_path, queue, root):
    """probe A from the proposal: a path in NO known root at all must still
    refuse via paths.reality — the off-repo fix layers on top of this check,
    it does not replace it."""
    code, err = run(draft(paths=["no/such/repo/path/definitely_absent_zz1.py"]),
                    tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "paths.reality" in err
    assert "paths.off_repo" not in err
    assert written(queue) == []


def test_landable_root_names_grants_workdir_and_declared_extras_only(tmp_path, monkeypatch):
    """Unit-level pin on landable_root_names itself: the workdir root is
    landable by default; a merely-known root (Ops-shaped) is not, unless the
    caller names it via extra (what --root does at the call site)."""
    workdir = tmp_path / "wd"
    ops = tmp_path / "ops"
    workdir.mkdir()
    ops.mkdir()
    monkeypatch.setenv("LOOP_WORKDIR", str(workdir))
    roots = {"wd": workdir.resolve(), "Ops": ops.resolve()}
    assert file_proposal.landable_root_names(roots) == {"wd"}
    assert file_proposal.landable_root_names(roots, extra=["Ops"]) == {"wd", "Ops"}


# --------------------------------------------------------------------------
# the host-config hatch: declared and enumerated, never inferred from absence
# --------------------------------------------------------------------------

def _host(**extra):
    payload = {"landing_surface": "host-config",
               "touches": ["System Settings > Spotlight > Privacy",
                           "~/Library/LaunchAgents/com.x.plist"],
               "landing_surface_rationale": "no code, no merge train"}
    payload.update(extra)
    return draft(paths=[], payload=payload)


def test_declared_host_config_work_may_have_no_repo_paths(tmp_path, queue, root):
    """Refusing this would refuse real work — proposal sha256:e413dda1 is
    Spotlight privacy entries and Backblaze excludes, and says so itself. A
    gate that refuses valid work gets bypassed, and then it protects nothing."""
    code, err = run(_host(), tmp_path, queue, root)
    assert code == file_proposal.EXIT_WRITTEN, err
    assert len(written(queue)) == 1
    assert "SERIALISE" in err, "no conflict-graph nodes must be said out loud"


def test_host_config_without_touches_is_refused(tmp_path, queue, root):
    art = _host()
    del art["payload"]["touches"]
    art["id"] = content_id(art["payload"])
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "host_config.touches" in err
    assert written(queue) == []


def test_host_config_without_rationale_is_refused(tmp_path, queue, root):
    art = _host()
    del art["payload"]["landing_surface_rationale"]
    art["id"] = content_id(art["payload"])
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "host_config.rationale" in err
    assert written(queue) == []


def test_the_hatch_cannot_be_reached_by_leaving_paths_out(tmp_path, queue, root):
    """The whole point: an absent `paths` is never read as an exemption."""
    code, err = run(draft(paths=[]), tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "paths.present" in err


def test_schema_also_holds_the_hatch_shut(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((PKG / "schema" / "envelope.schema.json").read_text())
    art = _host()
    del art["payload"]["touches"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(art, schema)
    jsonschema.validate(_host(), schema)


# --------------------------------------------------------------------------
# lanes are the partition of the graph
# --------------------------------------------------------------------------

def test_lane_with_empty_paths_is_refused(tmp_path, queue, root):
    art = draft(payload={"lanes": [{"lane": "a", "paths": []}]})
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "lanes.paths" in err
    assert written(queue) == []


def test_lane_path_missing_from_top_level_is_refused(tmp_path, queue, root):
    art = draft(paths=["bridge/governor.py"],
                payload={"lanes": [{"lane": "a",
                                    "paths": ["bridge/governor.py", "CONTRACT.md"]}]})
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "lanes.coverage" in err
    assert "CONTRACT.md" in err
    assert written(queue) == []


def test_top_level_path_owned_by_no_lane_is_refused(tmp_path, queue, root):
    art = draft(paths=["bridge/governor.py", "CONTRACT.md"],
                payload={"lanes": [{"lane": "a", "paths": ["bridge/governor.py"]}]})
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "belong to no lane" in err
    assert written(queue) == []


def test_matching_lanes_are_accepted(tmp_path, queue, root):
    art = draft(paths=["bridge/governor.py", "CONTRACT.md"],
                payload={"lanes": [{"lane": "a", "paths": ["bridge/governor.py"]},
                                   {"lane": "b", "paths": ["CONTRACT.md"]}]})
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_WRITTEN, err
    assert len(written(queue)) == 1


def test_dotfile_lanes_cover_top_level_paths(tmp_path, queue, root):
    (root / ".gitignore").write_text("*.pyc\n")
    art = draft(paths=[".gitignore", "CONTRACT.md"],
                payload={"lanes": [{"lane": "a", "paths": [".gitignore"]},
                                   {"lane": "b", "paths": ["CONTRACT.md"]}]})
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_WRITTEN, err
    assert "lanes.coverage" not in err
    assert len(written(queue)) == 1


# --------------------------------------------------------------------------
# identity, duplicates, live rejections
# --------------------------------------------------------------------------

def test_id_that_is_not_the_payload_hash_is_refused_with_the_right_one(
        tmp_path, queue, root):
    art = draft()
    real = art["id"]
    art["id"] = "sha256:" + "0" * 64
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "id.hash" in err
    assert real in err, "the refusal must hand back the id to use"
    assert written(queue) == []


def test_duplicate_id_is_do_not_retry(tmp_path, queue, root):
    art = draft()
    (queue / "proposals" / (art["id"].replace(":", "_") + ".json")).write_text("{}")
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_DO_NOT_RETRY
    assert "id.duplicate" in err


def test_live_rejection_is_do_not_retry(tmp_path, queue, root):
    art = draft()
    (queue / "rejected" / (art["id"].replace(":", "_") + ".json")).write_text(json.dumps({
        "id": art["id"], "expires_at": "2099-01-01T00:00:00Z",
        "remedy": "replan", "class": "candidate-defect", "reason": "empty paths"}))
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_DO_NOT_RETRY
    assert "id.live_rejected" in err
    assert "supersedes" in err, "the remedy must explain how a replan differs from a retry"
    assert written(queue) == []


def test_expired_rejection_does_not_block(tmp_path, queue, root):
    art = draft()
    (queue / "rejected" / (art["id"].replace(":", "_") + ".json")).write_text(json.dumps({
        "id": art["id"], "expires_at": "2000-01-01T00:00:00Z", "remedy": "replan"}))
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_WRITTEN, err
    assert "EXPIRED" in err
    assert len(written(queue)) == 1


def test_live_park_is_do_not_retry(tmp_path, queue, root):
    art = draft()
    (queue / "parked" / (art["id"].replace(":", "_") + ".json")).write_text(json.dumps({
        "id": art["id"], "expires_at": "2099-01-01T00:00:00Z", "remedy": "park"}))
    code, _ = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_REFUSED_DO_NOT_RETRY


def test_expiry_uses_utc_epoch_in_non_utc_timezone(monkeypatch):
    # time.mktime interprets a struct_time as LOCAL wall time; _expired's
    # inputs are always UTC (Z) stamps, so under a non-UTC TZ a mktime-based
    # comparison misreads a live future expiry as already expired. This must
    # fail on any implementation of _expired that routes through time.mktime.
    import time as _time

    prev_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    if hasattr(_time, "tzset"):
        _time.tzset()
    try:
        now = _time.time()
        future_stamp = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now + 30 * 60))
        past_stamp = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 30 * 60))
        assert file_proposal._expired(future_stamp) is False, (
            "a UTC expiry 30 minutes in the future must remain live")
        assert file_proposal._expired(past_stamp) is True, (
            "a UTC expiry 30 minutes in the past must be expired")
    finally:
        if prev_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev_tz
        if hasattr(_time, "tzset"):
            _time.tzset()


# --------------------------------------------------------------------------
# derive rather than ask
# --------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A checkout with a base commit and a branch touching two files."""
    r = tmp_path / "gitrepo"
    (r / "bridge").mkdir(parents=True)
    _git_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True,
                   capture_output=True)
    (r / "bridge" / "governor.py").write_text("a\n")
    (r / "CONTRACT.md").write_text("a\n")
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "base"], check=True,
                   capture_output=True, env=_git_env)
    base = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(r), "checkout", "-qb", "lane/x"], check=True,
                   capture_output=True)
    (r / "bridge" / "governor.py").write_text("b\n")
    (r / "CONTRACT.md").write_text("b\n")
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "work"], check=True,
                   capture_output=True, env=_git_env)
    return r, base, "lane/x"


def test_understating_a_branch_diff_is_refused(tmp_path, queue, git_repo):
    """The check the Implementer runs against a candidate, moved to write time."""
    repo, base, branch = git_repo
    art = draft(paths=["bridge/governor.py"], base_sha=base, branch=branch)
    code, err = run(art, tmp_path, queue, repo)
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "paths.understated" in err
    assert "CONTRACT.md" in err
    assert written(queue) == []


def test_derive_fills_paths_from_the_branch(tmp_path, queue, git_repo):
    repo, base, branch = git_repo
    art = draft(paths=["bridge/governor.py"], base_sha=base, branch=branch)
    code, err = run(art, tmp_path, queue, repo, "--derive")
    assert code == file_proposal.EXIT_WRITTEN, err
    body = json.loads(written(queue)[0].read_text())
    assert body["paths"] == ["CONTRACT.md", "bridge/governor.py"]
    assert "paths_derived_from" in body


def test_derive_with_nothing_to_derive_from_refuses_rather_than_writing_empty(
        tmp_path, queue, root):
    """The favourable-absence guard: 'could not derive' must never become
    'derived nothing, wrote nothing in paths, exit 0'."""
    code, err = run(draft(), tmp_path, queue, root, "--derive")
    assert code == file_proposal.EXIT_REFUSED_FIXABLE
    assert "derive" in err
    assert written(queue) == []


# --------------------------------------------------------------------------
# the write itself
# --------------------------------------------------------------------------

def test_happy_path_writes_atomically_and_ledgers(tmp_path, queue, root):
    art = draft(paths=["bridge/governor.py", "CONTRACT.md"])
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_WRITTEN, err
    files = written(queue)
    assert len(files) == 1
    assert list((queue / "proposals").glob("*.tmp")) == [], "no tmp left behind"
    body = json.loads(files[0].read_text())
    assert body["paths"] == ["CONTRACT.md", "bridge/governor.py"] or \
           sorted(body["paths"]) == ["CONTRACT.md", "bridge/governor.py"]
    events = [
        json.loads(line)
        for line in (queue / "ledger.jsonl").read_text().splitlines()
        if line
    ]
    assert events[-1]["event"] == "proposed"
    assert events[-1]["id"] == art["id"]


def test_check_mode_writes_nothing(tmp_path, queue, root):
    code, err = run(draft(), tmp_path, queue, root, "--check")
    assert code == file_proposal.EXIT_WRITTEN, err
    assert written(queue) == []
    assert (queue / "ledger.jsonl").read_text() == ""


def test_non_proposal_is_not_this_tools_job(tmp_path, queue, root):
    art = draft()
    art["kind"] = "finding"
    code, err = run(art, tmp_path, queue, root)
    assert code == file_proposal.EXIT_COULD_NOT_RUN
    assert written(queue) == []


def test_every_printed_refuse_code_matches_the_process_exit(tmp_path, queue, root):
    """Two carriers for one verdict must agree. AccurateGate's receipts record
    exit_code 4 while the process exits 2, and every reader since has had to be
    told which one to believe."""
    art = draft(paths=[])
    (queue / "proposals" / (art["id"].replace(":", "_") + ".json")).write_text("{}")
    code, err = run(art, tmp_path, queue, root)
    printed = {int(m) for m in __import__("re").findall(r"REFUSE\[(\d+)\]", err)}
    assert printed, "a refusal must print at least one REFUSE[n] line"
    assert code == max(printed)


# --------------------------------------------------------------------------
# the schema must refuse it too — not only this tool
# --------------------------------------------------------------------------

def test_schema_itself_rejects_an_empty_paths_proposal(tmp_path):
    """If the only thing enforcing `paths` were this tool, a model could bypass
    it by writing the file directly — which is exactly how the defect happened."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((PKG / "schema" / "envelope.schema.json").read_text())
    art = draft(paths=[])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(art, schema)


def test_schema_rejects_a_lane_with_no_paths(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((PKG / "schema" / "envelope.schema.json").read_text())
    art = draft(payload={"lanes": [{"lane": "a", "paths": []}]})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(art, schema)


def test_schema_still_accepts_a_well_formed_proposal(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((PKG / "schema" / "envelope.schema.json").read_text())
    jsonschema.validate(draft(), schema)
