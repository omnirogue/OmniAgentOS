"""Ruling #3: the envelope schema must not REQUIRE `payload.fixes` on a candidate.

CONTRACT.md §3 is the interop authority. It lists the candidate-only
requirements exhaustively:

    Additionally for `candidate`: `base_sha`, `branch`, `paths`, and >=1
    `evidence` entry with `verified_by: "execution"`.
    ... Everything else -- review verdicts, carrier tables, falsifiers, lane
    splits -- is optional ...

`payload.fixes` is NOT in that list. Yet `schema/envelope.schema.json`'s
candidate branch carried `payload.required: ["fixes"]`, so the offline
validator (bridge/validate_envelope.py) and Integration's admission check
REFUSED every contract-conformant candidate that legitimately omitted a
`fixes` narrative (e.g. one that carries only `resolves`, or a
`carrier_enumeration`, or a plain `summary`). A required/properties mismatch
on `payload.fixes` between the schema and the contract/producers.

Ruling #3 drops `fixes` from the candidate branch's `required` (the field
stays in `properties`, so producers that DO carry it are still type-checked).

Red-first: these five previously-refused, contract-conformant candidates
(none carrying `payload.fixes`) must now VALIDATE against
schema/envelope.schema.json. Before the fix every one fails with
"'fixes' is a required property"; after the fix every one validates.

Note on the ruling's wording: the operator's brief described the red-first as
"a candidate carrying payload.fixes now validates (was refused)". Measured
against the actual tree, a fixes-CARRYING candidate already validates today;
the candidates the schema refuses are the ones that OMIT fixes. This test
pins the semantically-correct, contract-aligned behaviour (fixes-absent
candidates are admissible), which is what dropping `required: ["fixes"]`
produces.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

PKG = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((PKG / "schema" / "envelope.schema.json").read_text())


def _errors(art: dict) -> list[str]:
    v = jsonschema.Draft202012Validator(SCHEMA)
    return [e.message for e in v.iter_errors(art)]


def _base_candidate(payload: dict) -> dict:
    """A candidate satisfying every CONTRACT.md §3 requirement:
    base_sha (full 40), branch, non-empty paths, >=1 execution evidence."""
    return {
        "contract": "v1.1",
        "id": "sha256:" + "a" * 64,
        "kind": "candidate",
        "title": "contract-conformant candidate without payload.fixes",
        "created_at": "2026-08-09T00:00:00Z",
        "producer": {"role": "implementer", "actor": "impl@probe",
                     "lineage": "anthropic"},
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "branch": "fix/thing-0809",
        "paths": ["bridge/governor.py"],
        "evidence": [{"claim": "pytest green", "verified_by": "execution",
                      "command": "pytest -q", "exit_code": 0, "result": "ok"}],
        "payload": payload,
    }


# Five distinct, real payload shapes a producer legitimately emits for a
# candidate WITHOUT a `fixes` narrative. Every one is refused today solely on
# the `required: ["fixes"]` constraint.
PREVIOUSLY_REFUSED = [
    pytest.param({"resolves": "sha256:" + "b" * 64}, id="resolves-only"),
    pytest.param(
        {"carrier_enumeration": [{"site": "bridge/governor.py:12",
                                  "ruling": "reached"}]},
        id="carrier-enumeration-only"),
    pytest.param({"summary": "tightened the bounds check at the parse site"},
                 id="summary-only"),
    pytest.param({"note": "mechanical rename; no behaviour change",
                  "resolves": "sha256:" + "c" * 64},
                 id="note-plus-resolves"),
    pytest.param({}, id="empty-payload-object"),
]


@pytest.mark.parametrize("payload", PREVIOUSLY_REFUSED)
def test_candidate_without_fixes_now_validates(payload):
    art = _base_candidate(payload)
    errs = _errors(art)
    assert errs == [], (
        f"contract-conformant candidate without payload.fixes was REFUSED "
        f"by schema/envelope.schema.json: {errs} -- Ruling #3 regressed "
        f"(payload.required must not include 'fixes')")


def test_no_error_mentions_fixes_as_required():
    """The specific failure mode Ruling #3 closes: no candidate in the corpus
    may be refused with a 'fixes is a required property' message."""
    for payload in (p.values[0] for p in PREVIOUSLY_REFUSED):
        for msg in _errors(_base_candidate(payload)):
            assert "fixes" not in msg, (
                f"schema still refuses on fixes: {msg!r}")


def test_candidate_carrying_fixes_still_validates():
    """Regression guard: producers that DO emit `fixes` must keep validating,
    and the field must remain type-checked (string)."""
    assert _errors(_base_candidate({"fixes": "did the thing"})) == []
    # Non-string fixes is still rejected by the retained properties.fixes type.
    errs = _errors(_base_candidate({"fixes": 123}))
    assert any("fixes" in m or "123" in m for m in errs), errs


def test_candidate_still_requires_contract_fields():
    """Dropping the fixes requirement must not weaken the real candidate
    requirements (base_sha, branch, non-empty paths, execution evidence)."""
    art = _base_candidate({"resolves": "sha256:" + "b" * 64})
    art["paths"] = []  # violates the candidate branch's minItems:1
    assert _errors(art), "empty paths must still be refused for a candidate"
