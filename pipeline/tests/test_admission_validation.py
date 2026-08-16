"""Pins Lane C: envelope validation fails CLOSED.

Before this file, `bridge/integration.py:_schema_validate` returning
'unavailable' (jsonschema unimportable, or the schema file broken) fell
through to structural checks only, and the caller (`validate()`) recorded a
WARNING and kept going. On this host, where no system interpreter has
jsonschema, that meant EVERY candidate's schema constraints were silently
never checked: an off-enum `producer.role`, a malformed `id`, or `paths: []`
on a `candidate` (the top-level `paths` property had no `minItems`) all
sailed through admission on structural checks alone. Measured directly by
this test suite (`git stash` before/after on this exact repo, 2026-08-08): a
candidate with `producer.role: "hacker-not-a-real-role"` was ADMITTED before
this change and is REFUSED after it.

Lane C changes three things, checked here:
  * schema/envelope.schema.json: `properties.paths` now carries `minItems: 1`
    (`kind: candidate` already required `paths` be PRESENT, but not
    non-empty -- `paths: []` validated for a candidate before this).
  * bridge/integration.py `validate()`: 'unavailable' is now an
    instrument-error/blocked REFUSAL (parking the candidate for re-admission
    once the instrument is fixed), never a warning-and-continue.
  * bridge/integration.py `Integration.__init__`: a daemon-start self-test
    imports jsonschema under `sys.executable` and refuses to start
    (`DaemonSelfTestFailed`) if it cannot -- downtime over corruption,
    intentionally (operator ruling D7-yes, 2026-08-08T12:45Z).

The operator's binding condition on this work (same ruling) is a canary
corpus of known-good artifacts that must still be admitted after the flip:
`TestCanaryCorpusStillAdmitted` is that corpus, one artifact per `kind` this
gate actually validates.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
BRIDGE = PKG / "bridge"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PKG))

from bridge import integration as I  # noqa: E402
from bridge.canonical import content_id  # noqa: E402
from verdict_absence_matrix import envelope, run_candidate, scratch_repo  # noqa: E402

# Probe discipline (verdict_absence_matrix.py's own convention): refuse to
# measure the wrong tree.
assert Path(I.__file__).resolve() == (PKG / "bridge" / "integration.py").resolve()

try:
    import jsonschema  # noqa: F401
    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False

pytestmark = pytest.mark.skipif(
    not _HAS_JSONSCHEMA,
    reason="this suite measures the jsonschema-PRESENT admission path; run it "
           "under a conforming interpreter (see tests/test_interpreter_remedy.py "
           "for the jsonschema-ABSENT path)",
)


# --------------------------------------------------------------------------
# Lane C4: schema/envelope.schema.json minItems:1 on `paths`
# --------------------------------------------------------------------------

SCHEMA = json.loads((PKG / "schema" / "envelope.schema.json").read_text())


def _validate_schema(art: dict) -> str | None:
    """Returns None if valid, else the ValidationError message."""
    try:
        jsonschema.validate(art, SCHEMA)
    except jsonschema.ValidationError as exc:
        return exc.message
    return None


class TestEmptyPathsRefusedBySchema:
    def test_candidate_with_empty_paths_refused(self):
        art = {
            "id": "sha256:" + "a" * 64, "kind": "candidate", "title": "t",
            "created_at": "2026-08-08T00:00:00Z",
            "producer": {"role": "implementer"},
            "base_sha": "a" * 40, "head_sha": "b" * 40, "branch": "x", "paths": [],
            "evidence": [{"claim": "c", "verified_by": "execution",
                         "command": "x", "exit_code": 0}],
            "payload": {"fixes": "x"},
        }
        msg = _validate_schema(art)
        assert msg is not None, "candidate with paths:[] validated -- Lane C4 regressed"

    def test_candidate_with_nonempty_paths_still_admitted_by_schema(self):
        art = {
            "id": "sha256:" + "a" * 64, "kind": "candidate", "title": "t",
            "created_at": "2026-08-08T00:00:00Z",
            "producer": {"role": "implementer"},
            "base_sha": "a" * 40, "head_sha": "b" * 40, "branch": "x",
            "paths": ["bridge/x.py"],
            "evidence": [{"claim": "c", "verified_by": "execution",
                         "command": "x", "exit_code": 0}],
            "payload": {"fixes": "x"},
        }
        assert _validate_schema(art) is None

    def test_proposal_with_empty_paths_still_refused(self):
        """Regression: the proposal branch's own nested minItems override
        already refused this before Lane C -- must not have been weakened."""
        art = {
            "id": "sha256:" + "b" * 64, "kind": "proposal", "title": "t",
            "created_at": "2026-08-08T00:00:00Z",
            "producer": {"role": "planner"},
            "paths": [],
            "payload": {"problem": "p", "implementation_plan": "x"},
        }
        assert _validate_schema(art) is not None


# --------------------------------------------------------------------------
# Lane C3: bridge/integration.py fails closed on 'unavailable'
# --------------------------------------------------------------------------

class TestUnavailableFailsClosed:
    def test_unavailable_status_is_now_a_skip_not_a_warning(self, monkeypatch, tmp_path):
        """Directly exercises the 'unavailable' branch of validate() without
        needing a second interpreter: monkeypatch _schema_validate to return
        it, the way it would if jsonschema genuinely broke mid-run.

        F1 repair round: 'unavailable' is `cand.skip`, NOT `cand.refusal`.
        `iterate()` routes every `.refusal` through `reject()`, which writes
        a TERMINAL `rejected` ledger event -- and `validate()` checks
        `ledger.terminal` FIRST, before this schema check ever runs again.
        A `.refusal` here would freeze the unchanged candidate dead forever,
        even after the host is repaired: an instrument error rendered as a
        candidate defect, one level down from the exact thing this whole
        plan exists to close. `cand.skip` never reaches `reject()` and
        writes no ledger event at all, so there is nothing to expire and
        nothing to reorder -- the next validate() call re-checks schema
        availability from a clean slate."""
        monkeypatch.setattr(I, "_schema_validate",
                            lambda art, schema_dir, schema_file="envelope.schema.json":
                            ("unavailable", "simulated: jsonschema not importable"))
        art = {
            "id": "sha256:" + "c" * 64, "kind": "candidate", "title": "t",
            "created_at": "2026-08-08T00:00:00Z",
            "producer": {"role": "implementer"},
            "base_sha": "a" * 40, "head_sha": "b" * 40, "branch": "x", "paths": ["f.py"],
            "evidence": [{"claim": "c", "verified_by": "execution",
                         "command": "x", "exit_code": 0}],
            "payload": {"fixes": "x"},
        }
        root = tmp_path / "root"
        (root / "var" / "loopqueue").mkdir(parents=True)
        cand = run_candidate(Path("/dev/null").parent, root, PKG / "schema", art)
        assert cand.refusal is None, (
            "'unavailable' produced a REFUSAL -- iterate() would route this through "
            "reject() and write a TERMINAL ledger event, freezing the candidate dead "
            "even after the host is repaired (F1)")
        assert cand.skip, (
            "'unavailable' admitted the candidate silently -- Lane C's fail-closed "
            "change regressed")
        assert "instrument error" in cand.skip and "simulated" in cand.skip

    def test_instrument_error_is_non_terminal_and_repairable(self, tmp_path):
        """F1's own falsifier, pinned as an automated regression (adapted from
        .fusion/repro/F1_instrument_error_terminalizes_candidate.py, whose
        own scaffolding assumed the pre-repair `cand.refusal` design -- this
        version asserts the actual contract: no ledger event at all, and
        the UNCHANGED candidate is admissible again once the schema is
        fixed, automatically, with no human intervention and no
        resubmission."""
        repo, base, branch = scratch_repo(tmp_path)
        root = tmp_path / "root"
        for name in ("rejected", "state"):
            (root / "var" / "loopqueue" / name).mkdir(parents=True, exist_ok=True)
        root = root / "var" / "loopqueue"

        broken_schema_dir = tmp_path / "broken-schema"
        broken_schema_dir.mkdir()
        (broken_schema_dir / "envelope.schema.json").write_text("{not valid json")

        art = envelope(base, branch,
                       [{"reviewer": "gpt", "lineage": "openai", "verdict": "approve"}])

        # The daemon self-test only checks `import jsonschema` -- it does not
        # (and cannot cheaply) validate every schema FILE at startup, so
        # constructing Integration with a broken schema dir succeeds; this is
        # the exact "schema file broke mid-run" scenario Lane C targets.
        adapter = I.Integration(root, repo, repo, broken_schema_dir, apply=True)

        first = I.Candidate(art["id"], Path("candidate.json"), art)
        I.validate(first, repo, root, broken_schema_dir, I.LedgerView([], False), {}, set())
        assert first.refusal is None, "instrument failure must not be a refusal (F1)"
        assert first.skip, "instrument failure must be a skip, not silent admission"

        # iterate()'s own logic: only `.refusal` candidates ever reach reject().
        # Confirm that literally, then confirm no ledger event landed either way.
        if first.refusal is not None:
            adapter.reject(first, first.refusal)  # would be the F1 bug, if reached
        after_failure = I.LedgerView.build(root)
        assert art["id"] not in after_failure.terminal, (
            "instrument failure was frozen as a terminal candidate rejection (F1)")

        # Repair: validate the SAME unchanged candidate against the real schema.
        repaired = I.Candidate(art["id"], Path("candidate.json"), art)
        I.validate(repaired, repo, root, PKG / "schema", after_failure, {}, set())
        assert repaired.refusal is None and not repaired.skip, (
            "unchanged candidate was not re-admissible after the instrument was repaired (F1)")

    def test_off_enum_role_admitted_before_refused_after(self, tmp_path):
        """The exact red-first proof for this proposal's falsifier (b), pinned
        as an automated regression: a candidate with an off-enum
        producer.role, a real resolvable branch, and every OTHER structural
        check satisfied. With a working schema validator (this test's own
        interpreter, since the suite is skipped without one) it is refused
        by the schema's producer.role enum -- exercising the exact
        constraint that the pre-Lane-C 'unavailable' path let past when
        jsonschema was unimportable."""
        repo, base, branch = scratch_repo(tmp_path)
        art = envelope(base, branch,
                       [{"reviewer": "opus", "lineage": "openai", "verdict": "approve"}],
                       producer_extra={"role": "hacker-not-a-real-role"})
        root = tmp_path / "root"
        (root / "var" / "loopqueue").mkdir(parents=True)
        cand = run_candidate(repo, root, PKG / "schema", art)
        assert cand.refusal is not None, (
            "off-enum producer.role was admitted -- schema validation is not "
            "actually enforcing the enum")
        assert "producer" in cand.refusal.reason.lower() or "role" in cand.refusal.reason.lower() \
            or "schema" in cand.refusal.reason.lower(), cand.refusal.reason


# --------------------------------------------------------------------------
# F1 SIBLING (repair round 3): is_ancestor_of_main() -> None must not
# terminalize either, and the chokepoint that closes it must hold for ANY
# instrument-error/blocked refusal, not just this one call site.
# --------------------------------------------------------------------------

class TestAncestryProbeInstrumentErrorNeverTerminalizes:
    """Round-2's F1 repair made the schema-unavailable branch non-terminal
    (validate() uses cand.skip -- see TestUnavailableFailsClosed above) but
    left a SIBLING: is_ancestor_of_main() returning None (git unreadable /
    object missing) still built a `cand.refusal` with cls="instrument-error",
    and iterate() routes EVERY `.refusal` through reject() unconditionally,
    which writes a TERMINAL `rejected` ledger event -- freezing a candidate
    that a broken PROBE, not the candidate's own code, refused, and which can
    never be re-admitted even after the probe is fixed (CONTRACT §1:
    instrument errors are never used for a rejection).

    Repair round 3 closes this at the CHOKEPOINT instead of at this one call
    site: `Integration.reject()` -- the single writer of the `"rejected"`
    ledger event in the whole bridge/ package -- now refuses to terminalize
    any Refusal whose `cls` is `instrument-error` or `blocked-on-human`,
    regardless of where it was constructed. This test exercises the ACTUAL
    is_ancestor_of_main()-failure path end to end through `iterate()` (not a
    monkeypatched `_schema_validate`, so it also proves the chokepoint holds
    for a refusal shape other than the one round 2 patched), and confirms
    the unchanged candidate is re-admitted, automatically, once the probe is
    repaired.
    """

    def test_ancestry_probe_failure_writes_no_terminal_event(self, tmp_path, monkeypatch):
        repo, base, branch = scratch_repo(tmp_path)
        root = tmp_path / "root" / "var" / "loopqueue"
        for name in ("candidates", "rejected", "state"):
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / "state" / "budget.json").write_text(json.dumps({
            "updated_at": I._iso(I._now()), "wip_cap": 4,
            "disk_free_gb": 100, "disk_free_gb_min": 20,
            "load_avg_1m": 0, "load_avg_1m_max": 100,
        }))

        art = envelope(base, branch,
                       [{"reviewer": "gpt", "lineage": "openai", "verdict": "approve"}])
        (root / "candidates" / "candidate.json").write_text(json.dumps(art))

        # Simulate the real is_ancestor_of_main() -> None contract for an
        # unreadable repo/object -- not a monkeypatch of _schema_validate,
        # to prove the chokepoint (not just the one F1 call site) is closed.
        monkeypatch.setattr(I, "is_ancestor_of_main", lambda _repo, _sha: None)

        adapter = I.Integration(root, repo, repo, PKG / "schema", apply=True)
        adapter.iterate()

        after_failure = I.LedgerView.build(root)
        assert art["id"] not in after_failure.terminal, (
            "an instrument-error refusal (ancestry probe unreadable) was written as a "
            "TERMINAL rejected event -- CONTRACT §1 forbids using an instrument error "
            "for a rejection (F1 sibling)")
        rejected_file = root / "rejected" / f"{art['id'].split(':', 1)[-1]}.json"
        assert not rejected_file.exists(), (
            "an instrument-error refusal wrote a rejected/ file -- it must be fully "
            "non-terminal, not just absent from the ledger")

    def test_unchanged_candidate_admitted_after_probe_repair(self, tmp_path, monkeypatch):
        """After the failed iteration above, repairing the probe (removing the
        monkeypatch) must re-admit the exact same, unchanged candidate on the
        very next iteration -- no resubmission, no human, no new id."""
        repo, base, branch = scratch_repo(tmp_path)
        root = tmp_path / "root" / "var" / "loopqueue"
        for name in ("candidates", "rejected", "state"):
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / "state" / "budget.json").write_text(json.dumps({
            "updated_at": I._iso(I._now()), "wip_cap": 4,
            "disk_free_gb": 100, "disk_free_gb_min": 20,
            "load_avg_1m": 0, "load_avg_1m_max": 100,
        }))

        art = envelope(base, branch,
                       [{"reviewer": "gpt", "lineage": "openai", "verdict": "approve"}])
        (root / "candidates" / "candidate.json").write_text(json.dumps(art))

        real_probe = I.is_ancestor_of_main
        monkeypatch.setattr(I, "is_ancestor_of_main", lambda _repo, _sha: None)
        broken = I.Integration(root, repo, repo, PKG / "schema", apply=True)
        broken.iterate()
        monkeypatch.setattr(I, "is_ancestor_of_main", real_probe)  # instrument repaired

        repaired = I.Integration(root, repo, repo, PKG / "schema", apply=True)
        repaired.iterate()
        admitted_ids = {row["id"] for row in repaired.summary.get("would_admit", [])}
        assert art["id"] in admitted_ids, (
            "unchanged candidate stayed frozen after the ancestry probe was repaired "
            "(F1 sibling) -- an instrument error must never permanently block a "
            "candidate it says nothing about")

    def test_reject_chokepoint_refuses_any_instrument_class_directly(self, tmp_path):
        """Unit-level pin on the chokepoint itself, independent of any one
        call site: Integration.reject() must never write a terminal event
        for cls in {instrument-error, blocked-on-human}, whoever constructs
        the Refusal."""
        root = tmp_path / "root"
        root.mkdir()
        adapter = I.Integration(root, tmp_path, tmp_path, PKG / "schema", apply=True)
        cand = I.Candidate(ident="sha256:" + "d" * 64, path=Path("/dev/null"), art={})
        for cls in ("instrument-error", "blocked-on-human"):
            adapter.reject(cand, I.Refusal(f"synthetic {cls}", cls, "blocked"))
        ledger = root / "ledger.jsonl"
        written = ledger.read_text() if ledger.exists() else ""
        assert "rejected" not in written, (
            "reject() wrote a terminal 'rejected' event for an instrument-error/"
            "blocked-on-human refusal -- the chokepoint must hold for every caller, "
            "not just the ones known today")
        assert not (root / "rejected").exists() or not list((root / "rejected").glob("*.json")), (
            "reject() wrote a rejected/ file for a non-candidate-defect refusal")


# --------------------------------------------------------------------------
# F2 (cross-lineage repair round): _self_validate() must fail closed too
# --------------------------------------------------------------------------

class TestSelfValidateFailsClosed:
    """Ruled out at 8b0376c as "not the external ingest surface Lane C
    targets" -- that ruling did not hold. `_self_validate` guards the
    adapter's OWN writes to the ledger, which is APPEND-ONLY and cannot be
    repaired after the fact; "the adapter writes it, so it is trustworthy"
    is the exact fail-open assumption Lane C exists to delete. It deserves
    the STRICTEST check, not the most forgiving one. Corrected here: on
    'unavailable', _self_validate now always raises RuntimeError instead of
    falling back to a required-keys + role-enum structural check that
    cannot see the schema's `event` enum at all."""

    def test_schema_invalid_ledger_event_refused_when_schema_unavailable(self, monkeypatch, tmp_path):
        """Adapted from .fusion/repro/F2_self_validate_unavailable_writes_invalid_ledger.py:
        required keys and role are valid, but `event` is not a real enum
        value. Before this fix, _self_validate's structural fallback checked
        only required keys + role and returned success-shaped -- this
        invalid event landed permanently in ledger.jsonl."""
        monkeypatch.setattr(I, "_schema_validate",
                            lambda obj, schema_dir, schema_file="envelope.schema.json":
                            ("unavailable", "simulated: schema file unreadable"))
        root = tmp_path / "root"
        root.mkdir()
        adapter = I.Integration(root, tmp_path, tmp_path, PKG / "schema", apply=True)
        invalid_event = {
            "ts": "2026-08-08T00:00:00Z", "role": "implementer",
            "event": "not-a-real-event", "id": "sha256:" + "a" * 64,
        }
        with pytest.raises(RuntimeError):
            adapter._append_ledger(invalid_event)
        ledger = root / "ledger.jsonl"
        written = ledger.read_text() if ledger.exists() else ""
        assert written == "", (
            "schema-invalid event was permanently appended to the ledger despite "
            "'unavailable' schema validation (F2)")

    def test_valid_ledger_event_still_written_when_schema_available(self, tmp_path):
        """Canary: a genuinely valid self-write must still succeed -- this
        fix must not make the adapter unable to write its own ledger."""
        root = tmp_path / "root"
        root.mkdir()
        adapter = I.Integration(root, tmp_path, tmp_path, PKG / "schema", apply=True)
        valid_event = {
            "ts": "2026-08-08T00:00:00Z", "role": "implementer",
            "event": "admitted", "id": "sha256:" + "b" * 64,
        }
        adapter._append_ledger(valid_event)  # must not raise
        ledger = root / "ledger.jsonl"
        assert ledger.exists() and ledger.read_text().strip() != ""


# --------------------------------------------------------------------------
# F3 (cross-lineage repair round): interpreter-probe timeouts must not escape
# --------------------------------------------------------------------------

class TestInterpreterProbeTimeout:
    """Adapted from .fusion/repro/F3_interpreter_probe_timeout_escapes.py:
    _discover_conforming_interpreter's subprocess.run(..., timeout=10) call
    caught only OSError; subprocess.TimeoutExpired is NOT an OSError
    subclass, so a genuinely hanging candidate interpreter escaped uncaught
    and turned the documented could-not-run contract (exit 2 in
    validate_envelope.py and file_proposal.py after Ruling #4, 'unavailable' in
    integration._schema_validate) into
    a raw traceback -- identically in the three places this probe used to
    be duplicated. Fixed by catching TimeoutExpired too, in the now-single
    shared implementation in bridge/validate_envelope.py."""

    def test_timeout_does_not_raise(self, monkeypatch):
        import subprocess as sp

        from bridge.validate_envelope import _discover_conforming_interpreter

        def hang(*args, **kwargs):
            raise sp.TimeoutExpired(args[0] if args else "probe", kwargs.get("timeout", 10))

        monkeypatch.setenv("VIRTUAL_ENV", str(PKG))  # any path that "exists" as PKG does
        monkeypatch.setattr(sp, "run", hang)
        found, probed = _discover_conforming_interpreter()  # must not raise
        assert found is None
        assert probed  # the candidate was probed, just timed out

    def test_shared_implementation_used_by_all_three_tools(self):
        """Pins the consolidation: file_proposal.py and integration.py must
        import the discovery helper from validate_envelope.py rather than
        keep their own copies -- one bug in the probe is one bug, not
        three."""
        import bridge.integration as itg
        import bridge.validate_envelope as ve

        # integration.py imports the hint function at module scope.
        assert itg._no_conforming_interpreter_hint is ve._no_conforming_interpreter_hint

        # file_proposal.py imports it lazily inside check_schema() (matching
        # its existing `from validate_envelope import _build_validator`
        # pattern) -- assert by SOURCE, not object identity, that no
        # duplicate implementation exists in either caller.
        for relpath in ("bridge/file_proposal.py", "bridge/integration.py"):
            text = (PKG / relpath).read_text()
            assert "def _discover_conforming_interpreter" not in text, (
                f"{relpath} redefines the discovery probe instead of importing "
                "the single shared implementation in bridge/validate_envelope.py")


# --------------------------------------------------------------------------
# Lane C1 / operator D7 condition: canary corpus of known-good artifacts
# --------------------------------------------------------------------------

class TestCanaryCorpusStillAdmitted:
    """Every artifact here is a genuinely well-formed one for its kind. None
    may be refused after the Lane C fail-closed flip -- a false refusal here
    is exactly the over-refusal risk the operator's D7 condition guards
    against."""

    def test_canary_candidate_admitted(self, tmp_path):
        repo, base, branch = scratch_repo(tmp_path)
        art = envelope(base, branch,
                       [{"reviewer": "opus", "lineage": "openai", "verdict": "approve"}])
        root = tmp_path / "root"
        (root / "var" / "loopqueue").mkdir(parents=True)
        cand = run_candidate(repo, root, PKG / "schema", art)
        assert cand.refusal is None, f"canary candidate refused: {cand.refusal.reason}"

    def test_canary_proposal_valid_against_schema(self):
        art = {
            "id": None, "kind": "proposal", "title": "a well-formed proposal",
            "created_at": "2026-08-08T00:00:00Z",
            "producer": {"role": "planner", "actor": "planner@probe", "lineage": "anthropic"},
            "paths": ["bridge/governor.py"],
            "payload": {"problem": "p", "implementation_plan": "do the thing"},
        }
        art["id"] = content_id(art["payload"])
        assert _validate_schema(art) is None, _validate_schema(art)

    def test_canary_finding_valid_against_schema(self):
        art = {
            "id": None, "kind": "finding", "title": "a well-formed finding",
            "created_at": "2026-08-08T00:00:00Z",
            "producer": {"role": "reviewer", "actor": "reviewer@probe", "lineage": "openai"},
            "payload": {"symptom": "the thing broke"},
        }
        art["id"] = content_id(art["payload"])
        assert _validate_schema(art) is None, _validate_schema(art)

    def test_canary_inquiry_valid_against_schema(self):
        art = {
            "id": None, "kind": "inquiry", "title": "a well-formed inquiry",
            "created_at": "2026-08-08T00:00:00Z",
            "producer": {"role": "planner", "actor": "planner@probe", "lineage": "anthropic"},
            "payload": {"area": "tooling", "observation": "o", "why_not_a_fix": "unknown yet"},
        }
        art["id"] = content_id(art["payload"])
        assert _validate_schema(art) is None, _validate_schema(art)


# --------------------------------------------------------------------------
# Lane C2: daemon-start self-test
# --------------------------------------------------------------------------

# Bare-interpreter discovery lives in the shared conftest fixture now — the
# local clone of it checked only "lacks jsonschema", not ">= 3.11", and the
# twin's system python 3.9 turned this probe into a false daemon-start test.


class TestDaemonSelfTest:
    def test_daemon_refuses_start_without_jsonschema(self, tmp_path, bare_interpreter):
        bare = bare_interpreter
        code = (
            "import sys; sys.path.insert(0, '.')\n"
            "from pathlib import Path\n"
            "from bridge.integration import Integration, DaemonSelfTestFailed\n"
            "try:\n"
            "    Integration(Path('/tmp'), Path('/tmp'), Path('/tmp'), Path('schema'), False)\n"
            "except DaemonSelfTestFailed as e:\n"
            "    print('SELF_TEST_FAILED:', e)\n"
            "    raise SystemExit(1)\n"
            "print('STARTED (BAD)')\n"
        )
        result = subprocess.run([bare, "-c", code], capture_output=True, text=True, cwd=str(PKG))
        assert result.returncode != 0, (
            f"daemon started without jsonschema (should have refused): {result.stdout}")
        assert "SELF_TEST_FAILED" in result.stdout

    def test_daemon_starts_with_jsonschema(self, tmp_path):
        # Under THIS interpreter (skipped-if-no-jsonschema at module level),
        # construction must succeed.
        it = I.Integration(tmp_path, tmp_path, tmp_path, PKG / "schema", False)
        assert it is not None
