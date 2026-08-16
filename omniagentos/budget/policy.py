"""Whether a budget STOPS work, and how big a task's budget should be.

the operator's directive (2026-07-24): *"raise the budget limit or remove it, it should be
task dependent but we should not have hard blockers."*

So a budget is now two separable things:

  * a **size**, derived from the work in front of it (:func:`swarm_run_budget`),
    so a 40-task swarm is not handed the same ceiling as a 3-task one; and
  * an **enforcement mode**, which defaults to ``advisory``: overshooting is
    recorded and surfaced, and work CONTINUES.

Before this, one number did both jobs and every overshoot was terminal — the
runner failed the run ``budget_exceeded``, the swarm scheduler blocked every
un-started task, the session reaper killed the process, and the per-worker
``--max-budget-usd`` made Claude Code stop spawning agents mid-task ("Budget
limit reached ($X of $Y)"). That is the class of hard blocker being removed.

Set ``OMNIAGENTOS_BUDGET_ENFORCEMENT=block`` to restore the terminal behavior
(useful for an untrusted or unattended run); anything else, including unset,
is advisory.

**This is a real trade-off, stated plainly:** in advisory mode nothing mechanical
stops a runaway loop from spending. The remaining controls are observability (the
overshoot notification names the run and the amount) and the provider's own
account limits. Enforcement is per-process env, so a single run can opt back in.
"""

from __future__ import annotations

import os

ENFORCEMENT_ENV = "OMNIAGENTOS_BUDGET_ENFORCEMENT"
BLOCK = "block"
ADVISORY = "advisory"

# Task-dependent sizing. A budget is an EXPECTATION, not a wall: it exists so an
# overshoot is visible and attributable, and so per-task spend stays comparable
# across runs of different sizes.
DEFAULT_PER_TASK_USD = 5.0
# Even a one-task run gets room to finish rather than dying near the line.
MIN_RUN_BUDGET_USD = 25.0


def enforcement_mode() -> str:
    """``"block"`` only when explicitly requested; ``"advisory"`` otherwise."""
    raw = (os.environ.get(ENFORCEMENT_ENV) or "").strip().lower()
    return BLOCK if raw == BLOCK else ADVISORY


def blocks() -> bool:
    """True when exceeding a budget should STOP work.

    Every enforcement site calls this rather than reading the env itself, so the
    posture is one decision in one place — and a test can flip it with monkeypatch.
    """
    return enforcement_mode() == BLOCK


def swarm_run_budget(task_count: int, requested: float | None = None) -> float:
    """Budget for a swarm run: the caller's number, else sized to the plan.

    An explicit ``requested`` value always wins — asking for a specific budget is
    a deliberate act. Otherwise the run gets ``DEFAULT_PER_TASK_USD`` per planned
    task, floored at ``MIN_RUN_BUDGET_USD``.
    """
    if requested is not None and requested > 0:
        return float(requested)
    return max(MIN_RUN_BUDGET_USD, DEFAULT_PER_TASK_USD * max(1, int(task_count or 1)))


def session_budget_cap(budget_usd_max: float | None) -> float | None:
    """The cap to hand a provider CLI, or None to leave it uncapped.

    In advisory mode this returns None even when a budget exists: a CLI-level cap
    is enforced INSIDE the worker, where it aborts the task half-done and reports
    a message OmniAgentOS cannot attribute to a run. Spend is still recorded on
    the session row either way, so the number is not lost — only the kill is.
    """
    if not blocks():
        return None
    return budget_usd_max


__all__ = [
    "ADVISORY",
    "BLOCK",
    "DEFAULT_PER_TASK_USD",
    "ENFORCEMENT_ENV",
    "MIN_RUN_BUDGET_USD",
    "blocks",
    "enforcement_mode",
    "session_budget_cap",
    "swarm_run_budget",
]
