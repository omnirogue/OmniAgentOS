from __future__ import annotations

import sqlite3
from pathlib import Path

from omniagentos.skills import (
    decide_proposal,
    get_skill,
    list_tree,
    propose_update,
    restore_version,
)


def test_seeded_webinar_voice_methods(skills_environment: tuple[Path, Path]) -> None:
    qwen = get_skill("webinars_voice_qwen9b")
    elevenlabs = get_skill("webinars_voice_elevenlabs")
    assert qwen["preferred_method"] == "Use RunPod inference endpoint with HF quantized model"
    assert qwen["fallback_method"] == "ElevenLabs API fallback (higher cost)"
    assert elevenlabs["preferred_method"] == "Use ElevenLabs API"
    assert elevenlabs["fallback_method"] == "Qwen 9B on RunPod"
    webinar = next(node for node in list_tree() if node["category"] == "Webinars")
    voice = next(node for node in webinar["subcategories"] if node["name"] == "Voice")
    assert {skill["id"] for skill in voice["skills"]} == {
        "webinars_voice_qwen9b",
        "webinars_voice_elevenlabs",
    }


def test_propose_decide_restore_and_notifications(
    skills_environment: tuple[Path, Path],
) -> None:
    db_path, _ = skills_environment
    proposal_id = propose_update(
        "infrastructure_runpod_deploy",
        proposed_content="validated version two",
        evidence_files=["evals/runpod.json"],
        linked_execution="run_123",
        risk="major",
        created_by="learning-loop",
    )
    assert get_skill("infrastructure_runpod_deploy")["current_version"] == 1
    result = decide_proposal(
        proposal_id,
        approve=True,
        note="Operator accepted benchmark",
        decided_by="owner",
    )
    assert result["state"] == "approved"
    assert result["new_version"] == 2
    assert get_skill("infrastructure_runpod_deploy")["current_version"] == 2
    assert restore_version("infrastructure_runpod_deploy", 1)["current_version"] == 1
    with sqlite3.connect(db_path) as connection:
        notifications = connection.execute(
            "SELECT kind, ref_type, ref_id FROM notifications WHERE ref_id = ? ORDER BY created_at",
            (proposal_id,),
        ).fetchall()
    assert len(notifications) == 2
    assert all(row == ("approval", "update_proposal", proposal_id) for row in notifications)


def test_fastapi_app_smoke_import() -> None:
    from omniagentos.api.main import app

    assert app.title == "OmniAgentOS Control Plane"
