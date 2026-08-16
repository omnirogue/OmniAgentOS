"""SQLite persistence for the H2 self-improvement lab."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from functools import wraps
from pathlib import Path
from typing import Any, cast

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.lab.contracts import (
    CandidateEvalCase,
    ChampionEntry,
    Elo,
    EvalCase,
    EvalResult,
    EvalSplit,
    EvalSuite,
    Experiment,
    JudgeRecord,
    LeaderboardEntry,
    MatchResult,
    PlaybookEntry,
    Surface,
    Tournament,
    surface_requires_human,
)

_EXPERIMENT_COLUMNS = frozenset(
    {
        "hypothesis",
        "discipline",
        "explore_policy",
        "mutable_surface_kind",
        "champion_surface_id",
        "challenger_surface_id",
        "eval_suite_id",
        "dataset_hash",
        "primary_metric",
        "budgets_json",
        "promotion_json",
        "status",
        "disposition",
        "scorecard_json",
        "snapshot_hash",
        "created_at",
        "updated_at",
    }
)
_TOURNAMENT_COLUMNS = frozenset(
    {
        "subject",
        "discipline",
        "arena_task_hash",
        "config_ids_json",
        "winner_config_id",
        "status",
        "created_at",
    }
)


class ChampionCASMismatch(RuntimeError):
    """Raised when a champion compare-and-swap uses a stale version."""


class LabJobLeaseLost(RuntimeError):
    """A fenced write lost its lease (the job was adopted)."""


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Serialize access through the composed H1 store's connection lock."""

    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        store = cast("LabStore", args[0])
        with store._store._lock:
            return method(*args, **kwargs)

    return wrapped


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _checked_update(fields: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    unknown = fields.keys() - allowed
    if unknown:
        raise ValueError(f"unknown columns: {', '.join(sorted(unknown))}")
    return fields


class LabStore:
    """Auto-migrated lab store sharing the H1 SQLite database and connection discipline."""

    def __init__(self, db_path: str) -> None:
        self.db_path = (
            db_path if db_path == ":memory:" else str(Path(db_path).expanduser().resolve())
        )
        self._store = SqliteStore(self.db_path)

    @property
    def _connection(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved live from the composed store.

        Never cache this on the instance. ``SqliteStore`` hands out one
        connection per thread and opens them with ``check_same_thread=False``,
        so a handle captured at construction time would bind this DAL to
        whichever thread built it and silently interleave its statements into
        another thread's transaction rather than raising.
        """
        return self._store._connection

    # --- surfaces + champions ---

    @_serialized
    def create_surface(self, s: Surface) -> None:
        safety_relevant = surface_requires_human(s.kind, s.safety_relevant)
        self._store._write(
            "INSERT INTO surfaces "
            "(id, kind, discipline, version, path, content_hash, parent_version, status, label, "
            "safety_relevant, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s.id,
                s.kind,
                s.discipline,
                s.version,
                s.path,
                s.content_hash,
                s.parent_version,
                s.status,
                s.label,
                int(safety_relevant),
                s.created_at,
            ),
        )

    @_serialized
    def get_surface(self, surface_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute(
                "SELECT * FROM surfaces WHERE id = ?", (surface_id,)
            ).fetchone()
        )

    @_serialized
    def list_surfaces(
        self,
        discipline: str | None = None,
        kind: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if discipline is not None:
            clauses.append("discipline = ?")
            parameters.append(discipline)
        if kind is not None:
            clauses.append("kind = ?")
            parameters.append(kind)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            parameters.append(limit)
        rows = self._connection.execute(
            f"SELECT * FROM surfaces{where} ORDER BY created_at DESC, id DESC{limit_sql}",
            parameters,
        ).fetchall()
        return _rows(rows)

    @_serialized
    def set_surface_status(self, surface_id: str, status: str) -> None:
        self._store._write("UPDATE surfaces SET status = ? WHERE id = ?", (status, surface_id))

    @_serialized
    def get_champion(self, discipline: str, kind: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute(
                "SELECT * FROM champions WHERE discipline = ? AND surface_kind = ?",
                (discipline, kind),
            ).fetchone()
        )

    @_serialized
    def set_champion(self, entry: ChampionEntry) -> None:
        """Atomically compare-and-swap the pointer and append its history event."""
        self._store._begin()
        try:
            self._set_champion_in_transaction(entry)
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise

    @_serialized
    def set_champion_with_statuses(
        self, entry: ChampionEntry, *, surface_statuses: dict[str, str]
    ) -> None:
        """CAS the pointer and update surface lifecycle states in one transaction."""
        self._store._begin()
        try:
            self._set_champion_in_transaction(entry)
            for surface_id, status in surface_statuses.items():
                cursor = self._connection.execute(
                    "UPDATE surfaces SET status = ? WHERE id = ?",
                    (status, surface_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"unknown surface in champion swap: {surface_id}")
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise

    def _set_champion_in_transaction(self, entry: ChampionEntry) -> None:
        current = self._connection.execute(
            "SELECT * FROM champions WHERE discipline = ? AND surface_kind = ?",
            (entry.discipline, entry.surface_kind),
        ).fetchone()
        current_version = 0 if current is None else int(current["cas_version"])
        if entry.cas_version != current_version:
            raise ChampionCASMismatch(
                f"stale champion CAS for {entry.discipline}/{entry.surface_kind}: "
                f"expected {entry.cas_version}, current {current_version}"
            )

        next_version = current_version + 1
        if current is None:
            self._connection.execute(
                "INSERT INTO champions "
                "(discipline, surface_kind, surface_id, surface_version, "
                "promoted_from_experiment, rollback_to_surface_id, cas_version, promoted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.discipline,
                    entry.surface_kind,
                    entry.surface_id,
                    entry.surface_version,
                    entry.promoted_from_experiment,
                    entry.rollback_to_surface_id,
                    next_version,
                    entry.promoted_at,
                ),
            )
            event = "promoted"
        else:
            cursor = self._connection.execute(
                "UPDATE champions SET surface_id = ?, surface_version = ?, "
                "promoted_from_experiment = ?, rollback_to_surface_id = ?, "
                "cas_version = ?, promoted_at = ? "
                "WHERE discipline = ? AND surface_kind = ? AND cas_version = ?",
                (
                    entry.surface_id,
                    entry.surface_version,
                    entry.promoted_from_experiment,
                    entry.rollback_to_surface_id,
                    next_version,
                    entry.promoted_at,
                    entry.discipline,
                    entry.surface_kind,
                    current_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ChampionCASMismatch(
                    f"champion CAS lost for {entry.discipline}/{entry.surface_kind}"
                )
            event = (
                "rolled_back"
                if entry.surface_id == current["rollback_to_surface_id"]
                else "promoted"
            )
        self._connection.execute(
            "INSERT INTO champion_history "
            "(discipline, surface_kind, surface_id, event, experiment, at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                entry.discipline,
                entry.surface_kind,
                entry.surface_id,
                event,
                entry.promoted_from_experiment,
                entry.promoted_at,
            ),
        )

    @_serialized
    def champion_history(self, discipline: str, kind: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM champion_history WHERE discipline = ? AND surface_kind = ? "
                "ORDER BY id ASC",
                (discipline, kind),
            ).fetchall()
        )

    @_serialized
    def list_champions(
        self, discipline: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        limit_sql = "" if limit is None else " LIMIT ?"
        if discipline is None:
            rows = self._connection.execute(
                "SELECT * FROM champions "
                f"ORDER BY promoted_at DESC, discipline ASC, surface_kind ASC{limit_sql}",
                () if limit is None else (limit,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM champions WHERE discipline = ? "
                f"ORDER BY promoted_at DESC, surface_kind ASC{limit_sql}",
                (discipline,) if limit is None else (discipline, limit),
            ).fetchall()
        return _rows(rows)

    @_serialized
    def get_agent_lineage(self, identity: str) -> str | None:
        row = self._connection.execute(
            "SELECT lineage FROM agents WHERE id = ? OR name = ? LIMIT 1",
            (identity, identity),
        ).fetchone()
        return None if row is None else str(row["lineage"])

    @_serialized
    def discipline_summaries(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the lab's discipline roll-up without exposing implementation tables."""
        rows = self._connection.execute(
            "WITH disciplines AS ("
            " SELECT discipline FROM surfaces UNION SELECT discipline FROM experiments"
            " UNION SELECT discipline FROM tournaments UNION SELECT discipline FROM champions"
            ") "
            "SELECT d.discipline, "
            "(SELECT COUNT(*) FROM experiments e WHERE e.discipline = d.discipline) "
            "AS experiment_count, "
            "(SELECT MAX(er.rating) FROM elo_ratings er JOIN surfaces s ON s.id = er.config_id "
            " WHERE s.discipline = d.discipline) AS top_elo, "
            "(SELECT MAX(e.created_at) FROM experiments e WHERE e.discipline = d.discipline) "
            "AS newest_at FROM disciplines d "
            "ORDER BY newest_at DESC, d.discipline DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return _rows(rows)

    # --- experiments + eval ---

    @_serialized
    def create_experiment(self, e: Experiment) -> None:
        data = e.model_dump(mode="json")
        self._store._write(
            "INSERT INTO experiments "
            "(id, hypothesis, discipline, explore_policy, mutable_surface_kind, "
            "champion_surface_id, challenger_surface_id, eval_suite_id, dataset_hash, "
            "primary_metric, budgets_json, promotion_json, status, disposition, "
            "scorecard_json, snapshot_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                e.id,
                e.hypothesis,
                e.discipline,
                e.explore_policy,
                e.mutable_surface_kind,
                e.champion_surface_id,
                e.challenger_surface_id,
                e.eval_suite_id,
                e.dataset_hash,
                e.primary_metric,
                _json(data["budgets"]),
                _json(data["promotion_threshold"]),
                e.status,
                e.disposition,
                None if data["scorecard"] is None else _json(data["scorecard"]),
                e.snapshot_hash,
                e.created_at,
                e.updated_at,
            ),
        )

    @_serialized
    def update_experiment(self, exp_id: str, fields: dict[str, Any]) -> None:
        values = dict(fields)
        aliases = {
            "budgets": "budgets_json",
            "promotion_threshold": "promotion_json",
            "scorecard": "scorecard_json",
        }
        for source, target in aliases.items():
            if source in values:
                if target in values:
                    raise ValueError(f"both {source} and {target} were provided")
                value = values.pop(source)
                if hasattr(value, "model_dump"):
                    value = value.model_dump(mode="json")
                values[target] = None if value is None else _json(value)
        _checked_update(values, _EXPERIMENT_COLUMNS)
        if not values:
            return
        assignments = ", ".join(f"{column} = ?" for column in values)
        self._store._write(
            f"UPDATE experiments SET {assignments} WHERE id = ?",
            (*values.values(), exp_id),
        )

    @_serialized
    def get_experiment(self, exp_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute("SELECT * FROM experiments WHERE id = ?", (exp_id,)).fetchone()
        )

    @_serialized
    def list_experiments(
        self,
        discipline: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if discipline is not None:
            clauses.append("discipline = ?")
            parameters.append(discipline)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        parameters.append(limit)
        return _rows(
            self._connection.execute(
                f"SELECT * FROM experiments{where} ORDER BY created_at DESC, id DESC LIMIT ?",
                parameters,
            ).fetchall()
        )

    @_serialized
    def create_eval_suite(self, s: EvalSuite) -> None:
        metrics = [metric.model_dump(mode="json") for metric in s.metrics]
        self._store._write(
            "INSERT INTO eval_suites "
            "(id, discipline, version, metrics_json, protected, dataset_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                s.id,
                s.discipline,
                s.version,
                _json(metrics),
                int(s.protected),
                s.dataset_hash,
                s.created_at,
            ),
        )

    @_serialized
    def list_eval_suites(self, discipline: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM eval_suites WHERE discipline = ? ORDER BY created_at DESC, id DESC",
                (discipline,),
            ).fetchall()
        )

    @_serialized
    def add_eval_case(self, c: EvalCase) -> None:
        expected = None if c.split == "held_out" or c.expected is None else _json(c.expected)
        self._store._write(
            "INSERT INTO eval_cases "
            "(id, suite_id, split, input_json, expected_json, rubric, weight) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (c.id, c.suite, c.split, _json(c.input), expected, c.rubric, c.weight),
        )

    @_serialized
    def get_eval_suite(self, suite_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute(
                "SELECT * FROM eval_suites WHERE id = ?", (suite_id,)
            ).fetchone()
        )

    @_serialized
    def record_eval_result(self, r: EvalResult) -> None:
        self._store._write(
            "INSERT INTO eval_results "
            "(id, experiment_id, arm, suite_id, suite_version, split, metrics_json, "
            "per_case_json, replicate, deterministic_passed, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r.id,
                r.experiment_id,
                r.arm,
                r.suite_id,
                r.suite_version,
                r.split,
                _json(r.metrics),
                _json(r.per_case),
                r.replicate,
                int(r.deterministic_passed),
                r.created_at,
            ),
        )

    @_serialized
    def eval_results(self, exp_id: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM eval_results WHERE experiment_id = ? "
                "ORDER BY created_at ASC, id ASC",
                (exp_id,),
            ).fetchall()
        )

    @_serialized
    def record_judge(self, j: JudgeRecord) -> None:
        self._store._write(
            "INSERT INTO judge_records "
            "(id, experiment_id, tournament_id, case_id, arm, judge_lineage, blind_token, "
            "score, notes, rubric_dimension, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                j.id,
                j.experiment_id,
                j.tournament_id,
                j.case_id,
                j.arm,
                j.judge_lineage,
                j.blind_token,
                j.score,
                j.notes,
                j.rubric_dimension,
                utc_now_iso(),
            ),
        )

    @_serialized
    def judge_records(self, experiment_id: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM judge_records WHERE experiment_id = ? "
                "ORDER BY created_at ASC, id ASC",
                (experiment_id,),
            ).fetchall()
        )

    # --- tournament + elo + playbook + leaderboard ---

    @_serialized
    def create_tournament(self, t: Tournament) -> None:
        self._store._write(
            "INSERT INTO tournaments "
            "(id, subject, discipline, arena_task_hash, config_ids_json, winner_config_id, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                t.id,
                t.subject,
                t.discipline,
                t.arena_task_hash,
                _json(t.config_ids),
                t.winner_config_id,
                t.status,
                t.created_at,
            ),
        )

    @_serialized
    def update_tournament(self, tid: str, fields: dict[str, Any]) -> None:
        values = dict(fields)
        if "config_ids" in values:
            if "config_ids_json" in values:
                raise ValueError("both config_ids and config_ids_json were provided")
            values["config_ids_json"] = _json(values.pop("config_ids"))
        _checked_update(values, _TOURNAMENT_COLUMNS)
        if not values:
            return
        assignments = ", ".join(f"{column} = ?" for column in values)
        self._store._write(
            f"UPDATE tournaments SET {assignments} WHERE id = ?", (*values.values(), tid)
        )

    @_serialized
    def get_tournament(self, tournament_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute(
                "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
            ).fetchone()
        )

    @_serialized
    def list_tournaments(
        self,
        subject: str | None = None,
        discipline: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if subject is not None:
            clauses.append("subject = ?")
            parameters.append(subject)
        if discipline is not None:
            clauses.append("discipline = ?")
            parameters.append(discipline)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        parameters.append(limit)
        return _rows(
            self._connection.execute(
                f"SELECT * FROM tournaments{where} ORDER BY created_at DESC, id DESC LIMIT ?",
                parameters,
            ).fetchall()
        )

    @_serialized
    def record_match(self, m: MatchResult) -> None:
        self._store._write(
            "INSERT INTO matches "
            "(id, tournament_id, config_a, config_b, winner, score_a, score_b, judge_notes, "
            "blind, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                m.id,
                m.tournament_id,
                m.config_a,
                m.config_b,
                m.winner,
                m.score_a,
                m.score_b,
                m.judge_notes,
                int(m.blind),
                utc_now_iso(),
            ),
        )

    @_serialized
    def tournament_matches(self, tournament_id: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM matches WHERE tournament_id = ? ORDER BY created_at ASC, id ASC",
                (tournament_id,),
            ).fetchall()
        )

    @_serialized
    def get_elo(self, subject: str, config_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute(
                "SELECT * FROM elo_ratings WHERE subject = ? AND config_id = ?",
                (subject, config_id),
            ).fetchone()
        )

    @_serialized
    def upsert_elo(self, e: Elo) -> None:
        self._store._write(
            "INSERT INTO elo_ratings "
            "(subject, config_id, rating, matches, wins, losses, draws, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(subject, config_id) DO UPDATE SET "
            "rating = excluded.rating, matches = excluded.matches, wins = excluded.wins, "
            "losses = excluded.losses, draws = excluded.draws, updated_at = excluded.updated_at",
            (
                e.subject,
                e.config_id,
                e.rating,
                e.matches,
                e.wins,
                e.losses,
                e.draws,
                e.updated_at,
            ),
        )

    @_serialized
    def elo_for_subject(self, subject: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM elo_ratings WHERE subject = ? ORDER BY rating DESC, config_id ASC",
                (subject,),
            ).fetchall()
        )

    @_serialized
    def leaderboard(
        self, subject: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        limit_sql = "" if limit is None else " LIMIT ?"
        if subject is None:
            rows = self._connection.execute(
                f"SELECT * FROM leaderboard ORDER BY subject ASC, rank ASC{limit_sql}",
                () if limit is None else (limit,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                f"SELECT * FROM leaderboard WHERE subject = ? ORDER BY rank ASC{limit_sql}",
                (subject,) if limit is None else (subject, limit),
            ).fetchall()
        return _rows(rows)

    @_serialized
    def upsert_leaderboard_entry(self, row: LeaderboardEntry) -> None:
        self._store._write(
            "INSERT INTO leaderboard "
            "(subject, discipline, rank, config_id, elo, summary, judge_notes, "
            "source_experiments_json, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(subject, rank) DO UPDATE SET discipline = excluded.discipline, "
            "config_id = excluded.config_id, elo = excluded.elo, summary = excluded.summary, "
            "judge_notes = excluded.judge_notes, "
            "source_experiments_json = excluded.source_experiments_json, "
            "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (
                row.subject,
                row.discipline,
                row.rank,
                row.config_id,
                row.elo,
                row.summary,
                row.judge_notes,
                _json(row.source_experiments),
                row.updated_by,
                row.updated_at,
            ),
        )

    @_serialized
    def replace_leaderboard(self, subject: str, rows: list[LeaderboardEntry]) -> None:
        """Replace one subject's ranking atomically, removing stale tail rows."""
        if any(row.subject != subject for row in rows):
            raise ValueError("all leaderboard rows must match the replaced subject")
        self._store._begin()
        try:
            self._connection.execute("DELETE FROM leaderboard WHERE subject = ?", (subject,))
            for row in rows:
                self._connection.execute(
                    "INSERT INTO leaderboard "
                    "(subject, discipline, rank, config_id, elo, summary, judge_notes, "
                    "source_experiments_json, updated_by, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row.subject,
                        row.discipline,
                        row.rank,
                        row.config_id,
                        row.elo,
                        row.summary,
                        row.judge_notes,
                        _json(row.source_experiments),
                        row.updated_by,
                        row.updated_at,
                    ),
                )
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise

    @_serialized
    def add_playbook_entry(self, p: PlaybookEntry) -> None:
        self._store._write(
            "INSERT INTO playbook "
            "(id, trait, discipline, evidence_experiments_json, evidence_tournaments_json, "
            "scope, confidence, elo_support, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p.id,
                p.trait,
                p.discipline,
                _json(p.evidence_experiments),
                _json(p.evidence_tournaments),
                p.scope,
                p.confidence,
                p.elo_support,
                p.status,
                p.created_at,
            ),
        )

    @_serialized
    def list_playbook(
        self, discipline: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        limit_sql = "" if limit is None else " LIMIT ?"
        if discipline is None:
            rows = self._connection.execute(
                f"SELECT * FROM playbook ORDER BY created_at DESC, id DESC{limit_sql}",
                () if limit is None else (limit,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                f"SELECT * FROM playbook WHERE discipline = ? "
                f"ORDER BY created_at DESC, id DESC{limit_sql}",
                (discipline,) if limit is None else (discipline, limit),
            ).fetchall()
        return _rows(rows)

    @_serialized
    def candidate_cases(self, suite_id: str, split: str) -> list[CandidateEvalCase]:
        """Return candidate-safe case models with no expected-value field."""
        rows = self._connection.execute(
            "SELECT id, split, input_json, rubric FROM eval_cases "
            "WHERE suite_id = ? AND split = ? ORDER BY id ASC",
            (suite_id, split),
        ).fetchall()
        return [
            CandidateEvalCase(
                id=str(row["id"]),
                split=EvalSplit(str(row["split"])),
                input=json.loads(str(row["input_json"])),
                rubric=str(row["rubric"]),
            )
            for row in rows
        ]

    @_serialized
    def enqueue_job(
        self,
        *,
        job_id: str,
        idempotency_key: str,
        experiment_id: str,
        dry_run: bool,
        now: str,
    ) -> tuple[dict[str, Any], bool]:
        rowcount = self._store._write_count(
            "INSERT OR IGNORE INTO lab_jobs "
            "(job_id, idempotency_key, experiment_id, state, dry_run, created_at, updated_at) "
            "VALUES (?, ?, ?, 'queued', ?, ?, ?)",
            (job_id, idempotency_key, experiment_id, int(dry_run), now, now),
        )
        created = rowcount == 1
        row = self._connection.execute(
            "SELECT * FROM lab_jobs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        assert row is not None
        row_dict = _row(row)
        assert row_dict is not None
        return row_dict, created

    @_serialized
    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM lab_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        return _row(row)

    @_serialized
    def get_job_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM lab_jobs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return _row(row)

    @_serialized
    def list_jobs(
        self,
        experiment_id: str | None = None,
        state: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if experiment_id is not None:
            clauses.append("experiment_id = ?")
            parameters.append(experiment_id)
        if state is not None:
            clauses.append("state = ?")
            parameters.append(state)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            parameters.append(limit)
        rows = self._connection.execute(
            f"SELECT * FROM lab_jobs{where} ORDER BY created_at DESC, job_id DESC{limit_sql}",
            parameters,
        ).fetchall()
        return _rows(rows)

    @_serialized
    def claim_job(
        self, *, owner: str, now: str, lease_expires_at: str
    ) -> dict[str, Any] | None:
        self._store._begin()
        try:
            row = self._connection.execute(
                "SELECT job_id, lease_generation FROM lab_jobs "
                "WHERE (state = 'queued' AND cancel_requested = 0) "
                "OR (state = 'running' AND (lease_expires_at IS NULL OR lease_expires_at <= ?)) "
                "ORDER BY created_at ASC, job_id ASC LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                self._store._rollback()
                return None
            job_id = row["job_id"]
            lease_generation = row["lease_generation"]

            cursor = self._connection.execute(
                "UPDATE lab_jobs SET state='running', lease_owner=?, lease_expires_at=?, "
                "lease_generation = lease_generation + 1, attempt = attempt + 1, updated_at=? "
                "WHERE job_id = ? AND lease_generation = ?",
                (owner, lease_expires_at, now, job_id, lease_generation),
            )
            if cursor.rowcount == 0:
                self._store._rollback()
                return None

            updated_row = self._connection.execute(
                "SELECT * FROM lab_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            self._store._commit()
            return _row(updated_row)
        except BaseException:
            self._store._rollback()
            raise

    @_serialized
    def heartbeat_job(
        self,
        *,
        job_id: str,
        owner: str,
        lease_generation: int,
        lease_expires_at: str,
        now: str,
    ) -> dict[str, Any]:
        rowcount = self._store._write_count(
            "UPDATE lab_jobs SET lease_expires_at = ?, updated_at = ? "
            "WHERE job_id = ? AND lease_owner = ? AND lease_generation = ? AND state = 'running'",
            (lease_expires_at, now, job_id, owner, lease_generation),
        )
        if rowcount != 1:
            raise LabJobLeaseLost(
                f"Heartbeat failed for job {job_id} under generation {lease_generation}: lease lost."
            )
        row = self._connection.execute(
            "SELECT * FROM lab_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        assert row is not None
        row_dict = _row(row)
        assert row_dict is not None
        return row_dict

    @_serialized
    def checkpoint_job(
        self,
        *,
        job_id: str,
        owner: str,
        lease_generation: int,
        checkpoint: dict[str, Any],
        lease_expires_at: str,
        now: str,
    ) -> dict[str, Any]:
        rowcount = self._store._write_count(
            "UPDATE lab_jobs SET checkpoint_json = ?, lease_expires_at = ?, updated_at = ? "
            "WHERE job_id = ? AND lease_owner = ? AND lease_generation = ? AND state = 'running'",
            (_json(checkpoint), lease_expires_at, now, job_id, owner, lease_generation),
        )
        if rowcount != 1:
            raise LabJobLeaseLost(
                f"Checkpoint failed for job {job_id} under generation {lease_generation}: lease lost."
            )
        row = self._connection.execute(
            "SELECT * FROM lab_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        assert row is not None
        row_dict = _row(row)
        assert row_dict is not None
        return row_dict

    @_serialized
    def request_job_cancel(self, *, job_id: str, now: str) -> dict[str, Any] | None:
        self._store._begin()
        try:
            row = self._connection.execute(
                "SELECT * FROM lab_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                self._store._rollback()
                return None

            state = row["state"]
            if state in ("succeeded", "failed", "cancelled"):
                self._store._rollback()
                return _row(row)

            if state == "queued":
                self._connection.execute(
                    "UPDATE lab_jobs SET state = 'cancelled', cancel_requested = 1, "
                    "finished_at = ?, error = 'cancelled before start', "
                    "lease_owner = NULL, lease_expires_at = NULL, updated_at = ? "
                    "WHERE job_id = ?",
                    (now, now, job_id),
                )
            elif state == "running":
                self._connection.execute(
                    "UPDATE lab_jobs SET cancel_requested = 1, updated_at = ? "
                    "WHERE job_id = ?",
                    (now, job_id),
                )

            updated_row = self._connection.execute(
                "SELECT * FROM lab_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            self._store._commit()
            return _row(updated_row)
        except BaseException:
            self._store._rollback()
            raise

    @_serialized
    def finish_job(
        self,
        *,
        job_id: str,
        owner: str,
        lease_generation: int,
        state: str,
        result: dict[str, Any] | None,
        error: str | None,
        now: str,
    ) -> dict[str, Any]:
        if state not in ("succeeded", "failed", "cancelled"):
            raise ValueError(f"Invalid terminal state for job finish: {state}")

        result_json = _json(result) if result is not None else None
        rowcount = self._store._write_count(
            "UPDATE lab_jobs SET state = ?, result_json = ?, error = ?, "
            "finished_at = ?, updated_at = ?, lease_owner = NULL, lease_expires_at = NULL "
            "WHERE job_id = ? AND lease_owner = ? AND lease_generation = ? AND state = 'running'",
            (state, result_json, error, now, now, job_id, owner, lease_generation),
        )
        if rowcount != 1:
            raise LabJobLeaseLost(
                f"Finish failed for job {job_id} under generation {lease_generation}: lease lost."
            )
        row = self._connection.execute(
            "SELECT * FROM lab_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        assert row is not None
        row_dict = _row(row)
        assert row_dict is not None
        return row_dict

    @_serialized
    def expired_jobs(
        self, *, now: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        limit_sql = "" if limit is None else " LIMIT ?"
        parameters: list[Any] = [now]
        if limit is not None:
            parameters.append(limit)
        rows = self._connection.execute(
            "SELECT * FROM lab_jobs WHERE state = 'running' AND lease_expires_at IS NOT NULL "
            f"AND lease_expires_at <= ? ORDER BY created_at ASC, job_id ASC{limit_sql}",
            parameters,
        ).fetchall()
        return _rows(rows)
