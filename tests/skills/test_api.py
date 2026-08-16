from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from omniagentos.api.main import app
from omniagentos.skills import get_skill


def test_edit_route_creates_unselected_version(
    skills_environment: tuple[Path, Path],
) -> None:
    with TestClient(app) as client:
        response = client.put(
            "/api/skills/webinars_script",
            json={"content": "edited script", "change_reason": "Test edit"},
        )
    assert response.status_code == 200
    assert response.json() == 2
    assert get_skill("webinars_script")["current_version"] == 1


def test_low_risk_proposal_auto_approves_and_promotes(
    skills_environment: tuple[Path, Path],
) -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/skills/webinars_script/propose",
            json={"proposed_content": "approved script", "risk": "low"},
        )
        assert response.status_code == 200
        proposal_id = response.json()
        approved = client.get("/api/updates?state=approved").json()
    assert any(proposal["id"] == proposal_id for proposal in approved)
    skill = get_skill("webinars_script")
    assert skill["current_version"] == 2
    assert skill["version"]["content_snapshot"] == "approved script"


def test_model_risk_stays_pending_until_decision(
    skills_environment: tuple[Path, Path],
) -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/skills/webinars_voice_qwen9b/propose",
            json={
                "proposed_content": "new model workflow",
                "risk": "model",
                "evidence_files": ["evidence/model-eval.json"],
                "created_by": "tester",
            },
        )
        proposal_id = response.json()
        pending = client.get("/api/updates?state=pending").json()
        assert any(proposal["id"] == proposal_id for proposal in pending)
        assert get_skill("webinars_voice_qwen9b")["current_version"] == 1
        decision = client.post(
            f"/api/updates/{proposal_id}/decide",
            json={"approve": True, "note": "Evaluation passed", "decided_by": "owner"},
        )
    assert decision.status_code == 200
    assert decision.json()["new_version"] == 2
    assert get_skill("webinars_voice_qwen9b")["current_version"] == 2


def test_restore_and_preferred_routes(skills_environment: tuple[Path, Path]) -> None:
    with TestClient(app) as client:
        client.post(
            "/api/skills/webinars_slides/propose",
            json={"proposed_content": "version two", "risk": "low"},
        )
        restored = client.post("/api/skills/webinars_slides/restore/1")
        preferred = client.post(
            "/api/skills/webinars_slides/preferred", json={"method": "Use reviewed Figma deck"}
        )
        searched = client.get("/api/skills/search?q=Qwen")
        read = client.get("/api/skills/webinars_slides")
        tree = client.get("/api/skills/tree")
    assert restored.status_code == 200
    assert restored.json()["current_version"] == 1
    assert preferred.json()["preferred_method"] == "Use reviewed Figma deck"
    assert searched.status_code == 200 and searched.json()
    assert read.status_code == 200
    assert tree.status_code == 200


def test_get_skills_flat(skills_environment: tuple[Path, Path]) -> None:
    db_path, _ = skills_environment
    with TestClient(app) as client:
        response = client.get("/api/skills")
        assert response.status_code == 200
        rows = response.json()
        slugs = {row["slug"] for row in rows}
        assert {
            "webinars_script",
            "webinars_slides",
            "webinars_video",
            "webinars_voice_elevenlabs",
            "webinars_voice_qwen9b",
            "infrastructure_runpod_deploy",
        } <= slugs
        for row in rows:
            assert {"id", "slug", "title", "domains", "status"} <= set(row)

        limited = client.get("/api/skills", params={"limit": 2})
        assert limited.status_code == 200
        assert len(limited.json()) == 2

        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "UPDATE skills SET status = 'archived' WHERE slug = 'webinars_video'"
            )
        active = client.get("/api/skills")
        assert "webinars_video" not in {row["slug"] for row in active.json()}
        with_archived = client.get("/api/skills", params={"include_archived": True})
        assert "webinars_video" in {row["slug"] for row in with_archived.json()}
