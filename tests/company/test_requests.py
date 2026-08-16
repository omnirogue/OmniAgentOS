"""Agent-request lifecycle (§7) with a mock adapter_fn — no live LLM CLIs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omniagentos.contracts import AgentResult, AgentUsage, ResultStatus
from omniagentos.orgdims import company_org as org
from omniagentos.orgdims import company_requests as requests

_USAGE = AgentUsage(wall_ms=1)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYTHON = sys.executable


def _result(
    status: ResultStatus = ResultStatus.OK, output_json=None, output_text: str = "", error=None
):
    return AgentResult(
        status=status, output_text=output_text, output_json=output_json, usage=_USAGE, error=error
    )


def _mock_adapter(response: AgentResult):
    def _fn(harness, prompt, *, output_schema=None, budget=None):
        return response

    return _fn


def _seeded(store, vault_dir):
    org.seed(store, vault_dir=vault_dir, vault_autocommit=False)
    return store


def test_create_success_flow(store, vault_dir):
    _seeded(store, vault_dir)
    design = {
        "name": "Latency Sleuth",
        "title": "Latency Investigator",
        "department": "Engineering",
        "role": "specialist",
        "harness": "cli-codex",
        "model": "",
        "charter": "Chase p95 latency regressions.",
        "schedule": {"cadence": "on_demand", "callable": True},
        "expertise": ["latency", "profiling"],
    }
    mock = _mock_adapter(_result(output_json=design))

    req_id = requests.create(store, "we need someone chasing latency regressions", adapter_fn=mock)

    req = store.get_agent_request(req_id)
    assert req.status == "awaiting_approval"
    assert req.design_json["name"] == "Latency Sleuth"
    assert req.design_json["department"] == "Engineering"
    assert req.improvement_id is not None

    imp = store.get_improvement(req.improvement_id)
    assert imp.kind == "new_agent"
    assert imp.origin == "agent_request"
    assert imp.risk_level == 2  # schema default — matches design's "L2" for new_agent
    assert imp.status == "proposed"

    agent_id = requests.approve_and_create(store, req_id, vault_dir=vault_dir)

    req_after = store.get_agent_request(req_id)
    assert req_after.status == "created"
    assert req_after.agent_id == agent_id

    agent = store.get_agent(agent_id)
    assert agent.name == "Latency Sleuth"
    assert agent.harness == "cli-codex"
    assert agent.vault_note_path
    assert Path(agent.vault_note_path).is_file()

    dept = store.get_org_unit(agent.org_unit_id)
    assert dept.name == "Engineering"


def test_create_adapter_error_marks_failed(store, vault_dir):
    _seeded(store, vault_dir)
    mock = _mock_adapter(_result(status=ResultStatus.ERROR, error="cli crashed"))

    req_id = requests.create(store, "anything", adapter_fn=mock)

    req = store.get_agent_request(req_id)
    assert req.status == "failed"
    assert "cli crashed" in req.design_json.get("error", "")


def test_design_existing_request_produces_design_json_artifact(
    store, vault_dir, db_path, tmp_path
):
    """Far-side witness (GC-HYG E1): design worker updates the row with design_json.

    Goes through ``python -m omniagentos.orgdims.company_requests design`` (subprocess)
    so removing the design dispatch branch fails this witness — not a direct
    ``_design_existing_request`` call. Asserts artifact fields, not exit code alone.
    """
    _seeded(store, vault_dir)
    req_id = store.create_agent_request(
        description="need a reliability watchdog agent", from_agent_id="human:owner"
    )
    store.update_agent_request_status(req_id, status="designing")
    # Close the fixture connection so the subprocess can open the same sqlite file.
    store._connection.close()

    design = {
        "name": "Reliability Watchdog",
        "title": "Watchdog",
        "department": "Engineering",
        "role": "specialist",
        "harness": "cli-claude",
        "charter": "Watch reliability events.",
        "schedule": {"cadence": "daily", "callable": True},
        "expertise": ["reliability"],
    }

    # Inject mock adapter via sitecustomize so a true `python -m` subprocess
    # still produces a deterministic design_json (no live LLM).
    site_dir = tmp_path / "site_inject"
    site_dir.mkdir()
    # Patch company_init.default_adapter_fn BEFORE -m execs company_requests so
    # `from company_init import default_adapter_fn` binds the mock (runpy re-exec
    # would wipe a post-import patch on company_requests itself).
    (site_dir / "sitecustomize.py").write_text(
        "import json, os\n"
        "from omniagentos.contracts import AgentResult, AgentUsage, ResultStatus\n"
        "from omniagentos.orgdims import company_init as _ci\n"
        "\n"
        "_design = json.loads(os.environ['OMNIAGENTOS_TEST_DESIGN_JSON'])\n"
        "\n"
        "def _mock(*_a, **_k):\n"
        "    return AgentResult(\n"
        "        status=ResultStatus.OK,\n"
        "        output_json=_design,\n"
        "        output_text='',\n"
        "        usage=AgentUsage(wall_ms=1),\n"
        "    )\n"
        "\n"
        "_ci.default_adapter_fn = _mock\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(site_dir), str(_REPO_ROOT), env.get("PYTHONPATH", "")])
    env["OMNIAGENTOS_VAULT_DIR"] = vault_dir
    env["OMNIAGENTOS_TEST_DESIGN_JSON"] = json.dumps(design)
    # Prevent sitecustomize from importing company_requests before -m sets up paths
    # incorrectly: import happens at site startup; PYTHONPATH has repo root so OK.

    proc = subprocess.run(
        [
            _PYTHON,
            "-m",
            "omniagentos.orgdims.company_requests",
            "design",
            "--request",
            req_id,
            "--db",
            db_path,
        ],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    payload = json.loads(proc.stdout)
    assert payload["id"] == req_id
    assert payload["status"] == "awaiting_approval"
    assert payload["design_json"]["name"] == "Reliability Watchdog"
    assert payload["design_json"]["department"] == "Engineering"
    assert payload["improvement_id"] is not None

    # Row-level far side: re-read from a fresh store connection.
    from omniagentos.reliability.store import SqliteReliabilityStore

    reopened = SqliteReliabilityStore(db_path)
    try:
        row = reopened.get_agent_request(req_id)
        assert row is not None
        assert row.status == "awaiting_approval"
        assert row.design_json["name"] == "Reliability Watchdog"
        assert row.improvement_id == payload["improvement_id"]
    finally:
        reopened._connection.close()


def test_create_unparseable_output_marks_failed_not_crash(store, vault_dir):
    _seeded(store, vault_dir)
    mock = _mock_adapter(_result(output_text="lol not json <html>whoops</html>"))

    req_id = requests.create(store, "anything", adapter_fn=mock)

    req = store.get_agent_request(req_id)
    assert req.status == "failed"
    assert "unparseable" in req.design_json.get("error", "")


def test_reject_records_reason(store, vault_dir):
    _seeded(store, vault_dir)
    design = {"name": "Whatever", "department": "Engineering", "role": "specialist"}
    mock = _mock_adapter(_result(output_json=design))
    req_id = requests.create(store, "anything", adapter_fn=mock)

    requests.reject(store, req_id, decided_by="owner", reason="not needed right now")

    req = store.get_agent_request(req_id)
    assert req.status == "rejected"
    assert req.design_json["reject_reason"] == "not needed right now"


def test_design_unknown_department_falls_back_to_company(store, vault_dir):
    _seeded(store, vault_dir)
    design = {
        "name": "Mystery Agent",
        "title": "Mystery",
        "department": "Nonexistent Dept",
        "role": "specialist",
        "harness": "cli-claude",
        "charter": "does mystery things",
        "schedule": {},
        "expertise": [],
    }
    mock = _mock_adapter(_result(output_json=design))
    req_id = requests.create(store, "mystery request", adapter_fn=mock)

    req = store.get_agent_request(req_id)
    assert req.design_json["department"] is None  # unknown department sanitized away

    agent_id = requests.approve_and_create(store, req_id, vault_dir=vault_dir)
    agent = store.get_agent(agent_id)
    placed_unit = store.get_org_unit(agent.org_unit_id)
    assert placed_unit.kind == "company"  # fell back to company placement


def test_design_name_collision_gets_suffixed(store, vault_dir):
    _seeded(store, vault_dir)
    design = {
        "name": "CTO",  # collides with the seeded CTO
        "department": "Engineering",
        "role": "specialist",
        "harness": "cli-claude",
        "charter": "x",
        "schedule": {},
        "expertise": [],
    }
    mock = _mock_adapter(_result(output_json=design))
    req_id = requests.create(store, "anything", adapter_fn=mock)

    req = store.get_agent_request(req_id)
    assert req.design_json["name"] != "CTO"
    assert req.design_json["name"].startswith("CTO")


def test_create_agent_from_request_requires_approved_status(store, vault_dir):
    _seeded(store, vault_dir)
    design = {"name": "Too Early", "department": "Engineering", "role": "specialist"}
    mock = _mock_adapter(_result(output_json=design))
    req_id = requests.create(
        store, "anything", adapter_fn=mock
    )  # status is awaiting_approval, not approved

    with pytest.raises(ValueError):
        requests.create_agent_from_request(store, req_id, vault_dir=vault_dir)
