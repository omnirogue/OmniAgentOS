"""Binding regressions for the pipeline-native builder worklist."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

PIPELINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE))

from bridge import canonical, claim  # noqa: E402
from bridge import spawn_builders as sb  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    (root / "README.md").write_text("baseline\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "baseline")
    return root


@pytest.fixture
def loops_root(repo: Path) -> Path:
    root = repo / "var" / "loopqueue"
    for name in ("claims", "state", "candidates", "proposals", "parked"):
        (root / name).mkdir(parents=True)
    (root / "ledger.jsonl").touch()
    (root / "state" / "landers.json").write_text(json.dumps({
        "repo": "test", "last_tick_ts": sb._iso(datetime.now(UTC)),
        "status": "ok", "pid": 1,
    }))
    return root


def _proposal(tag: str, *, paths: object = None, priority: int = 0) -> dict:
    payload = {
        "urgency": "p1", "benefit_class": "throughput", "impact": "high",
        "risk_level": "medium", "problem": f"build {tag}",
        "falsifier": f"{tag} is implemented", "implementation_plan": "implement it",
        "effort": "s", "new_paths": [], "repo": "pipeline",
    }
    ident = canonical.content_id(payload)
    return {
        "contract": "v1.1", "kind": "proposal", "title": f"proposal {tag}",
        "created_at": "2026-08-10T00:00:00Z",
        "producer": {"role": "external", "actor": "test", "lineage": "test"},
        "paths": ["README.md"] if paths is None else paths,
        "payload": payload, "id": ident, "priority": priority,
    }


def _write_proposal(root: Path, item: dict, *, filename: str | None = None) -> Path:
    name = filename or f"{item['id'].replace(':', '_', 1)}.json"
    path = root / "proposals" / name
    path.write_text(json.dumps(item))
    return path


def _select(root: Path) -> list[dict]:
    return sb._admitted_unclaimed(root, persist_alerts=False, alerts=[])


def _candidate(*, resolves: object, cand_id: str | None = None,
               filename: str | None = None) -> dict:
    payload = {"resolves": resolves, "lane": "lane/scratch", "head_sha": "b" * 40}
    ident = cand_id or canonical.content_id(payload)
    return {"contract": "v1.1", "kind": "candidate", "id": ident,
            "title": "candidate", "created_at": "2026-08-11T00:00:00Z",
            "producer": {"role": "implementer", "actor": "test"},
            "base_sha": "a" * 40, "head_sha": "b" * 40, "branch": "lane/scratch",
            "paths": ["README.md"], "evidence": [], "payload": payload}


def _write_candidate(root: Path, item: dict, *, filename: str | None = None) -> Path:
    name = filename or f"{item['id'].replace(':', '_', 1)}.json"
    path = root / "candidates" / name
    path.write_text(json.dumps(item))
    return path


def _events(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / "ledger.jsonl").read_text().splitlines()
            if line.strip()]


def _age_worklist(repo: Path) -> None:
    old = sb._iso(datetime.now(UTC) - timedelta(seconds=sb.ACK_GRACE_SECONDS + 1))
    for entry in sb._worklist_entries(repo):
        marker = Path(entry["worktree"]) / sb.MARKER_NAME
        body = json.loads(marker.read_text())
        body["provisioned_at"] = old
        marker.write_text(json.dumps(body))


def test_real_proposal_shape_uses_full_sha256_ids(loops_root: Path) -> None:
    item = _proposal("shape")
    _write_proposal(loops_root, item)
    assert len(item["id"]) == len("sha256:") + 64
    assert [row["id"] for row in _select(loops_root)] == [item["id"]]


@pytest.mark.parametrize("event", ["merged", "completed", "rejected"])
def test_terminal_ledger_ids_are_excluded(loops_root: Path, event: str) -> None:
    item = _proposal(event)
    _write_proposal(loops_root, item)
    detail = ({"merge_sha": "a" * 40} if event == "merged" else
              {"reason": "done"} if event == "completed" else
              {"reason": "no", "class": "candidate-defect",
               "expires_at": "2026-08-20T00:00:00Z"})
    with (loops_root / "ledger.jsonl").open("a") as handle:
        handle.write(json.dumps({"ts": "2026-08-10T00:00:00Z", "role": "implementer",
                                 "event": event, "id": item["id"], "detail": detail}) + "\n")
    assert _select(loops_root) == []


def test_live_resolver_candidate_withholds_proposal_without_claim(loops_root: Path) -> None:
    proposal = _proposal("live-resolver")
    _write_proposal(loops_root, proposal)
    candidate = _candidate(resolves=proposal["id"])
    _write_candidate(loops_root, candidate)
    assert _select(loops_root) == []


def test_rejected_resolver_reopens_proposal(loops_root: Path) -> None:
    proposal = _proposal("rejected-resolver")
    _write_proposal(loops_root, proposal)
    candidate = _candidate(resolves=proposal["id"])
    _write_candidate(loops_root, candidate)
    with (loops_root / "ledger.jsonl").open("a") as handle:
        handle.write(json.dumps({
            "ts": "2026-08-11T00:00:00Z", "role": "implementer", "event": "rejected",
            "id": candidate["id"],
            "detail": {"reason": "no", "class": "candidate-defect",
                      "expires_at": "2026-08-20T00:00:00Z"},
        }) + "\n")
    assert [row["id"] for row in _select(loops_root)] == [proposal["id"]]


def test_legacy_resolver_list_withholds_each_named_proposal(loops_root: Path) -> None:
    p1 = _proposal("legacy-a")
    p2 = _proposal("legacy-b")
    p3 = _proposal("legacy-c", paths=["docs/other.md"])
    _write_proposal(loops_root, p1)
    _write_proposal(loops_root, p2)
    _write_proposal(loops_root, p3)
    candidate = _candidate(resolves=[p1["id"], p2["id"], "not-a-valid-id"])
    _write_candidate(loops_root, candidate)
    remaining = {row["id"] for row in _select(loops_root)}
    assert remaining == {p3["id"]}


def test_corrupt_candidate_alerts_without_starving_valid_selection(loops_root: Path) -> None:
    live = _proposal("corrupt-live")
    other = _proposal("corrupt-other", paths=["docs/other.md"])
    _write_proposal(loops_root, live)
    _write_proposal(loops_root, other)
    resolver = _candidate(resolves=live["id"])
    _write_candidate(loops_root, resolver)
    (loops_root / "candidates" / "sha256_deadbeef0000000000000000000000000000000000000000000000000000dead.json").write_text(
        "{not valid json")
    alerts: list[str] = []
    items = sb._admitted_unclaimed(loops_root, persist_alerts=False, alerts=alerts)
    remaining = {row["id"] for row in items}
    assert remaining == {other["id"]}
    assert any("corrupt" in a or "unreadable" in a for a in alerts)


def test_traversal_id_never_reaches_claim_acquire(
        loops_root: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = _proposal("traversal")
    bad["id"] = "../.."
    _write_proposal(loops_root, bad, filename="traversal.json")
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid id reached claim.acquire")

    monkeypatch.setattr(sb._claim, "acquire", forbidden)
    result = sb._run_iteration(loops_root, repo)
    assert result["candidates"] == []
    assert called is False


def test_filename_must_agree_with_content_id(loops_root: Path) -> None:
    item = _proposal("filename")
    _write_proposal(loops_root, item, filename="sha256_" + "f" * 64 + ".json")
    assert _select(loops_root) == []


def test_park_sidecar_reads_full_id_from_body(loops_root: Path) -> None:
    item = _proposal("parked")
    _write_proposal(loops_root, item)
    (loops_root / "parked" / f"{item['id'].replace(':', '_', 1)}.json").write_text(
        json.dumps({"id": item["id"], "kind": "parkinfo"}))
    assert _select(loops_root) == []


def test_corrupt_park_or_ledger_state_refuses_selection(loops_root: Path) -> None:
    item = _proposal("closed")
    _write_proposal(loops_root, item)
    (loops_root / "parked" / "bad.json").write_text("{bad")
    with pytest.raises(sb._SelectionRefused):
        _select(loops_root)
    (loops_root / "parked" / "bad.json").unlink()
    (loops_root / "ledger.jsonl").write_text("{torn")
    with pytest.raises(sb._SelectionRefused):
        _select(loops_root)


def test_selection_never_reads_advice_would_admit(loops_root: Path) -> None:
    (loops_root / "state" / "advice.json").write_text(json.dumps({
        "would_admit": [{"id": "sha256:" + "a" * 64, "paths": 3}],
    }))
    assert _select(loops_root) == []


def test_dry_run_with_corrupt_proposal_makes_zero_writes(
        loops_root: Path, repo: Path) -> None:
    (loops_root / "proposals" / "broken.json").write_text("{bad")
    before = {p.relative_to(loops_root): p.read_bytes() for p in loops_root.rglob("*")
              if p.is_file()}
    result = sb._run_iteration(loops_root, repo, dry_run=True)
    after = {p.relative_to(loops_root): p.read_bytes() for p in loops_root.rglob("*")
             if p.is_file()}
    assert result["dry_run"] is True
    assert before == after
    assert not (loops_root / "ALERTS.md").exists()
    assert not sb._builders_root(repo).exists()


def test_claim_and_worklist_use_sanctioned_marker(loops_root: Path, repo: Path) -> None:
    item = _proposal("claim")
    _write_proposal(loops_root, item)
    result = sb._run_iteration(loops_root, repo, actor="builder-coordinator")
    assert result["claims_acquired"] == [item["id"]]
    assert len(result["worklist"]) == 1
    marker = claim._marker_path(loops_root, item["id"])
    assert json.loads(marker.read_text())["actor"] == "builder-coordinator"
    assert Path(result["worklist"][0]["brief"]).exists()


def test_registry_failure_refuses_debris_removal(
        loops_root: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _proposal("registry")
    path = sb._builders_root(repo) / f"spawn-{item['id'].removeprefix('sha256:')}"
    path.mkdir(parents=True)
    witness = path / "preserve.txt"
    witness.write_text("keep\n")
    real_git = sb._git

    def broken_git(target: Path, *args: str):
        if args == ("worktree", "list", "--porcelain"):
            return 1, "", "registry unavailable"
        return real_git(target, *args)

    monkeypatch.setattr(sb, "_git", broken_git)
    with pytest.raises(sb._RegistryUnreadable):
        sb._provision_worktree(loops_root, repo, item, base="main")
    assert witness.read_text() == "keep\n"


def test_generic_post_acquire_exception_releases_claim(
        loops_root: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _proposal("generic-release")
    _write_proposal(loops_root, item)

    def explode(*_args, **_kwargs):
        raise RuntimeError("generic post-acquire failure")

    monkeypatch.setattr(sb, "_write_brief", explode)
    result = sb._run_iteration(loops_root, repo, actor="coordinator")
    assert result["claims_acquired"] == [item["id"]]
    assert not claim._marker_path(loops_root, item["id"]).exists()
    assert [event["event"] for event in _events(loops_root)
            if event.get("id") == item["id"]] == ["claimed", "released"]


def test_release_refusal_is_reported_honestly(
        loops_root: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _proposal("release-refusal")
    _write_proposal(loops_root, item)
    monkeypatch.setattr(sb, "_write_brief",
                        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")))

    def refuse(*_args, **_kwargs):
        raise claim.ClaimError(claim.EXIT_LOST_RACE, "owner changed")

    monkeypatch.setattr(sb._claim, "release", refuse)
    result = sb._run_iteration(loops_root, repo, actor="coordinator")
    joined = "\n".join(result["alerts"])
    assert "release REFUSED" in joined
    assert "claim released" not in joined
    assert result["release_failures"]


def test_directive_inert_event_is_schema_valid_and_ack_based(
        loops_root: Path, repo: Path) -> None:
    first, second = _proposal("inert-a"), _proposal("inert-b")
    _write_proposal(loops_root, first)
    _write_proposal(loops_root, second)
    result = sb._run_iteration(loops_root, repo, actor="coordinator")
    assert len(result["worklist"]) == 2
    assert result["inertness"]["directive_inert"] is False

    _age_worklist(repo)
    check = sb._run_iteration(loops_root, repo, actor="coordinator")
    assert check["inertness"]["directive_inert"] is True
    event = [row for row in _events(loops_root)
             if row.get("detail", {}).get("condition") == "directive_inert"][-1]
    assert event["event"] == "instrument_error"
    assert event["detail"]["held_claims"] == 2
    assert event["detail"]["briefs_consumed"] == 0
    schema = json.loads((PIPELINE / "schema" / "ledger-event.schema.json").read_text())
    jsonschema.validate(event, schema)


def test_one_consumed_brief_prevents_false_inert_report(
        loops_root: Path, repo: Path) -> None:
    for tag in ("ack-a", "ack-b"):
        _write_proposal(loops_root, _proposal(tag))
    first = sb._run_iteration(loops_root, repo, actor="coordinator")
    _age_worklist(repo)
    ack = Path(first["worklist"][0]["ack"])
    ack.write_text(json.dumps({"id": first["worklist"][0]["id"],
                               "actor": "builder", "started_at": sb._iso(datetime.now(UTC))}))
    check = sb._run_iteration(loops_root, repo, actor="coordinator")
    assert check["inertness"]["briefs_consumed"] == 1
    assert check["inertness"]["directive_inert"] is False


def _commit_builder_change(worklist: dict) -> Path:
    worktree = Path(worklist["worktree"])
    (worktree / "README.md").write_text("built\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-qm", "build result")
    return worktree


def test_harvest_without_command_never_fabricates_exit_code(
        loops_root: Path, repo: Path) -> None:
    item = _proposal("unverified-harvest")
    _write_proposal(loops_root, item)
    result = sb._run_iteration(loops_root, repo, actor="coordinator")
    worktree = _commit_builder_change(result["worklist"][0])
    out = sb._harvest_one(loops_root, repo, worktree, actor="coordinator", test_cmd="")
    assert out is not None
    evidence = json.loads(out.read_text())["evidence"][0]
    assert evidence["verified_by"] == "reading"
    assert evidence["result"] == "unverified"
    assert "exit_code" not in evidence


def test_harvest_records_exit_code_only_from_command_it_runs(
        loops_root: Path, repo: Path) -> None:
    item = _proposal("verified-harvest")
    _write_proposal(loops_root, item)
    result = sb._run_iteration(loops_root, repo, actor="coordinator")
    worktree = _commit_builder_change(result["worklist"][0])
    command = f"{sys.executable} -c pass"
    out = sb._harvest_one(loops_root, repo, worktree, actor="coordinator",
                          test_cmd=command)
    assert out is not None
    envelope = json.loads(out.read_text())
    assert envelope["head_sha"] == _git(repo, "rev-parse", result["worklist"][0]["branch"])
    assert envelope["evidence"][0] == {
        "claim": "harvester ran the configured mechanical pass-list",
        "verified_by": "execution", "command": command, "exit_code": 0,
    }


def test_failed_harvest_command_writes_no_candidate(
        loops_root: Path, repo: Path) -> None:
    item = _proposal("red-harvest")
    _write_proposal(loops_root, item)
    result = sb._run_iteration(loops_root, repo, actor="coordinator")
    worktree = _commit_builder_change(result["worklist"][0])
    out = sb._harvest_one(loops_root, repo, worktree, actor="coordinator",
                          test_cmd=f"{sys.executable} -c 'raise SystemExit(7)'")
    assert out is None
    assert list((loops_root / "candidates").iterdir()) == []


def _add_fake_venv(repo: Path) -> Path:
    """Give a tmp repo a ``.venv/bin/python`` that IS a working interpreter
    (symlinked to the test's own interpreter), standing in for the repo venv
    the harvester must prefer over the ambient ``python3``."""
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)
    return venv_bin / "python"


def test_venv_default_resolves_to_repo_interpreter_never_ambient_python3(
        repo: Path) -> None:
    assert sb._venv_test_cmd(repo) is None  # no venv yet
    venv_py = _add_fake_venv(repo)
    cmd = sb._venv_test_cmd(repo)
    assert cmd is not None
    assert str(venv_py) in cmd
    assert cmd.endswith("-m pytest -q")
    # The whole point of the DROP-TRAP fix: never the bare ambient interpreter.
    assert not cmd.startswith("python3 ")


def test_provision_records_builder_test_command_in_marker(
        loops_root: Path, repo: Path) -> None:
    item = _proposal("marker-cmd")
    _write_proposal(loops_root, item)
    command = f"{sys.executable} -c pass"
    result = sb._run_iteration(loops_root, repo, actor="coordinator", test_cmd=command)
    marker = json.loads(
        (Path(result["worklist"][0]["worktree"]) / sb.MARKER_NAME).read_text())
    assert marker["test_cmd"] == command


def test_harvest_uses_coordinator_recorded_command_over_ambient_default(
        loops_root: Path, repo: Path) -> None:
    """A built worktree whose real test passes is harvested even though the old
    ambient ``python3 -m pytest`` default would have dropped it (no pytest on
    this estate). With no explicit --test-cmd the harvester runs the command the
    COORDINATOR recorded at provision time -- from the builder-immutable record,
    not the worktree marker."""
    item = _proposal("recorded-cmd-harvest")
    _write_proposal(loops_root, item)
    command = f"{sys.executable} -c pass"  # the real mechanical test, and it PASSES
    result = sb._run_iteration(loops_root, repo, actor="coordinator", test_cmd=command)
    assert sb._trusted_test_cmd(loops_root, item["id"]) == command
    worktree = _commit_builder_change(result["worklist"][0])
    out = sb._harvest_one(loops_root, repo, worktree, actor="coordinator")
    assert out is not None
    evidence = json.loads(out.read_text())["evidence"][0]
    assert evidence["verified_by"] == "execution"
    assert evidence["command"] == command
    assert "python3 -m pytest" not in evidence["command"]


def test_harvest_ignores_tampered_marker_test_command(
        loops_root: Path, repo: Path) -> None:
    """F1 (BLOCKER): the builder-writable marker must NEVER be able to substitute
    a weaker/no-op verification command for the coordinator's intended one. A
    builder that overwrites the marker's test_cmd with a no-op must not mint a
    false-green candidate -- harvest reads the coordinator record, so the
    intended (failing) command still runs and no candidate is written."""
    item = _proposal("marker-tamper")
    _write_proposal(loops_root, item)
    intended = f"{sys.executable} -c 'raise SystemExit(9)'"  # the coordinator's real test; it FAILS
    result = sb._run_iteration(loops_root, repo, actor="coordinator", test_cmd=intended)
    worktree = _commit_builder_change(result["worklist"][0])
    marker_path = worktree / sb.MARKER_NAME
    marker = json.loads(marker_path.read_text())
    assert marker["test_cmd"] == intended
    # Builder tampers the worktree marker to a no-op that would pass.
    marker["test_cmd"] = f"{sys.executable} -c pass"
    marker_path.write_text(json.dumps(marker))
    out = sb._harvest_one(loops_root, repo, worktree, actor="coordinator")
    assert out is None, "tampered marker test_cmd harvested a false-green candidate"
    assert list((loops_root / "candidates").iterdir()) == []


def test_harvest_without_venv_or_recorded_command_fails_closed_not_drops(
        loops_root: Path, repo: Path) -> None:
    """No explicit cmd, no recorded cmd, no repo venv: the harvester must ERROR
    loudly and write NO candidate -- never silently run the pytest-less ambient
    python3 and mark a real build red."""
    item = _proposal("no-interp-harvest")
    _write_proposal(loops_root, item)
    # provision with no explicit cmd; the tmp repo has no .venv, so the record
    # holds no runnable command.
    result = sb._run_iteration(loops_root, repo, actor="coordinator")
    assert sb._trusted_test_cmd(loops_root, item["id"]) is None
    worktree = _commit_builder_change(result["worklist"][0])
    errors: list = []
    out = sb._harvest_one(loops_root, repo, worktree, actor="coordinator", errors=errors)
    assert out is None
    assert list((loops_root / "candidates").iterdir()) == []
    assert "no runnable pass-list" in (loops_root / "ALERTS.md").read_text()
    # F3: the miss is SURFACED, not swallowed as None-as-success.
    assert [e["error"] for e in errors] == ["no_runnable_test_command"]


def _harvest_cli(loops_root: Path, repo: Path) -> tuple[int, dict]:
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        rc = sb.main(["--loops-root", str(loops_root), "--repo", str(repo), "--harvest"])
    return rc, json.loads(stream.getvalue())


def test_missing_interpreter_harvest_is_distinguishable_from_no_work(
        loops_root: Path, repo: Path, tmp_path: Path) -> None:
    """F3 (MAJOR): an unresolvable pass-list must produce a DISTINGUISHABLE
    non-success at the CLI boundary -- a non-zero exit and an explicit `errors`
    field -- never the same silent exit-0 empty result a healthy no-work harvest
    returns."""
    item = _proposal("missing-interp-cli")
    _write_proposal(loops_root, item)
    result = sb._run_iteration(loops_root, repo, actor="coordinator")  # no venv, no cmd
    _commit_builder_change(result["worklist"][0])
    missing_rc, missing_out = _harvest_cli(loops_root, repo)
    assert missing_rc != 0
    assert missing_out["harvested"] == []
    assert missing_out["errors"] and missing_out["errors"][0]["error"] == "no_runnable_test_command"

    # A genuinely empty, healthy harvest: no builders, no work.
    healthy_repo = tmp_path / "healthy"
    healthy_repo.mkdir()
    _git(healthy_repo, "init", "-q", "-b", "main")
    healthy_loops = healthy_repo / "var" / "loopqueue"
    for name in ("claims", "state", "candidates", "proposals", "parked"):
        (healthy_loops / name).mkdir(parents=True)
    (healthy_loops / "ledger.jsonl").touch()
    healthy_rc, healthy_out = _harvest_cli(healthy_loops, healthy_repo)
    assert healthy_rc == 0
    assert healthy_out == {"harvested": [], "skipped_no_own_work": [], "errors": []}
    assert (missing_rc, missing_out) != (healthy_rc, healthy_out)


def test_skipped_duplicate_write_does_not_report_success(
        loops_root: Path, repo: Path) -> None:
    """Defect 2: when the candidate envelope already exists on disk, the second
    harvest wrote nothing, so it must return None (a distinct, logged outcome)
    rather than masquerade as a fresh harvest."""
    item = _proposal("dup-write-harvest")
    _write_proposal(loops_root, item)
    command = f"{sys.executable} -c pass"
    result = sb._run_iteration(loops_root, repo, actor="coordinator", test_cmd=command)
    worktree = _commit_builder_change(result["worklist"][0])
    first = sb._harvest_one(loops_root, repo, worktree, actor="coordinator",
                            test_cmd=command, close=False)
    assert first is not None
    second = sb._harvest_one(loops_root, repo, worktree, actor="coordinator",
                             test_cmd=command, close=False)
    assert second is None  # skipped write MUST NOT report success
    assert len(list((loops_root / "candidates").iterdir())) == 1
    assert "already exists" in (loops_root / "ALERTS.md").read_text()


def test_duplicate_candidate_reconciles_claim_and_worktree(
        loops_root: Path, repo: Path) -> None:
    """F2 (MAJOR): a pre-existing candidate (a prior harvest that crashed between
    the candidate fsync and its cleanup) must not strand the claim and worktree.
    The reconciling harvest completes the release + worktree close idempotently
    and reports no fresh write."""
    item = _proposal("dup-reconcile")
    _write_proposal(loops_root, item)
    command = f"{sys.executable} -c pass"
    result = sb._run_iteration(loops_root, repo, actor="coordinator", test_cmd=command)
    worktree = Path(result["worklist"][0]["worktree"])
    branch = result["worklist"][0]["branch"]
    _commit_builder_change(result["worklist"][0])
    marker = json.loads((worktree / sb.MARKER_NAME).read_text())
    tip = _git(repo, "rev-parse", branch)
    envelope = sb._envelope(item, branch=branch, base_sha=marker["base_sha"], tip_sha=tip,
                            actor="coordinator", evidence={
                                "claim": "harvester ran the configured mechanical pass-list",
                                "verified_by": "execution", "command": command, "exit_code": 0})
    existing = loops_root / "candidates" / f"{envelope['id'].replace(':', '_', 1)}.json"
    existing.write_text(json.dumps(envelope, indent=2) + "\n")
    claim_path = claim._marker_path(loops_root, item["id"])
    assert claim_path.exists() and worktree.exists()

    harvested = sb._harvest_all(loops_root, repo, actor="coordinator", test_cmd=command,
                                close=True)
    assert harvested == []  # a duplicate is never reported as a fresh write
    assert not claim_path.exists(), "reconcile stranded the claim"
    assert not worktree.exists(), "reconcile stranded the worktree"


def _advance_main(repo: Path, name: str = "MAIN.md") -> str:
    """Land a commit on main, as happens constantly while builders work."""
    (repo / name).write_text("landed on main\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", f"main advances ({name})")
    return _git(repo, "rev-parse", "main")


def _sentinel_cmd(worktree: Path) -> tuple[Path, str]:
    """A pass-list command that leaves proof on disk that it actually ran, so
    'the suite was never launched' is asserted by execution, not by a spy."""
    sentinel = worktree.parent / f"{worktree.name}.passlist-ran"
    code = f"open({str(sentinel)!r}, 'w').close()"
    return sentinel, f"{sys.executable} -c {code!r}"


def test_harvest_skips_branch_carrying_no_commit_absent_from_main(
        loops_root: Path, repo: Path) -> None:
    """The emptiness predicate must be ancestry, not `tip != base`.

    Once main advances past the recorded base and the lane is re-anchored onto
    it without building anything, tip != base is TRUE while the branch carries
    no commit of its own. The old predicate therefore hands such a lane a full
    ~25-minute pass-list that cannot publish anything -- and, on a green exit,
    mints a candidate whose base..tip diff is main's OWN landed commits."""
    item = _proposal("vacuous-lane")
    _write_proposal(loops_root, item)
    result = sb._run_iteration(loops_root, repo, actor="coordinator")
    worklist = result["worklist"][0]
    worktree = Path(worklist["worktree"])
    branch = worklist["branch"]
    base_sha = json.loads((worktree / sb.MARKER_NAME).read_text())["base_sha"]

    main_sha = _advance_main(repo)
    _git(worktree, "reset", "--hard", "main")  # lane re-anchored; nothing built

    tip_sha = _git(repo, "rev-parse", branch)
    assert tip_sha != base_sha, "fixture must defeat the OLD tip != base guard"
    assert tip_sha == main_sha
    assert _git(repo, "rev-list", "--max-count=1", branch, "--not", "main") == "", \
        "fixture branch must carry no commit absent from main"

    sentinel, command = _sentinel_cmd(worktree)
    skipped: list = []
    errors: list = []
    out = sb._harvest_one(loops_root, repo, worktree, actor="coordinator",
                          test_cmd=command, errors=errors, skipped=skipped)

    assert out is None
    assert not sentinel.exists(), "the pass-list ran for a lane that can publish nothing"
    assert list((loops_root / "candidates").iterdir()) == [], \
        "a counterfeit candidate was minted from main's own landed commits"
    assert errors == [], "an empty lane is a healthy skip, not an instrument failure"
    assert [(s["branch"], s["reason"]) for s in skipped] == [(branch, "no_own_work")]


def test_harvest_runs_when_the_own_work_probe_is_unreadable(
        loops_root: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail OPEN on the instrument: `None` ("could not determine") is not
    `False` ("empty"). A probe that cannot run must degrade to the behaviour
    that predates it, never silently stop harvesting every lane."""
    item = _proposal("unreadable-probe")
    _write_proposal(loops_root, item)
    result = sb._run_iteration(loops_root, repo, actor="coordinator")
    worklist = result["worklist"][0]
    worktree = Path(worklist["worktree"])
    _advance_main(repo)
    _git(worktree, "reset", "--hard", "main")  # the same empty lane as above

    real_git = sb._git

    def blocked_probe(repo_arg: Path, *args: str):
        if args[:1] == ("rev-list",):
            return 128, "", "fatal: probe blocked"
        return real_git(repo_arg, *args)

    monkeypatch.setattr(sb, "_git", blocked_probe)

    sentinel, command = _sentinel_cmd(worktree)
    skipped: list = []
    out = sb._harvest_one(loops_root, repo, worktree, actor="coordinator",
                          test_cmd=command, skipped=skipped)

    assert sentinel.exists(), "an unreadable probe was treated as 'empty' and skipped"
    assert skipped == []
    assert out is not None  # exactly today's behaviour, no better and no worse


def test_harvest_still_publishes_a_branch_with_one_commit_of_its_own(
        loops_root: Path, repo: Path) -> None:
    """The skip must not be over-broad: a lane carrying real work is verified
    and published exactly as before, even after main advances past its base."""
    item = _proposal("real-work-lane")
    _write_proposal(loops_root, item)
    result = sb._run_iteration(loops_root, repo, actor="coordinator")
    worklist = result["worklist"][0]
    worktree = _commit_builder_change(worklist)
    base_sha = json.loads((worktree / sb.MARKER_NAME).read_text())["base_sha"]
    _advance_main(repo)  # main moves on; the lane's fork point is still its base

    sentinel, command = _sentinel_cmd(worktree)
    skipped: list = []
    out = sb._harvest_one(loops_root, repo, worktree, actor="coordinator",
                          test_cmd=command, skipped=skipped)

    assert sentinel.exists(), "a lane with real work was not verified"
    assert out is not None
    assert skipped == []
    envelope = json.loads(out.read_text())
    assert envelope["base_sha"] == base_sha
    assert envelope["head_sha"] == _git(repo, "rev-parse", worklist["branch"])


def test_envelope_base_is_reanchored_onto_the_branchs_real_fork_point(
        loops_root: Path, repo: Path) -> None:
    """The second half of the defect: a lane re-anchored onto a newer main and
    THEN built carries real work, so it is published -- but its recorded base is
    stale, and base..tip would attribute main's landed commits to the lane. The
    published range must contain this lane's commits and nothing else."""
    item = _proposal("reanchored-lane")
    _write_proposal(loops_root, item)
    result = sb._run_iteration(loops_root, repo, actor="coordinator")
    worklist = result["worklist"][0]
    worktree = Path(worklist["worktree"])
    stale_base = json.loads((worktree / sb.MARKER_NAME).read_text())["base_sha"]

    main_sha = _advance_main(repo)
    _git(worktree, "reset", "--hard", "main")
    (worktree / "LANE.md").write_text("lane work\n")
    _git(worktree, "add", "LANE.md")
    _git(worktree, "commit", "-qm", "lane work")

    _sentinel, command = _sentinel_cmd(worktree)
    out = sb._harvest_one(loops_root, repo, worktree, actor="coordinator",
                          test_cmd=command)
    assert out is not None
    envelope = json.loads(out.read_text())
    assert envelope["base_sha"] == main_sha != stale_base
    changed = set(_git(repo, "diff", "--name-only",
                       f"{envelope['base_sha']}...{envelope['head_sha']}").split())
    assert changed == {"LANE.md"}, "the published range attributes main's commits to the lane"


def test_only_main_is_a_public_callable_entrypoint() -> None:
    public_callables = {name for name, value in vars(sb).items()
                        if callable(value) and not name.startswith("_")
                        and getattr(value, "__module__", None) == sb.__name__}
    assert public_callables == {"main"}
