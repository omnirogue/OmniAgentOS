"""Every reconciler input is in the board ETag — one named case per source.

A weak ETag is a promise: "nothing that can change this feed has moved". The
board reconciler reads a dozen tables, and any one of them missing from the
stamp turns that promise into a lie the client cannot detect — a ``304`` for a
card whose company classification, blocked reason, chat title, approval or step
progress has already changed.

So the stamp's sources are enumerated in ONE place
(:data:`omniagentos.api.routes.intake._BOARD_STAMP_SOURCES`) and every entry has
a case here. ``test_every_stamped_source_has_a_coverage_case`` compares the two
lists, so a source added to the stamp without a case fails by name, and a case
whose source was dropped fails too. Each case then mutates that table exactly
the way the system does — including the UPDATE-in-place mutations (a project
reclassified, a step completed, an attempt closed) that a COUNT/MAX-id stamp
would sleep through.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import omniagentos.api.main  # noqa: F401 -- break the package's documented import cycle.
from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.api.routes.intake import _BOARD_STAMP_SOURCES
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from tests.support.db_template import make_store

_T0 = "2026-08-01T00:00:00Z"
_T1 = "2026-08-09T00:00:00Z"

Mutation = Callable[[CollabStore], None]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _write(collab: CollabStore, sql: str, params: tuple[Any, ...] = ()) -> None:
    collab._store._write(sql, params)


@pytest.fixture
def collab(tmp_path: Path) -> CollabStore:
    return make_store(CollabStore, tmp_path / "board-etag.db")


@pytest.fixture
def client(collab: CollabStore) -> Iterator[httpx.AsyncClient]:
    app.dependency_overrides[get_store] = lambda: collab._store
    app.dependency_overrides[get_collab_store] = lambda: collab
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield http
    finally:
        app.dependency_overrides.clear()


def _card(collab: CollabStore, title: str) -> str:
    card = BoardTask(title=title, description=title)
    collab.create_board_task(card)
    return card.id


@pytest.fixture
def seeded(collab: CollabStore) -> str:
    """One visible card plus one pre-existing row in every stamped table.

    The UPDATE-in-place cases need something to update; seeding them all up
    front also means the baseline tag already covers a populated database
    rather than a set of empty aggregates.
    """
    board_id = _card(collab, "stamped card")
    companion_id = _card(collab, "chat companion")
    _write(
        collab,
        "INSERT INTO tasks (id, title, created_at, updated_at) VALUES ('tsk_stamp', 't', ?, ?)",
        (_T0, _T0),
    )
    _write(
        collab,
        "INSERT INTO runs (id, task_id, harness, state, created_at, updated_at, queued_at, "
        "trace_id) VALUES ('run_stamp', 'tsk_stamp', 'mock', 'running', ?, ?, ?, 'trc_stamp')",
        (_T0, _T0, _T0),
    )
    _write(
        collab,
        "INSERT INTO steps (run_id, seq, name, status) VALUES ('run_stamp', 0, 'plan', 'pending')",
    )
    _write(
        collab,
        "INSERT INTO sessions (id, source, project_dir, state, created_at, updated_at) "
        "VALUES ('ses_stamp', 'bridge', '/tmp', 'running', ?, ?)",
        (_T0, _T0),
    )
    _write(
        collab,
        "INSERT INTO approvals (id, action_class, proposed_action, state, created_at) "
        "VALUES ('apr_stamp', 'consequential', 'Bash', 'pending', ?)",
        (_T0,),
    )
    _write(
        collab,
        "INSERT INTO chats (id, board_task_id, title, status, created_at, updated_at) "
        "VALUES ('cht_stamp', ?, 'companion', 'active', ?, ?)",
        (companion_id, _T0, _T0),
    )
    # Terminal on purpose: a running orchestration with an old heartbeat would
    # be swept to failed by the reconciler itself, which is a WRITE, not the
    # mutation under test.
    _write(
        collab,
        "INSERT INTO orchestrations (id, board_task_id, status, created_at, updated_at) "
        "VALUES ('orch_stamp', ?, 'completed', ?, ?)",
        (board_id, _T0, _T0),
    )
    # Seeded CLOSED: both ledgers carry a partial unique index allowing at most
    # ONE live attempt per card, so an open seed would make the insert cases
    # illegal writes rather than mutations.
    _write(
        collab,
        "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, seq, provider, model, "
        "started_at, ended_at, end_reason) "
        "VALUES ('swa_stamp', 'swr_stamp', ?, 0, 'claude', 'opus', ?, ?, 'crashed')",
        (board_id, _T0, _T0),
    )
    _write(
        collab,
        "INSERT INTO task_sessions (id, board_task_id, seq, harness, model, started_at, "
        "ended_at, end_reason) VALUES ('tks_stamp', ?, 0, 'cli-claude', 'opus', ?, ?, 'crashed')",
        (board_id, _T0, _T0),
    )
    _write(
        collab,
        "INSERT INTO task_categories (id, name, slug, created_at, updated_at) "
        "VALUES ('cat_stamp', 'Ops', 'ops', ?, ?)",
        (_T0, _T0),
    )
    for suffix in ("a", "b"):
        _write(
            collab,
            "INSERT INTO org_companies (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
            (f"co_{suffix}", f"company-{suffix}", f"Company {suffix}", _T0),
        )
    _write(
        collab,
        "INSERT INTO projects (id, name, created_at, org_company_id) "
        "VALUES ('prj_stamp', 'Stamped', ?, 'co_a')",
        (_T0,),
    )
    collab.update_board_task(board_id, {"project_id": "prj_stamp"})
    return board_id


# One entry per stamped source; the keys are compared against the stamp's own
# enumeration by test_every_stamped_source_has_a_coverage_case.
_MUTATIONS: dict[str, dict[str, Mutation]] = {
    "events": {
        # Append-only: an event is never updated in place.
        "append": lambda c: _write(
            c,
            "INSERT INTO events (ts, type, actor, action) "
            "VALUES (?, 'board.updated', 'api', 'updated')",
            (_T1,),
        ),
    },
    "runs": {
        "insert": lambda c: _write(
            c,
            "INSERT INTO runs (id, task_id, harness, state, created_at, updated_at, queued_at, "
            "trace_id) VALUES ('run_new', 'tsk_stamp', 'mock', 'queued', ?, ?, ?, 'trc_new')",
            (_T1, _T1, _T1),
        ),
        "finish": lambda c: _write(
            c,
            "UPDATE runs SET state = 'completed', updated_at = ? WHERE id = 'run_stamp'",
            (_T1,),
        ),
    },
    "sessions": {
        "insert": lambda c: _write(
            c,
            "INSERT INTO sessions (id, source, project_dir, state, created_at, updated_at) "
            "VALUES ('ses_new', 'bridge', '/tmp', 'running', ?, ?)",
            (_T1, _T1),
        ),
        "advance": lambda c: _write(
            c,
            "UPDATE sessions SET state = 'completed', updated_at = ? WHERE id = 'ses_stamp'",
            (_T1,),
        ),
    },
    "steps": {
        "insert": lambda c: _write(
            c,
            "INSERT INTO steps (run_id, seq, name, status) VALUES ('run_stamp', 1, 'apply', "
            "'pending')",
        ),
        # No timestamp is written here on purpose: a step's progress can move
        # with nothing but a status flip, which is what the board projects.
        "complete": lambda c: _write(
            c, "UPDATE steps SET status = 'completed' WHERE run_id = 'run_stamp' AND seq = 0"
        ),
    },
    "approvals": {
        "insert": lambda c: _write(
            c,
            "INSERT INTO approvals (id, action_class, proposed_action, state, created_at) "
            "VALUES ('apr_new', 'consequential', 'Bash', 'pending', ?)",
            (_T1,),
        ),
        "decide": lambda c: _write(
            c,
            "UPDATE approvals SET state = 'approved', decided_at = ? WHERE id = 'apr_stamp'",
            (_T1,),
        ),
    },
    "chats": {
        "insert": lambda c: _write(
            c,
            "INSERT INTO chats (id, board_task_id, title, status, created_at, updated_at) "
            "VALUES ('cht_new', ?, 'second', 'active', ?, ?)",
            (_card(c, "second companion"), _T1, _T1),
        ),
        "retitle": lambda c: _write(
            c,
            "UPDATE chats SET title = 'renamed', updated_at = ? WHERE id = 'cht_stamp'",
            (_T1,),
        ),
    },
    "orchestrations": {
        "insert": lambda c: _write(
            c,
            "INSERT INTO orchestrations (id, status, created_at, updated_at) "
            "VALUES ('orch_new', 'completed', ?, ?)",
            (_T1, _T1),
        ),
        "advance": lambda c: _write(
            c,
            "UPDATE orchestrations SET stage = 'verify', updated_at = ? WHERE id = 'orch_stamp'",
            (_T1,),
        ),
    },
    "swarm_attempts": {
        "insert": lambda c: _write(
            c,
            "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, seq, provider, model, "
            "started_at) SELECT 'swa_new', 'swr_stamp', board_task_id, 1, 'claude', 'opus', ? "
            "FROM swarm_attempts WHERE id = 'swa_stamp'",
            (_T1,),
        ),
        # The blocked_reason projection reads exactly this transition.
        "close": lambda c: _write(
            c,
            "UPDATE swarm_attempts SET ended_at = ?, end_reason = 'timeout' WHERE id = 'swa_stamp'",
            (_T1,),
        ),
    },
    "task_sessions": {
        "insert": lambda c: _write(
            c,
            "INSERT INTO task_sessions (id, board_task_id, seq, harness, model, started_at) "
            "SELECT 'tks_new', board_task_id, 1, 'cli-claude', 'opus', ? FROM task_sessions "
            "WHERE id = 'tks_stamp'",
            (_T1,),
        ),
        "close": lambda c: _write(
            c,
            "UPDATE task_sessions SET ended_at = ?, end_reason = 'killed' WHERE id = 'tks_stamp'",
            (_T1,),
        ),
    },
    "task_categories": {
        "insert": lambda c: _write(
            c,
            "INSERT INTO task_categories (id, name, slug, created_at, updated_at) "
            "VALUES ('cat_new', 'Growth', 'growth', ?, ?)",
            (_T1, _T1),
        ),
        "rename": lambda c: _write(
            c,
            "UPDATE task_categories SET name = 'Operations', updated_at = ? WHERE id = 'cat_stamp'",
            (_T1,),
        ),
    },
    "projects": {
        "insert": lambda c: _write(
            c,
            "INSERT INTO projects (id, name, created_at, org_company_id) "
            "VALUES ('prj_new', 'Second', ?, 'co_b')",
            (_T1,),
        ),
        # THE blocker: a reclassification changes org.organization_context on
        # every card in the project and touches no timestamp anywhere.
        "reclassify": lambda c: _write(
            c, "UPDATE projects SET org_company_id = 'co_b' WHERE id = 'prj_stamp'"
        ),
    },
    "org_companies": {
        "insert": lambda c: _write(
            c,
            "INSERT INTO org_companies (id, slug, name, created_at) "
            "VALUES ('co_c', 'company-c', 'Company C', ?)",
            (_T1,),
        ),
        "rename": lambda c: _write(
            c, "UPDATE org_companies SET slug = 'renamed' WHERE id = 'co_a'"
        ),
    },
}

_CASES = [(source, name) for source, cases in _MUTATIONS.items() for name in cases]


def _etag(client: httpx.AsyncClient, **params: Any) -> str:
    response = _run(client.get("/api/board", params=params or None))
    assert response.status_code == 200, response.text
    tag = response.headers.get("ETag")
    assert tag, "board read must offer an ETag"
    return str(tag)


def _settled_etag(client: httpx.AsyncClient) -> str:
    """The tag once the read path has stopped writing to its own inputs.

    The first board read of a database claims the throttled sweeps (stale
    orchestrations, metacog) and persists reconciled columns, so the tag is
    compared only after two consecutive reads agree — otherwise every case
    below would "pass" on the reconciler's own noise.
    """
    _etag(client)
    settled = _etag(client)
    assert _etag(client) == settled, "a board read with no writes must not move its own tag"
    return settled


def test_every_stamped_source_has_a_coverage_case() -> None:
    """The stamp's sources and this file's cases are the same set, by name.

    This is the pin: adding a table to the reconciler means adding it to
    ``_BOARD_STAMP_SOURCES``, and adding it there without a case here fails
    HERE, naming the source that has no proof it moves the tag.
    """
    stamped = [label for label, _sql in _BOARD_STAMP_SOURCES]
    assert len(stamped) == len(set(stamped)), "a source is enumerated twice"
    assert set(stamped) == set(_MUTATIONS), (
        "stamped sources with no coverage case: "
        f"{sorted(set(stamped) - set(_MUTATIONS))}; "
        f"cases for unstamped sources: {sorted(set(_MUTATIONS) - set(stamped))}"
    )


@pytest.mark.parametrize(("source", "case"), _CASES, ids=[f"{s}:{c}" for s, c in _CASES])
def test_a_write_to_a_reconciler_input_moves_the_stamp(
    client: httpx.AsyncClient,
    collab: CollabStore,
    seeded: str,
    source: str,
    case: str,
) -> None:
    before = _settled_etag(client)
    _MUTATIONS[source][case](collab)
    after = _etag(client)
    assert after != before, f"{source}:{case} changed a reconciler input without moving the ETag"


def test_a_board_write_still_moves_the_stamp(
    client: httpx.AsyncClient, collab: CollabStore, seeded: str
) -> None:
    """The board's own rows are stamped separately (``board_change_stamp``).

    A new card, not an edit to an existing one: ``board_change_stamp`` is
    (count, MAX(updated_at)) and this project's timestamps are second-
    resolution, so an edit landing in the same second as the previous board
    write is invisible to it. That bound predates this lane and is documented
    on :func:`_board_etag`; asserting the count half here keeps this test about
    the board half being present at all.
    """
    before = _settled_etag(client)
    _card(collab, "a new card")
    assert _etag(client) != before


def test_the_conditional_read_still_serves_304_when_nothing_moved(
    client: httpx.AsyncClient, seeded: str
) -> None:
    """A complete stamp is only useful if it still collapses the idle case."""
    tag = _settled_etag(client)
    response = _run(client.get("/api/board", headers={"If-None-Match": tag}))
    assert response.status_code == 304
    assert response.content == b""


def test_the_no_parameter_feed_is_byte_identical_with_and_without_a_validator(
    client: httpx.AsyncClient, seeded: str
) -> None:
    """The stamp decorates the read; it never edits the feed.

    A request with no parameters returns the same bytes whether it carries no
    validator at all or a stale one — the conditional machinery only ever adds
    a header or withholds a body, and byte-parity here is what says so.
    """
    _settled_etag(client)
    plain = _run(client.get("/api/board"))
    stale = _run(client.get("/api/board", headers={"If-None-Match": 'W/"board-nope"'}))
    assert plain.status_code == 200
    assert stale.status_code == 200
    assert stale.content == plain.content
    assert {card["id"] for card in plain.json()} >= {seeded}
