"""Stage 1 — the planner writes the spec.

:func:`plan_to_spec` runs the EXISTING planner (``intake.planner.plan_goal``, Fable
high/max) to produce a :class:`ProjectPlan`, then renders it as a markdown spec/plan
doc and writes it to the vault (reusing ``vault.write_note``) and, when a scoped
project dir is given, drops a copy there. That written doc is the spec injected into
every executor prompt downstream.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from omniagentos.contracts import NoteType, VaultFrontmatter, utc_now_iso
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.paths import _safe_filename_id

if TYPE_CHECKING:
    from omniagentos.intake.planner import PlannedSubProject, PlannedTask, ProjectPlan

LOG = logging.getLogger(__name__)

# The relative name of the spec copy dropped in a scoped project dir.
SCOPED_SPEC_FILENAME = "ORCHESTRATION_SPEC.md"

# A vault writer seam: (vault_dir, relpath, content) -> abs path. Defaults to
# vault.write_note; tests inject a recorder.
VaultWriter = Callable[[str, str, str], str]


def _render_task(task: PlannedTask, *, index: int, prefix: str = "") -> str:
    lines = [f"### {prefix}{index}. {task.title}"]
    if task.description:
        lines.append(task.description)
    if task.acceptance_criteria:
        lines.append("")
        lines.append("**Acceptance criteria:**")
        lines.extend(f"- {c}" for c in task.acceptance_criteria)
    if task.suggested_discipline:
        lines.append("")
        lines.append(f"_Suggested discipline:_ {task.suggested_discipline}")
    return "\n".join(lines)


def render_spec_markdown(plan: ProjectPlan, goal: str, run_id: str) -> str:
    """Render the plan as a self-contained spec/plan doc (with vault frontmatter)."""
    fm = VaultFrontmatter(
        id=f"orchestration-{_safe_filename_id(run_id)}",
        type=NoteType.BRIEFING,
        created=utc_now_iso(),
        source_run=run_id,
        status="active",
    )
    body: list[str] = [
        f"# Spec: {plan.project_name}",
        "",
        f"> Orchestration run `{run_id}` — planner-written spec (worker-as-planner).",
        "",
        "## Goal",
        goal.strip() or "(none)",
        "",
        "## Plan",
        f"- **Project:** {plan.project_name}",
        f"- **Complexity:** {plan.complexity}",
        f"- **Planner effort:** {plan.effort}",
    ]
    if plan.description:
        body += ["", plan.description]

    if plan.tasks:
        body += ["", "## Tasks"]
        for i, task in enumerate(plan.tasks, start=1):
            body += ["", _render_task(task, index=i)]

    for sp in plan.sub_projects:
        body += ["", f"## Sub-project: {sp.name}"]
        if sp.description:
            body += ["", sp.description]
        body += _render_subproject_tasks(sp)

    return render_frontmatter(fm) + "\n" + "\n".join(body) + "\n"


def _render_subproject_tasks(sp: PlannedSubProject) -> list[str]:
    out: list[str] = []
    for i, task in enumerate(sp.tasks, start=1):
        out += ["", _render_task(task, index=i, prefix=f"{sp.name} · ")]
    return out


def write_spec_doc(
    plan: ProjectPlan,
    goal: str,
    run_id: str,
    *,
    vault_dir: str,
    working_dir: str | None = None,
    writer: VaultWriter | None = None,
) -> str | None:
    """Write the rendered spec to the vault (and a scoped-dir copy), returning its path.

    Never raises: a vault/disk fault degrades to ``None`` so the orchestration still
    proceeds (the spec is also injected into prompts from the in-memory render).
    """
    content = render_spec_markdown(plan, goal, run_id)
    relpath = f"orchestration/{_safe_filename_id(run_id)}.md"
    write = writer or _default_vault_writer
    path: str | None = None
    try:
        path = write(vault_dir, relpath, content)
    except Exception:  # noqa: BLE001 -- spec persistence is best-effort.
        LOG.debug("orchestrator could not write spec note", exc_info=True)

    if working_dir:
        try:
            target = Path(working_dir) / SCOPED_SPEC_FILENAME
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError:
            LOG.debug("orchestrator could not write scoped spec copy", exc_info=True)
    return path


def _default_vault_writer(vault_dir: str, relpath: str, content: str) -> str:
    from omniagentos.vault import write_note

    return write_note(vault_dir, relpath, content)


__all__ = ["render_spec_markdown", "write_spec_doc", "SCOPED_SPEC_FILENAME"]
