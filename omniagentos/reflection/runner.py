"""Orchestrator runner for the nightly reflection loop (Lane R3).

Performs: harvest -> propose -> validate -> apply(observe-aware) -> report.
"""

from __future__ import annotations

import argparse
import inspect
import os
import sqlite3
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.reflection.fingerprint import proposal_fingerprint, proposal_strings
from omniagentos.reflection.settlement import (
    Settlement,
    classify_settlement,
    new_run_id,
    run_settlement,
)
from omniagentos.reflection.validate import validate_batch

# The five stage columns of reflection_runs, in execution order.
STAGES: tuple[str, ...] = ("harvest", "propose", "validate", "apply", "report")


class ReflectionRunDAL:
    """Data Access Layer for reflection_runs logging."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Assert that migration 077 has installed the reflection_runs table."""
        conn = sqlite3.connect(self.db_path)
        try:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("reflection_runs",),
            ).fetchone()
            if table is None:
                raise RuntimeError(
                    "reflection_runs table is missing; run `make migrate` before "
                    "starting the reflection runner"
                )
        finally:
            conn.close()

    def create_run(self, run_id: str) -> None:
        now = utc_now_iso()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO reflection_runs (
                    id, status, started_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, "running", now, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    def update_stage_status(
        self, run_id: str, stage: str, status: str | Settlement, error: str | None = None
    ) -> None:
        if stage not in STAGES:
            raise ValueError(f"Invalid stage name: {stage}")

        status = status.value if isinstance(status, Settlement) else status
        column_name = f"{stage}_status"
        now = utc_now_iso()
        conn = sqlite3.connect(self.db_path)
        try:
            if error:
                conn.execute(
                    f"""
                    UPDATE reflection_runs
                    SET {column_name} = ?, error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, error, now, run_id),
                )
            else:
                conn.execute(
                    f"""
                    UPDATE reflection_runs
                    SET {column_name} = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (status, now, run_id),
                )
            conn.commit()
        finally:
            conn.close()

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        now = utc_now_iso()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE reflection_runs
                SET status = ?, finished_at = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, now, error, now, run_id),
            )
            conn.commit()
        finally:
            conn.close()

    def settle_stage(
        self,
        run_id: str,
        stage: str,
        settlement: Settlement,
        error: str | None = None,
    ) -> Settlement:
        """Record the ARTIFACT-derived settlement for *stage*.

        This is the only way a stage status reaches the database.  There is no
        code path that writes a success literal without a settlement first
        having been derived from what the stage actually produced.
        """
        self.update_stage_status(run_id, stage, settlement.value, error=error)
        return settlement

    def stage_settlements(self, run_id: str) -> dict[str, str | None]:
        row = self.get_run(run_id) or {}
        return {stage: row.get(f"{stage}_status") for stage in STAGES}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM reflection_runs WHERE id = ?", (run_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


# Lazy-import resolution helpers to remain decoupled from R1/R2 packages


def get_harvester() -> Any:
    try:
        from omniagentos.reflection import harvest

        return harvest
    except ImportError:

        class MockHarvester:
            @staticmethod
            def harvest_evidence(date_str: str) -> dict[str, Any]:
                # Minimal mock fallback
                return {"date": date_str, "status": "mocked", "size": 100}

        return MockHarvester


def get_proposer() -> Any:
    try:
        from omniagentos.reflection import propose

        return propose
    except ImportError:

        class MockProposer:
            @staticmethod
            def generate_proposals(evidence: dict[str, Any]) -> list[dict[str, Any]]:
                # Minimal mock fallback return proposals
                return [
                    {
                        "id": "prop_mock_1",
                        "kind": "router_weight",
                        "target": {
                            "file": "configs/swarm.yaml",
                            "key": "router.lane_floors.weights",
                        },
                        "current": 1.0,
                        "proposed": 1.2,
                        "rationale": "Optimize router selection based on shadow win-rate analysis",
                        "evidence_refs": [],
                        "predicted_impact": "Higher performance router choices",
                        "risk_class": "low",
                    }
                ]

        return MockProposer


def get_applier() -> Any:
    try:
        from omniagentos.reflection import apply

        return apply
    except ImportError:

        class MockApplier:
            @staticmethod
            def apply_proposals(
                proposals: list[dict[str, Any]], observe_only: bool = True
            ) -> list[dict[str, Any]]:
                # Minimal mock fallback
                return [
                    {
                        "proposal_id": p["id"],
                        "status": "observed" if observe_only else "applied",
                    }
                    for p in proposals
                ]

            @staticmethod
            def auto_apply_eligible(
                db_path: str | None = None,
                accepted_fingerprints: Any = None,
            ) -> list[dict[str, Any]]:
                # Minimal mock fallback — mirrors the real fail-closed contract.
                return []

        return MockApplier


def get_reporter() -> Any:
    try:
        from omniagentos.reflection import report

        return report
    except ImportError:

        class MockReporter:
            @staticmethod
            def write_report(outcomes: list[dict[str, Any]], date_str: str) -> str:
                # Minimal mock fallback
                return f"# Nightly Reflection Report - {date_str}\nStatus: Completed successfully."

        return MockReporter


def load_reflection_config() -> dict[str, Any]:
    config_path = Path(__file__).resolve().parent.parent.parent / "configs" / "reflection.yaml"
    if not config_path.exists():
        return {
            "limits": {
                "max_formation_changes": 1,
                "max_router_weight_changes": 2,
                "max_context_size_bytes": 10485760,
            },
            "validation": {
                "router_win_rate_threshold": 0.55,
                "router_min_decisions": 10,
            },
        }
    try:
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _call_harvest(harvester: Any, date_str: str, run_id: str) -> Any:
    """Invoke the harvester, handing it the ALREADY-OPEN run row when it can
    accept one.

    The harvester used to mint its own ``ref_…`` id and INSERT a second row for
    the same night, so every night produced two rows that disagreed.  It now
    writes into the runner's row when the runner owns the lifecycle.
    """
    fn = harvester.harvest_evidence
    params: Mapping[str, inspect.Parameter]
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    accepts_run_id = "run_id" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if accepts_run_id:
        return fn(date_str, run_id=run_id)
    return fn(date_str)


def _harvest_evidence_flag(result: Any) -> bool:
    """True only when the harvest result proves it actually read something."""
    if result is None:
        return False
    for attr in ("bytes_read", "sources_read", "sources"):
        value = getattr(result, attr, None)
        if value is None and isinstance(result, dict):
            value = result.get(attr)
        if isinstance(value, int | float) and value > 0:
            return True
        if isinstance(value, list | tuple | set | dict) and len(value) > 0:
            return True
    return False


def _harvest_settlement(harvester: Any, date_str: str, result: Any) -> Settlement:
    """Settle the harvest stage on ``evidence.json``, not on "nothing threw"."""
    getter = getattr(harvester, "evidence_artifact_paths", None)
    if callable(getter):
        try:
            evidence_path, _digest_path = getter(date_str)
        except Exception:
            evidence_path = None
        if evidence_path is not None:
            return classify_settlement(evidence_path)
    return classify_settlement(evidence=_harvest_evidence_flag(result))


def run_reflection_loop(observe_only: bool | None = None) -> str:
    """Orchestrate the nightly reflection loop execution.

    Every stage status recorded here is derived from the artifact the stage
    produced via the one shared ``classify_settlement`` helper.  A stage that
    produced nothing gradeable settles ``ungateable`` — explicitly, so it is
    excluded from the acceptance floor instead of being counted as a success.
    """
    run_id = new_run_id()
    dal = ReflectionRunDAL()
    dal.create_run(run_id)

    date_str = datetime.now(UTC).strftime("%Y-%m-%d")

    # Determine observe-only mode from parameters, environment, or config files
    if observe_only is None:
        env_val = os.environ.get("OMNIAGENTOS_REFLECTION_OBSERVE_ONLY")
        if env_val is not None:
            observe_only = env_val == "1"
        else:
            config = load_reflection_config()
            observe_only = config.get("observe_only", True)

    limits_config = load_reflection_config()

    settlements: dict[str, Settlement] = {}

    try:
        # 1. Harvest Stage — settled on var/reflection/<date>/evidence.json
        dal.update_stage_status(run_id, "harvest", "running")
        try:
            harvester = get_harvester()
            harvest_result = _call_harvest(harvester, date_str, run_id)
            settlements["harvest"] = dal.settle_stage(
                run_id, "harvest", _harvest_settlement(harvester, date_str, harvest_result)
            )
        except Exception as exc:
            settlements["harvest"] = dal.settle_stage(
                run_id, "harvest", classify_settlement(error=exc), error=str(exc)
            )
            raise

        # 2. Propose Stage — settled on whether proposals were actually produced
        dal.update_stage_status(run_id, "propose", "running")
        try:
            proposer = get_proposer()
            propose_result = proposer.run_propose(date_str)
            proposals = (
                propose_result.get("proposals", []) if isinstance(propose_result, dict) else []
            )
            settlements["propose"] = dal.settle_stage(
                run_id, "propose", classify_settlement(evidence=bool(proposals))
            )
        except Exception as exc:
            settlements["propose"] = dal.settle_stage(
                run_id, "propose", classify_settlement(error=exc), error=str(exc)
            )
            raise

        # 3. Validate Stage
        accepted_fingerprints: dict[str, str] = {}
        dal.update_stage_status(run_id, "validate", "running")
        try:
            validation_results = validate_batch(proposals, limits_config)
            # Bind acceptance to exact content, not just id — apply verifies the
            # row still carries this fingerprint before writing anything.
            for p, ok, _reason in validation_results:
                if not ok or not p.get("id"):
                    continue
                target_str, proposed_str = proposal_strings(p.get("target"), p.get("proposed"))
                accepted_fingerprints[str(p["id"])] = proposal_fingerprint(
                    str(p.get("kind")), target_str, proposed_str
                )
            # A validation pass with nothing to validate is UNGATEABLE, not a
            # success: there was no decision to grade.
            settlements["validate"] = dal.settle_stage(
                run_id, "validate", classify_settlement(evidence=bool(validation_results))
            )
        except Exception as exc:
            settlements["validate"] = dal.settle_stage(
                run_id, "validate", classify_settlement(error=exc), error=str(exc)
            )
            raise

        # 4. Apply Stage
        dal.update_stage_status(run_id, "apply", "running")
        try:
            applier = get_applier()
            # Observe-only mode never writes; otherwise only the auto-apply
            # eligible tier (lesson/brief_template/model_config availability)
            # is applied — and only proposals the validate stage accepted,
            # verified by content fingerprint. Everything else stays pending
            # for human approval.
            applied: list[Any] = []
            if not observe_only:
                applied = (
                    applier.auto_apply_eligible(accepted_fingerprints=accepted_fingerprints) or []
                )
            # Observe-only nights apply nothing, so there is nothing to grade.
            settlements["apply"] = dal.settle_stage(
                run_id, "apply", classify_settlement(evidence=bool(applied))
            )
        except Exception as exc:
            settlements["apply"] = dal.settle_stage(
                run_id, "apply", classify_settlement(error=exc), error=str(exc)
            )
            raise

        # 5. Report Stage — settled on the BRIEFING FILE, never on the fact
        # that generate_reflection_report() returned without raising.
        dal.update_stage_status(run_id, "report", "running")
        try:
            reporter = get_reporter()
            briefing_path = reporter.generate_reflection_report(date_str)
            settlements["report"] = dal.settle_stage(
                run_id, "report", classify_settlement(briefing_path)
            )
        except Exception as exc:
            settlements["report"] = dal.settle_stage(
                run_id, "report", classify_settlement(error=exc), error=str(exc)
            )
            raise

        # reflection_runs.status is CHECK-constrained to running/completed/failed,
        # so it stays the coarse lifecycle field; the truthful three-valued
        # settlement lives in the per-stage columns and is folded here.
        overall = run_settlement(settlements.get(stage) for stage in STAGES)
        dal.finish_run(run_id, "failed" if overall is Settlement.FAILED else "completed")
        return run_id

    except Exception as exc:
        dal.finish_run(run_id, "failed", error=str(exc))
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Nightly Reflection Loop")
    parser.add_argument(
        "--observe-only",
        action="store_true",
        default=None,
        help="Run loop in observe-only mode (do not commit config changes)",
    )
    args = parser.parse_args()

    try:
        run_id = run_reflection_loop(observe_only=args.observe_only)
        print(f"Reflection loop execution finished. Run ID: {run_id}")
    except Exception as exc:
        print(f"Reflection loop failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
