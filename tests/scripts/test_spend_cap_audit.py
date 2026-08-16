from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omniagentos.db.store import SqliteStore

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "health-sentinel" / "audit_checks.py"


def _load() -> Any:
    name = "spend_cap_audit_under_test"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


audit = _load()
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    caps = repo / "configs" / "spend-caps.yaml"
    caps.write_text(
        "providers:\n"
        "  kimi:\n"
        "    enabled: true\n"
        "    daily_cap_usd: '100'\n"
        "  fireworks:\n"
        "    enabled: true\n"
        "    daily_cap_usd: '200'\n",
        encoding="utf-8",
    )
    return repo, repo / "state.sqlite3"


def _ctx(repo: Path) -> Any:
    return audit.AuditContext(
        repo_root=repo,
        accounts_root=repo,
        registry={
            "checks": {
                "provider_daily_spend": {
                    "threshold": "warn >80%; fail >100%",
                    "provenance": "test",
                    "db": "state.sqlite3",
                    "caps": "configs/spend-caps.yaml",
                    "providers": ["kimi", "fireworks"],
                }
            }
        },
        now=NOW,
    )


def _seed(store: SqliteStore, *, call_id: str, provider: str, nanos: int) -> None:
    whole, fractional = divmod(nanos, 1_000_000_000)
    decimal = str(whole) if not fractional else f"{whole}.{fractional:09d}".rstrip("0")
    store.record_provider_call(
        {
            "call_id": call_id,
            "request_id": f"req-{call_id}",
            "execution_id": f"exe-{call_id}",
            "stage": "worker",
            "provider": provider,
            "transport": "test",
            "requested_model": "kimi-k3",
            "effective_model": "kimi-k3",
            "model_lineage": "kimi",
            "billing_provider": provider,
            "adapter_key": "test",
            "request_state": "sent",
            "provider_outcome": "completed",
            "cost_usd_decimal": decimal,
            "cost_usd_nanos": nanos,
            "cost_quality": "exact",
            "cost_source": "provider-report",
            "created_at": "2026-08-05T12:00:00Z",
            "settled_at": "2026-08-05T12:00:00Z",
        }
    )


def test_direct_ledger_check_warns_above_eighty_percent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo, db = _repo(tmp_path)
    monkeypatch.setenv("OMNIAGENTOS_SPEND_DB", str(db))
    store = SqliteStore(str(db))
    try:
        _seed(store, call_id="kimi-81", provider="kimi", nanos=81_000_000_000)
    finally:
        store.close()

    result = audit.check_provider_daily_spend(_ctx(repo))

    assert result.status == audit.WARN
    assert result.detail["providers"]["kimi"]["ratio"] == 0.81
    assert result.detail["providers"]["fireworks"]["status"] == audit.OK


def test_direct_ledger_check_fails_above_cap_even_if_guard_is_bypassed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo, db = _repo(tmp_path)
    monkeypatch.setenv("OMNIAGENTOS_SPEND_DB", str(db))
    store = SqliteStore(str(db))
    try:
        _seed(store, call_id="fireworks-201", provider="fireworks", nanos=201_000_000_000)
    finally:
        store.close()

    result = audit.check_provider_daily_spend(_ctx(repo))

    assert result.status == audit.FAIL
    assert result.detail["providers"]["fireworks"]["ratio"] == 1.005
    assert "fireworks=$201.000000/$200.00" in result.evidence


def test_missing_or_unreadable_ledger_is_fail_not_skip(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo, db = _repo(tmp_path)
    monkeypatch.setenv("OMNIAGENTOS_SPEND_DB", str(db))
    result = audit.check_provider_daily_spend(_ctx(repo))
    assert result.status == audit.FAIL
    assert result.evidence.startswith("cannot-run: ledger missing")


def test_guard_and_audit_share_the_canonical_spend_db(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from omniagentos.adapters.spend_guard import SpendGuard

    repo, db = _repo(tmp_path)
    monkeypatch.setenv("OMNIAGENTOS_SPEND_DB", str(db))
    store = SqliteStore(str(db))
    store.close()
    guard = SpendGuard(config_path=repo / "configs/spend-caps.yaml")
    try:
        result = audit.check_provider_daily_spend(_ctx(repo))
        assert guard.db_path == str(db)
        assert str(audit.resolve_spend_db_path()) == str(db)
        assert result.detail["db"] == str(db)
    finally:
        guard.close()


def test_today_spend_under_unknown_billing_provider_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo, db = _repo(tmp_path)
    monkeypatch.setenv("OMNIAGENTOS_SPEND_DB", str(db))
    store = SqliteStore(str(db))
    try:
        _seed(store, call_id="uncapped-provider", provider="new-paid-provider", nanos=1)
    finally:
        store.close()

    result = audit.check_provider_daily_spend(_ctx(repo))

    assert result.status == audit.FAIL
    assert result.detail["uncapped_billing_providers"] == ["new-paid-provider"]
    assert "UNCAPPED billing provider(s): new-paid-provider" in result.evidence

def test_provider_daily_spend_yaml_providers_matches_the_python_fallback() -> None:
    """Regression for the 2026-08-06 review (configs/audit-checks.yaml:228).

    ``check_provider_daily_spend``'s Python fallback
    (``cfg.get("providers") or ["moonshot", "fireworks"]``) NEVER fires once
    configs/audit-checks.yaml supplies an explicit ``providers:`` list -- the
    YAML always wins. A stale YAML list (still ``[kimi, fireworks]`` after
    the Blocker-1 billing-identity rename) silently regressed this check from
    a useful FAIL to "cannot-run: ... enabled cap row is absent for kimi",
    which reads like the ordinary alarm-fatigue FAIL and is easy to wave
    past -- on the ONE check whose own provenance says it exists to catch the
    guard itself failing. This test fails if the YAML list and the Python
    fallback ever disagree again, and separately proves every configured
    provider has an enabled cap row (the exact precondition whose absence
    caused the "cannot-run" regression).
    """

    repo_root = _SCRIPT.parents[2]
    audit_checks_doc = yaml.safe_load(
        (repo_root / "configs" / "audit-checks.yaml").read_text(encoding="utf-8")
    )
    spend_caps_doc = yaml.safe_load(
        (repo_root / "configs" / "spend-caps.yaml").read_text(encoding="utf-8")
    )

    configured_providers = audit_checks_doc["checks"]["provider_daily_spend"]["providers"]
    # Must mirror check_provider_daily_spend's own Python fallback exactly.
    fallback_providers = ["moonshot", "fireworks"]
    assert configured_providers == fallback_providers, (
        f"configs/audit-checks.yaml providers={configured_providers!r} has "
        f"drifted from the Python fallback {fallback_providers!r} in "
        "scripts/health-sentinel/audit_checks.py:check_provider_daily_spend -- "
        "since the explicit YAML list always wins, keep them identical or the "
        "fallback is dead code that silently stops protecting production."
    )

    enabled_providers = {
        name
        for name, row in spend_caps_doc["providers"].items()
        if isinstance(row, dict) and row.get("enabled") is True
    }
    for provider in configured_providers:
        assert provider in enabled_providers, (
            f"{provider!r} is listed in configs/audit-checks.yaml's "
            "provider_daily_spend.providers but has no enabled cap row in "
            "configs/spend-caps.yaml -- check_provider_daily_spend degrades "
            "to cannot-run instead of a real FAIL/WARN/OK for this provider"
        )

