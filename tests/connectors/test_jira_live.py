"""Live Jira smoke (JG1-E13) — default-skipped; DEFERRED to SETUP-J in this lane.

Marked ``@pytest.mark.live``. Without a working token the module must collect
as skipped and exit 0. When run with ``-m live`` and credentials, only GETs.
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _require_live_token() -> None:
    if not os.environ.get("JIRA_API_TOKEN") or not os.environ.get("JIRA_EMAIL"):
        pytest.skip("JIRA_API_TOKEN/JIRA_EMAIL not set — live smoke deferred to SETUP-J")


def test_live_myself_read_only() -> None:
    """DEFERRED (JG1-E13): requires working token against example-team.atlassian.net."""
    from omniagentos.connectors.jira_client import JiraClient

    methods: list[str] = []

    def on_request(request: httpx.Request) -> None:
        methods.append(request.method.upper())

    http = httpx.Client(
        event_hooks={"request": [on_request]},
        timeout=30.0,
    )
    client = JiraClient(
        base_url=os.environ.get(
            "JIRA_BASE_URL", "https://example-team.atlassian.net"
        ),
        client=http,
    )
    try:
        me = client.myself()
    finally:
        client.close()
    assert me.display_name
    assert me.account_id
    assert set(methods) == {"GET"}
