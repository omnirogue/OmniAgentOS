"""WP-B: the daily-commitments pipeline wired into the morning DM and the
07:00 report.

Two surfaces read WP-A's ``commitments`` module, and both carry the SAME
fail-safe contract (never silently favourable — DESIGN M3, review F2): a
generation/resolution outage must render an explicit "unavailable"/
"unresolved" state, a genuinely empty result must render an explicit
"no commitments recorded" state, and the two must never collapse into one
another. These tests pin all three states on both surfaces, plus the one
ordering guarantee the morning DM makes (WP-A's ``_edc_morning_section`` seam
still lands AFTER the commitments section).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC as _UTC
from datetime import datetime as _real_dt
from typing import Any

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.db.store import SqliteStore
from omniagentos.team import commitments
from omniagentos.team import notify as team_notify
from omniagentos.team import report as team_report
from omniagentos.team.store import TeamStore

YESTERDAY = "2026-08-13"
TODAY = "2026-08-14"

# This module's fixtures are authored against the fixed TODAY/YESTERDAY above,
# but the stores stamp transitions with the real wall clock (``utc_now_iso``) —
# so cards driven through the real write path left the authored window the
# moment the calendar rolled past TODAY, and the module was green only on its
# authoring date. Pin the clock the stores read to noon of the authored TODAY.
# ``utc_now_iso`` resolves ``datetime`` from ``omniagentos.contracts`` module
# globals, so this one patch point covers every store regardless of how it
# imported the helper. Scoped to this module on purpose: the accountability
# API tests use the REAL clock end-to-end (``local_today()`` vs stamped rows)
# and must not be pinned. Same mechanism as tests/team_scoring/conftest.py.
_PINNED_NOW = (2026, 8, 14, 12, 0, 0)


@pytest.fixture(autouse=True)
def _pin_store_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime as _real_datetime

    class _PinnedDatetime(_real_datetime):
        @classmethod
        def now(cls, tz: Any = None) -> _PinnedDatetime:
            return cls(*_PINNED_NOW, tzinfo=tz)

    monkeypatch.setattr("omniagentos.contracts.datetime", _PinnedDatetime)


class _Notifier:
    """The same minimal Slack seam stub ``tests/team_slack/test_notify.py`` uses."""

    def __init__(self) -> None:
        self.dms: list[tuple[str, str]] = []

    def post_dm(self, user: str, text: str, *, blocks: object = None, color: object = None) -> bool:
        self.dms.append((user, text))
        return True

    def post_channel(self, text: str, *, blocks: object = None, color: object = None) -> bool:
        return True


def _finish(
    collab_store: CollabStore,
    team_store: TeamStore,
    card: BoardTask,
    *,
    actor: str,
) -> None:
    """Move a card to done the way a person does: evidence first, then status."""
    team_store.add_evidence(kind="commit", ref=f"ev-{card.id}", repo="omnios", task_id=card.id)
    collab_store.update_board_task(card.id, {"status": BoardTaskStatus.DONE.value}, actor=actor)


def _backdate_done_event(store: SqliteStore, task_id: str, timestamp: str) -> None:
    """Rewrite the done event's clock — the only way to pin a commitment to a day."""
    store._connection.execute(
        "UPDATE task_events SET created_at = ? WHERE task_id = ? AND event = 'status_change' "
        "AND to_status = 'done'",
        (timestamp, task_id),
    )
    store._connection.commit()


def _person(gathered: dict[str, object], employee_id: str) -> dict[str, object]:
    people = gathered["people"]
    assert isinstance(people, list)
    return next(person for person in people if person["employee_id"] == employee_id)  # type: ignore[index]


# ---------------------------------------------------------------------------
# the morning DM (notify.run_morning) — deliverable 1
# ---------------------------------------------------------------------------


class TestMorningCommitmentsSection:
    def test_run_morning_still_sends_when_generation_raises(
        self,
        team_store: TeamStore,
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """5(a): a generation outage renders the explicit unavailable state —
        never an absent section, never a silent 'no commitments recorded' —
        and the DM still sends."""
        monkeypatch.setattr(team_notify, "load_slack_map", lambda: {"US": employees["bob"]})

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(commitments, "generate_for_day", _raise)
        outbound = _Notifier()

        assert team_notify.run_morning(team_store, outbound) is True  # type: ignore[arg-type]
        assert len(outbound.dms) == 1
        text = outbound.dms[0][1]
        assert "Today's commitments" in text
        assert "⚠ commitments unavailable (generation failed)" in text
        assert "no commitments recorded" not in text

    def test_commitments_section_lists_task_and_improvement_for_active_dev(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """5(b): 'REF — title' for the task commitment, the improvement slot
        as its own line."""
        card = make_card(
            title="Ship the widget",
            ref="U-7",
            owner_employee_id=employees["bob"],
            status=BoardTaskStatus.IN_PROGRESS.value,
        )
        monkeypatch.setattr(team_notify, "load_slack_map", lambda: {"US": employees["bob"]})
        outbound = _Notifier()

        assert team_notify.run_morning(team_store, outbound) is True  # type: ignore[arg-type]
        text = outbound.dms[0][1]
        assert "Today's commitments" in text
        assert f"• {card.ref} — {card.title}" in text
        assert f"Improvement: {commitments.IMPROVEMENT_TITLE}" in text

    def test_operator_sees_the_genuinely_zero_state(
        self,
        team_store: TeamStore,
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The operator is excluded from generation (``commitments.active_devs``)
        — a genuinely empty result renders the explicit zero state, distinct
        from both the real-data and the failure states."""
        monkeypatch.setattr(team_notify, "load_slack_map", lambda: {"US": employees["owner"]})
        outbound = _Notifier()

        assert team_notify.run_morning(team_store, outbound) is True  # type: ignore[arg-type]
        text = outbound.dms[0][1]
        assert "• no commitments recorded" in text
        assert "unavailable" not in text

    def test_commitments_section_renders_before_the_edc_seam(
        self,
        team_store: TeamStore,
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """5(d): the EDC morning seam is untouched and still appended AFTER
        the commitments section."""
        monkeypatch.setattr(team_notify, "load_slack_map", lambda: {"US": employees["bob"]})
        monkeypatch.setattr(
            team_notify, "_edc_morning_section", lambda store, employee_id: "*EDC MARKER*"
        )
        outbound = _Notifier()

        assert team_notify.run_morning(team_store, outbound) is True  # type: ignore[arg-type]
        text = outbound.dms[0][1]
        assert "Today's commitments" in text and "EDC MARKER" in text
        assert text.index("Today's commitments") < text.index("EDC MARKER")


# ---------------------------------------------------------------------------
# the 07:00 report (report.gather / render / render_slack) — deliverable 2
# ---------------------------------------------------------------------------


class TestReportCommitmentsLine:
    def test_gather_day_argument_is_respected_under_the_pinned_clock(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """Canary for the module-wide clock pin (Sol review of #497, F3 r2):
        the ``day`` argument must drive CARD MEMBERSHIP, not just the echoed
        label. A card done at the pinned now (2026-08-14) belongs to TODAY's
        done_today bucket and must vanish from YESTERDAY's — a regression that
        drops the day argument for the ambient clock (``team_queues`` falling
        back to ``utc_now_iso()``) counts it on both days and fails here."""
        card = make_card(
            title="Done on the pinned day",
            ref="CAN-2",
            owner_employee_id=employees["bob"],
            acceptance_criteria="it lands today",
            status=BoardTaskStatus.IN_PROGRESS.value,
        )
        _finish(collab_store, team_store, card, actor=employees["bob"])
        today_view = _person(team_report.gather(team_store, TODAY), employees["bob"])
        yesterday_gathered = team_report.gather(team_store, YESTERDAY)
        yesterday_view = _person(yesterday_gathered, employees["bob"])
        assert today_view["queue"]["done_today"] == 1
        assert yesterday_view["queue"]["done_today"] == 0
        assert yesterday_gathered["day"] == YESTERDAY

    def test_gather_resolves_yesterday_and_renders_the_truth(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """5(c): gather() resolves yesterday's commitments and the rendered
        line matches the delivered/missed truth, on both surfaces."""
        delivered_card = make_card(
            title="Shipped it",
            owner_employee_id=employees["bob"],
            acceptance_criteria="it ships",
            status=BoardTaskStatus.IN_PROGRESS.value,
        )
        make_card(
            title="Slipped",
            ref="U-9",
            owner_employee_id=employees["bob"],
            status=BoardTaskStatus.CLAIMED.value,
        )
        commitments.generate_for_day(team_store, YESTERDAY, employee_ids=[employees["bob"]])
        _finish(collab_store, team_store, delivered_card, actor=employees["bob"])
        _backdate_done_event(store, delivered_card.id, f"{YESTERDAY}T12:00:00Z")

        # resolve=True is the SCHEDULED 07:00 path — the only caller allowed to
        # write. The preview route gathers with the default (pure read).
        gathered = team_report.gather(team_store, TODAY, resolve=True)
        person = _person(gathered, employees["bob"])
        commitments_summary = person["commitments"]  # type: ignore[index]
        # generate_for_day always mints 3 automation slots too (the operator's
        # 2026-08-14 ruling); none were shipped in this fixture, so 0/3.
        assert commitments_summary["line"] == (
            "Yesterday: delivered 1/2 commitments · improvement ✗ · automations 0/3"
        )
        assert commitments_summary["missed"] == ["U-9"]

        text = team_report.render(gathered)
        assert "Yesterday: delivered 1/2 commitments · improvement ✗ · automations 0/3" in text
        assert "missed: U-9" in text

        fallback, blocks = team_report.render_slack(gathered)
        assert fallback == text
        assert (
            "Yesterday: delivered 1/2 commitments · improvement ✗ · automations 0/3"
            in json.dumps(blocks, ensure_ascii=False)
        )

        # Idempotent: the 06:55 job may already have resolved this exact day —
        # a second gather() must not change the answer (WP-A's own contract).
        regathered = team_report.gather(team_store, TODAY, resolve=True)
        assert _person(regathered, employees["bob"])["commitments"] == commitments_summary

    def test_no_commitments_recorded_state(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        """Nothing was ever generated for 'yesterday' — the explicit zero
        state, never a bare '0/0' that would misread as two failures."""
        gathered = team_report.gather(team_store, TODAY, resolve=True)
        person = _person(gathered, employees["bob"])
        assert person["commitments"] == {  # type: ignore[comparison-overlap]
            "line": "Yesterday: no commitments recorded",
            "missed": [],
        }
        assert "Yesterday: no commitments recorded" in team_report.render(gathered)

    def test_resolve_day_failure_renders_the_unresolved_state(
        self,
        team_store: TeamStore,
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A resolution outage must read as an outage, never as an honest
        zero — the third distinguishable state."""

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(commitments, "resolve_day", _raise)

        gathered = team_report.gather(team_store, TODAY, resolve=True)
        person = _person(gathered, employees["bob"])
        assert person["commitments"] == {  # type: ignore[comparison-overlap]
            "line": "Yesterday: commitments unresolved ⚠",
            "missed": [],
        }
        text = team_report.render(gathered)
        assert "Yesterday: commitments unresolved ⚠" in text
        fallback, _blocks = team_report.render_slack(gathered)
        assert "commitments unresolved" in fallback


class TestOpenRowsArePendingNotMissed:
    """Round-2 review, item 1.

    ``gather`` is called two ways: the scheduled 07:00 run resolves first, and
    the preview route is a pure read. Bucketing every not-delivered row as
    "missed" was right for the first and an ACCUSATION in the second — on a day
    still in progress, every open row rendered as a failure nobody had judged.
    """

    def test_a_pure_read_renders_open_rows_as_pending(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(
            title="Still going",
            ref="U-11",
            owner_employee_id=employees["bob"],
            acceptance_criteria="it ships",
            status=BoardTaskStatus.IN_PROGRESS.value,
        )
        commitments.generate_for_day(team_store, YESTERDAY, employee_ids=[employees["bob"]])

        gathered = team_report.gather(team_store, TODAY)  # pure read: no resolve
        summary = _person(gathered, employees["bob"])["commitments"]

        assert summary == {  # type: ignore[comparison-overlap]
            "line": (
                "Yesterday: delivered 0/1 commitments · improvement pending · "
                "automations pending · 1 pending"
            ),
            "missed": [],
        }
        text = team_report.render(gathered)
        assert "1 pending" in text
        assert "automations pending" in text
        assert "missed:" not in text
        assert card.ref not in text.split("Yesterday:")[1].split("\n")[0]

    def test_a_carried_row_is_pending_too(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """``carried`` is an OPEN state — provenance, not a verdict."""
        make_card(
            title="Slipped twice",
            owner_employee_id=employees["bob"],
            acceptance_criteria="it ships",
            status=BoardTaskStatus.IN_PROGRESS.value,
        )
        day_before = "2026-08-12"
        commitments.generate_for_day(team_store, day_before, employee_ids=[employees["bob"]])
        commitments.resolve_day(team_store, day_before)
        assert {str(row["status"]) for row in team_store.list_commitments(day=YESTERDAY)} == {
            "carried"
        }

        summary = _person(team_report.gather(team_store, TODAY), employees["bob"])["commitments"]
        assert "1 pending" in str(summary["line"])  # type: ignore[index]
        assert summary["missed"] == []  # type: ignore[index]

    def test_the_scheduled_line_is_byte_identical_for_a_resolved_day(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """The 07:00 text must not move: after resolution there are no open
        rows, so the pending clause never appears and the phrase is exactly the
        one this report has always emitted."""
        delivered_card = make_card(
            title="Shipped it",
            owner_employee_id=employees["bob"],
            acceptance_criteria="it ships",
            status=BoardTaskStatus.IN_PROGRESS.value,
        )
        make_card(
            title="Slipped",
            ref="U-9",
            owner_employee_id=employees["bob"],
            status=BoardTaskStatus.CLAIMED.value,
        )
        commitments.generate_for_day(team_store, YESTERDAY, employee_ids=[employees["bob"]])
        _finish(collab_store, team_store, delivered_card, actor=employees["bob"])
        _backdate_done_event(store, delivered_card.id, f"{YESTERDAY}T12:00:00Z")

        gathered = team_report.gather(team_store, TODAY, resolve=True)
        summary = _person(gathered, employees["bob"])["commitments"]
        assert summary == {  # type: ignore[comparison-overlap]
            "line": "Yesterday: delivered 1/2 commitments · improvement ✗ · automations 0/3",
            "missed": ["U-9"],
        }
        assert "pending" not in str(summary["line"])  # type: ignore[index]
        assert "missed: U-9" in team_report.render(gathered)


# ---------------------------------------------------------------------------
# the operator's 2026-08-14 ruling: the standing targets line + the three daily
# automation slots on the morning DM, the daybrief, and the 07:00 report.
# ---------------------------------------------------------------------------


class TestStandingTargetsAndAutomationSlots:
    def _ship_automation(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        *,
        title: str,
        ref: str,
        maturity: str = "assisted",
        day: str = YESTERDAY,
        at: str = "12:00:00",
    ) -> BoardTask:
        """Mirrors ``tests/team/test_commitments.py``'s ``_ship_automation``:
        a done card the SYSTEM took over (``automation_maturity`` set, backed
        by pass-gated evidence) — the bar the automation slot actually checks."""
        card = make_card(
            title=title,
            ref=ref,
            owner_employee_id=employees["bob"],
            acceptance_criteria="it runs itself",
        )
        collab_store.update_board_task(card.id, {"automation_maturity": maturity})
        _finish(collab_store, team_store, card, actor=employees["bob"])
        # Convert the named LOCAL moment to real UTC before stamping — the
        # verbatim-"Z" form put dawn times on the previous local day on
        # Pacific runners (Sol review of #497, F6; same fix as
        # tests/team/test_commitments.py's _local_moment_as_utc).
        moment = _real_dt.fromisoformat(f"{day}T{at}")
        _backdate_done_event(
            store,
            card.id,
            moment.astimezone().astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        return card

    def test_report_line_shows_automations_delivered_out_of_three(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """Deliverable 4: two qualifying automations on a resolved day render
        'automations 2/3', on the summary dict, ``render()`` and
        ``render_slack()`` alike."""
        commitments.generate_for_day(team_store, YESTERDAY, employee_ids=[employees["bob"]])
        self._ship_automation(
            collab_store, team_store, store, make_card, employees, title="Auto one", ref="A-1"
        )
        self._ship_automation(
            collab_store,
            team_store,
            store,
            make_card,
            employees,
            title="Auto two",
            ref="A-2",
            maturity="autonomous",
            at="15:00:00",
        )

        gathered = team_report.gather(team_store, TODAY, resolve=True)
        summary = _person(gathered, employees["bob"])["commitments"]
        assert "automations 2/3" in str(summary["line"])

        text = team_report.render(gathered)
        assert "automations 2/3" in text
        fallback, blocks = team_report.render_slack(gathered)
        assert fallback == text
        assert "automations 2/3" in json.dumps(blocks, ensure_ascii=False)

    def test_absent_automation_slots_omit_the_clause_never_zero_of_three(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """A pre-migration day: task commitments exist, automation rows never
        do — the clause is OMITTED, never a '0/3' that would read as a
        judged miss on a day nobody measured."""
        make_card(
            title="Old-style card",
            owner_employee_id=employees["bob"],
            status=BoardTaskStatus.CLAIMED.value,
        )
        commitments.generate_for_day(team_store, YESTERDAY, employee_ids=[employees["bob"]])
        team_store._connection.execute(
            "DELETE FROM team_commitments WHERE day = ? AND kind = 'automation'", (YESTERDAY,)
        )
        team_store._connection.commit()

        gathered = team_report.gather(team_store, TODAY, resolve=True)
        summary = _person(gathered, employees["bob"])["commitments"]
        assert "automations" not in str(summary["line"])
        assert "automations" not in team_report.render(gathered)

    def test_morning_dm_shows_targets_line_and_open_automation_slots(
        self,
        team_store: TeamStore,
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Deliverable 1: the targets line is the FIRST line of the DM body;
        today's freshly-generated automation slots render as an OPEN count,
        never a ratio (nobody has judged them yet)."""
        monkeypatch.setattr(team_notify, "load_slack_map", lambda: {"US": employees["bob"]})
        outbound = _Notifier()

        assert team_notify.run_morning(team_store, outbound) is True  # type: ignore[arg-type]
        text = outbound.dms[0][1]
        assert text.startswith(
            "🎯 YOUR TARGETS: 100% of the operator's tasks automated · 10× verified dev speed"
        )
        assert "3 automation/skill slots open today" in text
        assert "automations 0/3" not in text

    def test_morning_dm_omits_the_targets_line_for_the_operator(
        self,
        team_store: TeamStore,
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The targets are FOR Bob & Alice (the operator's ruling) — the operator
        sets them, they are not addressed at the operator's own DM."""
        monkeypatch.setattr(team_notify, "load_slack_map", lambda: {"US": employees["owner"]})
        outbound = _Notifier()

        assert team_notify.run_morning(team_store, outbound) is True  # type: ignore[arg-type]
        assert "YOUR TARGETS" not in outbound.dms[0][1]

    def test_daybrief_shows_the_targets_line_under_the_title(
        self, team_store: TeamStore
    ) -> None:
        """Deliverable 2: the channel-wide brief carries the SAME targets
        line, once, under the title — in both the text and the blocks."""
        text, blocks, _color = team_notify.daybrief_payload(team_store, today=TODAY)
        lines = text.split("\n")
        assert lines[1] == team_notify._targets_line()
        assert team_notify._targets_line() in json.dumps(blocks, ensure_ascii=False)

    def test_report_targets_line_is_present_in_text_and_slack_blocks(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        """Deliverable 3: the report header gains the targets line once, at
        the top, in both render() and render_slack()."""
        gathered = team_report.gather(team_store, TODAY, resolve=True)
        text = team_report.render(gathered)
        assert text.split("\n")[1] == team_report._targets_line()
        fallback, blocks = team_report.render_slack(gathered)
        assert fallback == text
        assert team_report._targets_line() in json.dumps(blocks, ensure_ascii=False)
