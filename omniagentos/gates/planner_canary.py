"""Hourly liveness probe for the swarm planner.

Probes the CONFIGURED planner model (gpt-5.6-sol since the 2026-07-31 operator
repoint) every run. The fable escalation rung is probed only once per day
(FABLE_DAILY_HOUR_UTC) — an hourly fable probe burned a Claude-subscription
call 24x/day to smoke-test a model that no longer sits on the planner rung.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.intake.fable import run_fable_json

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
}


def _log_path() -> Path:
    """Resolve the canary log path at call time, never at import.

    Deliberately __file__-anchored only: the deployed launchd job points
    OMNIAGENTOS_VAR / OMNIAGENTOS_VAR_DIR at var/runtime, so honoring
    those knobs would silently move the log away from var/log/.
    """
    return Path(__file__).resolve().parents[2] / "var" / "log" / "planner-canary.jsonl"


# Must match configs/swarm.yaml `planner.model`; the canary exists to prove the
# planner rung is alive, so it probes what the planner actually runs.
PLANNER_MODEL = "gpt-5.6-sol"
# ~07:00 EDT — the single daily probe of the fable escalation rung.
FABLE_DAILY_HOUR_UTC = 11


def _record(model: str, ok: bool, elapsed_ms: int) -> None:
    """Append one canary result to the project log."""
    log_path = _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "model": model,
        "ok": ok,
        "ms": elapsed_ms,
    }
    with log_path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")


def _probe(model: str) -> tuple[bool, int]:
    """Probe one model through the planner path; return (ok, elapsed_ms)."""
    started = time.monotonic()
    result = run_fable_json(
        'Respond with `{"ok": true}`',
        SCHEMA,
        model=model,
        effort="low",
        wall_ms=60_000,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    ok = result is not None
    _record(model, ok, elapsed_ms)
    return ok, elapsed_ms


def main(*, now: datetime | None = None) -> int:
    """Run the planner liveness probe(s) and return a process exit code."""
    moment = now or datetime.now(UTC)
    planner_ok, planner_ms = _probe(PLANNER_MODEL)
    failures: list[str] = []
    if planner_ok:
        print(f"CANARY OK {PLANNER_MODEL} {planner_ms}")
    else:
        failures.append(
            f"planner ({PLANNER_MODEL}) returned None — swarm dispatches are "
            "silently degrading to flat solo plans"
        )
    if moment.hour == FABLE_DAILY_HOUR_UTC:
        fable_ok, fable_ms = _probe("fable")
        if fable_ok:
            print(f"CANARY OK fable {fable_ms}")
        else:
            failures.append(
                "daily fable probe returned None — the escalation rung "
                "(model_ladder top / complex floor) is unavailable"
            )
    for reason in failures:
        print(f"CANARY FAILED: {reason}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
