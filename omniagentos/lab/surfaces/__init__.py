"""Immutable versioning and guarded champion promotion for lab surfaces."""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path, PurePath
from typing import Any

from omniagentos.contracts import digest, new_id
from omniagentos.lab.contracts import (
    ChampionEntry,
    Disposition,
    ExperimentStatus,
    GenomeSpec,
    Surface,
    SurfaceKind,
    SurfaceStatus,
    surface_requires_human,
)
from omniagentos.path_containment import inode_relative_parts_anchored

OPERATOR_TOKEN_ENV = "OMNIAGENTOS_OPERATOR_TOKEN"


def _repository_root() -> Path:
    """Return the checkout root, independent of the caller's working directory."""
    return Path(__file__).resolve().parents[3]


def _relative_surface_path(path: str) -> Path:
    """Validate a persisted surface path before resolving it in this checkout."""
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"surface path must be a safe relative path: {path!r}")
    return relative


def _surface_file(path: str) -> Path:
    root = _repository_root().resolve()
    candidate = (root / _relative_surface_path(path)).resolve()
    if inode_relative_parts_anchored(candidate, root) is None:
        raise ValueError(f"surface path escapes repository: {path!r}")
    return candidate


def _role_path(role: str) -> str:
    """Accept one role name, never a path supplied by a caller."""
    component = PurePath(role)
    if not role or "/" in role or "\\" in role or component.name != role or role in {".", ".."}:
        raise ValueError(f"role must be a single path component: {role!r}")
    return role


def _next_version(store: Any, discipline: str, kind: SurfaceKind, *, path_prefix: str = "") -> int:
    rows = store.list_surfaces(discipline, kind.value)
    if path_prefix:
        rows = [row for row in rows if str(row["path"]).startswith(path_prefix)]
    return max((int(row["version"]) for row in rows), default=0) + 1


def _surface_kind(surface: dict[str, Any]) -> SurfaceKind:
    return SurfaceKind(str(surface["kind"]))


def _require_surface(store: Any, surface_id: str) -> dict[str, Any]:
    surface = store.get_surface(surface_id)
    if surface is None:
        raise ValueError(f"unknown surface: {surface_id}")
    return surface


def _require_operator_token(operator_token: str | None) -> None:
    """Require minimal, fail-closed operator auth for a safety action.

    H2 has no user session/authentication system, so a server-only token is the
    minimal stand-in for a human with server access approving a consequential
    champion change. Candidate automation has this variable scrubbed from its
    subprocess env, but that scrub is NOT the security boundary: the long-lived
    server process still holds the token in its own environment, which a same-uid
    process can read via ``ps -Eeww`` / ``/proc/<pid>/environ``. Exactly like the
    held-out encryption key (see eval/_crypto.py), this gate is only fully sound
    once candidates are OS/process-isolated (mount namespace / container /
    separate uid) — an H3 boundary, NOT claimed here. It closes the demonstrated
    single-POST ``decided_by='operator'`` bypass, not same-uid process inspection.
    If the server has no token configured, safety-surface promotion and rollback
    are disabled entirely (fail-closed).
    """
    server_token = os.environ.get(OPERATOR_TOKEN_ENV)
    if not server_token:
        raise ValueError(
            "safety-surface promotion is disabled: OMNIAGENTOS_OPERATOR_TOKEN is "
            "not configured on the server (fail-closed; no human approval is "
            "possible without it)"
        )
    if not operator_token or not hmac.compare_digest(operator_token, server_token):
        raise ValueError("operator token missing or invalid for this action")


def _require_human_identity(store: Any, identity: str | None, operator_token: str | None) -> None:
    """Require both a human audit label and the server operator token.

    The identity remains an audit label, not a credential: a bare request-body
    ``decided_by='operator'`` previously bypassed the human checkpoint. Token
    authentication and the identity-shape check are independently required.
    """
    if not identity:
        raise ValueError("human_decided_by is required for this surface")
    if identity != "operator":
        # L09's registry is in the same SQLite database. A registered automation
        # lineage is not a human sign-off; only explicit human/operator lineages
        # are accepted here.
        try:
            lineage = store.get_agent_lineage(identity)
        except (AttributeError, RuntimeError) as exc:  # pragma: no cover - alternate stores.
            raise ValueError("human_decided_by must be 'operator' or a registered human") from exc
        if lineage is None or lineage.lower() not in {"human", "operator"}:
            raise ValueError("human_decided_by must be 'operator' or a registered human")
    _require_operator_token(operator_token)


def version_prompt(
    store: Any,
    discipline: str,
    role: str,
    content: str,
    *,
    parent_version: int | None = None,
) -> Surface:
    """Create an immutable prompt surface and persist its exact text."""
    role = _role_path(role)
    prefix = f"vault/prompts/{role}/v"
    version = _next_version(store, discipline, SurfaceKind.PROMPT, path_prefix=prefix)
    path = f"vault/prompts/{role}/v{version:02d}.md"
    target = _surface_file(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    surface = Surface(
        kind=SurfaceKind.PROMPT,
        discipline=discipline,
        version=version,
        path=path,
        content_hash=digest(content),
        parent_version=parent_version,
        label=role,
    )
    store.create_surface(surface)
    return surface


def version_genome(
    store: Any,
    discipline: str,
    genome: dict[str, Any],
    *,
    parent_version: int | None = None,
) -> Surface:
    """Create a genome surface whose config id is exactly its surface id."""
    surface_id = new_id("srf")
    normalized = dict(genome)
    normalized["id"] = surface_id
    validated = GenomeSpec.model_validate(normalized)
    # Validate the shape without materializing Pydantic defaults: a caller's
    # genome must round-trip exactly, apart from the required surface-id binding.
    content = json.dumps(normalized, indent=2, sort_keys=True) + "\n"
    version = _next_version(store, discipline, SurfaceKind.ORCHESTRATION_GENOME)
    path = f"configs/genomes/{surface_id}.json"
    target = _surface_file(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    surface = Surface(
        id=surface_id,
        kind=SurfaceKind.ORCHESTRATION_GENOME,
        discipline=discipline,
        version=version,
        path=path,
        content_hash=digest(content),
        parent_version=parent_version,
        label=validated.archetype,
    )
    store.create_surface(surface)
    return surface


def load_surface_content(surface: dict[str, Any]) -> str | dict[str, Any]:
    """Read the immutable prompt text or validate and read a genome document."""
    kind = _surface_kind(surface)
    path = str(surface["path"])
    if kind == SurfaceKind.PROMPT:
        if not path.startswith("vault/prompts/"):
            raise ValueError(f"prompt surface has invalid path: {path!r}")
        return _surface_file(path).read_text(encoding="utf-8")
    if kind == SurfaceKind.ORCHESTRATION_GENOME:
        if not path.startswith("configs/genomes/"):
            raise ValueError(f"genome surface has invalid path: {path!r}")
        genome = json.loads(_surface_file(path).read_text(encoding="utf-8"))
        validated = GenomeSpec.model_validate(genome)
        if validated.id != str(surface["id"]):
            raise ValueError("genome id must equal its surface id")
        return genome
    raise ValueError(f"surface content is unsupported for kind: {kind.value}")


def seed_champion(store: Any, discipline: str, kind: SurfaceKind, surface_id: str) -> ChampionEntry:
    """Install champion #0.  A seed deliberately has no rollback target."""
    surface = _require_surface(store, surface_id)
    if _surface_kind(surface) != kind or surface["discipline"] != discipline:
        raise ValueError("seed surface must match the champion discipline and kind")
    if surface["status"] == SurfaceStatus.ARCHIVED.value:
        raise ValueError("an archived surface cannot be seeded as champion")
    if store.get_champion(discipline, kind.value) is not None:
        raise ValueError("a champion already exists for this discipline and kind")

    entry = ChampionEntry(
        discipline=discipline,
        surface_kind=kind,
        surface_id=surface_id,
        surface_version=int(surface["version"]),
    )
    store.set_champion_with_statuses(
        entry,
        surface_statuses={surface_id: SurfaceStatus.CHAMPION.value},
    )
    return entry.model_copy(update={"cas_version": 1})


def promote(
    store: Any,
    exp_id: str,
    challenger_surface_id: str,
    *,
    human_decided_by: str | None = None,
    operator_token: str | None = None,
) -> ChampionEntry:
    """Promote a passed challenger without ever overwriting a stale champion pointer."""
    experiment = store.get_experiment(exp_id)
    if experiment is None:
        raise ValueError(f"unknown experiment: {exp_id}")
    challenger = _require_surface(store, challenger_surface_id)
    challenger_kind = _surface_kind(challenger)
    requires_human = surface_requires_human(challenger_kind, bool(challenger["safety_relevant"]))

    if experiment["status"] != ExperimentStatus.DECIDED.value:
        raise ValueError("experiment must be decided before promotion")
    if experiment["challenger_surface_id"] != challenger_surface_id:
        raise ValueError("challenger surface does not belong to this experiment")
    if experiment["discipline"] != challenger["discipline"]:
        raise ValueError("challenger discipline does not match the experiment")
    if experiment["mutable_surface_kind"] != challenger_kind.value:
        raise ValueError("challenger kind does not match the experiment")
    if challenger["status"] == SurfaceStatus.ARCHIVED.value:
        raise ValueError("an archived surface cannot be promoted")

    scorecard = experiment.get("scorecard_json")
    if isinstance(scorecard, str):
        try:
            scorecard = json.loads(scorecard)
        except json.JSONDecodeError as exc:
            raise ValueError("experiment scorecard is invalid") from exc
    if isinstance(scorecard, dict) and scorecard.get("audit_flags"):
        raise ValueError("challenger has unresolved audit flags")

    disposition = experiment["disposition"]
    if requires_human:
        # A human gate is visible in the experiment record even after the human
        # approves the promotion; the schema has no separate approval table.
        if disposition == Disposition.PROMOTE.value:
            store.update_experiment(exp_id, {"disposition": Disposition.HUMAN_REVIEW})
        _require_human_identity(store, human_decided_by, operator_token)
        if disposition not in {Disposition.PROMOTE.value, Disposition.HUMAN_REVIEW.value}:
            raise ValueError("experiment did not pass the promotion gate")
    elif disposition != Disposition.PROMOTE.value:
        raise ValueError("experiment did not pass the promotion gate")

    current = store.get_champion(challenger["discipline"], challenger_kind.value)
    if current is None:
        raise ValueError("cannot promote without an outgoing champion")
    outgoing = _require_surface(store, str(current["surface_id"]))
    if (
        _surface_kind(outgoing) != challenger_kind
        or outgoing["discipline"] != challenger["discipline"]
    ):
        raise ValueError("outgoing champion does not match challenger scope")
    if outgoing["status"] == SurfaceStatus.ARCHIVED.value:
        raise ValueError("outgoing champion is already archived")

    entry = ChampionEntry(
        discipline=str(challenger["discipline"]),
        surface_kind=challenger_kind,
        surface_id=challenger_surface_id,
        surface_version=int(challenger["version"]),
        promoted_from_experiment=exp_id,
        rollback_to_surface_id=str(outgoing["id"]),
        cas_version=int(current["cas_version"]),
    )
    store.set_champion_with_statuses(
        entry,
        surface_statuses={
            str(outgoing["id"]): SurfaceStatus.ARCHIVED.value,
            challenger_surface_id: SurfaceStatus.CHAMPION.value,
        },
    )
    return entry.model_copy(update={"cas_version": entry.cas_version + 1})


def rollback(
    store: Any,
    discipline: str,
    kind: SurfaceKind,
    *,
    operator_token: str | None = None,
) -> ChampionEntry:
    """Restore the prior champion through CAS, authenticating safety changes.

    The server operator token is required whenever either the surface restored
    to or the outgoing surface is safety-relevant. Rollback has no identity
    label today, so the token alone is its authentication bar.
    """
    current = store.get_champion(discipline, kind.value)
    if current is None:
        raise ValueError("cannot roll back without a champion")
    rollback_to = current["rollback_to_surface_id"]
    if rollback_to is None:
        raise ValueError("seed champions have no rollback target")

    target = _require_surface(store, str(rollback_to))
    if _surface_kind(target) != kind or target["discipline"] != discipline:
        raise ValueError("rollback target does not match the champion scope")
    if target["status"] != SurfaceStatus.ARCHIVED.value:
        raise ValueError("rollback target must be an archived immutable surface")
    outgoing = _require_surface(store, str(current["surface_id"]))

    if surface_requires_human(kind, bool(target.get("safety_relevant"))) or surface_requires_human(
        kind, bool(outgoing.get("safety_relevant"))
    ):
        _require_operator_token(operator_token)

    entry = ChampionEntry(
        discipline=discipline,
        surface_kind=kind,
        surface_id=str(target["id"]),
        surface_version=int(target["version"]),
        promoted_from_experiment=current["promoted_from_experiment"],
        rollback_to_surface_id=str(outgoing["id"]),
        cas_version=int(current["cas_version"]),
    )
    statuses = {str(target["id"]): SurfaceStatus.CHAMPION.value}
    if outgoing["status"] != SurfaceStatus.ARCHIVED.value:
        statuses[str(outgoing["id"])] = SurfaceStatus.ARCHIVED.value
    store.set_champion_with_statuses(entry, surface_statuses=statuses)
    return entry.model_copy(update={"cas_version": entry.cas_version + 1})


__all__ = [
    "OPERATOR_TOKEN_ENV",
    "load_surface_content",
    "promote",
    "rollback",
    "seed_champion",
    "version_genome",
    "version_prompt",
]
