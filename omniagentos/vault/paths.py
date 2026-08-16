"""Vault relpath builders (contracts/vault-frontmatter.md layout)."""

from __future__ import annotations

import hashlib
import re

from omniagentos.vault.util import yyyy_mm

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename_id(value: str) -> str:
    """Return a filesystem-safe, collision-resistant representation of an id."""
    original = value
    sanitized = original.replace("..", "-")
    sanitized = re.sub(r"[\\/:\s]+", "-", sanitized)
    sanitized = _UNSAFE_FILENAME_CHARS.sub("-", sanitized)
    sanitized = sanitized.strip(".-") or "note"
    if sanitized == original:
        return sanitized
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
    return f"{sanitized}-{digest}"


def run_note_relpath(run_id: str, created: str) -> str:
    """`runs/<yyyy>/<mm>/<run-id>.md`, filed by the supplied stable note date.

    Run notes use the run's immutable ``finished_at`` (falling back to
    ``created_at``) for this value, so rerendering cannot move a note into a
    different month.
    """
    year, month = yyyy_mm(created)
    return f"runs/{year}/{month}/{_safe_filename_id(run_id)}.md"


def discipline_relpath(slug: str) -> str:
    return f"disciplines/{_safe_filename_id(slug)}.md"


def benchmark_relpath(bench_id: str) -> str:
    return f"benchmarks/{_safe_filename_id(bench_id)}.md"


def reflection_briefing_relpath(date_str: str) -> str:
    """``briefings/reflection-<YYYY-MM-DD>.md`` — the nightly reflection briefing.

    The ONE spelling, shared by the writer (``reflection.report``) and every
    reader (``reflection.watchdog``, the health sentinel). A briefing the loop
    wrote and the watchdog cannot find is scored as a failed night, so the name
    and the vault root both have to come from a single place: the root from
    ``contracts.default_vault_dir()``, the relpath from here.
    """
    return f"briefings/reflection-{_safe_filename_id(date_str)}.md"


def reflection_alert_relpath(date_str: str) -> str:
    """``briefings/reflection-ALERT-<YYYY-MM-DD>.md`` — the watchdog's failure report.

    Filed beside the briefing it is complaining about (same vault, same
    directory) so an operator reading one always sees the other.
    """
    return f"briefings/reflection-ALERT-{_safe_filename_id(date_str)}.md"


def swarm_relpath(run_id: str) -> str:
    """``swarm/<run-id>.md`` (WP7 run-summary note — flat, not date-bucketed:
    a swarm run id is already unique and operators look it up by run id, not
    by month)."""
    return f"swarm/{_safe_filename_id(run_id)}.md"
