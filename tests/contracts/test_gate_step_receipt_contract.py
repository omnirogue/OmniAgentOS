"""P2.4-CONTRACT.v1 — golden compat tests for the gate-step-receipt wire shape.

Follows the freeze-test idiom in ``tests/contracts/test_mission_contracts.py``:
wire-format literals are hand-written here, never imported from production,
so a silent production drift trips these tests instead of following it.

The legacy-payload construction pattern (build a receipt dict by hand, sign
it with the store's own HMAC key, write it to disk, then load it back through
the production reader) is copied from
``tests/scheduler/test_gate_evidence.py:254ff``
(``test_v2_record_is_quarantined_before_candidate_bound_reexecution``).

Pinned as of this package: ``STEP_SCHEMA == "omniagentos.gate-step-receipt.v1"``
and the exact ``GateStepReceipt`` field set. Two directions are golden:

* old-writer / new-reader — a receipt built to the frozen v1 field set,
  written and signed exactly as today's :meth:`GateEvidenceStore.record_step`
  would, must still load and verify through today's
  :meth:`GateEvidenceStore.load_step`.
* new-writer / old-reader — a receipt minted by today's
  :func:`record_step_receipt` must serialize to exactly the frozen v1 field
  set (same keys, same schema literal) so a reader written against the v1
  contract could still parse it.

The whole module is parametrized by ``schema_version`` (currently only
``"v1"``) so a future ``STEP_SCHEMA`` v2 with a legacy verify path slots in
as a second parametrized case without restructuring these tests — see
``_GOLDEN_STEP_RECEIPTS`` and ``_LEGACY_VERIFY`` below.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import fields
from pathlib import Path

import pytest

from omniagentos.scheduler import gate_evidence as gate_evidence_mod
from omniagentos.scheduler.gate_evidence import (
    MERGE_GATE_STEP_NAMES,
    GateStepReceipt,
    digest,
    record_step_receipt,
)


@pytest.fixture
def store(tmp_path: Path) -> gate_evidence_mod.GateEvidenceStore:
    return gate_evidence_mod.GateEvidenceStore(tmp_path / "gate-evidence")

# --- wire contract literals — do NOT derive these from production imports ---
# A test that constructs the expected side with `from ... import STEP_SCHEMA`
# follows a silent revert/bump instead of catching it.
_STEP_SCHEMA_V1_WIRE = "omniagentos.gate-step-receipt.v1"

# The exact field set of the v1 GateStepReceipt wire payload, in the order
# GateStepReceipt.to_payload() (dataclasses.asdict) emits them. Changing the
# dataclass's field names, order, or count must fail this pin.
_STEP_RECEIPT_V1_FIELDS: tuple[str, ...] = (
    "schema",
    "step",
    "candidate_sha",
    "merge_base_sha",
    "merge_tree_sha",
    "command",
    "workspace_digest",
    "output_digest",
    "exit_code",
    "summary",
    "started_at",
    "finished_at",
    "nonce",
    "signature",
)

# A frozen, fully-specified v1 step-receipt payload — every value literal, no
# derivation from production defaults. This is the "old writer" byte shape.
_GOLDEN_V1_CANDIDATE_SHA = "a" * 40
_GOLDEN_V1_MERGE_BASE_SHA = "b" * 40
_GOLDEN_V1_MERGE_TREE_SHA = "c" * 40
_GOLDEN_V1_PAYLOAD: dict[str, object] = {
    "schema": _STEP_SCHEMA_V1_WIRE,
    "step": "ladder",
    "candidate_sha": _GOLDEN_V1_CANDIDATE_SHA,
    "merge_base_sha": _GOLDEN_V1_MERGE_BASE_SHA,
    "merge_tree_sha": _GOLDEN_V1_MERGE_TREE_SHA,
    "command": "pytest -q tests/ladder",
    "workspace_digest": "d" * 64,
    "output_digest": "e" * 64,
    "exit_code": 0,
    "summary": "1 passed",
    "started_at": "2026-01-01T09:00:00Z",
    "finished_at": "2026-01-01T09:00:05Z",
    "nonce": "f" * 32,
    "signature": "",  # filled in per-test with the fixture store's own key
}


def _sign_golden_payload(key: bytes, payload: dict[str, object]) -> str:
    """Reproduce GateEvidenceStore._canonical_step_signing_bytes by hand.

    Deliberately re-implemented here (not imported) so this test also pins
    the canonical-signing-bytes shape: sorted-key, compact-separator JSON of
    every field except ``signature``.
    """
    unsigned = {key_name: value for key_name, value in payload.items() if key_name != "signature"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256().name).hexdigest()


# --- schema wire contract ----------------------------------------------------


def test_step_schema_constant_is_v1_wire_contract() -> None:
    """STEP_SCHEMA must be the v1 wire string, asserted independently of production.

    Failing-on-revert: bumping production to
    ``STEP_SCHEMA = "omniagentos.gate-step-receipt.v2"`` without adding a
    legacy verify path must fail this test.
    """
    assert gate_evidence_mod.STEP_SCHEMA == _STEP_SCHEMA_V1_WIRE


def test_step_receipt_dataclass_field_set_is_pinned() -> None:
    """GateStepReceipt's field names/order must match the frozen v1 wire set."""
    actual_fields = tuple(field.name for field in fields(GateStepReceipt))
    assert actual_fields == _STEP_RECEIPT_V1_FIELDS


def test_merge_gate_step_names_include_the_golden_fixture_step() -> None:
    """The fixture's step id must remain a real, known gate step."""
    assert _GOLDEN_V1_PAYLOAD["step"] in MERGE_GATE_STEP_NAMES


# --- direction 1: old-writer payload / new (today's) reader ------------------


class TestOldWriterNewReader:
    """A receipt built to the frozen v1 shape must still load through today's store."""

    def test_golden_v1_payload_round_trips_through_store_load_step(
        self, store: gate_evidence_mod.GateEvidenceStore
    ) -> None:
        payload = dict(_GOLDEN_V1_PAYLOAD)
        payload["signature"] = _sign_golden_payload(store._key, payload)
        path = store._step_receipt_path(payload["step"], payload["candidate_sha"])  # type: ignore[arg-type]
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

        loaded = store.load_step(payload["step"], payload["candidate_sha"])  # type: ignore[arg-type]

        assert loaded is not None
        assert loaded.schema == _STEP_SCHEMA_V1_WIRE
        assert loaded.step == payload["step"]
        assert loaded.candidate_sha == payload["candidate_sha"]
        assert loaded.merge_base_sha == payload["merge_base_sha"]
        assert loaded.merge_tree_sha == payload["merge_tree_sha"]
        assert loaded.command == payload["command"]
        assert loaded.workspace_digest == payload["workspace_digest"]
        assert loaded.output_digest == payload["output_digest"]
        assert loaded.exit_code == payload["exit_code"]
        assert loaded.summary == payload["summary"]
        assert loaded.nonce == payload["nonce"]
        assert loaded.signature == payload["signature"]

    def test_golden_v1_payload_with_wrong_signature_is_rejected(
        self, store: gate_evidence_mod.GateEvidenceStore
    ) -> None:
        """Negative control: a mis-signed old-shape payload must not load."""
        payload = dict(_GOLDEN_V1_PAYLOAD)
        payload["signature"] = "0" * 64
        path = store._step_receipt_path(payload["step"], payload["candidate_sha"])  # type: ignore[arg-type]
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

        with pytest.raises(gate_evidence_mod.GateExecutionInfraError):
            store.load_step(payload["step"], payload["candidate_sha"])  # type: ignore[arg-type]

    def test_golden_v1_payload_missing_a_field_is_rejected(
        self, store: gate_evidence_mod.GateEvidenceStore
    ) -> None:
        """A payload short one field (a real cross-version drift) never loads."""
        payload = dict(_GOLDEN_V1_PAYLOAD)
        del payload["output_digest"]
        # Sign what's left so this is purely a shape rejection, not a bad sig.
        unsigned = {k: v for k, v in payload.items() if k != "signature"}
        canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload["signature"] = hmac.new(store._key, canonical, "sha256").hexdigest()
        path = store._step_receipt_path("dominance-corpus", _GOLDEN_V1_CANDIDATE_SHA)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

        with pytest.raises(gate_evidence_mod.GateExecutionInfraError):
            store.load_step("dominance-corpus", _GOLDEN_V1_CANDIDATE_SHA)


# --- direction 2: new (today's) writer / old-shape reader ---------------------


class TestNewWriterOldReader:
    """A receipt minted by today's writer must match the frozen v1 field set."""

    def test_record_step_receipt_output_matches_the_golden_v1_field_set(
        self, tmp_path: Path
    ) -> None:
        evidence_root = tmp_path / "gate-evidence"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        output_path = tmp_path / "step-output.txt"
        output_path.write_text("1 passed\n", encoding="utf-8")

        record_step_receipt(
            step="ladder",
            candidate_sha=_GOLDEN_V1_CANDIDATE_SHA,
            merge_base_sha=_GOLDEN_V1_MERGE_BASE_SHA,
            merge_tree_sha=_GOLDEN_V1_MERGE_TREE_SHA,
            command="pytest -q tests/ladder",
            workspace=workspace,
            output_path=output_path,
            exit_code=0,
            summary="1 passed",
            evidence_root=evidence_root,
        )

        store = gate_evidence_mod.GateEvidenceStore(evidence_root, create_key=False)
        path = store._step_receipt_path("ladder", _GOLDEN_V1_CANDIDATE_SHA)
        on_disk = json.loads(path.read_text(encoding="utf-8"))

        # An old reader built against the v1 field set could parse this
        # payload without knowing about any field it does not expect.
        assert set(on_disk) == set(_STEP_RECEIPT_V1_FIELDS)
        assert on_disk["schema"] == _STEP_SCHEMA_V1_WIRE
        assert on_disk["step"] == "ladder"
        assert on_disk["candidate_sha"] == _GOLDEN_V1_CANDIDATE_SHA
        assert on_disk["merge_base_sha"] == _GOLDEN_V1_MERGE_BASE_SHA
        assert on_disk["merge_tree_sha"] == _GOLDEN_V1_MERGE_TREE_SHA
        assert on_disk["command"] == "pytest -q tests/ladder"
        assert on_disk["output_digest"] == digest(b"1 passed\n")
        assert on_disk["exit_code"] == 0
        assert on_disk["summary"] == "1 passed"

    def test_recorded_receipt_reloads_through_load_step(self, tmp_path: Path) -> None:
        """The new writer's own output must also satisfy the new reader."""
        evidence_root = tmp_path / "gate-evidence"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        output_path = tmp_path / "step-output.txt"
        output_path.write_text("2 passed\n", encoding="utf-8")

        record_step_receipt(
            step="doctrine",
            candidate_sha=_GOLDEN_V1_CANDIDATE_SHA,
            merge_base_sha=_GOLDEN_V1_MERGE_BASE_SHA,
            merge_tree_sha=_GOLDEN_V1_MERGE_TREE_SHA,
            command="pytest -q tests/doctrine",
            workspace=workspace,
            output_path=output_path,
            exit_code=0,
            summary="2 passed",
            evidence_root=evidence_root,
        )

        store = gate_evidence_mod.GateEvidenceStore(evidence_root, create_key=False)
        loaded = store.load_step("doctrine", _GOLDEN_V1_CANDIDATE_SHA)

        assert loaded is not None
        assert loaded.schema == _STEP_SCHEMA_V1_WIRE
        assert loaded.summary == "2 passed"


# --- future-version slot: parametrized by schema version ---------------------
#
# When a STEP_SCHEMA v2 lands with a legacy verify path, add its golden
# payload/fixture here and extend this dict; the parametrized test below
# then covers both v1 and v2 old-writer/new-reader round trips without any
# other restructuring.

_GOLDEN_STEP_RECEIPTS: dict[str, dict[str, object]] = {
    "v1": _GOLDEN_V1_PAYLOAD,
}


@pytest.mark.parametrize("schema_version", sorted(_GOLDEN_STEP_RECEIPTS))
def test_golden_step_receipt_fixture_is_self_consistent(schema_version: str) -> None:
    """Sanity: every registered golden fixture actually declares its own version tag."""
    payload = _GOLDEN_STEP_RECEIPTS[schema_version]
    assert payload["schema"].endswith(f".{schema_version}")  # type: ignore[union-attr]
