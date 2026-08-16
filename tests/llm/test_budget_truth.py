"""P1-COST-EDGE: BudgetGuard must not invent $0.00 for missing/null ledger cost.

Missing ``estimated_usd_cost`` is treated as a malformed line (skipped).
Explicit null is UNKNOWN — fail closed (never free). Exact zero remains a real
observation and still counts.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from omniagentos.llm.budget import (
    BudgetGuard,
    LLMBudgetExceededError,
    LLMBudgetUnknownError,
)


@pytest.fixture
def ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    var = tmp_path / "var"
    var.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var))
    return var


def _today_entry(**fields) -> str:
    today = datetime.datetime.now(datetime.UTC).isoformat()
    payload = {"timestamp": today, "model": "gemini-3.6-flash", **fields}
    return json.dumps(payload) + "\n"


def _write_ledger(guard: BudgetGuard, lines: list[str]) -> Path:
    from omniagentos.llm import budget as budget_mod

    path = Path(budget_mod._ledger_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    return path


class TestBudgetTruth:
    def test_missing_cost_key_is_skipped_not_zeroed(
        self, ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(ledger_dir))
        guard = BudgetGuard(config_dict={"daily_usd_cap": 10.0})
        _write_ledger(
            guard,
            [
                _today_entry(estimated_usd_cost=0.05),
                _today_entry(),  # missing estimated_usd_cost entirely
                _today_entry(estimated_usd_cost=0.02),
            ],
        )
        spent = guard.get_today_spend()
        assert spent == pytest.approx(0.07)

    def test_null_cost_fails_closed_not_zeroed(
        self, ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit null is unknown spend state — refuse, never treat as free."""
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(ledger_dir))
        guard = BudgetGuard(config_dict={"daily_usd_cap": 10.0})
        _write_ledger(
            guard,
            [
                _today_entry(estimated_usd_cost=0.04),
                _today_entry(estimated_usd_cost=None),
                _today_entry(estimated_usd_cost=0.01),
            ],
        )
        with pytest.raises(LLMBudgetUnknownError):
            guard.get_today_spend()

    def test_exact_zero_is_counted(
        self, ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(ledger_dir))
        guard = BudgetGuard(config_dict={"daily_usd_cap": 10.0})
        _write_ledger(
            guard,
            [
                _today_entry(estimated_usd_cost=0.0),
                _today_entry(estimated_usd_cost=0.03),
            ],
        )
        spent = guard.get_today_spend()
        assert spent == pytest.approx(0.03)

    def test_cap_still_enforced_on_known_spend(
        self, ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(ledger_dir))
        guard = BudgetGuard(config_dict={"daily_usd_cap": 0.05})
        _write_ledger(
            guard,
            [
                _today_entry(estimated_usd_cost=0.04),
                _today_entry(estimated_usd_cost=0.02),
            ],
        )
        with pytest.raises(LLMBudgetExceededError):
            guard.check_budget()

    def test_null_cost_blocks_check_budget(
        self, ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(ledger_dir))
        guard = BudgetGuard(config_dict={"daily_usd_cap": 10.0})
        _write_ledger(
            guard,
            [
                _today_entry(estimated_usd_cost=0.01),
                _today_entry(estimated_usd_cost=None),
            ],
        )
        with pytest.raises(LLMBudgetUnknownError):
            guard.check_budget()
