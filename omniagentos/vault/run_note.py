"""render_run_note (contracts/interfaces.md §p05, contracts/vault-frontmatter.md).

`run: dict` / `steps: list[dict]` are intentionally untyped (unlike
VaultFrontmatter/RunManifest/IdempotencyReceipt, which are frozen pydantic
models). They carry the `Store` row shape (contracts/schema.sql `runs` /
`steps` tables — the same dicts `Store.get_run` / `Store.get_steps` return).
The optional ``task_title``, ``task_input``, and ``artifacts_list`` parameters
carry related task/artifact data absent from a real run row. Every field is
read defensively (`util.pick`) so a partial/manually-built dict renders a
readable note instead of crashing.
"""

from __future__ import annotations

import json
from typing import Any

from omniagentos.contracts import IdempotencyReceipt, NoteType, VaultFrontmatter, utc_now_iso
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.paths import run_note_relpath
from omniagentos.vault.templating import render_template
from omniagentos.vault.usage import format_usage_lines
from omniagentos.vault.util import as_optional_str, as_str, pick

NOT_RECORDED = "_not recorded_"


def render_run_note(
    run: dict[str, Any],
    steps: list[dict[str, Any]],
    manifest_path: str,
    receipts: list[IdempotencyReceipt],
    *,
    artifacts_list: list[dict[str, Any]] | None = None,
    task_title: str | None = None,
    task_input: str | None = None,
) -> tuple[str, str]:
    """Render a run note. Returns (relpath, full note content incl. frontmatter).

    Required wikilinks (D-011): `[[<discipline-slug>]]` (when `run` carries a
    discipline) and `[[Home]]`. Callers with a Store should pass the related
    task's title and input, plus artifacts fetched through ``get_artifacts``;
    those fields do not live on a real ``runs`` row. The task title is plain
    text (no tasks/ folder in H1).
    """
    run_id = as_str(pick(run, "id", "run_id"), default="run_unknown")
    task_id = as_str(pick(run, "task_id"))
    title = task_title if task_title is not None else as_str(
        pick(run, "task_title", "title"), default=task_id or run_id
    )
    discipline_slug = as_optional_str(pick(run, "discipline", "discipline_id"))
    note_date = as_str(pick(run, "finished_at", "created_at"), default=utc_now_iso())

    fm = VaultFrontmatter(
        id=run_id,
        type=NoteType.RUN,
        discipline=discipline_slug,
        created=note_date,
        source_run=run_id,  # the run note's own run produced it (contract example)
        confidence=None,
        status="active",
        supersedes=None,
    )

    step_rows = [
        {
            "seq": pick(step, "seq", default="?"),
            "name": as_str(pick(step, "name"), default="?"),
            "status": as_str(pick(step, "status"), default="?"),
            "started_at": as_str(pick(step, "started_at"), default=NOT_RECORDED),
            "finished_at": as_str(pick(step, "finished_at"), default=NOT_RECORDED),
        }
        for step in steps
    ]
    receipt_rows = [
        {
            "key": receipt.key,
            "step_name": receipt.step_name,
            "created_at": receipt.created_at,
            "completed_at": receipt.completed_at,
        }
        for receipt in receipts
    ]
    artifact_values = (
        artifacts_list
        if artifacts_list is not None
        else pick(run, "artifacts", "artifact_paths", default=[])
    )
    artifacts = [
        as_str(pick(artifact, "uri", "path") if isinstance(artifact, dict) else artifact)
        for artifact in artifact_values
    ]
    input_value = task_input if task_input is not None else pick(run, "input_json", "prompt")

    body = render_template(
        "run_note.md.j2",
        title=title,
        discipline_slug=discipline_slug,
        state=as_str(pick(run, "state"), default=NOT_RECORDED),
        harness=as_str(pick(run, "harness"), default=NOT_RECORDED),
        arm=as_optional_str(pick(run, "arm")),
        model=as_str(pick(run, "model"), default=NOT_RECORDED),
        queued_at=as_str(pick(run, "queued_at"), default=NOT_RECORDED),
        started_at=as_str(pick(run, "started_at"), default=NOT_RECORDED),
        finished_at=as_str(pick(run, "finished_at"), default=NOT_RECORDED),
        usage_lines=format_usage_lines(run),
        output_text=as_optional_str(pick(run, "output_text")),
        output_json=_compact_json(pick(run, "output_json")),
        task_input=_compact_json(input_value),
        steps=step_rows,
        artifacts=artifacts,
        task_id=task_id or NOT_RECORDED,
        trace_id=as_str(pick(run, "trace_id"), default=NOT_RECORDED),
        manifest_path=manifest_path or NOT_RECORDED,
        receipts=receipt_rows,
    )

    relpath = run_note_relpath(run_id, note_date)
    return relpath, render_frontmatter(fm) + "\n" + body


def _compact_json(value: Any) -> str | None:
    """Return JSON in a stable compact form, preserving non-JSON text."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return json.dumps(json.loads(text), separators=(",", ":"), sort_keys=True)
    except json.JSONDecodeError:
        return text
