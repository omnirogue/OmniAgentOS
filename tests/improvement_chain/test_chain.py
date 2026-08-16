from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from omniagentos.contracts import AgentResult, AgentUsage, ResultStatus
from omniagentos.improvement_chain import ModelStage, _run_adapter, load_config, run_chain
from omniagentos.routing.config import Account


def _ok_sender(text: str) -> SimpleNamespace:
    """A fake alert sender that reports successful delivery (``.ok is True``),
    matching the ``getattr(result, "ok", False)`` contract ``_alert_once``
    checks before it ever persists a dedup key."""
    return SimpleNamespace(ok=True, detail="sent")


def _config(path: Path, target: str = "devtasks/LOOP-IMPROVEMENT-PLAN.md") -> Path:
    data = {
        "primary": {
            "harness": "cli-kimi",
            "model": "moonshot-ai/kimi-k2.7-code-highspeed",
            "effort": None,
            "can_edit_plans": False,
        },
        "plan_editor": {
            "harness": "cli-claude",
            "model": "claude-opus-5",
            "effort": "xhigh",
            "can_edit_plans": True,
        },
        "final_reviewer": {
            "harness": "cli-claude",
            "model": "claude-fable-5",
            "effort": "high",
            "can_edit_plans": False,
        },
        "plan": {"target": target, "max_bytes": 100_000},
        "sources": {
            "improvement_log_lines": 5,
            "playbook_max_chars": 5000,
            "existing_plan_max_chars": 5000,
            "latest_curator_reports": 1,
            "latest_reflection_reports": 1,
            "latest_backlog_digests": 1,
        },
        "budgets": {
            "primary_wall_ms": 1000,
            "editor_wall_ms": 1000,
            "reviewer_wall_ms": 1000,
        },
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _fresh_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "devtasks").mkdir()
    (repo / "var").mkdir()
    (repo / "vault" / "swarm").mkdir(parents=True)
    return repo


def test_config_requires_an_editing_opus_stage(tmp_path: Path) -> None:
    config_path = _config(tmp_path / "models.yaml")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["plan_editor"]["can_edit_plans"] = False
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="plan_editor"):
        load_config(config_path, root=tmp_path)


def test_config_rejects_plan_path_escape(tmp_path: Path) -> None:
    config_path = _config(tmp_path / "models.yaml", target="../outside.md")
    with pytest.raises(ValueError, match="repository-relative"):
        load_config(config_path, root=tmp_path)


def test_claude_stage_rotates_auth_failures_without_runtime_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failover to a healthy account is still legitimate (a second account
    can be fine while the first's credential expired), so the observable
    rotation behavior is preserved -- but the terminal failure on the broken
    account must now be classified, parked (so the next call stops retrying
    that specific known-broken credential), and alerted, instead of
    disappearing unclassified once a sibling account succeeds."""
    import omniagentos.improvement_chain as chain
    import omniagentos.routing.config as account_config

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")

    seen: list[str] = []
    alerted: list[str] = []

    def sender(text: str) -> SimpleNamespace:
        alerted.append(text)
        return _ok_sender(text)

    monkeypatch.setattr(
        chain,
        "park_chain_failure",
        lambda **kw: _ORIGINAL_PARK(**{**kw, "sender": sender}),
    )

    class FakeAdapter:
        def run(self, agent_input: object) -> AgentResult:
            raise AssertionError(f"unexpected unpooled call: {agent_input}")

        def _run_once(
            self,
            agent_input: object,
            *,
            env_overrides: dict[str, str],
        ) -> AgentResult:
            del agent_input
            seen.append(env_overrides["CLAUDE_CONFIG_DIR"])
            if len(seen) == 1:
                return AgentResult(
                    status=ResultStatus.ERROR,
                    usage=AgentUsage(wall_ms=1),
                    error="Failed to authenticate: OAuth session expired",
                )
            return AgentResult(
                status=ResultStatus.OK,
                output_text="edited",
                usage=AgentUsage(wall_ms=1),
            )

    accounts = [
        Account(id="expired", config_dir="/tmp/expired", priority=0),
        Account(id="healthy", config_dir="/tmp/healthy", priority=1),
    ]
    config = type(
        "AccountConfig",
        (),
        {"providers": {"claude": type("Provider", (), {"accounts": accounts})()}},
    )()
    monkeypatch.setattr(chain, "resolve_adapter", lambda harness: FakeAdapter())
    monkeypatch.setattr(account_config, "load_accounts_config", lambda: config)

    stage = ModelStage(
        harness="cli-claude",
        model="claude-opus-5",
        effort="xhigh",
        can_edit_plans=True,
    )
    result = _run_adapter(stage, object())  # type: ignore[arg-type]

    assert result.status == ResultStatus.OK
    assert seen == ["/tmp/expired", "/tmp/healthy"]

    parked = chain.get_parked_reason(chain._account_component(stage, "expired"))
    assert parked is not None
    assert parked["outcome"] == "auth_error"
    assert len(alerted) == 1


import omniagentos.improvement_chain as _chain_module  # noqa: E402

_ORIGINAL_PARK = _chain_module.park_chain_failure


def test_chain_orders_kimi_then_opus_edit_then_fable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _fresh_repo(tmp_path)
    config_path = _config(repo / "configs" / "loop_models.yaml")
    (repo / "var" / "improvement-log.jsonl").write_text(
        json.dumps({"notes": "existing loop suggestion"}) + "\n",
        encoding="utf-8",
    )
    (repo / "vault" / "swarm" / "playbook.md").write_text(
        "# Playbook\n\n- Improve planner fallback.\n",
        encoding="utf-8",
    )

    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", repo)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: repo / "var" / "loop-review")
    calls: list[str] = []

    def fake_json(
        stage: Any,
        prompt: str,
        schema: dict[str, Any],
        working_dir: Path,
        wall_ms: int,
    ) -> dict[str, Any]:
        del schema, working_dir, wall_ms
        calls.append(stage.model)
        assert "MUST NOT request that permission expansion" in prompt
        if stage.model == "moonshot-ai/kimi-k2.7-code-highspeed":
            assert "existing loop suggestion" in prompt
            return {
                "draft_plan": "# Draft\n\n- Fix planner fallback.",
                "source_findings": ["planner fallback"],
                "deferred": [],
            }
        assert "OPUS-EDITED PLAN" in prompt
        return {
            "verdict": "approved",
            "summary": "The plan is actionable.",
            "strengths": ["Concrete verification"],
            "required_changes": [],
        }

    def fake_editor(
        stage: Any,
        prompt: str,
        workspace: Path,
        plan_name: str,
        wall_ms: int,
    ) -> str:
        del wall_ms
        assert "DIRECTLY EDIT" in prompt
        assert "MUST NOT request that permission expansion" in (
            workspace / "EVIDENCE.md"
        ).read_text(encoding="utf-8")
        calls.append(stage.model)
        target = workspace / plan_name
        target.write_text(
            "# Loop Improvement Plan\n\n"
            "## P0\n\n- Fix planner fallback.\n"
            "- Verify: `pytest tests/swarm/test_planner.py`.\n",
            encoding="utf-8",
        )
        return "Edited the plan directly."

    result = run_chain(
        config_path=config_path,
        artifact_root=repo / "var" / "loop-review",
        json_runner=fake_json,
        plan_editor=fake_editor,
        now=lambda: datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
    )

    assert calls == [
        "moonshot-ai/kimi-k2.7-code-highspeed",
        "claude-opus-5",
        "claude-fable-5",
    ]
    assert result.verdict == "approved"
    plan = (repo / "devtasks" / "LOOP-IMPROVEMENT-PLAN.md").read_text(encoding="utf-8")
    assert "pytest tests/swarm/test_planner.py" in plan
    assert "## Fable final review" in plan
    assert "**Verdict:** `approved`" in plan


def test_default_kimi_seam_accepts_the_kwargs_its_real_call_sites_pass() -> None:
    """The swarm optimizer/summary bind ``run_kimi_json`` as their default runner
    and call it with ``effort``/``max_turns``/``wall_ms``. Both wrap the call in a
    bare ``except Exception``, so a signature mismatch degrades the narrative pass
    to silently-disabled instead of failing loudly. Bind the real signature here.
    """
    import inspect

    from omniagentos.improvement_chain import run_kimi_json

    inspect.signature(run_kimi_json).bind(
        "prompt", {}, effort="medium", max_turns=3, wall_ms=180_000
    )


def test_quota_error_parks_and_alerts_once_and_zero_provider_calls_on_repeat_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal (quota) failure from the Kimi stage must park the chain, send
    exactly one alert, and -- once parked -- the 2nd and 3rd scheduler ticks
    must make ZERO further provider calls (short-circuited by the persisted
    parked state), not merely fail loudly again. No live provider call is
    made -- the failing stage is a fake. The suspension sample is isolated
    to the single live Moonshot phrasing ("insufficient balance") so this
    proves quota classification specifically, not an auth+quota compound."""
    repo = _fresh_repo(tmp_path)
    config_path = _config(repo / "configs" / "loop_models.yaml")

    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", repo)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: repo / "var" / "loop-review")

    sent: list[str] = []
    provider_calls = 0

    def failing_kimi_json(
        stage: Any,
        prompt: str,
        schema: dict[str, Any],
        working_dir: Path,
        wall_ms: int,
    ) -> dict[str, Any]:
        nonlocal provider_calls
        del prompt, schema, working_dir, wall_ms
        provider_calls += 1
        if stage.harness == "cli-kimi":
            raise chain.StageFailure(stage, "insufficient balance")
        raise AssertionError("editor/reviewer must not run once Kimi has failed")

    def unreachable_editor(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("plan editor must not run once Kimi has failed")

    fixed_now = lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC)  # noqa: E731

    def fake_sender(text: str) -> SimpleNamespace:
        sent.append(text)
        return _ok_sender(text)

    monkeypatch.setattr(
        chain,
        "park_chain_failure",
        lambda **kw: _ORIGINAL_PARK(**{**kw, "sender": fake_sender}),
    )

    for tick in range(3):
        expected = chain.StageFailure if tick == 0 else chain.ChainParked
        with pytest.raises(expected):
            chain.run_chain(
                config_path=config_path,
                artifact_root=repo / "var" / "loop-review" / f"tick-{tick}",
                json_runner=failing_kimi_json,
                plan_editor=unreachable_editor,
                now=fixed_now,
            )

    assert provider_calls == 1, "ticks 2 and 3 must short-circuit before any provider call"
    assert len(sent) == 1
    assert "quota_exhausted" in sent[0]

    parked = chain.get_parked_reason("improvement_chain.primary")
    assert parked is not None
    assert parked["outcome"] == "quota_exhausted"


def test_transient_rate_limit_still_propagates_without_parking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient (retryable) provider error must NOT be parked or alerted --
    only quota/auth are terminal -- and must still propagate (it is genuinely
    retried on the next natural tick, not swallowed)."""
    repo = _fresh_repo(tmp_path)
    config_path = _config(repo / "configs" / "loop_models.yaml")

    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", repo)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: repo / "var" / "loop-review")

    monkeypatch.setattr(
        chain, "park_chain_failure", lambda **kw: pytest.fail("transient errors must not park")
    )

    def failing_kimi_json(
        stage: Any,
        prompt: str,
        schema: dict[str, Any],
        working_dir: Path,
        wall_ms: int,
    ) -> dict[str, Any]:
        del prompt, schema, working_dir, wall_ms
        raise chain.StageFailure(stage, "rate_limit_reached_error: too many requests")

    with pytest.raises(chain.StageFailure):
        chain.run_chain(
            config_path=config_path,
            artifact_root=repo / "var" / "loop-review",
            json_runner=failing_kimi_json,
            plan_editor=lambda *a, **k: pytest.fail("unreachable"),
            now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        )

    assert chain.get_parked_reason("improvement_chain.primary") is None

    # And a second tick must retry for real (no short-circuit): the same
    # provider call happens again, proving transient failures are not parked.
    calls = 0

    def failing_again(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise chain.StageFailure(args[0], "rate_limit_reached_error: too many requests")

    with pytest.raises(chain.StageFailure):
        chain.run_chain(
            config_path=config_path,
            artifact_root=repo / "var" / "loop-review" / "tick-2",
            json_runner=failing_again,
            plan_editor=lambda *a, **k: pytest.fail("unreachable"),
            now=lambda: datetime(2026, 8, 5, 9, 5, tzinfo=UTC),
        )
    assert calls == 1


def test_unknown_error_never_reads_as_terminal_or_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unclassified error text must not be treated as favourable (it still
    fails the stage) and must not be treated as terminal (no park, no
    alert) -- ``classify_chain_error`` returning ``None`` is not a verdict."""
    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")
    monkeypatch.setattr(
        chain, "park_chain_failure", lambda **kw: pytest.fail("unknown errors must not park")
    )

    stage = ModelStage(
        harness="cli-kimi", model="moonshot-ai/kimi-k3", effort=None, can_edit_plans=False
    )
    exc = chain.StageFailure(stage, "some unrelated tool crash with no known shape")
    outcome = chain._handle_stage_failure("test.component", exc)
    assert outcome is None
    assert chain.is_terminal_outcome(outcome) is False
    assert chain.get_parked_reason("test.component") is None


def test_alert_once_dedups_on_component_signature_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")
    sent: list[str] = []

    def sender(text: str) -> SimpleNamespace:
        sent.append(text)
        return _ok_sender(text)

    first = chain._alert_once(
        component="c", signature="quota_exhausted", day="2026-08-05", message="m1", sender=sender
    )
    second = chain._alert_once(
        component="c", signature="quota_exhausted", day="2026-08-05", message="m2", sender=sender
    )
    third_next_day = chain._alert_once(
        component="c", signature="quota_exhausted", day="2026-08-06", message="m3", sender=sender
    )

    assert first is True
    assert second is False
    assert third_next_day is True
    assert sent == ["m1", "m3"]


def test_alert_once_does_not_dedupe_a_failed_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed send (missing webhook / timeout / HTTP error) must leave the
    dedup key unwritten, so the next tick retries -- recording an
    undelivered page would silently drop it forever."""
    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")
    attempts: list[str] = []

    def failing_sender(text: str) -> SimpleNamespace:
        attempts.append(text)
        return SimpleNamespace(ok=False, detail="slack webhook error: HTTP 500")

    first = chain._alert_once(
        component="c",
        signature="quota_exhausted",
        day="2026-08-05",
        message="m1",
        sender=failing_sender,
    )
    second = chain._alert_once(
        component="c",
        signature="quota_exhausted",
        day="2026-08-05",
        message="m2",
        sender=failing_sender,
    )

    assert first is False
    assert second is False
    assert attempts == ["m1", "m2"]


def test_classify_chain_error_recognizes_claude_account_terminal_phrasings() -> None:
    """Live-plausible Claude-account phrasings the shared longhaul table has
    no reason to carry (it has no "claude" provider table at all) must still
    classify as TERMINAL through improvement_chain's local, additive-only
    extension -- and it must never contradict a shared-table match for a
    provider that table does cover."""
    from omniagentos.improvement_chain import classify_chain_error

    assert (
        classify_chain_error("claude", "Failed to authenticate: OAuth session expired")
        == "auth_error"
    )
    assert classify_chain_error("claude", "monthly spend limit reached") == "quota_exhausted"
    assert classify_chain_error("claude", "organization suspended") == "auth_error"
    # Isolated sample: standalone suspension text, no compound quota wording.
    assert classify_chain_error("kimi", "insufficient balance") == "quota_exhausted"


def test_run_chain_short_circuits_when_primary_is_parked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _fresh_repo(tmp_path)
    config_path = _config(repo / "configs" / "loop_models.yaml")

    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", repo)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: repo / "var" / "loop-review")
    _ORIGINAL_PARK(
        component="improvement_chain.primary",
        outcome="quota_exhausted",
        detail="pre-seeded",
        now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        sender=_ok_sender,
    )

    def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no provider call may happen while parked")

    with pytest.raises(chain.ChainParked):
        chain.run_chain(
            config_path=config_path,
            artifact_root=repo / "var" / "loop-review",
            json_runner=unreachable,
            plan_editor=unreachable,
            now=lambda: datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
        )


def test_run_chain_short_circuits_when_plan_editor_is_parked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _fresh_repo(tmp_path)
    config_path = _config(repo / "configs" / "loop_models.yaml")

    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", repo)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: repo / "var" / "loop-review")
    _ORIGINAL_PARK(
        component="improvement_chain.plan_editor",
        outcome="auth_error",
        detail="pre-seeded",
        now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        sender=_ok_sender,
    )

    def kimi_only_json(
        stage: Any, prompt: str, schema: dict[str, Any], working_dir: Path, wall_ms: int
    ) -> dict[str, Any]:
        del prompt, schema, working_dir, wall_ms
        assert stage.harness == "cli-kimi"
        return {"draft_plan": "# Draft\n\n- x.", "source_findings": [], "deferred": []}

    def unreachable_editor(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("plan editor must not run while parked")

    with pytest.raises(chain.ChainParked):
        chain.run_chain(
            config_path=config_path,
            artifact_root=repo / "var" / "loop-review",
            json_runner=kimi_only_json,
            plan_editor=unreachable_editor,
            now=lambda: datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
        )


def test_run_chain_short_circuits_when_final_reviewer_is_parked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _fresh_repo(tmp_path)
    config_path = _config(repo / "configs" / "loop_models.yaml")

    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", repo)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: repo / "var" / "loop-review")
    _ORIGINAL_PARK(
        component="improvement_chain.final_reviewer",
        outcome="auth_error",
        detail="pre-seeded",
        now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        sender=_ok_sender,
    )

    def json_runner(
        stage: Any, prompt: str, schema: dict[str, Any], working_dir: Path, wall_ms: int
    ) -> dict[str, Any]:
        del prompt, schema, working_dir, wall_ms
        if stage.harness == "cli-kimi":
            return {"draft_plan": "# Draft\n\n- x.", "source_findings": [], "deferred": []}
        raise AssertionError("final reviewer must not run while parked")

    def editor(stage: Any, prompt: str, workspace: Path, plan_name: str, wall_ms: int) -> str:
        del stage, prompt, wall_ms
        (workspace / plan_name).write_text("# Plan\n\n- edited.\n", encoding="utf-8")
        return "edited"

    with pytest.raises(chain.ChainParked):
        chain.run_chain(
            config_path=config_path,
            artifact_root=repo / "var" / "loop-review",
            json_runner=json_runner,
            plan_editor=editor,
            now=lambda: datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
        )


def test_run_kimi_json_short_circuits_when_already_parked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")
    _ORIGINAL_PARK(
        component="run_kimi_json.primary",
        outcome="quota_exhausted",
        detail="pre-seeded",
        now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        sender=_ok_sender,
    )

    def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("run_stage_json must not be called while parked")

    monkeypatch.setattr(chain, "load_config", unreachable)
    monkeypatch.setattr(chain, "run_stage_json", unreachable)

    assert chain.run_kimi_json("prompt", {}) is None


def test_run_kimi_json_classifies_a_non_stage_failure_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare ``except Exception: return None`` swallow (the real call
    sites -- swarm.optimize/summary -- both wrap this in their own bare
    ``except Exception``) must still classify+park a terminal-looking
    exception before disappearing it, not silently drop it unclassified."""
    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")
    monkeypatch.setattr(chain, "DEFAULT_ARTIFACT_ROOT", tmp_path / "loop-review")

    fake_stage = ModelStage(
        harness="cli-kimi", model="moonshot-ai/kimi-k3", effort=None, can_edit_plans=False
    )
    fake_config = SimpleNamespace(primary=fake_stage)
    monkeypatch.setattr(chain, "load_config", lambda: fake_config)

    def boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("insufficient balance")

    monkeypatch.setattr(chain, "run_stage_json", boom)

    sent: list[str] = []
    monkeypatch.setattr(
        chain,
        "park_chain_failure",
        lambda **kw: _ORIGINAL_PARK(
            **{**kw, "sender": lambda t: (sent.append(t), _ok_sender(t))[1]}
        ),
    )

    assert chain.run_kimi_json("prompt", {}) is None

    parked = chain.get_parked_reason("run_kimi_json.primary")
    assert parked is not None
    assert parked["outcome"] == "quota_exhausted"
    assert len(sent) == 1


def test_parked_state_persists_across_a_fresh_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parked state is read from disk on every call (no in-process cache), so
    it must survive a fresh process -- modeled here with ``importlib.reload``
    to force a brand-new module namespace, the way a new ``fable-curator.sh``
    invocation gets a fresh Python interpreter."""
    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")
    _ORIGINAL_PARK(
        component="improvement_chain.primary",
        outcome="auth_error",
        detail="pre-seeded",
        now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        sender=_ok_sender,
    )

    reloaded = importlib.reload(chain)
    monkeypatch.setattr(reloaded, "ROOT", tmp_path)
    monkeypatch.setattr(reloaded, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")

    parked = reloaded.get_parked_reason("improvement_chain.primary")
    assert parked is not None
    assert parked["outcome"] == "auth_error"


def test_production_parked_path_retries_an_undelivered_alert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-2 blocker 1: park with a failing notifier; the NEXT tick, through
    the REAL production ``_raise_if_parked`` short-circuit (not a direct
    ``_alert_once`` call), with a working notifier, must deliver exactly one
    alert -- retried from the stored outcome/detail -- and make zero further
    provider calls. A third tick, still parked and still working, must not
    re-send (once-per-day dedup applies once a send SUCCEEDS)."""
    repo = _fresh_repo(tmp_path)
    config_path = _config(repo / "configs" / "loop_models.yaml")

    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", repo)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: repo / "var" / "loop-review")

    send_calls: list[str] = []
    delivery_ok = {"value": False}

    def fake_send_slack(text: str, *, webhook_env: str = "OPS_ALERT_SLACK_WEBHOOK_URL") -> Any:
        del webhook_env
        send_calls.append(text)
        return SimpleNamespace(
            ok=delivery_ok["value"], detail="ok" if delivery_ok["value"] else "down"
        )

    monkeypatch.setattr(chain, "send_slack", fake_send_slack)

    provider_calls = 0

    def failing_kimi_json(
        stage: Any, prompt: str, schema: dict[str, Any], working_dir: Path, wall_ms: int
    ) -> dict[str, Any]:
        nonlocal provider_calls
        del prompt, schema, working_dir, wall_ms
        provider_calls += 1
        raise chain.StageFailure(stage, "insufficient balance")

    # Tick 1: the real production failure path parks with a DOWN webhook.
    with pytest.raises(chain.StageFailure):
        chain.run_chain(
            config_path=config_path,
            artifact_root=repo / "var" / "loop-review" / "t1",
            json_runner=failing_kimi_json,
            plan_editor=lambda *a, **k: pytest.fail("unreachable"),
            now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        )
    assert provider_calls == 1
    assert len(send_calls) == 1  # attempted, but not delivered
    assert chain.get_parked_reason("improvement_chain.primary") is not None

    def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no provider call once parked")

    # Tick 2: still parked, webhook now works -> the production short-circuit
    # itself (not a fresh failure) must retry and deliver, with ZERO provider
    # calls.
    delivery_ok["value"] = True
    with pytest.raises(chain.ChainParked):
        chain.run_chain(
            config_path=config_path,
            artifact_root=repo / "var" / "loop-review" / "t2",
            json_runner=unreachable,
            plan_editor=unreachable,
            now=lambda: datetime(2026, 8, 5, 9, 30, tzinfo=UTC),
        )
    assert provider_calls == 1, "tick 2 must make zero provider calls"
    assert len(send_calls) == 2, "tick 2 must retry the undelivered alert"

    # Tick 3: still parked, webhook still works -> once-per-day dedup holds.
    with pytest.raises(chain.ChainParked):
        chain.run_chain(
            config_path=config_path,
            artifact_root=repo / "var" / "loop-review" / "t3",
            json_runner=unreachable,
            plan_editor=unreachable,
            now=lambda: datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
        )
    assert len(send_calls) == 2, "a delivered alert must not re-send the same day"


def test_second_account_stage_invocation_skips_the_parked_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-2 test list: two consecutive account-stage invocations -- the
    second must skip the now-parked account with ZERO provider calls to it,
    while still failing over to the healthy sibling."""
    import omniagentos.improvement_chain as chain
    import omniagentos.routing.config as account_config

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")

    calls_per_account = {"expired": 0, "healthy": 0}

    class FakeAdapter:
        def run(self, agent_input: object) -> AgentResult:
            raise AssertionError(f"unexpected unpooled call: {agent_input}")

        def _run_once(self, agent_input: object, *, env_overrides: dict[str, str]) -> AgentResult:
            del agent_input
            account_id = "expired" if "expired" in env_overrides["CLAUDE_CONFIG_DIR"] else "healthy"
            calls_per_account[account_id] += 1
            if account_id == "expired":
                return AgentResult(
                    status=ResultStatus.ERROR,
                    usage=AgentUsage(wall_ms=1),
                    error="Failed to authenticate: OAuth session expired",
                )
            return AgentResult(
                status=ResultStatus.OK, output_text="ok", usage=AgentUsage(wall_ms=1)
            )

    accounts = [
        Account(id="expired", config_dir="/tmp/expired", priority=0),
        Account(id="healthy", config_dir="/tmp/healthy", priority=1),
    ]
    config = type(
        "AccountConfig",
        (),
        {"providers": {"claude": type("Provider", (), {"accounts": accounts})()}},
    )()
    monkeypatch.setattr(chain, "resolve_adapter", lambda harness: FakeAdapter())
    monkeypatch.setattr(account_config, "load_accounts_config", lambda: config)

    stage = ModelStage(
        harness="cli-claude", model="claude-opus-5", effort="xhigh", can_edit_plans=True
    )

    first = _run_adapter(stage, object())  # type: ignore[arg-type]
    assert first.status == ResultStatus.OK
    assert calls_per_account == {"expired": 1, "healthy": 1}

    second = _run_adapter(stage, object())  # type: ignore[arg-type]
    assert second.status == ResultStatus.OK
    assert calls_per_account == {
        "expired": 1,
        "healthy": 2,
    }, "the parked expired account must receive zero further provider calls"


def test_all_accounts_parked_makes_zero_provider_calls_and_parks_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-2 blocker 2a: once every configured account is parked, the old
    ``adapter.run(agent_input)`` fallback must NOT fire -- zero provider
    calls, and a stage-level park is created."""
    import omniagentos.improvement_chain as chain
    import omniagentos.routing.config as account_config

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")

    class UnreachableAdapter:
        def run(self, agent_input: object) -> AgentResult:
            raise AssertionError("adapter.run must never be reached when all accounts are parked")

        def _run_once(self, agent_input: object, *, env_overrides: dict[str, str]) -> AgentResult:
            raise AssertionError("_run_once must never be reached when all accounts are parked")

    accounts = [
        Account(id="one", config_dir="/tmp/one", priority=0),
        Account(id="two", config_dir="/tmp/two", priority=1),
    ]
    config = type(
        "AccountConfig",
        (),
        {"providers": {"claude": type("Provider", (), {"accounts": accounts})()}},
    )()
    monkeypatch.setattr(chain, "resolve_adapter", lambda harness: UnreachableAdapter())
    monkeypatch.setattr(account_config, "load_accounts_config", lambda: config)

    stage = ModelStage(
        harness="cli-claude", model="claude-opus-5", effort="xhigh", can_edit_plans=True
    )
    for account in accounts:
        chain.park_chain_failure(
            component=chain._account_component(stage, account.id),
            outcome="quota_exhausted",
            detail="pre-seeded",
            now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
            sender=_ok_sender,
        )

    result = _run_adapter(stage, object())  # type: ignore[arg-type]

    assert result.status == ResultStatus.ERROR
    stage_component = chain._stage_component(stage)
    parked = chain.get_parked_reason(stage_component)
    assert parked is not None
    assert parked["outcome"] == "quota_exhausted"


def test_all_accounts_parked_through_fable_gate_makes_zero_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-2 blocker 2b: ``reflection/fable_gate.py`` calls ``run_stage_json``
    directly (ungated at its own call site), but that seam is gated at the
    single lowest entry point (``_run_adapter``). With every configured
    account already parked, the gate's OWN default runner must still make
    ZERO provider calls and degrade the eligible proposal to ``needs_human``
    (its existing fail-closed contract) -- no changes to fable_gate.py
    itself were required or made."""
    import omniagentos.improvement_chain as chain
    import omniagentos.routing.config as account_config
    from omniagentos.db.store import SqliteStore
    from omniagentos.reflection.fable_gate import run_gate

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")

    class CountingAdapter:
        """Round-4 hygiene fix: an AssertionError raised from inside
        _run_adapter is still just an ``Exception`` to fable_gate's own
        broad ``except Exception`` -- it degrades to the SAME
        ``needs_human`` verdict either way, so a raising fake cannot prove
        zero provider calls (the assertion would be silently swallowed).
        Count calls instead and assert the count AFTER run_gate returns,
        outside its exception boundary."""

        def __init__(self) -> None:
            self.calls = 0

        def run(self, agent_input: object) -> AgentResult:
            self.calls += 1
            return AgentResult(
                status=ResultStatus.ERROR, usage=AgentUsage(wall_ms=1), error="unreachable"
            )

        def _run_once(self, agent_input: object, *, env_overrides: dict[str, str]) -> AgentResult:
            self.calls += 1
            return AgentResult(
                status=ResultStatus.ERROR, usage=AgentUsage(wall_ms=1), error="unreachable"
            )

    counting_adapter = CountingAdapter()
    accounts = [Account(id="only", config_dir="/tmp/only", priority=0)]
    account_cfg = type(
        "AccountConfig",
        (),
        {"providers": {"claude": type("Provider", (), {"accounts": accounts})()}},
    )()
    monkeypatch.setattr(chain, "resolve_adapter", lambda harness: counting_adapter)
    monkeypatch.setattr(account_config, "load_accounts_config", lambda: account_cfg)

    real_config = chain.load_config()
    reviewer_stage = real_config.final_reviewer
    assert reviewer_stage.harness == "cli-claude"
    chain.park_chain_failure(
        component=chain._account_component(reviewer_stage, "only"),
        outcome="quota_exhausted",
        detail="pre-seeded",
        now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        sender=_ok_sender,
    )

    db_path = str(tmp_path / "gate.db")
    SqliteStore(db_path)
    proposal_id = "prop-fable-gate-parked"
    from omniagentos.contracts import utc_now_iso

    store = SqliteStore(db_path)
    now = utc_now_iso()
    with store._lock:
        store._connection.execute(
            """
            INSERT INTO reflection_proposals (
                id, kind, target, current, proposed, rationale, evidence_refs_json,
                predicted_impact, risk_class, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                "model_config",
                json.dumps({"file": "configs/modelintel.yaml", "key": "models.gemini.available"}),
                "false",
                "true",
                "test rationale",
                "[]",
                "test impact",
                "low",
                "pending",
                now,
                now,
            ),
        )

    result = run_gate(
        db_path=db_path,
        mode="shadow",
        artifact_root=tmp_path / "fable-gate-artifacts",
    )

    # Asserted OUTSIDE run_gate's own exception boundary: this is the actual
    # proof of zero provider calls, not an inference from the verdict.
    assert counting_adapter.calls == 0, "fable_gate must make zero provider calls while parked"
    assert result.considered == 1
    assert result.verdicts[proposal_id]["verdict"] == "needs_human"
    stage_component = chain._stage_component(reviewer_stage)
    parked = chain.get_parked_reason(stage_component)
    assert parked is not None


def test_a_real_subprocess_restart_refuses_the_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-2 test list: a REAL subprocess restart (spawned interpreter, not
    ``importlib.reload`` in the same process) must read the persisted parked
    state and refuse the provider call -- modeling a fresh ``fable-curator.sh``
    invocation getting a brand-new Python interpreter.

    Round-4 hygiene fix: fully hermetic. Both the parent (via monkeypatch,
    auto-restored) and the child (by explicit assignment right after import)
    redirect ``chain.ROOT`` to THIS TEST'S OWN ``tmp_path`` -- never the real
    worktree's ``var/loop-review/``. The child's Slack webhook env vars are
    stripped so no alert can ever leave this process even if the
    parked-alert retry fires. Nothing is read from or written back to the
    real ``parked.json``/``alerted.json`` at any point, which the final
    assertion below verifies directly."""
    import os
    import subprocess
    import sys

    import omniagentos.improvement_chain as chain

    # Captured BEFORE any monkeypatching: the real on-disk repo root, needed
    # so the CHILD process can import the real ``omniagentos`` package --
    # this is independent of where its state (``chain.ROOT``) points.
    real_repo_root = Path(chain.__file__).resolve().parents[1]
    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")

    component = "test.subprocess_restart_probe"
    chain.park_chain_failure(
        component=component,
        outcome="quota_exhausted",
        detail="subprocess-restart-probe",
        now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        sender=_ok_sender,
    )

    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(real_repo_root)!r})\n"
        "from pathlib import Path as _Path\n"
        "import omniagentos.improvement_chain as _chain\n"
        f"_chain.ROOT = _Path({str(tmp_path)!r})\n"
        f"_chain._loop_review_root = lambda: _Path({str(tmp_path)!r}) / 'var' / 'loop-review'\n"
        f"record = _chain.get_parked_reason({component!r})\n"
        "assert record is not None, 'parked state did not survive a fresh process'\n"
        "assert record['outcome'] == 'quota_exhausted'\n"
        "try:\n"
        f"    _chain._raise_if_parked({component!r})\n"
        "    raise SystemExit('expected ChainParked to be raised')\n"
        "except _chain.ChainParked:\n"
        "    pass\n"
        "print('SUBPROCESS_OK')\n"
    )
    child_env = dict(os.environ)
    child_env.pop("OPS_ALERT_SLACK_WEBHOOK_URL", None)
    child_env.pop("SLACK_WEBHOOK_URL", None)

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=child_env,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "SUBPROCESS_OK" in result.stdout

    # Hermeticity check: this test must never have touched the real
    # worktree's persisted state.
    real_parked_path = real_repo_root / "var" / "loop-review" / "parked.json"
    if real_parked_path.is_file():
        real_state = json.loads(real_parked_path.read_text(encoding="utf-8"))
        assert component not in real_state
    real_alerted_path = real_repo_root / "var" / "loop-review" / "alerted.json"
    if real_alerted_path.is_file():
        real_alerted = json.loads(real_alerted_path.read_text(encoding="utf-8"))
        assert not any(key.startswith(f"{component}:") for key in real_alerted)


def test_concurrent_park_writers_for_different_components_both_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-2 major 3 / test list: two overlapping park writers for
    DIFFERENT components must not lose either update. A ``threading.Barrier``
    (round-4 hygiene fix) makes the overlap DETERMINISTIC -- both threads
    block until both have started, so they enter ``park_chain_failure``
    together rather than relying on OS scheduling luck. A delay is then
    injected inside the locked read-modify-write critical section (after the
    read, before the write) so an UNLOCKED implementation would race and
    clobber one write; ``_with_parked_lock``'s cross-process ``flock`` must
    fully serialize both callers regardless of whether they are threads or
    separate processes (an ``fcntl.flock`` on a stable file mutually
    excludes both)."""
    import threading
    import time

    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")

    barrier = threading.Barrier(2)
    original_read = chain._read_json_state

    def slow_read(path: Path) -> dict[str, Any]:
        result = original_read(path)
        time.sleep(0.05)
        return result

    monkeypatch.setattr(chain, "_read_json_state", slow_read)

    def worker(component: str, outcome: str) -> None:
        barrier.wait(timeout=10)
        chain.park_chain_failure(
            component=component,
            outcome=outcome,
            detail="concurrent-writer",
            now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
            sender=_ok_sender,
        )

    t1 = threading.Thread(target=worker, args=("component.one", "quota_exhausted"))
    t2 = threading.Thread(target=worker, args=("component.two", "auth_error"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not t1.is_alive() and not t2.is_alive()

    one = chain.get_parked_reason("component.one")
    two = chain.get_parked_reason("component.two")
    assert one is not None and one["outcome"] == "quota_exhausted"
    assert two is not None and two["outcome"] == "auth_error"


def test_stage_park_recovers_when_an_account_is_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-4 blocker: park all accounts (stage park gets written), then
    clear ONE account's park. The next invocation must clear the stage
    record (since account state is now successfully inspected and at least
    one account is recovered) and call the provider EXACTLY ONCE -- for the
    recovered account."""
    import omniagentos.improvement_chain as chain
    import omniagentos.routing.config as account_config

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")

    calls: list[str] = []

    class FakeAdapter:
        def run(self, agent_input: object) -> AgentResult:
            raise AssertionError("unpooled call")

        def _run_once(self, agent_input: object, *, env_overrides: dict[str, str]) -> AgentResult:
            del agent_input
            account_id = "one" if "one" in env_overrides["CLAUDE_CONFIG_DIR"] else "two"
            calls.append(account_id)
            return AgentResult(
                status=ResultStatus.OK, output_text="ok", usage=AgentUsage(wall_ms=1)
            )

    accounts = [
        Account(id="one", config_dir="/tmp/one", priority=0),
        Account(id="two", config_dir="/tmp/two", priority=1),
    ]
    config = type(
        "AccountConfig",
        (),
        {"providers": {"claude": type("Provider", (), {"accounts": accounts})()}},
    )()
    monkeypatch.setattr(chain, "resolve_adapter", lambda harness: FakeAdapter())
    monkeypatch.setattr(account_config, "load_accounts_config", lambda: config)

    stage = ModelStage(
        harness="cli-claude", model="claude-opus-5", effort="xhigh", can_edit_plans=True
    )
    for account in accounts:
        chain.park_chain_failure(
            component=chain._account_component(stage, account.id),
            outcome="quota_exhausted",
            detail="pre-seeded",
            now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
            sender=_ok_sender,
        )

    # Every account parked -> zero calls, stage park written.
    first = _run_adapter(stage, object())  # type: ignore[arg-type]
    assert first.status == ResultStatus.ERROR
    assert calls == []
    stage_component = chain._stage_component(stage)
    assert chain.get_parked_reason(stage_component) is not None

    # Recover account "one".
    chain.clear_parked_reason(chain._account_component(stage, "one"))

    # Next invocation: stage record must be cleared, and the provider is
    # called EXACTLY ONCE (for the recovered "one" account).
    second = _run_adapter(stage, object())  # type: ignore[arg-type]
    assert second.status == ResultStatus.OK
    assert calls == ["one"]
    assert chain.get_parked_reason(stage_component) is None


def test_config_load_failure_with_existing_stage_park_makes_zero_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-4 blocker: with a stage park already on record, an accounts
    config that raises on load must return the parked error with ZERO
    provider calls instead of falling through to ``adapter.run()``."""
    import omniagentos.improvement_chain as chain
    import omniagentos.routing.config as account_config

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")

    class UnreachableAdapter:
        def run(self, agent_input: object) -> AgentResult:
            raise AssertionError("adapter.run must never be reached while the stage is parked")

        def _run_once(self, agent_input: object, *, env_overrides: dict[str, str]) -> AgentResult:
            raise AssertionError("_run_once must never be reached while the stage is parked")

    def boom() -> Any:
        raise RuntimeError("accounts config unreadable")

    monkeypatch.setattr(chain, "resolve_adapter", lambda harness: UnreachableAdapter())
    monkeypatch.setattr(account_config, "load_accounts_config", boom)

    stage = ModelStage(
        harness="cli-claude", model="claude-fable-5", effort="high", can_edit_plans=False
    )
    stage_component = chain._stage_component(stage)
    chain.park_chain_failure(
        component=stage_component,
        outcome="auth_error",
        detail="pre-seeded",
        now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        sender=_ok_sender,
    )

    result = _run_adapter(stage, object())  # type: ignore[arg-type]

    assert result.status == ResultStatus.ERROR
    assert chain.get_parked_reason(stage_component) is not None


def test_all_accounts_parked_result_selects_the_most_recently_parked_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-4 polish: ``records[-1]`` (configured-account order) must
    actually be the MOST RECENTLY parked account, not merely the last one
    iterated. Park "two" (config order 2nd) FIRST and "one" (config order
    1st) SECOND -- the stage record must carry "one"'s outcome/detail."""
    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")

    stage = ModelStage(
        harness="cli-claude", model="claude-opus-5", effort="xhigh", can_edit_plans=True
    )
    accounts = [
        Account(id="one", config_dir="/tmp/one", priority=0),
        Account(id="two", config_dir="/tmp/two", priority=1),
    ]

    chain.park_chain_failure(
        component=chain._account_component(stage, "two"),
        outcome="auth_error",
        detail="two-parked-first",
        now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        sender=_ok_sender,
    )
    chain.park_chain_failure(
        component=chain._account_component(stage, "one"),
        outcome="quota_exhausted",
        detail="one-parked-second-and-is-most-recent",
        now=lambda: datetime(2026, 8, 5, 9, 30, tzinfo=UTC),
        sender=_ok_sender,
    )

    result = chain._all_accounts_parked_result(stage, accounts)  # type: ignore[arg-type]

    assert result.status == ResultStatus.ERROR
    stage_component = chain._stage_component(stage)
    parked = chain.get_parked_reason(stage_component)
    assert parked is not None
    assert parked["outcome"] == "quota_exhausted"
    assert "one-parked-second-and-is-most-recent" in parked["detail"]


def _build_refailing_recovery_fixture(
    chain: Any, account_config: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModelStage, str, Any]:
    """Shared setup for both recovery-then-refail regression tests below:
    two accounts and a stage all pre-parked, account "one" then recovered
    (cleared) so a recovery attempt is a live candidate; the fake adapter
    lets ONLY "one" through and re-fails it terminal."""

    class ReFailingAdapter:
        def __init__(self) -> None:
            self.run_calls = 0

        def run(self, agent_input: object) -> AgentResult:
            self.run_calls += 1
            return AgentResult(
                status=ResultStatus.ERROR, usage=AgentUsage(wall_ms=1), error="unreachable"
            )

        def _run_once(self, agent_input: object, *, env_overrides: dict[str, str]) -> AgentResult:
            del agent_input
            assert "one" in env_overrides["CLAUDE_CONFIG_DIR"], (
                "only the recovered account may be retried"
            )
            return AgentResult(
                status=ResultStatus.ERROR,
                usage=AgentUsage(wall_ms=1),
                error="monthly spend limit reached",  # terminal (quota_exhausted) again
            )

    adapter_instance = ReFailingAdapter()
    accounts = [
        Account(id="one", config_dir="/tmp/one", priority=0),
        Account(id="two", config_dir="/tmp/two", priority=1),
    ]
    good_config = type(
        "AccountConfig",
        (),
        {"providers": {"claude": type("Provider", (), {"accounts": accounts})()}},
    )()
    monkeypatch.setattr(chain, "resolve_adapter", lambda harness: adapter_instance)
    monkeypatch.setattr(account_config, "load_accounts_config", lambda: good_config)

    stage = ModelStage(
        harness="cli-claude", model="claude-opus-5", effort="xhigh", can_edit_plans=True
    )
    stage_component = chain._stage_component(stage)

    for account in accounts:
        chain.park_chain_failure(
            component=chain._account_component(stage, account.id),
            outcome="quota_exhausted",
            detail="pre-seeded",
            now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
            sender=_ok_sender,
        )
    chain.park_chain_failure(
        component=stage_component,
        outcome="quota_exhausted",
        detail="pre-seeded stage park",
        now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        sender=_ok_sender,
    )
    chain.clear_parked_reason(chain._account_component(stage, "one"))

    return stage, stage_component, adapter_instance


def test_stage_park_is_recreated_after_a_recovered_account_re_fails_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-5 blocker (Sol's precise trace), narrowed to ONE call: recovery
    lets the cleared account through for a real retry; if THAT account then
    fails terminal AGAIN, every account is parked once more, and the stage
    record must be present regardless of the (removed) `attempted` flag's
    old gating."""
    import omniagentos.improvement_chain as chain
    import omniagentos.routing.config as account_config

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")
    stage, stage_component, adapter_instance = _build_refailing_recovery_fixture(
        chain, account_config, monkeypatch
    )

    recovery_result = _run_adapter(stage, object())  # type: ignore[arg-type]

    assert recovery_result.status == ResultStatus.ERROR
    assert adapter_instance.run_calls == 0, (
        "the healthy-config path must never fall back to adapter.run()"
    )
    assert chain.get_parked_reason(chain._account_component(stage, "one")) is not None
    assert chain.get_parked_reason(stage_component) is not None, (
        "the stage park must be present after the recovered account re-fails terminal"
    )


def test_zero_provider_calls_across_recovery_refail_then_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-6 hygiene fix (per review): pins Sol's COMPLETE failing
    sequence by running BOTH calls before asserting anything about provider
    calls -- the previous version asserted the intermediate stage-record
    state first, which failed fast against the round-4 parent and never
    actually executed the second (config-error) call, so it did not
    genuinely prove the "zero calls across two calls" claim. Here the only
    assertion on ``run_calls`` comes AFTER both calls have run."""
    import omniagentos.improvement_chain as chain
    import omniagentos.routing.config as account_config

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")
    stage, _stage_component, adapter_instance = _build_refailing_recovery_fixture(
        chain, account_config, monkeypatch
    )

    # Call 1: recovery attempt, "one" retried for real, fails terminal
    # again, re-parks. (No assertions here -- see the narrower test above
    # for that; this test exists purely to pin the two-call sequence.)
    _run_adapter(stage, object())  # type: ignore[arg-type]

    # Call 2: accounts config is now unreadable.
    def boom() -> Any:
        raise RuntimeError("accounts config unreadable")

    monkeypatch.setattr(account_config, "load_accounts_config", boom)
    _run_adapter(stage, object())  # type: ignore[arg-type]

    assert adapter_instance.run_calls == 0, (
        "zero adapter.run() calls across the full recovery-refail-then-config-error sequence"
    )


def test_concurrent_config_error_during_an_in_flight_recovery_makes_zero_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-6 new test (per review): while a recovery candidate exists --
    the stage is parked, one account has been cleared, but the recovery
    call has not yet run/returned -- a CONCURRENT caller whose accounts
    config raises must still make ZERO provider calls. Under the retain
    design the stage record was never deleted in anticipation of the
    recovery outcome, so this is safe by construction: there is no window
    where "some account might be unparked" implies "the stage record is
    momentarily absent"."""
    import omniagentos.improvement_chain as chain
    import omniagentos.routing.config as account_config

    monkeypatch.setattr(chain, "ROOT", tmp_path)
    monkeypatch.setattr(chain, "_loop_review_root", lambda: tmp_path / "var" / "loop-review")

    accounts = [
        Account(id="one", config_dir="/tmp/one", priority=0),
        Account(id="two", config_dir="/tmp/two", priority=1),
    ]
    stage = ModelStage(
        harness="cli-claude", model="claude-fable-5", effort="high", can_edit_plans=False
    )
    stage_component = chain._stage_component(stage)

    for account in accounts:
        chain.park_chain_failure(
            component=chain._account_component(stage, account.id),
            outcome="auth_error",
            detail="pre-seeded",
            now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
            sender=_ok_sender,
        )
    chain.park_chain_failure(
        component=stage_component,
        outcome="auth_error",
        detail="pre-seeded stage park",
        now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        sender=_ok_sender,
    )

    # Simulate "recovery is in flight": account "one" has been cleared (a
    # recovery candidate now exists -- ``_maybe_recovery_candidate`` would
    # return True) but the actual recovery call has not run yet. The stage
    # record must still be present RIGHT NOW, because the retain design
    # never deletes it speculatively.
    chain.clear_parked_reason(chain._account_component(stage, "one"))
    assert chain._maybe_recovery_candidate(
        stage_component,
        [chain._account_component(stage, a.id) for a in accounts],
    ), "a recovery candidate must exist for this test to be meaningful"
    assert chain.get_parked_reason(stage_component) is not None, (
        "the stage record must still be present while recovery is merely a candidate, not yet confirmed"
    )

    # A CONCURRENT caller whose accounts config raises must make zero calls.
    class UnreachableAdapter:
        def run(self, agent_input: object) -> AgentResult:
            raise AssertionError("adapter.run must never be reached while the stage is parked")

        def _run_once(self, agent_input: object, *, env_overrides: dict[str, str]) -> AgentResult:
            raise AssertionError("_run_once must never be reached while the stage is parked")

    def boom() -> Any:
        raise RuntimeError("accounts config unreadable")

    monkeypatch.setattr(chain, "resolve_adapter", lambda harness: UnreachableAdapter())
    monkeypatch.setattr(account_config, "load_accounts_config", boom)

    result = _run_adapter(stage, object())  # type: ignore[arg-type]
    assert result.status == ResultStatus.ERROR
    assert chain.get_parked_reason(stage_component) is not None


def _assert_all_parked_implies_stage_record(
    chain: Any, stage: ModelStage, account_ids: list[str]
) -> None:
    """Invariant protected by the stage-park retain/clear design: if every
    configured account is parked, the stage park record MUST be present."""
    account_components = [chain._account_component(stage, account_id) for account_id in account_ids]
    stage_component = chain._stage_component(stage)
    every_account_parked = all(
        chain.get_parked_reason(component) is not None for component in account_components
    )
    if every_account_parked:
        assert chain.get_parked_reason(stage_component) is not None, (
            "invariant violated: every account is parked but stage park record is absent"
        )


def test_overlapping_recovery_outcomes_preserve_stage_park_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-7: two real concurrent recovery callers must not leave every
    account parked while the stage record is absent.

    Setup: accounts one+two and the stage are pre-parked; account "one" is
    then cleared so both callers observe a recovery candidate and both
    enter ``run_once`` for that same unparked account. A ``threading.Barrier``
    holds them inside ``_run_once`` until both have arrived; events then
    force BOTH completion orderings deterministically:

    * terminal-first: terminal parks the final account (and finishes,
      recreating/retaining the stage); OK then attempts the stage clear.
      Against the old unconditional ``clear_parked_reason(stage)`` this
      ordering deletes the stage AFTER every account is parked -- the
      permanent gap. Against the atomic re-verify-then-delete, the clear
      sees every account parked in the same locked snapshot and refuses.
    * ok-first: OK clears while the account is still unparked (legitimate);
      terminal then parks the final account and recreates the stage at
      loop end. Invariant holds at exit either way.

    After each ordering, a follow-up config-error call must make ZERO
    ``adapter.run()`` provider calls when the stage is (or must be) parked.
    """
    import threading
    import time

    import omniagentos.improvement_chain as chain
    import omniagentos.routing.config as account_config

    account_ids = ["one", "two"]

    def _seed_recovery_candidate() -> ModelStage:
        stage = ModelStage(
            harness="cli-claude", model="claude-opus-5", effort="xhigh", can_edit_plans=True
        )
        for account_id in account_ids:
            chain.park_chain_failure(
                component=chain._account_component(stage, account_id),
                outcome="quota_exhausted",
                detail="pre-seeded",
                now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
                sender=_ok_sender,
            )
        chain.park_chain_failure(
            component=chain._stage_component(stage),
            outcome="quota_exhausted",
            detail="pre-seeded stage park",
            now=lambda: datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
            sender=_ok_sender,
        )
        chain.clear_parked_reason(chain._account_component(stage, "one"))
        return stage

    def _run_ordering(terminal_first: bool) -> None:
        # Fresh parked.json per ordering so residual state cannot bleed.
        monkeypatch.setattr(
            chain, "ROOT", tmp_path / ("term_first" if terminal_first else "ok_first")
        )
        stage = _seed_recovery_candidate()
        stage_component = chain._stage_component(stage)
        account_components = [
            chain._account_component(stage, account_id) for account_id in account_ids
        ]
        assert chain._maybe_recovery_candidate(stage_component, account_components)

        # Barrier of 3: both recovery workers + the main thread. Main waits
        # until BOTH workers are inside ``_run_once`` (past the unparked
        # account check, about to produce a result), then forces completion
        # order via Events. This is genuine multi-threaded interleaving,
        # not single-threaded scripted sequencing.
        both_in_run_once = threading.Barrier(3)
        release_terminal = threading.Event()
        release_ok = threading.Event()
        terminal_done = threading.Event()
        ok_done = threading.Event()
        errors: list[BaseException] = []

        class RacingAdapter:
            def run(self, agent_input: object) -> AgentResult:
                return AgentResult(
                    status=ResultStatus.ERROR,
                    usage=AgentUsage(wall_ms=1),
                    error="unreachable-fallback",
                )

            def _run_once(
                self, agent_input: object, *, env_overrides: dict[str, str]
            ) -> AgentResult:
                del agent_input
                # Both recovery callers must only attempt the recovered account.
                assert "one" in env_overrides["CLAUDE_CONFIG_DIR"]
                role = threading.current_thread().name
                both_in_run_once.wait(timeout=10)
                if role == "terminal-recovery":
                    assert release_terminal.wait(timeout=10), "terminal release timed out"
                    return AgentResult(
                        status=ResultStatus.ERROR,
                        usage=AgentUsage(wall_ms=1),
                        error="monthly spend limit reached",  # terminal: quota_exhausted
                    )
                assert role == "ok-recovery"
                assert release_ok.wait(timeout=10), "ok release timed out"
                return AgentResult(
                    status=ResultStatus.OK,
                    output_text="ok",
                    usage=AgentUsage(wall_ms=1),
                )

        accounts = [
            Account(id="one", config_dir="/tmp/one", priority=0),
            Account(id="two", config_dir="/tmp/two", priority=1),
        ]
        good_config = type(
            "AccountConfig",
            (),
            {"providers": {"claude": type("Provider", (), {"accounts": accounts})()}},
        )()
        monkeypatch.setattr(chain, "resolve_adapter", lambda harness: RacingAdapter())
        monkeypatch.setattr(account_config, "load_accounts_config", lambda: good_config)
        monkeypatch.setattr(chain, "_alert_once", lambda **kwargs: True)

        def terminal_worker() -> None:
            try:
                _run_adapter(stage, object())  # type: ignore[arg-type]
            except BaseException as exc:  # noqa: BLE001 -- surface to main thread
                errors.append(exc)
            finally:
                terminal_done.set()

        def ok_worker() -> None:
            try:
                _run_adapter(stage, object())  # type: ignore[arg-type]
            except BaseException as exc:  # noqa: BLE001 -- surface to main thread
                errors.append(exc)
            finally:
                ok_done.set()

        t_terminal = threading.Thread(target=terminal_worker, name="terminal-recovery")
        t_ok = threading.Thread(target=ok_worker, name="ok-recovery")
        t_terminal.start()
        t_ok.start()

        # Block until both workers have entered ``_run_once`` together.
        both_in_run_once.wait(timeout=10)

        if terminal_first:
            # Force: terminal parks final account + finishes (stage retained/
            # recreated), THEN ok attempts stage clear.
            release_terminal.set()
            assert terminal_done.wait(timeout=10), "terminal worker did not finish"
            # Account must be parked before the OK clear races it -- this is
            # the ordering that permanently breaks the invariant under the
            # old unconditional clear.
            deadline = time.time() + 5
            while time.time() < deadline:
                if chain.get_parked_reason(chain._account_component(stage, "one")) is not None:
                    break
                time.sleep(0.01)
            assert chain.get_parked_reason(chain._account_component(stage, "one")) is not None
            release_ok.set()
            assert ok_done.wait(timeout=10), "ok worker did not finish"
        else:
            # Force: ok clear first (account still unparked -- legitimate),
            # THEN terminal parks the final account and recreates stage.
            release_ok.set()
            assert ok_done.wait(timeout=10), "ok worker did not finish"
            release_terminal.set()
            assert terminal_done.wait(timeout=10), "terminal worker did not finish"

        t_terminal.join(timeout=5)
        t_ok.join(timeout=5)
        assert not t_terminal.is_alive() and not t_ok.is_alive()
        assert not errors, f"worker errors: {errors!r}"

        # Core assertion: the invariant itself, not which recreation path fired.
        _assert_all_parked_implies_stage_record(chain, stage, account_ids)

        # Follow-up config-error / uninspectable-accounts path must make zero
        # provider calls when the stage is (or must be) still parked.
        class CountingFallbackAdapter:
            def __init__(self) -> None:
                self.run_calls = 0

            def run(self, agent_input: object) -> AgentResult:
                self.run_calls += 1
                return AgentResult(
                    status=ResultStatus.ERROR,
                    usage=AgentUsage(wall_ms=1),
                    error="must-not-be-called",
                )

            def _run_once(
                self, agent_input: object, *, env_overrides: dict[str, str]
            ) -> AgentResult:
                raise AssertionError("_run_once must not run on config-error fallback")

        fallback = CountingFallbackAdapter()

        def boom() -> Any:
            raise RuntimeError("accounts config unreadable")

        monkeypatch.setattr(chain, "resolve_adapter", lambda harness: fallback)
        monkeypatch.setattr(account_config, "load_accounts_config", boom)
        config_error_result = _run_adapter(stage, object())  # type: ignore[arg-type]
        assert config_error_result.status == ResultStatus.ERROR
        assert fallback.run_calls == 0, (
            "config-error fallback must make zero adapter.run() calls when "
            "the stage is parked per the all-accounts-parked invariant"
        )
        _assert_all_parked_implies_stage_record(chain, stage, account_ids)

    _run_ordering(terminal_first=True)
    _run_ordering(terminal_first=False)


# ---------------------------------------------------------------------------
# R0-2 lane B — cross-lineage proposer fallback and honest classification.
#
# The proposer was a SINGLE-VENDOR dependency: configs/loop_models.yaml pins the
# Moonshot kimi-k3 alias whose provider credential is commented out for the
# 2026-08-05 billing pause, and ``run_kimi_json`` collapsed park / StageFailure
# / sandbox refusal / adapter-resolution failure alike to ``None``.  11 of the
# 12 genuine reflection failures are that one seam.
# ---------------------------------------------------------------------------


def _stage(harness: str, model: str) -> ModelStage:
    return ModelStage(harness=harness, model=model, effort=None, can_edit_plans=False)


def _stage_failure(harness: str, model: str, detail: str) -> Exception:
    """Build a StageFailure from the LIVE module object.

    ``test_parked_state_persists_across_a_fresh_process`` reloads
    ``omniagentos.improvement_chain``, which rebinds ``StageFailure`` to a NEW
    class object.  An instance built from this module's import-time binding
    would then not be caught by the reloaded module's ``except StageFailure``,
    and the test would silently exercise the adapter-error path instead.
    """
    import omniagentos.improvement_chain as chain

    return chain.StageFailure(_stage(harness, model), detail)


def test_config_parses_an_optional_cross_lineage_fallback(tmp_path: Path) -> None:
    config_path = _config(tmp_path / "models.yaml")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["primary_fallback"] = {
        "harness": "cli-codex",
        "model": "gpt-5.6-terra",
        "effort": None,
        "can_edit_plans": False,
    }
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    config = load_config(config_path, root=tmp_path)
    assert config.primary_fallback is not None
    assert config.primary_fallback.harness == "cli-codex"


def test_config_absent_fallback_is_none(tmp_path: Path) -> None:
    """The key is OPTIONAL: an older config must still load."""
    config = load_config(_config(tmp_path / "models.yaml"), root=tmp_path)
    assert config.primary_fallback is None


def test_config_rejects_a_same_lineage_fallback(tmp_path: Path) -> None:
    """A 'fallback' on the same provider is not a fallback — it is the same
    outage twice.  The whole point of item 4 is a SECOND LINEAGE."""
    config_path = _config(tmp_path / "models.yaml")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["primary_fallback"] = {
        "harness": "cli-kimi",
        "model": "moonshot-ai/kimi-k3",
        "effort": None,
        "can_edit_plans": False,
    }
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="different lineage"):
        load_config(config_path, root=tmp_path)


def test_config_rejects_a_writable_fallback(tmp_path: Path) -> None:
    config_path = _config(tmp_path / "models.yaml")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["primary_fallback"] = {
        "harness": "cli-codex",
        "model": "gpt-5.6-terra",
        "effort": None,
        "can_edit_plans": True,
    }
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="read-only"):
        load_config(config_path, root=tmp_path)


def test_shipped_loop_models_config_carries_a_non_kimi_fallback() -> None:
    """The SHIPPED config — not a fixture — must not be single-vendor.

    OPERATOR CONSTRAINT (owner spec, carried verbatim): the fallback must be a
    non-Kimi lineage; the Moonshot key stays un-restored without the operator's approval.
    """
    config = load_config()
    assert config.primary_fallback is not None, "the proposer must not be single-vendor"
    assert config.primary_fallback.harness != config.primary.harness
    assert "kimi" not in config.primary_fallback.harness
    assert "moonshot" not in config.primary_fallback.model.lower()


def _result_for(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[ModelStage]:
    """Drive ``run_kimi_json_result`` with a scripted per-stage outcome list."""
    import omniagentos.improvement_chain as chain

    called: list[ModelStage] = []
    queue = list(outcomes)

    def _fake_run_stage_json(stage, prompt, schema, working_dir, wall_ms):  # type: ignore[no-untyped-def]
        called.append(stage)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(chain, "run_stage_json", _fake_run_stage_json)
    monkeypatch.setattr(chain, "get_parked_reason", lambda component: None)
    monkeypatch.setattr(chain, "park_chain_failure", lambda **kwargs: {})
    return called


def test_run_kimi_json_result_propagates_the_classified_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal auth failure comes back CLASSIFIED, not as a bare ``None``."""
    import omniagentos.improvement_chain as chain

    _result_for(
        monkeypatch,
        [_stage_failure("cli-kimi", "moonshot-ai/kimi-k3", "invalid api key")],
    )
    result = chain.run_kimi_json_result("p", {"type": "object"})
    assert result.ok is False
    assert result.outcome == "auth_error"
    assert result.failure_kind == "stage_failure"
    assert "invalid api key" in result.describe()
    # Backwards compatibility: the legacy seam still returns None.
    assert result.output is None


def test_run_kimi_json_result_classifies_a_sandbox_refusal_distinctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sandbox refusal is an ADAPTER error — it must not read as a spawn failure."""
    import omniagentos.improvement_chain as chain

    _result_for(monkeypatch, [RuntimeError("Kimi loop stage refused: outer macOS sandbox")])
    result = chain.run_kimi_json_result("p", {"type": "object"})
    assert result.failure_kind == "adapter_error"
    assert "sandbox" in (result.describe() or "")


def test_run_kimi_json_result_reports_a_park_as_parked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omniagentos.improvement_chain as chain

    monkeypatch.setattr(
        chain,
        "get_parked_reason",
        lambda component: {"outcome": "quota_exhausted", "detail": "insufficient balance"},
    )
    monkeypatch.setattr(chain, "_retry_parked_alert", lambda *a, **k: False)
    result = chain.run_kimi_json_result("p", {"type": "object"})
    assert result.failure_kind == "parked"
    assert result.outcome == "quota_exhausted"


def test_fallback_is_opt_in_and_reaches_a_second_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default OFF (the shared seam's other callers keep their shape); the
    reflection proposer opts in and the second lineage answers."""
    import omniagentos.improvement_chain as chain

    # Primary is now the ultra-cheap OpenRouter proposer (SI-loop revival
    # 2026-08-12); the fallback is still a second (Codex) lineage.
    primary_failure = _stage_failure("api-openrouter", "qwen/qwen3.7-flash", "rate limited")

    called = _result_for(monkeypatch, [primary_failure])
    assert chain.run_kimi_json_result("p", {"type": "object"}).ok is False
    assert len(called) == 1, "fallback must NOT fire unless opted into"

    called = _result_for(monkeypatch, [primary_failure, {"proposals": []}])
    result = chain.run_kimi_json_result("p", {"type": "object"}, allow_fallback=True)
    assert result.ok is True
    assert result.used_fallback is True
    assert [stage.harness for stage in called] == ["api-openrouter", "cli-codex"]


def test_both_lineage_failures_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither failure may hide the other — the record names both attempts."""
    import omniagentos.improvement_chain as chain

    _result_for(
        monkeypatch,
        [
            _stage_failure("cli-kimi", "moonshot-ai/kimi-k3", "invalid api key"),
            _stage_failure("cli-codex", "gpt-5.6-terra", "quota exceeded"),
        ],
    )
    result = chain.run_kimi_json_result("p", {"type": "object"}, allow_fallback=True)
    assert result.ok is False
    assert len(result.attempts) == 2
    described = result.describe()
    assert "auth_error" in described and "quota_exhausted" in described
    # The FIRST classified cause is the headline: it is the one that must be
    # remedied, and it is the one the old code reported as "spawn/CLI failure".
    assert result.outcome == "auth_error"


def test_run_kimi_json_legacy_seam_still_returns_output_or_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """swarm.optimize / swarm.summary bind ``run_kimi_json`` — do not break them."""
    import omniagentos.improvement_chain as chain

    _result_for(monkeypatch, [{"proposals": [1]}])
    assert chain.run_kimi_json("p", {"type": "object"}) == {"proposals": [1]}

    _result_for(monkeypatch, [_stage_failure("cli-kimi", "k", "invalid api key")])
    assert chain.run_kimi_json("p", {"type": "object"}) is None


@pytest.mark.parametrize(
    "harness,model",
    [
        ("api-kimi-k3", "kimi-k3"),  # registered API-tier Kimi adapter
        ("cli-kimi", "moonshot-ai/kimi-k3"),  # the literal same harness
        ("api-openrouter", "moonshotai/kimi-k3"),  # same vendor behind a router
    ],
)
def test_config_rejects_every_same_lineage_fallback(
    tmp_path: Path, harness: str, model: str
) -> None:
    """Round-1 critic finding (MAJOR): the guard only knew the five ``cli-*``
    harness keys, so ``api-kimi-k3`` compared UNEQUAL to ``kimi`` and a Kimi
    fallback for a Kimi primary would have been accepted -- the same outage
    twice, and a defeat of the operator constraint."""
    config_path = _config(tmp_path / "models.yaml")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["primary"]["model"] = "moonshot-ai/kimi-k3"
    data["primary_fallback"] = {
        "harness": harness,
        "model": model,
        "effort": None,
        "can_edit_plans": False,
    }
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="different lineage"):
        load_config(config_path, root=tmp_path)


@pytest.mark.parametrize(
    "harness,model",
    [
        ("cli-codex", "community/octopus-v2"),  # contains "opus" but is not Claude
        ("cli-grok", "grok-4.5"),
        ("cli-gemini", "gemini-3.6-pro"),
    ],
)
def test_config_accepts_genuinely_cross_lineage_fallbacks(
    tmp_path: Path, harness: str, model: str
) -> None:
    """Round-2 critic finding (MINOR): substring matching read ``octopus-v2`` as
    Claude and would have REFUSED a valid fallback. Tokens are matched on
    punctuation boundaries now."""
    config_path = _config(tmp_path / "models.yaml")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["primary_fallback"] = {
        "harness": harness,
        "model": model,
        "effort": None,
        "can_edit_plans": False,
    }
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_config(config_path, root=tmp_path).primary_fallback is not None


def test_openai_reasoning_aliases_resolve_to_the_codex_lineage(tmp_path: Path) -> None:
    """``o3`` is Codex lineage elsewhere in this repo; a Codex primary must not
    accept it as a 'different lineage' fallback."""
    config_path = _config(tmp_path / "models.yaml")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    data["primary"] = {
        "harness": "cli-codex",
        "model": "gpt-5.6-sol",
        "effort": None,
        "can_edit_plans": False,
    }
    data["primary_fallback"] = {
        "harness": "api-openrouter",
        "model": "o3",
        "effort": None,
        "can_edit_plans": False,
    }
    config_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="different lineage"):
        load_config(config_path, root=tmp_path)


@pytest.mark.parametrize(
    "harness,model,expected",
    [
        ("cli-codex", "community/octopus-v2", "codex"),  # "opus" is a substring, not a token
        ("cli-codex", "gpt-5.6-terra", "codex"),
        ("cli-claude", "claude-opus-5", "claude"),
        ("cli-claude", "fable", "claude"),
        ("api-kimi-k3", "kimi-k3", "kimi"),
        ("api-openrouter", "moonshotai/kimi-k3", "kimi"),
        ("api-openrouter", "o3", "codex"),
        ("cli-grok", "grok-4.5", "grok"),
    ],
)
def test_lineage_resolution_is_token_bounded(harness: str, model: str, expected: str) -> None:
    """Round-3 critic finding (MINOR): the octopus regression did not constrain
    the bug it named — both old and new code merely differed from the fixture's
    Kimi primary. This pins the resolver's output directly."""
    from omniagentos.improvement_chain import _lineage_for_stage

    stage = ModelStage(harness=harness, model=model, effort=None, can_edit_plans=False)
    assert _lineage_for_stage(stage) == expected
