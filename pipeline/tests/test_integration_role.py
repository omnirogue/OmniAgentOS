"""Pins the 0000000 role rename at the adapter and its copyable carriers.

Commit 0000000 renamed the loop roles in BOTH schemas
(`planning|repair|executor|integration` -> `planner|reviewer|implementer|external`)
but the propagation stopped at the schemas: `bridge/integration.py` still
hard-coded `ROLE = "integration"` and stamped it into every inquiry envelope
and every ledger event it wrote — Fusion finding INT-11, "the adapter exempts
itself from the schema it enforces on everyone else". Commit 61c9b2857 fixed
the constant, added fail-closed self-validation of the adapter's own writes,
and renamed the role VALUES in CONTRACT.md / EXAMPLE.md. It did not leave a
test behind, so nothing stopped the rename from silently regressing.

This file is that missing pin.

ROUND-2 FIX (cross-lineage review, gpt-5.6-sol@xhigh, L2-F1): the round-1
version asserted only that `I.ROLE` was SOME member of the live enum. Since
`planner|reviewer|implementer|external` are ALL current, valid values, that
membership check cannot distinguish the correct role (`implementer`, the
landing/integration adapter's own identity) from any OTHER current role — a
mutation of `ROLE = "implementer"` -> `"planner"` produced a schema-valid
tree and every test here kept passing. A pin for "the adapter carries THIS
specific role" has to assert the specific value, not merely that it is
drawn from the right vocabulary. `test_role_constant_is_exactly_implementer`
below is that hard assertion; the membership/enum tests are kept alongside
it because they catch the OTHER failure mode (a stale pre-rename value, or
an enum that silently grew/shrank) that an equality check alone would miss.

Three carrier classes are covered, because a rename in this codebase is
structurally several renames:
  * the constant itself: its exact value, AND its membership in both
    schema enums;
  * the ledger event and inquiry envelope shapes the adapter actually
    writes — exercised through the REAL writer, `Integration.
    instrument_inquiry` at bridge/integration.py:1538, not a dict this
    test reconstructs from `I.ROLE`. A test that reads the constant and
    then checks the constant against itself cannot fail no matter what the
    constant is; running the genuine write path and reading back what it
    actually put on disk is what gives the pin teeth. The stale-role
    refusal check keeps running the adapter's own `_schema_validate`
    directly (that part exercises the SCHEMA's enforcement, a concern
    independent of what `ROLE` currently is, and pre-rename values are
    hardcoded strings never derived from `I.ROLE`);
  * the docs a prompt-driven writer copies from. The live defect (2026-08-07)
    was a planning loop with no role string in its prompt copying the
    vocabulary it could see in CONTRACT.md's examples: writers read examples,
    not schemas, so an un-renamed example is a live writer of bad artifacts.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
SCHEMA_DIR = PKG / "schema"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PKG))

from bridge import integration as I  # noqa: E402

#: The pre-rename role values. Any of these appearing as a `role` VALUE is a
#: writer the rename missed. These are role names only — `integration` the
#: module, `repair` the round, and the loop DISPLAY names are separate
#: namespaces and are intentionally not matched here (they are only matched
#: in `"role": "..."` position by the docs test below).
PRE_RENAME_ROLES = ("planning", "repair", "executor", "integration")


def _enum(schema_file: str, *path: str) -> list[str]:
    node = json.loads((SCHEMA_DIR / schema_file).read_text())
    for key in path:
        node = node[key]
    return node["enum"]


def test_role_constant_is_exactly_implementer() -> None:
    """The landing/integration adapter is specifically the `implementer`
    role, not merely SOME current role.

    This is the assertion the membership checks below cannot make: all of
    `planner|reviewer|implementer|external` are valid post-rename values, so
    a membership check alone cannot tell `implementer` apart from any of its
    three siblings. Mutating `ROLE = "implementer"` to `"planner"` (or
    `"reviewer"`/`"external"`) must fail HERE — every other assertion in
    this file, including the enum/schema ones, stays green under that
    mutation because "planner" is itself a perfectly valid vocabulary word.
    """
    assert I.ROLE == "implementer", (
        f"bridge/integration.py ROLE={I.ROLE!r}, expected exactly 'implementer' — "
        "the adapter's role identity changed, not just its vocabulary."
    )


def test_role_constant_is_in_the_envelope_producer_role_enum() -> None:
    """`producer.role` on every envelope the adapter writes comes from ROLE."""
    enum = _enum("envelope.schema.json", "properties", "producer", "properties", "role")
    assert I.ROLE in enum, (
        f"bridge/integration.py ROLE={I.ROLE!r} is not in the envelope schema's "
        f"producer.role enum {enum} — the adapter is writing envelopes it would "
        f"itself refuse (INT-11)."
    )


def test_role_constant_is_in_the_ledger_event_role_enum() -> None:
    """`role` on every ledger line the adapter appends comes from ROLE, and the
    ledger is APPEND-ONLY: a bad value there cannot be repaired afterwards."""
    enum = _enum("ledger-event.schema.json", "properties", "role")
    assert I.ROLE in enum, (
        f"bridge/integration.py ROLE={I.ROLE!r} is not in the ledger-event "
        f"schema's role enum {enum} — every event the adapter appends is invalid, "
        f"permanently (INT-11)."
    )


def test_role_constant_is_not_a_pre_rename_value() -> None:
    assert I.ROLE not in PRE_RENAME_ROLES, (
        f"ROLE={I.ROLE!r} is pre-rename vocabulary from before 0000000."
    )


@pytest.mark.usefixtures("conforming_interpreter")
def test_adapter_ledger_event_validates_and_a_stale_role_is_refused() -> None:
    """The pin has teeth: run the adapter's OWN validator over the event shape
    it writes. `ok` for ROLE, `fail` for each pre-rename value — the second
    half is what proves the enum is actually enforced rather than merely
    declared, so this test cannot pass on a schema that stopped checking."""
    def event(role: str) -> dict:
        return {"ts": "2026-08-08T00:00:00Z", "role": role,
                "event": "admitted", "id": "sha256:" + "a" * 64,
                "actor": "integration-adapter"}

    status, msg = I._schema_validate(event(I.ROLE), SCHEMA_DIR, "ledger-event.schema.json")
    assert status == "ok", f"the adapter's own ledger event fails its schema: {msg}"

    for stale in PRE_RENAME_ROLES:
        status, msg = I._schema_validate(event(stale), SCHEMA_DIR, "ledger-event.schema.json")
        assert status == "fail", (
            f"ledger-event.schema.json accepted the pre-rename role {stale!r} "
            f"(status={status}, msg={msg}) — the enum is not enforcing the rename, "
            f"so the pins above prove nothing."
        )


def _gate_verdict(**overrides) -> I.GateVerdict:
    base = dict(result="instrument-error", exit_code=2, slug="dirty-workspace",
                reason="gate workspace has uncommitted files", receipt=None,
                stdout_tail="", duration_s=0.1)
    base.update(overrides)
    return I.GateVerdict(**base)


def _candidate(root: Path, **overrides) -> I.Candidate:
    base = dict(ident="sha256:" + "c" * 64, path=root / "candidate.json", art={},
                paths=["some/file.py"], branch="build/role-pin-0812",
                base_sha="0" * 40, tip_sha="1" * 40, title="role pin candidate")
    base.update(overrides)
    return I.Candidate(**base)


@pytest.mark.usefixtures("conforming_interpreter")
def test_adapter_inquiry_envelope_validates_under_its_role(tmp_path: Path) -> None:
    """The inquiry envelope filed by the REAL WRITER carries `producer.role
    = 'implementer'` on what it actually put on disk.

    Round-1 built this envelope as a dict inline from `I.ROLE` and validated
    that dict — a test that reads the constant and checks the constant
    against itself cannot fail regardless of what the constant is. This
    version drives `Integration.instrument_inquiry` (bridge/integration.py
    :1538), the adapter's own write path for this artifact, with `apply=True`
    so it actually writes to `tmp_path`, then reads back the file the
    adapter produced and asserts on THAT — proving the adapter emits the
    correct role, not merely that a schema can accept it."""
    integ = I.Integration(root=tmp_path, repo=tmp_path, gate_ws=tmp_path,
                          schema_dir=SCHEMA_DIR, apply=True)
    cand = _candidate(tmp_path)
    verdict = _gate_verdict()

    integ.instrument_inquiry(cand, verdict, workspace_fingerprint="fp-0812")

    inquiry_files = sorted((tmp_path / "inquiries").glob("*.json"))
    assert len(inquiry_files) == 1, (
        f"expected exactly one inquiry artifact written, found {inquiry_files}")
    art = json.loads(inquiry_files[0].read_text())

    status, msg = I._schema_validate(art, SCHEMA_DIR)
    assert status == "ok", f"the adapter's own inquiry envelope fails its schema: {msg}"
    assert art["producer"]["role"] == "implementer", (
        f"the inquiry artifact the adapter actually wrote has "
        f"producer.role={art['producer']['role']!r}, expected exactly 'implementer'")


@pytest.mark.usefixtures("conforming_interpreter")
def test_adapter_real_ledger_writes_carry_the_exact_role(tmp_path: Path) -> None:
    """Every ledger line the REAL WRITER appends for an instrument error
    (the `inquired` and `instrument_error` events at bridge/integration.py
    :1551 and :1568) carries `role == 'implementer'` on what actually landed
    in `ledger.jsonl` — not on a line this test assembles itself."""
    integ = I.Integration(root=tmp_path, repo=tmp_path, gate_ws=tmp_path,
                          schema_dir=SCHEMA_DIR, apply=True)
    cand = _candidate(tmp_path)
    verdict = _gate_verdict()

    integ.instrument_inquiry(cand, verdict, workspace_fingerprint="fp-0812")

    ledger_path = tmp_path / "ledger.jsonl"
    assert ledger_path.exists(), "instrument_inquiry did not append to ledger.jsonl"
    events = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
    assert events, "no ledger events were written"
    roles = {ev.get("role") for ev in events}
    assert roles == {"implementer"}, (
        f"real ledger events written by the adapter have role(s)={roles!r}, "
        "expected every one to be exactly {'implementer'}")


#: `"role": "<value>"` in an example a writer can copy. Matches JSON examples
#: in the docs only — prose about the old names (the rename table in
#: CONTRACT.md §, the historical notes) is deliberately preserved and must not
#: trip this.
_DOC_ROLE_VALUE = re.compile(r'"role"\s*:\s*"(' + "|".join(PRE_RENAME_ROLES) + r')"')


@pytest.mark.parametrize("doc", ["CONTRACT.md", "EXAMPLE.md"])
def test_docs_examples_carry_no_pre_rename_role_values(doc: str) -> None:
    """Writers read examples, not schemas — an un-renamed example IS a writer."""
    text = (PKG / doc).read_text()
    hits = [
        f"{doc}:{n}: {line.strip()}"
        for n, line in enumerate(text.splitlines(), 1)
        if _DOC_ROLE_VALUE.search(line)
    ]
    assert not hits, (
        "copyable example(s) still teach pre-rename role values; a prompt-driven "
        "writer copies what it can see:\n  " + "\n  ".join(hits)
    )
