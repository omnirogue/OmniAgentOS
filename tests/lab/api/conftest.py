"""ASGI fixtures and an in-memory LabStore test double."""

from __future__ import annotations

import copy
import json
import sys
from collections.abc import Generator
from types import ModuleType
from typing import Any

import httpx
import pytest

from omniagentos.api.main import app
from omniagentos.contracts import utc_now_iso
from omniagentos.lab.api.deps import get_lab_store, get_protected_evaluator
from omniagentos.lab.contracts import (
    Budgets,
    ChampionEntry,
    Disposition,
    Elo,
    EvalCase,
    EvalResult,
    EvalSplit,
    EvalSuite,
    Experiment,
    ExperimentStatus,
    LeaderboardEntry,
    MatchResult,
    MetricSpec,
    PlaybookEntry,
    Scorecard,
    Surface,
    SurfaceKind,
    SurfaceStatus,
    Tournament,
)


class FakeLabStore:
    """Minimal in-memory LabStore with the same route-facing semantics."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.surfaces: dict[str, dict[str, Any]] = {}
        self.champions: dict[tuple[str, str], dict[str, Any]] = {}
        self.histories: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.experiments: dict[str, dict[str, Any]] = {}
        self.suites: dict[str, dict[str, Any]] = {}
        self.results: dict[str, list[dict[str, Any]]] = {}
        self.judges: dict[str, list[dict[str, Any]]] = {}
        self.tournaments: dict[str, dict[str, Any]] = {}
        self.matches: dict[str, list[dict[str, Any]]] = {}
        self.elos: dict[str, list[dict[str, Any]]] = {}
        self.board: list[dict[str, Any]] = []
        self.playbook: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self._seed()

    @staticmethod
    def _copy(value: Any) -> Any:
        return copy.deepcopy(value)

    def _seed(self) -> None:
        champion = Surface(
            id="srf_champion",
            kind=SurfaceKind.PROMPT,
            discipline="writing",
            path="vault/prompts/champion.md",
            content_hash="champion-hash",
            status=SurfaceStatus.CHAMPION,
            label="Champion",
            created_at="2026-01-01T00:00:00Z",
        )
        challenger = Surface(
            id="srf_challenger",
            kind=SurfaceKind.PROMPT,
            discipline="writing",
            version=2,
            path="vault/prompts/challenger.md",
            content_hash="challenger-hash",
            status=SurfaceStatus.CHALLENGER,
            label="Challenger",
            created_at="2026-01-02T00:00:00Z",
        )
        self.create_surface(champion)
        self.create_surface(challenger)
        self.set_champion(
            ChampionEntry(
                discipline="writing",
                surface_kind=SurfaceKind.PROMPT,
                surface_id=champion.id,
                surface_version=champion.version,
                cas_version=0,
                promoted_at="2026-01-01T00:00:00Z",
            )
        )
        self.suites["evs_writing"] = EvalSuite(
            id="evs_writing",
            discipline="writing",
            metrics=[MetricSpec(name="accuracy", role="primary")],
        ).model_dump(mode="json")
        experiment = Experiment(
            id="exp_seed",
            hypothesis="A clearer prompt improves summaries.",
            discipline="writing",
            mutable_surface_kind=SurfaceKind.PROMPT,
            champion_surface_id=champion.id,
            challenger_surface_id=challenger.id,
            eval_suite_id="evs_writing",
            primary_metric="accuracy",
            budgets=Budgets(replicates=2),
            status=ExperimentStatus.DECIDED,
            disposition=Disposition.PROMOTE,
            scorecard=Scorecard(audit_flags=[], safety_regression=False),
            created_at="2026-01-03T00:00:00Z",
            updated_at="2026-01-03T00:00:00Z",
        )
        self.create_experiment(experiment)
        held_out_case = EvalCase(
            id="evc_held",
            suite="evs_writing",
            split=EvalSplit.HELD_OUT,
            input={"prompt": "protected"},
            expected={"answer": "CANARY_HELD_OUT_EXPECTED"},
        )
        self.results[experiment.id] = [
            {
                "id": "evr_held",
                "experiment_id": experiment.id,
                "split": "held_out",
                "metrics_json": '{"quality":0.9}',
                "per_case_json": '{"case_1":{"quality":0.9}}',
                "expected": held_out_case.expected,
            }
        ]
        self.judges[experiment.id] = [{"id": "jdg_1", "notes": "clearer", "split": "held_out"}]
        tournament = Tournament(
            id="tnm_seed",
            subject="writing-arena",
            discipline="writing",
            arena_task_hash="arena-hash",
            config_ids=[champion.id, challenger.id],
            created_at="2026-01-04T00:00:00Z",
        )
        self.create_tournament(tournament)
        self.matches[tournament.id] = [
            MatchResult(
                id="mch_seed",
                tournament_id=tournament.id,
                config_a=champion.id,
                config_b=challenger.id,
                winner=challenger.id,
                judge_notes="better structure",
            ).model_dump(mode="json")
        ]
        self.elos[tournament.subject] = [
            Elo(subject=tournament.subject, config_id=challenger.id, rating=1024).model_dump(
                mode="json"
            )
        ]
        self.board = [
            LeaderboardEntry(
                subject=tournament.subject,
                discipline="writing",
                rank=1,
                config_id=challenger.id,
                elo=1024,
                summary="Structured writer",
                source_experiments=[experiment.id],
            ).model_dump(mode="json")
        ]
        self.playbook = [
            PlaybookEntry(
                id="pbk_seed", trait="Use a concise review pass", discipline="writing"
            ).model_dump(mode="json")
        ]

    def insert_event(self, *args: Any, **kwargs: Any) -> int:
        self.events.append({"args": args, "kwargs": kwargs})
        return len(self.events)

    def create_surface(self, surface: Surface) -> None:
        self.surfaces[surface.id] = surface.model_dump(mode="json")

    def get_surface(self, surface_id: str) -> dict[str, Any] | None:
        return self._copy(self.surfaces.get(surface_id))

    def list_surfaces(
        self, discipline: str | None = None, kind: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.surfaces.values()
            if (discipline is None or row["discipline"] == discipline)
            and (kind is None or row["kind"] == kind)
        ]
        ordered = sorted(rows, key=lambda row: row["created_at"], reverse=True)
        return self._copy(ordered if limit is None else ordered[:limit])

    def set_surface_status(self, surface_id: str, status: str) -> None:
        self.surfaces[surface_id]["status"] = status

    def get_champion(self, discipline: str, kind: str) -> dict[str, Any] | None:
        return self._copy(self.champions.get((discipline, kind)))

    def list_champions(
        self, discipline: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for (row_discipline, _), row in self.champions.items()
            if discipline is None or row_discipline == discipline
        ]
        ordered = sorted(rows, key=lambda row: row["promoted_at"], reverse=True)
        return self._copy(ordered if limit is None else ordered[:limit])

    def set_champion(self, entry: ChampionEntry) -> None:
        key = (entry.discipline, entry.surface_kind.value)
        current = self.champions.get(key)
        if current is not None and entry.cas_version != current["cas_version"]:
            raise RuntimeError("stale champion")
        row = entry.model_dump(mode="json")
        row["surface_kind"] = entry.surface_kind.value
        row["cas_version"] = 1 if current is None else current["cas_version"] + 1
        self.champions[key] = row
        self.histories.setdefault(key, []).append(
            {"surface_id": entry.surface_id, "event": "promoted", "at": entry.promoted_at}
        )

    def set_champion_with_statuses(
        self, entry: ChampionEntry, *, surface_statuses: dict[str, str]
    ) -> None:
        self.set_champion(entry)
        for surface_id, status in surface_statuses.items():
            self.set_surface_status(surface_id, status)

    def champion_history(self, discipline: str, kind: str) -> list[dict[str, Any]]:
        return self._copy(self.histories.get((discipline, kind), []))

    def discipline_summaries(self, limit: int = 100) -> list[dict[str, Any]]:
        disciplines = sorted({row["discipline"] for row in self.surfaces.values()}, reverse=True)
        return [
            {
                "discipline": discipline,
                "experiment_count": sum(
                    row["discipline"] == discipline for row in self.experiments.values()
                ),
                "top_elo": 1024.0,
                "newest_at": "2026-01-04T00:00:00Z",
            }
            for discipline in disciplines[:limit]
        ]

    def create_experiment(self, experiment: Experiment) -> None:
        row = experiment.model_dump(mode="json")
        row["budgets_json"] = json.dumps(row.pop("budgets"))
        row["promotion_json"] = json.dumps(row.pop("promotion_threshold"))
        row["scorecard_json"] = (
            json.dumps(row.pop("scorecard")) if row["scorecard"] is not None else None
        )
        self.experiments[experiment.id] = row

    def update_experiment(self, experiment_id: str, fields: dict[str, Any]) -> None:
        values = dict(fields)
        if "scorecard" in values:
            scorecard = values.pop("scorecard")
            values["scorecard_json"] = json.dumps(scorecard.model_dump(mode="json"))
        self.experiments[experiment_id].update(values)
        self.experiments[experiment_id]["updated_at"] = utc_now_iso()

    def get_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        return self._copy(self.experiments.get(experiment_id))

    def list_experiments(
        self, discipline: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.experiments.values()
            if (discipline is None or row["discipline"] == discipline)
            and (status is None or row["status"] == status)
        ]
        return self._copy(sorted(rows, key=lambda row: row["created_at"], reverse=True)[:limit])

    def get_eval_suite(self, suite_id: str) -> dict[str, Any] | None:
        return self._copy(self.suites.get(suite_id))

    def eval_results(self, experiment_id: str) -> list[dict[str, Any]]:
        return self._copy(self.results.get(experiment_id, []))

    def record_eval_result(self, result: EvalResult) -> None:
        row = result.model_dump(mode="json")
        row["metrics_json"] = json.dumps(row.pop("metrics"))
        row["per_case_json"] = json.dumps(row.pop("per_case"))
        self.results.setdefault(result.experiment_id, []).append(row)

    def record_judge(self, record: Any) -> None:
        experiment_id = str(getattr(record, "experiment_id", "") or "")
        self.judges.setdefault(experiment_id, []).append(record.model_dump(mode="json"))

    def judge_records(self, experiment_id: str) -> list[dict[str, Any]]:
        return self._copy(self.judges.get(experiment_id, []))

    def create_tournament(self, tournament: Tournament) -> None:
        row = tournament.model_dump(mode="json")
        row["config_ids_json"] = json.dumps(row.pop("config_ids"))
        self.tournaments[tournament.id] = row

    def get_tournament(self, tournament_id: str) -> dict[str, Any] | None:
        return self._copy(self.tournaments.get(tournament_id))

    def list_tournaments(
        self, subject: str | None = None, discipline: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.tournaments.values()
            if (subject is None or row["subject"] == subject)
            and (discipline is None or row["discipline"] == discipline)
        ]
        return self._copy(sorted(rows, key=lambda row: row["created_at"], reverse=True)[:limit])

    def tournament_matches(self, tournament_id: str) -> list[dict[str, Any]]:
        return self._copy(self.matches.get(tournament_id, []))

    def update_tournament(self, tournament_id: str, fields: dict[str, Any]) -> None:
        self.tournaments[tournament_id].update(fields)

    def record_match(self, match: MatchResult) -> None:
        self.matches.setdefault(match.tournament_id, []).append(match.model_dump(mode="json"))

    def get_elo(self, subject: str, config_id: str) -> dict[str, Any] | None:
        return self._copy(
            next(
                (row for row in self.elos.get(subject, []) if row["config_id"] == config_id),
                None,
            )
        )

    def upsert_elo(self, elo: Elo) -> None:
        rows = self.elos.setdefault(elo.subject, [])
        rows[:] = [row for row in rows if row["config_id"] != elo.config_id]
        rows.append(elo.model_dump(mode="json"))

    def elo_for_subject(self, subject: str) -> list[dict[str, Any]]:
        return self._copy(self.elos.get(subject, []))

    def leaderboard(
        self, subject: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        rows = [row for row in self.board if subject is None or row["subject"] == subject]
        ordered = sorted(rows, key=lambda row: (row["subject"], row["rank"]))
        return self._copy(ordered if limit is None else ordered[:limit])

    def list_playbook(
        self, discipline: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            row for row in self.playbook if discipline is None or row["discipline"] == discipline
        ]
        return self._copy(rows if limit is None else rows[:limit])

    def enqueue_job(
        self,
        *,
        job_id: str,
        idempotency_key: str,
        experiment_id: str,
        dry_run: bool,
        now: str,
    ) -> tuple[dict[str, Any], bool]:
        existing = next(
            (job for job in self.jobs.values() if job["idempotency_key"] == idempotency_key),
            None,
        )
        if existing is not None:
            return self._copy(existing), False

        job = {
            "job_id": job_id,
            "idempotency_key": idempotency_key,
            "experiment_id": experiment_id,
            "state": "queued",
            "dry_run": int(dry_run),
            "attempt": 0,
            "lease_owner": None,
            "lease_expires_at": None,
            "lease_generation": 0,
            "cancel_requested": 0,
            "checkpoint_json": "{}",
            "result_json": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
        }
        self.jobs[job_id] = job
        return self._copy(job), True

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self._copy(self.jobs.get(job_id))

    def request_job_cancel(self, *, job_id: str, now: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        state = job["state"]
        if state in ("succeeded", "failed", "cancelled"):
            return self._copy(job)

        if state == "queued":
            job["state"] = "cancelled"
            job["cancel_requested"] = 1
            job["finished_at"] = now
            job["error"] = "cancelled before start"
            job["lease_owner"] = None
            job["lease_expires_at"] = None
            job["updated_at"] = now
        elif state == "running":
            job["cancel_requested"] = 1
            job["updated_at"] = now

        return self._copy(job)


@pytest.fixture
def store() -> FakeLabStore:
    return FakeLabStore()


@pytest.fixture
def asgi_client(
    store: FakeLabStore, monkeypatch: pytest.MonkeyPatch
) -> Generator[httpx.AsyncClient, None, None]:
    class FakeEvaluator:
        def run_deterministic(
            self,
            suite_id: str,
            split: EvalSplit,
            arm: str,
            outputs: dict[str, Any],
        ) -> EvalResult:
            del outputs
            return EvalResult(
                experiment_id="",
                arm=arm,
                suite_id=suite_id,
                suite_version=1,
                split=split,
                metrics={"accuracy": 0.80 if arm == "champion" else 0.86},
            )

        def score_experiment(self, exp_id: str, *, dev_only: bool) -> Scorecard:
            del exp_id, dev_only
            return Scorecard()

    executor = ModuleType("omniagentos.lab.executor")

    def run_surface_over_cases(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"outputs": {"case-1": {"text": "candidate-safe output"}}, "manifests": []}

    executor.run_surface_over_cases = run_surface_over_cases  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omniagentos.lab.executor", executor)
    app.dependency_overrides[get_lab_store] = lambda: store
    app.dependency_overrides[get_protected_evaluator] = FakeEvaluator
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
