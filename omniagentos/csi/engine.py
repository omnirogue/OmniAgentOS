"""CSI engine — shared pipeline through NO_CHANGE or AWAITING_HUMAN (PLAN §3).

Never merges. Implement only after human approval via ``csi.implement``.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from omniagentos.csi.config import CsiConfig, load_csi_config
from omniagentos.csi.conflict import ConflictForecastService
from omniagentos.csi.evidence import IMPLEMENTED_ROUTINES, compile_packet
from omniagentos.csi.models import CsiRunResult
from omniagentos.csi.planner import RoutinePlanner
from omniagentos.csi.store import CsiStore
from omniagentos.csi.synthesis import PlanSynthesisService

LOG = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parents[2]


class CsiEngine:
    def __init__(
        self,
        *,
        config: CsiConfig | None = None,
        db_path: str | Path | None = None,
        store: CsiStore | None = None,
        product_db_path: str | Path | None = None,
    ) -> None:
        import warnings

        warnings.warn(
            "DEPRECATION WARNING: The csi engine is frozen and deprecated. It will be removed in a future release.",
            DeprecationWarning,
            stacklevel=2,
        )
        LOG.warning(
            "DEPRECATION WARNING: The csi engine is frozen and deprecated. It will be removed in a future release."
        )
        self.config = config or load_csi_config()
        default_db = _ROOT / "var" / "csi" / "csi.db"
        self.db_path = str(db_path or default_db)
        self.product_db_path = str(product_db_path or self.db_path)
        self.store = store or CsiStore(self.db_path)

    def run_routine(self, routine_id: str, *, force: bool = False) -> CsiRunResult:
        t0 = time.monotonic()
        # global_halt is never bypassed by --force (Opus caveat).
        if self.config.global_halt:
            return CsiRunResult(
                run_id="",
                routine_id=routine_id,
                status="CANCELLED",
                verdict="halted",
                no_change_reason="self_improvement.global_halt is true",
                wall_clock_s=0.0,
            )
        ok, reason = self.config.is_runnable()
        # force only bypasses enabled:false, not halt
        if not ok and not force:
            return CsiRunResult(
                run_id="",
                routine_id=routine_id,
                status="CANCELLED",
                verdict="halted",
                no_change_reason=reason,
                wall_clock_s=0.0,
            )
        if not ok and force and "global_halt" in reason:
            return CsiRunResult(
                run_id="",
                routine_id=routine_id,
                status="CANCELLED",
                verdict="halted",
                no_change_reason=reason,
                wall_clock_s=0.0,
            )

        spec = self.config.routine(routine_id)
        if spec is None:
            return CsiRunResult(
                run_id="",
                routine_id=routine_id,
                status="CANCELLED",
                verdict="halted",
                no_change_reason=f"unknown_routine:{routine_id}",
            )
        if not spec.enabled and not force:
            return CsiRunResult(
                run_id="",
                routine_id=routine_id,
                status="CANCELLED",
                verdict="halted",
                no_change_reason=f"routine_disabled:{routine_id}",
            )

        rid = routine_id.replace("-", "_")
        if rid not in IMPLEMENTED_ROUTINES:
            return CsiRunResult(
                run_id="",
                routine_id=routine_id,
                status="CANCELLED",
                verdict="halted",
                no_change_reason=f"routine_not_implemented_yet:{routine_id}",
            )
        try:
            packet = compile_packet(
                rid,
                config=self.config,
                db_path=self.db_path,
                window_days=spec.window_days,
            )
        except ValueError as exc:
            return CsiRunResult(
                run_id="",
                routine_id=routine_id,
                status="CANCELLED",
                verdict="halted",
                no_change_reason=str(exc),
            )

        run_id = self.store.create_run(
            routine_id=routine_id,
            window_days=spec.window_days,
            codebase_sha=packet.codebase_sha,
            evidence=packet.model_dump(),
        )

        try:
            planner = RoutinePlanner(
                timeout_s=float(self.config.planner_timeout_s),
                min_panel_size=self.config.min_panel_size,
                use_live=self.config.use_live_planners,
            )
        except RuntimeError as exc:
            self.store.finish_run(
                run_id,
                status="INCIDENT",
                verdict="halted",
                no_change_reason=str(exc),
                wall_clock_s=time.monotonic() - t0,
                error=str(exc),
            )
            return CsiRunResult(
                run_id=run_id,
                routine_id=routine_id,
                status="INCIDENT",
                verdict="halted",
                no_change_reason=str(exc),
                wall_clock_s=time.monotonic() - t0,
                error=str(exc),
            )

        plans = planner.run_panel(packet, spec.planners)
        for plan in plans:
            self.store.save_plan(run_id, plan)

        synthesis = PlanSynthesisService(min_panel_size=self.config.min_panel_size).synthesize(
            plans
        )

        conflict_svc = ConflictForecastService()
        conflict = conflict_svc.forecast(synthesis.accepted_proposals)

        improvement_id: str | None = None
        approval_status = "none"
        if synthesis.verdict == "insufficient_panel":
            status, verdict = "NO_CHANGE", "insufficient_panel"
            ncr = synthesis.reason
        elif synthesis.verdict == "no_change":
            status, verdict = "NO_CHANGE", "no_change"
            ncr = synthesis.reason
        elif not conflict.safe_to_implement:
            status, verdict = "DEFERRED", "propose"
            ncr = "conflict_forecast_blocked: " + "; ".join(conflict.reasons)
        else:
            # CSI-native approval only (isolated from reliability apply path).
            # Optional audit-only improvements row is NOT used for implement unlock.
            status, verdict = "AWAITING_HUMAN", "propose"
            ncr = ""
            approval_status = "proposed"
            try:
                # Optional dual-write for human visibility only — implement
                # never keys off this (Opus final #1). Prefer product_db when set.
                from omniagentos.csi.approval import create_approval_card

                improvement_id = create_approval_card(
                    db_path=self.product_db_path,
                    run_id=run_id,
                    routine_id=routine_id,
                    synthesis=synthesis,
                    conflict=conflict.model_dump(),
                )
            except Exception as exc:  # noqa: BLE001
                LOG.warning("optional audit card failed: %s", exc, exc_info=True)
                # Still AWAITING_HUMAN — native approval_status=proposed is enough

        wall = time.monotonic() - t0
        self.store.finish_run(
            run_id,
            status=status,
            verdict=verdict,
            no_change_reason=ncr,
            synthesis=synthesis,
            conflict=conflict.model_dump(),
            improvement_id=improvement_id,
            wall_clock_s=wall,
            approval_status=approval_status,
        )
        return CsiRunResult(
            run_id=run_id,
            routine_id=routine_id,
            status=status,
            verdict=verdict,
            no_change_reason=ncr,
            synthesis=synthesis,
            conflict=conflict,
            improvement_id=improvement_id,
            wall_clock_s=wall,
        )


def run_self_learning(
    *,
    force: bool = False,
    db_path: str | Path | None = None,
    product_db_path: str | Path | None = None,
    config: CsiConfig | None = None,
) -> CsiRunResult:
    """Entry point for Self-Learning routine (routine #2)."""
    return run_routine(
        "self_learning",
        force=force,
        db_path=db_path,
        product_db_path=product_db_path,
        config=config,
    )


def run_routine(
    routine_id: str,
    *,
    force: bool = False,
    db_path: str | Path | None = None,
    product_db_path: str | Path | None = None,
    config: CsiConfig | None = None,
) -> CsiRunResult:
    """Run any implemented CSI routine (design, self_learning, routing, …)."""
    return CsiEngine(
        config=config,
        db_path=db_path,
        product_db_path=product_db_path or db_path,
    ).run_routine(routine_id.replace("-", "_"), force=force)


__all__ = ["CsiEngine", "run_routine", "run_self_learning"]
