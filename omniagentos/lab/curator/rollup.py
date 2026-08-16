"""Pure, deterministic summarization helpers used by `curate()`.

No I/O beyond what is explicitly passed in (manifests/rows) except
`scan_vault_context`, which does a best-effort, read-only walk of an existing
vault directory using H1's own frontmatter parser -- never a lab-specific
path, so it degrades to an empty list before L08/L09 exist or when
`vault_dir` is missing/empty, exactly the shape the ``--dry-run`` acceptance
test exercises.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from omniagentos.contracts import RunManifest
from omniagentos.vault import VaultError, parse_frontmatter

# Note types the curator cares about as extra context (contracts.py NoteType):
# the disciplines a run/experiment stub belongs to, or a collab conversation
# note type are deliberately NOT included here -- they are not yet part of the
# frozen NoteType enum, so this scan stays generic/type-driven rather than
# guessing a folder layout L08/L09 have not landed yet.
_VAULT_CONTEXT_TYPES = ("tournament", "experiment", "leaderboard", "playbook")


def _value(value: object) -> str:
    return str(getattr(value, "value", value))


def summarize_runs(manifests: list[RunManifest]) -> dict[str, Any]:
    """Rollup of the run ledger, grouped by state + discipline (the curator
    cares about disciplines/subjects, not the baseline-ladder arm)."""
    states: Counter[str] = Counter()
    disciplines: Counter[str] = Counter()
    cost = 0.0
    tokens = 0
    for manifest in manifests:
        states[_value(manifest.state)] += 1
        disciplines[manifest.discipline or "(none)"] += 1
        if manifest.usage is not None:
            cost += manifest.usage.cost_usd or 0.0
            tokens += (manifest.usage.input_tokens or 0) + (manifest.usage.output_tokens or 0)
    return {
        "count": len(manifests),
        "by_state": dict(sorted(states.items())),
        "by_discipline": dict(sorted(disciplines.items())),
        "total_est_cost_usd": cost,
        "total_est_tokens": tokens,
    }


def summarize_experiments(experiments: list[dict[str, Any]]) -> dict[str, Any]:
    """Rollup of the experiment ledger (`LabStore.list_experiments`)."""
    status: Counter[str] = Counter()
    disposition: Counter[str] = Counter()
    disciplines: Counter[str] = Counter()
    for experiment in experiments:
        status[str(experiment.get("status") or "(none)")] += 1
        disciplines[str(experiment.get("discipline") or "(none)")] += 1
        if experiment.get("disposition"):
            disposition[str(experiment["disposition"])] += 1
    return {
        "count": len(experiments),
        "by_status": dict(sorted(status.items())),
        "by_disposition": dict(sorted(disposition.items())),
        "by_discipline": dict(sorted(disciplines.items())),
    }


def summarize_playbook(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Rollup of the validated-traits playbook (`LabStore.list_playbook`)."""
    disciplines: Counter[str] = Counter()
    confidence: Counter[str] = Counter()
    for entry in entries:
        disciplines[str(entry.get("discipline") or "(none)")] += 1
        confidence[str(entry.get("confidence") or "(none)")] += 1
    return {
        "count": len(entries),
        "by_discipline": dict(sorted(disciplines.items())),
        "by_confidence": dict(sorted(confidence.items())),
    }


def scan_vault_context(vault_dir: str, *, limit: int = 20) -> list[dict[str, str | None]]:
    """Best-effort excerpt of existing vault notes relevant to curation
    (tournament/experiment/leaderboard/playbook types), used as extra
    `curation_prompt` context ("+ relevant vault/collab files"). Returns `[]`
    for a missing/empty `vault_dir` -- L08/L09 are lazy dependencies this
    function does not require to exist yet, and a file with no/invalid
    frontmatter (e.g. a plain README) is silently skipped rather than raising."""
    root = Path(vault_dir)
    if not root.is_dir():
        return []
    found: list[dict[str, str | None]] = []
    for path in sorted(root.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
            frontmatter = parse_frontmatter(text)
        except (VaultError, OSError, UnicodeDecodeError):
            continue
        if frontmatter.type.value in _VAULT_CONTEXT_TYPES:
            found.append(
                {
                    "path": str(path.relative_to(root)),
                    "id": frontmatter.id,
                    "type": frontmatter.type.value,
                    "discipline": frontmatter.discipline,
                }
            )
            if len(found) >= limit:
                break
    return found
