"""Fleet capacity snapshot for fan-out hard ceilings (Volume III §4.1)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    global_free_slots: int
    project_free_slots: int
    domain_free_slots: int
    provider_quota: int
    repository_writer_slots: int
    verifier_absorption: int
    integration_absorption: int
    budget_workers: int | None = None

    def hard_capacity(self, independent_units: int) -> int:
        limits = [
            max(0, int(independent_units)),
            max(0, self.global_free_slots),
            max(0, self.project_free_slots),
            max(0, self.domain_free_slots),
            max(0, self.provider_quota),
            max(0, self.repository_writer_slots),
            max(0, self.verifier_absorption),
            max(0, self.integration_absorption),
        ]
        if self.budget_workers is not None:
            limits.append(max(0, int(self.budget_workers)))
        return min(limits) if limits else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "global_free_slots": self.global_free_slots,
            "project_free_slots": self.project_free_slots,
            "domain_free_slots": self.domain_free_slots,
            "provider_quota": self.provider_quota,
            "repository_writer_slots": self.repository_writer_slots,
            "verifier_absorption": self.verifier_absorption,
            "integration_absorption": self.integration_absorption,
            "budget_workers": self.budget_workers,
        }


def capacity_from_mapping(raw: Mapping[str, Any]) -> CapacitySnapshot:
    return CapacitySnapshot(
        global_free_slots=int(raw.get("global_free_slots", raw.get("free_slots", 1))),
        project_free_slots=int(raw.get("project_free_slots", 8)),
        domain_free_slots=int(raw.get("domain_free_slots", 8)),
        provider_quota=int(raw.get("provider_quota", 10)),
        repository_writer_slots=int(raw.get("repository_writer_slots", 4)),
        verifier_absorption=int(raw.get("verifier_absorption", raw.get("verifier_capacity", 2))),
        integration_absorption=int(raw.get("integration_absorption", 2)),
        budget_workers=(
            int(raw["budget_workers"]) if raw.get("budget_workers") is not None else None
        ),
    )


# Coding policy: four concurrent writers per repository (Volume I).
DEFAULT_REPO_WRITER_SLOTS = 4
