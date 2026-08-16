"""Usage-section text (contracts/vault-frontmatter.md: run note Usage section
must show tokens/cost WITH the `estimated` flag surfaced in the text — never
just silently embed a number that might be a guess)."""

from __future__ import annotations

from typing import Any

from omniagentos.vault.util import as_bool, pick


def format_usage_lines(run: dict[str, Any]) -> list[str]:
    """Build the Usage section bullet lines for a run note. `run` is expected to
    carry the `AgentUsage`-shaped fields as flattened columns (contracts/schema.sql
    `runs` table: input_tokens, output_tokens, cost_usd, turns, wall_ms,
    usage_estimated, usage_source) — the same names Store rows use.
    """
    input_tokens = pick(run, "input_tokens")
    output_tokens = pick(run, "output_tokens")
    cost_usd = pick(run, "cost_usd")
    turns = pick(run, "turns")
    wall_ms = pick(run, "wall_ms")
    estimated = as_bool(pick(run, "usage_estimated", "estimated", default=True))
    source = pick(run, "usage_source", "source", default="estimator")

    lines: list[str] = []
    if input_tokens is None and output_tokens is None:
        lines.append("**Tokens:** not reported")
    else:
        in_str = "?" if input_tokens is None else str(input_tokens)
        out_str = "?" if output_tokens is None else str(output_tokens)
        lines.append(f"**Tokens:** input={in_str}, output={out_str}")

    if cost_usd is None:
        lines.append("**Cost:** not reported")
    else:
        lines.append(f"**Cost:** ${float(cost_usd):.4f} USD")

    if turns is not None:
        lines.append(f"**Turns:** {turns}")
    if wall_ms is not None:
        lines.append(f"**Wall time:** {wall_ms} ms")

    flag = "**yes**" if estimated else "**no**"
    lines.append(f"**Estimated:** {flag} (source: {source})")
    return lines
