"""Regression coverage for the read-only, realpath-contained project file API."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from omniagentos.api.routes import projects
from omniagentos.projects import ProjectStore


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_project_containment_migration_old_vs_new(tmp_path: Path) -> None:
    real = tmp_path / "real"
    candidate = real / "sub"
    candidate.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    prefix_collision = tmp_path / "real-evil"
    prefix_collision.mkdir()

    legacy_lexical_verdict = candidate == alias or alias in candidate.parents
    assert legacy_lexical_verdict is False
    assert projects._is_within(candidate, alias)
    assert not projects._is_within(prefix_collision, real)


@pytest.mark.real_auth
def test_project_file_listing_requires_session_token(
    asgi_client: httpx.AsyncClient, project_store: ProjectStore
) -> None:
    """An unauthenticated caller cannot enumerate a project's file grants."""
    project_id = str(project_store.create_project({"name": "Private listing"})["id"])

    response = _run(asgi_client.get(f"/api/projects/{project_id}/files"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.real_auth
def test_project_file_preview_requires_session_token(
    asgi_client: httpx.AsyncClient, project_store: ProjectStore
) -> None:
    """An unauthenticated caller cannot read a project-relative file path."""
    project_id = str(project_store.create_project({"name": "Private preview"})["id"])

    response = _run(asgi_client.get(f"/api/projects/{project_id}/files/secret.txt"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.real_auth
def test_encoded_question_mark_cannot_split_gate_from_router(
    asgi_client: httpx.AsyncClient, project_store: ProjectStore
) -> None:
    """A %3F must not let a file GET skip the auth gate (gate/router split-brain).

    ``request.url.path`` truncates at the decoded '?', hiding the ``/files`` segment
    from the gate while the router still dispatches to the file handler on
    ``scope["path"]``. The gate now reads ``scope["path"]`` too, so it still fires.
    """
    project_id = str(project_store.create_project({"name": "Split brain"})["id"])

    response = _run(asgi_client.get(f"/api/projects/{project_id}%3F/files"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_repointed_project_root_symlink_cannot_change_canonical_grant(
    asgi_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A root symlink is canonicalized once; repointing it grants nothing new."""
    granted = tmp_path / "granted"
    outside = tmp_path / "outside"
    granted.mkdir()
    outside.mkdir()
    (outside / "outside-only.txt").write_text("must not escape\n", encoding="utf-8")
    root_link = tmp_path / "root-link"
    root_link.symlink_to(granted, target_is_directory=True)
    monkeypatch.setenv("OMNIAGENTOS_PROJECT_BASES", str(tmp_path))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))

    created = _run(
        asgi_client.post(
            "/api/projects",
            json={"name": "Canonical root", "root_dirs": [str(root_link)]},
        )
    )
    assert created.status_code == 201
    assert created.json()["root_dirs"] == [str(granted.resolve())]

    root_link.unlink()
    root_link.symlink_to(outside, target_is_directory=True)
    blocked = _run(asgi_client.get(f"/api/projects/{created.json()['id']}/files/outside-only.txt"))

    assert blocked.status_code == 404
    assert blocked.json()["error"]["code"] == "not_found"


def test_project_files_list_and_text_preview_stay_within_grants(
    asgi_client: httpx.AsyncClient,
    project_store: ProjectStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A traversal request cannot read a file outside the project's root.

    The percent-encoded path keeps an HTTP client from normalising the ``..``
    segments before FastAPI sees them; the route must still return a clear 403.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "agent-output.md").write_text("# Agent output\n", encoding="utf-8")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    project = project_store.create_project(
        {"name": "Files", "root_dirs": [str(workspace)], "allowed_dirs": []}
    )
    project_id = str(project["id"])

    listing = _run(asgi_client.get(f"/api/projects/{project_id}/files"))
    assert listing.status_code == 200
    assert listing.json()["files"] == [
        {
            "name": "agent-output.md",
            "path": "agent-output.md",
            "size": len("# Agent output\n"),
            "modified": listing.json()["files"][0]["modified"],
            "type": "text",
        }
    ]

    preview = _run(asgi_client.get(f"/api/projects/{project_id}/files/agent-output.md"))
    assert preview.status_code == 200
    assert preview.text == "# Agent output\n"

    traversal_path = "../../etc/passwd"
    encoded_traversal = traversal_path.replace("..", "%2E%2E")
    blocked = _run(asgi_client.get(f"/api/projects/{project_id}/files/{encoded_traversal}"))
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "forbidden"


def test_upload_project_files_happy_path(
    asgi_client: httpx.AsyncClient,
    project_store: ProjectStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """POST /api/projects/{id}/files/upload against the REAL app (F-019).

    ``files_common.py`` (P2) was extracted from this endpoint but previously
    had only the board-files router as a direct HTTP test consumer; this pins
    the projects.py caller too so both sides of the shared module stay covered.
    """
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    project_id = str(project_store.create_project({"name": "Upload target"})["id"])

    response = _run(
        asgi_client.post(
            f"/api/projects/{project_id}/files/upload",
            files=[("files", ("report.txt", b"hello world", "text/plain"))],
            data={"instructions": "please review"},
        )
    )

    assert response.status_code == 201
    body = response.json()
    assert body == {
        "uploaded": [{"name": "report.txt", "path": "uploads/report.txt"}],
        "instructions_saved": True,
    }
    workspace = tmp_path / "var" / "projects" / project_id
    assert (workspace / "uploads" / "report.txt").read_bytes() == b"hello world"
    assert "please review" in (workspace / "uploads" / "_instructions.md").read_text(
        encoding="utf-8"
    )


def test_scoped_managed_workspace_lists_outputs_and_uploads_only(
    asgi_client: httpx.AsyncClient,
    project_store: ProjectStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An orchestration workspace hides managed logs and runner artifacts."""
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    project_id = "proj_scopedworkspace"
    managed_root = tmp_path / "var" / "projects" / project_id
    workspace = managed_root / "workspace"
    uploads = managed_root / "uploads"
    logs = managed_root / "logs"
    workspace.mkdir(parents=True)
    uploads.mkdir()
    logs.mkdir()
    (workspace / "ORCHESTRATION_SPEC.md").write_text("# Spec\n", encoding="utf-8")
    (workspace / "clean-check.txt").write_text("clean.\n", encoding="utf-8")
    (uploads / "brief.txt").write_text("upload\n", encoding="utf-8")
    (logs / "activity.log").write_text("not a deliverable\n", encoding="utf-8")
    (managed_root / "a5c3e8f1").write_text("artifact\n", encoding="utf-8")
    project_store.create_project(
        {
            "id": project_id,
            "name": "Orchestration: Scoped workspace",
            "root_dirs": [str(workspace)],
        }
    )

    listing = _run(asgi_client.get(f"/api/projects/{project_id}/files"))

    assert listing.status_code == 200
    assert [row["path"] for row in listing.json()["files"]] == [
        "uploads/brief.txt",
        "workspace/clean-check.txt",
        "workspace/ORCHESTRATION_SPEC.md",
    ]
    assert (
        _run(asgi_client.get(f"/api/projects/{project_id}/files/workspace/clean-check.txt")).text
        == "clean.\n"
    )
