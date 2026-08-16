"""Numbered confirm-first repair decisions: registration, replies, execution."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from omniagentos.team import decisions
from omniagentos.team.session_tracker import Overall


@pytest.fixture()
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "var").mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path / "var"))
    return tmp_path


class _Notifier:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def post_channel(self, text: str, **kwargs: Any) -> bool:
        self.posts.append(text)
        return True


class _DmNotifier(_Notifier):
    def open_dm(self, slack_user_id: str) -> str | None:
        assert slack_user_id == "UOWNER"
        return "DOWNER"

    def post_dm(self, slack_user_id: str, text: str, **kwargs: Any) -> bool:
        self.posts.append(f"{slack_user_id}:{text}")
        return True


def _pending_edc(repo_root: Path) -> None:
    state = decisions.load_state(repo_root)
    state["next_number"] = 2
    state["decisions"].append(
        {
            "number": 1,
            "kind": "edc",
            "dedup_key": "edc:dcn_1:sha",
            "title": "Reply to customer",
            "payload": {
                "decision_id": "dcn_1",
                "owner_employee_id": "emp_owner",
                "owner_slack_id": "UOWNER",
                "dm_channel": "DOWNER",
                "action_sha": "sha",
            },
            "status": "pending",
            "proposed_at": decisions._iso(decisions._utcnow()),
            "decided_by": None,
            "decided_at": None,
            "result": None,
        }
    )
    decisions.save_state(repo_root, state)


def _red() -> Overall:
    return Overall(bottleneck="merge gate red (x)", failed_merges_last_hour=2, merge_queue=1)


def test_register_numbers_monotonic_and_deduped(repo_root: Path) -> None:
    first = decisions.register_repair_proposals(repo_root, _red())
    again = decisions.register_repair_proposals(repo_root, _red())
    assert [d["number"] for d in first] == [1]
    assert [d["number"] for d in again] == [1]  # deduped while pending, not renumbered
    healthy = decisions.register_repair_proposals(repo_root, Overall(bottleneck="none"))
    assert [d["number"] for d in healthy] == [1]  # pending survives a healthy pass


def test_render_carries_reply_instructions(repo_root: Path) -> None:
    pending = decisions.register_repair_proposals(repo_root, _red())
    text = decisions.render_proposals(pending)
    assert "1." in text and "`1 yes`" in text


def test_reply_parsing_authorization_and_variants() -> None:
    roster = {"UOWNER": "emp_owner"}
    messages = [
        {"user": "UOWNER", "text": "1 yes"},
        {"user": "UOUTSIDER", "text": "2 yes"},
        {"user": "UOWNER", "text": "repair 3 NO"},
        {"user": "UOWNER", "text": "unrelated chatter 4 maybe"},
    ]
    replies = decisions.collect_replies(messages, roster)
    assert replies == [(3, False, "emp_owner", "repair"), (1, True, "emp_owner", None)]


def test_edc_yes_from_non_owner_is_ignored(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pending_edc(repo_root)
    monkeypatch.setattr(
        decisions,
        "_slack_history",
        lambda token, channel, oldest: [{"user": "UBOB", "text": "edc 1 yes"}],
    )
    monkeypatch.setattr(
        decisions,
        "load_slack_map",
        lambda: {"UOWNER": "emp_owner", "UBOB": "emp_bob"},
    )
    stats = decisions.process_replies(repo_root, notifier=_DmNotifier(), token="t", channel="CTEAM")
    assert stats == {"approved": 0, "declined": 0, "executed": 0, "failed": 0}
    assert decisions.load_state(repo_root)["decisions"][0]["status"] == "pending"


def test_edc_kind_hint_cannot_authorize_another_kind(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pending_edc(repo_root)
    monkeypatch.setattr(
        decisions,
        "_slack_history",
        lambda token, channel, oldest: [{"user": "UOWNER", "text": "repair 1 yes"}],
    )
    monkeypatch.setattr(decisions, "load_slack_map", lambda: {"UOWNER": "emp_owner"})
    stats = decisions.process_replies(repo_root, notifier=_DmNotifier(), token="t", channel="CTEAM")
    assert stats == {"approved": 0, "declined": 0, "executed": 0, "failed": 0}
    assert decisions.load_state(repo_root)["decisions"][0]["status"] == "pending"


def test_dm_history_error_is_distinct_from_valid_no_reply(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pending_edc(repo_root)
    monkeypatch.setattr(decisions, "load_slack_map", lambda: {"UOWNER": "emp_owner"})
    monkeypatch.setattr(decisions, "_slack_history", lambda token, channel, oldest: [])
    no_reply = decisions.process_replies(
        repo_root, notifier=_DmNotifier(), token="t", channel="CTEAM"
    )
    assert "history_errors" not in no_reply

    def unavailable(token: str, channel: str, oldest: float) -> list[dict[str, Any]]:
        raise decisions.SlackHistoryError("ratelimited")

    monkeypatch.setattr(decisions, "_slack_history", unavailable)
    fetch_error = decisions.process_replies(
        repo_root, notifier=_DmNotifier(), token="t", channel="CTEAM"
    )
    assert fetch_error["history_errors"] == 1
    assert decisions.load_state(repo_root)["decisions"][0]["status"] == "pending"


def test_yes_executes_filing_and_is_one_shot(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decisions.register_repair_proposals(repo_root, _red())
    filed: list[int] = []
    monkeypatch.setattr(
        decisions,
        "_file_repair_finding",
        lambda root, d: filed.append(d["number"]) or "sha256:" + "a" * 64,
    )
    monkeypatch.setattr(
        decisions,
        "_slack_history",
        lambda token, channel, oldest: [{"user": "UOWNER", "text": "1 yes"}],
    )
    monkeypatch.setattr(decisions, "load_slack_map", lambda: {"UOWNER": "emp_owner"})
    outbound = _Notifier()
    stats = decisions.process_replies(
        repo_root, notifier=outbound, token="t", channel="C", dry_run=False
    )
    assert stats == {"approved": 1, "declined": 0, "executed": 1, "failed": 0}
    assert filed == [1]
    assert "confirmed by emp_owner" in outbound.posts[0]
    # One-shot: a second identical pass finds nothing pending.
    stats2 = decisions.process_replies(
        repo_root, notifier=outbound, token="t", channel="C", dry_run=False
    )
    assert stats2 == {"approved": 0, "declined": 0, "executed": 0, "failed": 0}


def test_no_declines_without_executing(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    decisions.register_repair_proposals(repo_root, _red())
    monkeypatch.setattr(
        decisions,
        "_file_repair_finding",
        lambda root, d: (_ for _ in ()).throw(AssertionError("must not execute")),
    )
    monkeypatch.setattr(
        decisions,
        "_slack_history",
        lambda token, channel, oldest: [{"user": "UOWNER", "text": "1 no"}],
    )
    monkeypatch.setattr(decisions, "load_slack_map", lambda: {"UOWNER": "emp_owner"})
    outbound = _Notifier()
    stats = decisions.process_replies(repo_root, notifier=outbound, token="t", channel="C")
    assert stats["declined"] == 1 and stats["executed"] == 0
    assert "declined by emp_owner" in outbound.posts[0]
    state = decisions.load_state(repo_root)
    assert state["decisions"][0]["status"] == "declined"


def test_filing_failure_reports_and_marks_failed(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decisions.register_repair_proposals(repo_root, _red())
    monkeypatch.setattr(
        decisions,
        "_file_repair_finding",
        lambda root, d: (_ for _ in ()).throw(RuntimeError("envelope refused")),
    )
    monkeypatch.setattr(
        decisions,
        "_slack_history",
        lambda token, channel, oldest: [{"user": "UOWNER", "text": "1 yes"}],
    )
    monkeypatch.setattr(decisions, "load_slack_map", lambda: {"UOWNER": "emp_owner"})
    outbound = _Notifier()
    stats = decisions.process_replies(repo_root, notifier=outbound, token="t", channel="C")
    assert stats["failed"] == 1
    assert "FILING FAILED" in outbound.posts[0]


def test_pending_expires_after_24h(repo_root: Path) -> None:
    decisions.register_repair_proposals(repo_root, _red())
    state = decisions.load_state(repo_root)
    state["decisions"][0]["proposed_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 25 * 3600)
    )
    decisions.save_state(repo_root, state)
    pending = decisions.register_repair_proposals(repo_root, Overall(bottleneck="none"))
    assert pending == []


def test_crash_window_cannot_reexecute(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Approved is persisted BEFORE execution: a crash mid-filing never re-fires."""
    decisions.register_repair_proposals(repo_root, _red())
    calls: list[int] = []

    def crashy(root: Path, d: dict[str, Any]) -> str:
        calls.append(d["number"])
        raise KeyboardInterrupt  # simulates a hard crash mid-execution

    monkeypatch.setattr(decisions, "_file_repair_finding", crashy)
    monkeypatch.setattr(
        decisions,
        "_slack_history",
        lambda token, channel, oldest: [{"user": "UOWNER", "text": "1 yes"}],
    )
    monkeypatch.setattr(decisions, "load_slack_map", lambda: {"UOWNER": "emp_owner"})
    with pytest.raises(KeyboardInterrupt):
        decisions.process_replies(repo_root, notifier=_Notifier(), token="t", channel="C")
    # The decision was persisted as approved before the crash…
    assert decisions.load_state(repo_root)["decisions"][0]["status"] == "approved"
    # …so the next pass has nothing pending and never re-executes.
    stats = decisions.process_replies(repo_root, notifier=_Notifier(), token="t", channel="C")
    assert calls == [1]
    assert stats == {"approved": 0, "declined": 0, "executed": 0, "failed": 0}


def test_strict_reply_regex_rejects_trailing_prose() -> None:
    roster = {"UOWNER": "emp_owner"}
    messages = [
        {"user": "UOWNER", "text": "1 yes but wait no"},
        {"user": "UOWNER", "text": "overnight 2 no"},
        {"user": "UOWNER", "text": "repair 3 yes!"},
    ]
    assert decisions.collect_replies(messages, roster) == [
        (3, True, "emp_owner", "repair"),
        (2, False, "emp_owner", "overnight"),
    ]


def test_render_warns_about_threads_and_prefixes_numbers(repo_root: Path) -> None:
    pending = decisions.register_repair_proposals(repo_root, _red())
    text = decisions.render_proposals(pending)
    assert "not in a thread" in text
    assert "repair 1." in text


def test_concurrent_registrations_get_distinct_numbers(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BLOCKER repro (Sol): two registrars racing the shared next_number
    allocator with no lock both read the same stale number and the second
    save clobbers the first (last-write-wins) — one registration silently
    lost, both numbered 1. The fix makes load->modify->save atomic under a
    lock so concurrent registrations always get DISTINCT numbers and neither
    is lost.
    """
    original_expire = decisions._expire

    def slow_expire(state: dict[str, Any]) -> None:
        original_expire(state)
        time.sleep(0.2)  # widen the load/modify/save race window

    monkeypatch.setattr(decisions, "_expire", slow_expire)

    merge_gate = Overall(bottleneck="merge gate red (x)", failed_merges_last_hour=2, merge_queue=1)
    throughput = Overall(bottleneck="gate throughput (y)", merge_queue=5)
    errors: list[BaseException] = []

    def worker(overall: Overall) -> None:
        try:
            decisions.register_repair_proposals(repo_root, overall)
        except BaseException as exc:  # noqa: BLE001 — surface it to the assertion below
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=(merge_gate,))
    t2 = threading.Thread(target=worker, args=(throughput,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive(), "registration thread hung"
    assert not errors, errors

    state = decisions.load_state(repo_root)
    assert len(state["decisions"]) == 2, state["decisions"]  # neither registration lost
    numbers = sorted(d["number"] for d in state["decisions"])
    assert numbers == [1, 2]  # distinct numbers, no collision
    dedup_keys = {d["dedup_key"] for d in state["decisions"]}
    assert dedup_keys == {"repair:merge-gate-red", "repair:gate-throughput"}


def test_reply_kind_prefix_does_not_authorize_wrong_kind(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR repro (Sol): process_replies used to dispatch on the STORED kind
    and ignore the reply's own kind prefix, so a "repair 1 yes" reply could
    authorize a decision that is actually stored under a DIFFERENT kind at
    number 1 (e.g. an overnight decision that collided on the same number).
    A reply naming "repair 1" must never authorize a non-repair decision 1.
    """
    state = decisions.load_state(repo_root)
    state["next_number"] = 2
    state["decisions"].append(
        {
            "number": 1,
            "kind": "overnight",
            "dedup_key": "overnight:card-1:2026-08-11",
            "title": "Overnight session for card-1",
            "payload": {"employee_id": "emp_owner", "card_id": "card-1", "ref": "card-1"},
            "status": "pending",
            "proposed_at": decisions._iso(decisions._utcnow()),
            "decided_by": None,
            "decided_at": None,
            "result": None,
        }
    )
    decisions.save_state(repo_root, state)
    monkeypatch.setattr(
        decisions,
        "_file_repair_finding",
        lambda root, d: (_ for _ in ()).throw(AssertionError("must not execute")),
    )
    monkeypatch.setattr(
        decisions,
        "_slack_history",
        lambda token, channel, oldest: [{"user": "UOWNER", "text": "repair 1 yes"}],
    )
    monkeypatch.setattr(decisions, "load_slack_map", lambda: {"UOWNER": "emp_owner"})
    outbound = _Notifier()
    stats = decisions.process_replies(repo_root, notifier=outbound, token="t", channel="C")
    assert stats == {"approved": 0, "declined": 0, "executed": 0, "failed": 0}
    decision_after = decisions.load_state(repo_root)["decisions"][0]
    assert decision_after["number"] == 1
    assert decision_after["status"] == "pending"  # never authorized by the mismatched reply
    assert decision_after["decided_by"] is None


def test_authorization_only_decision_status_is_not_executed(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR repro (Sol): filing a repair finding only QUEUES it for the
    implementer loop, which drains findings asynchronously — it is not
    itself executing the repair. An authorize-and-file decision must not be
    stored as status "executed" when nothing has actually executed yet.
    """
    decisions.register_repair_proposals(repo_root, _red())
    monkeypatch.setattr(
        decisions,
        "_file_repair_finding",
        lambda root, d: "sha256:" + "a" * 64,
    )
    monkeypatch.setattr(
        decisions,
        "_slack_history",
        lambda token, channel, oldest: [{"user": "UOWNER", "text": "1 yes"}],
    )
    monkeypatch.setattr(decisions, "load_slack_map", lambda: {"UOWNER": "emp_owner"})
    decisions.process_replies(
        repo_root, notifier=_Notifier(), token="t", channel="C", dry_run=False
    )
    status = decisions.load_state(repo_root)["decisions"][0]["status"]
    assert status != "executed"
    assert status == "approved"  # authorized + filed, not (yet) executed
