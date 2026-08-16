"""U-M1 authorization at the ``recall()`` call site — decided against real hits.

WHY THIS FILE LOOKS THE WAY IT DOES
-----------------------------------
The first version of these tests asserted ``result == []`` against backends
that were empty anyway, and ended its self-described "DECISIVE TEST" on
``assert isinstance(result_b, list)``. Every one of them stayed green with
``_is_authorized`` hard-wired to ``True`` — i.e. with the entire deny-by-default
boundary deleted. A denial test that passes when nothing is denied decides
nothing.

So every leg here is SEEDED with a distinctive marker string, and the assertion
is about whether that marker reaches the caller:

* an ALLOWED leg must surface its marker (proving the query really ran, so an
  empty result can never be mistaken for a denial), and
* a DENIED leg must never surface its marker AND must never have been queried.

Removing the authorization filter turns the denial tests red, which is the only
property that makes them worth running.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from omniagentos.context.lanes import AccessDesignation, LaneProfile, load_profile
from omniagentos.retrieval import fusion
from omniagentos.retrieval.recall import recall

CONVERSATION_MARKER = "conversation-leg-marker-6f21"
METACOG_MARKER = "metacog-leg-marker-6f21"


@dataclass
class _Turn:
    seq: int
    content: str


@dataclass
class _Memory:
    id: str
    statement: str


class _SeededConversations:
    """A conversation leg that always has something to return, and counts asks."""

    def __init__(self) -> None:
        self.queries: list[tuple[str, str]] = []

    def recent_turns(self, scope_type: str, scope_id: str, limit: int) -> list[_Turn]:
        self.queries.append((scope_type, scope_id))
        return [_Turn(seq=1, content=CONVERSATION_MARKER)]


class _SeededMetacog:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search_memory(self, query: str, **kwargs: Any) -> list[_Memory]:
        self.queries.append(query)
        return [_Memory(id="mem_auth_fixture", statement=METACOG_MARKER)]


@pytest.fixture(autouse=True)
def seeded() -> Iterator[dict[str, Any]]:
    """Isolate the process-global fusion registry and install seeded legs."""
    import omniagentos.context.lanes as lanes_module
    import omniagentos.retrieval.recall as recall_mod

    before = fusion.registered_backends()
    for name in list(before):
        fusion.unregister_backend(name)

    saved_hooks = (
        recall_mod._conversation_store_factory,
        recall_mod._metacog_store_factory,
        recall_mod._knowledge_search,
        recall_mod._vault_search,
    )
    saved_profile = lanes_module._GLOBAL_PROFILE

    conversations = _SeededConversations()
    metacog = _SeededMetacog()
    recall_mod._conversation_store_factory = lambda: conversations
    recall_mod._metacog_store_factory = lambda: metacog
    recall_mod._knowledge_search = lambda query, limit: []
    recall_mod._vault_search = lambda query, limit: []

    try:
        yield {"conversations": conversations, "metacog": metacog}
    finally:
        for name in list(fusion.registered_backends()):
            fusion.unregister_backend(name)
        for spec in before.values():
            fusion.register_backend(spec)
        (
            recall_mod._conversation_store_factory,
            recall_mod._metacog_store_factory,
            recall_mod._knowledge_search,
            recall_mod._vault_search,
        ) = saved_hooks
        lanes_module._GLOBAL_PROFILE = saved_profile


def _install_profile(profile: LaneProfile) -> None:
    import omniagentos.context.lanes as lanes_module

    lanes_module._GLOBAL_PROFILE = profile


def _row(
    *,
    surface: str = "conversation",
    scope: str | None = "task",
    mode: str = "read",
    holder: str = "lane:runner.step",
) -> AccessDesignation:
    return AccessDesignation(
        holder=holder,
        surface=surface,
        scope=scope,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        grant_ref="fixture grant",
        revocation="drop the fixture row",
    )


def _texts(lines: list[Any]) -> str:
    return " | ".join(line.text for line in lines)


# ---------------------------------------------------------------------------
# The seeded legs really do produce hits (so "empty" always means "denied")
# ---------------------------------------------------------------------------


def test_the_fixture_leg_surfaces_its_marker_without_authorization(
    seeded: dict[str, Any],
) -> None:
    """Control. If this ever goes empty, every denial test below is vacuous."""
    lines = recall(
        "anything",
        scope=("task", "tsk_control"),
        top_k=5,
        sources=["conversation"],
        holder=None,
    )
    assert CONVERSATION_MARKER in _texts(lines)
    assert seeded["conversations"].queries == [("task", "tsk_control")]


# ---------------------------------------------------------------------------
# Denials actually deny
# ---------------------------------------------------------------------------


def test_a_holder_with_no_row_never_reaches_the_backend(seeded: dict[str, Any]) -> None:
    """Deny-by-default: no row means the leg is not even asked."""
    _install_profile(load_profile([]))

    lines = recall(
        "anything",
        scope=("task", "tsk_1"),
        top_k=5,
        sources=["conversation"],
        holder="lane:runner.step",
    )

    assert lines == []
    assert CONVERSATION_MARKER not in _texts(lines)
    assert seeded["conversations"].queries == [], (
        "a denied leg must not be queried at all; denial after the read is not a "
        "boundary"
    )


def test_a_non_canonical_holder_is_denied_while_the_canonical_one_reads(
    seeded: dict[str, Any],
) -> None:
    """Both halves in one test, so 'deny everything' cannot pass either."""
    _install_profile(load_profile([_row()]))

    denied = recall(
        "anything",
        scope=("task", "tsk_1"),
        top_k=5,
        sources=["conversation"],
        holder="agent:bob",
    )
    allowed = recall(
        "anything",
        scope=("task", "tsk_1"),
        top_k=5,
        sources=["conversation"],
        holder="lane:runner.step",
    )

    assert CONVERSATION_MARKER not in _texts(denied)
    assert CONVERSATION_MARKER in _texts(allowed)


def test_only_the_granted_surface_is_readable(seeded: dict[str, Any]) -> None:
    """A row for one surface is not a key to the next one."""
    _install_profile(load_profile([_row(surface="metacog", scope=None)]))

    lines = recall(
        "anything",
        scope=("task", "tsk_1"),
        top_k=5,
        sources=["conversation", "metacog"],
        holder="lane:runner.step",
    )

    text = _texts(lines)
    assert METACOG_MARKER in text, "the granted surface must still be readable"
    assert CONVERSATION_MARKER not in text, "the ungranted surface leaked"
    assert seeded["conversations"].queries == []


def test_a_cross_scope_read_is_denied_while_the_in_scope_one_reads(
    seeded: dict[str, Any],
) -> None:
    """The scope check is a real filter, not a shape assertion."""
    _install_profile(load_profile([_row(scope="task")]))

    wrong_scope = recall(
        "anything",
        scope=("project", "prj_1"),
        top_k=5,
        sources=["conversation"],
        holder="lane:runner.step",
    )
    right_scope = recall(
        "anything",
        scope=("task", "tsk_1"),
        top_k=5,
        sources=["conversation"],
        holder="lane:runner.step",
    )

    assert CONVERSATION_MARKER not in _texts(wrong_scope)
    assert CONVERSATION_MARKER in _texts(right_scope)


# ---------------------------------------------------------------------------
# K4.2 — scope bypass by omission
# ---------------------------------------------------------------------------


def test_a_scoped_row_is_not_satisfied_by_an_unscoped_query() -> None:
    """``scope=None`` against a task-scoped row asks for EVERY task at once.

    ``LaneProfile.authorize`` used to check the scope only when BOTH sides
    supplied one, so omitting the scope skipped the check entirely and a
    task-scoped designation authorized an unscoped read — reachable today
    through ``recall(query, holder=..., scope=None)``. Omission is not a
    narrower request.
    """
    profile = load_profile([_row(scope="task")])

    assert profile.authorize("lane:runner.step", "conversation", None, "read").allowed is False
    assert (
        profile.authorize("lane:runner.step", "conversation", ("task", "t1"), "read").allowed
        is True
    )
    assert (
        profile.authorize("lane:runner.step", "conversation", ("project", "p1"), "read").allowed
        is False
    )
    # An UNSCOPED designation is unaffected: it never asked for a scope.
    unscoped = load_profile([_row(scope=None)])
    assert unscoped.authorize("lane:runner.step", "conversation", None, "read").allowed is True


def test_an_unscoped_query_cannot_read_a_scoped_leg_through_recall(
    seeded: dict[str, Any],
) -> None:
    """The same defect at the call site that can actually reach it."""
    _install_profile(load_profile([_row(scope="task")]))

    lines = recall(
        "anything",
        scope=None,
        top_k=5,
        sources=["conversation"],
        holder="lane:runner.step",
    )

    assert CONVERSATION_MARKER not in _texts(lines)
    assert seeded["conversations"].queries == []


# ---------------------------------------------------------------------------
# K4.1 — mode escalation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("designated", "requested", "expected"),
    [
        ("read", "read", True),
        ("read", "write", False),
        # The escalation: a read-only row must not satisfy a read-write ask.
        ("read", "read-write", False),
        ("write", "write", True),
        ("write", "read", False),
        ("write", "read-write", False),
        ("read-write", "read", True),
        ("read-write", "write", True),
        ("read-write", "read-write", True),
    ],
)
def test_the_designated_mode_is_a_ceiling_not_a_label(
    designated: str, requested: str, expected: bool
) -> None:
    """Every cell of the mode matrix, including the two that were wrong."""
    profile = load_profile([_row(scope=None, mode=designated)])
    receipt = profile.authorize(
        "lane:runner.step",
        "conversation",
        ("task", "tsk_1"),
        requested,  # type: ignore[arg-type]
    )
    assert receipt.allowed is expected, (
        f"a {designated!r} designation "
        f"{'allowed' if receipt.allowed else 'refused'} a {requested!r} request"
    )


def test_the_public_api_cannot_be_used_to_escalate_a_read_row() -> None:
    """The reachable shape of K4.1: through ``authorize_memory_access``."""
    from omniagentos.context.lanes import authorize_memory_access

    _install_profile(load_profile([_row(scope=None, mode="read")]))

    assert authorize_memory_access("lane:runner.step", "conversation", None, "read").allowed
    assert not authorize_memory_access(
        "lane:runner.step", "conversation", None, "read-write"
    ).allowed
    assert not authorize_memory_access("lane:runner.step", "conversation", None, "write").allowed


# ---------------------------------------------------------------------------
# K4.3 — a broken authorizer authorizes nothing
# ---------------------------------------------------------------------------


def test_an_authorization_fault_denies_rather_than_releasing_the_boundary(
    seeded: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A LaneProfile fault used to switch the whole enforcement layer off.

    ``except Exception: return True`` meant a corrupt seed, an import error or
    a plain bug in lanes.py silently authorized every leg for every holder —
    and nothing observed it. The degradation is now a denial plus a loud log.
    """
    import omniagentos.retrieval.recall as recall_mod

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("LaneProfile seeds are corrupt")

    monkeypatch.setattr("omniagentos.context.lanes.authorize_memory_access", _explode)

    with caplog.at_level(logging.ERROR, logger=recall_mod.__name__):
        lines = recall(
            "anything",
            scope=("task", "tsk_1"),
            top_k=5,
            sources=["conversation", "metacog"],
            holder="lane:runner.step",
        )

    assert lines == []
    assert seeded["conversations"].queries == []
    assert seeded["metacog"].queries == []
    assert "authorization is unavailable" in caplog.text, (
        "a silently degraded boundary is indistinguishable from no boundary"
    )


def test_a_fault_cannot_release_a_leg_a_healthy_profile_would_deny(
    seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property stated directly: what a fault permits ⊆ what health permits."""
    _install_profile(load_profile([]))
    healthy = recall(
        "anything",
        scope=("task", "tsk_1"),
        top_k=5,
        sources=["conversation"],
        holder="lane:runner.step",
    )
    assert healthy == []

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("broken")

    monkeypatch.setattr("omniagentos.context.lanes.authorize_memory_access", _explode)
    faulted = recall(
        "anything",
        scope=("task", "tsk_1"),
        top_k=5,
        sources=["conversation"],
        holder="lane:runner.step",
    )
    assert faulted == []
    assert seeded["conversations"].queries == []


# ---------------------------------------------------------------------------
# Backward compatibility (holder=None is still today's behavior)
# ---------------------------------------------------------------------------


def test_no_holder_means_no_authorization_and_no_change_in_behavior(
    seeded: dict[str, Any],
) -> None:
    _install_profile(load_profile([]))  # denies everything, if consulted

    lines = recall(
        "anything",
        scope=("task", "tsk_1"),
        top_k=5,
        sources=["conversation", "metacog"],
        holder=None,
    )

    text = _texts(lines)
    assert CONVERSATION_MARKER in text
    assert METACOG_MARKER in text


def test_recall_never_raises_for_any_holder_spelling(seeded: dict[str, Any]) -> None:
    """Denial is a value, not an exception — callers must not need a try block."""
    for holder in ["lane:runner.step", "lane:chat", "agent:bob", "invalid", "*"]:
        lines = recall(
            "anything",
            scope=("task", "tsk_1"),
            top_k=5,
            sources=["conversation"],
            holder=holder,
        )
        assert isinstance(lines, list)


def test_recall_still_honors_top_k_and_unknown_sources(seeded: dict[str, Any]) -> None:
    _install_profile(load_profile([_row(scope=None)]))

    assert recall("anything", scope=("task", "t"), top_k=0, sources=["conversation"]) == []
    assert (
        recall(
            "anything",
            scope=("task", "t"),
            top_k=3,
            sources=["not_a_backend"],
            holder="lane:runner.step",
        )
        == []
    )


# ---------------------------------------------------------------------------
# Registry load rules (unchanged behavior, kept pinned)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("holder", ["*", "all-agents", "agent:bob", "system:foo", ""])
def test_a_wildcard_or_non_canonical_row_fails_load(holder: str) -> None:
    with pytest.raises(ValueError, match="Non-canonical holder"):
        _row(holder=holder)


@pytest.mark.parametrize("revocation", ["", "none", "None", "*", "revoke *"])
def test_a_row_without_a_meaningful_revocation_fails_load(revocation: str) -> None:
    with pytest.raises(ValueError, match="revocation"):
        AccessDesignation(
            holder="lane:runner.step",
            surface="conversation",
            scope="task",
            mode="read",
            grant_ref="fixture grant",
            revocation=revocation,
        )


def test_gap05_none_holder_bypass_is_logged_not_silent(caplog):
    """GAP-05 step 1: the bypass must be OBSERVABLE, with behaviour unchanged.

    No production caller threads a holder, so this branch is the one every real
    call takes and the authorization block below it has never executed. Until
    the operator rules on the semantics, the requirement is that it stops being silent —
    an operator (and the ledger) can see the top-of-lattice default firing, and
    which surface asked for it.
    """
    import logging

    from omniagentos.retrieval.recall import _is_authorized

    with caplog.at_level(logging.WARNING, logger="omniagentos.retrieval.recall"):
        allowed = _is_authorized(None, "vault", ("project", "omniagentos"))

    assert allowed is True, "step 1 must NOT change behaviour — observe only"
    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert records, "the bypass must announce itself, not pass in silence"
    message = records[0].getMessage()
    assert "BYPASSED" in message
    assert "vault" in message, "the refused-nothing surface must be named"
