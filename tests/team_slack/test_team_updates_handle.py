"""team_updates_handle: sender mapping, reply routing, and the socket hook's
never-break-ingestion guarantee.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

import omniagentos.team.slack_updates as slack_updates
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.comms.sockets import slack as slack_socket
from omniagentos.team.store import TeamStore

from .conftest import message_event


class _CapturePoster:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, channel: str, thread_ts: str, text: str) -> None:
        self.calls.append((channel, thread_ts, text))


class TestSenderMapping:
    def test_a_mapped_senders_command_is_applied_and_replied_to(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        slack_map: dict[str, str],
    ) -> None:
        make_card(title="Ship it", ref="U3", owner_employee_id=employees["bob"])
        poster = _CapturePoster()
        event = message_event(user="U0BOB", text="done U3 shipped it")

        slack_updates.team_updates_handle(
            event, collab=collab_store, team=team_store, slack_map=slack_map, poster=poster
        )

        assert len(poster.calls) == 1
        channel, thread_ts, text = poster.calls[0]
        assert channel == "C0000EXAMPLE"
        assert thread_ts == "1700000000.000100"  # falls back to ts, no thread_ts on the event
        assert "done" in text

        row = collab_store.get_board_task(
            [t for t in collab_store.list_board_tasks() if t["ref"] == "U3"][0]["id"]
        )
        assert row is not None
        assert row["status"] == "done"

    def test_an_unmapped_sender_is_ignored_and_logged(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        poster = _CapturePoster()
        event = message_event(user="U0STRANGER", text="done U3 shipped it")

        slack_updates.team_updates_handle(
            event, collab=collab_store, team=team_store, slack_map=slack_map, poster=poster
        )

        assert poster.calls == []
        captured = capsys.readouterr()
        assert "unmapped Slack sender" in captured.err
        assert "U0STRANGER" in captured.err

    def test_ordinary_chatter_is_silently_ignored(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        poster = _CapturePoster()
        event = message_event(user="U0BOB", text="anyone free for standup?")

        slack_updates.team_updates_handle(
            event, collab=collab_store, team=team_store, slack_map=slack_map, poster=poster
        )

        assert poster.calls == []
        # Chatter must never be noisy -- only an unmapped sender's COMMAND logs.
        assert capsys.readouterr().err == ""

    def test_thread_ts_is_honoured_when_present(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        slack_map: dict[str, str],
    ) -> None:
        make_card(title="Ship it", ref="U3", owner_employee_id=employees["bob"])
        poster = _CapturePoster()
        event = message_event(
            user="U0BOB", text="done U3 shipped it", thread_ts="1699999999.000000"
        )

        slack_updates.team_updates_handle(
            event, collab=collab_store, team=team_store, slack_map=slack_map, poster=poster
        )

        assert poster.calls[0][1] == "1699999999.000000"

    def test_report_command_replies_with_live_report_text(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
    ) -> None:
        poster = _CapturePoster()
        slack_updates.team_updates_handle(
            message_event(user="U0BOB", text="report"),
            collab=collab_store,
            team=team_store,
            slack_map=slack_map,
            poster=poster,
        )

        assert len(poster.calls) == 1
        assert poster.calls[0][2].startswith("DAILY PRODUCTION — ")


class TestPermalink:
    def test_strips_the_dot(self) -> None:
        assert (
            slack_updates.permalink("C0000EXAMPLE", "1700000000.000100")
            == "https://slack.com/archives/C0000EXAMPLE/p1700000000000100"
        )


class TestPostReplyNeverRaises:
    def test_no_token_is_a_stderr_line_not_an_exception(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        slack_updates.post_reply(
            "C0000EXAMPLE", "1700000000.000100", "hello", token_resolver=lambda: ""
        )
        assert "no SLACK_BOT_TOKEN" in capsys.readouterr().err


class TestSocketHook:
    def test_flag_off_never_invokes_the_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            slack_updates, "team_updates_handle", lambda event, **kw: calls.append(dict(event))
        )
        monkeypatch.delenv(slack_socket.TEAM_SLACK_UPDATES_FLAG_ENV, raising=False)

        slack_socket._maybe_handle_team_update(
            message_event(user="U0BOB", text="done U3 shipped it")
        )

        assert calls == []

    def test_flag_on_but_channel_mismatched_never_invokes_the_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            slack_updates, "team_updates_handle", lambda event, **kw: calls.append(dict(event))
        )
        monkeypatch.setenv(slack_socket.TEAM_SLACK_UPDATES_FLAG_ENV, "1")

        slack_socket._maybe_handle_team_update(
            message_event(channel="C0OTHERCHANNEL", user="U0BOB", text="done U3 shipped it")
        )

        assert calls == []

    def test_flag_on_and_channel_matched_invokes_the_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            slack_updates, "team_updates_handle", lambda event, **kw: calls.append(dict(event))
        )
        monkeypatch.setenv(slack_socket.TEAM_SLACK_UPDATES_FLAG_ENV, "1")

        slack_socket._maybe_handle_team_update(
            message_event(user="U0BOB", text="done U3 shipped it")
        )

        assert len(calls) == 1

    def test_a_raising_handler_never_escapes_the_hook(
        self,
        collab_store: CollabStore,
        slack_map: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(slack_updates, "_collab_store", lambda: collab_store)
        monkeypatch.setattr(slack_updates, "_slack_map", lambda: slack_map)

        def _raise(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("boom")

        monkeypatch.setattr(slack_updates, "apply", _raise)
        monkeypatch.setenv(slack_socket.TEAM_SLACK_UPDATES_FLAG_ENV, "1")

        # Must not raise -- the hook's own try/except (never logging.exception,
        # which would need a live traceback) is what makes this safe.
        with caplog.at_level("WARNING", logger="omniagentos.comms.sockets.slack"):
            slack_socket._maybe_handle_team_update(
                message_event(user="U0BOB", text="done U3 shipped it")
            )

        assert "slack team update handling failed" in caplog.text


class TestTaskVerbRouting:
    def test_a_task_command_creates_the_card_and_replies_in_thread(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
        slack_map: dict[str, str],
    ) -> None:
        poster = _CapturePoster()
        event = message_event(user="U0TEAM", text="<@U0BOB> task fix the login bug !top")

        slack_updates.team_updates_handle(
            event, collab=collab_store, team=team_store, slack_map=slack_map, poster=poster
        )

        card = collab_store.list_board_tasks()[0]
        assert card["owner_employee_id"] == employees["bob"]
        assert card["priority"] == "urgent"
        channel, thread_ts, text = poster.calls[0]
        assert (channel, thread_ts) == ("C0000EXAMPLE", "1700000000.000100")
        assert text.startswith(f"Created {card['id']}:")

    def test_an_unmapped_sender_cannot_assign(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
    ) -> None:
        poster = _CapturePoster()
        event = message_event(user="U0STRANGER", text="<@U0BOB> task fix the login bug")

        slack_updates.team_updates_handle(
            event, collab=collab_store, team=team_store, slack_map=slack_map, poster=poster
        )

        assert collab_store.list_board_tasks() == []
        assert poster.calls == []

    def test_channel_gating_is_the_same_for_task_as_for_every_other_verb(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            slack_updates, "team_updates_handle", lambda event, **kw: calls.append(dict(event))
        )
        text = "<@U0BOB> task fix the login bug"

        monkeypatch.delenv(slack_socket.TEAM_SLACK_UPDATES_FLAG_ENV, raising=False)
        slack_socket._maybe_handle_team_update(message_event(user="U0TEAM", text=text))
        assert calls == []

        monkeypatch.setenv(slack_socket.TEAM_SLACK_UPDATES_FLAG_ENV, "1")
        slack_socket._maybe_handle_team_update(
            message_event(channel="C0OTHERCHANNEL", user="U0TEAM", text=text)
        )
        assert calls == []

        slack_socket._maybe_handle_team_update(message_event(user="U0TEAM", text=text))
        assert len(calls) == 1
