"""capture_skill — turn a verified run/workflow into a reusable skill.

Writes a structured note into `vault/playbook/` (input format, steps, output
structure, validation rules) via `omniagentos.vault.write_note` — the only
sanctioned way to write vault files (contracts/vault-frontmatter.md: "no
other package writes vault files directly; they call p05's generator API").
Optionally mirrors a `SKILL.md` under a skills dir (folder-per-skill, the
"Agent Skills" convention) so a coding agent can load the skill directly.

HARD RULE (self-improving-loop method): `capture_skill` refuses
(`UnverifiedCaptureError`) unless the supplied `VerificationGate.status` is
`GateStatus.PASSED`. This is enforced before any write happens — capturing an
unverified workflow as a "skill" would poison every future run that reuses
it, so there is no partial/best-effort path here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from omniagentos.contracts import NoteType, VaultFrontmatter, utc_now_iso
from omniagentos.selfimprove.errors import UnverifiedCaptureError
from omniagentos.selfimprove.gate import gate_from_status_json
from omniagentos.selfimprove.models import (
    GateStatus,
    SkillCaptureResult,
    SkillMetadata,
    VerificationGate,
)
from omniagentos.selfimprove.paths import (
    ensure_safe_write_target,
    open_no_follow,
    skill_md_path,
    skill_relpath,
)
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.templating import render_template
from omniagentos.vault.write import write_note

if TYPE_CHECKING:
    from omniagentos.knowledge.store import KnowledgeStore


def render_skill_note(metadata: SkillMetadata, gate: VerificationGate) -> tuple[str, str]:
    """Render the `vault/playbook/skill-<id>.md` frontmatter + body. Pure —
    does not touch disk, does not check the gate (callers that need the HARD
    RULE enforced should call `capture_skill`, not this directly). Returns
    `(relpath, content)`."""
    fm = VaultFrontmatter(
        id=metadata.skill_id,
        type=NoteType.PLAYBOOK,
        discipline=metadata.discipline,
        created=utc_now_iso(),
        source_run=gate.source_run_id,
        confidence="high",  # only ever rendered downstream of a PASSED gate
        status="active",
        supersedes=None,
    )
    body = render_template(
        "selfimprove/skill_note.md.j2",
        title=metadata.title,
        discipline_slug=metadata.discipline,
        summary=metadata.summary,
        input_format=metadata.input_format,
        steps=metadata.steps,
        output_structure=metadata.output_structure,
        validation_rules=metadata.validation_rules,
        tags=metadata.tags,
        gate_status=gate.status.value,
        gate_evidence=gate.evidence,
        gate_checked_at=gate.checked_at,
        source_run_id=gate.source_run_id,
    )
    return skill_relpath(metadata.skill_id), render_frontmatter(fm) + "\n" + body


def capture_skill(
    metadata: SkillMetadata,
    gate: VerificationGate,
    vault_dir: str,
    *,
    skills_dir: str | None = None,
    autocommit: bool | None = None,
    capability_store: KnowledgeStore | None = None,
) -> SkillCaptureResult:
    """Capture `metadata` as a reusable skill.

    Writes `vault/playbook/skill-<id>.md` via `omniagentos.vault.write_note`.
    When `skills_dir` is given, also mirrors `<skills_dir>/<id>/SKILL.md`
    (YAML frontmatter `name`/`description` + the same body sections).

    Raises `UnverifiedCaptureError` — and writes nothing — unless
    `gate.status == GateStatus.PASSED` (self-improving-loop HARD RULE).
    """
    # F1: enforce the invariant directly on `gate.status`, never via the
    # `.passed` property — `.passed` is a plain `@property` on a pydantic
    # model, trivially overridable by a `VerificationGate` subclass (e.g.
    # `class Forged(VerificationGate): passed = property(lambda self: True)`),
    # so trusting it here would let a forged gate with `status=FAILED` still
    # authorize a write.
    if gate.status is not GateStatus.PASSED:
        raise UnverifiedCaptureError(
            f"refusing to capture skill {metadata.skill_id!r}: verification gate status "
            f"is {gate.status.value!r}, not 'passed' (self-improving-loop HARD RULE — "
            "only capture after a verified gate)"
        )

    capability_drafts = []
    if metadata.capabilities:
        from omniagentos.knowledge.capabilities import CapabilityDraft

        if capability_store is None:
            raise ValueError("capability_store is required when distilled capabilities are present")
        if not metadata.company_id or not metadata.brand:
            raise ValueError("company_id and brand are required for capability capture")
        # Validate the whole batch before either the skill note or a capability is written.
        capability_drafts = [
            CapabilityDraft.model_validate(item.model_dump()) for item in metadata.capabilities
        ]

    relpath, content = render_skill_note(metadata, gate)
    note_path = write_note(vault_dir, relpath, content, autocommit=autocommit)

    written_skill_md: str | None = None
    if skills_dir is not None:
        written_skill_md = str(_write_skill_md(metadata, gate, skills_dir))

    capability_fact_ids: list[int] = []
    capability_note_paths: list[str] = []
    if capability_drafts:
        from omniagentos.knowledge.capabilities import capture_capabilities

        assert capability_store is not None  # validated before the first write above
        notes = capture_capabilities(
            capability_store,
            capability_drafts,
            run_id=gate.source_run_id or metadata.skill_id,
            brand=metadata.brand or "",
            company_id=metadata.company_id,
            verified_at=gate.checked_at[:10],
            vault_dir=vault_dir,
            autocommit=autocommit,
        )
        capability_fact_ids = [note.fact_id for note in notes if note.fact_id is not None]
        capability_note_paths = [
            str((Path(vault_dir) / "capabilities" / f"{note.id}.md").resolve()) for note in notes
        ]

    return SkillCaptureResult(
        skill_id=metadata.skill_id,
        note_path=note_path,
        skill_md_path=written_skill_md,
        capability_fact_ids=capability_fact_ids,
        capability_note_paths=capability_note_paths,
    )


def capture_skill_from_run_dir(
    run_dir: str,
    metadata: SkillMetadata,
    vault_dir: str,
    *,
    skills_dir: str | None = None,
    autocommit: bool | None = None,
    capability_store: KnowledgeStore | None = None,
) -> SkillCaptureResult:
    """Convenience wrapper: reads the `VerificationGate` from
    `<run_dir>/status.json` (the shared Fusion worker status schema, see
    `omniagentos.selfimprove.gate`) and calls `capture_skill`. Use
    `capture_skill` directly when the gate evidence already lives in memory
    or comes from a differently-shaped verification artifact."""
    gate = gate_from_status_json(run_dir)
    return capture_skill(
        metadata,
        gate,
        vault_dir,
        skills_dir=skills_dir,
        autocommit=autocommit,
        capability_store=capability_store,
    )


def _write_skill_md(metadata: SkillMetadata, gate: VerificationGate, skills_dir: str) -> Path:
    root = Path(skills_dir)
    target = skill_md_path(metadata.skill_id, skills_dir=skills_dir)
    # F3: refuse before writing if a pre-existing symlink (the skill's own
    # directory, or SKILL.md itself) would redirect this write outside
    # skills_dir, then open the leaf with O_NOFOLLOW as a second layer of
    # defense against the same symlink-overwrite scenario.
    ensure_safe_write_target(root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    ensure_safe_write_target(root, target)
    with open_no_follow(target, "w", encoding="utf-8") as handle:
        handle.write(_render_skill_md(metadata, gate))
    return target


def _render_skill_md(metadata: SkillMetadata, gate: VerificationGate) -> str:
    frontmatter = yaml.safe_dump(
        {"name": metadata.skill_id, "description": metadata.summary},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    lines = [
        f"---\n{frontmatter}---",
        "",
        f"# {metadata.title}",
        "",
        metadata.summary,
        "",
        "## Input format",
        "",
        metadata.input_format,
        "",
        "## Steps",
        "",
        *[f"{i}. {step}" for i, step in enumerate(metadata.steps, start=1)],
        "",
        "## Output structure",
        "",
        metadata.output_structure,
        "",
        "## Validation rules",
        "",
        *[f"- {rule}" for rule in metadata.validation_rules],
        "",
        "## Provenance",
        "",
        "- Captured from a **{}** verification gate{}.".format(
            gate.status.value,
            f" (source run `{gate.source_run_id}`)" if gate.source_run_id else "",
        ),
        f"- Gate evidence: {gate.evidence or '_none recorded_'}",
        f"- Gate checked at: {gate.checked_at}",
    ]
    return "\n".join(lines) + "\n"
