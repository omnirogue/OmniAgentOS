"""The channel-wide morning brief (v3 alerts) and its first-pulse-of-day wiring."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

import omniagentos.team.notify as notify
from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.team.store import TeamStore

TODAY = "2026-08-13"


class _Notifier:
    def __init__(self) -> None:
        self.channels: list[str] = []
        self.blocks: list[list] = []
        self.colors: list[str | None] = []

    def post_channel(self, text: str, *, blocks=None, color=None) -> bool:
        self.channels.append(text)
        self.blocks.append(blocks or [])
        self.colors.append(color)
        return True

    def post_dm(self, user: str, text: str, *, blocks=None, color=None) -> bool:  # pragma: no cover
        raise AssertionError("the daybrief is a channel message, never a DM")


@pytest.fixture
def five_companies(store: SqliteStore, goals_store: CompanyGoalsStore) -> dict[str, str]:
    """All five brief companies with a general-engineering goal each → {slug: goal_id}."""
    goals: dict[str, str] = {}
    for slug, name in notify.COMPANY_ORDER:
        store._connection.execute(
            "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (f"co_{slug}", slug, name, "active", utc_now_iso()),
        )
        goal = goals_store.create_goal(
            org_company_id=f"co_{slug}",
            title=f"General engineering — {name}",
            horizon="quarter",
        )
        goals[slug] = str(goal["id"])
    return goals


def _pool_card(
    make_card: Callable[..., BoardTask],
    goal_id: str,
    *,
    title: str,
    ref: str,
    priority: str = "normal",
    due_date: str | None = None,
    created_at: str | None = None,
) -> BoardTask:
    fields: dict = {
        "title": title,
        "ref": ref,
        "goal_id": goal_id,
        "acceptance_criteria": "observable and testable",
        "priority": priority,
        "due_date": due_date,
    }
    if created_at is not None:
        # Same-second creations tie and fall through to the uuid tie-break;
        # explicit stamps keep 'the five oldest render' deterministic.
        fields["created_at"] = created_at
    return make_card(**fields)


class TestDaybriefRendering:
    def test_all_five_companies_in_fixed_order_with_empty_markers(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        five_companies: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pool_card(make_card, five_companies["globex"], title="Fix checkout", ref="CF1")
        _pool_card(make_card, five_companies["omniagentos"], title="Harden gate", ref="GR1")
        monkeypatch.setattr(notify, "load_slack_map", lambda: {})

        text, blocks, color = notify.daybrief_payload(team_store, today=TODAY)

        positions = [text.index(name) for _, name in notify.COMPANY_ORDER]
        assert positions == sorted(positions)
        assert "*Globex* — 1 queued" in text
        assert "*AcmeUni* — empty" in text
        assert "*Hooli* — empty" in text
        assert "*Initech* — empty" in text
        assert "*OmniAgentOS* — 1 queued" in text
        assert f"☀️ Work queue — {TODAY}" in text
        assert text.splitlines()[-1] == notify.TASK_FOOTER
        from omniagentos.team import slack_blocks

        assert color == slack_blocks.GREEN  # nothing overdue
        assert len(blocks) <= 48

    def test_card_grammar_priority_glyphs_and_overdue_deadline(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        five_companies: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pool_card(
            make_card,
            five_companies["globex"],
            title="Fix checkout",
            ref="CF1",
            priority="urgent",
            due_date="2026-08-10",
        )
        _pool_card(
            make_card,
            five_companies["globex"],
            title="Refresh landing page",
            ref="CF2",
            priority="low",
            due_date="2026-08-14",
        )
        monkeypatch.setattr(notify, "load_slack_map", lambda: {})

        text, _blocks, color = notify.daybrief_payload(team_store, today=TODAY)

        assert "🔥 CF1 Fix checkout — urgent 🔴⏰2026-08-10" in text
        assert "⬇ CF2 Refresh landing page — low ⏰2026-08-14" in text
        # An overdue card turns the side-bar amber.
        from omniagentos.team import slack_blocks

        assert color == slack_blocks.AMBER
        # Urgent sorts above low regardless of creation order.
        assert text.index("CF1") < text.index("CF2")

    def test_company_and_person_card_caps_render_plus_n_more(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        five_companies: dict[str, str],
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for index in range(7):
            _pool_card(
                make_card,
                five_companies["acmeuni"],
                title=f"Pool card {index}",
                ref=f"AP{index}",
                created_at=f"2026-08-0{index + 1}T00:00:00Z",
            )
        for index in range(2):
            make_card(
                title=f"Doing {index}",
                ref=f"S{index}",
                owner_employee_id=employees["bob"],
                status=BoardTaskStatus.CLAIMED,
                created_at=f"2026-08-01T0{index}:00:00Z",
            )
        for index in range(5):
            make_card(
                title=f"Waiting {index}",
                ref=f"W{index}",
                owner_employee_id=employees["bob"],
                created_at=f"2026-08-02T0{index}:00:00Z",
            )
        monkeypatch.setattr(
            notify, "load_slack_map", lambda: {"U0BOB": employees["bob"]}
        )

        text, _blocks, _color = notify.daybrief_payload(team_store, today=TODAY)

        assert "*AcmeUni* — 7 queued" in text
        assert "AP4" in text and "AP5" not in text
        assert "+2 more" in text
        assert "👤 Bob — in progress 2 · queued 5" in text
        assert "▶️ S0 Doing 0" in text
        assert "▫️ W2 Waiting 2" in text
        # 7 owned cards, capped at 5 lines + '+2 more'.
        assert "W3" not in text and "W4" not in text

    def test_person_order_is_owner_alice_bob(
        self,
        team_store: TeamStore,
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            notify,
            "load_slack_map",
            lambda: {
                "U0BOB": employees["bob"],
                "U0ALICE": employees["alice"],
                "U0TEAM": employees["owner"],
            },
        )

        text, _blocks, _color = notify.daybrief_payload(team_store, today=TODAY)

        assert (
            text.index("👤 Owner")
            < text.index("👤 Alice")
            < text.index("👤 Bob")
        )

    def test_test_flag_prefixes_header_in_text_and_blocks(
        self, team_store: TeamStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(notify, "load_slack_map", lambda: {})

        text, blocks, _color = notify.daybrief_payload(team_store, today=TODAY, test=True)

        assert text.startswith(f"*🧪 TEST — ☀️ Work queue — {TODAY}*")
        assert blocks[0]["type"] == "header"
        assert blocks[0]["text"]["text"].startswith("🧪 TEST — ")

    def test_titles_are_sanitized_and_blocks_stay_under_the_limit(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        five_companies: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _pool_card(
            make_card,
            five_companies["initech"],
            title="leak https://example.test xoxb-secret <!channel>",
            ref="OR1",
        )
        monkeypatch.setattr(notify, "load_slack_map", lambda: {})

        text, blocks, _color = notify.daybrief_payload(team_store, today=TODAY)
        body = json.dumps(blocks)

        for hostile in ("https://", "xoxb-", "<!channel>"):
            assert hostile not in text
            assert hostile not in body
        assert len(blocks) <= 48

    def test_pool_outage_degrades_loudly_not_as_fake_empty(
        self, team_store: TeamStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(notify, "load_slack_map", lambda: {})
        monkeypatch.setattr(notify, "_pool_cards", lambda store, **kwargs: None)

        text, _blocks, _color = notify.daybrief_payload(team_store, today=TODAY)

        assert "pool unavailable" in text
        assert "— empty" not in text


class TestDeadlineSuffix:
    def test_overdue_today_and_missing(self) -> None:
        assert notify._deadline_suffix("2026-08-10", today=TODAY) == " 🔴⏰2026-08-10"
        assert notify._deadline_suffix("2026-08-13", today=TODAY) == " ⏰2026-08-13"
        assert notify._deadline_suffix("2026-08-14T18:00:00Z", today=TODAY) == " ⏰2026-08-14"
        assert notify._deadline_suffix(None, today=TODAY) == ""
        assert notify._deadline_suffix("", today=TODAY) == ""


class TestDaybriefWiring:
    def _pin_state(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        state = tmp_path / "daybrief-state.json"
        monkeypatch.setattr(
            notify, "daybrief_state_path", lambda override=None: state
        )
        return state

    def test_first_pulse_of_day_is_the_brief_then_the_hourly_pulse(
        self, team_store: TeamStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._pin_state(monkeypatch, tmp_path)
        calls: list[str] = []
        monkeypatch.setattr(
            notify, "run_daybrief", lambda *a, **k: calls.append("daybrief") or True
        )
        monkeypatch.setattr(notify, "run_pulse", lambda *a, **k: calls.append("pulse") or True)

        assert notify._pulse_entry(
            team_store, None, dry_run=False, overnight=False, test=False
        )
        assert notify._pulse_entry(
            team_store, None, dry_run=False, overnight=False, test=False
        )

        assert calls == ["daybrief", "pulse"]

    def test_a_test_or_dry_run_brief_never_consumes_the_day_slot(
        self, team_store: TeamStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state = self._pin_state(monkeypatch, tmp_path)
        monkeypatch.setattr(notify, "run_daybrief", lambda *a, **k: True)

        assert notify._daybrief_entry(team_store, None, dry_run=True, test=False)
        assert notify._daybrief_entry(team_store, None, dry_run=False, test=True)
        assert not state.exists()

        assert notify._daybrief_entry(team_store, None, dry_run=False, test=False)
        assert json.loads(state.read_text())["date"]

    def test_a_failed_brief_does_not_mark_the_day_sent(
        self, team_store: TeamStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state = self._pin_state(monkeypatch, tmp_path)
        monkeypatch.setattr(notify, "run_daybrief", lambda *a, **k: False)

        assert notify._pulse_entry(
            team_store, None, dry_run=False, overnight=False, test=False
        ) is False
        assert not state.exists()

    def test_corrupt_state_reads_as_not_sent(
        self, team_store: TeamStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state = self._pin_state(monkeypatch, tmp_path)
        state.write_text('{"date":')
        assert notify._daybrief_sent_today(TODAY) is False

    def test_cli_daybrief_dry_run_prints_channel_json(
        self,
        team_store: TeamStore,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._pin_state(monkeypatch, tmp_path)
        monkeypatch.setattr(notify, "TeamStore", lambda _db: team_store)
        monkeypatch.setattr(notify, "load_slack_env", lambda: None)
        monkeypatch.setattr(notify, "load_slack_map", lambda: {})
        monkeypatch.setenv(notify.PULSE_CHANNEL_ENV, "CPULSE")

        assert notify.main(["--daybrief", "--dry-run", "--db", str(tmp_path / "x.db")]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["channel"] == "CPULSE"
        assert "Work queue" in payload["text"]
        assert payload["blocks"] > 0

    def test_test_flag_requires_a_channel_surface(self) -> None:
        with pytest.raises(SystemExit):
            notify.main(["--morning", "--test", "--db", "unused.db"])

    def test_live_daybrief_posts_to_the_channel(
        self,
        team_store: TeamStore,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self._pin_state(monkeypatch, tmp_path)
        monkeypatch.setattr(notify, "load_slack_map", lambda: {})
        outbound = _Notifier()

        assert notify.run_daybrief(team_store, outbound) is True  # type: ignore[arg-type]
        assert len(outbound.channels) == 1
        assert "Work queue" in outbound.channels[0]
        assert outbound.blocks[0]


def test_companies_depth_line_lists_all_five_and_other() -> None:
    line = notify._companies_depth_line(
        {"globex": 3, "omniagentos": 2, "mystery": 1, None: 1}
    )
    assert line == (
        "🏢 Globex 3 · AcmeUni 0 · Hooli 0 · Initech 0 · OmniAgentOS 2 · other 2"
    )
