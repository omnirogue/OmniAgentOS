from __future__ import annotations

from omniagentos.lab.contracts import Elo, MatchResult, Tournament
from omniagentos.lab.curator import queries
from omniagentos.lab.db import LabStore


def _store_with_two_configs() -> LabStore:
    store = LabStore(":memory:")
    store.create_tournament(
        Tournament(
            id="tnm_1", subject="s1", discipline="d1", arena_task_hash="h", config_ids=["a", "b"]
        )
    )
    store.record_match(
        MatchResult(
            id="mch_1",
            tournament_id="tnm_1",
            config_a="a",
            config_b="b",
            winner="b",
            judge_notes="b won on clarity",
        )
    )
    store.record_match(
        MatchResult(
            id="mch_2",
            tournament_id="tnm_1",
            config_a="a",
            config_b="b",
            winner="b",
            judge_notes="b won again",
        )
    )
    store.upsert_elo(Elo(subject="s1", config_id="a", rating=990))
    store.upsert_elo(Elo(subject="s1", config_id="b", rating=1020))
    return store


def test_distinct_subjects_reads_tournaments() -> None:
    store = _store_with_two_configs()
    assert queries.distinct_subjects(store) == [{"subject": "s1", "discipline": "d1"}]


def test_distinct_subjects_also_reads_an_existing_leaderboard_with_no_tournament_row() -> None:
    from omniagentos.lab.contracts import LeaderboardEntry

    store = LabStore(":memory:")
    store.upsert_leaderboard_entry(
        LeaderboardEntry(
            subject="orphan", discipline="d2", rank=1, config_id="x", elo=1000, summary="x"
        )
    )
    assert queries.distinct_subjects(store) == [{"subject": "orphan", "discipline": "d2"}]


def test_distinct_subjects_is_empty_for_a_fresh_store() -> None:
    assert queries.distinct_subjects(LabStore(":memory:")) == []


def test_top_elo_orders_by_rating_desc() -> None:
    store = _store_with_two_configs()
    rows = queries.top_elo(store, "s1", 10)
    assert [row["config_id"] for row in rows] == ["b", "a"]
    assert rows[0]["rating"] == 1020.0


def test_top_elo_respects_limit() -> None:
    store = _store_with_two_configs()
    rows = queries.top_elo(store, "s1", 1)
    assert len(rows) == 1
    assert rows[0]["config_id"] == "b"


def test_top_elo_breaks_ties_on_config_id() -> None:
    store = LabStore(":memory:")
    store.upsert_elo(Elo(subject="s1", config_id="z", rating=1000))
    store.upsert_elo(Elo(subject="s1", config_id="a", rating=1000))
    rows = queries.top_elo(store, "s1", 10)
    assert [row["config_id"] for row in rows] == ["a", "z"]


def test_recent_judge_notes_returns_most_recent_distinct_notes_first() -> None:
    store = _store_with_two_configs()
    notes = queries.recent_judge_notes(store, "s1", "b")
    assert notes == ["b won again", "b won on clarity"]


def test_recent_judge_notes_respects_limit() -> None:
    store = _store_with_two_configs()
    notes = queries.recent_judge_notes(store, "s1", "b", limit=1)
    assert notes == ["b won again"]


def test_recent_judge_notes_covers_both_participants_not_just_the_winner() -> None:
    # Judge notes explain the MATCH, not just the winning side -- a log-book
    # digest for the loser should show the same notes (e.g. why it lost).
    store = _store_with_two_configs()
    assert queries.recent_judge_notes(store, "s1", "a") == ["b won again", "b won on clarity"]


def test_recent_judge_notes_excludes_matches_with_no_notes() -> None:
    store = LabStore(":memory:")
    store.create_tournament(
        Tournament(
            id="tnm_1", subject="s1", discipline="d1", arena_task_hash="h", config_ids=["a", "b"]
        )
    )
    store.record_match(
        MatchResult(id="mch_1", tournament_id="tnm_1", config_a="a", config_b="b", winner="a")
    )
    assert queries.recent_judge_notes(store, "s1", "a") == []


def test_recent_judge_notes_empty_for_unknown_subject() -> None:
    store = _store_with_two_configs()
    assert queries.recent_judge_notes(store, "unknown", "b") == []


def test_recent_tournaments_orders_newest_first_and_respects_limit() -> None:
    store = LabStore(":memory:")
    store.create_tournament(
        Tournament(id="tnm_1", subject="s1", discipline="d1", arena_task_hash="h1", config_ids=[])
    )
    store.create_tournament(
        Tournament(id="tnm_2", subject="s1", discipline="d1", arena_task_hash="h2", config_ids=[])
    )
    rows = queries.recent_tournaments(store, "s1", limit=1)
    assert len(rows) == 1
    assert rows[0]["id"] == "tnm_2"
