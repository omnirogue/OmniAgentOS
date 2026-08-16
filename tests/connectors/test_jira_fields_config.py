"""jira_fields.yaml logical-name boundary (JG1-E12)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omniagentos.connectors.jira_client import JiraClient, JiraError
from omniagentos.connectors.jira_client import _resolve_custom_fields as resolve_custom_fields


class RecordingTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(204)


def test_logical_names_resolve_into_request_body(tmp_path: Path) -> None:
    """Far side = recorded request body contains customfield_* from config."""
    cfg = tmp_path / "jira_fields.yaml"
    cfg.write_text(
        """
version: 1
fields:
  department:
    type: single_select
    values: [Sales, Operations]
    project_ids:
      ACM: customfield_10042
  ai_proposal_state:
    type: single_select
    values: ["Human-created", "AI-proposed"]
    project_ids:
      ACM: customfield_10043
""",
        encoding="utf-8",
    )
    transport = RecordingTransport()
    http = httpx.Client(transport=transport)
    client = JiraClient(
        base_url="https://example-team.atlassian.net",
        email="bot@example.com",
        api_token="tok",
        client=http,
        sleep=lambda _s: None,
        fields_config_path=cfg,
    )
    # M3: the write surface is intentionally private behind the broker/security
    # boundary. Call the `_put_issue_fields` seam — never restore a public alias.
    client._put_issue_fields(
        "ACM-9",
        "ACM",
        {"department": "Sales", "ai_proposal_state": "AI-proposed"},
    )
    assert len(transport.requests) == 1
    body = json.loads(transport.requests[0].content.decode())
    fields = body["fields"]
    assert "customfield_10042" in fields
    assert fields["customfield_10042"] == {"value": "Sales"}
    assert "customfield_10043" in fields
    assert fields["customfield_10043"] == {"value": "AI-proposed"}
    # Logical names must not appear as raw keys in the wire body.
    assert "department" not in fields
    client.close()


def test_raw_customfield_id_refused_at_boundary(tmp_path: Path) -> None:
    cfg = tmp_path / "jira_fields.yaml"
    cfg.write_text(
        """
version: 1
fields:
  department:
    project_ids: {ACM: customfield_10042}
""",
        encoding="utf-8",
    )
    with pytest.raises(JiraError, match="raw custom field id refused"):
        resolve_custom_fields(
            "ACM",
            {"customfield_10042": {"value": "Sales"}},
            config_path=cfg,
        )


def test_repo_jira_fields_yaml_has_real_ids_and_live_keys() -> None:
    """SETUP-J is complete: every slot holds a real, distinct customfield_* id.

    Pre-SETUP-J this asserted the opposite (PLACEHOLDER present). the operator created the
    two fields separately in each of the five projects, so all ten ids must be
    DISTINCT — a repeated id means one project's context was reused and writes
    would silently land on the wrong field.
    """
    import re

    from omniagentos.connectors.jira_client import load_jira_fields_config

    cfg = load_jira_fields_config()
    assert set(cfg["project_keys"]) == {"ACM", "CA", "INI", "HOO", "OAOS"}
    assert "In Review" in cfg["statuses"]
    assert "Review" not in cfg["statuses"]  # G-A1: never bare Review
    fields = cfg["fields"]
    assert "department" in fields
    assert "ai_proposal_state" in fields

    seen: set[str] = set()
    for name in ("department", "ai_proposal_state"):
        ids = fields[name]["project_ids"]
        for key in ("ACM", "CA", "INI", "HOO", "OAOS"):
            assert key in ids
            value = str(ids[key])
            assert re.fullmatch(r"customfield_\d+", value), f"{name}/{key} = {value}"
            assert "PLACEHOLDER" not in value
            seen.add(value)
    assert len(seen) == 10, f"ids must be distinct per project/field, got {sorted(seen)}"
