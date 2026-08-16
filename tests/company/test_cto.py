"""CTO daily re-rank + weekly deep architecture review (§7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.contracts import AgentResult, AgentUsage, ResultStatus
from omniagentos.orgdims import company_cto as cto
from omniagentos.orgdims import company_org as org

_USAGE = AgentUsage(wall_ms=1)


def _ok(output_json=None, output_text: str = "") -> AgentResult:
    return AgentResult(
        status=ResultStatus.OK, output_text=output_text, output_json=output_json, usage=_USAGE
    )


def _never_call(*args, **kwargs):  # pragma: no cover - assertion helper
    raise AssertionError("adapter_fn should not have been called")


def test_ranking_score_for_is_a_pure_deterministic_function():
    old_created = "2020-01-01T00:00:00Z"  # far enough in the past to hit the age cap
    score = cto.ranking_score_for("fix", risk_level=2, attempt=0, created_at=old_created)
    expected = (cto._KIND_WEIGHT["fix"] * 10) + cto._MAX_AGE_BONUS_DAYS - (2 * 2) - 0
    assert score == expected
    # same inputs -> same output
    assert cto.ranking_score_for("fix", 2, 0, old_created) == score


def test_daily_review_no_backlog_never_calls_adapter(store):
    result = cto.daily_review(store, adapter_fn=_never_call)
    assert result == {"ranked": [], "narrative": "", "new_improvement_ids": []}


def test_daily_review_updates_ranking_scores_and_sorts_descending(store):
    # both improvements get created_at="now" by the store — the fix should still
    # outrank the docs item purely on kind weight and the risk/attempt penalties.
    id_fix = store.create_improvement(origin="realtime", kind="fix", title="Fix A")
    store.update_improvement_fields(id_fix, risk_level=1, attempt=0)

    id_docs = store.create_improvement(origin="audit", kind="docs", title="Docs B")
    store.update_improvement_fields(id_docs, risk_level=3, attempt=2)

    def mock_fn(harness, prompt, *, output_schema=None, budget=None):
        return _ok(output_json={"narrative": "focus on the fix first", "new_proposals": []})

    result = cto.daily_review(store, adapter_fn=mock_fn)

    ids_in_order = [r["id"] for r in result["ranked"]]
    assert (
        ids_in_order[0] == id_fix
    )  # fix (weight 5) beats docs (weight 1) even after risk/attempt penalties
    assert result["narrative"] == "focus on the fix first"

    fix_after = store.get_improvement(id_fix)
    docs_after = store.get_improvement(id_docs)
    # abs tolerance: ranking_score_for() is a pure function of wall-clock age, so
    # re-evaluating it microseconds later than the store's own internal call
    # legitimately drifts by a hair — that's not the thing under test here.
    assert fix_after.ranking_score == pytest.approx(
        cto.ranking_score_for("fix", 1, 0, fix_after.created_at), abs=1e-2
    )
    assert docs_after.ranking_score == pytest.approx(
        cto.ranking_score_for("docs", 3, 2, docs_after.created_at), abs=1e-2
    )


def test_daily_review_creates_new_proposals_with_cto_origin(store):
    store.create_improvement(origin="realtime", kind="fix", title="Existing item")

    def mock_fn(harness, prompt, *, output_schema=None, budget=None):
        return _ok(
            output_json={
                "narrative": "one new thing surfaced",
                "new_proposals": [
                    {
                        "title": "Add a cost dashboard tile",
                        "summary": "the operator keeps asking for this.",
                        "kind": "optimization",
                        "root_cause": "no visibility",
                        "expected_impact": "faster cost decisions",
                    }
                ],
            }
        )

    result = cto.daily_review(store, adapter_fn=mock_fn)

    assert len(result["new_improvement_ids"]) == 1
    imp = store.get_improvement(result["new_improvement_ids"][0])
    assert imp.origin == "cto"
    assert imp.title == "Add a cost dashboard tile"


def test_daily_review_garbage_narrative_does_not_crash(store):
    store.create_improvement(origin="realtime", kind="fix", title="Existing item")

    def mock_fn(harness, prompt, *, output_schema=None, budget=None):
        return _ok(output_text="the model rambled instead of returning json")

    result = cto.daily_review(store, adapter_fn=mock_fn)  # must not raise

    assert result["narrative"] == ""
    assert result["new_improvement_ids"] == []
    # ranking pass still completed despite the narrative step failing
    assert len(result["ranked"]) == 1


def test_weekly_review_creates_improvements_and_writes_roadmap_note(store, vault_dir):
    org.seed(store, vault_dir=vault_dir, vault_autocommit=False)
    imp_id = store.create_improvement(origin="realtime", kind="fix", title="Old fix")
    store.transition_improvement(imp_id, "proposed", "testing", actor="test")
    store.transition_improvement(imp_id, "testing", "judging", actor="test")
    store.transition_improvement(imp_id, "judging", "approved", actor="test")
    store.transition_improvement(imp_id, "approved", "applying", actor="test")
    store.transition_improvement(imp_id, "applying", "applied", actor="test")

    def mock_fn(harness, prompt, *, output_schema=None, budget=None):
        return _ok(
            output_json={
                "narrative": "retire the unused Benchmark Curator role; try a cheaper model for Ops",
                "proposals": [
                    {
                        "title": "Retire Benchmark Curator",
                        "summary": "underused",
                        "kind": "architecture",
                        "root_cause": "headcount drift",
                        "expected_impact": "lower cost",
                    }
                ],
            }
        )

    result = cto.weekly_review(
        store, adapter_fn=mock_fn, vault_dir=vault_dir, vault_autocommit=False
    )

    assert len(result["new_improvement_ids"]) == 1
    imp = store.get_improvement(result["new_improvement_ids"][0])
    assert imp.origin == "weekly"

    assert result["vault_note_path"]
    note_path = Path(result["vault_note_path"])
    assert note_path.is_file()
    content = note_path.read_text(encoding="utf-8")
    assert "retire the unused Benchmark Curator role" in content
    assert "## Notes (human)" in content


def test_weekly_review_preserves_human_edited_notes_section(store, vault_dir):
    org.seed(store, vault_dir=vault_dir, vault_autocommit=False)

    def mock_fn(harness, prompt, *, output_schema=None, budget=None):
        return _ok(output_json={"narrative": "first pass", "proposals": []})

    first = cto.weekly_review(
        store, adapter_fn=mock_fn, vault_dir=vault_dir, vault_autocommit=False
    )
    note_path = Path(first["vault_note_path"])
    content = note_path.read_text(encoding="utf-8")
    edited = content.replace(
        "## Notes (human)\n", "## Notes (human)\n\nthe operator says: keep this.\n"
    )
    note_path.write_text(edited, encoding="utf-8")

    def mock_fn_2(harness, prompt, *, output_schema=None, budget=None):
        return _ok(output_json={"narrative": "second pass", "proposals": []})

    cto.weekly_review(store, adapter_fn=mock_fn_2, vault_dir=vault_dir, vault_autocommit=False)

    final_content = note_path.read_text(encoding="utf-8")
    assert "the operator says: keep this." in final_content
    assert "second pass" in final_content


def test_weekly_review_adapter_failure_still_writes_note(store, vault_dir):
    org.seed(store, vault_dir=vault_dir, vault_autocommit=False)

    def mock_fn(harness, prompt, *, output_schema=None, budget=None):
        return AgentResult(status=ResultStatus.ERROR, usage=_USAGE, error="boom")

    result = cto.weekly_review(
        store, adapter_fn=mock_fn, vault_dir=vault_dir, vault_autocommit=False
    )

    assert result["new_improvement_ids"] == []
    assert result["vault_note_path"]
    assert Path(result["vault_note_path"]).is_file()


# --------------------------------------------------------------------------
# K7 — machine grant holders are not headcount
# --------------------------------------------------------------------------


def test_machine_grant_holders_never_appear_in_the_weekly_headcount(store, vault_dir):
    """``agents`` is also the identity table broker grants FK to (migration 108).

    Those rows inherit ``enabled=1`` and ``org_role='specialist'`` from
    migration 042's defaults, so ``loop:render_probe`` and
    ``loop:flowers_collection`` were rendered into the weekly review under
    "Agents (headcount/harness for the 'unnecessary agents / better models'
    question)" — machine identities polluting the exact analysis that proposes
    retiring agents.
    """
    from omniagentos.orgdims import company_org as org
    from omniagentos.orgdims.company_cto import _weekly_context

    org.seed(store, vault_dir=vault_dir, vault_autocommit=False)

    seeded = [a for a in store.list_agents() if str(a.id).startswith("loop:")]
    assert seeded, (
        "migration 108 must have seeded loop grant holders, or this test proves "
        "nothing about filtering them"
    )

    context = _weekly_context(store)

    for holder in seeded:
        assert holder.id not in context
        assert holder.name not in context

    # ...and the real roster is still there, so "filter everything" cannot pass.
    assert "CTO" in context


def test_the_same_holders_stay_visible_to_grant_administration(store, vault_dir):
    """The filter is a QUERY-side headcount rule, not a demotion.

    ``enabled`` cannot carry this distinction — migration 048 gave it PAUSE
    semantics — and grant administration deliberately needs these holders
    listed. Pinning both halves stops the fix from being 'reapplied' at seed
    time, where it would break the capability system.
    """
    from omniagentos.orgdims import company_org as org

    org.seed(store, vault_dir=vault_dir, vault_autocommit=False)

    enabled_ids = {a.id for a in store.list_agents(enabled=1)}
    assert any(str(i).startswith("loop:") for i in enabled_ids), (
        "machine holders must remain enabled and queryable; the headcount fix "
        "belongs at the report, not in the identity table"
    )


def test_the_machine_identity_predicate_is_prefix_exact():
    from omniagentos.context.lanes import is_machine_identity

    for machine in ("loop:render_probe", "lane:api", "job:nightly", "human:owner"):
        assert is_machine_identity(machine) is True
    for person in ("agt_abc123", "CTO", "loopy", "laneless", "", None):
        assert is_machine_identity(person) is False
