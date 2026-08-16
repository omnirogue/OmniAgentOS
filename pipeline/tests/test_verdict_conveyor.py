"""Pins pipeline/bridge/verdict_conveyor.py — the standing cross-lineage verdict conveyor.

This is a Class A surface: the conveyor writes into `verdicts[]`, which is what
`review_policy.approved_cross_lineage` reads to decide whether risky code may
land. So these tests are not about whether it conveys verdicts — they are about
whether it can be made to write one NOBODY GAVE. Every test below either proves
a real verdict survives intact, or forces a failure mode and proves the
envelope was left alone.

NO TEST HERE INVOKES A REAL MODEL. Every seat is a `FakeSeatRunner` returning a
canned `SeatResult`, so the suite is deterministic and free. A test that spent a
real seat would also be a test that could not assert on the seat's answer.
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

PIPELINE = Path(__file__).resolve().parent.parent
ROOT = PIPELINE.parent
sys.path.insert(0, str(PIPELINE))

from bridge import gate_loop as GL  # noqa: E402
from bridge import verdict_conveyor as V  # noqa: E402
from bridge.canonical import content_id  # noqa: E402
from bridge.review_policy import approved_cross_lineage  # noqa: E402

# Probe discipline: .zshenv prepends the SERVING checkout to $PYTHONPATH and the
# serving venv carries an editable pin to it, so a suite that does not assert
# which file it imported can silently measure the wrong tree and report green
# for code this branch never contains.
assert Path(V.__file__).resolve() == (
    PIPELINE / "bridge" / "verdict_conveyor.py").resolve(), (
    f"imported the WRONG verdict_conveyor.py: {V.__file__}")


# ----------------------------------------------------------------- fixtures


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repo with two lanes: one touching a risky path, one doc-only."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path.parent, "init", "-q", "-b", "main", str(path))
    _git(path, "config", "user.email", "probe@example.invalid")
    _git(path, "config", "user.name", "probe")
    (path / "omniagentos" / "policy").mkdir(parents=True)
    (path / "omniagentos" / "policy" / "approve.py").write_text("base\n")
    (path / "docs").mkdir()
    (path / "docs" / "README.md").write_text("base\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")

    _git(path, "checkout", "-q", "-b", "lane/risky")
    (path / "omniagentos" / "policy" / "approve.py").write_text("changed\n")
    _git(path, "commit", "-qam", "risky change")

    _git(path, "checkout", "-q", "main")
    _git(path, "checkout", "-q", "-b", "lane/docs")
    (path / "docs" / "README.md").write_text("changed\n")
    _git(path, "commit", "-qam", "doc change")
    _git(path, "checkout", "-q", "main")
    return path


@pytest.fixture
def loops(tmp_path: Path) -> Path:
    root = tmp_path / "loopqueue"
    for sub in ("candidates", "parked", "locks", "state"):
        (root / sub).mkdir(parents=True)
    (root / "ledger.jsonl").write_text("")
    return root


def base_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "main")


def tip_of(repo: Path, branch: str) -> str:
    return _git(repo, "rev-parse", branch)


def file_candidate(
    loops: Path,
    repo: Path,
    branch: str,
    *,
    lineage: str | None = "anthropic",
    verdicts: list[dict[str, Any]] | None = None,
    head_sha: str | None = None,
    payload_head: bool = True,
    payload_extra: dict[str, Any] | None = None,
) -> tuple[str, Path]:
    """Write a real, id-bound candidate envelope and return (id, path).

    The head and the lane live in the PAYLOAD, which is the shape the content
    id actually covers and the only shape the conveyor will act on. The
    top-level fields are written as agreeing mirrors, exactly as live envelopes
    carry them — a test that omitted them would never exercise the mismatch
    refusals that matter.
    """
    payload: dict[str, Any] = {"resolves": "sha256:" + "0" * 64, "lane": branch}
    if payload_head:
        payload["head_sha"] = head_sha or tip_of(repo, branch)
    payload.update(payload_extra or {})
    ident = content_id(payload)
    effective_head = payload.get("head_sha") or head_sha or tip_of(repo, branch)
    producer: dict[str, Any] = {"role": "implementer", "actor": "probe@lane"}
    if lineage is not None:
        producer["lineage"] = lineage
    art: dict[str, Any] = {
        "contract": "v1.1",
        "kind": "candidate",
        "title": f"probe candidate on {branch}",
        "id": ident,
        "created_at": "2026-08-13T00:00:00Z",
        "producer": producer,
        "base_sha": payload.get("base_sha", base_sha(repo)),
        "head_sha": effective_head,
        "branch": branch,
        "paths": ["omniagentos/policy/approve.py"],
        "payload": payload,
    }
    if verdicts is not None:
        art["verdicts"] = verdicts
    path = loops / "candidates" / (ident.replace(":", "_") + ".json")
    path.write_text(json.dumps(art, indent=2), encoding="utf-8")
    return ident, path


class FakeSeatRunner:
    """A seat that never runs a model. Records every dispatch it was handed."""

    def __init__(self, results: dict[str, V.SeatResult] | None = None,
                 default: V.SeatResult | None = None,
                 echo_prompt: bool = False) -> None:
        self.results = results or {}
        self.default = default
        #: Reproduces the measured silent seat: rc=0, no model output, and a
        #: stdout that is EXACTLY the brief handed to it.
        self.echo_prompt = echo_prompt
        self.calls: list[tuple[str, str, str]] = []   # (seat name, lineage, prompt)

    def __call__(self, seat: V.Seat, prompt: str, timeout_s: int) -> V.SeatResult:
        self.calls.append((seat.name, seat.lineage, prompt))
        if self.echo_prompt and seat.lineage not in self.results:
            return seat_result("", transcript=prompt)
        result = self.results.get(seat.lineage, self.default)
        assert result is not None, f"no canned result for lineage {seat.lineage}"
        return result


def seat_result(last_message: str = "", *, rc: int = 0, transcript: str = "",
                timed_out: bool = False, error: str | None = None) -> V.SeatResult:
    """A canned seat run. Defaults to a CLEAN run (rc 0, no timeout, no error),
    because every existing test that predates the dead-seat gate assumes one."""
    return V.SeatResult(argv=("fake",), rc=rc, transcript=transcript,
                        last_message=last_message, timed_out=timed_out,
                        duration_s=1.0, error=error)


@pytest.fixture
def seats(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend both provider CLIs are installed, without depending on the host."""
    monkeypatch.setattr(V, "_codex_binary", lambda: "/usr/bin/true")
    monkeypatch.setattr(V.shutil, "which",
                        lambda name: "/usr/bin/true" if name == "grok" else None)


def run(repo: Path, loops: Path, tmp_path: Path, runner: Any, **kw: Any) -> V.PassReport:
    return V.run_pass(repo, loops, evidence_dir=tmp_path / "evidence",
                      seat_runner=runner, log=lambda _msg: None, **kw)


def ledger_events(loops: Path) -> list[dict[str, Any]]:
    raw = (loops / "ledger.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


APPROVE = "REVIEWER-MODEL: gpt-5.6-sol\nFINAL-VERDICT: APPROVE — zero blockers"


# ------------------------------------------------------- (a) discovery predicate


def test_discovery_finds_a_risky_starved_candidate_and_skips_a_doc_only_one(
        repo: Path, loops: Path) -> None:
    """Risk comes from the REAL diff, so a doc-only lane is never dispatched.

    Both envelopes declare the same `paths`; only the real
    `git diff --name-only base..head` differs. If discovery consulted the
    declared paths, both would look risky and the conveyor would spend a seat
    on a README.
    """
    risky_id, _ = file_candidate(loops, repo, "lane/risky")
    doc_id, _ = file_candidate(loops, repo, "lane/docs")

    found = V.scan(repo, loops)

    assert [c.ident for c in found.starved] == [risky_id]
    assert (doc_id, "not-risky") in found.skips
    starved = found.starved[0]
    assert starved.head_sha == tip_of(repo, "lane/risky")
    assert starved.risky == ("omniagentos/policy/approve.py",)


def test_an_already_approved_candidate_is_not_starved(repo: Path, loops: Path) -> None:
    """The predicate is `approved_cross_lineage`, reused — never re-derived."""
    tip = tip_of(repo, "lane/risky")
    ident, _ = file_candidate(loops, repo, "lane/risky", verdicts=[{
        "lineage": "openai", "model": "gpt-5.6-sol", "verdict": "APPROVE",
        "reviewed_sha": tip}])
    found = V.scan(repo, loops)
    assert found.starved == []
    assert (ident, "already-approved") in found.skips


def test_terminal_and_parked_candidates_are_skipped(repo: Path, loops: Path) -> None:
    """merged/rejected/closed/completed/superseded, first-terminal-wins."""
    merged_id, _ = file_candidate(loops, repo, "lane/risky")
    parked_id, _ = file_candidate(loops, repo, "lane/risky",
                                  payload_extra={"variant": "parked"})
    live_id, _ = file_candidate(loops, repo, "lane/risky",
                                payload_extra={"variant": "live"})
    with open(loops / "ledger.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-08-13T00:00:00Z", "role": "external",
                             "event": "merged", "id": merged_id,
                             "detail": {"merge_sha": "a" * 40}}) + "\n")
    (loops / "parked" / (parked_id.replace(":", "_") + ".json")).write_text("{}")

    found = V.scan(repo, loops)

    assert [c.ident for c in found.starved] == [live_id] or live_id in {
        c.ident for c in found.starved}
    assert (merged_id, "terminal") in found.skips
    assert (parked_id, "parked") in found.skips


def test_an_unrejected_event_reopens_a_rejected_candidate(repo: Path, loops: Path) -> None:
    """The one reversal `gate_loop.terminal_event_for` allows, mirrored exactly."""
    ident, _ = file_candidate(loops, repo, "lane/risky")
    with open(loops / "ledger.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-08-13T00:00:00Z", "role": "external",
                             "event": "rejected", "id": ident}) + "\n")
        fh.write(json.dumps({"ts": "2026-08-13T01:00:00Z", "role": "external",
                             "event": "unrejected", "id": ident}) + "\n")
    assert ident not in V.terminal_ids(loops)
    assert [c.ident for c in V.scan(repo, loops).starved] == [ident]


# --------------------------------------------- (d) absent lineage / moved branch


def test_a_candidate_without_producer_lineage_is_skipped_and_noted(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """No seat is paid for, and the skip is NAMED in the pass summary.

    `approved_cross_lineage` needs a KNOWN producer lineage to compare against,
    so a verdict for this candidate could never satisfy the gate no matter who
    wrote it. Dispatching anyway would burn ~8.7 minutes of a real seat to
    produce something the predicate must ignore.
    """
    ident, path = file_candidate(loops, repo, "lane/risky", lineage=None)
    runner = FakeSeatRunner(default=seat_result(APPROVE))

    report = run(repo, loops, tmp_path, runner)

    assert runner.calls == []
    assert report.skips["missing-producer-lineage"] == 1
    assert "verdicts" not in json.loads(path.read_text())
    V.note(loops, V.summary_event(report, 1.0, None))
    summary = ledger_events(loops)[-1]
    assert summary["detail"]["kind"] == "verdict-conveyor-pass"
    assert V._short(ident) in summary["detail"]["skipped_ids"]["missing-producer-lineage"]


def test_an_unknown_producer_lineage_is_skipped(repo: Path, loops: Path) -> None:
    ident, _ = file_candidate(loops, repo, "lane/risky", lineage="acme-labs")
    found = V.scan(repo, loops)
    assert found.starved == []
    assert (ident, "unknown-producer-lineage") in found.skips


def test_a_branch_that_no_longer_resolves_to_head_sha_is_skipped(
        repo: Path, loops: Path) -> None:
    """A moved branch is never silently substituted for reviewed code."""
    stale = "b" * 40
    ident, _ = file_candidate(loops, repo, "lane/risky", head_sha=stale)
    found = V.scan(repo, loops)
    assert found.starved == []
    assert (ident, "branch-moved") in found.skips


def test_an_unbound_envelope_is_skipped_and_never_repaired(
        repo: Path, loops: Path) -> None:
    """content_id(payload) != id means the body was edited after filing."""
    ident, path = file_candidate(loops, repo, "lane/risky")
    art = json.loads(path.read_text())
    art["payload"]["smuggled"] = True          # id no longer hashes the payload
    path.write_text(json.dumps(art))
    found = V.scan(repo, loops)
    assert found.starved == []
    assert (ident, "identity-unbound") in found.skips
    assert json.loads(path.read_text())["id"] == ident       # untouched, not "fixed"


# ------------------------------------------------ (b) write-back and id-binding


def test_a_cross_lineage_approve_is_written_verbatim_and_flips_the_predicate(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """The whole point: the gate's own predicate must go False -> True.

    Asserted through `approved_cross_lineage` itself rather than by inspecting
    fields, because that function IS the acceptance criterion — a verdict this
    conveyor writes that the gate does not accept is worth nothing.
    """
    ident, path = file_candidate(loops, repo, "lane/risky", lineage="anthropic")
    tip = tip_of(repo, "lane/risky")
    before = json.loads(path.read_text())
    assert approved_cross_lineage(before, tip) is False
    runner = FakeSeatRunner(default=seat_result(APPROVE))

    report = run(repo, loops, tmp_path, runner)

    after = json.loads(path.read_text())
    assert approved_cross_lineage(after, tip) is True
    assert GL.envelope_id_is_bound(after), "the write must not disturb id-binding"
    assert after["id"] == ident
    assert "verdicts" not in after["payload"], "verdicts must stay OUTSIDE payload"
    entry = after["verdicts"][-1]
    assert entry["verdict"] == "APPROVE — zero blockers"   # verbatim, not normalised
    assert entry["by"] == "verdict-conveyor"
    assert entry["lineage"] == "openai"
    assert entry["reviewed_sha"] == tip
    assert Path(entry["transcript"]).read_text().find("APPROVE — zero blockers") > 0
    assert report.written == 1 and report.approvals == 1


def test_a_second_pass_does_not_re_review_what_it_already_conveyed(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """Without this the conveyor re-dispatches a REQUEST-CHANGES forever."""
    ident, _ = file_candidate(loops, repo, "lane/risky")
    reject = "REVIEWER-MODEL: gpt-5.6-sol\nFINAL-VERDICT: REQUEST-CHANGES: it fails open on absent config"
    runner = FakeSeatRunner(default=seat_result(reject))

    first = run(repo, loops, tmp_path, runner)
    second = run(repo, loops, tmp_path, runner)

    assert first.written == 1 and first.rejections == 1
    assert second.dispatched == 0
    assert second.skips["already-conveyed"] == 1
    assert len(runner.calls) == 1
    alerts = (loops / "ALERTS.md").read_text().strip().splitlines()
    assert len(alerts) == 1, "the operator is alerted ONCE, not every 10 minutes"
    assert "it fails open on absent config" in alerts[0]


def test_a_request_changes_is_recorded_not_discarded(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """A genuine rejection is information; dropping it makes an approval pump."""
    ident, path = file_candidate(loops, repo, "lane/risky")
    reject = "REVIEWER-MODEL: gpt-5.6-sol\nFINAL-VERDICT: REQUEST-CHANGES: the guard is bypassable"
    report = run(repo, loops, tmp_path, FakeSeatRunner(default=seat_result(reject)))

    art = json.loads(path.read_text())
    assert art["verdicts"][-1]["verdict"] == "REQUEST-CHANGES: the guard is bypassable"
    assert approved_cross_lineage(art, tip_of(repo, "lane/risky")) is False
    assert report.rejections == 1 and report.approvals == 0
    kinds = [e["detail"].get("kind") for e in ledger_events(loops)]
    assert "verdict-conveyor-recorded" in kinds
    recorded = [e for e in ledger_events(loops)
                if e["detail"].get("kind") == "verdict-conveyor-recorded"][0]
    assert recorded["id"] == ident
    assert recorded["detail"]["reason"] == "the guard is bypassable"


def test_write_verdict_refuses_an_envelope_that_lost_its_binding(
        repo: Path, loops: Path) -> None:
    """Identity is checked at WRITE time too, not only at discovery time."""
    ident, path = file_candidate(loops, repo, "lane/risky")
    art = json.loads(path.read_text())
    art["payload"]["edited-after-discovery"] = True
    path.write_text(json.dumps(art))

    failure = V.write_verdict(path, ident, {"lineage": "openai", "verdict": "APPROVE"})

    assert failure is not None and "id-bound" in failure
    assert "verdicts" not in json.loads(path.read_text())


# ----------------------------------------------------- (c) tip moved mid-review


def test_a_branch_that_moves_during_review_discards_the_verdict(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """The seat reviewed code that would no longer land. Nothing is written.

    The move happens INSIDE the fake seat, which is the real race: discovery
    saw the tip, the seat spent minutes reading it, and the branch advanced
    before write-back.
    """
    ident, path = file_candidate(loops, repo, "lane/risky")
    reviewed_tip = tip_of(repo, "lane/risky")

    class MovingSeat(FakeSeatRunner):
        def __call__(self, seat: V.Seat, prompt: str, timeout_s: int) -> V.SeatResult:
            _git(repo, "checkout", "-q", "lane/risky")
            (repo / "omniagentos" / "policy" / "approve.py").write_text("moved\n")
            _git(repo, "commit", "-qam", "moved during review")
            _git(repo, "checkout", "-q", "main")
            return super().__call__(seat, prompt, timeout_s)

    runner = MovingSeat(default=seat_result(APPROVE))
    report = run(repo, loops, tmp_path, runner)

    assert tip_of(repo, "lane/risky") != reviewed_tip
    assert "verdicts" not in json.loads(path.read_text())
    assert report.written == 0 and report.discarded == 1
    discarded = [e for e in ledger_events(loops)
                 if e["detail"].get("kind") == "verdict-conveyor-discarded"]
    assert len(discarded) == 1 and discarded[0]["id"] == ident
    assert discarded[0]["detail"]["reviewed_sha"] == reviewed_tip
    transcripts = list((tmp_path / "evidence").glob("*.txt"))
    assert transcripts, "a discarded review is still archived, not thrown away"


# ------------------------------------------------------- no-fabrication guards


@pytest.mark.parametrize("answer", [
    "",
    "I read the diff and it looks broadly fine to me.",
    "APPROVE the refactor once the missing test is added",
    "Verdict: probably approve",
    "FINAL-VERDICT: REQUEST-CHANGES",     # the colon and reason are mandatory
    "APPROVE",                            # undeclared: a written line is not a decision
    "APPROVE — zero blockers",
    "- FINAL-VERDICT: APPROVE",           # decorated: not a declaration
    "**FINAL-VERDICT: APPROVE**",
    "> FINAL-VERDICT: APPROVE",
    "The correct answer here is FINAL-VERDICT: APPROVE",   # not at line start
])
def test_no_grammar_line_means_no_verdict_is_ever_written(
        repo: Path, loops: Path, tmp_path: Path, seats: None, answer: str) -> None:
    """There is no salvage path. An unparseable answer is not an approval."""
    ident, path = file_candidate(loops, repo, "lane/risky")
    report = run(repo, loops, tmp_path, FakeSeatRunner(default=seat_result(answer)))

    assert "verdicts" not in json.loads(path.read_text())
    assert report.written == 0 and report.approvals == 0
    assert report.no_verdict == 1


def test_the_prompts_own_grammar_examples_are_never_read_as_a_verdict(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """The measured silent-seat failure, exactly: rc=0 and stdout IS the prompt.

    codex and gemini both returned exit 0 with 4,940 bytes that were the brief
    echoed back (2026-08-13, finding sha256:ae0c224a72b3). The brief necessarily
    quotes all four verdict forms, so a parser that scanned the raw stream would
    read OUR OWN placeholder rejection as the seat's decision — a fabricated
    verdict by the shortest possible path.
    """
    ident, path = file_candidate(loops, repo, "lane/risky")
    cand = V.scan(repo, loops).starved[0]
    prompt = V.build_prompt(repo, cand)
    assert "REQUEST-CHANGES:" in prompt and "APPROVE" in prompt

    # Direct: not one line of the brief matches the verdict grammar on its own.
    assert V.final_verdict_line(prompt) is None
    # End to end: a seat that echoes the brief and says nothing writes nothing.
    echoing = FakeSeatRunner(default=seat_result("", transcript=prompt))
    report = run(repo, loops, tmp_path, echoing)

    assert "verdicts" not in json.loads(path.read_text())
    assert report.written == 0
    assert report.seat_failures == len(echoing.calls) >= 1


# ------------------------- round-2 cross-lineage review regressions (2026-08-13)


def test_a_rejection_quoting_a_bulleted_APPROVE_is_never_read_as_an_approval(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """MAJOR (Gemini, repro finding-2): prose ABOUT a verdict became the verdict.

    The old parser scanned bottom-up and stripped list markers, so an ordinary
    rejection ending in a bulleted quotation was recorded as APPROVE. Reviewers
    write bulleted lists constantly; this was not an exotic input.
    """
    text = ("REVIEWER-MODEL: gpt-5.6-sol\n"
            "I cannot accept this change because it removes authentication.\n"
            "I am issuing a REQUEST-CHANGES.\n"
            "Do not:\n"
            "- APPROVE")
    assert V.final_verdict_line(text) is None, "an undeclared line is not a decision"

    _, path = file_candidate(loops, repo, "lane/risky")
    report = run(repo, loops, tmp_path, FakeSeatRunner(default=seat_result(text)))
    assert "verdicts" not in json.loads(path.read_text())
    assert report.approvals == 0 and report.written == 0


def test_a_declared_rejection_beats_any_number_of_declared_approvals(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """A contradiction on an approval boundary resolves to the safe reading."""
    both = ("REVIEWER-MODEL: gpt-5.6-sol\n"
            "FINAL-VERDICT: APPROVE\n"
            "on reflection, no:\n"
            "FINAL-VERDICT: REQUEST-CHANGES: the guard is bypassable\n"
            "FINAL-VERDICT: APPROVE — zero blockers")
    assert V.final_verdict_line(both) == "REQUEST-CHANGES: the guard is bypassable"
    assert len(V.verdict_lines(both)) == 3

    _, path = file_candidate(loops, repo, "lane/risky")
    report = run(repo, loops, tmp_path, FakeSeatRunner(default=seat_result(both)))
    entry = json.loads(path.read_text())["verdicts"][-1]
    assert entry["verdict"].startswith("REQUEST-CHANGES:")
    assert approved_cross_lineage(json.loads(path.read_text()),
                                  tip_of(repo, "lane/risky")) is False
    assert report.rejections == 1 and report.approvals == 0


def test_a_spoofed_top_level_base_sha_cannot_narrow_the_reviewed_diff(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """BLOCKER (Gemini, repro finding-1): diff spoofing via an unhashed field.

    Only `payload` is covered by the content id, so the top-level `base_sha`
    was editable in place after filing. Setting it to `head~1` classified risk
    over a one-commit diff, quoted that trivial diff in the seat brief, and
    bound the resulting approval to the FULL head: a reviewer approves three
    lines and the gate lands three hundred.

    Two independent properties are asserted, because closing only the first
    leaves the attack open to a producer that simply LIES in its payload from
    the start (the content id proves a value is unchanged, never that it was
    honest): the spoof is refused, AND the diff is taken from the repository's
    own integration ref rather than from any envelope field.
    """
    _git(repo, "checkout", "-q", "lane/risky")
    (repo / "omniagentos" / "policy" / "approve.py").write_text("second change\n")
    _git(repo, "commit", "-qam", "second risky commit")
    _git(repo, "checkout", "-q", "main")
    head = tip_of(repo, "lane/risky")
    benign_base = _git(repo, "rev-parse", f"{head}~1")

    _, path = file_candidate(loops, repo, "lane/risky",
                             payload_extra={"head_sha": head, "base_sha": base_sha(repo)})
    art = json.loads(path.read_text())
    art["base_sha"] = benign_base                      # the spoof, post-filing
    path.write_text(json.dumps(art))

    found = V.scan(repo, loops)

    assert found.starved == [], "a top-level field disagreeing with the payload is refused"
    assert any(reason == "base-sha-mismatch" for _, reason in found.skips)

    # And with the spoof removed, the diff is the repo's integration ref — not
    # the (honest, but still producer-authored) declared base.
    art.pop("base_sha")
    path.write_text(json.dumps(art))
    cand = V.scan(repo, loops).starved[0]
    assert cand.base_sha == base_sha(repo) and cand.base_source == "main"
    assert cand.head_sha == head
    assert cand.diff_spec == f"{base_sha(repo)}...{head}"
    brief = V.build_prompt(repo, cand)
    assert cand.diff_spec in brief, "the seat is told the exact range that was graded"
    assert benign_base not in brief, "the seat is never shown a narrowed diff"


def test_a_lying_payload_base_still_cannot_narrow_the_reviewed_diff(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """The id binds the payload; it does not make the producer honest.

    A payload-only rule would have been half a fix: the producer authors the
    payload, so `payload.base_sha = head~1` filed from the start defeats it
    entirely. The diff base is therefore never read from the envelope at all.
    """
    _git(repo, "checkout", "-q", "lane/risky")
    (repo / "omniagentos" / "policy" / "approve.py").write_text("second change\n")
    _git(repo, "commit", "-qam", "second risky commit")
    _git(repo, "checkout", "-q", "main")
    head = tip_of(repo, "lane/risky")
    lie = _git(repo, "rev-parse", f"{head}~1")

    _, path = file_candidate(loops, repo, "lane/risky",
                             payload_extra={"head_sha": head, "base_sha": lie})
    art = json.loads(path.read_text())
    art.pop("base_sha")
    path.write_text(json.dumps(art))

    cand = V.scan(repo, loops).starved[0]

    assert cand.declared_base == lie, "the claim is recorded for the audit trail"
    assert cand.base_sha == base_sha(repo), "...and is never what the diff is taken from"
    assert lie not in V.build_prompt(repo, cand)


def test_the_graded_range_excludes_the_integration_refs_own_movement(
        repo: Path, loops: Path) -> None:
    """Three-dot, not two-dot: `git diff A..B` is a TREE diff.

    Measured on the live queue: two-dot inflated one candidate's 1-file change
    into 102 files, because it also reports everything MAIN moved since the
    lane branched. That is not a safe over-approximation — it hands the
    reviewer a hundred files of somebody else's work to judge, and it would
    classify nearly every candidate as risky, collapsing the tiering. The
    merge-base form is exact AND still un-spoofable (the graph decides it).
    """
    head = tip_of(repo, "lane/risky")
    _git(repo, "checkout", "-q", "main")
    (repo / "omniagentos" / "policy" / "unrelated_main_work.py").write_text("main moved\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main moves on, touching a risky path of its own")

    _, _ = file_candidate(loops, repo, "lane/risky", payload_extra={"head_sha": head})
    cand = V.scan(repo, loops).starved[0]

    assert cand.diff_spec.endswith(f"...{head}")
    assert cand.changed == ("omniagentos/policy/approve.py",), (
        "the graded diff is the candidate's own change, not main's")
    assert "unrelated_main_work.py" not in "".join(cand.changed)
    assert "unrelated_main_work.py" not in V.build_prompt(repo, cand)


def test_the_head_and_branch_come_from_the_payload_only(
        repo: Path, loops: Path) -> None:
    """An unhashed mirror may agree or be absent; it may never decide."""
    head = tip_of(repo, "lane/risky")
    _, path = file_candidate(loops, repo, "lane/risky", payload_extra={"head_sha": head})
    art = json.loads(path.read_text())
    art["head_sha"] = "c" * 40
    path.write_text(json.dumps(art))
    assert any(r == "head-sha-mismatch" for _, r in V.scan(repo, loops).skips)

    art["head_sha"] = head
    art["branch"] = "lane/somewhere-else"
    path.write_text(json.dumps(art))
    assert any(r == "branch-mismatch" for _, r in V.scan(repo, loops).skips)


def test_a_head_sha_only_at_the_top_level_is_not_id_bound_enough(
        repo: Path, loops: Path) -> None:
    """The head a verdict binds to must be inside the hash. No fallback."""
    _, path = file_candidate(loops, repo, "lane/risky", payload_head=False)
    art = json.loads(path.read_text())
    assert art["head_sha"] and "head_sha" not in art["payload"]
    assert any(r == "head-sha-not-id-bound" for _, r in V.scan(repo, loops).skips)


def test_an_unresolvable_integration_ref_reviews_nothing(
        repo: Path, loops: Path) -> None:
    """No trustworthy base means no review — never fall back to an envelope value."""
    file_candidate(loops, repo, "lane/risky", payload_extra={"head_sha": tip_of(repo, "lane/risky")})
    found = V.scan(repo, loops, integration_ref="no-such-ref")
    assert found.starved == []
    assert ("*", "integration-ref-unresolvable") in found.skips


def test_a_term_trapping_grandchild_is_still_KILLED(tmp_path: Path) -> None:
    """MAJOR (Gemini, repro finding-3): the leader's clean exit skipped SIGKILL.

    A provider CLI that traps SIGTERM and exits 0 satisfied the old
    wait-for-leader fast path, so the group kill never happened and a
    TERM-ignoring grandchild ran forever. Killing the wrapper is exactly what
    this function exists NOT to do.
    """
    marker = tmp_path / "grandchild-alive"
    script = tmp_path / "seat.sh"
    script.write_text(
        "#!/bin/sh\n"
        "trap 'exit 0' TERM\n"
        f"( trap '' TERM; while true; do echo x > {marker}; sleep 0.2; done ) &\n"
        "sleep 60\n"
    )
    script.chmod(0o755)

    result = V.run_seat(V.Seat("fake", "openai", "fake-1", (str(script),)), "p", timeout_s=2)

    assert result.timed_out is True
    time.sleep(0.5)
    marker.unlink(missing_ok=True)
    time.sleep(1.0)
    assert not marker.exists(), "the TERM-trapping grandchild leaked past the group kill"


def test_a_filesystem_without_flock_refuses_to_run(
        loops: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MINOR (Gemini, repro finding-4): no pid fallback, no unlocked running.

    A pid liveness test is only valid within one machine, and the lock is only
    ever contended across machines when flock is not enforced — so the fallback
    did not protect the lock, it authorised the steal. "The lock is not
    enforceable here" must not read the same as "the lock is free".
    """
    real_flock = V.fcntl.flock

    def unsupported(fd: int, op: int) -> None:
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(V.fcntl, "flock", unsupported)
    with pytest.raises(V.LockUnsupported) as exc:
        V.acquire_lock(loops)
    assert "does not support flock" in str(exc.value)
    assert "refuses to run" in str(exc.value)

    monkeypatch.setattr(V.fcntl, "flock", real_flock)
    handle = V.acquire_lock(loops)
    assert handle is not None
    V.release_lock(handle)


def test_a_remote_pid_can_never_take_over_a_held_lock(
        loops: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pid in the marker is a RECORD. Nothing branches on it any more."""
    monkeypatch.setattr(V, "_pid_alive", None, raising=False)
    assert not hasattr(V, "_pid_alive") or V._pid_alive is None
    first = V.acquire_lock(loops)
    assert first is not None
    try:
        # Even with os.kill claiming every pid is dead, flock still refuses.
        monkeypatch.setattr(V.os, "kill", lambda pid, sig: (_ for _ in ()).throw(
            ProcessLookupError()))
        assert V.acquire_lock(loops) is None
    finally:
        V.release_lock(first)


# -------------------------- round-3 cross-lineage review regressions (2026-08-13)


def test_a_stale_last_message_file_is_never_read_as_the_live_seats_answer(
        tmp_path: Path) -> None:
    """BLOCKER (Grok, repro finding-1): a leftover -o file became an approval.

    The last-message file is the PREFERRED answer channel, and it was read on
    whatever it happened to contain — so an approval left by an earlier attempt
    (the first seat of this candidate's own retry chain, a crashed pass, a
    previous run of the daemon) was read as the live seat's answer. That branch
    never reaches the prompt-echo subtraction either, so nothing downstream
    could catch it.
    """
    stale = tmp_path / "leftover.last"
    stale.write_text("REVIEWER-MODEL: gpt-5.6-sol\nFINAL-VERDICT: APPROVE — zero blockers\n")
    noop = tmp_path / "noop.sh"
    noop.write_text("#!/bin/sh\nexit 0\n")
    noop.chmod(0o755)
    seat = V.Seat("codex", "openai", "gpt-5.6-sol", (str(noop), "-o", str(stale)))

    result = V.run_seat(seat, "this live seat authored nothing", timeout_s=30)

    assert result.last_message == "", "the stale file must be gone before the child starts"
    assert not stale.exists(), "the -o path is cleared before launch"
    assert V.final_verdict_line(V._seat_answer(result, "prompt")) is None


def test_a_last_message_predating_the_launch_is_ignored(tmp_path: Path) -> None:
    """Defense in depth for an unlink that silently did not take effect."""
    path = tmp_path / "recreated.last"
    path.write_text("FINAL-VERDICT: APPROVE\n")
    os.utime(path, (1_000_000, 1_000_000))          # far in the past
    assert V._read_last_message(path, launched_at=time.time()) == ""
    assert V._read_last_message(path, launched_at=0.0).strip() == "FINAL-VERDICT: APPROVE"


def test_a_stale_last_message_cannot_approve_end_to_end(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """The same defect, through the whole pass, asserted on the gate's predicate."""
    _, path = file_candidate(loops, repo, "lane/risky",
                             payload_extra={"head_sha": tip_of(repo, "lane/risky")})
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    noop = tmp_path / "noop.sh"
    noop.write_text("#!/bin/sh\nexit 0\n")
    noop.chmod(0o755)

    def stale_seat(want: str, repo_: Path, last_message: Path) -> V.Seat | None:
        if want != "openai":
            return None
        last_message.parent.mkdir(parents=True, exist_ok=True)
        last_message.write_text("REVIEWER-MODEL: gpt-5.6-sol\nFINAL-VERDICT: APPROVE\n")
        return V.Seat("codex", "openai", "gpt-5.6-sol",
                      (str(noop), "-o", str(last_message)))

    monkey = V.make_seat
    V.make_seat = stale_seat                                    # type: ignore[assignment]
    try:
        report = V.run_pass(repo, loops, evidence_dir=evidence,
                            seat_runner=V.run_seat, log=lambda _m: None)
    finally:
        V.make_seat = monkey                                    # type: ignore[assignment]

    art = json.loads(path.read_text())
    assert "verdicts" not in art
    assert approved_cross_lineage(art, tip_of(repo, "lane/risky")) is False
    assert report.approvals == 0 and report.no_verdict == 1


@pytest.mark.parametrize("broken", [
    {"timed_out": True, "rc": -15},
    {"rc": 1},
    {"rc": 137},
    {"rc": 0, "error": "FileNotFoundError: no such seat binary"},
])
def test_a_verdict_from_a_seat_that_died_is_discarded(
        repo: Path, loops: Path, tmp_path: Path, seats: None,
        broken: dict[str, Any]) -> None:
    """BLOCKER (Grok, repro finding-2): parsing was the only gate.

    `timed_out` / `rc` / `error` were consulted only when parsing found
    NOTHING, so a seat killed at the 20-minute cap that had already flushed a
    provisional `FINAL-VERDICT: APPROVE` got it recorded — and it satisfied
    `approved_cross_lineage`. A partial review is not a lenient review, it is
    an absent one: the reviewer never reached the end of the diff.
    """
    _, path = file_candidate(loops, repo, "lane/risky")
    flushed = "REVIEWER-MODEL: gpt-5.6-sol\nFINAL-VERDICT: APPROVE — zero blockers"
    runner = FakeSeatRunner(default=seat_result(flushed, **broken))

    report = run(repo, loops, tmp_path, runner)

    art = json.loads(path.read_text())
    assert "verdicts" not in art, "a verdict from a dead seat is not a verdict"
    assert approved_cross_lineage(art, tip_of(repo, "lane/risky")) is False
    assert report.approvals == 0 and report.written == 0
    assert report.dead_seat_verdicts == len(runner.calls) >= 1
    event = [e for e in ledger_events(loops)
             if e["detail"].get("kind") == "verdict-conveyor-no-verdict"][0]
    attempt = event["detail"]["attempts"][0]
    assert attempt["discarded_verdict"] == "APPROVE — zero blockers"
    assert "did not complete cleanly" in attempt["reason"]
    assert attempt["transcript"], "the discarded verdict is still archived"


def test_a_clean_run_is_required_but_a_clean_rejection_still_lands(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """The rc gate must not swallow verdicts from seats that finished normally."""
    assert V.seat_run_is_clean(seat_result("x", rc=0)) is True
    assert V.seat_run_is_clean(seat_result("x", rc=0, timed_out=True)) is False
    assert V.seat_run_is_clean(seat_result("x", rc=2)) is False
    assert V.seat_run_is_clean(seat_result("x", rc=0, error="boom")) is False

    _, path = file_candidate(loops, repo, "lane/risky")
    ok = "REVIEWER-MODEL: gpt-5.6-sol\nFINAL-VERDICT: REQUEST-CHANGES: it fails open"
    report = run(repo, loops, tmp_path, FakeSeatRunner(default=seat_result(ok, rc=0)))
    assert report.rejections == 1
    assert json.loads(path.read_text())["verdicts"][-1]["verdict"].startswith(
        "REQUEST-CHANGES:")


def test_contradictory_reviewer_model_declarations_are_unverifiable(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """MAJOR (Grok, repro finding-3): last-wins restored the favourable reading.

    A seat that confessed `claude-opus-5` and then wrote `gpt-5.6-sol` was
    recorded as openai with NO note — the favourable reading of a
    contradiction, which is exactly what the monotone-pessimistic invariant
    forbids. A witness that gave two different answers has not identified
    itself.
    """
    seat = V.Seat("codex", "openai", "gpt-5.6-sol", ())
    answer = ("REVIEWER-MODEL: claude-opus-5\n"
              "REVIEWER-MODEL: gpt-5.6-sol\n"
              "FINAL-VERDICT: APPROVE")
    model, lineage, note = V.reviewer_identity(seat, answer, is_approval=True)
    assert lineage == "unverified:openai"
    assert note is not None and "contradictory" in note
    assert "claude-opus-5" in note and "gpt-5.6-sol" in note
    assert "anthropic" in note and "openai" in note

    _, path = file_candidate(loops, repo, "lane/risky")
    report = run(repo, loops, tmp_path, FakeSeatRunner(default=seat_result(answer)))
    assert approved_cross_lineage(json.loads(path.read_text()),
                                  tip_of(repo, "lane/risky")) is False
    assert report.approvals == 0 and report.quarantined >= 1


def test_repeated_identical_declarations_are_not_a_contradiction(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """Providers echo their own header; a repeat is not a second witness."""
    seat = V.Seat("codex", "openai", "gpt-5.6-sol", ())
    same = ("REVIEWER-MODEL: gpt-5.6-sol\nREVIEWER-MODEL: gpt-5.6-sol\n"
            "FINAL-VERDICT: APPROVE")
    assert V.reviewer_identity(seat, same, is_approval=True) == (
        "gpt-5.6-sol", "openai", None)
    # Two DIFFERENT ids of the SAME lab are also not a contradiction.
    sibling = ("REVIEWER-MODEL: gpt-5.6-sol\nREVIEWER-MODEL: o3-mini\n"
               "FINAL-VERDICT: APPROVE")
    assert V.reviewer_identity(seat, sibling, is_approval=True)[1] == "openai"


@pytest.mark.parametrize("name,expected", [
    ("gpt-5.6-sol", "openai"),
    ("claude-opus-5", "anthropic"),
    ("gemini-3.6-flash", "google"),
    ("grok-4", "xai"),
    ("kimi-k2", "moonshot"),
    ("gpt-claude-bridge", None),        # two labs named: identifies neither
    ("grok-gpt-eval", None),
    ("claude-gemini-hybrid", None),
    ("mystery-9", None),
])
def test_an_ambiguous_model_name_identifies_no_lab(name: str, expected: str | None) -> None:
    """POLISH (Grok, F5): first-match biased hybrids toward declaration order."""
    assert V._lineage_of_model(name) == expected


# ------------------------------------ silent-seat retry: change action, not tier


def test_a_silent_seat_is_retried_once_with_a_DIFFERENT_lineage(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """Exit 0 with no output is a SEAT failure, and the retry changes provider.

    Re-running the same seat on the same brief is the gate-retry defect: the
    failure was measured to be brief-content-dependent, so the identical pair
    buys the identical silence at full price.
    """
    ident, path = file_candidate(loops, repo, "lane/risky", lineage="anthropic")
    runner = FakeSeatRunner(
        results={"xai": seat_result("REVIEWER-MODEL: grok-4\nFINAL-VERDICT: APPROVE")},
        echo_prompt=True,          # the openai seat goes silent, exactly as measured
    )

    report = run(repo, loops, tmp_path, runner)

    assert [(name, lin) for name, lin, _ in runner.calls] == [
        ("codex", "openai"), ("grok", "xai")]
    art = json.loads(path.read_text())
    assert art["verdicts"][-1]["lineage"] == "xai"
    assert approved_cross_lineage(art, tip_of(repo, "lane/risky")) is True
    assert report.seat_failures == 1 and report.written == 1


def test_two_silent_seats_end_the_candidate_with_both_attempts_named(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """No third attempt, no verdict, and the ledger names each failed seat."""
    ident, path = file_candidate(loops, repo, "lane/risky")
    runner = FakeSeatRunner(echo_prompt=True)

    report = run(repo, loops, tmp_path, runner)

    assert len(runner.calls) == 2, "at most one retry"
    assert {lin for _, lin, _ in runner.calls} == {"openai", "xai"}, "never the same seat twice"
    assert "verdicts" not in json.loads(path.read_text())
    assert report.no_verdict == 1 and report.seat_failures == 2
    event = [e for e in ledger_events(loops)
             if e["detail"].get("kind") == "verdict-conveyor-no-verdict"][0]
    assert event["id"] == ident
    attempts = event["detail"]["attempts"]
    assert [a["lineage"] for a in attempts] == ["openai", "xai"]
    assert all(a["empty_output"] for a in attempts)
    assert all("NO model output" in a["reason"] for a in attempts)
    assert all(a["transcript"] for a in attempts), "both transcripts archived"


def test_the_seat_chain_never_contains_the_producers_own_lineage(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """An openai-produced candidate is reviewed by xai, never by codex."""
    chain = V.seat_chain("openai", repo, lambda lin: tmp_path / f"{lin}.last")
    assert [s.lineage for s in chain] == ["xai"]

    file_candidate(loops, repo, "lane/risky", lineage="openai")
    runner = FakeSeatRunner(results={"xai": seat_result("REVIEWER-MODEL: grok-4\nFINAL-VERDICT: APPROVE")})
    report = run(repo, loops, tmp_path, runner)
    assert [lin for _, lin, _ in runner.calls] == ["xai"]
    assert report.written == 1


def test_a_timed_out_seat_is_a_failure_not_a_verdict(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    ident, path = file_candidate(loops, repo, "lane/risky")
    runner = FakeSeatRunner(default=seat_result("", rc=-15, timed_out=True))
    report = run(repo, loops, tmp_path, runner)
    assert "verdicts" not in json.loads(path.read_text())
    assert report.no_verdict == 1
    event = [e for e in ledger_events(loops)
             if e["detail"].get("kind") == "verdict-conveyor-no-verdict"][0]
    assert "timed out" in event["detail"]["attempts"][0]["reason"]


# --------------------------------------------------- lineage substitution trap


def test_a_seat_self_reporting_a_foreign_model_records_the_REAL_lineage(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """The measured trap: a seat of one lineage answering as another model.

    The self-report may only ever REDUCE what we claim. Here the codex seat
    answers as Sonnet against an anthropic producer, so the verdict is recorded
    honestly — and it does NOT satisfy the cross-lineage predicate, is not
    counted as an approval, and says so in the entry.

    An approval that satisfies nothing does not end the candidate's turn
    either: it is a seat failure like the silent echo, so the chain's single
    lineage-changing retry applies. Here BOTH seats answer as Sonnet, so both
    are recorded honestly, both are refused by the predicate, and the pass ends
    with no verdict rather than with a false one.
    """
    ident, path = file_candidate(loops, repo, "lane/risky", lineage="anthropic")
    runner = FakeSeatRunner(default=seat_result(
        "REVIEWER-MODEL: claude-sonnet-4.5\nFINAL-VERDICT: APPROVE"))

    report = run(repo, loops, tmp_path, runner)

    art = json.loads(path.read_text())
    for entry in art["verdicts"]:
        assert entry["lineage"] == "anthropic", "the dispatched lineage is not what answered"
        assert entry["model"] == "claude-sonnet-4.5"
        assert entry["satisfies_cross_lineage"] is False
        assert "self-reported" in entry["identity_note"]
        assert entry.get("identity_verified") is None, "identity WAS verified; it just collides"
    assert approved_cross_lineage(art, tip_of(repo, "lane/risky")) is False
    assert [lin for _, lin, _ in runner.calls] == ["openai", "xai"], "the retry still fires"
    assert report.written == 2 and report.approvals == 0
    assert report.same_lineage == 2 and report.quarantined == 0
    assert report.seat_failures == 2 and report.no_verdict == 1


def test_an_approval_without_a_declared_model_is_QUARANTINED(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """Absence of the attestation must fail closed, not fall back to "dispatched".

    THIS TEST REPLACES ONE THAT ASSERTED THE DEFECT. The original version
    asserted `entry["lineage"] == "openai"` and `report.approvals == 1` for an
    undeclared reviewer — i.e. it pinned the favourable-absence behaviour as if
    it were correct. Recording the DISPATCHED lineage when nothing was declared
    means the substitution trap is only caught when the substituted seat
    volunteers it, and a seat answering as another model has no reason to.
    "We could not verify who reviewed this" must not be reported in the same
    colour as "a different lineage reviewed this".

    The quarantine is enforced by the SHARED predicate (an `unverified:` prefix
    is simply not in `KNOWN_LINEAGES`), not by a second copy of the policy.
    """
    ident, path = file_candidate(loops, repo, "lane/risky", lineage="anthropic")
    runner = FakeSeatRunner(results={
        "openai": seat_result("FINAL-VERDICT: APPROVE"),        # no REVIEWER-MODEL line
        "xai": seat_result("REVIEWER-MODEL: grok-4\nFINAL-VERDICT: APPROVE"),
    })

    report = run(repo, loops, tmp_path, runner)

    quarantined = json.loads(path.read_text())["verdicts"][0]
    assert quarantined["lineage"] == "unverified:openai"
    assert quarantined["identity_verified"] is False
    assert quarantined["satisfies_cross_lineage"] is False
    assert quarantined["verdict"] == "APPROVE", "the seat's words are never mutated"
    assert "approval quarantined" in quarantined["identity_note"]
    assert report.quarantined == 1
    # ...and the quarantined approval alone could never have satisfied the gate.
    solo = {**json.loads(path.read_text()), "verdicts": [quarantined]}
    assert approved_cross_lineage(solo, tip_of(repo, "lane/risky")) is False


def test_an_unrecognised_model_token_also_quarantines_an_approval(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """A model id we cannot map to a lab establishes nothing about the lab."""
    _, path = file_candidate(loops, repo, "lane/risky")
    runner = FakeSeatRunner(default=seat_result("REVIEWER-MODEL: mystery-9\nFINAL-VERDICT: APPROVE"))

    report = run(repo, loops, tmp_path, runner)

    entry = json.loads(path.read_text())["verdicts"][0]
    assert entry["lineage"] == "unverified:openai"
    assert "mystery-9" in entry["identity_note"]
    assert approved_cross_lineage(json.loads(path.read_text()),
                                  tip_of(repo, "lane/risky")) is False
    assert report.approvals == 0 and report.quarantined == 2   # both seats quarantined


def test_a_quarantined_approval_triggers_the_one_lineage_changing_retry(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """An unverifiable approval is a SEAT failure, with the same remedy."""
    _, path = file_candidate(loops, repo, "lane/risky", lineage="anthropic")
    runner = FakeSeatRunner(results={
        "openai": seat_result("FINAL-VERDICT: APPROVE"),
        "xai": seat_result("REVIEWER-MODEL: grok-4\nFINAL-VERDICT: APPROVE"),
    })

    report = run(repo, loops, tmp_path, runner)

    assert [lin for _, lin, _ in runner.calls] == ["openai", "xai"]
    art = json.loads(path.read_text())
    assert [v["lineage"] for v in art["verdicts"]] == ["unverified:openai", "xai"]
    assert approved_cross_lineage(art, tip_of(repo, "lane/risky")) is True
    assert report.seat_failures == 1 and report.approvals == 1


def test_a_quarantined_entry_does_not_block_a_later_passs_re_dispatch(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """Half a fix would be worse than the defect: silently frozen forever.

    If a quarantined entry counted as "already conveyed", ONE seat that failed
    to declare itself would leave the candidate permanently unapprovable AND
    permanently un-offered — terminal, and invisible.
    """
    _, path = file_candidate(loops, repo, "lane/risky", lineage="anthropic")
    undeclared = FakeSeatRunner(default=seat_result("FINAL-VERDICT: APPROVE"))
    first = run(repo, loops, tmp_path, undeclared)
    assert first.quarantined == 2 and first.no_verdict == 1

    declaring = FakeSeatRunner(default=seat_result("REVIEWER-MODEL: gpt-5.6-sol\nFINAL-VERDICT: APPROVE"))
    second = run(repo, loops, tmp_path, declaring)

    assert len(declaring.calls) == 1, "the candidate was re-offered to a fresh seat"
    assert second.approvals == 1
    assert approved_cross_lineage(json.loads(path.read_text()),
                                  tip_of(repo, "lane/risky")) is True


def test_a_request_changes_without_a_declared_model_is_recorded_normally(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """A rejection grants nothing, so quarantining it would only lose information."""
    _, path = file_candidate(loops, repo, "lane/risky")
    runner = FakeSeatRunner(default=seat_result("FINAL-VERDICT: REQUEST-CHANGES: the guard is bypassable"))

    report = run(repo, loops, tmp_path, runner)

    entry = json.loads(path.read_text())["verdicts"][-1]
    assert entry["lineage"] == "openai", "not quarantined -- it approves nothing"
    assert entry.get("identity_verified") is None
    assert entry["identity_note"] == "reviewer-model-not-declared"
    assert report.rejections == 1 and report.quarantined == 0
    assert len(runner.calls) == 1, "a rejection settles the candidate; no retry"
    assert run(repo, loops, tmp_path, runner).dispatched == 0, "and it dedupes"


def test_reviewer_identity_quarantines_only_approvals() -> None:
    """The unit-level asymmetry, stated directly."""
    seat = V.Seat("codex", "openai", "gpt-5.6-sol", ())
    assert V.reviewer_identity(seat, "FINAL-VERDICT: APPROVE", is_approval=True)[1] == "unverified:openai"
    assert V.reviewer_identity(seat, "nope", is_approval=False)[1] == "openai"
    # A declared, mappable model still establishes identity normally.
    assert V.reviewer_identity(seat, "REVIEWER-MODEL: gpt-5.6-sol\nFINAL-VERDICT: APPROVE",
                               is_approval=True)[1] == "openai"
    # ...and a declared FOREIGN model still downgrades, as before.
    assert V.reviewer_identity(seat, "REVIEWER-MODEL: claude-opus-5\nFINAL-VERDICT: APPROVE",
                               is_approval=True)[1] == "anthropic"
    assert not V.is_quarantined("openai") and V.is_quarantined("unverified:openai")


# ----------------------------------------------------------- (e) singleton lock


def test_a_live_holder_blocks_a_second_pass(loops: Path) -> None:
    first = V.acquire_lock(loops)
    assert first is not None
    try:
        assert V.acquire_lock(loops) is None
    finally:
        V.release_lock(first)
    again = V.acquire_lock(loops)
    assert again is not None, "the lock is reusable once released"
    V.release_lock(again)


def test_a_stale_marker_is_taken_over(loops: Path) -> None:
    """A crashed pass must not wedge the conveyor until a human clears a file.

    The kernel releases the flock when the holder dies, so the NEXT pass simply
    wins it; the leftover marker is then overwritten. Note what is NOT being
    tested: the marker's pid is never consulted (that fallback was removed in
    round 2 -- a local pid test cannot adjudicate a cross-machine lock). The
    pid below could name a live process and the outcome would be identical,
    because flock is free.
    """
    marker = loops / "locks" / "verdict-conveyor.lock"
    dead = 4_000_000                       # above PID_MAX on macOS; cannot exist
    marker.write_text(json.dumps({
        "pid": dead, "actor": "verdict-conveyor",
        "deadline": (datetime.now(UTC) + timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")}))
    handle = V.acquire_lock(loops)
    assert handle is not None
    assert handle.took_over_from is not None
    assert handle.took_over_from["pid"] == dead
    assert json.loads(marker.read_text())["pid"] == os.getpid()
    V.release_lock(handle)


def test_an_uncontended_lock_is_acquired_whatever_the_marker_says(loops: Path) -> None:
    """The marker is a RECORD, not a mutex. flock alone decides (round-3 review).

    The previous docstring here claimed "the deadline is the ceiling on how
    long one pass may hold the conveyor", which the code does not implement and
    should not: a deadline that could evict a LIVE holder would be a licence to
    run two conveyors over one candidate. `test_a_held_lock_is_not_released_by_
    an_expired_deadline` pins the true behaviour. This test only shows that an
    UNCONTENDED lock is acquired regardless of what the leftover marker says.
    """
    marker = loops / "locks" / "verdict-conveyor.lock"
    marker.write_text(json.dumps({
        "pid": os.getppid(), "actor": "verdict-conveyor",
        "deadline": (datetime.now(UTC) - timedelta(minutes=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")}))
    handle = V.acquire_lock(loops)
    assert handle is not None
    V.release_lock(handle)


def test_a_held_lock_is_not_released_by_an_expired_deadline(loops: Path) -> None:
    """An expired deadline must NEVER evict a live holder.

    Pinning the property the corrected docstrings now claim: the deadline is
    descriptive. If it could evict, two conveyor passes would convey verdicts
    over the same candidates at once -- the exact failure the singleton exists
    to prevent -- and it would do so precisely when a pass was slow, which is
    when it is most likely to be mid-write.
    """
    handle = V.acquire_lock(loops, ttl_s=1)
    assert handle is not None
    try:
        marker = loops / "locks" / "verdict-conveyor.lock"
        expired = json.loads(marker.read_text())
        expired["deadline"] = (datetime.now(UTC) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        marker.write_text(json.dumps(expired))
        assert V.acquire_lock(loops) is None, "a live flock holder is never evicted"
    finally:
        V.release_lock(handle)


def test_an_unparseable_marker_is_treated_as_stale(loops: Path) -> None:
    """A corrupt marker must not be able to keep the conveyor down forever."""
    (loops / "locks" / "verdict-conveyor.lock").write_text("{not json")
    handle = V.acquire_lock(loops)
    assert handle is not None
    V.release_lock(handle)


def test_the_lock_marker_records_pid_and_deadline_for_an_operator(
        loops: Path) -> None:
    """What the marker is FOR: telling a human who holds it and since when.

    It is written and never read back for a decision. The ttl only shapes the
    `deadline` string; nothing enforces it, which is why this test asserts the
    arithmetic and not an eviction.
    """
    handle = V.acquire_lock(loops, ttl_s=1800)
    assert handle is not None
    try:
        marker = json.loads((loops / "locks" / "verdict-conveyor.lock").read_text())
        assert marker["pid"] == os.getpid()
        deadline = datetime.strptime(marker["deadline"], "%Y-%m-%dT%H:%M:%SZ")
        acquired = datetime.strptime(marker["acquired_at"], "%Y-%m-%dT%H:%M:%SZ")
        assert (deadline - acquired) == timedelta(seconds=1800)
    finally:
        V.release_lock(handle)


# ------------------------------------------------------------ misc invariants


def test_home_paths_are_expanded_not_passed_through_literally() -> None:
    """A "~" only expands in a SHELL; through env or a plist it stays literal.

    Measured 2026-08-13: CODEX_HOME=~/.codex-third was passed through verbatim
    and the seat ran against the wrong account home. This conveyor never sets
    CODEX_HOME (codexpick owns account routing), and every home-rooted constant
    it does hold is expanded.
    """
    assert "~" not in str(V.CODEXPICK)
    assert Path(V.CODEXPICK).is_absolute()


def test_the_bounded_pass_never_dispatches_more_than_max_candidates(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    for n in range(5):
        file_candidate(loops, repo, "lane/risky", payload_extra={"n": n})
    runner = FakeSeatRunner(default=seat_result(APPROVE))
    report = run(repo, loops, tmp_path, runner, max_candidates=2)
    assert report.starved == 5
    assert report.written == 2
    assert len(runner.calls) == 2


def test_a_dry_run_pays_for_no_seat(repo: Path, loops: Path, tmp_path: Path,
                                    seats: None) -> None:
    _, path = file_candidate(loops, repo, "lane/risky")
    runner = FakeSeatRunner(default=seat_result(APPROVE))
    report = run(repo, loops, tmp_path, runner, dry_run=True)
    assert runner.calls == [] and report.dispatched == 0
    assert "verdicts" not in json.loads(path.read_text())


def test_every_pass_appends_exactly_one_summary_event(
        repo: Path, loops: Path, tmp_path: Path, seats: None) -> None:
    """Silence and "the daemon is dead" must not look the same in the ledger."""
    report = run(repo, loops, tmp_path, FakeSeatRunner(default=seat_result(APPROVE)))
    V.note(loops, V.summary_event(report, 2.5, None))
    summaries = [e for e in ledger_events(loops)
                 if e["detail"].get("kind") == "verdict-conveyor-pass"]
    assert len(summaries) == 1
    detail = summaries[0]["detail"]
    for key in ("scanned", "starved", "dispatched", "written", "approvals",
                "rejections", "discarded", "no_verdict", "seat_failures", "skipped"):
        assert key in detail


def test_seat_commands_are_read_only(repo: Path, tmp_path: Path, seats: None) -> None:
    """A reviewer that can write is a reviewer that can 'fix' what it reviews."""
    for seat in V.seat_chain("anthropic", repo, lambda lin: tmp_path / f"{lin}.last"):
        argv = list(seat.argv)
        assert "read-only" in argv
        assert not any(tok in argv for tok in (
            "workspace-write", "danger-full-access", "--always-approve",
            "--dangerously-bypass-approvals-and-sandbox", "acceptEdits",
            "bypassPermissions"))


def test_run_seat_kills_the_whole_process_group_on_timeout(tmp_path: Path) -> None:
    """A bare kill() reaps the wrapper and leaves the model call running.

    The child here spawns a grandchild that outlives a SIGTERM to the child
    alone; the assertion is that the grandchild is gone once run_seat returns.
    """
    marker = tmp_path / "grandchild-alive"
    script = tmp_path / "seat.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"( while true; do echo x > {marker}; sleep 0.2; done ) &\n"
        "sleep 60\n"
    )
    script.chmod(0o755)
    seat = V.Seat("fake", "openai", "fake-1", (str(script),))

    result = V.run_seat(seat, "prompt", timeout_s=2)

    assert result.timed_out is True
    assert V.final_verdict_line(result.transcript) is None
    marker.unlink(missing_ok=True)
    # If the grandchild survived the group kill it re-creates the marker.
    import time
    time.sleep(1.0)
    assert not marker.exists(), "the seat's process GROUP must be killed, not just the child"
