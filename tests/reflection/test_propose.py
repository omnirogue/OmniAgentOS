"""Unit tests for Stage B (propose)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.improvement_chain import KimiJsonResult, StageAttempt
from omniagentos.reflection.propose import (
    _EVIDENCE_BUDGET_BYTES,
    EvidenceStarved,
    ImprovementProposal,
    _cap_evidence,
    _trim_container,
    call_llm,
    extract_json_array,
    is_hard_stop,
    run_propose,
)


def test_is_hard_stop():
    """Verify is_hard_stop behaves as a strict guard against dangerous edits."""
    # Forbidden configs/credentials
    assert is_hard_stop("formation", "configs/governance.yaml") is True
    assert is_hard_stop("formation", "omniagentos/policy/rules.yaml") is True
    assert is_hard_stop("model_config", "configs/credentials.yaml") is True
    assert is_hard_stop("effort_override", "configs/budget_caps.yaml") is True
    assert is_hard_stop("risk_pin", "configs/swarm.yaml") is True

    # Allowed configs
    assert is_hard_stop("formation", "configs/formations.yaml") is False
    assert is_hard_stop("router_weight", "configs/swarm.yaml") is False


def test_improvement_proposal_schema():
    """Assert ImprovementProposal matches the required schema fields."""
    p_data = {
        "id": "rfl_prop_test_01",
        "kind": "model_config",
        "target": {"file": "configs/swarm.yaml", "key": "lane_floors.complex"},
        "current": ["gpt-4o"],
        "proposed": ["gemini-3.6-flash"],
        "rationale": "Better TTFT and cost-efficiency.",
        "evidence_refs": ["session_abc123"],
        "predicted_impact": "Success rate +5%",
        "risk_class": "low",
    }
    proposal = ImprovementProposal.model_validate(p_data)
    assert proposal.id == "rfl_prop_test_01"
    assert proposal.kind == "model_config"
    assert proposal.target["file"] == "configs/swarm.yaml"


def test_extract_json_array():
    """Assert json array extraction handles different formatting styles from LLM."""
    md_json = """
Here are your proposals:
```json
[
  {
    "id": "rfl_prop_01",
    "kind": "model_config",
    "target": "configs/modelintel.yaml",
    "current": "x",
    "proposed": "y",
    "rationale": "test",
    "evidence_refs": [],
    "predicted_impact": "high",
    "risk_class": "low"
  }
]
```
Have a nice day!
"""
    items = extract_json_array(md_json)
    assert len(items) == 1
    assert items[0]["id"] == "rfl_prop_01"


def test_cap_evidence_under_budget():
    """Test evidence capping when evidence is already under budget."""
    evidence = {
        "runs": [{"id": f"run_{i}"} for i in range(5)],
        "metadata": {"timestamp": "2026-07-31"},
    }
    budget = 50000  # 50KB, plenty for this small evidence
    capped = _cap_evidence(evidence, budget_bytes=budget)
    # Should be unchanged if under budget
    assert len(capped["runs"]) == 5


def test_cap_evidence_over_budget():
    """Test evidence capping when evidence exceeds budget."""
    # Create a large evidence structure
    runs = [{"id": f"run_{i}", "data": "x" * 1000} for i in range(100)]
    evidence = {"runs": runs, "metadata": {"timestamp": "2026-07-31"}}
    budget = 50000  # 50KB cap
    capped = _cap_evidence(evidence, budget_bytes=budget)
    # Should be truncated
    capped_size = len(json.dumps(capped, indent=2).encode("utf-8"))
    assert capped_size <= budget
    assert len(capped.get("runs", [])) < len(runs)
    # Metadata should indicate truncation
    assert "_truncated_runs" in capped or len(capped.get("runs", [])) < 100


def test_cap_evidence_raises_instead_of_summary_only():
    """Un-trimmable over-budget evidence FAILS LOUDLY; it is never summarised away.

    This inverts the previous ``test_cap_evidence_fallback`` expectation on
    purpose (R0-2 item 2).  The old contract returned a three-field note and let
    the run carry on, so 31 nights asked a model to improve the system while
    showing it nothing.  Asking for a judgement with the evidence deleted is
    worse than not asking, so the stage now raises.
    """
    huge_data = "x" * (200 * 1024)
    evidence = {"huge_field": huge_data}
    with pytest.raises(EvidenceStarved) as excinfo:
        _cap_evidence(evidence, budget_bytes=1000)
    # The refusal must name its own remedy, not just complain.
    assert "huge_field" in str(excinfo.value)


# ---------------------------------------------------------------------------
# R0-2 lane A — evidence truth (size-driven truncation) and error truth.
# ---------------------------------------------------------------------------

_FIXTURE = Path(__file__).parent / "fixtures" / "evidence_shape_20260805.json"


def _synthesise_evidence_from_shape() -> dict[str, Any]:
    """Rebuild an evidence dict with the REAL 2026-08-05 top-level proportions.

    The committed fixture is a shape manifest (key, type, item count, encoded
    bytes), so the truncation path is exercised against the real proportions --
    ``runs`` small and early, ``runs_ledger_attempts`` dominant -- without
    checking a 2.3 MB blob into the repo.
    """
    shape = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    evidence: dict[str, Any] = {}
    for entry in shape["top_level"]:
        key, kind, target = entry["key"], entry["type"], entry["encoded_bytes"]
        if kind == "list":
            count = entry["items"]
            if count == 0:
                evidence[key] = []
                continue
            per_item = max(1, (target - 2 - (count - 1)) // count - 20)
            evidence[key] = [{"i": i, "d": "x" * per_item} for i in range(count)]
        elif kind == "dict":
            count = max(1, entry["items"])
            per_item = max(1, target // count - 20)
            evidence[key] = {f"k{i}": "x" * per_item for i in range(count)}
        elif kind == "int":
            evidence[key] = 0
        else:
            evidence[key] = "x" * max(1, target - 2)
    return evidence


def test_cap_evidence_trims_largest_key_not_first_name_match():
    """The dominant key is what gets trimmed — not whichever name matched first.

    Red against the pre-fix code: ``_cap_evidence`` matched the four hardcoded
    names ("runs", "sessions", "executions", "records") and ``break``ed on the
    FIRST match, so on this shape it trimmed ``runs`` (2 entries, 20 KB) and
    never touched ``runs_ledger_attempts`` (1867 entries, 1.26 MB).
    """
    evidence = _synthesise_evidence_from_shape()
    raw = len(json.dumps(evidence, indent=2).encode("utf-8"))
    assert raw > _EVIDENCE_BUDGET_BYTES, "fixture must reproduce an over-budget input"

    capped = _cap_evidence(evidence)

    # 1. It actually fits now (the old code did not, and fell through to the
    #    summary-only note).
    assert len(json.dumps(capped, indent=2).encode("utf-8")) <= _EVIDENCE_BUDGET_BYTES
    # 2. Evidence SURVIVED — this is not the summary-only dict.
    assert set(capped) >= set(evidence), "every top-level key must still be present"
    # 3. The dominant key is the one that lost entries.
    assert len(capped["runs_ledger_attempts"]) < len(evidence["runs_ledger_attempts"])
    # 4. The small key that the old code sacrificed is untouched.
    assert len(capped["runs"]) == len(evidence["runs"])
    # 5. The trim is disclosed to the model, not silent.
    assert "_truncated_runs_ledger_attempts" in capped


def test_cap_evidence_never_returns_summary_only():
    """No input may produce the old three-field 'summary only' payload."""
    evidence = _synthesise_evidence_from_shape()
    capped = _cap_evidence(evidence)
    assert "note" not in capped
    assert "original_size_bytes" not in capped


def test_call_llm_records_the_classified_cause_not_spawn_failure(monkeypatch):
    """A missing credential is recorded as auth_error, never as 'spawn/CLI failure'.

    Red against the pre-fix code: every failure class (park, StageFailure,
    sandbox refusal, adapter-resolution failure) collapsed to ``None`` and the
    raise reported a mechanical spawn failure for all of them.
    """
    result = KimiJsonResult(
        output=None,
        attempts=(
            StageAttempt(
                role="primary",
                component="run_kimi_json.primary",
                harness="cli-kimi",
                model="moonshot-ai/kimi-k3",
                failure_kind="stage_failure",
                outcome="auth_error",
                detail="invalid_authentication_error: api key not configured",
            ),
        ),
    )
    monkeypatch.setattr(
        "omniagentos.reflection.propose.run_kimi_json_result",
        lambda *a, **k: result,
    )
    with pytest.raises(RuntimeError) as excinfo:
        call_llm("prompt")
    message = str(excinfo.value)
    assert "auth_error" in message
    assert "spawn/CLI failure" not in message
    assert "cli-kimi" in message


def test_call_llm_reports_both_lineages_when_the_fallback_also_fails(monkeypatch):
    """A two-lineage failure names BOTH attempts, so neither hides the other."""
    result = KimiJsonResult(
        output=None,
        attempts=(
            StageAttempt(
                role="primary",
                component="run_kimi_json.primary",
                harness="cli-kimi",
                model="moonshot-ai/kimi-k3",
                failure_kind="stage_failure",
                outcome="auth_error",
                detail="invalid api key",
            ),
            StageAttempt(
                role="fallback",
                component="run_kimi_json.fallback",
                harness="cli-codex",
                model="gpt-5.6-terra",
                failure_kind="stage_failure",
                outcome="quota_exhausted",
                detail="insufficient balance",
            ),
        ),
    )
    monkeypatch.setattr(
        "omniagentos.reflection.propose.run_kimi_json_result",
        lambda *a, **k: result,
    )
    with pytest.raises(RuntimeError) as excinfo:
        call_llm("prompt")
    message = str(excinfo.value)
    assert "auth_error" in message and "quota_exhausted" in message
    assert "cli-kimi" in message and "cli-codex" in message


def test_call_llm_asks_the_fallback_lineage(monkeypatch):
    """The proposer opts INTO the cross-lineage fallback (item 4)."""
    seen: dict[str, Any] = {}

    def _fake(prompt, schema, **kwargs):
        seen.update(kwargs)
        return KimiJsonResult(output={"proposals": []}, attempts=())

    monkeypatch.setattr("omniagentos.reflection.propose.run_kimi_json_result", _fake)
    assert json.loads(call_llm("prompt")) == []
    assert seen.get("allow_fallback") is True


def test_run_propose_fallback(tmp_path, monkeypatch):
    """Test run_propose fallback when gemini CLI fails or mock environment is used."""
    db_path = str(tmp_path / "test.db")
    store = SqliteStore(db_path)

    # Monkeypatch call_llm to simulate an LLM response
    mock_response = """
```json
[
  {
    "id": "rfl_prop_fallback_01",
    "kind": "model_config",
    "target": {"file": "configs/modelintel.yaml", "key": "models.gemini.available"},
    "current": false,
    "proposed": true,
    "rationale": "Gemini model is now certified stable.",
    "evidence_refs": ["lesson_20260726"],
    "predicted_impact": "Faster latency",
    "risk_class": "low"
  }
]
```
"""
    monkeypatch.setattr("omniagentos.reflection.propose.call_llm", lambda prompt: mock_response)
    # This test covers the response -> validation -> DB path. Prompt building is
    # stubbed because `_cap_evidence` now REFUSES a date with no harvested
    # evidence (the zero-evidence gate), which is asserted separately in
    # test_zero_evidence_raises_before_the_size_check.
    monkeypatch.setattr(
        "omniagentos.reflection.propose.build_analyst_prompt",
        lambda digest_text, evidence_data, compiled_learnings: "stub prompt",
    )

    res = run_propose(date_str="2026-07-26", db_path=db_path)
    assert len(res["proposals"]) == 1
    assert res["proposals"][0]["id"] == "rfl_prop_fallback_01"

    # Verify database write
    with store._lock:
        row = store._connection.execute(
            "SELECT * FROM reflection_proposals WHERE id = ?", ("rfl_prop_fallback_01",)
        ).fetchone()
        assert row is not None
        assert row["kind"] == "model_config"
        assert row["status"] == "pending"


# ---------------------------------------------------------------------------
# Round-1 critic findings (codex-critic / GPT-5.6 Sol) — regression tests.
# ---------------------------------------------------------------------------


def test_dict_trim_keeps_the_content_not_the_insertion_tail():
    """MAJOR (r1): keeping a dict's LAST n entries dropped all the content.

    The real ``harness_transcripts`` is ordered ``claude, gemini, kimi, codex,
    grok, swarm_verdicts`` where ``claude`` holds every byte of transcript and
    the other five are empty lists.  Trimming 6 -> 3 by insertion order kept
    three empty providers and threw away the only real evidence, while the
    payload still looked populated.
    """
    evidence = {
        "harness_transcripts": {
            "claude": [{"text": "x" * 40_000} for _ in range(5)],
            "gemini": [],
            "kimi": [],
            "codex": [],
            "grok": [],
            "swarm_verdicts": [],
        },
        "filler": [{"d": "x" * 900} for _ in range(900)],
    }
    capped = _cap_evidence(evidence, budget_bytes=200_000)
    transcripts = capped["harness_transcripts"]
    assert "claude" in transcripts, "the only non-empty transcript must survive"
    assert transcripts["claude"], "claude's transcripts must not be emptied"


def test_zero_evidence_raises_before_the_size_check():
    """MAJOR (r1): a starved payload is SMALL, so a size-only guard missed it.

    ``run_propose`` manufactures exactly these sentinels when the harvester
    produced nothing or evidence.json will not parse.  Each is under budget, so
    each used to sail through and reach the proposer.
    """
    for starved in (
        {},
        {"note": "No evidence file found for date 2026-08-05."},
        {"error": "Expecting value: line 1 column 1 (char 0)"},
        {"date": "2026-08-05", "runs": [], "sessions": {}},
    ):
        with pytest.raises(EvidenceStarved):
            _cap_evidence(starved)


def test_under_budget_evidence_with_real_records_still_passes_through():
    """The zero-evidence gate must not reject a small but REAL payload."""
    evidence = {"date": "2026-08-05", "runs": [{"id": "r1"}], "sessions": []}
    assert _cap_evidence(evidence) == evidence


def test_disclosure_overhead_cannot_spin_the_trim_loop():
    """MINOR (r1): each `_truncated_` note costs ~36 bytes, so trimming a tiny
    container can GROW the payload.  Unproductive keys must be retired, not
    retried until the iteration guard trips.

    The bound is 30s (was tightened to 10s, before that 60s): on a 20,000-unit
    payload, the victim-selection rebuild that used to run once per guard tick
    was O(units) per pick -- O(units^2) once nearly every unit gets retired --
    which measured ~47s here, well inside the old 60s slack. 30s is still red
    on that O(n^2) rebuild and green on the O(log n)-pick heap that replaced
    it (this payload finishes in ~3s on an idle host), with margin for the PR
    lane's full-tree escalation, which clamps xdist to the runner's 4 vCPUs
    and runs this test alongside the rest of the suite instead of alone -- the
    same scheduling contention _clamped_workers documents for other
    heavyweight fixtures (measured 2026-08-11..14). 10s had no headroom for
    that; 30s keeps the guard tight against a real regression while
    tolerating CI noise.
    """
    evidence = {f"k{i:05d}": [0, 0] for i in range(20_000)}
    started = time.monotonic()
    with pytest.raises(EvidenceStarved):
        _cap_evidence(evidence, budget_bytes=1000)
    assert time.monotonic() - started < 30, "must retire unproductive keys, not spin"


# ---------------------------------------------------------------------------
# Round-2 critic findings — regression tests.
# ---------------------------------------------------------------------------


def test_quiet_night_scaffolding_is_not_mistaken_for_evidence():
    """MAJOR (r2): the gate counted CONTAINERS, so scaffolding passed as evidence.

    ``harvest_evidence`` writes ``harness_transcripts[name] = digests`` for every
    active adapter, so a night that harvested nothing still ships a six-key dict
    of empty lists.  Counting non-empty containers saw ``len(...) == 6``, called
    it evidence, and let the starved packet through the gate built to stop it.
    """
    quiet_night = {
        "date": "2026-08-05",
        "runs": [],
        "sessions": [],
        "runs_ledger_attempts": [],
        "harness_transcripts": {
            "claude": [],
            "gemini": [],
            "kimi": [],
            "codex": [],
            "grok": [],
            "swarm_verdicts": [],
        },
        "bytes_read": 0,
    }
    with pytest.raises(EvidenceStarved):
        _cap_evidence(quiet_night)

    # One real record anywhere in that same scaffolding is enough to proceed.
    populated = json.loads(json.dumps(quiet_night))
    populated["harness_transcripts"]["claude"] = [{"source_name": "s", "bytes_read": 10}]
    assert _cap_evidence(populated) == populated


def test_trim_container_dict_branch_keeps_largest_not_insertion_tail():
    """MAJOR (r2 follow-up): the earlier test never reached the dict branch.

    ``harness_transcripts`` is a container-of-containers, so it is trimmed
    INSIDE each provider — reverting the dict branch to insertion-tail would
    still have passed.  This exercises the dict branch directly, with the
    content deliberately NOT at the insertion tail.
    """
    scalars = {"big": "x" * 5000, "a": "s", "b": "s", "c": "s"}
    kept = _trim_container(scalars, 2)
    assert "big" in kept, "the largest entry must survive"
    assert len(kept) == 2
    # Original key order is preserved among survivors.
    assert list(kept) == [key for key in scalars if key in kept]


def test_cap_evidence_preserves_every_provider_key_and_does_not_mutate_input():
    """All six providers must survive, and the caller's dict must be untouched."""
    evidence = {
        "harness_transcripts": {
            "claude": [{"text": "x" * 40_000} for _ in range(5)],
            "gemini": [],
            "kimi": [],
            "codex": [],
            "grok": [],
            "swarm_verdicts": [],
        },
        "filler": [{"d": "x" * 900} for _ in range(900)],
    }
    before = json.dumps(evidence, sort_keys=True)
    capped = _cap_evidence(evidence, budget_bytes=300_000)

    assert set(capped["harness_transcripts"]) == set(evidence["harness_transcripts"])
    assert capped["harness_transcripts"]["claude"], "the only real transcript must survive"
    assert json.dumps(evidence, sort_keys=True) == before, "input must not be mutated"


def test_a_trim_that_fits_exactly_is_not_refused_on_an_estimate():
    """MINOR (r2): an approximate 'unproductive' verdict must not starve a run.

    The estimate can be off by a byte or two; a candidate that actually fits
    must be verified exactly before the unit is retired for good.
    """
    evidence = {"runs": [{"a": "xxx", "b": "yyy"}, {"a": "xxx", "b": "yyy"}]}
    exact_one_record = len(
        json.dumps(
            {"runs": [{"a": "xxx", "b": "yyy"}], "_truncated_runs": "kept last 1 of 2"},
            indent=2,
        ).encode("utf-8")
    )
    capped = _cap_evidence(evidence, budget_bytes=exact_one_record)
    assert len(capped["runs"]) == 1
    assert len(json.dumps(capped, indent=2).encode("utf-8")) <= exact_one_record


def test_historical_lessons_and_cap_metadata_are_not_current_evidence():
    """MAJOR (r3): `compiled_learnings` is the HISTORICAL corpus and `caps_hit`
    is the harvester's own truncation metadata. The harvester attaches both
    every night, so counting them let a night with zero current records pass
    the gate carrying only old lessons."""
    starved_but_decorated = {
        "date": "2026-08-05",
        "runs": [],
        "sessions": [],
        "harness_transcripts": {"claude": [], "gemini": [], "kimi": []},
        "compiled_learnings": [{"lesson": "an old lesson from a previous week"}],
        "caps_hit": ["total_token_cap"],
        "bytes_read": 0,
    }
    with pytest.raises(EvidenceStarved):
        _cap_evidence(starved_but_decorated)


def test_two_trims_that_only_fit_in_combination_are_both_kept():
    """MINOR (r3): requiring one trim to finish the whole reduction retired
    every individually-insufficient trim, starving a run that had a viable
    answer available from the combination."""
    evidence = {
        "runs": [{"a": "xxx", "b": "yyy"}, {"a": "xxx", "b": "yyy"}],
        "sessions": [{"a": "xxx", "b": "yyy"}, {"a": "xxx", "b": "yyy"}],
    }
    both_trimmed = {
        "runs": [{"a": "xxx", "b": "yyy"}],
        "sessions": [{"a": "xxx", "b": "yyy"}],
        "_truncated_runs": "kept last 1 of 2",
        "_truncated_sessions": "kept last 1 of 2",
    }
    budget = len(json.dumps(both_trimmed, indent=2).encode("utf-8"))
    capped = _cap_evidence(evidence, budget_bytes=budget)
    assert len(capped["runs"]) == 1 and len(capped["sessions"]) == 1
    assert len(json.dumps(capped, indent=2).encode("utf-8")) <= budget


def test_a_marginal_trim_never_enlarges_a_payload_that_already_fits():
    """R3 critic (MAJOR): an ASYMMETRIC shape the symmetric combination test
    misses. Trimming the large `sessions` unit alone already fits the budget
    (179 bytes). Trimming the tiny two-item `runs` next saves ~5 bytes but its
    `_truncated_runs` note costs ~36, ENLARGING the fitting payload to 211. The
    old accept-if-smaller-than-stale-`final_bytes` test took it anyway and then
    raised EvidenceStarved on a night whose evidence actually fit. A payload
    that meets the budget must be RETURNED, not grown past it and refused."""
    evidence = {"runs": ["x", "x"], "sessions": ["y" * 70, "y" * 70]}
    fits_with_sessions_trimmed = {
        "runs": ["x", "x"],
        "sessions": ["y" * 70],
        "_truncated_sessions": "kept last 1 of 2",
    }
    budget = len(json.dumps(fits_with_sessions_trimmed, indent=2).encode("utf-8"))
    capped = _cap_evidence(evidence, budget_bytes=budget)
    assert capped["runs"] == ["x", "x"]
    assert len(capped["sessions"]) == 1
    assert len(json.dumps(capped, indent=2).encode("utf-8")) <= budget


def test_a_negative_estimated_delta_trim_that_does_not_shrink_is_rejected():
    """F1 (Sol round-3): the strict-shrink guard fired only when the ESTIMATED
    delta was >= 0. On a negative estimate the trim was accepted with no exact
    re-measure, so a size-NEUTRAL trim (213 -> 210 only because a whole record
    was discarded to pay for its own disclosure note) discarded evidence for no
    budget gain. Here trimming `sessions` alone already reaches the budget; the
    dict unit `k` must be left intact because trimming it does not shrink the
    real payload.
    """
    evidence = {
        "k": {"a": "x" * 28, "b": "x" * 28},
        "sessions": ["y" * 40, "y" * 40],
    }
    fits_with_sessions_trimmed = {
        "k": evidence["k"],
        "sessions": ["y" * 40],
        "_truncated_sessions": "kept last 1 of 2",
    }
    budget = len(json.dumps(fits_with_sessions_trimmed, indent=2).encode("utf-8"))
    # Trimming `k` from 2 -> 1 is size-neutral (its disclosure note eats the
    # bytes the dropped record saved), so it must never be accepted.
    both_trimmed = {
        "k": {"a": "x" * 28},
        "sessions": ["y" * 40],
        "_truncated_k": "kept largest 1 of 2",
        "_truncated_sessions": "kept last 1 of 2",
    }
    assert len(json.dumps(both_trimmed, indent=2).encode("utf-8")) == budget
    capped = _cap_evidence(evidence, budget_bytes=budget)
    assert capped["k"] == evidence["k"], "a non-shrinking trim discarded a k record"
    assert len(capped["sessions"]) == 1
    assert len(json.dumps(capped, indent=2).encode("utf-8")) <= budget
