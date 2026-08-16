"""SI-loop revival (operator approval 2026-08-12): free/cheap-model proposer,
the two previously-unregistered improvement jobs, and the $50/UTC-day OpenRouter
hard cap.

These pin the wiring the revival depends on:

* ``configs/loop_models.yaml`` loads with a FREE OpenRouter primary and a
  DIFFERENT-lineage read-only fallback, and ``load_config`` still REFUSES a
  same-lineage fallback (the one-vendor-outage guard).
* ``omniagentos.improve.dispatcher`` and ``omniagentos.lab.jobs`` are registered
  in ``BUILTIN_JOBS`` and route through ``builtin_for``.
* The revived loop's paid model spend is bounded at $50/UTC-day: the shipped
  spend-caps declare an enabled ``openrouter`` provider capped at $50, and every
  model the last-resort OpenRouter rung can try is priced there (so the guard
  never refuses a listed candidate for a missing pricing row).
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from omniagentos.improvement_chain import (
    DEFAULT_CONFIG_PATH,
    _lineage_for_stage,
    load_config,
)

# --- (a) loop_models: free primary + cross-lineage fallback ----------------


def test_shipped_loop_models_uses_cheap_primary_and_cross_lineage_fallback() -> None:
    config = load_config()

    # Primary is the ultra-cheap OpenRouter proposer, read-only.
    assert config.primary.harness == "api-openrouter"
    assert config.primary.model == "qwen/qwen3.7-flash"
    assert config.primary.can_edit_plans is False
    # The paused Moonshot seam must NOT be restored.
    assert "kimi" not in config.primary.model.lower()
    assert "moonshot" not in config.primary.model.lower()

    # A real, read-only, DIFFERENT-lineage fallback exists.
    assert config.primary_fallback is not None
    assert config.primary_fallback.can_edit_plans is False
    primary_lineage = _lineage_for_stage(config.primary)
    fallback_lineage = _lineage_for_stage(config.primary_fallback)
    assert primary_lineage == "qwen"
    assert fallback_lineage != primary_lineage


def test_load_config_refuses_a_same_lineage_fallback(tmp_path: Path) -> None:
    """The one-vendor-outage guard: primary and fallback may not share a lineage."""
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    # Point the fallback at the SAME lineage as the (qwen) primary.
    raw = copy.deepcopy(raw)
    raw["primary_fallback"] = {
        "harness": "api-openrouter",
        "model": "qwen/qwen3.7-max",
        "effort": None,
        "can_edit_plans": False,
    }
    config_path = tmp_path / "loop_models.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="different lineage"):
        # root points at the real repo so plan.target still resolves.
        load_config(config_path, root=DEFAULT_CONFIG_PATH.parents[1])


# --- (b) BUILTIN_JOBS registration -----------------------------------------


def test_improve_and_lab_jobs_are_registered() -> None:
    from omniagentos.scheduler.builtin_jobs import BUILTIN_JOBS, builtin_for

    assert "omniagentos.improve.dispatcher" in BUILTIN_JOBS
    assert "omniagentos.lab.jobs" in BUILTIN_JOBS

    for module in ("omniagentos.improve.dispatcher", "omniagentos.lab.jobs"):
        template = {"input": {"module": module}}
        assert callable(builtin_for(template)), module


# --- (c) $50/UTC-day OpenRouter cap ----------------------------------------


def _openrouter_cap():
    from omniagentos.adapters.spend_guard import SpendGuard

    return SpendGuard()._load_config().providers.get("openrouter")


def test_openrouter_daily_cap_resolves_to_fifty_usd() -> None:
    from omniagentos.contracts import NANO_USD_SCALE

    provider = _openrouter_cap()
    assert provider is not None, "openrouter provider must be enabled"
    assert provider.daily_cap_usd_nanos == 50 * NANO_USD_SCALE


def test_every_openrouter_candidate_is_priced_and_has_a_lineage() -> None:
    from omniagentos.routing.api_policy import (
        LINEAGE_UNKNOWN,
        model_lineage,
        openrouter_models,
    )

    provider = _openrouter_cap()
    assert provider is not None
    candidates = {*openrouter_models(), "google/gemma-4-31b-it:free"}
    for model in candidates:
        # A missing pricing row would make the guard REFUSE the candidate
        # (unknown_model) on the last-resort rung — the exact regression this
        # test exists to prevent when someone adds to the allow-list.
        assert model in provider.models, f"{model} has no openrouter pricing row"
        assert model_lineage(model) != LINEAGE_UNKNOWN, model


def test_openrouter_adapter_routes_every_call_through_the_spend_guard() -> None:
    from omniagentos.adapters.openrouter import OpenRouterAdapter

    adapter = OpenRouterAdapter()
    # Non-None billing provider == the spend-cap preflight runs on every call.
    assert adapter.spend_guard_provider("google/gemma-4-31b-it:free") == "openrouter"
    assert adapter.spend_guard_provider("deepseek/deepseek-v4-pro") == "openrouter"


# --- F1: the cap can never be EXCEEDED during settlement -------------------


def _seed_prior_openrouter_spend(store, usd: str, *, day: str) -> None:
    now = f"{day}T12:00:00Z"
    store.record_provider_call(
        {
            "call_id": "prior-openrouter-spend",
            "request_id": "prior-request",
            "execution_id": "prior-execution",
            "stage": "worker",
            "provider": "openrouter",
            "transport": "http",
            "requested_model": "deepseek/deepseek-v4-pro",
            "effective_model": "deepseek/deepseek-v4-pro",
            "model_lineage": "deepseek",
            "billing_provider": "openrouter",
            "adapter_key": "openrouter",
            "request_state": "sent",
            "provider_outcome": "completed",
            "cost_usd_decimal": usd,
            "cost_usd_nanos": int(float(usd) * 1_000_000_000),
            "cost_quality": "exact",
            "cost_source": "provider-report",
            "created_at": now,
            "settled_at": now,
        }
    )


def test_openrouter_cap_refuses_a_fallback_before_it_can_exceed_fifty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 regression: seed near the $50 cap, force the free primary to 429, and
    assert the paid fallback is REFUSED at preflight (a true-upper-bound
    reservation) BEFORE any billing — never settled-then-over. The invariant:
    settled cap spend stays <= $50 no matter what a misbehaving provider bills.
    """
    import requests

    from omniagentos.adapters.openrouter import OpenRouterAdapter
    from omniagentos.adapters.spend_guard import SpendGuard, SpendGuardRefusal
    from omniagentos.contracts import AgentInput
    from omniagentos.db.store import SqliteStore

    day = "2026-08-12"
    now = f"{day}T12:00:00Z"
    nanos = 1_000_000_000
    db_path = tmp_path / "f1-openrouter-cap.sqlite3"
    guard = SpendGuard(
        config_path=Path.cwd() / "configs" / "spend-caps.yaml",
        db_path=str(db_path),
        now=lambda: now,
        alert_sender=lambda *_a, **_k: type("Alert", (), {"ok": False})(),
    )
    store = SqliteStore(str(db_path))
    _seed_prior_openrouter_spend(store, "49.97", day=day)

    class _Resp:
        def __init__(self, code: int, body: dict) -> None:
            self.status_code = code
            self._body = body

        def json(self) -> dict:
            return self._body

    # gemma:free 429s (shared-pool rate limit), then a paid model would bill an
    # over-reservation amount — but the guard must refuse it before it is sent.
    responses = iter(
        [
            _Resp(429, {"error": "free tier rate limited"}),
            _Resp(
                200,
                {
                    "id": "paid",
                    "model": "deepseek/deepseek-v4-pro",
                    "choices": [{"message": {"content": "overshoot"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 16_667, "cost": "0.04"},
                },
            ),
        ]
    )
    monkeypatch.setattr(requests, "post", lambda *_a, **_k: next(responses))

    adapter = OpenRouterAdapter()
    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")
    monkeypatch.setattr(adapter, "api_base", lambda: "https://example.invalid/v1")
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)

    with pytest.raises(SpendGuardRefusal):
        adapter.run(
            AgentInput(
                run_id="f1-run",
                task_id="f1-task",
                prompt="revived proposer",
                model="google/gemma-4-31b-it:free",
                metadata={"call_id": "f1-call"},
                # The unbounded shape the repro drives: tokens_max=None.
            )
        )

    spend = store.provider_spend_day(provider="openrouter", utc_day=day)[0]["spend_usd_nanos"]
    # The hard invariant: settled spend never exceeds the cap.
    assert spend <= 50 * nanos, "a hard cap must remain >= actual settled spend"
    # The refused paid fallback ($0.04) never billed — spend stays essentially at
    # the seed (only the free primary's tiny conservative 429 reservation settled).
    assert spend < int(49.98 * nanos), "the refused fallback must not have billed $0.04"


# --- F3: a spend-gate failure or malformed result is UNHEALTHY, not healthy -


def test_dispatcher_tick_fails_closed_on_malformed_or_uncontrolled_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3 regression: a missing/unknown mode is not accepted, and a
    budget_unenforceable / budget_overshot tick records adverse with the flags
    preserved in the receipt notes."""
    from contextlib import ExitStack
    from unittest.mock import patch

    from omniagentos.scheduler.builtin_jobs import run_improve_dispatcher_tick
    from omniagentos.scheduler.routines import OUTCOME_FAVOURABLE

    class _Store:
        _connection = object()

    def run_case(payload: dict):
        with ExitStack() as stack:
            stack.enter_context(
                patch("omniagentos.improve.dispatcher.GitCli", return_value=object())
            )
            stack.enter_context(
                patch("omniagentos.improve.dispatcher.tick", return_value=payload)
            )
            stack.enter_context(
                patch("omniagentos.runtime_paths.resolve_var_root", return_value="/tmp/inbox")
            )
            return run_improve_dispatcher_tick(_Store())

    missing = run_case({})
    assert missing.outcome_class != OUTCOME_FAVOURABLE
    assert missing.accepted is False

    budget_failed = run_case(
        {
            "mode": "on",
            "reconciled": [],
            "ingested": [],
            "dispatched": [],
            "budget_blocked": ["t-1"],
            "budget_overshot": True,
            "budget_unenforceable": True,
            "blocked_by": None,
        }
    )
    assert budget_failed.outcome_class != OUTCOME_FAVOURABLE
    assert budget_failed.accepted is False
    assert "budget_unenforceable" in budget_failed.notes


# --- F1 round 5 (final): the SIMPLIFIED, sound contract ---------------------
# SI's OpenRouter calls are cap-safe by ADMISSION (prior+reserved<=cap) + TRUE-
# COST recording. The bespoke round-4 durable-breach machinery (a per-adapter
# settlement-enforcement copy) was fail-open in six ways and has been REMOVED;
# settlement-side enforcement / the shared DAL boundary is proposal 9f7448b1.


def test_openrouter_settlement_records_the_true_billed_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful OpenRouter call records the ACTUAL billed cost (never capped
    or understated), cost_quality=exact, cost_source=provider-report, and that is
    exactly what provider_spend_day reflects. Scratch ledger only."""
    import requests

    from omniagentos.adapters.openrouter import OpenRouterAdapter
    from omniagentos.adapters.spend_guard import SpendGuard
    from omniagentos.contracts import AgentInput, BudgetSpec
    from omniagentos.db.store import SqliteStore

    day = "2026-08-12"
    now = f"{day}T12:00:00Z"
    nanos = 1_000_000_000
    cost = "0.0123456"
    db_path = tmp_path / "settle-true-cost.sqlite3"
    guard = SpendGuard(
        config_path=Path.cwd() / "configs" / "spend-caps.yaml",
        db_path=str(db_path),
        now=lambda: now,
        alert_sender=lambda *_a, **_k: type("Alert", (), {"ok": False})(),
    )
    store = SqliteStore(str(db_path))

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {
                "id": "billed",
                "model": "qwen/qwen3.7-flash",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 12, "cost": cost},
            }

    monkeypatch.setattr(requests, "post", lambda *_a, **_k: _Resp())
    adapter = OpenRouterAdapter()
    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")
    monkeypatch.setattr(adapter, "api_base", lambda: "https://example.invalid/v1")
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)

    result = adapter.run(
        AgentInput(
            run_id="billed-run",
            task_id="billed-task",
            prompt="propose one improvement",
            model="qwen/qwen3.7-flash",
            budget=BudgetSpec(tokens_max=64),
            metadata={"call_id": "billed-call", "strict_model": True},
        )
    )
    assert result.status.value == "ok"

    row = store.get_provider_call("billed-call")
    assert row is not None
    assert int(row["cost_usd_nanos"]) == int(float(cost) * nanos)
    assert row["cost_quality"] == "exact"
    assert row["cost_source"] == "provider-report"
    spend = int(store.provider_spend_day(provider="openrouter", utc_day=day)[0]["spend_usd_nanos"])
    assert spend == int(float(cost) * nanos)  # truthful, never understated
    store.close()
    guard.close()


# --- NEW-2: a config-read failure must FAIL CLOSED, never send uncapped -----


def test_openrouter_fails_closed_when_the_output_ceiling_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEW-2 (2026-08-12): if the model's output ceiling can't be read (spend-caps
    config error), the call must be REFUSED — never admitted with the token cap
    silently dropped. Pins the fail-closed branch of _cap_bounded_input."""
    import requests

    from omniagentos.adapters.openrouter import OpenRouterAdapter
    from omniagentos.adapters.spend_guard import SpendGuard, SpendGuardRefusal
    from omniagentos.contracts import AgentInput, BudgetSpec
    from omniagentos.db.store import SqliteStore

    day = "2026-08-12"
    now = f"{day}T12:00:00Z"
    db_path = tmp_path / "cfg-err.sqlite3"
    guard = SpendGuard(
        config_path=Path.cwd() / "configs" / "spend-caps.yaml",
        db_path=str(db_path),
        now=lambda: now,
        alert_sender=lambda *_a, **_k: type("Alert", (), {"ok": False})(),
    )
    SqliteStore(str(db_path)).close()

    adapter = OpenRouterAdapter()
    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")
    monkeypatch.setattr(adapter, "api_base", lambda: "https://example.invalid/v1")
    monkeypatch.setattr(adapter, "spend_guard", lambda: guard)

    def _ceiling_read_fails(_model: str) -> int:
        raise RuntimeError("simulated spend-caps config read failure")

    monkeypatch.setattr(adapter, "_model_output_ceiling", _ceiling_read_fails)

    posted: list = []
    monkeypatch.setattr(requests, "post", lambda *_a, **_k: posted.append(_k) or None)

    with pytest.raises(SpendGuardRefusal) as caught:
        adapter.run(
            AgentInput(
                run_id="cfg-err-run",
                task_id="cfg-err-task",
                prompt="propose one improvement",
                model="qwen/qwen3.7-flash",
                budget=BudgetSpec(tokens_max=64),
                metadata={"call_id": "cfg-err-call", "strict_model": True},
            )
        )
    assert caught.value.reason_class == "spend_cap_config_unreadable"
    assert posted == [], "a config-read error must not send an uncapped request"
    guard.close()
