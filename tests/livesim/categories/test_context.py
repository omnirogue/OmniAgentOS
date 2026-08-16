"""LiveSim: context assembly & continuity — capsule digest, manifest, handoff.

Subsystems under observation:

  * omniagentos/context/capsule.py — the Context Capsule v1 shadow manifest.
    `brief_digest` MUST be a deterministic function of the delivered prompt
    (a counterfeit once zeroed it); the budget model (byte_cap/per_slice_cap)
    is the COMPACTION story; secret-kind sources must be rejected before any
    digest/storage.
  * omniagentos/sessions/manifest.py — SessionManifest, the exactly-once
    terminal handoff record. Continuity means: written once, immutable after,
    honest nullable telemetry (cost_usd NULL stays null, never coerced to 0).
  * CONTAMINATION — one session's context must never leak into another
    session's capsule manifest or terminal manifest.

All tests here are pure-code + scratch-FS: nothing touches the live DB or the
live API, every file lands under the per-test scratch dir, and every synthetic
identifier is tagged with the livesim_ns namespace.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from unittest import mock

import pytest

pytestmark = pytest.mark.livesim

from omniagentos.context.capsule import (  # noqa: E402
    CONTEXT_CAPSULE_ENV,
    REASON_EXCLUDED_BUDGET,
    REASON_INCLUDED,
    REASON_TRUNCATED_BUDGET,
    CapsuleContractTruncated,
    CapsuleSecretSource,
    CapsuleSource,
    build_capsule_manifest,
    context_capsule_mode,
    observed_sources_from_prompt,
    write_capsule_manifest,
)
from omniagentos.sessions.manifest import SessionManifest  # noqa: E402

_FENCE = "OMNIAGENTOS_DATA_NOT_INSTRUCTIONS"


def _fenced(label: str, delim: str, body: str) -> str:
    return (
        f"<<<{_FENCE} label={label} delimiter={delim}>>>\n"
        f"{body}\n"
        f"<<<END_{_FENCE} delimiter={delim}>>>"
    )


def _build(prompt: str, sources, **over):
    kw = dict(
        prompt=prompt,
        task_id="task_livesim",
        run_id="run_livesim",
        project_id="proj_livesim",
        contract_version="v1",
        repo_sha="deadbeef",
        preset_digest="preset0",
        sources=sources,
        byte_cap=10_000,
        per_slice_cap=5_000,
        compression_mode="none",
    )
    kw.update(over)
    return build_capsule_manifest(**kw)


def _session_row(session_id: str, project_dir: str, state: str = "completed") -> dict:
    return {
        "id": session_id,
        "source": "livesim",
        "project_dir": project_dir,
        "provider": "claude",
        "session_ref": None,
        "state": state,
        "model": "synthetic",
        "created_at": "2026-08-06T00:00:00Z",
        "updated_at": "2026-08-06T00:05:00Z",
        "cost_usd": None,  # nullable ON PURPOSE — must survive as null
    }


# ---------------------------------------------------------------------------
# Capsule digest — determinism (the counterfeit once zeroed it)
# ---------------------------------------------------------------------------


@pytest.mark.positive
def test_brief_digest_is_deterministic_and_never_zeroed(livesim):
    """Same prompt + same sources => byte-identical manifest JSON, and
    brief_digest is the real sha256 of the delivered prompt (a counterfeit
    once zeroed it — this pins the honest value). UTF-8 bytes are measured,
    not characters."""
    livesim.target("fs")
    prompt = "wörker brief → résumé of task\n" + _fenced("PROJECT_CONTRACT", "a" * 24, "do the thing")
    sources = observed_sources_from_prompt(prompt)
    m1 = _build(prompt, sources)
    m2 = _build(prompt, list(sources))  # different container, same content
    expected = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    livesim.record(
        inputs={"prompt_chars": len(prompt), "prompt_bytes": len(prompt.encode("utf-8"))},
        outputs={"brief_digest": m1.brief_digest, "brief_bytes": m1.brief_bytes},
    )
    assert m1.brief_digest == expected
    assert m1.brief_digest != "0" * 64 and m1.brief_digest != ""
    assert m1.brief_bytes == len(prompt.encode("utf-8")) > len(prompt)  # unicode: bytes > chars
    assert m1.to_json() == m2.to_json()  # full determinism, not just the digest
    livesim.cleanup(True)


@pytest.mark.positive
def test_observed_sources_extraction_is_stable(livesim):
    """Fenced blocks + unfenced gaps extract deterministically: PROJECT_CONTRACT
    maps to kind=contract, other labels to fenced, gaps become base_brief_N,
    ranks follow byte offset, and an unterminated fence degrades into the
    unfenced remainder instead of vanishing."""
    livesim.target("fs")
    d1, d2 = "a" * 24, "b" * 24
    prompt = (
        "intro text\n"
        + _fenced("PROJECT_CONTRACT", d1, "contract body here")
        + "\nmiddle text\n"
        + _fenced("MEMORY_SLICE", d2, "memory body here")
        + "\ntail text\n"
        + f"<<<{_FENCE} label=BROKEN delimiter={'c' * 24}>>>\nno closing fence"
    )
    s1 = observed_sources_from_prompt(prompt)
    s2 = observed_sources_from_prompt(prompt)
    assert s1 == s2  # deterministic
    by_name = {s.name: s for s in s1}
    livesim.record(
        inputs={"fences": 3, "closed": 2},
        outputs={s.name: {"kind": s.kind, "rank": s.rank, "bytes": len(s.content)} for s in s1},
    )
    assert by_name["PROJECT_CONTRACT"].kind == "contract"
    assert by_name["PROJECT_CONTRACT"].content == "contract body here"
    assert by_name["MEMORY_SLICE"].kind == "fenced"
    assert by_name["MEMORY_SLICE"].content == "memory body here"
    # BROKEN never became a fenced source; its text survives in a base_brief gap.
    assert "BROKEN" not in by_name
    gap_text = "".join(s.content for s in s1 if s.kind == "base_brief")
    assert "no closing fence" in gap_text
    # Ranks are 0..N-1 in byte-offset order and every gap is a true substring.
    assert [s.rank for s in s1] == list(range(len(s1)))
    for s in s1:
        if s.kind == "base_brief":
            assert s.content in prompt
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# COMPACTION — the byte_cap / per_slice_cap budget model
# ---------------------------------------------------------------------------


@pytest.mark.boundary
def test_compaction_budget_contract_first_then_rank_order(livesim):
    """Budget semantics: the contract is admitted first and is NEVER truncated
    (an oversize contract raises instead); optional slices are admitted lowest
    rank first until byte_cap, later ones get excluded_budget; an optional
    slice above per_slice_cap is flagged truncated_budget; included bytes
    never exceed byte_cap."""
    livesim.target("fs")
    contract = CapsuleSource(name="CONTRACT", kind="contract", content="C" * 100, rank=9)
    keep = CapsuleSource(name="keep", kind="fenced", content="K" * 200, rank=0)
    oversize = CapsuleSource(name="oversize", kind="fenced", content="O" * 400, rank=1)
    dropped = CapsuleSource(name="dropped", kind="fenced", content="D" * 300, rank=2)
    prompt = contract.content + keep.content + oversize.content
    m = _build(
        prompt,
        [dropped, oversize, keep, contract],  # deliberately shuffled input order
        byte_cap=750,          # 100 + 200 + 400 = 700 fit; +300 would not
        per_slice_cap=350,     # oversize (400) exceeds the per-slice cap
    )
    by_name = {s.name: s for s in m.slices}
    livesim.record(
        inputs={"byte_cap": 750, "per_slice_cap": 350,
                "sizes": {"CONTRACT": 100, "keep": 200, "oversize": 400, "dropped": 300}},
        outputs={n: {"included": s.included, "reason": s.reason_code} for n, s in by_name.items()},
    )
    assert by_name["CONTRACT"].included and by_name["CONTRACT"].reason_code == REASON_INCLUDED
    assert by_name["keep"].included and by_name["keep"].reason_code == REASON_INCLUDED
    assert by_name["oversize"].included
    assert by_name["oversize"].reason_code == REASON_TRUNCATED_BUDGET
    assert not by_name["dropped"].included
    assert by_name["dropped"].reason_code == REASON_EXCLUDED_BUDGET
    included_bytes = sum(s.bytes for s in m.slices if s.included)
    assert included_bytes <= 750
    # Emission order is rank-then-name, independent of the shuffled input order.
    assert [s.name for s in m.slices] == ["keep", "oversize", "dropped", "CONTRACT"]
    # "Complete TaskContract is never truncated": an oversize contract raises.
    big_contract = CapsuleSource(name="BIG", kind="contract", content="B" * 400, rank=0)
    with pytest.raises(CapsuleContractTruncated):
        _build(prompt, [big_contract], byte_cap=750, per_slice_cap=350)
    with pytest.raises(CapsuleContractTruncated):
        # fits per-slice but not the remaining byte budget
        _build(prompt, [contract, CapsuleSource(name="C2", kind="contract", content="X" * 300, rank=1)],
               byte_cap=350, per_slice_cap=350)
    livesim.note(
        "OBSERVATION: an optional slice above per_slice_cap is labelled "
        "truncated_budget but its FULL byte count is admitted against byte_cap "
        "and its full content stays in the brief — V1 shadow records intent, "
        "nothing is physically truncated."
    )
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# Secrets are not sources
# ---------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.negative
def test_secret_kind_source_is_rejected_before_digest(livesim, scratch_dir, livesim_ns):
    """A secret-kind source must raise CapsuleSecretSource before any digest or
    storage — the secret must not appear in any artifact the capsule produced."""
    livesim.target("fs")
    secret_value = f"{livesim_ns}_sk-THIS_MUST_NEVER_PERSIST"
    prompt = "brief without the secret"
    with pytest.raises(CapsuleSecretSource):
        _build(
            prompt,
            [
                CapsuleSource(name="ok", kind="fenced", content="benign", rank=0),
                CapsuleSource(name="apikey", kind="secret", content=secret_value, rank=1),
            ],
        )
    # Nothing was written anywhere under our scratch, and the secret string
    # exists in no file the test created.
    leaked = [p for p in scratch_dir.rglob("*") if p.is_file() and secret_value in p.read_text(errors="replace")]
    livesim.record(inputs={"secret_tag": livesim_ns}, outputs={"raised": True, "leaked_files": len(leaked)})
    assert leaked == []
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# Mode gate + shadow write (context loading rollout switch)
# ---------------------------------------------------------------------------


@pytest.mark.boundary
def test_capsule_mode_gate_off_shadow_enforce(livesim, scratch_dir):
    """Rollout gate: invalid/absent env values resolve to 'off'; off writes
    nothing; shadow writes a deterministic context-capsule.json; V1 'enforce'
    writes byte-identically to shadow (enforce IS shadow in V1)."""
    livesim.target("fs")
    assert context_capsule_mode(None) in ("off", "shadow", "enforce")  # env-resolved
    for bad in ("banana", "ON", "1", "  ", "enforce-hard"):
        assert context_capsule_mode(bad) == "off"
    assert context_capsule_mode(" Shadow ") == "shadow"  # trimmed + lowercased

    prompt = "handoff brief body"
    m = _build(prompt, observed_sources_from_prompt(prompt))
    off_dir = scratch_dir / "off"
    shadow_dir = scratch_dir / "shadow"
    enforce_dir = scratch_dir / "enforce"
    with mock.patch.dict(os.environ, {CONTEXT_CAPSULE_ENV: ""}):
        assert write_capsule_manifest(m, evidence_dir=off_dir) is None
    with mock.patch.dict(os.environ, {CONTEXT_CAPSULE_ENV: "shadow"}):
        p_shadow = write_capsule_manifest(m, evidence_dir=shadow_dir)
    with mock.patch.dict(os.environ, {CONTEXT_CAPSULE_ENV: "enforce"}):
        p_enforce = write_capsule_manifest(m, evidence_dir=enforce_dir)
    livesim.record(
        inputs={"modes": ["off", "shadow", "enforce"]},
        outputs={
            "off_wrote": off_dir.exists() and any(off_dir.iterdir()),
            "shadow_path": str(p_shadow),
            "enforce_same_bytes": p_shadow.read_bytes() == p_enforce.read_bytes(),
        },
    )
    assert not (off_dir / "context-capsule.json").exists()
    assert p_shadow is not None and p_shadow.name == "context-capsule.json"
    assert p_enforce is not None
    assert p_shadow.read_bytes() == p_enforce.read_bytes()  # V1: enforce == shadow
    parsed = json.loads(p_shadow.read_text())
    assert parsed["brief_digest"] == m.brief_digest
    # The written manifest carries digests only — never raw slice content.
    assert "content_for_test" not in p_shadow.read_text()
    livesim.cleanup(True)


@pytest.mark.recovery
def test_capsule_write_failure_is_swallowed_never_blocks_spawn(livesim, scratch_dir):
    """Contract: 'IO failures are logged and swallowed so capsule failure
    cannot block a spawn'. An impossible evidence path (parent is a FILE)
    must return None, not raise."""
    livesim.target("fs")
    blocker = scratch_dir / "not_a_dir"
    blocker.write_text("i am a file, not a directory")
    m = _build("tiny brief", observed_sources_from_prompt("tiny brief"))
    with mock.patch.dict(os.environ, {CONTEXT_CAPSULE_ENV: "shadow"}):
        result = write_capsule_manifest(m, evidence_dir=blocker / "sub")
    livesim.record(inputs={"evidence_dir": str(blocker / "sub")}, outputs={"result": result})
    assert result is None  # swallowed, no exception escaped
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# Handoff continuity — SessionManifest exactly-once terminal record
# ---------------------------------------------------------------------------


@pytest.mark.positive
def test_session_manifest_handoff_roundtrip_exactly_once(livesim, scratch_dir, livesim_ns):
    """Terminal handoff: the manifest round-trips the session truthfully
    (cost_usd NULL stays null — never coerced to a favourable 0.0; approvals
    are counted, not copied) and a second write CANNOT rewrite history."""
    livesim.target("fs")
    sm = SessionManifest(ledger_dir=scratch_dir / "ledger")
    sid = f"{livesim_ns}_A"
    approvals = [
        {"state": "approved"},
        {"state": "rejected"},
        {"state": "expired"},
        {"state": "pending"},
    ]
    path = sm.write(_session_row(sid, str(scratch_dir)), approvals, killed_by=None)
    first_bytes = path.read_bytes()
    rec = json.loads(first_bytes)
    livesim.record(
        inputs={"session_id": sid, "approvals": [a["state"] for a in approvals]},
        outputs={k: rec[k] for k in ("final_state", "cost_usd", "approvals_requested",
                                     "approvals_granted", "approvals_denied", "killed_by")},
    )
    assert rec["session_id"] == sid
    assert rec["final_state"] == "completed"
    assert rec["cost_usd"] is None  # favourable-absence guard: NULL != $0
    assert rec["approvals_requested"] == 4
    assert rec["approvals_granted"] == 1
    assert rec["approvals_denied"] == 2  # rejected + expired; pending is neither
    assert rec["killed_by"] is None
    assert first_bytes.endswith(b"\n") and first_bytes.count(b"\n") == 1  # one JSONL line
    # Exactly-once: a conflicting rewrite is silently a no-op on the record.
    path2 = sm.write(_session_row(sid, str(scratch_dir), state="failed"), [], killed_by="max-park")
    assert path2 == path
    assert path.read_bytes() == first_bytes  # history is immutable
    # No stranded temp files.
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")] == []
    livesim.cleanup(True)


@pytest.mark.concurrency
def test_session_manifest_concurrent_writers_single_record(livesim, scratch_dir, livesim_ns):
    """Eight threads racing to terminalize the same session must produce
    exactly ONE manifest record (O_EXCL temp + hard-link), no exceptions and
    no stranded temp files — the crash-safe half of handoff continuity."""
    livesim.target("fs")
    sm = SessionManifest(ledger_dir=scratch_dir / "ledger")
    sid = f"{livesim_ns}_race"
    n_threads = 8
    barrier = threading.Barrier(n_threads)
    errors: list[str] = []
    paths: list[str] = []
    lock = threading.Lock()

    def writer(i: int) -> None:
        try:
            barrier.wait(timeout=30)
            p = sm.write(_session_row(sid, str(scratch_dir)), [{"state": "approved"}] * i)
            with lock:
                paths.append(str(p))
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    session_dir = scratch_dir / "ledger" / "sessions"
    files = sorted(p.name for p in session_dir.iterdir())
    livesim.record(
        inputs={"threads": n_threads, "session_id": sid},
        outputs={"errors": errors, "distinct_paths": sorted(set(paths)), "files": files},
    )
    assert errors == []
    assert len(paths) == n_threads and len(set(paths)) == 1
    assert files == [f"{sid}.jsonl"]  # exactly one record, zero .tmp strays
    rec = json.loads((session_dir / f"{sid}.jsonl").read_text())
    assert rec["session_id"] == sid  # whichever writer won, the record is coherent
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# CONTAMINATION — session A's context never leaks into session B's artifacts
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_no_cross_session_context_contamination(livesim, scratch_dir, livesim_ns):
    """Two sessions with distinct namespaced content: neither the capsule
    manifests nor the terminal manifests of one may contain the other's
    content, digests, or ids — and writing B must not disturb A's bytes."""
    livesim.target("fs")
    tok_a = f"{livesim_ns}_TOKEN_ALPHA_ONLY"
    tok_b = f"{livesim_ns}_TOKEN_BRAVO_ONLY"
    prompt_a = f"session alpha brief\n{_fenced('MEMORY_SLICE', 'a' * 24, tok_a)}"
    prompt_b = f"session bravo brief\n{_fenced('MEMORY_SLICE', 'b' * 24, tok_b)}"
    m_a = _build(prompt_a, observed_sources_from_prompt(prompt_a), run_id="run_A", task_id="task_A")
    m_b = _build(prompt_b, observed_sources_from_prompt(prompt_b), run_id="run_B", task_id="task_B")
    json_a, json_b = m_a.to_json(), m_b.to_json()

    # Capsule isolation: distinct briefs, distinct digests, zero shared slice digests.
    assert m_a.brief_digest != m_b.brief_digest
    digests_a = {s.digest for s in m_a.slices}
    digests_b = {s.digest for s in m_b.slices}
    assert digests_a.isdisjoint(digests_b)
    # No raw content in either manifest, and no cross-tokens anywhere.
    for blob in (json_a, json_b):
        assert tok_a not in blob and tok_b not in blob  # digests only, never content
    assert "run_B" not in json_a and "run_A" not in json_b

    # Terminal-manifest isolation: A's record is byte-stable across B's write.
    sm = SessionManifest(ledger_dir=scratch_dir / "ledger")
    sid_a, sid_b = f"{livesim_ns}_ctA", f"{livesim_ns}_ctB"
    path_a = sm.write(_session_row(sid_a, f"/proj/{tok_a}"), [])
    bytes_a_before = path_a.read_bytes()
    path_b = sm.write(_session_row(sid_b, f"/proj/{tok_b}"), [])
    assert path_a != path_b
    assert path_a.read_bytes() == bytes_a_before  # B's handoff never touched A
    assert tok_b not in path_a.read_text() and tok_a not in path_b.read_text()
    livesim.record(
        inputs={"tokens": [tok_a, tok_b]},
        outputs={
            "brief_digest_a": m_a.brief_digest,
            "brief_digest_b": m_b.brief_digest,
            "shared_slice_digests": 0,
            "manifest_files": [path_a.name, path_b.name],
        },
    )
    livesim.cleanup(True)

