"""Fleet Claude-balance alert: breach detection, fallbacks, and edge-triggering."""

from __future__ import annotations

from typing import Any

from omniagentos.team import balance_alerts as ba

NOW = 1_800_000_000.0


def _report(
    host: str,
    *,
    best: float | None,
    authed: int = 2,
    no_snap: int = 0,
    accounts: list[dict[str, Any]] | None = None,
    generated_offset_s: float = 60.0,
    with_usage: bool = True,
) -> dict[str, Any]:
    import datetime as dt

    generated = dt.datetime.fromtimestamp(NOW - generated_offset_s, dt.UTC)
    report: dict[str, Any] = {
        "host": host,
        "employee_id": "emp_x",
        "generated_at": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if with_usage:
        report["claude_usage"] = {
            "accounts": accounts or [],
            "distinct_accounts": authed,
            "authed_accounts": authed,
            "authed_no_snapshot": no_snap,
            "best_remaining_percent": best,
            "best_dir": ".claude-account-1" if best is not None else None,
        }
    return report


def test_healthy_machine_is_ok() -> None:
    machine = ba.assess_machine(_report("mac-a", best=42.0), now=NOW)
    assert machine.status == "ok"


def test_below_threshold_without_fallback_breaches() -> None:
    machine = ba.assess_machine(_report("mac-a", best=4.0), now=NOW)
    assert machine.status == "breached"
    assert "no fallback" in machine.reason


def test_fresh_unmeasured_account_makes_breach_at_risk_not_ok() -> None:
    machine = ba.assess_machine(_report("mac-a", best=4.0, no_snap=1), now=NOW)
    assert machine.status == "at_risk"
    assert "unverified" in machine.reason
    # at_risk neither pages nor clears/recovers a standing breach.
    alerts, recoveries, state = ba.decide_notifications([machine], {}, now=NOW)
    assert alerts == [] and recoveries == []
    breached = ba.assess_machine(_report("mac-a", best=1.0), now=NOW)
    _, _, breach_state = ba.decide_notifications([breached], {}, now=NOW)
    alerts2, recoveries2, state2 = ba.decide_notifications([machine], breach_state, now=NOW + 60)
    assert alerts2 == [] and recoveries2 == []
    assert state2["emp_x"]["state"] == "breached"


def test_malformed_remote_fields_never_crash_assessment() -> None:
    report = _report("mac-a", best=50.0)
    report["claude_usage"]["authed_accounts"] = "broken"
    report["claude_usage"]["best_remaining_percent"] = float("inf")
    machine = ba.assess_machine(report, now=NOW)
    # Malformed counts/percent degrade to unknown-ish verdicts, never a crash
    # and never a fabricated healthy number.
    assert machine.status in ("unknown", "no_claude")


def test_profiles_present_but_none_authed_is_a_paging_breach() -> None:
    report = _report("mac-a", best=None, authed=0)
    report["claude_usage"]["distinct_accounts"] = 3  # profiles exist, auth dead
    machine = ba.assess_machine(report, now=NOW)
    assert machine.status == "no_auth"


def test_never_provisioned_machine_is_context_not_a_page() -> None:
    report = _report("mac-a", best=None, authed=0)
    report["claude_usage"]["distinct_accounts"] = 0
    machine = ba.assess_machine(report, now=NOW)
    assert machine.status == "no_claude"
    alerts, recoveries, _ = ba.decide_notifications([machine], {}, now=NOW)
    assert alerts == [] and recoveries == []


def test_no_claude_clears_a_stale_breach_without_a_recovery_note() -> None:
    breached = ba.assess_machine(_report("mac-a", best=1.0), now=NOW)
    _, _, state = ba.decide_notifications([breached], {}, now=NOW)
    gone = _report("mac-a", best=None, authed=0)
    gone["claude_usage"]["distinct_accounts"] = 0
    left_fleet = ba.assess_machine(gone, now=NOW)
    alerts, recoveries, state2 = ba.decide_notifications([left_fleet], state, now=NOW + 60)
    assert alerts == [] and recoveries == []
    assert state2["emp_x"]["state"] == "ok"


def test_stale_report_is_unknown_never_healthy() -> None:
    machine = ba.assess_machine(_report("mac-a", best=99.0, generated_offset_s=4 * 3600.0), now=NOW)
    assert machine.status == "unknown"


def test_missing_claude_usage_is_unknown_never_healthy() -> None:
    machine = ba.assess_machine(_report("mac-a", best=None, with_usage=False), now=NOW)
    assert machine.status == "unknown"
    assert "collector" in machine.reason


def test_exactly_ten_percent_is_not_a_breach() -> None:
    machine = ba.assess_machine(_report("mac-a", best=10.0), now=NOW)
    assert machine.status == "ok"


def test_edge_trigger_alerts_once_then_realerts_after_window() -> None:
    breached = ba.assess_machine(_report("mac-a", best=1.0), now=NOW)

    alerts, recoveries, state = ba.decide_notifications([breached], {}, now=NOW)
    assert [m.host for m in alerts] == ["mac-a"] and not recoveries

    # Same breach five minutes later: suppressed.
    alerts2, _, state2 = ba.decide_notifications([breached], state, now=NOW + 300)
    assert alerts2 == []

    # Past the re-alert window: fires again.
    later = NOW + ba.REALERT_H * 3600.0 + 1
    alerts3, _, state3 = ba.decide_notifications([breached], state2, now=later)
    assert [m.host for m in alerts3] == ["mac-a"]

    # Recovery posts exactly once.
    recovered = ba.assess_machine(_report("mac-a", best=55.0), now=NOW)
    alerts4, recoveries4, state4 = ba.decide_notifications([recovered], state3, now=later + 60)
    assert alerts4 == [] and [m.host for m in recoveries4] == ["mac-a"]
    alerts5, recoveries5, _ = ba.decide_notifications([recovered], state4, now=later + 120)
    assert alerts5 == [] and recoveries5 == []


def test_unknown_mid_breach_neither_pages_nor_clears() -> None:
    breached = ba.assess_machine(_report("mac-a", best=1.0), now=NOW)
    _, _, state = ba.decide_notifications([breached], {}, now=NOW)

    gone_quiet = ba.assess_machine(
        _report("mac-a", best=1.0, generated_offset_s=5 * 3600.0), now=NOW
    )
    assert gone_quiet.status == "unknown"
    alerts, recoveries, state2 = ba.decide_notifications([gone_quiet], state, now=NOW + 600)
    assert alerts == [] and recoveries == []
    assert state2["emp_x"]["state"] == "breached"


def test_duplicate_logins_never_count_as_fallback_accounts() -> None:
    # Two dirs, one Anthropic account: the collector marks the second
    # duplicate_of and best_remaining reflects the single real window.
    accounts = [
        {"dir": "a", "authed": True, "remaining_percent": 3.0, "duplicate_of": None},
        {"dir": "b", "authed": True, "remaining_percent": 3.0, "duplicate_of": "a"},
    ]
    report = _report("mac-a", best=3.0, authed=1, accounts=accounts)
    machine = ba.assess_machine(report, now=NOW)
    assert machine.status == "breached"
    assert machine.accounts_out == ["a"]


def test_json_bomb_and_non_report_files_become_unknown_rows(tmp_path) -> None:
    directory = tmp_path / "team-sessions"
    directory.mkdir()
    (directory / "bomb.json").write_text("[" * 20000 + "]" * 20000, encoding="utf-8")
    (directory / "list.json").write_text("[]", encoding="utf-8")
    machines = ba.assess_fleet(tmp_path, now=NOW)
    assert sorted((m.host, m.status) for m in machines) == [
        ("bomb", "unknown"),
        ("list", "unknown"),
    ]


def test_present_null_no_snapshot_is_malformed_not_legacy_zero() -> None:
    report = _report("mac-a", best=50.0)
    report["claude_usage"]["authed_no_snapshot"] = None
    machine = ba.assess_machine(report, now=NOW)
    assert machine.status == "unknown"


def test_edge_state_keys_by_employee_never_by_host() -> None:
    # Two employees, same default hostname: one breached, one healthy — the
    # healthy report must NOT recover or silence the other's breach (Grok F1).
    breached = ba.assess_machine(
        dict(_report("MacBook-Pro.local", best=1.0), employee_id="emp_victim"),
        now=NOW,
    )
    _, _, state = ba.decide_notifications([breached], {}, now=NOW)

    healthy_other = ba.assess_machine(
        dict(_report("MacBook-Pro.local", best=80.0), employee_id="emp_other"),
        now=NOW,
    )
    alerts, recoveries, state2 = ba.decide_notifications([healthy_other], state, now=NOW + 60)
    assert recoveries == []  # no false recovery for the victim
    assert state2["emp_victim"]["state"] == "breached"  # breach not silenced

    # The victim's own recovery still works, keyed to them.
    recovered = ba.assess_machine(
        dict(_report("MacBook-Pro.local", best=70.0), employee_id="emp_victim"),
        now=NOW,
    )
    _, recoveries2, state3 = ba.decide_notifications([recovered], state2, now=NOW + 120)
    assert [m.employee_id for m in recoveries2] == ["emp_victim"]
    assert state3["emp_victim"]["state"] == "ok"
