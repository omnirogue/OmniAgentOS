"""The deterministic grammar: every verb parses, chatter never errors.

No LLM anywhere in ``parse_command`` — it is a fixed regex grammar, and this
file is the proof: every recognised shape returns a :class:`Command`, and
everything else (including near-misses) returns ``None`` rather than raising.
"""

from __future__ import annotations

import pytest

from omniagentos.team.slack_updates import Command, parse_command


class TestEachVerbParses:
    def test_done_with_a_ref_and_a_note(self) -> None:
        assert parse_command("done U3 shipped it") == Command(
            verb="done", ref="U3", note="shipped it"
        )

    def test_done_with_a_ref_and_no_note(self) -> None:
        assert parse_command("done U3") == Command(verb="done", ref="U3", note="")

    def test_progress_requires_a_note(self) -> None:
        assert parse_command("progress S5 talked to the customer") == Command(
            verb="progress", ref="S5", note="talked to the customer"
        )
        assert parse_command("progress S5") is None

    def test_blocked_requires_a_reason(self) -> None:
        assert parse_command("blocked OPS-2 waiting on Alice") == Command(
            verb="blocked", ref="OPS-2", note="waiting on Alice"
        )
        assert parse_command("blocked OPS-2") is None

    def test_claim_takes_only_a_ref(self) -> None:
        assert parse_command("claim UP-1") == Command(verb="claim", ref="UP-1", note="")

    def test_claim_accepts_the_pool_id_printed_in_morning_dms(self) -> None:
        assert parse_command("claim btk_ab12cd") == Command(verb="claim", ref="btk_ab12cd", note="")

    def test_my_queue(self) -> None:
        assert parse_command("my queue") == Command(verb="my_queue")
        assert parse_command("  my   queue  ") == Command(verb="my_queue")

    def test_report(self) -> None:
        assert parse_command("report") == Command(verb="report")


class TestUnknownTextIsIgnoredNotErrored:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "hello there",
            "done",  # no ref
            "claim",  # no ref
            "finished U3",  # not a recognised verb
            "my report",
            "reports",
        ],
    )
    def test_returns_none(self, text: str) -> None:
        assert parse_command(text) is None


class TestRefTokenBoundary:
    """``U3`` and ``U33`` must never be confused with one another."""

    def test_u3_and_u33_parse_to_different_refs(self) -> None:
        assert parse_command("done U3 x").ref == "U3"  # type: ignore[union-attr]
        assert parse_command("done U33 x").ref == "U33"  # type: ignore[union-attr]

    def test_a_bare_word_is_not_a_ref(self) -> None:
        # No trailing digits -- not ref-shaped, and not quoted either.
        assert parse_command("done widget fixed it") is None


class TestQuotedTitlePrefix:
    def test_quoted_title_with_a_trailing_note(self) -> None:
        command = parse_command('done "Fix login bug" landed via hotfix')
        assert command == Command(
            verb="done", title_prefix="Fix login bug", note="landed via hotfix"
        )

    def test_quoted_title_with_no_trailing_note(self) -> None:
        assert parse_command('claim "Fix login bug"') == Command(
            verb="claim", title_prefix="Fix login bug", note=""
        )

    def test_empty_quotes_do_not_parse(self) -> None:
        assert parse_command('done "" landed it') is None


class TestCaseInsensitiveVerbs:
    @pytest.mark.parametrize("verb", ["done", "DONE", "Done", "dOnE"])
    def test_done_case_variants(self, verb: str) -> None:
        command = parse_command(f"{verb} U3 shipped it")
        assert command is not None
        assert command.verb == "done"

    def test_my_queue_is_case_insensitive(self) -> None:
        assert parse_command("MY QUEUE") == Command(verb="my_queue")

    def test_report_is_case_insensitive(self) -> None:
        assert parse_command("REPORT") == Command(verb="report")


class TestTaskVerb:
    """``task`` is the only verb that CREATES a card; still no LLM anywhere."""

    def test_mention_first_form(self) -> None:
        assert parse_command("<@U0BOB> task fix the login bug") == Command(
            verb="task",
            assignee_slack_id="U0BOB",
            title="fix the login bug",
            raw_text="<@U0BOB> task fix the login bug",
        )

    def test_verb_first_form(self) -> None:
        command = parse_command("task <@U0BOB> fix the login bug")
        assert command is not None
        assert (command.verb, command.assignee_slack_id, command.title) == (
            "task",
            "U0BOB",
            "fix the login bug",
        )

    def test_a_mention_with_a_display_label_still_resolves_the_id(self) -> None:
        command = parse_command("<@U0BOB|bob> task fix it")
        assert command is not None
        assert command.assignee_slack_id == "U0BOB"

    def test_verb_is_case_insensitive(self) -> None:
        command = parse_command("<@U0BOB> TASK fix it")
        assert command is not None
        assert command.verb == "task"

    @pytest.mark.parametrize(
        "text",
        [
            "<@U0BOB> task !top ship the fix",
            "<@U0BOB> task ship the !top fix",
            "<@U0BOB> task ship the fix !top",
        ],
    )
    def test_top_flag_is_stripped_from_anywhere_and_sets_urgent(self, text: str) -> None:
        command = parse_command(text)
        assert command is not None
        assert command.priority == "urgent"
        assert "!top" not in (command.title or "")
        assert (command.title or "").replace(" ", "") == "shipthefix"

    def test_company_flag_is_stripped_from_anywhere(self) -> None:
        command = parse_command("<@U0BOB> task #initech ship the fix")
        assert command is not None
        assert command.company == "initech"
        assert command.title == "ship the fix"

    def test_both_flags_together(self) -> None:
        command = parse_command("task <@U0ALICE> ship the fix !top #omni")
        assert command is not None
        assert (command.priority, command.company, command.title) == (
            "urgent",
            "omni",
            "ship the fix",
        )

    def test_no_flags_defaults_to_normal_and_no_company(self) -> None:
        command = parse_command("<@U0BOB> task ship it")
        assert command is not None
        assert (command.priority, command.company) == ("normal", None)

    def test_whitespace_is_collapsed(self) -> None:
        command = parse_command("<@U0BOB> task   ship    the   fix  ")
        assert command is not None
        assert command.title == "ship the fix"

    def test_a_second_mention_inside_the_title_is_stripped(self) -> None:
        # A surviving mention would be echoed in the threaded reply and ping.
        command = parse_command("<@U0BOB> task pair with <@U0ALICE> on the fix")
        assert command is not None
        assert command.title == "pair with on the fix"

    def test_a_title_longer_than_the_cap_is_truncated(self) -> None:
        command = parse_command("<@U0BOB> task " + "x" * 800)
        assert command is not None
        assert len(command.title or "") == 500

    @pytest.mark.parametrize(
        "text",
        [
            "<@U0BOB> task",
            "<@U0BOB> task !top",
            "task <@U0BOB>",
            "task fix the login bug",  # no assignee at all
            "tasks <@U0BOB> fix it",
        ],
    )
    def test_a_task_with_no_usable_title_or_assignee_is_not_a_command(self, text: str) -> None:
        assert parse_command(text) is None

    def test_the_raw_text_is_kept_for_the_card_description(self) -> None:
        command = parse_command("  <@U0BOB> task ship it !top  ")
        assert command is not None
        assert command.raw_text == "<@U0BOB> task ship it !top"
