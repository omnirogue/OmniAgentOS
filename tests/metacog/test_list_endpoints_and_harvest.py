"""Tests for the Metacog GET list endpoints and Session Harvest Hook."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.main import app
from omniagentos.api.routes import metacog as metacog_routes
from omniagentos.metacog.service import MetacogService
from omniagentos.metacog.store import MetacogStore
from omniagentos.sessions.dal import SessionsDal, SessionState
from omniagentos.sessions.supervisor import SessionSupervisor


@pytest.fixture()
def client_and_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, MetacogService, SessionsDal]:
    db = str(tmp_path / "test.db")
    monkeypatch.setenv("OMNIAGENTOS_DB", db)
    monkeypatch.setenv("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", str(tmp_path / "arts"))

    # Initialize metacog service
    metacog_svc = MetacogService(store=MetacogStore(db))
    metacog_routes._SERVICE = metacog_svc

    # Initialize session DAL
    session_dal = SessionsDal(db)

    # Initialize client
    client = TestClient(app)

    return client, metacog_svc, session_dal


def test_list_artifacts_and_memories_api(
    client_and_services: tuple[TestClient, MetacogService, SessionsDal],
) -> None:
    client, metacog_svc, _ = client_and_services

    # 1. Register an artifact first to make sure there is one
    reg_res = client.post(
        "/api/metacog/artifacts/register",
        json={
            "artifact_type": "code_diff",
            "content": '{"diff": "+2"}',
            "task_id": "test_task_1",
            "run_id": "test_run_1",
        },
    )
    assert reg_res.status_code == 200
    art_id = reg_res.json()["id"]

    # Test GET list artifacts
    list_art_res = client.get("/api/metacog/artifacts")
    assert list_art_res.status_code == 200
    artifacts = list_art_res.json()["artifacts"]
    assert len(artifacts) >= 1
    assert artifacts[0]["task_id"] == "test_task_1"

    # Filter by task_id
    list_art_filter = client.get("/api/metacog/artifacts?task_id=test_task_1")
    assert list_art_filter.status_code == 200
    assert len(list_art_filter.json()["artifacts"]) == 1

    # Filter by non-existent task_id
    list_art_empty = client.get("/api/metacog/artifacts?task_id=non_existent")
    assert list_art_empty.status_code == 200
    assert len(list_art_empty.json()["artifacts"]) == 0

    # 2. Create memory candidate (must use existing artifact ID as evidence)
    cand_res = client.post(
        "/api/metacog/memory/candidates",
        json={
            "statement": "The user prefers light mode.",
            "evidence": [art_id],
            "memory_type": "fact",
            "company_id": "default",
        },
    )
    assert cand_res.status_code == 200

    # Test GET list memories
    list_mem_res = client.get("/api/metacog/memory")
    assert list_mem_res.status_code == 200
    memories = list_mem_res.json()["memories"]
    assert len(memories) >= 1
    assert memories[0]["statement"] == "The user prefers light mode."

    # Test GET list memories (plural endpoint)
    list_mems_res = client.get("/api/metacog/memories")
    assert list_mems_res.status_code == 200
    assert len(list_mems_res.json()["memories"]) == len(memories)


def test_session_end_harvest_hook(
    client_and_services: tuple[TestClient, MetacogService, SessionsDal], tmp_path: Path
) -> None:
    _, metacog_svc, session_dal = client_and_services

    # Create a dummy session (must use "ses_" prefix)
    session_id = "ses_harvest_1"
    session_dal.create_session(
        {
            "id": session_id,
            "source": "bridge",
            "project_dir": str(tmp_path),
            "provider": "claude",
            "state": SessionState.STARTING.value,
            "session_ref": "ref_1",
        }
    )

    # Add some messages (one with explicit "remember:" instruction)
    session_dal.enqueue_message(
        session_id=session_id,
        message="Testing... and remember: the database must always be backed up before migrations.",
    )
    session_dal.enqueue_message(session_id=session_id, message="Got it, thanks!")

    # Initialize a supervisor on the same DAL
    supervisor = SessionSupervisor(dal=session_dal)

    # Transition the session to completed (calls _finish internally)
    supervisor._finish(session_id, SessionState.COMPLETED)

    # Check that a memory candidate was harvested and stored!
    mems = metacog_svc.list_memories()
    assert len(mems) >= 1

    # Verify that our harvested lesson appears in the memories list
    statements = [m.statement for m in mems]
    assert any("the database must always be backed up before migrations." in s for s in statements)
