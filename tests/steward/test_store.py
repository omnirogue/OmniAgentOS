from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.steward.store import (
    _SUGGESTION_EVIDENCE_MAX,
    StewardStore,
    suggestion_occurrences,
)


@pytest.fixture
def store(tmp_path: Path) -> StewardStore:
    return StewardStore(SqliteStore(str(tmp_path / "steward.db")))


def test_goals_snapshots_and_fact_links(store: StewardStore) -> None:
    goal = store.upsert_goal(
        {
            "name": "Profitable growth",
            "north_star": {"metric": "roas"},
            "target": {"value": 2.0},
            "keywords": ["revenue"],
        }
    )
    assert goal["id"].startswith("gl_") and len(goal["id"]) == 15
    assert store.get_goal(goal["id"])["north_star"] == {"metric": "roas"}  # type: ignore[index]
    updated = store.upsert_goal(
        {
            "name": "Profitable growth",
            "description": "Updated",
            "north_star": {"metric": "roas"},
            "status": "paused",
        }
    )
    assert updated["id"] == goal["id"]
    assert store.list_goals("paused") == [updated]

    now = datetime.now(UTC)
    # Metrics are one authoritative value per (goal, source, metric, calendar day):
    # the daily collector writes one row per completed day, so a real series spans days.
    later_id = store.insert_metric_snapshot(
        {
            "goal_id": goal["id"],
            "source": "ads",
            "metric": "roas",
            "value": 2.5,
            "captured_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT12:00:00Z"),
            "meta": {"campaigns": 4},
        }
    )
    earlier_id = store.insert_metric_snapshot(
        {
            "goal_id": goal["id"],
            "source": "ads",
            "metric": "roas",
            "value": 1.5,
            "captured_at": (now - timedelta(days=2)).strftime("%Y-%m-%dT12:00:00Z"),
        }
    )
    assert earlier_id > later_id  # earlier_id is the second insert → higher autoincrement id
    latest = store.latest_snapshot("ads", "roas", goal["id"])
    assert latest is not None and latest["value"] == 2.5
    assert latest["meta"] == {"campaigns": 4}
    assert [row["value"] for row in store.snapshot_series("roas")] == [1.5, 2.5]

    # Re-writing the same calendar day upserts in place (idempotent daily collector),
    # never accumulating duplicate intra-day rows (council H1/PERF-002 fix).
    store.insert_metric_snapshot(
        {
            "goal_id": goal["id"],
            "source": "ads",
            "metric": "roas",
            "value": 2.9,
            "captured_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT18:00:00Z"),
        }
    )
    assert [row["value"] for row in store.snapshot_series("roas")] == [1.5, 2.9]

    store.link_fact_to_goal(goal["id"], 9, "curator")
    store.link_fact_to_goal(goal["id"], 9, "ignored")
    assert store.goal_fact_links(goal["id"]) == [
        {
            "goal_id": goal["id"],
            "fact_id": 9,
            "linked_by": "curator",
            "linked_at": store.goal_fact_links(goal["id"])[0]["linked_at"],
        }
    ]


def test_comms_message_dedupe_filters_and_sources(store: StewardStore) -> None:
    first, created = store.insert_comms_message(
        {
            "source": "zapier",
            "external_id": "ext-1",
            "thread_id": "thread-1",
            "sender": "vip@example.com",
            "recipients": ["owner@example.com"],
            "subject": "Urgent account issue",
            "body_text": "Please investigate",
            "raw": {"provider": "mail"},
            "attachments": [{"name": "invoice.pdf"}],
        }
    )
    duplicate, duplicate_created = store.insert_comms_message(
        {"source": "zapier", "external_id": "ext-1", "subject": "replacement"}
    )
    second, _ = store.insert_comms_message(
        {"source": "zapier", "external_id": "ext-2", "thread_id": "thread-2"}
    )
    assert created is True and duplicate_created is False
    assert duplicate == first
    assert first["recipients"] == ["owner@example.com"]
    assert first["raw"] == {"provider": "mail"}
    assert first["attachments"] == [{"name": "invoice.pdf"}]
    assert store.get_comms_message(first["id"]) == first
    assert store.list_comms_messages(q="urgent") == [first]
    assert store.list_comms_messages(thread_id="thread-1") == [first]
    assert store.list_comms_messages(before_id=second["id"]) == [first]

    store.set_message_kb(first["id"], kb_status="ingested", episode_id=42)
    ingested = store.list_comms_messages(kb_status="ingested", source="zapier")
    assert ingested[0]["episode_id"] == 42

    source = store.upsert_comms_source(
        "zapier", "webhook", status="active", config={"secret_env": "SECRET"}
    )
    source = store.upsert_comms_source("zapier", "webhook", last_poll_at="2026-01-01")
    assert source["status"] == "active"
    assert source["config"] == {"secret_env": "SECRET"}
    assert store.list_comms_sources() == [source]


def test_alert_cooldown_ack_and_count(store: StewardStore) -> None:
    alert = store.create_alert(
        {
            "rule": "spend-spike",
            "severity": "high",
            "title": "Spend spike",
            "body": "Spend rose 80%",
            "evidence": {"pct": 80},
            "cooldown_key": "spend:all",
            "cooldown_minutes": 240,
        }
    )
    assert alert is not None
    assert alert["evidence"]["pct"] == 80
    # CASE IDENTITY: a fresh incident's case bookkeeping starts at occurrence 1.
    assert alert["evidence"]["_case"]["occurrence_count"] == 1
    assert store.open_alert_count() == 1
    assert (
        store.create_alert(
            {
                "rule": "spend-spike",
                "title": "Duplicate",
                "cooldown_key": "spend:all",
                "cooldown_minutes": 240,
            }
        )
        is None
    )
    # The repeat did NOT append a second row: it quietly bumped the SAME open
    # case's occurrence count instead (council: 82 opens collapsing to 3 keys).
    open_alerts = store.list_alerts("open")
    assert len(open_alerts) == 1
    assert open_alerts[0]["id"] == alert["id"]
    assert open_alerts[0]["evidence"]["_case"]["occurrence_count"] == 2
    # The suppressed repeat's own (lower/absent) severity never touched the row.
    assert open_alerts[0]["severity"] == "high"
    acked = store.ack_alert(alert["id"], "operator")
    assert acked is not None and acked["state"] == "acked" and acked["acked_at"]
    assert store.ack_alert(alert["id"], "operator") is None
    assert store.open_alert_count() == 0


def test_create_alert_escalation_updates_same_open_case_not_a_new_row(
    store: StewardStore,
) -> None:
    """H12 escalation now updates the SAME case row instead of appending a
    second one -- a worsening incident is still one open case, not two.
    """
    first = store.create_alert(
        {
            "rule": "roas_floor",
            "severity": "critical",
            "title": "ROAS below floor",
            "body": "shortfall 0.1",
            "cooldown_key": "roas_floor",
            "cooldown_minutes": 240,
            "magnitude": 0.1,
        }
    )
    assert first is not None
    escalated = store.create_alert(
        {
            "rule": "roas_floor",
            "severity": "critical",
            "title": "ROAS way below floor",
            "body": "shortfall 0.8",
            "cooldown_key": "roas_floor",
            "cooldown_minutes": 240,
            "magnitude": 0.8,
        }
    )
    assert escalated is not None
    assert escalated["id"] == first["id"]
    assert escalated["title"] == "ROAS way below floor"
    assert escalated["evidence"]["magnitude"] == pytest.approx(0.8)
    assert escalated["evidence"]["_case"]["occurrence_count"] == 2
    assert len(store.list_alerts("open")) == 1


def test_resolve_alert_closes_open_case_and_is_idempotent(store: StewardStore) -> None:
    alert = store.create_alert(
        {
            "rule": "spend-spike",
            "severity": "high",
            "title": "Spend spike",
            "cooldown_key": "spend:all",
            "cooldown_minutes": 240,
        }
    )
    assert alert is not None
    resolved = store.resolve_alert(alert["id"], reason="recovered")
    assert resolved is not None
    assert resolved["state"] == "resolved"
    assert resolved["evidence"]["_case"]["resolved_reason"] == "recovered"
    assert resolved["evidence"]["_case"]["resolved_at"]
    assert store.open_alert_count() == 0
    # Resolving an already-resolved (or missing) alert is a no-op.
    assert store.resolve_alert(alert["id"], reason="recovered") is None
    assert store.resolve_alert(999_999, reason="recovered") is None

    # A fresh occurrence after resolution starts a BRAND NEW case, not a
    # reopen of the closed row (fire -> recover -> fire == open -> resolved ->
    # new case).
    reopened = store.create_alert(
        {
            "rule": "spend-spike",
            "severity": "high",
            "title": "Spend spike again",
            "cooldown_key": "spend:all",
            "cooldown_minutes": 240,
        }
    )
    assert reopened is not None
    assert reopened["id"] != alert["id"]
    assert reopened["evidence"]["_case"]["occurrence_count"] == 1
    assert store.open_alert_count() == 1


def test_open_alerts_is_unpaginated_beyond_the_first_page(store: StewardStore) -> None:
    """The alert-side twin of ``open_suggestion_by_title``'s pagination fix.

    ``list_alerts("open")`` defaults to ``limit=100``, which used to be the
    ONLY read the monitor's auto-resolve/disabled-rule/stale-backlog sweeps
    had -- so an open case old enough to sit past the first page was
    invisible to all three, exactly when the backlog (the thing the sweeps
    exist to shrink) was worst. ``open_alerts`` is unlimited on purpose.
    """
    target = store.create_alert(
        {
            "rule": "roas_floor",
            "severity": "high",
            "title": "Old case",
            "cooldown_key": "target-key",
            "cooldown_minutes": 240,
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    assert target is not None
    # 150 newer rows on a different key push the target off page one:
    # list_alerts orders by created_at DESC, so newer rows crowd it out.
    for index in range(150):
        store.create_alert(
            {
                "rule": "filler_rule",
                "severity": "low",
                "title": f"Filler {index}",
                "cooldown_key": f"filler-{index}",
                "cooldown_minutes": 240,
                "created_at": f"2026-02-01T00:{index // 60:02d}:{index % 60:02d}Z",
            }
        )
    assert len(store.list_alerts("open")) == 100  # the page the old code saw
    assert target["id"] not in {row["id"] for row in store.list_alerts("open")}

    open_rows = store.open_alerts()
    assert len(open_rows) == 151
    assert target["id"] in {row["id"] for row in open_rows}


def test_create_or_bump_suggestion_dedupes_and_does_not_resurrect_decided(
    store: StewardStore,
) -> None:
    first = store.create_or_bump_suggestion(
        {
            "title": "Review underperforming ad sets",
            "rationale": "Suggested by alert rule roas_floor.",
            "evidence": [{"metric": "roas", "value": 0.5}],
            "risk_class": "read_only",
            "source": "alerts",
            "alert_id": 1,
        }
    )
    bumped = store.create_or_bump_suggestion(
        {
            "title": "Review underperforming ad sets",
            "rationale": "Suggested by alert rule roas_floor.",
            "evidence": [{"metric": "roas", "value": 0.2}],
            "risk_class": "read_only",
            "source": "alerts",
            "alert_id": 2,
        }
    )
    assert bumped["id"] == first["id"]
    assert len(bumped["evidence"]) == 2
    assert bumped["alert_id"] == 2  # points at the CURRENT alert occurrence
    assert store.list_suggestions(state="open") == [bumped]

    # A decided suggestion never resurrects: the next occurrence opens a new row.
    store.decide_suggestion(str(bumped["id"]), state="accepted", decided_by="operator")
    third = store.create_or_bump_suggestion(
        {
            "title": "Review underperforming ad sets",
            "evidence": [{"metric": "roas", "value": 0.1}],
            "risk_class": "read_only",
            "source": "alerts",
            "alert_id": 3,
        }
    )
    assert third["id"] != bumped["id"]
    assert len(third["evidence"]) == 1
    assert store.list_suggestions(state="open") == [third]
    assert len(store.list_suggestions()) == 2


def test_zero_magnitude_does_not_defeat_cooldown(store: StewardStore) -> None:
    # RR-PROD-001: a $0-revenue day emits magnitude 0.0; the escalation check must NOT
    # treat 0.0 >= 0.0 * 1.5 as an escalation, or the flagship revenue alert re-fires
    # every 15-min cycle instead of being cooldown-throttled.
    base = {
        "rule": "revenue_drop",
        "severity": "critical",
        "title": "Revenue at zero",
        "cooldown_key": "revenue_drop",
        "cooldown_minutes": 240,
        "magnitude": 0.0,
    }
    first = store.create_alert(dict(base))
    assert first is not None
    # Two more identical zero-magnitude cycles within cooldown must both suppress.
    assert store.create_alert(dict(base)) is None
    assert store.create_alert(dict(base)) is None
    assert store.open_alert_count() == 1

    # A genuine magnitude jump from a POSITIVE baseline still escalates through cooldown
    # (the fix guards only the zero-vs-zero degenerate case, not real escalations).
    spend_base = {
        "rule": "spend_spike",
        "severity": "high",
        "title": "Spend up 30%",
        "cooldown_key": "spend_spike",
        "cooldown_minutes": 240,
        "magnitude": 300.0,
    }
    assert store.create_alert(dict(spend_base)) is not None
    same = {**spend_base, "title": "Spend still up ~30%", "magnitude": 320.0}
    assert store.create_alert(dict(same)) is None  # 320 < 300*1.5 -> suppressed
    worse = {**spend_base, "title": "Spend up 80%", "magnitude": 800.0}
    assert store.create_alert(dict(worse)) is not None  # 800 >= 300*1.5 -> escalation


def test_suggestion_lifecycle(store: StewardStore) -> None:
    suggestion = store.create_suggestion(
        {
            "title": "Pause ads",
            "rationale": "ROAS fell",
            "evidence": [{"metric": "roas", "value": 0.8}],
            "proposed_plan": [{"action": "pause"}],
            "risk_class": "external_reversible",
            "source": "alerts",
        }
    )
    sid = suggestion["id"]
    assert sid.startswith("sug_") and store.get_suggestion(sid) == suggestion
    assert store.list_suggestions("open") == [suggestion]
    decided = store.decide_suggestion(
        sid, state="accepted", decided_by="operator", task_id="tsk_1", run_id="run_1"
    )
    assert decided is not None and decided["state"] == "accepted"
    assert store.decide_suggestion(sid, state="rejected", decided_by="operator") is None
    outcome = store.record_suggestion_outcome(sid, {"result": "improved"})
    assert outcome is not None and outcome["outcome"] == {"result": "improved"}
    assert store.record_suggestion_outcome("missing", {}) is None


def test_suggestion_claim_release_is_atomic(store: StewardStore) -> None:
    # H9 approve path: claim_suggestion must atomically move open->approving and
    # RETURN the row (Wave A shipped a SET updated=? against a non-existent column,
    # which raised on every call — this is the missing regression guard).
    suggestion = store.create_suggestion(
        {"title": "Scale winners", "risk_class": "read_only", "source": "goals"}
    )
    sid = suggestion["id"]

    claimed = store.claim_suggestion(sid)
    assert claimed is not None and claimed["state"] == "approving"
    # Only one claimer wins: a second claim on the now-'approving' row returns None.
    assert store.claim_suggestion(sid) is None
    # Release rolls back to open; a second release is a no-op (None).
    released = store.release_suggestion(sid)
    assert released is not None and released["state"] == "open"
    assert store.release_suggestion(sid) is None
    # After release the suggestion is claimable again.
    assert store.claim_suggestion(sid) is not None
    # decide_suggestion finalizes from the 'approving' state.
    decided = store.decide_suggestion(sid, state="approved", decided_by="operator", run_id="run_x")
    assert decided is not None and decided["state"] == "approved" and decided["run_id"] == "run_x"


def test_briefing_lifecycle(store: StewardStore) -> None:
    first = store.insert_briefing(
        {
            "briefing_date": "2026-07-12",
            "vault_path": "briefings/2026-07-12.md",
            "summary": {"headline": "First"},
            "deliveries": [{"channel": "slack"}],
        }
    )
    second = store.insert_briefing(
        {"briefing_date": "2026-07-13", "summary": {"headline": "Second"}}
    )
    assert first["id"] == "brf_2026-07-12"
    assert store.latest_briefing() == second
    assert store.list_briefings(limit=1) == [second]
    acked = store.ack_briefing(second["id"])
    assert acked is not None and acked["acked_at"]
    assert store.ack_briefing("missing") is None


def _suggestion(title: str, value: float, **fields: object) -> dict[str, object]:
    return {
        "title": title,
        "rationale": "Suggested by alert rule roas_floor.",
        "evidence": [{"metric": "roas", "value": value}],
        "risk_class": "read_only",
        "source": "alerts",
        **fields,
    }


def test_suggestion_dedupe_finds_an_open_row_beyond_the_first_page(
    store: StewardStore,
) -> None:
    """Dedupe must not be paginated -- it fails exactly when the backlog is worst.

    The lookup used to scan ``list_suggestions(state="open")``, whose default
    ``limit=100`` hid every open suggestion past the first page. With 101+ open
    suggestions the duplicate title landed on page two and the "dedupe" inserted
    a second row for it -- the pile-up it exists to prevent, reappearing only
    once there is a pile-up.
    """
    target = store.create_or_bump_suggestion(
        _suggestion("Review underperforming ad sets", 0.5, created_at="2026-01-01T00:00:00Z")
    )
    # Push the target well past page one: list_suggestions orders by created_at
    # DESC, so newer rows crowd it out. Explicit timestamps, because 150 rows
    # written in the same second would tie-break on a random id.
    for index in range(150):
        store.create_suggestion(
            _suggestion(
                f"Filler {index}",
                1.0,
                created_at=f"2026-02-01T00:{index // 60:02d}:{index % 60:02d}Z",
            )
        )
    assert len(store.list_suggestions(state="open")) == 100  # the page the old code saw
    assert target["id"] not in {row["id"] for row in store.list_suggestions(state="open")}

    bumped = store.create_or_bump_suggestion(_suggestion("Review underperforming ad sets", 0.2))
    assert bumped["id"] == target["id"], "a duplicate title beyond the first page inserted a row"
    assert len(bumped["evidence"]) == 2
    assert suggestion_occurrences(bumped) == 2
    titled = [
        row
        for row in store.list_suggestions(state="open", limit=1000)
        if row["title"] == "Review underperforming ad sets"
    ]
    assert len(titled) == 1


def test_open_suggestion_by_title_matches_only_open_rows(store: StewardStore) -> None:
    created = store.create_or_bump_suggestion(_suggestion("Rotate creatives", 0.4))
    assert store.open_suggestion_by_title("Rotate creatives")["id"] == created["id"]  # type: ignore[index]
    assert store.open_suggestion_by_title("rotate creatives") is None  # exact match, by design
    store.decide_suggestion(str(created["id"]), state="accepted", decided_by="operator")
    assert store.open_suggestion_by_title("Rotate creatives") is None


def test_bumped_suggestion_evidence_is_capped_and_the_count_survives(
    store: StewardStore,
) -> None:
    """Evidence is bounded; the occurrence count is not, and is not len(evidence).

    A rule that fires every cycle used to grow one JSON blob forever. Capping it
    without an explicit counter would have replaced unbounded growth with a
    quiet lie -- "fired 25 times" for a rule on its 60th firing.
    """
    store.create_or_bump_suggestion(_suggestion("Pause spend", 0.0))
    for index in range(59):
        store.create_or_bump_suggestion(_suggestion("Pause spend", float(index)))

    current = store.open_suggestion_by_title("Pause spend")
    assert current is not None
    assert len(current["evidence"]) == _SUGGESTION_EVIDENCE_MAX
    # The most recent firings are the ones kept.
    assert current["evidence"][-1] == {"metric": "roas", "value": 58.0}
    assert suggestion_occurrences(current) == 60
    assert current["outcome"]["_case"]["evidence_kept"] == _SUGGESTION_EVIDENCE_MAX


def test_a_legacy_row_without_a_counter_keeps_counting_from_its_evidence(
    store: StewardStore,
) -> None:
    """Rows written before the counter existed carry the count in their evidence."""
    legacy = store.create_suggestion(
        {
            "title": "Legacy suggestion",
            "evidence": [{"n": 1}, {"n": 2}, {"n": 3}],
            "risk_class": "read_only",
            "source": "alerts",
        }
    )
    assert legacy["outcome"] == {}
    bumped = store.create_or_bump_suggestion(_suggestion("Legacy suggestion", 4.0))
    assert bumped["id"] == legacy["id"]
    assert suggestion_occurrences(bumped) == 4


def test_recording_an_outcome_keeps_the_dedupe_bookkeeping(store: StewardStore) -> None:
    """The outcome blob belongs to its writer; ``_case`` inside it belongs here."""
    created = store.create_or_bump_suggestion(_suggestion("Refresh audiences", 0.3))
    store.create_or_bump_suggestion(_suggestion("Refresh audiences", 0.2))
    recorded = store.record_suggestion_outcome(str(created["id"]), {"dismiss_reason": "noise"})
    assert recorded is not None
    assert recorded["outcome"]["dismiss_reason"] == "noise"
    assert suggestion_occurrences(recorded) == 2
