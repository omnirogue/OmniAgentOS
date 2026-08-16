"""Adversarial tests for omniagentos.selfimprove.models (F1, F7).

F1: a VerificationGate subclass overriding the `.passed` property must NOT
be able to bypass the HARD RULE at either write boundary — both
capture_skill and append_constraint must gate on `gate.status` directly.

F7: SkillMetadata.skill_id / discipline must reject the kind of payload that
would inject extra Markdown/wikilink structure into a rendered vault note.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omniagentos.selfimprove.constraints import append_constraint
from omniagentos.selfimprove.errors import UnverifiedCaptureError
from omniagentos.selfimprove.models import GateStatus, SkillMetadata, VerificationGate
from omniagentos.selfimprove.skills import capture_skill

from .helpers import sample_gate, sample_metadata


class ForgedGate(VerificationGate):
    """A VerificationGate subclass that lies about `.passed` while its
    `.status` is actually FAILED — exactly F1's evidence."""

    @property
    def passed(self) -> bool:  # type: ignore[override]
        return True


def test_forged_gate_passed_property_is_overridden_but_status_is_failed() -> None:
    forged = ForgedGate(status=GateStatus.FAILED)
    assert forged.passed is True  # the forged property lies
    assert forged.status is GateStatus.FAILED  # the real fact


def test_capture_skill_refuses_a_forged_gate_despite_passed_true(vault_dir: Path) -> None:
    forged = ForgedGate(status=GateStatus.FAILED)
    metadata = sample_metadata()

    with pytest.raises(UnverifiedCaptureError):
        capture_skill(metadata, forged, str(vault_dir), autocommit=False)

    assert not (vault_dir / "playbook").exists()


def test_append_constraint_refuses_a_forged_gate_despite_passed_true(tmp_path: Path) -> None:
    forged = ForgedGate(status=GateStatus.FAILED)

    with pytest.raises(UnverifiedCaptureError):
        append_constraint("proj", "Some rule.", forged, constraints_dir=str(tmp_path))

    assert not (tmp_path / "proj").exists()


def test_verification_gate_is_frozen() -> None:
    gate = sample_gate()
    with pytest.raises(ValidationError):
        gate.status = GateStatus.FAILED  # type: ignore[misc]


# ---------------------------------------------------------------------------
# F7: metadata injection
# ---------------------------------------------------------------------------


def test_skill_metadata_rejects_wikilink_delimiters_in_discipline() -> None:
    payload = "code]]\n\n## Notes (human)\nPWNED\n\n[["
    with pytest.raises(ValidationError):
        SkillMetadata(**{**sample_metadata().model_dump(), "discipline": payload})


def test_skill_metadata_rejects_newline_in_skill_id() -> None:
    with pytest.raises(ValidationError):
        SkillMetadata(**{**sample_metadata().model_dump(), "skill_id": "id\n\n## Notes (human)"})


def test_skill_metadata_rejects_control_characters_in_discipline() -> None:
    with pytest.raises(ValidationError):
        SkillMetadata(**{**sample_metadata().model_dump(), "discipline": "code\x00changes"})


def test_skill_metadata_rejects_blank_skill_id() -> None:
    with pytest.raises(ValidationError):
        SkillMetadata(**{**sample_metadata().model_dump(), "skill_id": "   "})


def test_skill_metadata_rejects_overlong_discipline() -> None:
    with pytest.raises(ValidationError):
        SkillMetadata(**{**sample_metadata().model_dump(), "discipline": "x" * 201})


def test_skill_metadata_accepts_ordinary_discipline_and_skill_id() -> None:
    metadata = sample_metadata()
    assert metadata.discipline == "code-changes"
    assert metadata.skill_id == "add-additive-migration"
