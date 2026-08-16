"""Request models for the lab HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from omniagentos.lab.contracts import Budgets, Disposition, ExplorePolicy, SurfaceKind


class CreateExperimentRequest(BaseModel):
    hypothesis: str = Field(min_length=1)
    discipline: str = Field(min_length=1)
    mutable_surface_kind: SurfaceKind
    challenger_surface_id: str = Field(min_length=1)
    eval_suite_id: str = Field(min_length=1)
    budgets: Budgets | None = None
    explore_policy: ExplorePolicy | None = None


class RunExperimentRequest(BaseModel):
    dry_run: bool = False


class DispositionRequest(BaseModel):
    decision: Disposition
    note: str = ""
    decided_by: str = Field(min_length=1)
    operator_token: str | None = None


class RollbackRequest(BaseModel):
    operator_token: str | None = None


class CreateTournamentRequest(BaseModel):
    subject: str = Field(min_length=1)
    discipline: str = Field(min_length=1)
    config_ids: list[str] = Field(min_length=1)
    arena_task: dict[str, Any]
    dry_run: bool = False


class CurateRequest(BaseModel):
    dry_run: bool = False
