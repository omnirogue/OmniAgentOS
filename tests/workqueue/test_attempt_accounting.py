"""Attempt accounting — the one place this design can silently break its promise.

`claim` increments unconditionally; `record_result` is where the promise that an
instrument failure never consumes a candidate's attempt budget is actually kept.
Every assertion here is a line of §3.1/§4.3/§4.5 made executable.
"""

from __future__ import annotations

from tests.workqueue.conftest import at, submit

OWNER = "mac-studio:w1"


def _claim(store, now):
    claimed = store.claim("mac-studio", "w1", [], now=now)
    assert claimed is not None
    return claimed


def test_instrument_errors_never_consume_the_attempt_budget(store):
    unit_id, _ = store.enqueue(submit("instrument"))

    alerts = []
    claims = 0
    seen = []
    for cycle in range(5):
        now = at(cycle * 2000)
        claimed = store.claim("mac-studio", "w1", [], now=now)
        if claimed is None:
            break
        claims += 1
        out = store.record_result(
            unit_id,
            OWNER,
            claimed["lease_generation"],
            "instrument-error",
            exit_code=2,
            retryable=1,
            remedy="disk I/O error on the runner — check the volume, not the code",
            now=at(cycle * 2000 + 10),
        )
        seen.append(dict(out["unit"]))
        if out["alert"] is not None:
            alerts.append(out["alert"])

    unit = store.get_unit(unit_id)
    assert unit["attempt"] == 0, "an instrument fault must not spend a candidate's budget"
    assert unit["instrument_retries"] == 3, "the counter stops AT the cap it parked on"
    assert unit["state"] == "parked"
    assert unit["terminal_reason"] == "terminal-instrument"
    assert claims == 4, "3 backoff retries (60/300/900s) then the park"

    # Backoff schedule, in order.
    assert [row["not_before"] for row in seen[:3]] == [
        at(10 + 60),
        at(2010 + 300),
        at(4010 + 900),
    ]
    assert seen[3]["not_before"] is None

    # Exactly one alert per park (§4.5) — and only for the park.
    assert len(alerts) == 1
    assert alerts[0]["terminal_reason"] == "terminal-instrument"
    assert "disk I/O error" in alerts[0]["remedy"]
    assert len(store.alerts()) == 1
    # A second park of the same unit never re-alerts.
    assert store.park(unit_id, "terminal-instrument", "same again", now=at(99999)) is None
    assert len(store.alerts()) == 1


def test_backoff_gates_the_next_claim(store):
    store.enqueue(submit("backoff"))
    claimed = _claim(store, at(0))
    store.record_result(
        claimed["unit"]["id"],
        OWNER,
        claimed["lease_generation"],
        "environment",
        exit_code=2,
        retryable=1,
        remedy="rate limit exceeded — wait for the window",
        now=at(10),
    )
    assert store.claim("mac-studio", "w1", [], now=at(20)) is None, "not_before must hold it back"
    assert store.claim("mac-studio", "w1", [], now=at(80)) is not None


def test_retryable_zero_parks_at_the_first_occurrence(store):
    unit_id, _ = store.enqueue(submit("auth"))
    claimed = _claim(store, at(0))
    out = store.record_result(
        unit_id,
        OWNER,
        claimed["lease_generation"],
        "environment",
        exit_code=2,
        retryable=0,
        remedy="401 from the provider — rotate the key in ~/.config/omni/connections.env",
        now=at(10),
    )
    unit = out["unit"]
    # The auth/suspension row in §4.4b: terminal at 1, park + one alert, and it
    # never enters the defect count.
    assert unit["state"] == "parked"
    assert unit["terminal_reason"] == "terminal-instrument"
    assert unit["attempt"] == 0
    assert unit["instrument_retries"] == 1
    assert out["alert"] is not None


def test_timeout_consumes_the_attempt(store):
    unit_id, _ = store.enqueue(submit("timeout", max_attempts=2))

    claimed = _claim(store, at(0))
    out = store.record_result(
        unit_id, OWNER, claimed["lease_generation"], "timeout", exit_code=124, now=at(10)
    )
    # SPEC §3.1 lists six reversing outcomes; timeout is not one of them. The
    # unit's own timeout_s bounds the work, so blowing it is evidence about the
    # candidate, not about the box.
    assert out["unit"]["attempt"] == 1
    assert out["unit"]["state"] == "queued"

    claimed = _claim(store, at(1000))
    out = store.record_result(
        unit_id, OWNER, claimed["lease_generation"], "timeout", exit_code=124, now=at(1010)
    )
    assert out["unit"]["attempt"] == 2
    assert out["unit"]["state"] == "parked"
    assert out["unit"]["terminal_reason"] == "attempts-exhausted"
    assert out["alert"] is not None


def test_candidate_defects_exhaust_the_budget_and_swap_lineage_on_the_second(store):
    unit_id, _ = store.enqueue(submit("defect", agent_profile="codex-exec"))

    profiles = []
    alerts = []
    for cycle in range(3):
        claimed = _claim(store, at(cycle * 1000))
        out = store.record_result(
            unit_id,
            OWNER,
            claimed["lease_generation"],
            "candidate-defect",
            exit_code=1,
            remedy="the test it added fails",
            now=at(cycle * 1000 + 10),
        )
        profiles.append(out["unit"]["agent_profile"])
        if out["alert"] is not None:
            alerts.append(out["alert"])

    # §4.6: on no-progress the queue changes the ACTION, not the tier — the
    # second defect dispatches to a different lineage.
    assert profiles[0] == "codex-exec"
    assert profiles[1] == "claude-headless"
    assert profiles[2] == "claude-headless"

    unit = store.get_unit(unit_id)
    assert unit["attempt"] == 3
    assert unit["state"] == "parked"
    assert unit["terminal_reason"] == "attempts-exhausted"
    assert len(alerts) == 1

    # An exhausted unit is not claimable, and after a human unpark it is.
    assert store.claim("mac-studio", "w1", [], now=at(9000)) is None
    store.unpark(unit_id, because="the flaky fixture was fixed on main")
    unit = store.get_unit(unit_id)
    assert unit["state"] == "queued"
    assert unit["terminal_reason"] is None
    assert unit["attempt"] == 0, "an unpark that leaves it unclaimable accomplishes nothing"
    assert store.claim("mac-studio", "w1", [], now=at(9100)) is not None


def test_unchanged_retry_soft_parks_without_an_alert(store):
    unit_id, _ = store.enqueue(submit("unchanged"))
    claimed = _claim(store, at(0))
    out = store.record_result(
        unit_id,
        OWNER,
        claimed["lease_generation"],
        "unchanged-retry",
        exit_code=2,
        retryable=0,
        remedy="the gate already refused this exact input_key — change the input",
        now=at(10),
    )
    unit = out["unit"]
    assert unit["state"] == "parked"
    assert unit["terminal_reason"] is None, "soft park: no terminal reason"
    assert unit["park_remedy"].startswith("the gate already refused")
    assert unit["attempt"] == 0, "nothing ran, so nothing was spent"
    # Re-queueing would busy-loop the ledger, and alerting on it would drown the
    # one-alert guard; the storm park four refusals later is the event.
    assert out["alert"] is None
    assert store.alerts() == []


def test_storm_park_and_lease_lost_accounting(store):
    unit_id, _ = store.enqueue(submit("storm"))

    claimed = _claim(store, at(0))
    out = store.record_result(unit_id, OWNER, claimed["lease_generation"], "lease-lost", now=at(10))
    assert out["unit"]["attempt"] == 0
    assert out["unit"]["state"] == "queued"

    claimed = _claim(store, at(100))
    out = store.record_result(
        unit_id,
        OWNER,
        claimed["lease_generation"],
        "storm-parked",
        exit_code=2,
        retryable=0,
        remedy="5 refusals on one input_key — land the exemption on main first",
        now=at(110),
    )
    assert out["unit"]["attempt"] == 0
    assert out["unit"]["state"] == "parked"
    assert out["unit"]["terminal_reason"] == "storm-parked"
    assert out["alert"] is not None
