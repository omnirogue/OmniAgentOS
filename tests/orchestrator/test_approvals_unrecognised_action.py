"""LS-022 — the classifier fail-open: an UNRECOGNISED verb auto-approved.

THE DEFECT (LiveSim, observed live against the real resolver). Every floor in
``approvals.py`` that decides "is this destructive?" keys on an *enumerated verb
vocabulary* (``_DESTRUCTIVE_VERB_RE`` / ``_VALUE_MOVE_VERB_RE``). A request whose
verb is simply not on that list falls through the whole classifier to
auto-approve, however clearly it names a production / customer / money target::

    reset the production database to factory state   -> approved (no "reset" verb)
    python manage.py reset_db --env production       -> approved
    format the customer table                        -> approved (no "format" verb)

That is the favourable-absence defect landing on the approval boundary: the
UNKNOWN verb was indistinguishable from a known-safe one.

THE FIX AND ITS BOUNDARY, both asserted here rather than left implicit (see
:func:`test_the_surface_boundary_is_explicit_and_has_two_halves`):

* a STRUCTURED request (a tool call, i.e. everything the live hook-eval path
  sends) has its verb half inverted only on the HIGH-VALUE SURFACE -- money,
  customer records, production. There it must PROVE it is ordinary. Off that
  surface nothing changes, because "unrecognised -> park" applied to every shell
  command was measured at 1,289 parks over 8,496 real agent commands (15.2%):
  that is the whole-classifier allowlist this product cannot run, with ``make
  build`` and ``pytest -q`` inside it.
* a PLAIN-LANGUAGE request -- no tool, no tool input, the prose IS the evidence
  -- has no surface gate at all, because there is nothing else to check it
  against. This is what covers destructive intent aimed at a noun no list holds
  either (``clear the audit trail``, ``vaporize the staging cluster``), and it
  costs nothing live.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from omniagentos.orchestrator import approvals
from omniagentos.orchestrator.approvals import (
    ApprovalGateway,
    NotificationEscalator,
    classify_hard_stop,
    resolve_approval,
)
from omniagentos.orchestrator.contracts import ApprovalRequest


def _request(action: str, command: str | None = None) -> ApprovalRequest:
    return ApprovalRequest(
        proposed_action=action,
        action_class="consequential",
        tool_name="Bash",
        tool_input={"command": command or action},
    )


# The exact vectors LiveSim recorded auto-approving (LS-022 evidence).
LS022_MUST_PARK: tuple[tuple[str, str | None, str], ...] = (
    (
        "reset the production database to factory state",
        "python manage.py reset_db --env production",
        "'reset' is in no verb list; a production noun is named",
    ),
    ("format the customer table", None, "'format' is in no verb list; a customer noun is named"),
)


@pytest.mark.parametrize(
    ("action", "command", "why"), LS022_MUST_PARK, ids=[r[0] for r in LS022_MUST_PARK]
)
def test_ls022_unrecognised_verb_on_a_high_value_noun_parks(
    action: str, command: str | None, why: str
) -> None:
    decision = resolve_approval(_request(action, command))
    assert decision.approved is False, f"LS-022 fail-open still open ({why}): {action}"
    assert decision.escalated is True
    assert classify_hard_stop(_request(action, command)) is not None


def test_the_park_reason_names_the_unrecognised_action_trigger() -> None:
    """An operator must be able to tell this park from an enumerated one.

    The reason PREFIX is deliberately unchanged (``omniagentos/toolplane/session.py``
    maps prefixes to denial codes, and an unknown prefix would be recorded as a
    plain ``denied`` instead of ``approval_required``); the new information travels
    in the trigger.
    """
    decision = resolve_approval(_request("format the customer table"))
    assert decision.reason.startswith("parked per finance-only policy")
    assert "trigger: unrecognised-customer-action" in decision.reason

    production = resolve_approval(
        _request(
            "reset the production database to factory state",
            "python manage.py reset_db --env production",
        )
    )
    assert "trigger: unrecognised-production-action" in production.reason


def test_an_unrecognised_action_hiding_in_a_script_path_still_parks() -> None:
    """The path AT A COMMAND POSITION is the executable, not an inert file name.

    The money floors drop path tokens because "a document PATH is content", and
    applying that rule here unchanged made ``bash scripts/reset-prod.sh`` lose the
    only token naming production -- the LS-022 defect wearing a filename. Reading
    an equally production-named path as an ARGUMENT stays hands-off.
    """
    assert resolve_approval(_request("bash scripts/reset-prod.sh")).approved is False
    assert resolve_approval(_request("vim infra/prod/deploy.tf")).approved is True
    assert resolve_approval(_request("cat scripts/reset-prod.sh")).approved is True


def test_a_recognised_leading_command_cannot_launder_an_unrecognised_one() -> None:
    """The verb slot is judged on the line that NAMES the value, not the first line.

    ``echo ok; curl … | bash`` is this module's own history: a transparent leader
    made the dangerous downstream read as safe. The same shape must not work here.
    """
    laundered = _request(
        "ls -la /tmp && python manage.py reset_db --env production",
        "ls -la /tmp && python manage.py reset_db --env production",
    )
    assert resolve_approval(laundered).approved is False


# LSC-01. The vector above kept ``--env production`` ON the destructive line, so
# it passed against a floor that only judged the lines naming the surface. Split
# the marker from the action across an ordinary separator and the same floor stood
# down. Splitting is free -- ``cd <dir> && <cmd>`` is how agents invoke things, and
# ``\n`` is a statement separator, so a two-line command already has this shape.
LAUNDERED_ACROSS_LINES: tuple[tuple[str, str], ...] = (
    ("cd /srv/production/app && python manage.py reset_db", "cd prefix, marker in the path"),
    ("cd /app/production && bundle exec rake db:reset", "cd prefix + chained task runners"),
    ("echo production && python manage.py reset_db", "marker printed by a transparent leader"),
    ("cat config/production.yml && python manage.py reset_db", "marker in a read operand"),
    ("git log --oneline -1 -- prod/ && python manage.py reset_db", "marker in a VCS operand"),
    ("cd /srv/production/app\npython manage.py reset_db", "a plain two-line agent command"),
    ("python manage.py reset_db && echo production", "marker AFTER the action"),
    ("ls /srv/prod | xargs -I{} python manage.py reset_db", "marker upstream in a pipeline"),
)


@pytest.mark.parametrize(("command", "why"), LAUNDERED_ACROSS_LINES)
def test_the_surface_and_the_action_cannot_be_split_across_lines(command: str, why: str) -> None:
    """Once the REQUEST is on the surface, every readable line must be ordinary.

    A per-line test cannot hold here: an attacker (or an ordinary ``cd``) puts the
    production marker on a recognised line and the destructive action on a line
    that names nothing, and no single line is both aimed and unrecognised.
    """
    assert resolve_approval(_request(command, command)).approved is False, why


def test_a_chain_of_task_runners_cannot_consume_the_verb_window() -> None:
    """``bundle exec rake db:reset`` -- the window was spent on the transports.

    "exec" and "rake" are both recognised and neither is the action; the task that
    resets the database sat one operand past the end of the window.
    """
    assert (
        resolve_approval(_request("bundle exec rake db:reset --env production")).approved is False
    )
    assert (
        resolve_approval(_request("poetry run python manage.py reset_db --env prod")).approved
        is False
    )
    # ...and the same walk must not turn ordinary chained runners into parks.
    assert resolve_approval(_request("bundle exec rake test")).approved is True
    assert resolve_approval(_request("poetry run pytest -q tests/prod")).approved is True


def test_a_line_of_nothing_but_transports_and_flags_parks_rather_than_clearing() -> None:
    """``all([])`` is True, which would have made "proved nothing" read as "proved safe"."""
    only_transports = _request("reset the production database", "sudo env -- ")
    assert resolve_approval(only_transports).approved is False


@pytest.mark.parametrize(
    ("action", "why"),
    [
        ("zap the payout queue", "unknown verb + money noun"),
        ("quiesce every customer record", "unknown verb + customer noun"),
        ("rehydrate the production cluster from a snapshot", "unknown verb + production noun"),
        ("cycle the prod database", "unknown verb + production noun"),
    ],
)
def test_unrecognised_verbs_park_across_the_whole_high_value_surface(action: str, why: str) -> None:
    assert resolve_approval(_request(action)).approved is False, why


# --- LSC-02: the floor must not be a no-op for non-Bash tools -----------------
# ``api/routes/sessions.py::_format_proposed_action`` renders a tool with no
# ``command`` key as ``"<ToolName> <key>=<value>"`` or ``"<ToolName> <compact
# json>"``. Neither is a shell command line. The first version of this floor
# declared any such line RECOGNISED (to keep a ``WebFetch`` read hands-off) and
# separately stripped every ``{...}`` span as content -- two independently
# sufficient ways for the whole non-Bash tool surface to clear the floor.


def _tool(name: str, tool_input: dict[str, object], action: str) -> ApprovalRequest:
    return ApprovalRequest(
        proposed_action=action,
        action_class="consequential",
        tool_name=name,
        tool_input=tool_input,
    )


TOOL_CALLS_THAT_MUST_PARK: tuple[tuple[str, dict[str, object], str, str], ...] = (
    (
        "mcp__db__reset",
        {"query": "reset production customers"},
        "mcp__db__reset query=reset production customers",
        "the tool's own NAME is the destructive action",
    ),
    (
        "mcp__db__reset",
        {"env": "production", "mode": "factory"},
        'mcp__db__reset {"env":"production","mode":"factory"}',
        "the JSON payload names the target and was deleted before the floor read it",
    ),
    (
        "mcp__crm__recycle",
        {"scope": "all customers"},
        'mcp__crm__recycle {"scope":"all customers"}',
        "customer surface reached only through the payload",
    ),
    (
        "mcp__query__reset",
        {"env": "production"},
        'mcp__query__reset {"env":"production"}',
        "a recognised SERVER name must not launder the tool name",
    ),
    (
        "mcp__db__reset",
        {"targets": [{"table": "customers", "env": "production"}]},
        'mcp__db__reset {"targets":[{"table":"customers","env":"production"}]}',
        "a nested payload still names the target",
    ),
)


@pytest.mark.parametrize(
    ("name", "tool_input", "action", "why"),
    TOOL_CALLS_THAT_MUST_PARK,
    ids=[case[3] for case in TOOL_CALLS_THAT_MUST_PARK],
)
def test_a_non_bash_tool_call_is_judged_by_its_own_name_and_its_payload(
    name: str, tool_input: dict[str, object], action: str, why: str
) -> None:
    assert resolve_approval(_tool(name, tool_input, action)).approved is False, why


def test_the_tool_label_escape_is_replaced_by_a_structural_read_proof() -> None:
    """The wound was real; the remedy now matches its size.

    ``WebFetch``/``WebSearch`` are reads by construction, so they belong in
    ``_TRUSTED_READ_TOOLS`` -- a statement about two tools -- rather than being
    covered by a rule that cleared the floor for every tool there is.
    """
    fetch = _tool(
        "WebFetch",
        {"url": "https://status.example.com/production"},
        "WebFetch url=https://status.example.com/production",
    )
    assert resolve_approval(fetch).approved is True
    search = _tool(
        "WebSearch",
        {"query": "production incident customers refund"},
        "WebSearch query=production incident customers refund",
    )
    assert resolve_approval(search).approved is True


def test_a_tool_whose_name_says_it_reads_stays_hands_off() -> None:
    """The last segment of the tool name is the action, and ``query`` is a read."""
    reader = _tool(
        "mcp__db__query",
        {"sql": "SELECT id FROM customers WHERE env = 'production'"},
        'mcp__db__query {"sql":"SELECT id FROM customers WHERE env = \'production\'"}',
    )
    assert resolve_approval(reader).approved is True


def test_prose_bearing_payload_keys_are_still_never_read_as_a_target() -> None:
    """The strip that had to survive: a Task prompt MENTIONING production.

    This is why the whole-payload strip existed. Narrowing it to the prose keys
    this module already refuses to scan keeps that true negative and stops
    deleting the keys that name a real target.
    """
    task = _tool(
        "Task",
        {
            "subagent_type": "general-purpose",
            "description": "read the production customer docs",
            "prompt": "Summarise how the production customers table is populated.",
        },
        'Task {"description":"read the production customer docs",'
        '"prompt":"Summarise how the production customers table is populated.",'
        '"subagent_type":"general-purpose"}',
    )
    assert approvals._tool_input_surface_text(task) == ("subagent_type general-purpose", True)
    assert approvals._unrecognised_action_floor(task) is None
    assert resolve_approval(task).approved is True


def test_a_tool_input_too_deep_to_read_is_unprovable_not_clean() -> None:
    """SELF-REVIEW CATCH. The walk over a caller-supplied tool input is bounded,
    and a bound that silently truncates turns "we could not see all of it" into
    "there is nothing there" -- the favourable-absence defect, reintroduced by
    the fix for it. A truncated walk arms the floor with its own trigger, so the
    audit trail does not claim a target the module never saw."""
    deep = _tool(
        "mcp__db__reset",
        {"a": {"b": {"c": {"d": {"e": "production"}}}}},
        'mcp__db__reset {"a":{"b":{"c":{"d":{"e":"production"}}}}}',
    )
    text, complete = approvals._tool_input_surface_text(deep)
    assert complete is False
    assert "production" not in text, "the fixture must actually exceed the bound"

    decision = resolve_approval(deep)
    assert decision.approved is False
    assert approvals.UNRECOGNISED_OPAQUE_INPUT_ACTION in decision.reason

    # ...and it still only costs an UNRECOGNISED action: a deep payload handed to
    # a tool whose name says what it does stays hands-off.
    ordinary = _tool(
        "TodoWrite",
        {"todos": [{"content": "deploy to production", "status": "pending"}]},
        'TodoWrite {"todos":[{"content":"deploy to production","status":"pending"}]}',
    )
    assert resolve_approval(ordinary).approved is True


def test_a_bash_request_is_never_treated_as_a_tool_label() -> None:
    """``bash scripts/reset-prod.sh`` must never clear itself by naming its own tool."""
    assert resolve_approval(_request("bash scripts/reset-prod.sh")).approved is False
    assert (
        approvals._is_tool_label_request(
            ApprovalRequest(
                proposed_action="bash scripts/reset-prod.sh",
                action_class="consequential",
                tool_name="Bash",
                tool_input={"command": "bash scripts/reset-prod.sh"},
            )
        )
        is False
    )


# --- GCV-01: the tool-name rule reopened LS-022 one level up ------------------
# LSC-02 replaced "a tool label is recognised outright" with "judge the LAST
# segment of the tool name". The Bash path takes the FIRST word of its token
# (``_is_recognised_action_token``), which is why an executable named
# ``reset_query.sh`` parks. The tool path took the opposite end WITHOUT inverting
# the reasoning that made the Bash choice safe, so an allowlisted trailing segment
# cleared an unrecognised leading verb:
#
#     mcp__db__reset        -> parks
#     mcp__db__reset_query  -> AUTO-APPROVED against "production customers table"
#
# The fix for THAT required the first AND the last segment -- and a third round
# (GCV-01-CONFIRM-1) found the same class again one position further in:
#
#     mcp__db__query_reset_fetch -> AUTO-APPROVED
#
# "reset" is neither end, and it is deliberately absent from the enumerated risk
# verbs (that absence is the founding premise of this floor), so it was dropped
# from ``verb_slots`` entirely and ``all()`` cleared vacuously over the two
# ordinary ends. Three rounds, one class: A RULE THAT PICKS WHICH SEGMENTS TO
# JUDGE ALWAYS LEAVES A SEGMENT UNJUDGED. The tests below pin the invariant that
# replaced it -- no segment of an ambiguous name may be dropped from judgement --
# rather than any particular position.
#
# Live-reachable: ``api/routes/sessions.py`` returns ``{"decision":"allow"}`` for
# an orchestrator session the moment ``verdict.approved`` is true.

TOOL_NAMES_THAT_MUST_PARK: tuple[tuple[str, str], ...] = (
    ("mcp__db__reset", "the tool's own name IS the unrecognised verb"),
    ("mcp__db__reset_query", "GCV-01: an allowlisted OBJECT must not clear the verb"),
    ("mcp__db__resetQuery", "GCV-01 in camelCase -- an MCP server picks its own casing"),
    ("mcp__db__reset_v2", "a version suffix is not an action"),
    ("mcp__reset__db", "the SERVER name is namespace; the tool's own name says nothing"),
    ("mcp__query__reset", "a recognised server name must not launder the tool name"),
    ("mcp__admin__customers", "a noun names no action"),
    ("reset_query", "an un-namespaced DELIMITED name is read verb-first too"),
    # --- GCV-01-CONFIRM-1: the same class, in a position no rule was watching ---
    (
        "mcp__db__query_reset_fetch",
        "CONFIRM-1: ordinary ends must not clear an unrecognised MIDDLE",
    ),
    (
        "mcp__db__queryReset_fetch",
        "the middle case in camelCase mixed with underscores",
    ),
    (
        "mcp__db__query_reset_v2",
        "a trailing version does not turn the middle into an end",
    ),
    (
        "mcp__db__query_reset_recycle_fetch",
        "TWO unrecognised middles between two ordinary ends, neither enumerated",
    ),
    (
        "mcp__db__get_rotate_list",
        "'rotate' is unlisted by design; the floor must not need it enumerated",
    ),
)


@pytest.mark.parametrize(
    ("tool_name", "why"),
    TOOL_NAMES_THAT_MUST_PARK,
    ids=[case[0] for case in TOOL_NAMES_THAT_MUST_PARK],
)
def test_no_segment_of_a_tool_name_can_launder_another(tool_name: str, why: str) -> None:
    request = _tool(
        tool_name,
        {"target": "production customers table", "op": "factory state"},
        f'{tool_name} {{"op":"factory state","target":"production customers table"}}',
    )
    assert resolve_approval(request).approved is False, why


TOOL_NAMES_THAT_MUST_CLEAR: tuple[tuple[str, dict[str, object], str], ...] = (
    (
        "mcp__db__query",
        {"sql": "SELECT id FROM customers WHERE env = 'production'"},
        "one segment, and it says the tool reads",
    ),
    (
        "TodoWrite",
        {"todos": [{"content": "deploy to production", "status": "pending"}]},
        "a BARE name is a first-party tool: modifier+head, so the LAST segment is the verb",
    ),
    (
        "MultiEdit",
        {"file_path": "/srv/production/app/customers.py"},
        "same, and an MCP server cannot register a name without its mcp__ prefix",
    ),
)


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "why"),
    TOOL_NAMES_THAT_MUST_CLEAR,
    ids=[case[0] for case in TOOL_NAMES_THAT_MUST_CLEAR],
)
def test_an_unambiguous_tool_name_still_clears_on_its_own_verb(
    tool_name: str, tool_input: dict[str, object], why: str
) -> None:
    assert resolve_approval(_tool(tool_name, tool_input, f"{tool_name} x=1")).approved is True, why


def _verbs(name: str) -> list[str]:
    return approvals._tool_name_action_slots(
        ApprovalRequest(proposed_action="", tool_name=name, tool_input={})
    )[1]


def test_an_ambiguous_name_must_be_ordinary_under_EVERY_reading() -> None:
    """The rule itself, and why it is not a choice between positions.

    Three conventions occur and no vocabulary separates them -- ``reset``,
    ``brave`` and ``Todo`` are all unknown to every regex in this module. Every
    single-position rule was measured wrong on this operator's own traffic:
    last-only approved ``mcp__db__reset_query`` against a customer table,
    first-only approved ``mcp__globex__create_launch_batch`` against a live
    ad budget (6 real calls), and first-AND-last approved
    ``mcp__db__query_reset_fetch`` because "reset" was in neither position. So the
    floor stopped selecting: an ambiguous name has to read ordinary EVERYWHERE.
    """
    assert approvals._split_tool_name("mcp__db__reset_query") == (["reset", "query"], True)
    assert approvals._split_tool_name("mcp__db__resetQuery") == (["reset", "Query"], True)
    assert approvals._split_tool_name("mcp__reset__db") == (["db"], True)
    assert approvals._split_tool_name("reset_query") == (["reset", "query"], True)
    assert approvals._split_tool_name("TodoWrite") == (["Todo", "Write"], False)
    assert approvals._split_tool_name("") == ([], False)

    # verb_object: the object noun is not the verb, but it is still required.
    assert _verbs("mcp__db__list_tables") == ["list", "tables"]
    # object_verb: the same name read the other way round, and the same demand.
    assert _verbs("mcp__playwright__browser_click") == ["browser", "click"]
    # ...and everything BETWEEN the two ends, which is the round-three defect.
    assert _verbs("mcp__db__query_reset_fetch") == ["query", "reset", "fetch"]
    assert _verbs("mcp__db__query_reset_recycle_fetch") == [
        "query",
        "reset",
        "recycle",
        "fetch",
    ]
    # A bare first-party name is read one way only, so only its head is required.
    assert _verbs("TodoWrite") == ["Write"]


def test_no_segment_of_an_ambiguous_name_is_ever_dropped_from_judgement() -> None:
    """THE INVARIANT, asserted as a property of every name rather than by example.

    A rule that decides WHICH segments to judge leaves a segment unjudged, and
    three rounds of GCV-01 each lived in whichever segment the current rule was not
    watching. The property below is what makes a fourth round of the same class
    impossible: for an ambiguous name, ``verb_slots`` IS ``segments``. There is no
    exempt position to aim at.

    It also has to hold for segments the enumerations have never heard of --
    ``reset``, ``recycle``, ``rotate``, ``retire`` are all absent from
    ``_DESTRUCTIVE_VERB_RE`` and ``_VALUE_MOVE_VERB_RE`` ON PURPOSE, and a floor
    that only saw enumerated middles is the floor GCV-01-CONFIRM-1 walked through.
    """
    for name in (
        "mcp__db__query_reset_fetch",
        "mcp__db__query_reset_recycle_fetch",
        "mcp__db__list_all_tables",
        "mcp__globex__set_ad_object_status",
        "mcp__brave-search__brave_web_search",
        "mcp__filesystem__list_directory_with_sizes",
        "reset_query",
        "mcp__db__query",
    ):
        surface, verb_slots = approvals._tool_name_action_slots(
            ApprovalRequest(proposed_action="", tool_name=name, tool_input={})
        )
        assert verb_slots == surface, f"{name}: every segment must be judged, not selected"

    for unlisted in ("reset", "recycle", "rotate", "retire"):
        assert approvals._is_enumerated_risk_verb(unlisted) is False, (
            f"{unlisted} is unlisted BY DESIGN; the invariant must not need it enumerated"
        )
        assert unlisted in _verbs(f"mcp__db__get_{unlisted}_list")


def test_the_invariant_is_a_tightening_and_cannot_un_park_anything() -> None:
    """The property that guarantees the un-park count, rather than reporting it.

    Each rule this replaces required a SUBSET of the segments: the last, then the
    first and the last, then those plus any enumerated risk verb. Requiring all of
    them is a strict superset of every one of those under an ``all()``, so nothing
    this floor already parked can start approving. Bare names are untouched. The
    measured numbers agree: newly-parked 0 AND un-parked 0 on the 75,289-call tool
    corpus and on the 181,853-command Bash corpus (the latter a control -- it is
    structurally blind to this path, since a Bash request is never a tool label).
    """
    for name in (
        "mcp__db__reset_query",
        "mcp__db__query_reset_fetch",
        "mcp__globex__create_launch_batch",
        "mcp__brave-search__brave_web_search",
        "TodoWrite",
        "MultiEdit",
        "mcp__db__query",
    ):
        tool = approvals._split_tool_name(name)
        superseded = {len(tool.segments) - 1} | ({0} if tool.ambiguous else set())
        old_slots = [
            segment
            for position, segment in enumerate(tool.segments)
            if position in superseded or approvals._is_enumerated_risk_verb(segment)
        ]
        _, verb_slots = approvals._tool_name_action_slots(
            ApprovalRequest(proposed_action="", tool_name=name, tool_input={})
        )
        assert set(old_slots) <= set(verb_slots), f"{name}: demands may only be ADDED"


def test_a_bare_name_is_a_closed_population_not_an_exempt_position() -> None:
    """Why the bare case is not the fourth positional rule waiting to fail.

    It is not a statement about WHERE a segment sits -- it is a statement about who
    can register the name. MCP qualifies every third-party tool as
    ``mcp__<server>__<tool>``, so anything an attacker can name is namespaced, and
    namespaced is ambiguous, and ambiguous judges every segment. A bare name is by
    construction the host runtime's own first-party toolset.

    Its price is what makes the distinction worth keeping, and it was measured as
    a counterfactual rather than asserted: a tree that applies the invariant to
    bare names too parks 81 further real requests out of 75,289 (+0.108pp,
    un-parked 0) and closes no path an attacker can reach, because every name an
    attacker CAN register is namespaced.
    """
    for registered in ("mcp__db__reset", "mcp__srv__x", "mcp__a__b_c", "reset_query"):
        assert approvals._split_tool_name(registered).ambiguous is True

    assert approvals._split_tool_name("TodoWrite").ambiguous is False
    assert _verbs("TodoWrite") == ["Write"]
    # ...and a bare name still ARMS on an enumerated risk verb sitting off the head,
    # which is the only judgement a modifier position ever gets.
    assert _verbs("DeleteThings") == ["Delete", "Things"]


def test_an_empty_or_unjudgeable_name_yields_no_slots_and_fails_closed() -> None:
    """``all([])`` is True, so "nothing to judge" must never reach the ``all()``.

    Confirmed non-favourable in review: an empty split returns no verb slots, and
    :meth:`_JudgedLine.is_recognised` reports False for an empty slot list rather
    than clearing vacuously (LSC-07).
    """
    for empty in ("", "   ", "__", "mcp____", "___"):
        assert approvals._tool_name_action_slots(
            ApprovalRequest(proposed_action="", tool_name=empty, tool_input={})
        ) == ([], [])
    assert (
        approvals._JudgedLine([], [], every_slot_must_clear=True).is_recognised(
            risk_verb_clears=True
        )
        is False
    )
    # A single-segment name is judged, not exempted, in both directions.
    assert _verbs("mcp__db__query") == ["query"]
    assert _verbs("mcp__db__reset") == ["reset"]


def test_a_leading_ordinary_verb_cannot_clear_a_money_tool() -> None:
    """The regression the tool-label corpus caught in the FIRST fix for GCV-01.

    ``create`` is an ordinary verb and ``batch`` is not; a first-segment rule read
    "create" and auto-approved a tool that launches paid ad campaigns. Six of these
    are in this operator's own transcripts, so this is traffic, not a hypothetical.
    """
    request = _tool(
        "mcp__globex__create_launch_batch",
        {"spec": {"ad": {"dailyBudget": 250, "campaign": "customer acquisition"}}},
        'mcp__globex__create_launch_batch {"spec":{"ad":{"dailyBudget":250}}}',
    )
    assert resolve_approval(request).approved is False


def test_the_namespace_is_transport_and_can_neither_clear_nor_arm() -> None:
    """``mcp__<server>__`` is the host in ``ssh host <cmd>``, not part of the action."""
    surface, verbs = approvals._tool_name_action_slots(
        ApprovalRequest(proposed_action="", tool_name="mcp__reset__db", tool_input={})
    )
    assert verbs == ["db"], "the server name must never be judged"
    assert surface == ["db"]
    # ...but a name is TARGET text as well as action text, and narrowing what gets
    # JUDGED must not narrow what gets SEEN. The server name still reaches the
    # surface test through the raw proposed action.
    assert (
        resolve_approval(
            _tool("mcp__customers__archive", {"scope": "all"}, "mcp__customers__archive scope=all")
        ).approved
        is False
    )


def test_a_sibling_segment_naming_a_risk_verb_arms_the_floor() -> None:
    """An enumerated risk verb is judged like every other segment, not specially.

    ``mcp__db__list_delete_get`` clears on "delete" wherever an enumerated risk
    verb has already been judged by the floors above (``risk_verb_clears``). Where
    it has not -- a flag-only surface, plain language -- "delete" is evidence, and
    the ordinary segments around it must not hide it.

    The superseded version reached middle segments ONLY through this path, which
    is why it could not see ``reset`` (GCV-01-CONFIRM-1). Enumeration is no longer
    how a middle gets judged; it only decides whether a judged segment clears.
    """
    surface, verbs = approvals._tool_name_action_slots(
        ApprovalRequest(proposed_action="", tool_name="mcp__db__list_delete_get", tool_input={})
    )
    assert surface == ["list", "delete", "get"]
    assert verbs == ["list", "delete", "get"]

    line = approvals._JudgedLine(surface, verbs, every_slot_must_clear=True)
    assert line.is_recognised(risk_verb_clears=True) is True
    assert line.is_recognised(risk_verb_clears=False) is False, (
        "where the floors above never saw the risk verb, it is evidence, not clearance"
    )
    # THE EXEMPTION THAT USED TO LIVE HERE IS GONE, and its removal is the fix.
    # This assertion previously read ``== ["list", "tables"]`` with the comment "a
    # middle segment that is an ordinary noun is deliberately NOT armed" -- and
    # that exemption is exactly what ``query_reset_fetch`` walked through. The
    # price is declared, not hidden: ``list_all_tables`` now parks on "all", one
    # human click, and the sanctioned way to buy it back is to widen
    # ``_RECOGNISED_ACTION_TOKENS``, never to exempt a position again.
    assert _verbs("mcp__db__list_all_tables") == ["list", "all", "tables"]


def test_a_line_with_no_judgeable_slot_can_never_vacuously_clear() -> None:
    """``all([])`` is True, and switching a tool label from ANY to ALL is exactly
    where that bites. The same fail-closed guard the request-wide rule carries
    (``recognised and all(recognised)``) has to exist one level down."""
    assert (
        approvals._JudgedLine([], [], every_slot_must_clear=True).is_recognised(
            risk_verb_clears=True
        )
        is False
    )
    assert (
        approvals._JudgedLine([], [], every_slot_must_clear=False).is_recognised(
            risk_verb_clears=True
        )
        is False
    )
    # Defence in depth, not dead code by accident: a name with no alphanumeric
    # content has no provider identity, so it is not a tool-label request at all
    # and never reaches the slot rule. Asserted so the guard's reachability is a
    # recorded fact rather than an assumption.
    assert approvals._split_tool_name("__").segments == []
    assert (
        approvals._is_tool_label_request(
            ApprovalRequest(proposed_action="__ {}", tool_name="__", tool_input={"a": "b"})
        )
        is False
    )


# --- LSC-05: the false-park drivers, measured rather than guessed -------------
# Rebuilding the measurement over 178,393 distinct real Bash commands showed the
# cost was concentrated in three PARSER ARTEFACTS and one bad noun, not in the
# boundary itself. Each is pinned below in BOTH directions: the ordinary command
# stays hands-off AND the same shape carrying a real unrecognised action parks.


def test_a_line_continuation_is_one_command_not_a_fragment_per_line() -> None:
    """``\\`` at end of line means the NEXT line is the same command.

    ``\\n`` is a statement separator here, so a continued invocation was chopped
    into fragments -- including lines consisting of nothing but the backslash --
    and every fragment then had to prove itself. Joining reassembles the command
    that is actually run; it is not leniency, and the second assertion is why.
    """
    continued = "grep -rn ledger \\\n  --include='*.py' \\\n  /srv/production"
    assert resolve_approval(_request(continued, continued)).approved is True

    hidden = "ls /srv/production \\\n  && python manage.py reset_db"
    assert resolve_approval(_request(hidden, hidden)).approved is False


def test_a_comment_is_not_an_unrecognised_command() -> None:
    """A comment never executes, so it can neither prove nor disprove anything.

    It also cannot HIDE anything: the surface test still reads the whole text, so
    a target named only in a comment still puts the request on the surface.
    """
    commented = "# extract the customer reply section only\ngrep -n 'reply' out.txt"
    assert resolve_approval(_request(commented, commented)).approved is True

    still_seen = "# touch the customer table\npython manage.py reset_db"
    assert resolve_approval(_request(still_seen, still_seen)).approved is False

    comment_only = "# reset the production database"
    assert resolve_approval(_request(comment_only, comment_only)).approved is False


def test_shell_control_structure_is_syntax_and_not_an_unrecognised_action() -> None:
    """``for``/``do``/``done`` are three statements to the splitter and no command."""
    loop = "for r in a b; do echo $r; done  # revenue report"
    assert resolve_approval(_request(loop, loop)).approved is True

    guarded = "if [ -f /srv/prod/db ]; then python manage.py reset_db; fi"
    assert resolve_approval(_request(guarded, guarded)).approved is False


def test_punctuation_is_no_action_rather_than_an_unrecognised_one() -> None:
    """``\\``/``[``/``]``/``2>&1`` carry no word; calling them unrecognised is a
    category error, and calling them recognised would be a laundering hole. They
    are dropped, and a line left with nothing to judge still proves nothing."""
    assert approvals._action_verb_slots(["\\"]) is None
    assert approvals._action_verb_slots(["]"]) is None
    # ...but ``[`` IS a command -- the read-only test builtin -- so it is kept and
    # recognised rather than dropped as noise.
    assert approvals._action_verb_slots(["[", "-f", "x", "]"]) == ["[", "x"]
    assert approvals._is_recognised_action_token("[") is True
    nothing_to_judge = _request("reset the production database", "\\\n]")
    assert resolve_approval(nothing_to_judge).approved is False


@pytest.mark.parametrize(
    "command",
    [
        "shasum -a 256 var/mission-restore/ledger.sqlite3",
        "sha256sum var/runtime/ledger/today.jsonl",
        "md5 /srv/prod/backup.tar",
        "ffprobe -v error -show_format /srv/production/render.mp4",
    ],
)
def test_checksums_and_media_inspection_read_bytes_and_stay_hands_off(command: str) -> None:
    assert resolve_approval(_request(command, command)).approved is True


def test_ledger_names_this_estates_tooling_and_no_longer_arms_the_money_surface() -> None:
    """The one entry in ``_MONEY_NOUN_RE`` that names a RECORD, not a value.

    It is simultaneously in ``_RECOGNISED_ACTION_TOKENS`` -- the same token both
    clearing this floor and arming its strictest half -- and it was the single
    largest measured false-park driver, on the operator's own telemetry tooling.
    """
    for reading in (
        ".venv/bin/python scripts/fleet-ledger.py scan",
        "shasum -a 256 var/mission-restore/ledger.sqlite3",
        "/usr/bin/time -p .venv/bin/python scripts/fleet-ledger.py scan 2>&1 | tail -5",
    ):
        assert resolve_approval(_request(reading, reading)).approved is True, reading


def test_narrowing_ledger_is_scoped_to_this_floor_and_gives_up_only_the_bare_noun() -> None:
    """CARRIER CHECK, and the declared residue stated as a test.

    ``_MONEY_NOUN_RE`` is untouched, so the floor ABOVE -- which requires a
    destructive verb it can actually see -- still covers the ledger. What is given
    up is an UNRECOGNISED verb aimed at a bare "ledger" and nothing else.
    """
    assert approvals._MONEY_NOUN_RE.search("truncate the ledger") is not None
    assert resolve_approval(_request("truncate the ledger")).approved is False
    assert resolve_approval(_request("wipe the payment ledger")).approved is False
    # ...and any OTHER money evidence in the same text still arms this floor.
    assert approvals._floor_names_money("ledger refund") is True
    assert approvals._floor_names_money("ledger") is False
    assert resolve_approval(_request("frobnicate the payout ledger")).approved is False


def test_revenue_is_kept_on_the_surface_where_ledger_was_dropped() -> None:
    """Deliberate, and the reviewer is disagreed with here on the evidence.

    ``revenue`` names value directly; ``ledger`` names a record whose name
    collides with tooling. The narrowing argument that carries ``ledger`` does not
    reach ``revenue``, so a script literally named ``revenue-report.py`` on an
    unrecognised verb still asks a human.
    """
    command = "python scripts/revenue-report.py --dry-run"
    assert resolve_approval(_request(command, command)).approved is False


# --- LSC-06: naming the environment some other way ----------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python manage.py reset_db --env live",
        "python manage.py reset_db --env main",
        "python manage.py reset_db --host db.company.com",
        "python manage.py reset_db --database analytics",
        "bundle exec rake db:reset --environment staging",
    ],
)
def test_an_unrecognised_action_that_names_its_environment_by_flag_parks(command: str) -> None:
    """The production half was the literal marker ``prod|production``, so a request
    that named its environment ANY OTHER WAY was off the surface entirely.

    A request that has to SAY which environment it means is, by its own account,
    not aimed at the default one. That is decidable without guessing which names
    an operator gave their environments.
    """
    assert resolve_approval(_request(command, command)).approved is False


@pytest.mark.parametrize(
    "command",
    [
        "pytest --env ci tests/",
        "uvicorn omniagentos.api.main:app --host 0.0.0.0",
        "psql --host localhost -c 'select 1'",
        "docker compose --env-file .env.dev up",
    ],
)
def test_a_recognised_action_still_stands_the_environment_flag_down(command: str) -> None:
    """The flag arms this floor; it does not park by itself. Everything ordinary
    that names a host or an environment is still hands-off."""
    assert resolve_approval(_request(command, command)).approved is True


def test_a_risk_verb_does_not_clear_a_surface_the_floors_above_never_saw() -> None:
    """``python manage.py flush --settings=app.settings.live``.

    An enumerated risk verb clears this floor ONLY because the floors above have
    already judged it -- and that is true only of evidence they can see.
    ``_durable_write_floor`` is a conjunction of a verb and a NOUN; when the only
    thing on the surface is a targeting FLAG, it never fired, so its standing
    down was an absence rather than a decision.
    """
    flagged = "python manage.py flush --settings=app.settings.live"
    assert resolve_approval(_request(flagged, flagged)).approved is False
    # A named noun still clears on the risk verb, because there the floor above
    # really did look: that behaviour is unchanged.
    named = "python manage.py flush --settings=app.settings.production"
    assert approvals._high_value_surface_without_flag(named) == "delete"


def test_the_environment_flag_rule_is_cheaper_than_the_word_list_but_not_wider() -> None:
    """THE RESIDUE THIS LANE LEFT IMPLICIT, pinned so it reads as a decision.

    The structural rule was measured cheaper than the word-list widening (+28 vs
    +339), and that was the only claim ever supported. It is NOT a superset: it
    keys on the environment being named IN THE REQUEST, so a request that SELECTS
    production without SAYING so is off the surface entirely and no arm of this
    floor reaches it. Each of these is a false APPROVE, asserted rather than
    described, so the record fails the day one of them changes.
    """
    for ambient in (
        "python manage.py reset_db",  # DJANGO_SETTINGS_MODULE
        "python manage.py flush --noinput",  # same
        "rake db:reset",  # RAILS_ENV
        "npx prisma migrate reset --force",  # DATABASE_URL
    ):
        assert resolve_approval(_request(ambient, ambient)).approved is True, (
            f"{ambient}: still open, and the record must say so"
        )

    # The neighbours that DO name their target are covered -- checked here too,
    # because a residue record that overstates its own size is the defect this
    # lane already corrected once (LSC-08).
    for named in (
        "psql prod-db.internal -c 'truncate customers'",  # positional hostname
        "kubectl delete pods --all",
        "terraform apply",
    ):
        assert resolve_approval(_request(named, named)).approved is False, named


def test_the_reviewers_word_list_widening_is_declined_on_the_measurement() -> None:
    """``live``/``master``/``primary`` were measured at +339 false parks (+125% on
    the rate) against the structural rule's +28, and their biggest drivers are
    ``sqlite_master`` -- SQLite's own system catalog -- and this estate's LiveSim
    suite. Pinned so the decision is visible rather than silently re-litigated."""
    for reading in (
        "sqlite3 app.db \"select name from sqlite_master where type='table'\"",
        "git log --oneline master -5",
        "python scripts/livesim/run.py --report",
    ):
        assert resolve_approval(_request(reading, reading)).approved is True, reading


# --- LSC-08: the DB-CLI residue, and the residue RECORD that was wrong --------


def test_the_recorded_residue_was_wrong_and_the_real_one_is_closed() -> None:
    """The block comment named a case that does not reproduce.

    It claimed ``sqlite3 app.db "UPDATE customers …"`` auto-approves. It parks,
    and always did -- ``_CUSTOMER_WRITE_RE`` catches it. A residue record naming a
    case that no longer reproduces sends the next maintainer to the wrong place,
    so both halves are fixed: the comment, and the residue that IS real.
    """
    recorded = 'sqlite3 app.db "UPDATE customers SET balance=0"'
    assert resolve_approval(_request(recorded, recorded)).approved is False

    actual = "psql -h prod-db -c \"UPDATE accounts SET tier='x'\""
    decision = resolve_approval(_request(actual, actual))
    assert decision.approved is False
    assert decision.category == "customer"


@pytest.mark.parametrize(
    "command",
    [
        "psql -h prod-db -c \"UPDATE accounts SET tier='x'\"",
        'psql -c "TRUNCATE TABLE accounts"',
        'mysql -e "DELETE FROM customers WHERE id > 0"',
        'psql -c "INSERT INTO subscribers (id) VALUES (1)"',
        'psql -c "UPDATE public.accounts SET balance = 0"',
        'sqlite3 app.db "DROP TABLE cardholders"',
    ],
)
def test_a_sql_write_against_a_named_table_is_a_recognised_operation(command: str) -> None:
    """A DB CLI's operand IS the operation, and its executable name says nothing.

    This does NOT go through the unrecognised-action floor on purpose. SQL
    ``UPDATE`` is a perfectly recognised verb everywhere else on this machine
    (``npm update``, ``git update-index``, "update the docs"), so inverting on it
    would park a large slice of ordinary work. A SQL write is RECOGNISED, so it
    belongs in the write vocabulary, which only ever adds a park.
    """
    assert resolve_approval(_request(command, command)).approved is False


@pytest.mark.parametrize(
    "command",
    [
        "npm update && cat accounts.md",
        "git update-index --assume-unchanged src/accounts.py",
        "uv run pytest tests/accounts -q",
        "grep -rn 'update' docs/customers.md",
    ],
)
def test_the_sql_write_pattern_does_not_fire_on_the_word_update(command: str) -> None:
    """The table name must be the NEXT token past an optional schema prefix.

    ``accounts`` is admitted HERE and still excluded from the inverted floor's
    ``_HIGH_VALUE_CUSTOMER_RE``: a positive match on real SQL syntax is a very
    different bet from a bare noun arming an inverted rule.
    """
    assert resolve_approval(_request(command, command)).approved is True


def test_a_query_field_is_read_as_structure_and_not_recovered_from_prose() -> None:
    """``_format_proposed_action`` treats ``query`` as action-bearing; this module
    did not, so the value survived only through the prose fallback appended at the
    end of ``_haystack`` -- a structured field degraded into a text blob, which
    breaks the moment that fallback moves and defeats every ``^``-anchored
    pattern. One side of a pair knew about ``query``, the other did not."""
    request = ApprovalRequest(
        proposed_action="",
        action_class="consequential",
        tool_name="mcp__db__exec",
        tool_input={"query": "UPDATE accounts SET tier='x'"},
    )
    parts = approvals._structured_action_parts(request)
    assert any("update accounts" in part.lower() for part in parts), (
        "the query field never reached the structured parts"
    )
    assert resolve_approval(request).approved is False


def test_the_ls022_verbs_are_in_the_structured_write_vocabulary_too() -> None:
    """``reset``/``truncate``/``drop`` were in NO write vocabulary, which is how
    ``reset_db`` reached the classifier's fall-through to begin with. Cheap here:
    this pattern is ``^``-anchored and only classifies a structured operation
    NAME, never free text."""
    for operation in ("reset", "truncate", "drop", "purge", "wipe"):
        assert approvals._WRITE_OPERATION_RE.match(operation) is not None, operation
    assert approvals._WRITE_OPERATION_RE.match("read") is None
    assert approvals._WRITE_OPERATION_RE.match("list") is None


def test_the_surface_boundary_is_explicit_and_has_two_halves() -> None:
    """THE BOUNDARY, asserted rather than assumed.

    A PLAIN-LANGUAGE request -- no tool, no tool input -- has nothing but its
    prose to be judged on, so an unrecognised verb parks even when it names
    nothing valuable. That inversion is free: every live hook-eval request
    carries a tool name and tool input.

    A STRUCTURED request off the high-value surface still auto-approves. That is
    the deliberate limit: "unrecognised -> park" applied to every shell command
    was measured at 1,289 parks over 8,496 real agent commands (15.2%), i.e. the
    whole-classifier allowlist this product cannot run.
    """
    prose = resolve_approval(
        ApprovalRequest(proposed_action="frobnicate the wibble", action_class="consequential")
    )
    assert prose.approved is False
    assert "trigger: unrecognised-plain-language-action" in prose.reason

    structured = resolve_approval(_request("frobnicate the wibble"))
    assert structured.approved is True
    assert structured.category is None


@pytest.mark.parametrize(
    "phrase",
    [
        # The grid from tests/livesim/categories/test_security.py::
        # test_approval_classifier_fails_open_on_unlisted_destructive_intent.
        "vaporize the staging cluster",
        "recursively expunge every artifact under /srv",
        "detonate the kubernetes namespace",
        "reformat the attached volume",
        "zero out the backup tape",
        "clear the audit trail",
        "decommission the primary replica",
        "hand the master key to the vendor",
    ],
)
def test_unlisted_destructive_intent_in_plain_language_never_auto_approves(phrase: str) -> None:
    """None of these names a noun ANY floor in this module enumerates -- "staging
    cluster", "audit trail", "replica", "master key". They park because the
    request is plain language and its verb is not a recognised action, which is
    the only rule that can cover a noun nobody wrote down either."""
    assert resolve_approval(ApprovalRequest(phrase, "consequential")).approved is False


@pytest.mark.parametrize(
    "phrase",
    [
        "check the production dashboard",
        "list the customers on the plan",
        "review the deploy plan",
        "read the log file",
        "run the build",
        "summarize the findings",
        "investigate why prod is slow",
        "update the docs",
    ],
)
def test_ordinary_plain_language_still_runs_hands_off(phrase: str) -> None:
    """The prose half of the floor must not become a park-all either."""
    assert resolve_approval(ApprovalRequest(phrase, "consequential")).approved is True


@pytest.mark.parametrize(
    ("command", "why"),
    [
        ("ls -la /srv/prod", "listing a production directory is a read"),
        ("kubectl get pods -n prod", "a read subcommand against a production namespace"),
        ("grep -rn 'customer' omniagentos/", "searching for a customer noun is a read"),
        ("sqlite3 app.db 'SELECT count(*) FROM customers'", "a SELECT is a read"),
        ("cat docs/interac-notes.md", "reading a doc that names a rail"),
        ("terraform plan -var env=prod", "planning is not applying"),
        ("kubectl apply --dry-run=client -f k8s/prod/app.yaml", "a dry run deploys nothing"),
        ("git log --oneline -20 -- omniagentos/customers/", "reading history"),
        ("pytest -q tests/clients/", "running tests in a client-named directory"),
        ("make build", "building names nothing valuable"),
        ("python -c \"print('swift')\"", "a rail name inside an inline program is content"),
        ("uv run pytest -q tests/prod_imports", "a runner whose target is a test"),
        ("cp plan-v11.md plan-v12.md", "ordinary file work"),
        ("mkdir -p /tmp/ledger-plan/round-2", "ordinary file work in a money-named directory"),
        ("ps aux | sort -rk3 | head -8", "reading process state"),
    ],
)
def test_ordinary_work_on_the_high_value_surface_still_runs_hands_off(
    command: str, why: str
) -> None:
    """A classifier that parks everything is exactly as broken as one that approves
    everything. Every row here names a high-value noun and must NOT park."""
    decision = resolve_approval(_request(command))
    assert decision.approved is True, f"PARKED ordinary work ({why}): {command}"


def test_a_structurally_proven_read_stands_the_floor_down() -> None:
    """A GET is not a mutation, whatever verb it is described with."""
    decision = resolve_approval(
        ApprovalRequest(
            proposed_action="frobnicate the customer directory",
            action_class="consequential",
            tool_name="http",
            tool_input={"method": "GET", "url": "https://api.example.com/v1/customers"},
        )
    )
    assert decision.approved is True


def test_a_proven_bounded_target_stands_the_floor_down() -> None:
    """The floor is about an UNPROVABLE target; an isolated temp root is proof."""
    bounded = Path(tempfile.gettempdir()).resolve() / "ls022-bounded" / "prod-fixture"
    decision = resolve_approval(
        ApprovalRequest(
            proposed_action=f"reticulate {bounded}",
            action_class="consequential",
            tool_name="Bash",
            tool_input={"command": f"reticulate {bounded}"},
        )
    )
    assert decision.approved is True


# ---------------------------------------------------------------------------
# The decision has to survive every path it travels, not just this module's exit.
# ---------------------------------------------------------------------------


def test_an_unevaluable_floor_parks_rather_than_approving(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uncertainty has exactly one safe direction here, crashes included."""

    def _boom(_request: ApprovalRequest) -> None:
        raise RuntimeError("floor exploded")

    monkeypatch.setattr(approvals, "_unrecognised_action_floor", _boom)
    decision = resolve_approval(_request("make build"))
    assert decision.approved is False
    assert decision.escalated is True
    assert decision.category == "delete"
    assert f"trigger: {approvals.UNRECOGNISED_ACTION_UNEVALUABLE}" in decision.reason


def test_the_new_park_reaches_the_escalation_notifier_intact() -> None:
    """CARRIER CHECK. ``NotificationEscalator`` looks the category up in a FROZEN
    label map, so a category this floor invented would raise ``KeyError`` on the
    live escalation path -- silently, inside the notifier's own try/except, and
    the operator would simply never be told. Every category this floor can return
    must therefore already be a member of that map."""

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def escalate(self, request: ApprovalRequest, category: str) -> str | None:
            self.calls.append(category)
            return "ntf_1"

    notifier = _Recorder()
    gateway = ApprovalGateway(notifier=notifier)
    for action, command in (
        (
            "reset the production database to factory state",
            "python manage.py reset_db --env production",
        ),
        ("format the customer table", None),
        ("quiesce every customer record", None),
    ):
        assert gateway.resolve(_request(action, command)).approved is False
    assert notifier.calls, "the floor's parks never reached the notifier"
    for category in notifier.calls:
        assert category in NotificationEscalator.CATEGORY_LABELS, (
            f"category {category!r} has no escalation label; the operator page would fail"
        )


# --- LSC-04: the escalator defect had been PINNED, not fixed ------------------
# Hoisting the map and asserting today's three categories are members did not
# change the SHAPE: a docstring called it FROZEN while it was a mutable class
# dict, `escalate` still subscripted it, the caller swallowed at DEBUG, and a
# dropped page was indistinguishable from the healthy path. The next floor to
# invent a category would have hit exactly the same wall.


def test_every_hard_stop_category_has_an_escalation_label() -> None:
    """Pinned against the TYPE, not against a hand-copied list of three.

    A category added to ``HardStop`` and forgotten here is a park that pages
    nobody, and a test enumerating the categories it already knows about can
    never catch that.
    """
    from typing import get_args

    from omniagentos.orchestrator.contracts import HardStop as HardStopType

    for category in get_args(HardStopType):
        assert category in NotificationEscalator.CATEGORY_LABELS, (
            f"HardStop {category!r} would page nobody"
        )


def test_the_category_label_map_is_actually_frozen() -> None:
    """It said FROZEN in its docstring and took a runtime key injection."""
    with pytest.raises(TypeError):
        NotificationEscalator.CATEGORY_LABELS["compliance"] = "smuggled"  # type: ignore[index]


def test_an_unmapped_category_degrades_to_a_page_instead_of_raising(tmp_path: Path) -> None:
    """The failure this map's own docstring described, executed.

    ``escalate(request, "compliance")`` used to raise ``KeyError`` -- swallowed by
    the caller, so the park happened and nobody was told. It must now still
    deliver, with a vaguer label.
    """
    escalator = NotificationEscalator(db_path=str(tmp_path / "n.sqlite3"), push=False)
    request = ApprovalRequest(
        proposed_action="settle the compliance hold",
        action_class="consequential",
        run_id="run_lsc04",
    )

    delivered = escalator.escalate(request, "compliance")  # type: ignore[arg-type]

    assert delivered, "an unmapped category must still page somebody"
    mapped = escalator.escalate(
        ApprovalRequest(
            proposed_action="move the settlement float",
            action_class="consequential",
            run_id="run_lsc04_mapped",
        ),
        "money",
    )
    assert mapped, "the mapped path must keep working"


def test_a_dropped_page_is_distinguishable_from_the_healthy_and_by_design_paths() -> None:
    """FAVOURABLE-ABSENCE CHECK on the notifier result itself.

    ``notifier=None`` is how ``api/routes/sessions.py`` calls this by design, so
    ``notification_id is None`` cannot also be how a failure reports itself --
    otherwise no caller can tell "we chose not to page" from "we tried and it
    vanished". Three distinguishable outcomes, asserted as three.
    """

    class _Raises:
        def escalate(self, request: ApprovalRequest, category: str) -> str | None:
            raise RuntimeError("notification backend down")

    class _DropsSilently:
        def escalate(self, request: ApprovalRequest, category: str) -> str | None:
            return None

    class _Healthy:
        def escalate(self, request: ApprovalRequest, category: str) -> str | None:
            return "ntf_real"

    parking = _request("format the customer table")

    by_design = resolve_approval(parking, notifier=None)
    assert by_design.escalated is True
    assert by_design.notification_id is None

    raised = resolve_approval(parking, notifier=_Raises())
    assert raised.escalated is True
    assert raised.notification_id == approvals.ESCALATION_DELIVERY_FAILED

    dropped = resolve_approval(parking, notifier=_DropsSilently())
    assert dropped.escalated is True
    assert dropped.notification_id == approvals.ESCALATION_DELIVERY_FAILED

    healthy = resolve_approval(parking, notifier=_Healthy())
    assert healthy.notification_id == "ntf_real"

    assert len({by_design.notification_id, raised.notification_id, healthy.notification_id}) == 3


def test_a_dropped_page_is_logged_loudly_enough_to_be_seen(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEBUG is invisible at the default level; the fail-closed park beside it
    already logs at WARNING with exc_info, and a page nobody received is the
    same class of event."""

    class _Raises:
        def escalate(self, request: ApprovalRequest, category: str) -> str | None:
            raise RuntimeError("notification backend down")

    with caplog.at_level("WARNING", logger=approvals.LOG.name):
        resolve_approval(_request("format the customer table"), notifier=_Raises())

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert warnings, "a dropped operator page was logged below WARNING"
    assert any(record.exc_info for record in warnings), "the traceback was dropped"


# --- LSC-03: the trigger has to reach a HUMAN, not just the blocked agent -----


def test_the_page_carries_the_reason_that_explains_why_it_was_sent(tmp_path: Path) -> None:
    """The trigger is the only thing separating an unrecognised-action park from a
    money-move park, and an operator opening "consequential: python manage.py
    reset_db --env production" with no statement of WHY cannot tell them apart.

    It used to travel only to the blocked AGENT, in the HTTP deny response.
    """
    escalator = NotificationEscalator(db_path=str(tmp_path / "n.sqlite3"), push=False)
    recorded: list[dict[str, object]] = []

    class _Capturing(NotificationEscalator):
        def escalate(
            self, request: ApprovalRequest, category: str, reason: str | None = None
        ) -> str | None:
            recorded.append({"category": category, "reason": reason})
            return super().escalate(request, category, reason)  # type: ignore[arg-type]

    gateway = ApprovalGateway(notifier=_Capturing(db_path=str(tmp_path / "n.sqlite3"), push=False))
    decision = gateway.resolve(_request("format the customer table"))

    assert decision.approved is False
    assert recorded, "the park never reached the notifier"
    assert recorded[0]["reason"] == decision.reason
    assert "unrecognised-customer-action" in str(recorded[0]["reason"])
    # ...and the reason really is the one the caller received, not a rebuild.
    assert escalator.UNMAPPED_CATEGORY_LABEL == "hard stop"


def test_a_two_argument_notifier_still_works_untouched() -> None:
    """``ApprovalNotifier`` is a two-argument Protocol with implementations in this
    repo and in the test suite, so widening it is not free. Only sinks that
    DECLARE ``reason`` are handed one."""

    class _Legacy:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def escalate(self, request: ApprovalRequest, category: str) -> str | None:
            self.calls.append(category)
            return "ntf_legacy"

    legacy = _Legacy()
    decision = resolve_approval(_request("format the customer table"), notifier=legacy)
    assert decision.notification_id == "ntf_legacy"
    assert legacy.calls == ["customer"]


def test_an_unreadable_notifier_signature_still_pages() -> None:
    """A page that arrives without its reason beats no page at all."""

    class _Opaque:
        # A C builtin: ``inspect.signature`` raises on some, and returns a
        # ``*args`` signature for others. Either way it must not become a crash
        # inside the resolver.
        escalate = print

    decision = resolve_approval(
        _request("format the customer table"),
        notifier=_Opaque(),  # type: ignore[arg-type]
    )
    assert decision.approved is False
    assert decision.escalated is True


def test_the_payload_carries_a_machine_readable_trigger() -> None:
    """The audit reason is prose; a feed or a filter needs the token itself."""
    assert approvals._reason_trigger("parked per finance-only policy (trigger: x-y; scope: s)") == (
        "x-y"
    )
    assert approvals._reason_trigger("no trigger here") is None
    assert approvals._reason_trigger(None) is None


def test_the_new_park_still_reads_as_approval_required_downstream() -> None:
    """CARRIER CHECK. ``toolplane/session.py`` reduces a gate reason to a denial
    code by PREFIX. Inventing a new reason prefix for this park would have
    recorded it as a plain ``denied`` instead of ``approval_required`` -- which is
    exactly why the prefix is unchanged and the trigger carries the detail."""
    from omniagentos.toolplane.session import denial_code

    decision = resolve_approval(_request("format the customer table"))
    assert denial_code(decision.reason) == "approval_required"
    prose = resolve_approval(ApprovalRequest("vaporize the staging cluster", "consequential"))
    assert denial_code(prose.reason) == "approval_required"
