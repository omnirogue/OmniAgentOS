"""Retire-on-land: a landed candidate must retire the proposal it resolves.

THE MEASURED DEFECT. Nothing retired a proposal when the candidate that
resolves it merged, so the proposal stayed selectable forever: planners
re-selected it, builders rebuilt it (up to 4x on one id), and reviewers
re-refused it. 56 of 143 recent rejections were re-refusals of already-shipped
work — the single largest rejection bucket.

Two halves are under test here, and they are deliberately the SAME event shape:

  * FORWARD (``gate_loop.GateLoop._terminalize_resolved_proposals``) — at land
    time, once ``main`` has actually moved, every proposal named by a landed
    candidate's ``payload.resolves`` gets a terminal ledger event naming the
    landing commit as its carrier.
  * BACKFILL (``close_on_land.retire_proposals``) — one-shot, dry-run by
    default, for everything that landed before the forward half existed.

The terminal state is the CONTRACT's own, not a new marker class: CONTRACT.md
§8 derives status from the ledger ("Artifacts are immutable, so status is
derived from the ledger, never stored"), §5 lists ``completed`` as a terminal
event, and §10 sweeps the artifact 7 days after it. Selection reads exactly
that: ``integration.LedgerView.terminal`` (which admits
merged/completed/rejected/closed) is what ``spawn_builders._admitted_unclaimed``
filters on. So a passing test here is a proposal that a REAL selection pass no
longer offers — asserted below by calling ``_admitted_unclaimed`` itself, not
by re-deriving what it might do.

Every test is written to fail against a daemon that:
  * retires on anything less than a CONFIRMED, nameable landing commit,
  * retires a proposal a landed candidate never named,
  * writes a terminal event the ledger schema rejects (a `completed` with no
    `detail.reason`, or a `detail.result` outside the pass/fail enum, is
    invisible to every validating reader and unrepairable once appended),
  * or writes anything at all during a backfill DRY RUN.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import close_on_land as col  # noqa: E402
from bridge import gate_loop as gl  # noqa: E402
from bridge import spawn_builders as sb  # noqa: E402
from bridge.canonical import content_id  # noqa: E402
from bridge.gate_loop import GateLoop  # noqa: E402
from bridge.integration import GateVerdict  # noqa: E402
from bridge.train_assembler import Train  # noqa: E402


def _payload(marker: str) -> dict:
    return {"direction": 1, "problem": marker}


#: Selection re-derives a proposal's id from its payload and SKIPS any file
#: whose id disagrees; retirement now does the same for candidates. So every
#: id in this module is a REAL content address, exactly as a filed envelope's
#: is — a test fixture that could not survive its own producer's rules proves
#: nothing about the producer.
PROPOSAL = content_id(_payload("p1"))
PROPOSAL_2 = content_id(_payload("p2"))


def _cand_payload(marker: str = "c1", *, resolves: object = PROPOSAL,
                  with_resolves: bool = True) -> dict:
    payload: dict = {"marker": marker}
    if with_resolves:
        payload["resolves"] = resolves
    return payload


CANDIDATE = content_id(_cand_payload("c1"))
CANDIDATE_2 = content_id(_cand_payload("c2", resolves=PROPOSAL_2))
MERGE_SHA = "a" * 40


# ------------------------------------------------------------------ fixtures


def _stem(ident: str) -> str:
    return ident.replace(":", "_", 1)


def _write_proposal(root: Path, marker: str = "p1", *,
                    paths: list[str] | None = None) -> str:
    payload = _payload(marker)
    ident = content_id(payload)
    d = root / "proposals"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{_stem(ident)}.json").write_text(json.dumps({
        "contract": "v1.1", "id": ident, "kind": "proposal", "title": "p",
        "created_at": "2026-08-10T00:00:00Z",
        "producer": {"role": "planner", "actor": "planner@x"},
        "paths": paths or ["pipeline/bridge/gate_loop.py"],
        "payload": payload,
    }), encoding="utf-8")
    return ident


def _write_candidate(root: Path, marker: str = "c1", *,
                     resolves: object = PROPOSAL,
                     with_resolves: bool = True,
                     spoof_id: str | None = None,
                     filename: str | None = None,
                     payload: object = "__derive__") -> str:
    """A candidate envelope. BOUND by default: its id is `content_id(payload)`
    and its filename is that id, exactly as a real one filed by the pipeline.

    `spoof_id` / `filename` / a hand-written `payload` are the forgery levers,
    used only by the tests that must prove an unbound envelope retires nothing.
    """
    d = root / "candidates"
    d.mkdir(parents=True, exist_ok=True)
    if payload == "__derive__":
        payload = _cand_payload(marker, resolves=resolves,
                                with_resolves=with_resolves)
    ident = spoof_id or content_id(payload)
    (d / (filename or f"{_stem(ident)}.json")).write_text(json.dumps({
        "contract": "v1.1", "id": ident, "kind": "candidate", "title": "c",
        "created_at": "2026-08-10T00:00:00Z",
        "producer": {"role": "implementer", "actor": "impl@x"},
        "base_sha": "b" * 40, "head_sha": "e" * 40, "branch": "fix/thing",
        "paths": ["pipeline/bridge/gate_loop.py"],
        "evidence": [{"claim": "built", "verified_by": "execution",
                      "command": "pytest", "exit_code": 0}],
        "payload": payload,
    }), encoding="utf-8")
    return ident


def _append(root: Path, event: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "ledger.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def _merged_event(ident: str, merge_sha: str = MERGE_SHA) -> dict:
    return {"ts": "2026-08-11T00:00:00Z", "role": "implementer", "event": "merged",
            "id": ident, "actor": "gate-loop-daemon",
            "detail": {"result": "pass", "merge_sha": merge_sha,
                       "receipt": "receipts/land.json", "train": "train/t"}}


def _events(root: Path) -> list[dict]:
    path = root / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _events_for(root: Path, ident: str) -> list[dict]:
    return [e for e in _events(root) if e.get("id") == ident]


def _loop(root: Path, repo: Path | None = None) -> GateLoop:
    root.mkdir(parents=True, exist_ok=True)
    return GateLoop(root, repo or (root.parent / "repo"), push=False, remote=None)


def _selectable(root: Path) -> list[str]:
    """What a REAL selection pass would offer a builder right now."""
    return [p["id"] for p in
            sb._admitted_unclaimed(root, persist_alerts=False, alerts=[])]


def _schema() -> dict:
    return json.loads((ROOT / "schema" / "ledger-event.schema.json").read_text())


def _apply(root: Path, **kw) -> tuple[int, dict]:
    """The reviewed two-step the CLI enforces: dry-run, then apply THAT plan.

    `--apply` will not write against a plan nobody read, so every apply in this
    module goes through the snapshot the immediately preceding dry run named.
    """
    _code, dry = col.retire_proposals(root, render=False)
    return col.retire_proposals(root, apply=True, snapshot=dry["snapshot"], **kw)


# ---------------------------------------------- git, for the _record_landing test


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, check=False)
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {p.stderr or p.stdout}")
    return p.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "tester")
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "baseline")
    return path


def _train(repo: Path, base: str, members: list[dict]) -> Train:
    _git(repo, "checkout", "-q", "-B", "train/manual", base)
    (repo / "t.txt").write_text("TTT\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "train/manual: t.txt")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return Train(branch="train/manual", base=base, tip=tip, members=members,
                 paths=["t.txt"])


# ================================================================ FORWARD half


def test_a_landed_candidate_retires_its_proposal(tmp_path: Path) -> None:
    """The acceptance case, in the exact shape the defect was measured in.

    While the candidate is LIVE, `_live_resolver_targets` keeps its proposal
    away from a second builder. The moment the candidate goes terminal that
    suppression lifts on purpose (a REJECTED resolver must re-enable its
    proposal for a recut) — so a MERGED candidate hands the proposal straight
    back to the next planner unless something retires it. That is the bug.
    """
    root = tmp_path / "loops"
    _write_proposal(root)
    _write_candidate(root)
    _append(root, {"ts": "2026-08-10T00:00:00Z", "role": "planner",
                   "event": "proposed", "id": PROPOSAL, "actor": "planner@x"})
    _append(root, _merged_event(CANDIDATE))
    assert _selectable(root) == [PROPOSAL]          # re-offered although shipped

    loop = _loop(root)
    loop._terminalize_resolved_proposals(CANDIDATE, MERGE_SHA,
                                         "receipts/land.json", "train/t")

    retirement = _events_for(root, PROPOSAL)
    assert [e["event"] for e in retirement] == ["proposed", "completed"]
    assert _selectable(root) == []


def test_the_retirement_event_validates_against_the_ledger_schema(
        tmp_path: Path) -> None:
    """A terminal event no validating reader accepts is not a retirement.

    `completed` REQUIRES `detail.reason` (schema allOf), and `detail.result`
    is an enum of exactly pass|fail — a free-text result silently invalidates
    every line, and the ledger is append-only, so it cannot be repaired.
    """
    jsonschema = pytest.importorskip("jsonschema")
    root = tmp_path / "loops"
    _write_proposal(root)
    _write_candidate(root)
    loop = _loop(root)
    loop._terminalize_resolved_proposals(CANDIDATE, MERGE_SHA,
                                         "receipts/land.json", "train/t")
    ev = _events_for(root, PROPOSAL)[0]
    jsonschema.validate(ev, _schema())                    # must not raise


def test_the_retirement_names_its_carrier_commit_and_the_candidate(
        tmp_path: Path) -> None:
    """Whoever reads this line must be able to go and look at the commit."""
    root = tmp_path / "loops"
    _write_proposal(root)
    _write_candidate(root)
    _loop(root)._terminalize_resolved_proposals(
        CANDIDATE, MERGE_SHA, "receipts/land.json", "train/t")
    detail = _events_for(root, PROPOSAL)[0]["detail"]
    assert detail["resolved_by"] == CANDIDATE
    assert detail["carrier_sha"] == MERGE_SHA
    assert MERGE_SHA[:12] in detail["reason"]
    assert detail["receipt"] == "receipts/land.json"


@pytest.mark.parametrize("bad", ["", "abc123", "z" * 40, None, 12345])
def test_no_retirement_without_a_nameable_landing_commit(
        tmp_path: Path, bad: object) -> None:
    """close_on_land's sharpest refusal, reused: a landing nobody can NAME is
    not evidence. Retiring a proposal against an unnameable carrier hides
    shipped work behind a commit no one can check."""
    root = tmp_path / "loops"
    _write_proposal(root)
    _write_candidate(root)
    loop = _loop(root)
    loop._terminalize_resolved_proposals(CANDIDATE, bad,  # type: ignore[arg-type]
                                         "receipts/land.json", "train/t")
    assert _events_for(root, PROPOSAL) == []
    assert any("nameable" in ln for ln in loop.lines), loop.lines


def test_a_candidate_with_no_resolves_retires_nothing(tmp_path: Path) -> None:
    """About half of live candidates never stamped the link. That is a gap to
    make VISIBLE, never one to guess at."""
    root = tmp_path / "loops"
    _write_proposal(root)
    cid = _write_candidate(root, with_resolves=False)
    loop = _loop(root)
    loop._terminalize_resolved_proposals(cid, MERGE_SHA,
                                         "receipts/land.json", "train/t")
    assert _events(root) == []
    assert _selectable(root) == [PROPOSAL]
    assert any("no payload.resolves" in ln for ln in loop.lines), loop.lines


@pytest.mark.parametrize("resolves", ["x", "sha256:nope", 7, {"id": PROPOSAL},
                                      ["../../etc/passwd"], [None]])
def test_a_malformed_resolves_retires_nothing_and_says_why(
        tmp_path: Path, resolves: object) -> None:
    root = tmp_path / "loops"
    _write_proposal(root)
    cid = _write_candidate(root, resolves=resolves)
    loop = _loop(root)
    loop._terminalize_resolved_proposals(cid, MERGE_SHA,
                                         "receipts/land.json", "train/t")
    assert _events(root) == []
    assert any("unrecognised id" in ln or "no payload.resolves" in ln
               for ln in loop.lines), loop.lines


def test_a_proposal_already_terminal_is_never_terminalized_twice(
        tmp_path: Path) -> None:
    """`exactly_one_terminal_event` is a ledger invariant and the ledger is
    append-only, so a second terminal can never be repaired."""
    root = tmp_path / "loops"
    _write_proposal(root)
    _write_candidate(root)
    _append(root, {"ts": "2026-08-10T00:00:00Z", "role": "implementer",
                   "event": "completed", "id": PROPOSAL, "actor": "operator",
                   "detail": {"reason": "applied out of repo"}})
    loop = _loop(root)
    loop._terminalize_resolved_proposals(CANDIDATE, MERGE_SHA,
                                         "receipts/land.json", "train/t")
    assert len(_events_for(root, PROPOSAL)) == 1


def test_a_list_of_resolves_retires_every_named_proposal_once(
        tmp_path: Path) -> None:
    root = tmp_path / "loops"
    _write_proposal(root)
    _write_proposal(root, "p2")
    cid = _write_candidate(root, resolves=[PROPOSAL, PROPOSAL_2, PROPOSAL])
    _append(root, _merged_event(cid))
    _loop(root)._terminalize_resolved_proposals(
        cid, MERGE_SHA, "receipts/land.json", "train/t")
    assert len(_events_for(root, PROPOSAL)) == 1
    assert len(_events_for(root, PROPOSAL_2)) == 1
    assert _selectable(root) == []


def test_a_partial_train_retires_only_its_landed_members(tmp_path: Path) -> None:
    """A train lands its OWN members. A candidate that resolves another
    proposal but is not in this train keeps that proposal open — retirement is
    keyed on exact candidate-id membership of what actually landed, never on
    "something landed near it"."""
    repo = _init_repo(tmp_path / "repo")
    base = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "loops"
    _write_proposal(root)
    _write_proposal(root, "p2")
    assert _write_candidate(root, "c1") == CANDIDATE
    assert _write_candidate(root, "c2", resolves=PROPOSAL_2) == CANDIDATE_2  # not in the train

    loop = _loop(root, repo)
    train = _train(repo, base, [{"id": CANDIDATE, "branch": "fix/thing",
                                 "base": base, "paths": ["t.txt"]}])
    verdict = GateVerdict("pass", 0, "", "gate passed", None, "", 1.0)
    merge_sha = train.tip
    loop._record_landing(train, verdict, merge_sha)

    assert [e["event"] for e in _events_for(root, PROPOSAL)] == ["completed"]
    assert _events_for(root, PROPOSAL_2) == []
    # and the landed member's OWN terminal event is still the ordinary `merged`
    assert [e["event"] for e in _events_for(root, CANDIDATE)] == ["merged"]
    assert _events_for(root, CANDIDATE_2) == []


def test_an_unreadable_candidate_artifact_retires_nothing(tmp_path: Path) -> None:
    """The link is unknown, not absent — and unknown never reads as a licence
    to retire something."""
    root = tmp_path / "loops"
    _write_proposal(root)
    (root / "candidates").mkdir(parents=True, exist_ok=True)
    (root / "candidates" / f"{_stem(CANDIDATE)}.json").write_text(
        "{not json", encoding="utf-8")
    loop = _loop(root)
    loop._terminalize_resolved_proposals(CANDIDATE, MERGE_SHA,
                                         "receipts/land.json", "train/t")
    assert _events(root) == []
    assert _selectable(root) == [PROPOSAL]


# =============================================================== BACKFILL half


def _landed_backfill_fixture(tmp_path: Path) -> Path:
    """One candidate that merged and whose proposal was never retired."""
    root = tmp_path / "loops"
    _write_proposal(root)
    _write_candidate(root)
    _append(root, {"ts": "2026-08-10T00:00:00Z", "role": "planner",
                   "event": "proposed", "id": PROPOSAL, "actor": "planner@x"})
    _append(root, _merged_event(CANDIDATE))
    return root


def test_backfill_dry_run_names_each_pair_and_writes_nothing(
        tmp_path: Path) -> None:
    root = _landed_backfill_fixture(tmp_path)
    before = (root / "ledger.jsonl").read_text()

    code, report = col.retire_proposals(root)

    assert report["applied"] is False
    assert [(p["proposal"], p["carrier"]) for p in report["pairs"]] == [
        (PROPOSAL, MERGE_SHA)]
    assert report["retired"] == []
    assert (root / "ledger.jsonl").read_text() == before
    assert _selectable(root) == [PROPOSAL]          # still offered: nothing wrote
    assert code == 0


def test_backfill_apply_retires_the_pairs_it_named(tmp_path: Path) -> None:
    root = _landed_backfill_fixture(tmp_path)
    _code, dry = col.retire_proposals(root)
    code, report = _apply(root)

    assert report["applied"] is True
    assert report["retired"] == [PROPOSAL]
    assert [(p["proposal"], p["carrier"]) for p in report["pairs"]] == \
           [(p["proposal"], p["carrier"]) for p in dry["pairs"]]
    ev = _events_for(root, PROPOSAL)[-1]
    assert ev["event"] == "completed"
    assert ev["detail"]["carrier_sha"] == MERGE_SHA
    assert ev["detail"]["resolved_by"] == CANDIDATE
    assert _selectable(root) == []
    assert code == 0


def test_backfill_emits_the_same_event_shape_as_the_land_time_half(
        tmp_path: Path) -> None:
    """One writer, one shape. A backfill that invents its own record makes the
    two halves indistinguishable only by luck."""
    jsonschema = pytest.importorskip("jsonschema")
    root = _landed_backfill_fixture(tmp_path)
    _apply(root)
    ev = _events_for(root, PROPOSAL)[-1]
    jsonschema.validate(ev, _schema())
    assert set(ev["detail"]) >= {"reason", "resolved_by", "carrier_sha"}
    # ...but the HAND is named honestly: a one-shot sweep of old history must
    # not read as a landing the daemon made at that instant.
    assert ev["actor"] == col.RETIRE_ACTOR


def test_backfill_is_idempotent(tmp_path: Path) -> None:
    root = _landed_backfill_fixture(tmp_path)
    _apply(root)
    code, report = _apply(root)
    assert report["pairs"] == []
    assert report["already_retired"] == [PROPOSAL]
    assert len([e for e in _events_for(root, PROPOSAL)
                if e["event"] == "completed"]) == 1
    assert code == 0


def test_backfill_reports_shipped_work_that_reads_rejected_and_writes_nothing(
        tmp_path: Path) -> None:
    """THE KNOWN CASE. Proposals resolved by a merged candidate that later
    collected a `rejected` event read as refused although the work landed
    (measured: candidate sha256:1412ed42, merged as a8e4100e). A second
    terminal event cannot repair that on an append-only ledger, so this is
    REPORTED with its carrier named and left for a human — never overwritten,
    and never silently counted as retired."""
    root = _landed_backfill_fixture(tmp_path)
    _append(root, {"ts": "2026-08-11T01:00:00Z", "role": "implementer",
                   "event": "rejected", "id": PROPOSAL, "actor": "impl@x",
                   "detail": {"reason": "superseded", "class": "candidate-defect",
                              "expires_at": "2026-09-11T00:00:00Z"}})
    before = (root / "ledger.jsonl").read_text()

    code, report = _apply(root)

    assert report["pairs"] == []
    assert report["retired"] == []
    assert [(c["proposal"], c["carrier"]) for c in report["contradictions"]] == [
        (PROPOSAL, MERGE_SHA)]
    assert (root / "ledger.jsonl").read_text() == before
    assert code == 1                                    # needs a human


def test_backfill_refuses_a_carrier_it_cannot_name(tmp_path: Path) -> None:
    root = tmp_path / "loops"
    _write_proposal(root)
    _write_candidate(root)
    _append(root, {"ts": "2026-08-11T00:00:00Z", "role": "implementer",
                   "event": "merged", "id": CANDIDATE, "actor": "x",
                   "detail": {"result": "pass"}})       # no merge_sha
    code, report = _apply(root)
    assert report["pairs"] == []
    assert report["unnameable"] == [CANDIDATE]
    assert _events_for(root, PROPOSAL) == []
    assert code == 1


def test_backfill_ignores_candidates_that_never_landed(tmp_path: Path) -> None:
    """A rejected or still-open candidate proves nothing about `main`."""
    root = tmp_path / "loops"
    _write_proposal(root)
    _write_candidate(root)
    _append(root, {"ts": "2026-08-11T00:00:00Z", "role": "implementer",
                   "event": "rejected", "id": CANDIDATE, "actor": "x",
                   "detail": {"reason": "defect", "class": "candidate-defect",
                              "expires_at": "2026-09-11T00:00:00Z"}})
    code, report = _apply(root)
    assert report["pairs"] == []
    assert _events_for(root, PROPOSAL) == []
    assert code == 0


def test_backfill_counts_landed_candidates_that_never_named_a_proposal(
        tmp_path: Path) -> None:
    root = tmp_path / "loops"
    cid = _write_candidate(root, with_resolves=False)
    _append(root, _merged_event(cid))
    code, report = _apply(root)
    assert report["pairs"] == []
    assert report["no_link"] == [cid]
    assert code == 0


def test_backfill_apply_refuses_an_implausibly_large_sweep(tmp_path: Path) -> None:
    """close_on_land's `implausible_close_rate` posture: a run that wants to
    retire everything at once is a broken comparison, not a good day."""
    root = tmp_path / "loops"
    for i in range(4):
        pid = _write_proposal(root, f"bulk{i}")
        cid = _write_candidate(root, f"bulk-c{i}", resolves=pid)
        _append(root, _merged_event(cid))
    code, report = _apply(root, max_retire=2)
    assert report["refused"]
    assert report["retired"] == []
    assert all(e["event"] == "merged" for e in _events(root))
    assert code == 2                                    # do not re-run unchanged


def test_backfill_cli_dry_run_is_the_default_and_writes_nothing(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _landed_backfill_fixture(tmp_path)
    before = (root / "ledger.jsonl").read_text()
    code = col.main(["retire-proposals", "--loops-root", str(root)])
    out = capsys.readouterr().out
    assert PROPOSAL[:19] in out or PROPOSAL in out
    assert MERGE_SHA[:12] in out
    assert (root / "ledger.jsonl").read_text() == before
    assert code == 0


def test_backfill_cli_apply_writes(tmp_path: Path) -> None:
    """The two-step the CLI now enforces: emit a plan, read it, apply THAT."""
    root = _landed_backfill_fixture(tmp_path)
    plan = tmp_path / "plan.json"
    assert col.main(["retire-proposals", "--loops-root", str(root),
                     "--emit-snapshot", str(plan)]) == 0
    assert json.loads(plan.read_text())["pairs"][0]["proposal"] == PROPOSAL
    assert col.main(["retire-proposals", "--loops-root", str(root),
                     "--snapshot", str(plan), "--apply"]) == 0
    assert _selectable(root) == []


def test_backfill_cli_apply_without_a_reviewed_plan_writes_nothing(
        tmp_path: Path) -> None:
    root = _landed_backfill_fixture(tmp_path)
    before = (root / "ledger.jsonl").read_text()
    assert col.main(["retire-proposals", "--loops-root", str(root), "--apply"]) == 2
    assert (root / "ledger.jsonl").read_text() == before


def test_backfill_apply_refuses_a_plan_the_queue_has_drifted_from(
        tmp_path: Path) -> None:
    """E-003: a pair that arrived AFTER the reviewed dry run must not ride in on
    an --apply that a human authorized for a different plan."""
    root = _landed_backfill_fixture(tmp_path)
    _code, dry = col.retire_proposals(root, render=False)
    assert [p["proposal"] for p in dry["pairs"]] == [PROPOSAL]

    late = _write_proposal(root, "arrived-late")                # new work lands
    late_cid = _write_candidate(root, "late", resolves=late)
    _append(root, _merged_event(late_cid, "b" * 40))
    before = (root / "ledger.jsonl").read_text()

    code, report = col.retire_proposals(root, apply=True,
                                        snapshot=dry["snapshot"], render=False)

    assert report["retired"] == []
    assert report["drift"]["appeared"] == [late]
    assert (root / "ledger.jsonl").read_text() == before
    assert code == 2                                    # do not re-run unchanged


def test_the_pr_closing_cli_still_requires_its_own_flags(tmp_path: Path) -> None:
    """The backfill is an ADDED entry point, not a replacement: the existing
    `--repo/--git-dir` surface every caller uses must be untouched."""
    with pytest.raises(SystemExit):
        col.main(["--git-dir", str(tmp_path)])          # missing --repo


# ============================ CROSS-LINEAGE REVIEW, 2026-08-12 (Gemini 3.1 Pro)
# Three findings against the first cut of this lane. Each test below is the
# reviewer's repro, kept permanently so the hole cannot be reopened quietly.


# ------------------------------------------------ BLOCKER: false retirement via
# a spoofed candidate id. A retirement takes a claim made by artifact A and
# writes an unrepairable terminal event onto artifact B. If the id asserting
# that claim is taken on trust, a file dropped into candidates/ picks any live
# proposal out of the queue and kills it.


def test_a_spoofed_candidate_file_retires_nothing(tmp_path: Path) -> None:
    """The reviewer's repro: a crafted file under an arbitrary name, claiming
    an already-merged candidate's id, naming a victim proposal."""
    root = tmp_path / "loops"
    victim = _write_proposal(root)
    real_cid = _write_candidate(root, "real", resolves=None, with_resolves=False)
    _append(root, _merged_event(real_cid))
    # the forgery: same id, attacker-chosen `resolves`, any filename it likes
    _write_candidate(root, spoof_id=real_cid, filename="malicious.json",
                     payload={"resolves": victim})

    code, report = _apply(root, render=False)

    assert report["retired"] == []
    assert _events_for(root, victim) == []
    assert _selectable(root) == [victim]         # still the planner's to hand out
    assert any("malicious.json" in item for item in report["id_unbound"])
    assert code == 1                             # surfaced, not swallowed


def test_a_candidate_body_edited_after_filing_retires_nothing(
        tmp_path: Path) -> None:
    """The subtler half: the file IS at the path its id names, but the body was
    edited afterwards, so the id no longer hashes the payload. Indistinguishable
    from a forgery, and treated the same."""
    root = tmp_path / "loops"
    victim = _write_proposal(root)
    honest = content_id(_cand_payload("honest", with_resolves=False))
    _write_candidate(root, spoof_id=honest, payload={"resolves": victim})
    _append(root, _merged_event(honest))

    code, report = _apply(root, render=False)

    assert report["retired"] == []
    assert report["id_unbound"] == [f"{honest} (content_id(payload) != id)"]
    assert _events_for(root, victim) == []
    assert code == 1


def test_the_daemon_refuses_a_body_that_was_edited_in_place(
        tmp_path: Path) -> None:
    """Half one on the land-time path: the file IS at the path its id names,
    but the body no longer hashes to that id.

    (This test used to claim `_read_queue_artifact` "binds the FILENAME to the
    member id". It does not: it BUILDS the path from `member_id` and returns
    whatever body is there, without ever asking the body whether it agrees.
    That wrong claim is what let the case below go untested — see the next
    test, which is the one that actually mattered.)"""
    root = tmp_path / "loops"
    victim = _write_proposal(root)
    member = content_id(_cand_payload("member", with_resolves=False))
    _write_candidate(root, spoof_id=member, payload={"resolves": victim})
    loop = _loop(root)

    loop._terminalize_resolved_proposals(member, MERGE_SHA,
                                         "receipts/land.json", "train/t")

    assert _events(root) == []
    assert gl.terminal_event_for(root, victim) is None
    assert any("content_id(payload) != id" in ln for ln in loop.lines), loop.lines


def test_the_daemon_refuses_a_self_consistent_body_at_another_members_path(
        tmp_path: Path) -> None:
    """THE ROUND-2 BLOCKER, and the sharp one.

    A self-consistency check alone is not a barrier at all here: the attacker
    picks the payload AND the id together, so `{"id": content_id(P), "payload":
    P}` is trivially well-formed — no preimage needed. Written at the path of a
    candidate that really merged, it used to pass, and the retirement it caused
    cited the REAL member id and the REAL merge sha as cover. The preimage
    barrier only exists once the body's id is pinned to a key the attacker does
    not choose, which here is `member_id`."""
    root = tmp_path / "loops"
    victim = _write_proposal(root)
    member = content_id(_cand_payload("the-candidate-that-really-landed"))
    forged = {"resolves": victim}
    _write_candidate(root, spoof_id=content_id(forged),   # self-consistent...
                     filename=f"{_stem(member)}.json",    # ...at HIS path
                     payload=forged)
    loop = _loop(root)

    loop._terminalize_resolved_proposals(member, MERGE_SHA,
                                         "receipts/land.json", "train/t")

    assert _events(root) == []
    assert gl.terminal_event_for(root, victim) is None
    assert any("is not the" in ln and "it was read as" in ln
               for ln in loop.lines), loop.lines
    # NOT asserted: that the victim is still SELECTABLE. It is not, and that is
    # a separate and much weaker weakness in `spawn_builders._live_resolver_targets`,
    # which reads `resolves` off any candidate file without proving its identity
    # either. The consequence there is only that work is WITHHELD from a builder
    # while the file exists — recoverable, and not this module's to fix. What
    # must never happen is the unrepairable one: a terminal event on the ledger.


def test_the_already_on_main_seam_refuses_the_same_forgery(
        tmp_path: Path) -> None:
    """The second landing path reads artifacts through the same seam, so it
    inherits the same proof rather than a softer one."""
    from bridge.integration import Candidate

    repo = _init_repo(tmp_path / "repo")
    main_sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "loops"
    victim = _write_proposal(root)
    member = content_id(_cand_payload("landed-elsewhere"))
    forged = {"resolves": victim}
    path = root / "candidates" / f"{_stem(member)}.json"
    _write_candidate(root, spoof_id=content_id(forged),
                     filename=path.name, payload=forged)
    candidate = Candidate(member, path, json.loads(path.read_text()),
                          branch=main_sha, base_sha=main_sha, tip_sha=main_sha)

    _loop(root, repo)._reconcile_already_merged([candidate], main_sha)

    assert _events_for(root, victim) == []


def test_the_identity_proof_is_one_function_used_by_both_paths(
        tmp_path: Path) -> None:
    """The asymmetry that caused this finding was two paths enforcing different
    halves of the same rule. They share one function now, and this fails if a
    future edit gives either path its own copy."""
    good = {"marker": "m", "resolves": PROPOSAL}
    art = {"kind": "candidate", "id": content_id(good), "payload": good}
    assert gl.envelope_identity_problem(art, content_id(good)) is None
    assert "it was read as" in gl.envelope_identity_problem(art, CANDIDATE)
    assert col._import_queue_bridge()["envelope_identity_problem"] is \
        gl.envelope_identity_problem


def test_a_bound_envelope_is_still_retired_normally(tmp_path: Path) -> None:
    """The guard must not be a blanket refusal: a real, content-addressed
    envelope keeps working. (Measured on the live queue: 224 of 249 candidates
    are bound; the 25 that are not are named in the report, not silently
    dropped.)"""
    root = _landed_backfill_fixture(tmp_path)
    code, report = _apply(root, render=False)
    assert report["retired"] == [PROPOSAL]
    assert report["id_unbound"] == []
    assert code == 0


# ---------------------------------------------------- MAJOR: TOCTOU double-write
# The exactly-one-terminal guard was a pre-read taken before the ledger lock,
# while the transport locks only the byte append. Two retirement writers could
# both observe "not terminal" and both append — and an append-only ledger
# cannot take the second one back.


def test_concurrent_apply_writes_exactly_one_terminal_event(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reviewer's repro, with the barrier forcing both runs to read the
    ledger before either writes."""
    import threading

    from bridge.integration import LedgerView

    root = _landed_backfill_fixture(tmp_path)
    _code, dry = col.retire_proposals(root, render=False)   # one reviewed plan
    original_build = LedgerView.build
    barrier = threading.Barrier(2)

    def slow_build(path):
        view = original_build(path)
        barrier.wait()
        return view
    monkeypatch.setattr(LedgerView, "build", slow_build)

    threads = [threading.Thread(target=col.retire_proposals, args=(root,),
                                kwargs={"apply": True, "render": False,
                                        "snapshot": dry["snapshot"]})
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "retirement deadlocked"

    completed = [e for e in _events_for(root, PROPOSAL) if e["event"] == "completed"]
    assert len(completed) == 1, f"double write: {completed}"


def test_a_terminal_that_lands_between_the_scan_and_the_lock_is_honoured(
        tmp_path: Path) -> None:
    """Cross-PROCESS, and deterministic: a child holds the retirement lock,
    writes the terminal itself, then releases. The parent is already committed
    to retiring the same proposal — it must block, re-read under the lock, and
    decline. This is what proves the re-check is inside the lock and that the
    lock is a real cross-process flock, not a process-local convention."""
    import subprocess as sp
    import textwrap
    import time

    root = _landed_backfill_fixture(tmp_path)
    child_src = textwrap.dedent(f"""
        import json, sys, time
        sys.path.insert(0, {str(ROOT)!r})
        from pathlib import Path
        from bridge.gate_loop import retirement_lock
        from bridge.ledger_write import append_event
        root = Path({str(root)!r})
        with retirement_lock(root):
            Path({str(tmp_path / 'child-holds-lock')!r}).write_text("1")
            time.sleep(1.0)
            append_event(root, {{"ts": "2026-08-12T00:00:00Z", "role": "implementer",
                                 "event": "completed", "id": {PROPOSAL!r},
                                 "actor": "someone-else",
                                 "detail": {{"reason": "applied by hand"}}}})
    """)
    child = sp.Popen([sys.executable, "-c", child_src])
    try:
        held = tmp_path / "child-holds-lock"
        deadline = time.time() + 10
        while not held.exists() and time.time() < deadline:
            time.sleep(0.01)
        assert held.exists(), "child never took the lock"
        code, report = _apply(root, render=False)
    finally:
        child.wait(timeout=30)

    assert report["retired"] == []
    assert report["raced"] == [PROPOSAL]
    completed = [e for e in _events_for(root, PROPOSAL) if e["event"] == "completed"]
    assert len(completed) == 1
    assert completed[0]["actor"] == "someone-else"
    assert code == 0


def test_the_retirement_lock_is_a_file_under_locks(tmp_path: Path) -> None:
    """Named, so an operator inspecting a stuck sweep can find it — and placed
    beside the transport's own lock rather than replacing it."""
    root = tmp_path / "loops"
    root.mkdir(parents=True)
    with gl.retirement_lock(root):
        pass
    assert (root / "locks" / "proposal-retire.lock").exists()


def test_an_unrejected_proposal_can_still_be_retired(tmp_path: Path) -> None:
    """The in-lock re-check must reproduce LedgerView's ONE reversal, or a
    legitimately re-opened proposal is frozen out of retirement forever."""
    root = _landed_backfill_fixture(tmp_path)
    _append(root, {"ts": "2026-08-11T02:00:00Z", "role": "implementer",
                   "event": "rejected", "id": PROPOSAL, "actor": "x",
                   "detail": {"reason": "r", "class": "candidate-defect",
                              "expires_at": "2026-09-11T00:00:00Z"}})
    _append(root, {"ts": "2026-08-11T03:00:00Z", "role": "implementer",
                   "event": "unrejected", "id": PROPOSAL, "actor": "operator"})
    assert gl.terminal_event_for(root, PROPOSAL) is None

    code, report = _apply(root, render=False)
    assert report["retired"] == [PROPOSAL]
    assert code == 0


# ------------------------------------------- MINOR: favourable absence in the
# resolves reader — a corrupt artifact must not read as a healthy one that
# simply never stamped the link.


def test_a_missing_payload_is_distinguishable_from_a_missing_link() -> None:
    """The reviewer's repro, verbatim in intent."""
    assert col._resolved_ids({"resolves": None}) != col._resolved_ids(None)
    assert col._resolved_ids({"resolves": None}) == ([], "no-link")
    assert col._resolved_ids(None) == ([], "corrupt")


@pytest.mark.parametrize("payload", [None, "a string", 42, [], ["resolves"]])
def test_a_corrupt_payload_is_counted_as_artifact_evidence(
        tmp_path: Path, payload: object) -> None:
    root = tmp_path / "loops"
    _write_proposal(root)
    cid = _write_candidate(root, payload=payload)
    _append(root, _merged_event(cid))
    code, report = _apply(root, render=False)
    assert report["no_link"] == []                       # NOT a routine gap
    assert report["unreadable"]                          # counted as evidence
    assert report["retired"] == []
    assert code == 1        # unknown population is never a clean no-op


def test_a_resolves_that_parses_to_nothing_is_corrupt_not_no_link(
        tmp_path: Path) -> None:
    """`resolves` was stamped, so this is not the "never linked" gap — it is an
    artifact whose link cannot be read, and it must be visible as one."""
    root = tmp_path / "loops"
    cid = _write_candidate(root, resolves=["not-an-id", 7])
    _append(root, _merged_event(cid))
    _code, report = _apply(root, render=False)
    assert report["no_link"] == []
    assert any("unreadable `resolves`" in item for item in report["unreadable"])


# ============ CROSS-LINEAGE REVIEW #2, 2026-08-12 (GPT-5.6-Sol, correctness lens)
# Every landing path owes the retirement, and every emitted terminal event has
# to survive the schema. These pin the four findings not already covered above.


def test_recovery_replays_the_retirement_when_members_are_already_terminal(
        tmp_path: Path) -> None:
    """E-001. A crash between the member `merged` events and the retirement left
    every member terminal and the proposal open — and the recovery, seeing all
    members terminal, skipped the train AND cleared the intent. The retirement
    was then lost forever, which is the exact bug this lane exists to fix,
    reintroduced by its own recovery path."""
    repo = _init_repo(tmp_path / "repo")
    merge_sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "loops"
    _write_proposal(root)
    cid = _write_candidate(root)
    loop = _loop(root, repo)
    train = Train(branch="train/repro", base=merge_sha, tip=merge_sha,
                  members=[{"id": cid, "branch": "fix/thing", "base": merge_sha,
                            "paths": ["README.md"]}], paths=["README.md"])
    verdict = GateVerdict("pass", 0, "", "gate passed", None, "", 0.1)
    assert loop._write_landing_intent([(train, verdict)])
    # the crash point: the member's `merged` line made it, the retirement did not
    _append(root, _merged_event(cid, merge_sha))

    loop._recover_landing_intent(merge_sha)

    retired = [e for e in _events_for(root, PROPOSAL) if e["event"] == "completed"]
    assert len(retired) == 1, loop.lines
    assert retired[0]["detail"]["carrier_sha"] == merge_sha
    assert not loop._landing_intent_path().exists()   # and the intent is cleared


def test_recovery_is_idempotent_across_repeated_ticks(tmp_path: Path) -> None:
    """Replaying the tail must never mint a second terminal for anyone."""
    repo = _init_repo(tmp_path / "repo")
    merge_sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "loops"
    _write_proposal(root)
    cid = _write_candidate(root)
    loop = _loop(root, repo)
    train = Train(branch="train/repro", base=merge_sha, tip=merge_sha,
                  members=[{"id": cid, "branch": "fix/thing", "base": merge_sha,
                            "paths": ["README.md"]}], paths=["README.md"])
    verdict = GateVerdict("pass", 0, "", "ok", None, "", 0.1)
    for _ in range(3):
        assert loop._write_landing_intent([(train, verdict)])
        loop._recover_landing_intent(merge_sha)
    assert len([e for e in _events_for(root, PROPOSAL)
                if e["event"] == "completed"]) == 1
    assert len([e for e in _events_for(root, cid) if e["event"] == "merged"]) == 1


def test_the_already_on_main_path_retires_and_is_schema_valid(
        tmp_path: Path) -> None:
    """E-002. `_reconcile_already_merged` is a SECOND landing-bookkeeping path.
    It terminalized the candidate and stopped, so a candidate that reached main
    by any other hand left its proposal offered forever — and its `merged` event
    said `result: "already-on-main"`, which the schema's pass|fail enum rejects."""
    jsonschema = pytest.importorskip("jsonschema")
    from bridge.integration import Candidate

    repo = _init_repo(tmp_path / "repo")
    main_sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "loops"
    _write_proposal(root)
    cid = _write_candidate(root)
    art = json.loads(
        (root / "candidates" / f"{_stem(cid)}.json").read_text())
    candidate = Candidate(cid, root / "candidates" / f"{_stem(cid)}.json", art,
                          branch=main_sha, base_sha=main_sha, tip_sha=main_sha)

    remaining = _loop(root, repo)._reconcile_already_merged([candidate], main_sha)

    assert remaining == []
    merged = [e for e in _events_for(root, cid) if e["event"] == "merged"]
    assert len(merged) == 1
    jsonschema.validate(merged[0], _schema())            # must not raise
    assert merged[0]["detail"]["disposition"] == "already-on-main"
    retired = [e for e in _events_for(root, PROPOSAL) if e["event"] == "completed"]
    assert len(retired) == 1
    assert retired[0]["detail"]["carrier_sha"] == main_sha
    assert _selectable(root) == []


def test_the_already_on_main_path_still_refuses_an_unbound_envelope(
        tmp_path: Path) -> None:
    """The second path gets the same guard, not a softer one."""
    from bridge.integration import Candidate

    repo = _init_repo(tmp_path / "repo")
    main_sha = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "loops"
    victim = _write_proposal(root)
    forged = content_id(_cand_payload("honest", with_resolves=False))
    _write_candidate(root, spoof_id=forged, payload={"resolves": victim})
    path = root / "candidates" / f"{_stem(forged)}.json"
    candidate = Candidate(forged, path, json.loads(path.read_text()),
                          branch=main_sha, base_sha=main_sha, tip_sha=main_sha)

    _loop(root, repo)._reconcile_already_merged([candidate], main_sha)

    assert _events_for(root, victim) == []


@pytest.mark.parametrize("event,why", [
    ({"detail": {"reason": "r", "result": "resolved-by-candidate"}},
     "detail.result"),
    ({"detail": {"reason": "r"}, "role": "gremlin"}, "role"),
    ({"detail": {"reason": "r"}, "ts": "yesterday"}, "ts"),
    ({"detail": {"reason": "r"}, "id": "sha256:nope"}, "id"),
    ({"detail": {"reason": "r", "merge_sha": "abc"}}, "detail.merge_sha"),
    ({"detail": {"reason": "r", "expires_at": "soon"}}, "detail.expires_at"),
    ({"detail": {"reason": "r", "exit_code": "two"}}, "detail.exit_code"),
    ({"detail": {"reason": "r", "class": "vibes"}}, "detail.class"),
    ({"detail": {}}, "detail.reason"),
    ({"detail": "a string"}, "detail"),
])
def test_the_write_boundary_refuses_every_invalid_completed(
        tmp_path: Path, event: dict, why: str) -> None:
    """E-005. The guard used to check only that `detail.reason` was truthy — so
    the very event that started this lane (a bad `detail.result`) would still
    have been written. The whole schema is the bar, and the refusal names the
    field that failed."""
    root = tmp_path / "loops"
    root.mkdir(parents=True)
    loop = _loop(root)
    bad = {"ts": "2026-08-12T00:00:00Z", "role": "implementer",
           "event": "completed", "id": "sha256:" + "a" * 64, "actor": "x",
           **event}
    with pytest.raises(ValueError) as caught:
        loop._append_ledger(bad)
    assert why in str(caught.value), caught.value
    assert not (root / "ledger.jsonl").exists()          # nothing persisted


def test_the_write_boundary_still_accepts_a_valid_completed(
        tmp_path: Path) -> None:
    """The guard must refuse invalid events, not `completed` events."""
    root = tmp_path / "loops"
    root.mkdir(parents=True)
    loop = _loop(root)
    loop._append_ledger({
        "ts": "2026-08-12T00:00:00Z", "role": "implementer", "event": "completed",
        "id": "sha256:" + "a" * 64, "actor": "x",
        "detail": {"reason": "host-ops change applied and verified"}})
    assert len(_events(root)) == 1


def test_the_schema_checker_agrees_with_jsonschema_on_the_real_event(
        tmp_path: Path) -> None:
    """The structural check is the lens that survives a host without
    `jsonschema`, so it must not be weaker than the library on the one event
    this lane actually mints."""
    jsonschema = pytest.importorskip("jsonschema")
    event = gl.proposal_retirement_event(
        PROPOSAL, resolved_by=CANDIDATE, carrier_sha=MERGE_SHA,
        receipt="receipts/land.json", train="train/t")
    assert gl.ledger_event_problems(event) == []
    jsonschema.validate(event, _schema())
