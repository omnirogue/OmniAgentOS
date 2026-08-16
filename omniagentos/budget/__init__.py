"""Pure budget-limit checks."""

from __future__ import annotations

from omniagentos.contracts import BudgetDecision, BudgetSpec


def check(
    spec: BudgetSpec,
    used_wall_ms: int,
    used_tokens: int,
    used_cost_usd: float,
    used_turns: int = 0,
) -> BudgetDecision:
    """Return whether the supplied aggregate usage fits within ``spec``.

    ``used_turns`` is the per-run iteration count and is checked against
    ``spec.max_turns`` -- the loop stop-condition that keeps a run from spinning
    forever ("Ralph Wiggum" stop). It defaults to 0 so existing four-argument
    callers keep their exact behavior; the wall/token/cost caps are unchanged.
    """
    limits: tuple[tuple[str, int | float, int | float | None], ...] = (
        ("wall_ms", used_wall_ms, spec.wall_ms_max),
        ("tokens", used_tokens, spec.tokens_max),
        ("cost_usd", used_cost_usd, spec.cost_usd_max),
        ("turns", used_turns, spec.max_turns),
    )
    for name, used, cap in limits:
        if cap is not None and used > cap:
            return BudgetDecision(
                allowed=False,
                reason=f"{name} {_format_value(name, used)} > cap {_format_value(name, cap)}",
            )
    return BudgetDecision(allowed=True)


def _format_value(name: str, value: int | float) -> str:
    return f"{value:.2f}" if name == "cost_usd" else str(value)
