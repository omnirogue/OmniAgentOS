"""Contract tests for the read-only LoopDeck engine projection.

Ported from ``source/pr217-original:tests/api/test_engine_routes.py`` and
adapted to the trimmed v1 surface frozen in
``docs/attempt-1-implementation-design.md`` (SS2): the binding-table tests are
dropped (no migration in this contract) and a bounded ``GET /api/engine/runs``
list plus ``context``/``evidence``/``approval`` projection tests are added.

Evidence contract this file pins down (the builder's ``engine.py`` MUST match
it exactly, since these are the tests it is implemented against):

- ``context`` is read from the run's ``plan_json`` (fallback ``metrics_json``):
  ``repository`` must match ``^[\\w.-]+/[\\w.-]+$``, ``branch`` must match
  ``^[\\w./-]{1,255}$`` with no ``..`` and no leading/trailing ``/``, and
  ``head_sha`` must match ``^[0-9a-f]{7,40}$``. Any field that fails its
  regex becomes ``null`` and is never echoed raw.
- ``evidence`` is accumulated from every event in the fetched window whose
  ``action`` is ``"evidence.reported"``; each such payload may carry partial
  ``commits``/``files``/``tests``/``reports`` arrays. Lists are deduplicated
  (commits by ``sha``, files by ``path``, tests/reports by ``name``) keeping
  the first occurrence and its position (stable order) UNLESS a later
  duplicate carries an adverse ``status`` (``failed``/``error``) while the
  kept entry does not — an adverse status always wins over a non-adverse one
  for the same identity, so a later failure can never be masked by an
  earlier pass.
- Every string value projected anywhere in the snapshot (event payloads,
  evidence entries) is passed through the same secret/host-path redaction as
  the rest of the router: token-like substrings and ``/home``, ``/Users``,
  ``/tmp`` paths become ``"[redacted]"``.
- Any ``url`` field in a projected evidence entry that does not start with
  ``https://github.com/`` is nulled (the entry itself is kept). When the
  original ``url`` was present but invalid (an explicit non-GitHub value),
  the entry also carries ``url_refused: true`` so a client cannot mistake a
  backend refusal for an absent field and derive a link to paper over it.
- ``approval`` is ``{"approved": false, "receipt": null}`` unless an event
  with ``action == "approval.recorded"`` bound to *this* run_id (and this
  run's ``head_sha``, when context has one) is present; artifacts and
  passing tests must never flip it.
- Each snapshot is strictly scoped to the requested ``run_id`` — evidence
  from a different run is never mixed in, which is the backend half of
  "reset evidence when the authoritative run binding changes" (the client
  half is ``mergeEngineRunSnapshot`` in ``hooks.engineRun.test.ts``).

DO NOT COMMIT this file directly — it is folded into the builder's
``feat(engine): add focused LoopDeck evidence API`` commit per the loop
contract; see ``.loopdeck/handoff.md``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.metacog import get_metacog_service
from omniagentos.api.routes.swarm import get_swarm_dal
from omniagentos.db.store import SqliteStore

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _EmptyDal:
    """Minimal DAL: a run-id -> row map, no tasks/attempts/deps.

    Most of the new evidence/context/approval/redaction/link/dedup tests only
    care about the run row + events, so tasks/attempts/deps stay empty to
    keep each scenario's fixture small and single-purpose.
    """

    def __init__(self, runs: dict[str, dict[str, Any]] | None = None) -> None:
        self._runs = runs or {}
        self.get_run_calls: list[str] = []

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        self.get_run_calls.append(run_id)
        return self._runs.get(run_id)

    def tasks_for_run(self, run_id: str) -> list[dict[str, Any]]:
        del run_id
        return []

    def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        del task_id
        return []

    def deps_for_run(self, run_id: str) -> list[dict[str, str]]:
        del run_id
        return []

    def list_runs(self, status: str | None = None) -> list[dict[str, Any]]:
        del status
        return list(self._runs.values())


class _FullFakeDal:
    """Ported verbatim from source/pr217-original, plus a plan_json context."""

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        if run_id == "missing":
            return None
        return {
            "id": run_id,
            "status": "running",
            "goal": "Improve the adapter",
            "project_id": "project-1",
            "board_task_id": "root",
            "working_dir": "/private/engine/repository",
            "plan_json": (
                '{"repository":"acme/widgets","branch":"main",'
                '"head_sha":"deadbeef0123"}'
            ),
            "metrics_json": '{"task_count":2,"private_path":"/secret/path"}',
        }

    def tasks_for_run(self, run_id: str) -> list[dict[str, Any]]:
        del run_id
        return [
            {"id": "root", "status": "in_progress", "owned_paths": ["/"]},
            {
                "id": "task-1",
                "title": "Backend",
                "description": "Implement projection",
                "status": "in_progress",
                "owned_paths": ["omniagentos/api/routes/engine.py"],
            },
        ]

    def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "attempt-1",
                "board_task_id": task_id,
                "status": "running",
                "provider": "codex",
                "model": "gpt-test",
                "account_id": "private-account-id",
                "argv": ["tool", "--token", "secret"],
                "log_path": "/private/log",
            }
        ]

    def deps_for_run(self, run_id: str) -> list[dict[str, str]]:
        del run_id
        return [{"task_id": "task-1", "depends_on_task_id": "task-0"}]

    def list_runs(self, status: str | None = None) -> list[dict[str, Any]]:
        del status
        run = self.get_run("run-1")
        assert run is not None
        return [run]


class _EventStore:
    """Static ``target_id`` -> events map; ``after``/``limit`` are ignored,

    matching the source fake's behavior (real filtering is store-level and
    exercised elsewhere; these tests only pin the router's projection).
    """

    def __init__(self, events: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._events = events or {}

    def get_events_for_target(
        self, target_type: str, target_id: str, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        del target_type, after_id, limit
        return self._events.get(target_id, [])


class _Artifact:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def model_dump(self) -> dict[str, Any]:
        return self._data


class _ArtifactStore:
    def __init__(self, artifacts: list[_Artifact] | None = None) -> None:
        self._artifacts = artifacts or []

    def list_artifacts(self, *, run_id: str, limit: int) -> list[_Artifact]:
        del run_id, limit
        return self._artifacts


def _install(
    dal: Any,
    events: dict[str, list[dict[str, Any]]] | None = None,
    artifacts: list[_Artifact] | None = None,
) -> None:
    app.dependency_overrides[get_swarm_dal] = lambda: dal
    app.dependency_overrides[get_metacog_service] = lambda: SimpleNamespace(
        store=_ArtifactStore(artifacts)
    )
    app.dependency_overrides[get_store] = lambda: _EventStore(events)


@pytest.fixture
def client() -> Iterator[TestClient]:
    try:
        # Do not enter lifespan: these are isolated route-contract tests. The
        # full-app startup suite owns migration-inventory/startup assertions.
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Ported tests
# --------------------------------------------------------------------------


def test_capabilities_requires_auth(client: TestClient) -> None:
    response = client.get("/api/engine/capabilities")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_capabilities_are_versioned_and_contain_no_host_details(
    client: TestClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omniagentos.api.routes.engine.swarm_execute_enabled", lambda: True)
    monkeypatch.setattr("omniagentos.api.routes.engine.swarm_worktrees_enabled", lambda: False)
    response = client.get("/api/engine/capabilities", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["api_version"] == "loopdeck-engine/v1"
    assert body["read_only"] is True
    assert body["capabilities"]["execution_enabled"] is True
    assert body["capabilities"]["worktree_isolation_enabled"] is False
    encoded = response.text.lower()
    assert "working_dir" not in encoded
    assert "session-token" not in encoded


def test_snapshot_404_uses_standard_envelope(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    _install(_FullFakeDal())
    response = client.get("/api/engine/runs/missing/snapshot", headers=auth_headers)
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "engine run not found",
            "detail": {"id": "missing"},
        }
    }


def test_snapshot_is_scoped_cursor_based_and_strictly_projected(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    _install(
        _FullFakeDal(),
        events={
            "run-1": [
                {
                    "id": 42,
                    "action": "attempt.started",
                    "created_at": "2026-08-07T12:00:00Z",
                    "payload_json": '{"task_id":"task-1","provider":"codex",'
                    '"working_dir":"/private/repo","token":"secret"}',
                }
            ]
        },
        artifacts=[
            _Artifact(
                {
                    "id": "artifact-1",
                    "artifact_type": "review",
                    "run_id": "run-1",
                    "content_hash": "a" * 64,
                    "content_uri": "/private/artifacts/blob",
                }
            )
        ],
    )
    response = client.get("/api/engine/runs/run-1/snapshot?after=41&limit=10", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["api_version"] == "loopdeck-engine/v1"
    assert body["run"]["id"] == "run-1"
    assert [task["id"] for task in body["tasks"]] == ["task-1"]
    assert body["progress"] == {"in_progress": 1}
    assert body["next_activity_cursor"] == 42
    assert body["activity"][0]["payload"] == {"task_id": "task-1", "provider": "codex"}
    assert body["artifacts"][0]["content_hash"] == "a" * 64
    assert body["metrics"] == {"task_count": 2}

    # New adapted fields.
    assert body["context"] == {
        "repository": "acme/widgets",
        "branch": "main",
        "head_sha": "deadbeef0123",
    }
    assert body["evidence"] == {"commits": [], "files": [], "tests": [], "reports": []}
    assert body["approval"] == {"approved": False, "receipt": None}

    encoded = response.text.lower()
    for forbidden in (
        "working_dir",
        "owned_paths",
        "content_uri",
        "account_id",
        "private-account-id",
        "argv",
        "/private",
        "secret",
    ):
        assert forbidden not in encoded


def test_snapshot_rejects_invalid_cursor(client: TestClient, auth_headers: dict[str, str]) -> None:
    _install(_FullFakeDal())
    response = client.get("/api/engine/runs/run-1/snapshot?after=-1", headers=auth_headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation"


# --------------------------------------------------------------------------
# New: input validation
# --------------------------------------------------------------------------


def test_snapshot_rejects_malformed_run_id_before_any_dal_call(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    dal = _EmptyDal()
    _install(dal)
    response = client.get("/api/engine/runs/not a valid id!/snapshot", headers=auth_headers)
    assert response.status_code in (400, 422)
    assert response.json()["error"]["code"] in ("validation", "bad_request")
    assert dal.get_run_calls == []


def test_snapshot_tolerates_malformed_plan_json_without_500(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    dal = _EmptyDal(
        runs={
            "run-1": {
                "id": "run-1",
                "status": "running",
                "plan_json": "{not valid json",
                "metrics_json": "{not valid json",
            }
        }
    )
    _install(dal)
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["context"] == {"repository": None, "branch": None, "head_sha": None}


# --------------------------------------------------------------------------
# New: context validation
# --------------------------------------------------------------------------


def test_snapshot_nulls_an_invalid_head_sha_but_keeps_valid_context_fields(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    dal = _EmptyDal(
        runs={
            "run-1": {
                "id": "run-1",
                "status": "running",
                "plan_json": (
                    '{"repository":"acme/widgets","branch":"main",'
                    '"head_sha":"not-a-sha!"}'
                ),
            }
        }
    )
    _install(dal)
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["context"] == {
        "repository": "acme/widgets",
        "branch": "main",
        "head_sha": None,
    }


def test_snapshot_nulls_an_invalid_repository_and_branch(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    dal = _EmptyDal(
        runs={
            "run-1": {
                "id": "run-1",
                "status": "running",
                "plan_json": (
                    '{"repository":"not-a-repo","branch":"../../etc",'
                    '"head_sha":"deadbeef0123"}'
                ),
            }
        }
    )
    _install(dal)
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["context"] == {
        "repository": None,
        "branch": None,
        "head_sha": "deadbeef0123",
    }


# --------------------------------------------------------------------------
# New: evidence redaction, dedup, and link filtering
# --------------------------------------------------------------------------


def test_snapshot_redacts_secrets_and_host_paths_in_evidence(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "evidence.reported",
                    "created_at": "2026-08-07T12:00:00Z",
                    "payload_json": (
                        '{"commits":[{"sha":"abc1234",'
                        '"message":"fix ghp_abcdefghijklmnopqrst leak"}],'
                        '"reports":[{"name":"log at /home/ubuntu/secret/out.log"}]}'
                    ),
                }
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["evidence"]["commits"][0]["message"] == "fix [redacted] leak"
    assert body["evidence"]["reports"][0]["name"] == "log at [redacted]"
    encoded = response.text
    assert "ghp_abcdefghijklmnopqrst" not in encoded
    assert "/home/ubuntu/secret" not in encoded


def test_snapshot_deduplicates_overlapping_evidence_keeping_stable_order(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "evidence.reported",
                    "payload_json": (
                        '{"commits":[{"sha":"aaa1111","message":"first"}],'
                        '"files":[{"path":"a.py"}],'
                        '"tests":[{"name":"test_a","status":"passed"}]}'
                    ),
                },
                {
                    "id": 2,
                    "action": "evidence.reported",
                    "payload_json": (
                        '{"commits":[{"sha":"aaa1111","message":"duplicate-should-not-win"},'
                        '{"sha":"bbb2222","message":"second"}],'
                        '"files":[{"path":"a.py"},{"path":"b.py"}],'
                        '"tests":[{"name":"test_a","status":"failed"},'
                        '{"name":"test_b","status":"passed"}]}'
                    ),
                },
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    evidence = response.json()["evidence"]
    assert [c["sha"] for c in evidence["commits"]] == ["aaa1111", "bbb2222"]
    assert evidence["commits"][0]["message"] == "first"  # non-adverse dup: first occurrence wins
    assert [f["path"] for f in evidence["files"]] == ["a.py", "b.py"]
    assert [t["name"] for t in evidence["tests"]] == ["test_a", "test_b"]
    # A later "failed" always overrides an earlier "passed" for the same
    # identity: an adverse status must never be masked by an earlier pass.
    assert evidence["tests"][0]["status"] == "failed"


def test_snapshot_late_refused_url_overrides_earlier_absent_url(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """CP-003 round 2: a later real refusal must win the dedup.

    Commits/files carry no ``status`` field, so the adverse-status override
    can never fire for them — a commit first reported with NO url (absent)
    and later re-reported with a refused non-GitHub url must surface the
    refusal, not freeze the earlier merely-absent entry forever.
    """
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "evidence.reported",
                    "payload_json": '{"commits":[{"sha":"aaa1111","message":"first"}]}',
                },
                {
                    "id": 2,
                    "action": "evidence.reported",
                    "payload_json": (
                        '{"commits":[{"sha":"aaa1111","message":"re-reported",'
                        '"url":"https://gitlab.com/x/y"}]}'
                    ),
                },
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    evidence = response.json()["evidence"]
    assert len(evidence["commits"]) == 1
    assert evidence["commits"][0]["url"] is None
    assert evidence["commits"][0]["url_refused"] is True


def test_snapshot_adverse_status_and_url_refused_merge_field_wise_status_then_refused(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Round-3 precedence matrix, order 1: an earlier failed test plus a
    later passed-but-refused duplicate must keep BOTH adverse facts. Whole-
    entry replacement would let the later "passed" clobber the earlier
    "failed" while picking up url_refused -- the two must merge field-wise.
    """
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "evidence.reported",
                    "payload_json": '{"tests":[{"name":"test_a","status":"failed"}]}',
                },
                {
                    "id": 2,
                    "action": "evidence.reported",
                    "payload_json": (
                        '{"tests":[{"name":"test_a","status":"passed",'
                        '"url":"https://gitlab.com/x/y"}]}'
                    ),
                },
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    test = response.json()["evidence"]["tests"][0]
    assert test["status"] == "failed"
    assert test["url_refused"] is True


def test_snapshot_adverse_status_and_url_refused_merge_field_wise_refused_then_status(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Round-3 precedence matrix, order 2: an earlier passed-but-refused
    duplicate plus a later failed status must also keep BOTH adverse facts,
    regardless of which carrier arrives first.
    """
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "evidence.reported",
                    "payload_json": (
                        '{"tests":[{"name":"test_a","status":"passed",'
                        '"url":"https://gitlab.com/x/y"}]}'
                    ),
                },
                {
                    "id": 2,
                    "action": "evidence.reported",
                    "payload_json": '{"tests":[{"name":"test_a","status":"failed"}]}',
                },
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    test = response.json()["evidence"]["tests"][0]
    assert test["status"] == "failed"
    assert test["url_refused"] is True


def test_snapshot_reporter_cannot_manufacture_url_refused(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """NEW-003: url_refused is exclusively backend-owned. A reporter that
    sends the flag directly without a rejected non-null url must not have it
    survive projection -- only a genuinely rejected url sets the flag.
    """
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "evidence.reported",
                    "payload_json": '{"commits":[{"sha":"aaa1111","url_refused":true}]}',
                },
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    commit = response.json()["evidence"]["commits"][0]
    assert commit["url"] is None
    assert "url_refused" not in commit


def test_snapshot_filters_non_github_links_but_keeps_the_entry(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "evidence.reported",
                    "payload_json": (
                        '{"commits":[{"sha":"aaa1111","url":"https://gitlab.com/x/y"}],'
                        '"reports":[{"name":"ci",'
                        '"url":"https://github.com/acme/widgets/actions/runs/1"}]}'
                    ),
                }
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    evidence = response.json()["evidence"]
    assert evidence["commits"][0]["sha"] == "aaa1111"
    assert evidence["commits"][0]["url"] is None
    # A present-but-invalid url is an active refusal, distinct from a url the
    # reporter never supplied at all -- the client must not treat it as absent.
    assert evidence["commits"][0]["url_refused"] is True
    assert evidence["reports"][0]["url"] == "https://github.com/acme/widgets/actions/runs/1"
    assert "url_refused" not in evidence["reports"][0]


def test_snapshot_evidence_url_absent_is_not_flagged_as_refused(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "evidence.reported",
                    "payload_json": '{"commits":[{"sha":"aaa1111"}]}',
                }
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    commit = response.json()["evidence"]["commits"][0]
    assert commit["url"] is None
    assert "url_refused" not in commit


def test_snapshot_later_adverse_status_overrides_earlier_non_adverse(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """CP-001: a later ``failed`` must win over an earlier ``passed`` for the
    same identity, in both engine.py's seen-set (this test) and the client's
    ``mergeEngineRunSnapshot`` (``hooks.engineRun.test.ts``)."""
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "evidence.reported",
                    "payload_json": '{"tests":[{"name":"test_a","status":"passed"}]}',
                },
                {
                    "id": 2,
                    "action": "evidence.reported",
                    "payload_json": '{"tests":[{"name":"test_a","status":"failed"}]}',
                },
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    tests = response.json()["evidence"]["tests"]
    assert len(tests) == 1
    assert tests[0]["status"] == "failed"


def test_snapshot_earlier_adverse_status_is_not_overridden_by_a_later_pass(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """A later non-adverse report must not un-fail an already-failed identity
    within the same window; only the adverse status wins in either order."""
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "evidence.reported",
                    "payload_json": '{"tests":[{"name":"test_a","status":"failed"}]}',
                },
                {
                    "id": 2,
                    "action": "evidence.reported",
                    "payload_json": '{"tests":[{"name":"test_a","status":"passed"}]}',
                },
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    tests = response.json()["evidence"]["tests"]
    assert len(tests) == 1
    assert tests[0]["status"] == "failed"


# --------------------------------------------------------------------------
# New: approval is never inferred
# --------------------------------------------------------------------------


def test_snapshot_never_infers_approval_from_artifacts_or_passing_tests(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "evidence.reported",
                    "payload_json": '{"tests":[{"name":"test_a","status":"passed"}]}',
                }
            ]
        },
        artifacts=[_Artifact({"id": "review-1", "artifact_type": "review"})],
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["evidence"]["tests"][0]["status"] == "passed"
    assert len(body["artifacts"]) == 1
    assert body["approval"] == {"approved": False, "receipt": None}


def test_snapshot_approval_flips_only_on_explicit_bound_receipt(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    dal = _EmptyDal(
        runs={
            "run-1": {
                "id": "run-1",
                "status": "running",
                "plan_json": '{"head_sha":"deadbeef0123"}',
            }
        }
    )
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "approval.recorded",
                    "payload_json": (
                        '{"reviewer":"alice","run_id":"run-1",'
                        '"head_sha":"deadbeef0123"}'
                    ),
                }
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["approval"] == {
        "approved": True,
        "receipt": {"reviewer": "alice", "run_id": "run-1", "head_sha": "deadbeef0123"},
    }


def test_snapshot_approval_does_not_flip_for_a_receipt_bound_to_another_run(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "approval.recorded",
                    "payload_json": '{"reviewer":"alice","run_id":"run-OTHER"}',
                }
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["approval"] == {"approved": False, "receipt": None}


# --------------------------------------------------------------------------
# New: bounded runs list
# --------------------------------------------------------------------------


def test_runs_list_is_bounded_and_newest_first(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    dal = _EmptyDal(
        runs={
            "run-1": {"id": "run-1", "status": "done", "created_at": "2026-08-07T10:00:00Z"},
            "run-2": {"id": "run-2", "status": "running", "created_at": "2026-08-07T12:00:00Z"},
        }
    )
    _install(dal)
    response = client.get("/api/engine/runs?limit=1", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["runs"]) == 1
    assert body["runs"][0]["id"] == "run-2"


def test_runs_list_rejects_limit_above_bound(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    _install(_EmptyDal())
    response = client.get("/api/engine/runs?limit=51", headers=auth_headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation"


# --------------------------------------------------------------------------
# Activity timeline fields — role / account label / phase / evidence_links
# --------------------------------------------------------------------------
#
# LoopDeck's readable activity timeline needs these operator-facing fields on
# each projected activity event. They are labels and free text already emitted
# by swarm (see contracts/swarm-api.md payload conventions and scheduler emits
# that include ``role``). ``account_id`` remains forbidden — only the human
# account LABEL may project.


def test_snapshot_activity_projects_role_account_phase_and_evidence_links(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Activity payload allow-list must carry timeline fields the UI renders.

    Previously only task_id/attempt_id/status/reason/provider/model/note were
    projected, which made role/account/phase invisible to the control-plane
    timeline even when the swarm emitter wrote them.
    """
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 7,
                    "action": "task_assigned",
                    "created_at": "2026-08-07T12:00:00Z",
                    "target_type": "swarm_run",
                    "target_id": "run-1",
                    "payload_json": (
                        "{"
                        '"task_id":"task-1",'
                        '"provider":"codex",'
                        '"model":"grok-4.5",'
                        '"role":"implementer",'
                        '"account":"codex-2",'
                        '"phase":"running",'
                        '"status":"running",'
                        '"reason":"slot admitted",'
                        '"account_id":"private-account-id",'
                        '"working_dir":"/private/repo",'
                        '"evidence_links":['
                        '{"label":"aaa1111",'
                        '"url":"https://github.com/acme/widgets/commit/aaa1111"},'
                        '{"label":"evil","url":"https://evil.example.com/x"}'
                        "]"
                        "}"
                    ),
                }
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["activity"]) == 1
    event = body["activity"][0]
    assert event["id"] == 7
    assert event["action"] == "task_assigned"
    assert event["created_at"] == "2026-08-07T12:00:00Z"
    payload = event["payload"]
    assert payload["role"] == "implementer"
    assert payload["account"] == "codex-2"
    assert payload["phase"] == "running"
    assert payload["model"] == "grok-4.5"
    assert payload["provider"] == "codex"
    assert payload["status"] == "running"
    assert payload["reason"] == "slot admitted"
    assert payload["task_id"] == "task-1"
    # Evidence links: GitHub kept, non-GitHub url nulled (entry may stay).
    links = payload["evidence_links"]
    assert isinstance(links, list) and len(links) == 2
    assert links[0]["label"] == "aaa1111"
    assert links[0]["url"] == "https://github.com/acme/widgets/commit/aaa1111"
    assert links[1]["label"] == "evil"
    assert links[1]["url"] is None

    encoded = response.text.lower()
    for forbidden in ("account_id", "private-account-id", "working_dir", "/private"):
        assert forbidden not in encoded


def test_snapshot_activity_omits_unknown_payload_keys_and_still_strips_secrets(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Allow-list stays closed: unknown keys do not leak; secrets still redact."""
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 8,
                    "action": "worker_spawned",
                    "created_at": "2026-08-07T12:01:00Z",
                    "payload_json": (
                        "{"
                        '"role":"reviewer",'
                        '"model":"claude-opus",'
                        '"account":"claude-1",'
                        '"phase":"reviewing",'
                        '"note":"token ghp_abcdefghijklmnopqrst present",'
                        '"argv":["tool","--token","secret"],'
                        '"env":{"HOME":"/home/ubuntu"}'
                        "}"
                    ),
                }
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    payload = response.json()["activity"][0]["payload"]
    assert payload["role"] == "reviewer"
    assert payload["account"] == "claude-1"
    assert payload["phase"] == "reviewing"
    assert "argv" not in payload
    assert "env" not in payload
    assert "ghp_abcdefghijklmnopqrst" not in response.text
    assert "[redacted]" in payload["note"]


# --------------------------------------------------------------------------
# Repair-round tests (cross-lineage receipt crit-20260813T115400Z, F001-F003)
# --------------------------------------------------------------------------


def test_snapshot_activity_type_checks_the_allow_list_so_containers_never_leak(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """F001: an allow-listed KEY must not admit an arbitrary VALUE.

    A dict/list smuggled under role/account/phase (or credential fields inside
    an evidence_links entry) would carry nested private data through the
    projection and into the Raw-payload UI. Label fields project only as plain
    strings and evidence-link entries have their own closed
    ``label``/``name``/``url`` schema.
    """
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 9,
                    "action": "task_assigned",
                    "created_at": "2026-08-07T12:00:00Z",
                    "payload_json": (
                        "{"
                        '"account":{"account_id":"private-account-id",'
                        '"credential":"plaintext-secret"},'
                        '"role":{"internal_role":"reviewer-private"},'
                        '"phase":["running",{"operator_note":"hidden-note"}],'
                        '"note":{"nested":"container-under-scalar-slot"},'
                        '"evidence_links":[{"label":"commit",'
                        '"url":"https://github.com/acme/widgets/commit/aaa1111",'
                        '"account_id":"private-account-id",'
                        '"authorization":"Basic plaintext-secret"}]'
                        "}"
                    ),
                }
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    payload = response.json()["activity"][0]["payload"]

    # Containers under label fields are dropped, not copied.
    for field in ("account", "role", "phase", "note"):
        assert field not in payload, f"{field} admitted a non-string container"

    # Evidence-link entries carry exactly the closed schema.
    links = payload["evidence_links"]
    assert len(links) == 1
    assert set(links[0]) <= {"label", "name", "url"}
    assert links[0]["label"] == "commit"
    assert links[0]["url"] == "https://github.com/acme/widgets/commit/aaa1111"

    encoded = response.text
    for forbidden in (
        "account_id",
        "private-account-id",
        "credential",
        "plaintext-secret",
        "authorization",
        "internal_role",
        "operator_note",
        "container-under-scalar-slot",
    ):
        assert forbidden not in encoded, f"private field leaked: {forbidden}"


def test_snapshot_activity_reads_ts_from_real_store_rows(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """F002: real event rows carry ``ts`` (001_init.sql), not ``created_at``.

    Exercised against the REAL SqliteStore and migration schema — not a
    synthetic dict — so the projected ``created_at`` is pinned to the actual
    column the events table writes.
    """
    store = SqliteStore(":memory:")
    store.insert_event(
        type="swarm",
        actor="runner:worker-1",
        action="run_started",
        target_type="swarm_run",
        target_id="run-1",
        payload={"role": "implementer", "phase": "running"},
    )
    rows = store.get_events_for_target("swarm_run", "run-1")
    assert rows, "real store must return the inserted event"
    assert isinstance(rows[0].get("ts"), str) and rows[0]["ts"]
    assert "created_at" not in rows[0], "schema drifted: this test pins the ts column"

    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    app.dependency_overrides[get_swarm_dal] = lambda: dal
    app.dependency_overrides[get_metacog_service] = lambda: SimpleNamespace(
        store=_ArtifactStore()
    )
    app.dependency_overrides[get_store] = lambda: store

    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    event = response.json()["activity"][0]
    assert event["created_at"] == rows[0]["ts"]
    assert event["payload"] == {"role": "implementer", "phase": "running"}


def test_snapshot_activity_synthetic_created_at_still_projects(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Feeds that already emit ``created_at`` keep working; ``ts`` wins if both."""
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "run_started",
                    "created_at": "2026-08-07T12:00:00Z",
                    "payload_json": "{}",
                },
                {
                    "id": 2,
                    "action": "task_assigned",
                    "ts": "2026-08-07T12:01:00Z",
                    "created_at": "1999-01-01T00:00:00Z",
                    "payload_json": "{}",
                },
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    activity = response.json()["activity"]
    assert activity[0]["created_at"] == "2026-08-07T12:00:00Z"
    assert activity[1]["created_at"] == "2026-08-07T12:01:00Z"


def test_snapshot_bounds_evidence_links_with_an_explicit_truncation_marker(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """F003: one bounded event must not expand into an unbounded payload.

    Both bounds engage on adversarial input: the entry count collapses into an
    explicit marker entry and over-long labels are cut with a visible suffix —
    truncation is never silent.
    """
    raw_count = 1_000
    raw_label_chars = 4_096
    links = [
        {
            "label": "x" * raw_label_chars,
            "url": f"https://github.com/acme/widgets/commit/{index}",
        }
        for index in range(raw_count)
    ]
    dal = _EmptyDal(runs={"run-1": {"id": "run-1", "status": "running"}})
    _install(
        dal,
        events={
            "run-1": [
                {
                    "id": 1,
                    "action": "evidence.reported",
                    "created_at": "2026-08-07T12:00:00Z",
                    "payload_json": json.dumps({"evidence_links": links}),
                }
            ]
        },
    )
    response = client.get("/api/engine/runs/run-1/snapshot", headers=auth_headers)
    assert response.status_code == 200
    projected = response.json()["activity"][0]["payload"]["evidence_links"]

    assert len(projected) < raw_count
    # The last entry is the explicit marker for everything dropped.
    marker = projected[-1]
    assert marker["url"] is None
    assert "truncated" in marker["label"]
    assert str(raw_count - (len(projected) - 1)) in marker["label"]
    # Every projected label is bounded and marked when cut.
    for entry in projected[:-1]:
        assert len(entry["label"]) < raw_label_chars
        assert entry["label"].endswith("… [truncated]")
