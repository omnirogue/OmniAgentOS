"""Outbound queue DMs and event-watch behavior use a mocked Slack seam."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import omniagentos.team.notify as notify
from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore, append_task_event
from omniagentos.team.store import EVENTS_TABLE, TeamStore


class _Notifier:
    def __init__(self) -> None:
        self.dms: list[tuple[str, str]] = []
        self.channels: list[str] = []

    def post_dm(self, user: str, text: str, *, blocks=None, color=None) -> bool:
        self.dms.append((user, text))
        return True

    def post_channel(self, text: str, *, blocks=None, color=None) -> bool:
        self.channels.append(text)
        return True


class TestMorning:
    def test_content_includes_buckets_warning_and_pool(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        make_card(
            title="Claimed task",
            ref="U1",
            owner_employee_id=employees["bob"],
            status=BoardTaskStatus.CLAIMED,
        )
        make_card(title="Ready task", ref="U2", owner_employee_id=employees["bob"])
        make_card(title="Pool task", ref="P1")
        monkeypatch.setattr(notify, "load_slack_map", lambda: {"US": employees["bob"]})
        monkeypatch.setattr(
            notify,
            "_pool_cards",
            lambda store: [{"id": "btk_pool", "ref": "P1", "title": "Pool task", "status": "open"}],
        )
        outbound = _Notifier()

        assert notify.run_morning(team_store, outbound) is True  # type: ignore[arg-type]
        assert outbound.dms[0][0] == "US"
        text = outbound.dms[0][1]
        assert "Claimed task" in text
        assert "Ready task" in text
        assert "Capacity: 1 of 5 active — room for more." in text
        assert "Pool task" in text
        assert "reply `claim <REF>`" in text

    def test_warning_uses_the_active_only_contract_not_ready_cards(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for number in range(5):
            make_card(
                title=f"Ready {number}", ref=f"U{number}", owner_employee_id=employees["bob"]
            )
        monkeypatch.setattr(notify, "load_slack_map", lambda: {"US": employees["bob"]})
        outbound = _Notifier()

        notify.run_morning(team_store, outbound)  # type: ignore[arg-type]
        assert "Capacity: 0 of 5 active — room for more." in outbound.dms[0][1]

    def test_unmapped_employees_are_skipped(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        make_card(title="Frank's card", ref="A1", owner_employee_id=employees["andy"])
        monkeypatch.setattr(notify, "load_slack_map", lambda: {})
        outbound = _Notifier()

        assert notify.run_morning(team_store, outbound) is True  # type: ignore[arg-type]
        assert outbound.dms == []
        assert "no Slack mapping" in capsys.readouterr().err

    def test_pool_helper_uses_limit_and_old_signature_falls_back(
        self, team_store: TeamStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cards = [
            {"id": f"btk_{index}", "title": str(index), "status": "open"} for index in range(8)
        ]

        def modern(*, limit: int | None = None) -> list[dict[str, str]]:
            assert limit == 5
            return cards[:limit]

        monkeypatch.setattr(team_store, "pool_cards", modern, raising=False)
        assert len(notify._pool_cards(team_store) or []) == 5

        def legacy() -> list[dict[str, str]]:
            return cards

        monkeypatch.setattr(team_store, "pool_cards", legacy, raising=False)
        assert len(notify._pool_cards(team_store) or []) == 5

    def test_pool_failure_degrades_only_the_pool_section(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        make_card(title="assigned", owner_employee_id=employees["bob"])

        def broken(*, limit: int | None = None) -> list[dict[str, str]]:
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(team_store, "pool_cards", broken, raising=False)
        monkeypatch.setattr(notify, "load_slack_map", lambda: {"US": employees["bob"]})
        assert "pool unavailable" in notify.morning_messages(team_store)[0][2]


class TestPulse:
    def test_pulse_includes_floor_blocked_oldest_and_low_pool(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        blocked = make_card(title="Oldest blocker", ref="B1", owner_employee_id=employees["bob"])
        collab_store.update_board_task(
            blocked.id, {"status": BoardTaskStatus.BLOCKED, "blocked_reason": "waiting"}
        )
        make_card(title="Later blocker", ref="B2", owner_employee_id=employees["bob"])
        queues = team_store.team_queues()

        text = notify.render_pulse_message(
            queues,
            {"US": employees["bob"]},
            [{"id": "pool", "ref": "P1", "title": "Pool task"}],
            2,
        )

        # v4 Work-vs-Tasks: the person line compresses the count segments into
        # one ongoing-Work figure (queued 1 + blocked 1 = 2) with the ⚠ floor.
        assert "👤 Bob — 🔧 Work 2/5 ⚠" in text
        assert 'blocked 1 B1 "Oldest blocker"' in text
        assert f"Pool: 2 ⚠ low (<{notify.POOL_DEPTH_FLOOR})" in text
        assert text.splitlines()[-1] == notify.TASK_FOOTER

    def test_overnight_suggestions_include_queue_work_and_empty_queue(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        make_card(title="Ready overnight", ref="R1", owner_employee_id=employees["bob"])
        blocked = make_card(
            title="Blocked overnight", ref="B1", owner_employee_id=employees["bob"]
        )
        collab_store.update_board_task(
            blocked.id, {"status": BoardTaskStatus.BLOCKED, "blocked_reason": "waiting"}
        )

        # Two pool cards: suggestions are allocated positionally (person index →
        # pool index), and Bob renders first in the fixed person order.
        text = notify.render_pulse_message(
            team_store.team_queues(),
            {"US": employees["bob"], "UA": employees["andy"]},
            [
                {"id": "pool", "ref": "P1", "title": "Claim this"},
                {"id": "pool2", "ref": "P2", "title": "Also this"},
            ],
            1,
            overnight=True,
        )

        assert "*Overnight suggestions*" in text
        assert 'start an overnight loop on R1 "Ready overnight"' in text
        assert 'claim P1 "Claim this"' in text  # Bob is below the active floor
        assert 'unblock tonight? B1 "Blocked overnight"' in text
        assert 'queue clear — grab from pool P2 "Also this"' in text  # Frank is empty

    def test_overnight_section_requires_the_explicit_flag(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        queues = team_store.team_queues()
        slack_map = {"US": employees["bob"]}
        plain = notify.render_pulse_message(queues, slack_map, [], 0)
        overnight = notify.render_pulse_message(queues, slack_map, [], 0, overnight=True)

        assert "Overnight suggestions" not in plain
        assert "Overnight suggestions" in overnight

    def test_cli_only_enables_overnight_with_its_explicit_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[bool] = []
        monkeypatch.setattr(notify, "TeamStore", lambda db: object())
        monkeypatch.setattr(notify, "_daybrief_sent_today", lambda day: True)
        monkeypatch.setattr(
            notify,
            "run_pulse",
            lambda store, notifier, **kwargs: seen.append(kwargs["overnight"]) or True,
        )

        assert notify.main(["--pulse", "--dry-run", "--db", "unused.db"]) == 0
        assert notify.main(["--pulse", "--overnight", "--dry-run", "--db", "unused.db"]) == 0
        assert seen == [False, True]

    def test_pulse_dry_run_prints_channel_json(
        self,
        team_store: TeamStore,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(
            notify, "pulse_payload", lambda store, **kwargs: ("pulse text", [], "#2eb67d")
        )
        monkeypatch.setenv(notify.CHANNEL_ENV, "CPULSE")
        monkeypatch.setenv(notify.PULSE_CHANNEL_ENV, "CPULSEOVERRIDE")

        assert notify.run_pulse(team_store, None, dry_run=True) is True

        assert json.loads(capsys.readouterr().out) == {
            "channel": "CPULSEOVERRIDE",
            "text": "pulse text",
            "blocks": 0,
            "color": "#2eb67d",
        }

    def test_pulse_uses_pool_depth_contract_floor(
        self, team_store: TeamStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The footer is always the last line; the pool line sits above it.
        monkeypatch.setattr(team_store, "pool_depth", lambda: 9, raising=False)
        assert "low" in notify.pulse_message(team_store).splitlines()[-2]
        monkeypatch.setattr(team_store, "pool_depth", lambda: 10, raising=False)
        assert "low" not in notify.pulse_message(team_store).splitlines()[-2]

    def test_unmapped_pulse_member_is_logged(self, capsys: pytest.CaptureFixture[str]) -> None:
        notify.render_pulse_message(
            {"emp_missing": notify.TeamQueueBuckets(employee_id="emp_missing")}, {}, [], 10
        )
        assert "pulse skip 'emp_missing': no Slack mapping" in capsys.readouterr().err

    def test_overnight_pool_card_is_not_reused_or_double_claimed(self) -> None:
        queues = {
            employee_id: notify.TeamQueueBuckets(employee_id=employee_id)
            for employee_id in ("emp_a", "emp_b")
        }
        text = notify.render_pulse_message(
            queues,
            {"UA": "emp_a", "UB": "emp_b"},
            [{"id": "btk_one", "ref": None, "title": "One"}],
            1,
            overnight=True,
        )
        assert text.count("btk_one") == 1
        # The claim CTA appears once per suggestion line; the footer (which
        # legitimately says "claim" twice) is excluded from the per-card check.
        assert all(
            line.count("claim") <= 1
            for line in text.splitlines()
            if line != notify.TASK_FOOTER
        )

    def test_pulse_marks_up_to_two_urgent_cards_and_warns_below_the_work_floor(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        # Explicit created_at: three cards made in the same second would tie and
        # fall through to the id tie-break, so "the two oldest fires" would be
        # whichever uuids sorted first.
        make_card(title="Routine", ref="N1", owner_employee_id=employees["bob"])
        for ref, title, created_at in (
            ("U1", "Outage in checkout", "2026-08-01T00:00:00Z"),
            ("U2", "Second fire", "2026-08-02T00:00:00Z"),
            ("U3", "Third fire", "2026-08-03T00:00:00Z"),
        ):
            make_card(
                title=title,
                ref=ref,
                owner_employee_id=employees["bob"],
                priority="urgent",
                created_at=created_at,
            )

        text = notify.render_pulse_message(
            team_store.team_queues(), {"US": employees["bob"]}, [], 10
        )

        assert "\U0001f525 U1 Outage in checkout" in text
        assert "\U0001f525 U2 Second fire" in text
        # Never more than two 🔥 markers on one line — U3 may appear in the
        # compact queued-refs run, but it never earns a marker of its own.
        assert text.count("\U0001f525") == 2
        assert "\U0001f525 U3" not in text
        # v4: the ready-floor warning is superseded by the ongoing-Work floor
        # (4 open cards -> Work 4/5, supply visibility, never a block).
        assert "\U0001f527 Work 4/5 \u26a0" in text

    def test_an_urgent_card_with_no_ref_is_marked_by_its_id_prefix(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        queues = team_store.team_queues()
        queues[employees["bob"]].ready.append(
            notify.QueueCard(
                id="btk_abc123456",
                title="x" * 60,
                status="open",
                priority="urgent",
            )
        )

        text = notify.render_pulse_message(queues, {"US": employees["bob"]}, [], 10)

        assert "\U0001f525 btk_abc1 " + "x" * 40 in text
        assert "x" * 41 not in text

    def test_a_full_work_load_gets_no_floor_warning(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        for index in range(notify.ACTIVE_QUEUE_FLOOR):
            make_card(
                title=f"Ready {index}",
                ref=f"F{index}",
                owner_employee_id=employees["bob"],
            )

        text = notify.render_pulse_message(
            team_store.team_queues(), {"US": employees["bob"]}, [], 10
        )

        assert f"🔧 Work {notify.ACTIVE_QUEUE_FLOOR}/{notify.ACTIVE_QUEUE_FLOOR}" in text
        assert "⚠" not in text.splitlines()[1]  # the person line carries no floor warning

    def test_pulse_titles_are_sanitized(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        queues = team_store.team_queues()
        queues[employees["bob"]].blocked.append(
            notify.QueueCard(
                id="btk_blocked",
                ref="B1",
                title="https://example.test xoxb-secret",
                status="blocked",
            )
        )

        text = notify.render_pulse_message(queues, {"US": employees["bob"]}, [], 0)

        assert "https://" not in text
        assert "xoxb-" not in text


class TestWatch:
    def test_assignment_by_other_sends_dm_and_cursor_makes_rerun_idempotent(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        card = make_card(title="Assigned by Alice", ref="U9")
        cursor = tmp_path / "cursor.json"
        notify._write_cursor(cursor, notify._latest_event_rowid(team_store))
        collab_store.update_board_task(
            card.id, {"owner_employee_id": employees["bob"]}, actor=employees["alice"]
        )
        # update_board_task appends the C3v2 `assign` event itself (p1's
        # _append_update_event) — hand-appending a second one here would make
        # the watcher deliver the same assignment twice.
        collab_store._store._connection.commit()
        monkeypatch.setattr(notify, "load_slack_map", lambda: {"US": employees["bob"]})
        outbound = _Notifier()

        assert notify.run_watch_once(team_store, outbound, cursor_file=cursor) is True  # type: ignore[arg-type]
        assert len(outbound.dms) == 1
        assert 'You\'ve been assigned U9 "Assigned by Alice"' in outbound.dms[0][1]
        persisted = json.loads(cursor.read_text())
        assert persisted["rowid"]
        assert notify.run_watch_once(team_store, outbound, cursor_file=cursor) is True  # type: ignore[arg-type]
        assert len(outbound.dms) == 1

    def test_self_assignment_does_not_send_a_dm(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        card = make_card(title="Self assigned", ref="U10")
        cursor = tmp_path / "cursor.json"
        collab_store.update_board_task(
            card.id, {"owner_employee_id": employees["bob"]}, actor=employees["bob"]
        )
        monkeypatch.setattr(notify, "load_slack_map", lambda: {"US": employees["bob"]})
        outbound = _Notifier()

        assert notify.run_watch_once(team_store, outbound, cursor_file=cursor) is True  # type: ignore[arg-type]
        assert outbound.dms == []

    def test_inference_nudge_is_terse_and_never_uses_the_card_body(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
        tmp_path: Path,
    ) -> None:
        card = BoardTask(
            title="Fix webhook retries",
            description="body https://example.test/private?token=xoxb-secret",
            ref="U12",
            owner_employee_id=employees["bob"],
            source="inference-github",
        )
        cursor = tmp_path / "cursor.json"
        notify._write_cursor(cursor, notify._latest_event_rowid(team_store))
        collab_store.create_board_task(card, actor="inference")
        outbound = _Notifier()

        assert notify.run_watch_once(team_store, outbound, cursor_file=cursor) is True  # type: ignore[arg-type]
        assert len(outbound.channels) == 1
        assert '"Fix webhook retries"' in outbound.channels[0]
        assert "https://" not in outbound.channels[0]
        assert "xoxb-" not in outbound.channels[0]
        assert "body" not in outbound.channels[0]


def test_dm_channel_is_opened_once_and_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_call(
        url: str, payload: dict[str, Any], token: str, *, timeout: int = 30
    ) -> dict[str, Any]:
        calls.append((url, payload))
        if url == notify._CONVERSATIONS_OPEN_URL:
            return {"ok": True, "channel": {"id": "D123"}}
        return {"ok": True}

    monkeypatch.setattr(notify, "_slack_call", fake_call)
    client = notify.SlackNotifier("token")

    assert client.post_dm("UUSER", "first") is True
    assert client.post_dm("UUSER", "second") is True
    assert [url for url, _ in calls].count(notify._CONVERSATIONS_OPEN_URL) == 1
    assert [
        payload["channel"] for url, payload in calls if url == notify._CHAT_POST_MESSAGE_URL
    ] == [
        "D123",
        "D123",
    ]


def test_post_channel_uses_the_current_call_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_call(
        url: str, payload: dict[str, Any], token: str, *, timeout: int = 30
    ) -> dict[str, Any]:
        calls.append((url, payload))
        return {"ok": True}

    monkeypatch.setattr(notify, "_slack_call", fake_call)
    client = notify.SlackNotifier("token", channel="C123")

    assert client.post_channel("hello") is True
    assert calls[0][0] == notify._CHAT_POST_MESSAGE_URL
    assert calls[0][1]["channel"] == "C123"


def test_main_modes_accept_dry_runs_and_standalone_bootstrap(
    team_store: TeamStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = tmp_path / "cursor.json"
    loaded: list[bool] = []
    monkeypatch.setattr(notify, "TeamStore", lambda _db: team_store)
    monkeypatch.setattr(notify, "cursor_path", lambda _override=None: cursor)
    monkeypatch.setattr(notify, "load_slack_env", lambda: loaded.append(True))
    monkeypatch.setattr(notify, "run_morning", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(notify, "run_pulse", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(notify, "run_daybrief", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(notify, "run_watch_once", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(notify, "_daybrief_sent_today", lambda _day: True)

    db = str(tmp_path / "team_slack.db")
    assert notify.main(["--morning", "--dry-run", "--db", db]) == 0
    assert notify.main(["--watch-once", "--dry-run", "--db", db]) == 0
    assert notify.main(["--pulse", "--dry-run", "--db", db]) == 0
    assert notify.main(["--pulse", "--overnight", "--dry-run", "--db", db]) == 0
    assert notify.main(["--daybrief", "--dry-run", "--db", db]) == 0
    assert notify.main(["--daybrief", "--test", "--dry-run", "--db", db]) == 0
    assert notify.main(["--bootstrap", "--db", db]) == 0
    assert json.loads(cursor.read_text())["rowid"] >= 0
    assert len(loaded) == 7


class TestWatcherDurability:
    def _bootstrap(self, store: TeamStore, cursor: Path) -> None:
        notify._write_cursor(cursor, notify._latest_event_rowid(store))

    def test_same_second_events_after_cursor_all_arrive(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        tmp_path: Path,
    ) -> None:
        cursor = tmp_path / "cursor.json"
        for index in range(3):
            make_card(title=f"before {index}", ref=f"A{index}", source="inference")
        self._bootstrap(team_store, cursor)
        for index in range(3):
            card = make_card(title=f"after {index}", ref=f"B{index}", source="inference")
            append_task_event(
                collab_store._store._connection, task_id=card.id, actor="inference", event="create"
            )
        collab_store._store._connection.execute(
            f"UPDATE {EVENTS_TABLE} SET created_at = ?", ("2026-08-11T00:00:00Z",)
        )
        collab_store._store._connection.commit()
        outbound = _Notifier()
        assert notify.run_watch_once(team_store, outbound, cursor_file=cursor) is True  # type: ignore[arg-type]
        assert len(outbound.channels) == 3
        assert all(f'"after {index}"' in outbound.channels[index] for index in range(3))

    def test_backdated_events_scan_in_cursor_order_without_replay(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        tmp_path: Path,
    ) -> None:
        cursor = tmp_path / "cursor.json"
        self._bootstrap(team_store, cursor)
        first = BoardTask(title="first", source="inference")
        second = BoardTask(title="backdated", source="inference")
        collab_store.create_board_task(first, actor="inference")
        collab_store.create_board_task(second, actor="inference")
        newest = collab_store._store._connection.execute(
            f"SELECT MAX(rowid) AS rowid FROM {EVENTS_TABLE}"
        ).fetchone()
        collab_store._store._connection.execute(
            f"UPDATE {EVENTS_TABLE} SET created_at = ? WHERE rowid = ?",
            ("2000-01-01T00:00:00Z", int(newest["rowid"])),
        )
        collab_store._store._connection.commit()

        outbound = _Notifier()
        assert notify.run_watch_once(team_store, outbound, cursor_file=cursor) is True  # type: ignore[arg-type]
        first_cursor = json.loads(cursor.read_text())["rowid"]
        assert len(outbound.channels) == 2
        assert notify.run_watch_once(team_store, outbound, cursor_file=cursor) is True  # type: ignore[arg-type]
        assert outbound.channels and len(outbound.channels) == 2
        assert json.loads(cursor.read_text())["rowid"] >= first_cursor

    def test_fresh_cursor_bootstraps_without_replaying(
        self, team_store: TeamStore, make_card: Callable[..., BoardTask], tmp_path: Path
    ) -> None:
        make_card(title="historical", source="inference")
        cursor = tmp_path / "cursor.json"
        outbound = _Notifier()
        assert notify.run_watch_once(team_store, outbound, cursor_file=cursor) is True  # type: ignore[arg-type]
        assert outbound.channels == []
        assert json.loads(cursor.read_text())["rowid"] > 0

    def test_corrupt_cursor_is_loud_and_never_replays(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        make_card(title="historical", source="inference")
        cursor = tmp_path / "cursor.json"
        cursor.write_text('{"rowid":')
        outbound = _Notifier()
        assert notify.run_watch_once(team_store, outbound, cursor_file=cursor) is False  # type: ignore[arg-type]
        assert outbound.channels == []
        assert "corrupt cursor" in capsys.readouterr().err

    def test_terminal_error_skips_once_and_advances(
        self, team_store: TeamStore, make_card: Callable[..., BoardTask], tmp_path: Path
    ) -> None:
        cursor = tmp_path / "cursor.json"
        self._bootstrap(team_store, cursor)
        bad = make_card(title="bad", source="inference")
        good = make_card(title="good", source="inference")
        append_task_event(team_store._connection, task_id=bad.id, actor="inference", event="create")
        append_task_event(
            team_store._connection, task_id=good.id, actor="inference", event="create"
        )
        team_store._connection.commit()

        class Terminal(_Notifier):
            last_error = "channel_not_found"

            def post_channel(self, text: str, *, blocks=None, color=None) -> bool:
                self.channels.append(text)
                return "bad" not in text

        outbound = Terminal()
        assert notify.run_watch_once(team_store, outbound, cursor_file=cursor) is True  # type: ignore[arg-type]
        assert len(outbound.channels) == 2
        assert notify.run_watch_once(team_store, outbound, cursor_file=cursor) is True  # type: ignore[arg-type]
        assert len(outbound.channels) == 2

    def test_transient_error_retries_then_parks(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cursor = tmp_path / "cursor.json"
        self._bootstrap(team_store, cursor)
        retry = make_card(title="retry", source="inference")
        append_task_event(
            team_store._connection, task_id=retry.id, actor="inference", event="create"
        )
        team_store._connection.commit()
        sleeps: list[float] = []
        monkeypatch.setattr(notify.time, "sleep", sleeps.append)

        class Transient(_Notifier):
            last_error = "rate_limited"
            retry_after = 0

            def post_channel(self, text: str, *, blocks=None, color=None) -> bool:
                self.channels.append(text)
                return False

        outbound = Transient()
        assert notify.run_watch_once(team_store, outbound, cursor_file=cursor) is False  # type: ignore[arg-type]
        assert len(outbound.channels) == 3
        assert sleeps == [0.0, 0.0]

    def test_c3v2_assignment_note_routes_each_event_to_its_named_owner(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        card = make_card(title="reassigned", ref="R1")
        cursor = tmp_path / "cursor.json"
        self._bootstrap(team_store, cursor)
        # An assign-shaped legacy event with no C3v2 owner token is a field
        # edit, not an assignment notification.
        append_task_event(
            collab_store._store._connection,
            task_id=card.id,
            actor=employees["alice"],
            event="assign",
            note="parent_task_id",
        )
        append_task_event(
            collab_store._store._connection,
            task_id=card.id,
            actor=employees["alice"],
            event="assign",
            note=f"owner:{employees['bob']}",
        )
        append_task_event(
            collab_store._store._connection,
            task_id=card.id,
            actor=employees["alice"],
            event="assign",
            note=f"owner:{employees['andy']}",
        )
        collab_store._store._connection.commit()
        monkeypatch.setattr(
            notify,
            "load_slack_map",
            lambda: {"UBOB": employees["bob"], "UANDY": employees["andy"]},
        )
        outbound = _Notifier()
        assert notify.run_watch_once(team_store, outbound, cursor_file=cursor) is True  # type: ignore[arg-type]
        assert [recipient for recipient, _ in outbound.dms] == ["UBOB", "UANDY"]
