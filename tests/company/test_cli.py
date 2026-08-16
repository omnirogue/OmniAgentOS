"""CLI smoke tests (`python -m omniagentos.orgdims.company_requests ...`) —
mock adapter only, never a live LLM CLI. Exercises the argparse dispatch
end-to-end against a tmp_path db."""

from __future__ import annotations

import json

from omniagentos.contracts import AgentResult, AgentUsage, ResultStatus
from omniagentos.orgdims import company_departments as departments
from omniagentos.orgdims import company_requests as requests
from omniagentos.reliability.store import SqliteReliabilityStore

_USAGE = AgentUsage(wall_ms=1)


def _ok(output_json=None, output_text: str = "") -> AgentResult:
    return AgentResult(
        status=ResultStatus.OK, output_text=output_text, output_json=output_json, usage=_USAGE
    )


def test_cli_seed(db_path, vault_dir, monkeypatch, capsys):
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", vault_dir)
    rc = requests.main(["--db", db_path, "seed"])
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert out["org_units_created"]

    s = SqliteReliabilityStore(db_path)
    try:
        assert len(s.list_agents()) > 0
    finally:
        s._connection.close()


def test_cli_request_then_approve(db_path, vault_dir, monkeypatch, capsys):
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", vault_dir)
    requests.main(["--db", db_path, "seed"])
    capsys.readouterr()

    design = {
        "name": "CLI Test Agent",
        "title": "Tester",
        "department": "Engineering",
        "role": "specialist",
        "harness": "cli-claude",
        "charter": "test",
        "schedule": {},
        "expertise": [],
    }
    monkeypatch.setattr(requests, "default_adapter_fn", lambda *a, **k: _ok(output_json=design))

    rc = requests.main(["--db", db_path, "request", "we need a cli test agent"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "awaiting_approval"
    req_id = payload["id"]

    rc = requests.main(["--db", db_path, "approve", req_id])
    assert rc == 0
    approve_payload = json.loads(capsys.readouterr().out)
    assert approve_payload["agent_id"]

    s = SqliteReliabilityStore(db_path)
    try:
        agent = s.get_agent(approve_payload["agent_id"])
        assert agent.name == "CLI Test Agent"
    finally:
        s._connection.close()


def test_cli_review_department(db_path, vault_dir, monkeypatch, capsys):
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", vault_dir)
    requests.main(["--db", db_path, "seed"])
    capsys.readouterr()

    monkeypatch.setattr(
        departments,
        "default_adapter_fn",
        lambda *a, **k: _ok(output_json={"proposals": []}),
    )

    rc = requests.main(["--db", db_path, "review", "--department", "Engineering"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reviewed"] == ["Engineering"]


def test_cli_request_adapter_error_exits_nonzero(db_path, vault_dir, monkeypatch, capsys):
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", vault_dir)
    requests.main(["--db", db_path, "seed"])
    capsys.readouterr()

    monkeypatch.setattr(
        requests,
        "default_adapter_fn",
        lambda *a, **k: AgentResult(status=ResultStatus.ERROR, usage=_USAGE, error="boom"),
    )

    rc = requests.main(["--db", db_path, "request", "anything"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"


def test_cli_design_accepts_db_before_and_after_subcommand(db_path, vault_dir, monkeypatch, capsys):
    """Legacy argv contract: --db works both before and after the design subcommand."""
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", vault_dir)
    requests.main(["--db", db_path, "seed"])
    capsys.readouterr()

    design = {
        "name": "Argv Order Agent",
        "title": "Argv Tester",
        "department": "Engineering",
        "role": "specialist",
        "harness": "cli-claude",
        "charter": "pin both --db orders",
        "schedule": {},
        "expertise": [],
    }
    monkeypatch.setattr(requests, "default_adapter_fn", lambda *a, **k: _ok(output_json=design))

    # Parent-first order: --db PATH design --request ID
    s = SqliteReliabilityStore(db_path)
    try:
        req_a = s.create_agent_request(description="parent-first db order", from_agent_id="human:owner")
        s.update_agent_request_status(req_a, status="pending")
    finally:
        s._connection.close()

    rc = requests.main(["--db", db_path, "design", "--request", req_a])
    assert rc == 0, capsys.readouterr()
    payload_a = json.loads(capsys.readouterr().out)
    assert payload_a["id"] == req_a
    assert payload_a["status"] == "awaiting_approval"
    assert payload_a["design_json"]["name"] == "Argv Order Agent"

    # Legacy order: design --request ID --db PATH
    s = SqliteReliabilityStore(db_path)
    try:
        req_b = s.create_agent_request(description="legacy db order", from_agent_id="human:owner")
        s.update_agent_request_status(req_b, status="pending")
    finally:
        s._connection.close()

    rc = requests.main(["design", "--request", req_b, "--db", db_path])
    assert rc == 0, capsys.readouterr()
    payload_b = json.loads(capsys.readouterr().out)
    assert payload_b["id"] == req_b
    assert payload_b["status"] == "awaiting_approval"
    assert payload_b["design_json"]["name"] == "Argv Order Agent"
