"""Extract PROVIDER-REPORTED usage from a parsed CLI response.

Deliberately narrow. ``adapters.common.estimated_usage`` will happily produce a
token count from a character-count heuristic when the CLI says nothing, and that
is the right behavior for budget guards -- but it is the wrong thing to write
into the effort dataset, where the whole point is comparing measured spend
across effort levels. An estimate recorded beside a measurement, indistinguishable
after the fact, would quietly answer the question wrong.

So: return numbers only when the provider actually reported them, and say which
provider reports what rather than pretending uniform coverage.

  claude  `claude -p --output-format json` reports usage.input_tokens /
          usage.output_tokens and total_cost_usd -- a complete measurement
          (SOURCE_CLI_REPORT). See adapters/claude.py:_usage.
  codex   `codex exec --json` reports exact token counts on turn.completed
          (input_tokens, output_tokens, cached_input_tokens,
          cache_write_input_tokens, reasoning_output_tokens) but never a cost
          field. Tokens are captured; cost stays None; source is
          SOURCE_TOKENS_ONLY. SessionsDal.record_session_usage clears any
          creation-time cost seed to SQL NULL for that source so the row cannot
          look like a measured $0.00.
  grok    reports both token counts and cost when the CLI emits them (live
          sessions carry non-zero cost_usd under SOURCE_CLI_REPORT).

A NULL here is honest missing data. A zero would be a claim. After migration
088, sessions.cost_usd is nullable; tokens-only writes force NULL, while an
explicit total_cost_usd / cost_usd of 0.0 remains a genuine free report under
SOURCE_CLI_REPORT. Swarm dollar caps read the joined live ledger and treat any
tokens-only session as explicitly unenforceable, including legacy rows whose
creation-time 0.0 seed predates the nullable invariant.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

# Written into `usage_source` so an analysis can filter to evidence only.
SOURCE_CLI_REPORT = "cli-report"
SOURCE_TOKENS_ONLY = "cli-report-tokens-only"
SOURCE_NONE = "none"

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReportedUsage:
    """What the provider itself said it spent. Any field may be None."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    wall_ms: int | None = None
    source: str = SOURCE_NONE
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None

    @property
    def has_tokens(self) -> bool:
        return self.input_tokens is not None or self.output_tokens is not None


def _payload_of(parsed: Any) -> dict[str, Any]:
    payload = getattr(parsed, "payload", None)
    return payload if isinstance(payload, dict) else {}


def _int_or_none(value: Any) -> int | None:
    # bool is an int subclass; a stray True must not become 1 token.
    # Negative counts are not valid provider usage measurements.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        # Reject NaN, ±Infinity, and negatives — not valid usage measurements.
        if not math.isfinite(result) or result < 0:
            return None
        return result
    return None


def extract(
    parsed: Any,
    *,
    wall_ms: int | None = None,
    model: str | None = None,
) -> ReportedUsage:
    """Provider-reported usage from an adapter's parsed response.

    Never raises: a malformed or unfamiliar payload yields an all-None record
    with ``source='none'``, because failing to record telemetry must not fail
    the run that produced it.
    """
    payload = _payload_of(parsed)
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    input_tokens = _int_or_none(usage.get("input_tokens"))
    output_tokens = _int_or_none(usage.get("output_tokens"))
    cached_input_tokens = _int_or_none(usage.get("cached_input_tokens"))
    cache_write_input_tokens = _int_or_none(usage.get("cache_write_input_tokens"))
    cost = _float_or_none(payload.get("total_cost_usd"))
    if cost is None:
        cost = _float_or_none(usage.get("cost_usd"))

    has_tokens = input_tokens is not None or output_tokens is not None
    # Explicit 0.0 is a real report ("cost nothing"); only None is missing.
    if cost is not None:
        source = SOURCE_CLI_REPORT
    elif has_tokens:
        source = SOURCE_TOKENS_ONLY
    else:
        source = SOURCE_NONE

    if source == SOURCE_TOKENS_ONLY:
        try:
            model_part = f" model={model}" if model else ""
            LOG.warning(
                "provider reported tokens but no cost%s: input_tokens=%s "
                "output_tokens=%s cached_input_tokens=%s "
                "cache_write_input_tokens=%s; cost_usd left unknown (NULL), "
                "never a silent $0.00 — no pricing table is applied here",
                model_part,
                input_tokens,
                output_tokens,
                cached_input_tokens,
                cache_write_input_tokens,
            )
        except Exception:  # noqa: BLE001 -- telemetry path must never fail the run
            pass

    return ReportedUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        wall_ms=wall_ms,
        source=source,
        cached_input_tokens=cached_input_tokens,
        cache_write_input_tokens=cache_write_input_tokens,
    )


__all__ = [
    "ReportedUsage",
    "SOURCE_CLI_REPORT",
    "SOURCE_NONE",
    "SOURCE_TOKENS_ONLY",
    "extract",
]
