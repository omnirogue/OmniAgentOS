"""Broker allowlist probe for jira.read (DEFECT 1 / security-class).

Issue-create and transition POSTs must be REFUSED under jira.read.
Capability methods are exactly {GET} — no POST on any path (including search).
"""

from __future__ import annotations

import pytest

from omniagentos.connectors import load_registry
from omniagentos.connectors.broker import BrokerDenied, validate_request


def _jira_read_cap():
    # Bust lru_cache so this probe always sees the on-disk connectors.yaml.
    load_registry.cache_clear()
    return load_registry().capability("jira.read")


def test_jira_read_methods_exactly_get() -> None:
    """Counterfeit-sensitive: method set must be exactly {GET}; loosening goes RED."""
    cap = _jira_read_cap()
    methods = {m.upper() for m in (cap.http.methods if cap.http else [])}
    assert methods == {"GET"}


def test_jira_read_refuses_issue_create_and_transition_posts() -> None:
    """Counterfeit-sensitive: create + transition POSTs must be BrokerDenied."""
    cap = _jira_read_cap()
    with pytest.raises(BrokerDenied):
        validate_request(cap, "POST", "/rest/api/3/issue")
    with pytest.raises(BrokerDenied):
        validate_request(cap, "POST", "/rest/api/3/issue/ACM-1/transitions")
    with pytest.raises(BrokerDenied):
        validate_request(cap, "POST", "/rest/api/3/issue/ACM-1/comment")
    # Read-shaped POSTs also refused under GET-only (broker framework unchanged).
    with pytest.raises(BrokerDenied):
        validate_request(cap, "POST", "/rest/api/3/search/jql")
    with pytest.raises(BrokerDenied):
        validate_request(cap, "POST", "/rest/api/3/issue/bulkfetch")


def test_jira_read_allows_gets() -> None:
    cap = _jira_read_cap()
    validate_request(cap, "GET", "/rest/api/3/myself")
    validate_request(cap, "GET", "/rest/api/3/issue/ACM-1")
    validate_request(cap, "GET", "/rest/api/3/project/ACM/statuses")
    validate_request(cap, "GET", "/rest/api/3/search/jql")
