"""Ratchets for the memlife SQLite / filesystem split-brain defects.

Historical defects (now fixed where this lane owns them):
  - the review queue reported pending=0 while the production tick staged
    filesystem candidates (F4 / Lane B dual-write)
  - nothing INSERTed into memlife_candidates (Lane B ``db.stage_candidate``)
  - render_lessons had no production caller (Lane C — flipped)
  - recall_bridge had no production caller (superseded by
    ``recall._search_memlife``; AT-18 TEST_ONLY_CALLER)

Lane B re-points the first ratchet at the production path (tick), because
staging through ``Lifecycle.stage`` alone must not write SQLite (spec B2:
guard dual-write in ``dream.py``, not in ``lifecycle.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.memlife.contracts import EpisodicEvent, EventResult
from omniagentos.memlife.dream import ensure_dream_cycle_routine
from omniagentos.memlife.store import MemlifeStore
from omniagentos.policy import load_policy
from omniagentos.scheduler.routines_tick import tick

DUE_NOW = datetime(2026, 7, 29, 3, 0, tzinfo=UTC)
ENV_STORE = "OMNIAGENTOS_MEMLIFE_STORE"
EVENT_TS = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _valid_event_line(event_id: str, *, reflection: str) -> str:
    event = EpisodicEvent(
        id=event_id,
        ts=EVENT_TS,
        skill="swarm.coder",
        action="attempt",
        result=EventResult.FAILURE,
        pain=8.0,
        importance=9.0,
        reflection=reflection,
    )
    return event.model_dump_json()


def test_staged_candidates_are_visible_to_the_review_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production tick dual-writes: FS pending and SQLite pending agree.

    Was xfail(strict=True) documenting pending=0 while the store held
    candidates. Flipped when Lane B wired stage_candidate through the tick.
    """
    db_path = tmp_path / "runtime.db"
    database = SqliteStore(str(db_path))
    root = tmp_path / "memlife"
    MemlifeStore(root).ensure_layout()
    monkeypatch.setenv(ENV_STORE, str(root))
    import omniagentos.api.routes.memlife as memlife_routes

    if hasattr(memlife_routes, "_clear_store_root_cache"):
        memlife_routes._clear_store_root_cache()

    events_path = root / "episodic" / "events.jsonl"
    events_path.write_text(
        "\n".join(
            [
                _valid_event_line(
                    "ev_a",
                    reflection="Agents cannot commit inside a sandboxed worktree",
                ),
                _valid_event_line(
                    "ev_b",
                    reflection="Always pin exact dependency versions in lockfiles",
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    ensure_dream_cycle_routine(database)
    result = tick(database, load_policy(), now=DUE_NOW)
    assert len(result["fired"]) == 1, result

    fs_pending = MemlifeStore(root).list_pending()
    assert len(fs_pending) == 2, "filesystem store really has 2 pending"

    with database._lock:
        row = database._connection.execute(
            "SELECT COUNT(*) AS n FROM memlife_candidates "
            "WHERE status IN ('staged','reopened')"
        ).fetchone()

    assert int(row["n"]) == 2, (
        f"GET /api/memlife/queue would report pending={row['n']} while the store "
        "the dream cycle writes holds 2 pending candidates"
    )


def _production_insert_writers(table_fragment: str) -> list[Path]:
    """Return omniagentos/*.py files whose source contains an INSERT for *table*."""
    src = Path("omniagentos")
    writers: list[Path] = []
    for p in src.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        if table_fragment in text:
            writers.append(p)
    return writers


def test_something_writes_memlife_candidate_rows() -> None:
    """Lane B landed: omniagentos/memlife/db.py is the sole INSERT writer.

    Was xfail(strict=True) documenting the empty-writer defect; flipped to a
    normal pass when Lane B's stage_candidate XPASS'd the ratchet.
    F4 single-writer: no other production module may emit the INSERT.
    """
    writers = _production_insert_writers("INSERT INTO memlife_candidates")
    assert writers, "no module INSERTs into memlife_candidates"
    rel = sorted(p.as_posix() for p in writers)
    assert rel == ["omniagentos/memlife/db.py"], rel


def test_something_writes_memlife_decision_rows() -> None:
    """F4: memlife_decisions INSERTs also live only in memlife/db.py.

    Routes call ``append_decision``; they must not embed the SQL themselves.
    """
    writers = _production_insert_writers("INSERT INTO memlife_decisions")
    assert writers, "no module INSERTs into memlife_decisions"
    rel = sorted(p.as_posix() for p in writers)
    assert rel == ["omniagentos/memlife/db.py"], rel


@pytest.mark.parametrize(
    "symbol",
    [
        "omniagentos.memlife.render:render_lessons",
        # default_knowledge_recaller superseded — AT-18 TEST_ONLY_CALLER.
        # Production read end is the recall front-door leg:
        "omniagentos.retrieval.recall:_search_memlife",
    ],
)
def test_memlife_recall_chain_has_a_production_caller(symbol: str) -> None:
    """L5 claims graduated lessons reach recall. Both ends must be wired.

    Lane C flipped this strict xfail once production callers landed:
    graduate_candidate → render_lessons, and BackendSpec(search=_search_memlife).
    """
    module, name = symbol.split(":")
    defining = Path(module.replace(".", "/") + ".py")
    callers = []
    for p in Path("omniagentos").rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        # Real call site `name(` (not a docstring cross-reference), OR a
        # DI-seam registration such as BackendSpec(search=_search_memlife) —
        # that reaches production as a value, not via name(.
        if p == defining:
            if f"search={name}" in text or f"id_fn={name}" in text:
                callers.append(str(p))
            continue
        if f"{name}(" in text:
            callers.append(str(p))
    assert callers, f"{symbol} is never called in production code"
