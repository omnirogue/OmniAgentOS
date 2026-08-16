"""Selection parsing, the <=3 cap, deny-list enforcement, judge fallback."""

from __future__ import annotations

import json

import pytest


def _candidates(executor, n: int = 6):
    return [
        executor.Candidate(id=f"c{i}", title=f"item {i}", source="test", text=f"text {i}")
        for i in range(n)
    ]


def test_parse_picks_valid_and_unknown_ids(executor, sandbox):
    ids = {"c1", "c2"}
    reply = json.dumps(
        {
            "picks": [
                {"id": "c1", "why": "small", "brief": "do it", "verify_hint": "pytest -k x"},
                {"id": "ghost", "why": "?", "brief": "nope"},
            ]
        }
    )
    picks = executor.parse_picks(reply, ids, 3)
    assert [p.id for p in picks] == ["c1"]
    assert picks[0].verify_hint == "pytest -k x"


def test_parse_picks_malformed_raises(executor):
    with pytest.raises(ValueError):
        executor.parse_picks("total prose, no json here", {"c1"}, 3)
    with pytest.raises(ValueError):
        executor.parse_picks('{"not_picks": []}', {"c1"}, 3)
    with pytest.raises(ValueError):
        executor.parse_picks('{"picks": [{"id": "c1"}]}', {"c1"}, 3)  # missing brief/why


def test_parse_picks_tolerates_fenced_json(executor):
    reply = '```json\n{"picks": [{"id": "c1", "why": "w", "brief": "b"}]}\n```'
    picks = executor.parse_picks(reply, {"c1"}, 3)
    assert len(picks) == 1


def test_cap_never_exceeds_three(executor):
    ids = {f"c{i}" for i in range(6)}
    reply = json.dumps({"picks": [{"id": f"c{i}", "why": "w", "brief": "b"} for i in range(6)]})
    picks = executor.parse_picks(reply, ids, executor.HARD_MAX_ITEMS)
    assert len(picks) == executor.HARD_MAX_ITEMS == 3


def test_select_picks_kimi_then_kimi_retry(executor, sandbox, tmp_path):
    calls: list[str] = []
    good = json.dumps({"picks": [{"id": "c0", "why": "w", "brief": "b"}]})

    def kimi(prompt, workdir):
        calls.append("kimi")
        if len(calls) == 1:
            raise RuntimeError("boom")
        return good

    picks = executor.select_picks(
        _candidates(executor),
        "prompt text",
        executor.Policy(),
        workdir=tmp_path,
        kimi_runner=kimi,
    )
    assert calls == ["kimi", "kimi"]
    assert [p.id for p in picks] == ["c0"]


def test_select_picks_all_judges_fail_skips_the_night(executor, sandbox, tmp_path):
    def bad(prompt, workdir):
        raise RuntimeError("no")

    picks = executor.select_picks(
        _candidates(executor),
        "prompt",
        executor.Policy(),
        workdir=tmp_path,
        grok_runner=bad,
        kimi_runner=bad,
    )
    assert picks == []


def test_code_level_deny_list_drops_picks(executor):
    mk = executor.Pick
    picks = [
        mk(id="a", why="ok", brief="tighten the playbook parser and add a unit test"),
        mk(id="b", why="ok", brief="edit configs/policy.yaml mode"),
        mk(id="c", why="ok", brief="rework the Approvals gate"),
        mk(id="d", why="ok", brief="add migration 053 for a new table"),
        mk(id="e", why="ok", brief="write settings.json permission rules"),
        mk(id="f", why="ok", brief="rotate secrets in the vault"),
        mk(id="g", why="ok", brief="add a payment retry"),
        mk(id="h", why="ok", brief="delete stale rows nightly"),
    ]
    kept, dropped = executor.enforce_deny_list(picks)
    assert [p.id for p in kept] == ["a"]
    assert {p.id for p, _ in dropped} == {"b", "c", "d", "e", "f", "g", "h"}


def test_deny_list_scans_why_and_verify_hint_too(executor):
    picks = [
        executor.Pick(id="a", why="removes the payment gate", brief="innocent words"),
        executor.Pick(id="b", why="ok", brief="innocent", verify_hint="pytest tests/approvals"),
    ]
    kept, dropped = executor.enforce_deny_list(picks)
    assert kept == []
    assert len(dropped) == 2
