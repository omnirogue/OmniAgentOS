"""render_experiment_note (contracts/lab-interfaces.md §L08-labvault).

`exp: dict[str, Any]` carries a champion-vs-challenger Experiment; it is read
defensively (H1's `pick`) so it renders whether the caller hands a raw
LabStore `experiments` row (contracts/schema.sql — `*_json` columns are
JSON-encoded strings) or an `Experiment.model_dump(mode="json")` (nested
dicts already decoded). `results` is the matching list of `eval_results`
rows/dumps (one per arm x split x replicate). `scorecard` is the Section 11.2
Scorecard dict — passed separately (rather than read from
`exp["scorecard_json"]`) because a PROPOSED/RUNNING experiment has no
scorecard yet; callers pass `{}` in that case.

Wikilinks (contracts/lab-interfaces.md: "Notes wikilink experiments<->
tournaments<->surfaces<->leaderboard<->playbook"): `[[<champion_surface_id>]]`
and `[[<challenger_surface_id>]]` (both resolve once `render_prompt_note` — or
a future genome note — has been written for that surface id), `[[<discipline>]]`,
and `[[Home]]`.

Reward-hacking invariant (Section 11.7): `eval_results` structurally never
carries a held-out `expected` value (contracts/schema.sql has no such column
there), but every metrics/per_case mapping is scrubbed anyway
(`omniagentos.lab.vault.util.scrub_held_out`) as defense-in-depth — this note
must NEVER be the leak path, and the "Scorecard" section always surfaces
`audit_flags` (a non-empty list forces HUMAN_REVIEW regardless of the numeric
gates — Section 11.5).
"""

from __future__ import annotations

from typing import Any

from omniagentos.contracts import NoteType, VaultFrontmatter, utc_now_iso
from omniagentos.lab.vault.paths import experiment_relpath
from omniagentos.lab.vault.util import fmt_metrics, maybe_json, scrub_held_out
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.templating import render_template
from omniagentos.vault.util import as_optional_str, as_str, pick

NOT_SET = "_not set_"

_ARM_ORDER = {"champion": 0, "challenger": 1}
_SPLIT_ORDER = {"dev": 0, "held_out": 1}


def render_experiment_note(
    exp: dict[str, Any],
    results: list[dict[str, Any]],
    scorecard: dict[str, Any],
) -> tuple[str, str]:
    """Render an experiment note. Returns (relpath, full content incl.
    frontmatter). Shows the champion-vs-challenger scorecard (incl.
    `audit_flags`) and the per-arm eval results. NEVER renders a held-out
    `expected` value.
    """
    exp_id = as_str(pick(exp, "id"), default="exp_unknown")
    discipline = as_optional_str(pick(exp, "discipline"))
    note_date = as_str(pick(exp, "updated_at", "created_at"), default=utc_now_iso())

    fm = VaultFrontmatter(
        id=exp_id,
        type=NoteType.EXPERIMENT,
        discipline=discipline,
        created=note_date,
        source_run=None,
        confidence=None,
        status="active",
        supersedes=None,
    )

    card = scrub_held_out(maybe_json(scorecard) or {})
    if not isinstance(card, dict):
        card = {}

    result_rows = sorted((_result_row(r) for r in results), key=_result_sort_key)

    body = render_template(
        "lab/experiment_note.md.j2",
        title=as_str(pick(exp, "hypothesis"), default=exp_id),
        discipline=discipline,
        status=as_str(pick(exp, "status"), default=NOT_SET),
        disposition=as_optional_str(pick(exp, "disposition")),
        explore_policy=as_str(pick(exp, "explore_policy"), default=NOT_SET),
        mutable_surface_kind=as_str(pick(exp, "mutable_surface_kind"), default=NOT_SET),
        champion_surface_id=as_optional_str(pick(exp, "champion_surface_id")),
        challenger_surface_id=as_optional_str(pick(exp, "challenger_surface_id")),
        eval_suite_id=as_str(pick(exp, "eval_suite_id"), default=NOT_SET),
        primary_metric=as_optional_str(pick(exp, "primary_metric")),
        dataset_hash=as_optional_str(pick(exp, "dataset_hash")),
        snapshot_hash=as_optional_str(pick(exp, "snapshot_hash")),
        created_at=as_str(pick(exp, "created_at"), default=NOT_SET),
        updated_at=as_str(pick(exp, "updated_at"), default=NOT_SET),
        champion_metrics=fmt_metrics(card.get("champion")),
        challenger_metrics=fmt_metrics(card.get("challenger")),
        primary_delta=_fmt_optional_number(card.get("primary_delta")),
        utility=_fmt_optional_number(card.get("utility")),
        complexity_delta=_fmt_optional_number(card.get("complexity_delta")),
        confidence_interval=card.get("confidence_interval") or None,
        safety_regression=bool(card.get("safety_regression", False)),
        audit_flags=[str(flag) for flag in (card.get("audit_flags") or [])],
        has_scorecard=bool(card),
        results=result_rows,
    )

    relpath = experiment_relpath(exp_id)
    return relpath, render_frontmatter(fm) + "\n" + body


def _result_row(r: dict[str, Any]) -> dict[str, Any]:
    per_case = maybe_json(pick(r, "per_case_json", "per_case", default={}))
    case_count = len(per_case) if isinstance(per_case, dict) else 0
    return {
        "arm": as_str(pick(r, "arm"), default="?"),
        "split": as_str(pick(r, "split"), default="?"),
        "replicate": pick(r, "replicate", default=0),
        "suite_version": pick(r, "suite_version", default="?"),
        "deterministic_passed": bool(pick(r, "deterministic_passed", default=True)),
        "metrics": fmt_metrics(pick(r, "metrics_json", "metrics", default={})),
        "cases": case_count,
        "created_at": as_str(pick(r, "created_at"), default="?"),
    }


def _result_sort_key(row: dict[str, Any]) -> tuple[int, int, int]:
    replicate = row["replicate"] if isinstance(row["replicate"], int) else 0
    return (
        _ARM_ORDER.get(str(row["arm"]), 2),
        _SPLIT_ORDER.get(str(row["split"]), 2),
        replicate,
    )


def _fmt_optional_number(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)
