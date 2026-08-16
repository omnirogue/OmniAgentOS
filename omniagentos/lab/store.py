"""Persistence boundary for auditable lab-verdict provenance."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.lab.db.store import LabStore as BaseLabStore

DEFAULT_AGREEMENT_FLOOR = 0.60
DEFAULT_MIN_EFFECTIVE_N = 2

INVALIDATED_VERDICTS_SQL = """
SELECT *
FROM lab_verdict_provenance
WHERE invalidation_status = 'invalidated'
   OR panel_lineage_count < 2
   OR agreement < ?
   OR effective_n < ?
   OR ABS(observed_effect) < mde
ORDER BY created_at ASC, verdict_id ASC
"""


@dataclass(frozen=True)
class PanelMember:
    """One judge recorded in a verdict panel."""

    identity: str
    lineage: str


@dataclass(frozen=True)
class VerdictProvenance:
    """Stable persistence contract for the evidence behind one verdict."""

    experiment_id: str
    panel_composition: tuple[PanelMember, ...]
    replicate_count: int
    effective_n: int
    agreement: float
    mde: float
    observed_effect: float
    blind_presentation_seed: int
    invalidation_status: str = "valid"
    verdict_id: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.experiment_id:
            raise ValueError("experiment_id must not be empty")
        if self.replicate_count < 0 or self.effective_n < 0:
            raise ValueError("provenance counts must be non-negative")
        if not 0.0 <= self.agreement <= 1.0:
            raise ValueError("agreement must be in [0, 1]")
        if self.mde < 0.0:
            raise ValueError("mde must be non-negative")
        if self.invalidation_status not in {"valid", "invalidated"}:
            raise ValueError("invalidation_status must be valid or invalidated")


@dataclass(frozen=True)
class InvalidatedVerdict:
    """A verdict selected for invalidation, with every applicable reason."""

    provenance: VerdictProvenance
    reasons: tuple[str, ...]


def _from_row(row: sqlite3.Row) -> VerdictProvenance:
    panel_data: list[dict[str, Any]] = json.loads(str(row["panel_composition_json"]))
    return VerdictProvenance(
        verdict_id=str(row["verdict_id"]),
        experiment_id=str(row["experiment_id"]),
        panel_composition=tuple(
            PanelMember(identity=str(member["identity"]), lineage=str(member["lineage"]))
            for member in panel_data
        ),
        replicate_count=int(row["replicate_count"]),
        effective_n=int(row["effective_n"]),
        agreement=float(row["agreement"]),
        mde=float(row["mde"]),
        observed_effect=float(row["observed_effect"]),
        invalidation_status=str(row["invalidation_status"]),
        blind_presentation_seed=int(row["blind_presentation_seed"]),
        created_at=str(row["created_at"]),
    )


class LabStore(BaseLabStore):
    """Existing lab store extended with verdict-provenance persistence."""

    def record_verdict_provenance(self, record: VerdictProvenance) -> str:
        """Persist the latest provenance for an experiment and return its id."""

        verdict_id = record.verdict_id or new_id("lvp")
        created_at = record.created_at or utc_now_iso()
        panel_json = json.dumps(
            [asdict(member) for member in record.panel_composition],
            sort_keys=True,
            separators=(",", ":"),
        )
        lineage_count = len({member.lineage for member in record.panel_composition})
        self._store._write(
            """
            INSERT INTO lab_verdict_provenance (
              verdict_id, experiment_id, panel_composition_json, panel_lineage_count,
              replicate_count, effective_n, agreement, mde, observed_effect,
              invalidation_status, blind_presentation_seed, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(experiment_id) DO UPDATE SET
              verdict_id = excluded.verdict_id,
              panel_composition_json = excluded.panel_composition_json,
              panel_lineage_count = excluded.panel_lineage_count,
              replicate_count = excluded.replicate_count,
              effective_n = excluded.effective_n,
              agreement = excluded.agreement,
              mde = excluded.mde,
              observed_effect = excluded.observed_effect,
              invalidation_status = excluded.invalidation_status,
              blind_presentation_seed = excluded.blind_presentation_seed,
              created_at = excluded.created_at
            """,
            (
                verdict_id,
                record.experiment_id,
                panel_json,
                lineage_count,
                record.replicate_count,
                record.effective_n,
                record.agreement,
                record.mde,
                record.observed_effect,
                record.invalidation_status,
                record.blind_presentation_seed,
                created_at,
            ),
        )
        return verdict_id

    def get_verdict_provenance(self, experiment_id: str) -> VerdictProvenance | None:
        """Return the persisted verdict provenance for an experiment."""

        row = self._connection.execute(
            "SELECT * FROM lab_verdict_provenance WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()
        return None if row is None else _from_row(row)

    def invalidated_verdicts(
        self,
        *,
        agreement_floor: float = DEFAULT_AGREEMENT_FLOOR,
        min_effective_n: int = DEFAULT_MIN_EFFECTIVE_N,
    ) -> list[InvalidatedVerdict]:
        """Find verdicts that fail current measurement-validity requirements."""

        rows = self._connection.execute(
            INVALIDATED_VERDICTS_SQL,
            (agreement_floor, min_effective_n),
        ).fetchall()
        results: list[InvalidatedVerdict] = []
        for row in rows:
            reasons: list[str] = []
            if row["invalidation_status"] == "invalidated":
                reasons.append("EXPLICITLY_INVALIDATED")
            if int(row["panel_lineage_count"]) < 2:
                reasons.append("SINGLE_LINEAGE_PANEL")
            if float(row["agreement"]) < agreement_floor:
                reasons.append("LOW_AGREEMENT")
            if int(row["effective_n"]) < min_effective_n:
                reasons.append("INSUFFICIENT_N")
            if abs(float(row["observed_effect"])) < float(row["mde"]):
                reasons.append("EFFECT_BELOW_MDE")
            results.append(InvalidatedVerdict(_from_row(row), tuple(reasons)))
        return results

    def invalidate_verdict(self, experiment_id: str) -> bool:
        """Explicitly invalidate an existing experiment verdict."""

        updated = self._store._write_count(
            "UPDATE lab_verdict_provenance "
            "SET invalidation_status = 'invalidated' WHERE experiment_id = ?",
            (experiment_id,),
        )
        return updated == 1
