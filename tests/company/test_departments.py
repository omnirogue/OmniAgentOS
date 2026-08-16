"""Department health reviews — valid proposals, parse robustness on garbage
output, and "one department's failure never aborts the loop" (§7)."""

from __future__ import annotations

import pytest

from omniagentos.contracts import AgentResult, AgentUsage, ResultStatus
from omniagentos.orgdims import company_departments as departments
from omniagentos.orgdims import company_org as org

_USAGE = AgentUsage(wall_ms=1)


@pytest.fixture(autouse=True)
def _no_archdocs(monkeypatch):
    """archdocs (W9, a co-evolving sibling package) may or may not be present/
    stable in this checkout; keep department-review prompts deterministic and
    scoped to just this package's own context-building regardless."""
    monkeypatch.setattr(departments, "_archdocs_context", lambda focus_terms: "")


def _ok(output_json=None, output_text: str = "") -> AgentResult:
    return AgentResult(
        status=ResultStatus.OK, output_text=output_text, output_json=output_json, usage=_USAGE
    )


def _seeded(store, vault_dir):
    org.seed(store, vault_dir=vault_dir, vault_autocommit=False)
    return store


_VALID_PROPOSALS = {
    "proposals": [
        {
            "title": "Fix flaky retry loop",
            "summary": "Retries spike under load.",
            "kind": "fix",
            "root_cause": "no backoff jitter",
            "risk_hint": 1,
            "expected_impact": "fewer retry_spike events",
            "plan": ["add jitter", "add test"],
        },
        {
            # deliberately malformed kind/risk_hint — must sanitize, not crash
            "title": "Optimize queue depth check",
            "kind": "not_a_real_kind",
            "risk_hint": 999,
        },
    ]
}


def test_review_creates_improvements_from_valid_proposals(store, vault_dir):
    _seeded(store, vault_dir)

    def mock_fn(harness, prompt, *, output_schema=None, budget=None):
        return _ok(output_json=_VALID_PROPOSALS)

    result = departments.run_department_reviews(store, adapter_fn=mock_fn, department="Engineering")

    assert result["reviewed"] == ["Engineering"]
    assert not result["errors"]
    assert len(result["improvement_ids"]) == 2

    imps = [store.get_improvement(i) for i in result["improvement_ids"]]
    assert {imp.origin for imp in imps} == {"department"}
    assert all(imp.proposal_json.get("department") == "Engineering" for imp in imps)
    kinds = {imp.kind for imp in imps}
    assert "fix" in kinds
    assert "optimization" in kinds  # the malformed kind sanitized to the default


def test_review_garbage_output_logged_skip_no_crash(store, vault_dir):
    _seeded(store, vault_dir)

    def mock_fn(harness, prompt, *, output_schema=None, budget=None):
        return _ok(output_text="not json at all, just prose from a confused model")

    result = departments.run_department_reviews(store, adapter_fn=mock_fn)  # no crash

    assert result["improvement_ids"] == []
    assert result["reviewed"] == []
    assert len(result["errors"]) == len(org.DEPARTMENTS)
    assert store.list_improvements() == []


def test_one_department_failure_does_not_abort_the_loop(store, vault_dir):
    _seeded(store, vault_dir)

    def mock_fn(harness, prompt, *, output_schema=None, budget=None):
        if "Security" in prompt:
            raise RuntimeError("simulated adapter crash for Security")
        return _ok(output_json=_VALID_PROPOSALS)

    result = departments.run_department_reviews(store, adapter_fn=mock_fn)

    failed_depts = {e["department"] for e in result["errors"]}
    assert "Security" in failed_depts
    # every other department still got reviewed despite Security's crash
    other_departments = {d["name"] for d in org.DEPARTMENTS if d["name"] != "Security"}
    assert other_departments.issubset(set(result["reviewed"]))
    assert len(result["improvement_ids"]) == 2 * len(other_departments)


def test_department_filter_reviews_only_that_department(store, vault_dir):
    _seeded(store, vault_dir)

    def mock_fn(harness, prompt, *, output_schema=None, budget=None):
        return _ok(output_json=_VALID_PROPOSALS)

    result = departments.run_department_reviews(store, adapter_fn=mock_fn, department="Research")

    assert result["reviewed"] == ["Research"]
    assert result["skipped"] == []
    assert result["errors"] == []


def test_disabled_manager_is_skipped_entirely(store, vault_dir):
    _seeded(store, vault_dir)
    manager = next(
        a for a in store.list_agents(org_role="manager") if a.name == "Engineering Manager"
    )
    store.update_agent(manager.id, enabled=0)

    def mock_fn(harness, prompt, *, output_schema=None, budget=None):
        return _ok(output_json=_VALID_PROPOSALS)

    result = departments.run_department_reviews(store, adapter_fn=mock_fn)

    assert "Engineering" not in result["reviewed"]
    assert "Engineering" not in result["skipped"]
    assert not any(e["department"] == "Engineering" for e in result["errors"])


def test_adapter_error_status_recorded_and_loop_continues(store, vault_dir):
    _seeded(store, vault_dir)

    def mock_fn(harness, prompt, *, output_schema=None, budget=None):
        if "Engineering" in prompt:
            return AgentResult(status=ResultStatus.ERROR, usage=_USAGE, error="rate limited")
        return _ok(output_json=_VALID_PROPOSALS)

    result = departments.run_department_reviews(store, adapter_fn=mock_fn)

    eng_errors = [e for e in result["errors"] if e["department"] == "Engineering"]
    assert len(eng_errors) == 1
    assert "rate limited" in eng_errors[0]["error"]
    assert "Research" in result["reviewed"]  # unaffected department still proceeds
