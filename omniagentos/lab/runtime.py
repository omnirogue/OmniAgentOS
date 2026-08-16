"""Read-only runtime selection of promoted lab prompt champions.

The lab's write-side promotion machinery lives in :mod:`omniagentos.lab.surfaces`.
Runtime callers intentionally get a much smaller boundary: read the champion
pointer, validate its immutable prompt surface, and compare it with the prompt
the caller already planned to use.

``OMNIAGENTOS_CHAMPION_PROMPT_MODE`` ships ``off``.  ``shadow`` resolves a
champion and reports what would change without changing the selected prompt;
``enforce`` selects a valid champion.  Missing or invalid champion data never
breaks prompt assembly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Literal, Protocol, cast

from omniagentos.contracts import digest
from omniagentos.lab.contracts import SurfaceKind, SurfaceStatus
from omniagentos.path_containment import inode_relative_parts_anchored

CHAMPION_PROMPT_MODE_ENV = "OMNIAGENTOS_CHAMPION_PROMPT_MODE"

ChampionPromptMode = Literal["off", "shadow", "enforce"]
PromptSource = Literal["fallback", "champion"]

CHAMPION_PROMPT_MODES: tuple[ChampionPromptMode, ...] = ("off", "shadow", "enforce")
DEFAULT_CHAMPION_PROMPT_MODE: ChampionPromptMode = "off"


class ChampionPromptStore(Protocol):
    """The read methods required from ``lab.db.store.LabStore``."""

    def get_champion(self, discipline: str, kind: str) -> dict[str, Any] | None: ...

    def get_surface(self, surface_id: str) -> dict[str, Any] | None: ...


class ChampionPromptError(ValueError):
    """A champion pointer or its immutable prompt surface is inconsistent."""


@dataclass(frozen=True)
class ChampionPrompt:
    """Validated prompt content reached through the promoted champion pointer."""

    role: str
    discipline: str
    content: str
    surface_id: str
    surface_version: int
    content_hash: str
    cas_version: int
    promoted_from_experiment: str | None
    promoted_at: str


@dataclass(frozen=True)
class PromptSelectionDiff:
    """Structured shadow evidence for the prompt selection decision."""

    role: str
    discipline: str
    current_source: PromptSource
    would_select: PromptSource
    changed: bool
    fallback_hash: str
    champion_hash: str | None
    fallback_length: int
    champion_length: int | None
    champion_surface_id: str | None
    champion_surface_version: int | None
    reason: str


@dataclass(frozen=True)
class PromptSelection:
    """Effective prompt plus provenance and optional shadow-mode evidence."""

    mode: ChampionPromptMode
    prompt: str
    source: PromptSource
    champion: ChampionPrompt | None
    shadow_diff: PromptSelectionDiff | None
    reason: str

    @property
    def selected_prompt(self) -> str:
        """Explicit alias for integration call sites."""

        return self.prompt


def parse_champion_prompt_mode(value: object) -> ChampionPromptMode:
    """Parse a mode spelling, defaulting invalid or absent values to ``off``.

    Only the three named ramp stages are accepted.  In particular, a typo or
    generic truthy value must not unexpectedly enable prompt enforcement.
    """

    if not isinstance(value, str):
        return DEFAULT_CHAMPION_PROMPT_MODE
    normalized = value.strip().lower()
    if normalized in CHAMPION_PROMPT_MODES:
        return cast(ChampionPromptMode, normalized)
    return DEFAULT_CHAMPION_PROMPT_MODE


def champion_prompt_mode(
    env: Mapping[str, str] | None = None,
) -> ChampionPromptMode:
    """Return the tri-state champion-prompt mode from the environment."""

    source = os.environ if env is None else env
    return parse_champion_prompt_mode(source.get(CHAMPION_PROMPT_MODE_ENV))


def _repository_root() -> Path:
    """Return the checkout root independently of the process working directory."""

    return Path(__file__).resolve().parents[2]


def _validated_component(value: str, *, field: str) -> str:
    component = PurePath(value)
    if (
        not value
        or "/" in value
        or "\\" in value
        or component.name != value
        or value in {".", ".."}
    ):
        raise ChampionPromptError(f"{field} must be a single path component: {value!r}")
    return value


def _required_text(row: Mapping[str, Any], key: str, *, record: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ChampionPromptError(f"{record} has invalid {key}")
    return value


def _required_int(row: Mapping[str, Any], key: str, *, record: str) -> int:
    value = row.get(key)
    if value is None or isinstance(value, bool):
        raise ChampionPromptError(f"{record} has invalid {key}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ChampionPromptError(f"{record} has invalid {key}") from exc
    if parsed < 0:
        raise ChampionPromptError(f"{record} has invalid {key}")
    return parsed


def _prompt_file(
    surface: Mapping[str, Any],
    *,
    role: str,
    repository_root: Path,
) -> Path:
    persisted_path = _required_text(surface, "path", record="prompt surface")
    relative = Path(persisted_path)
    expected_prefix = ("vault", "prompts", role)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or tuple(relative.parts[:3]) != expected_prefix
    ):
        raise ChampionPromptError(
            f"prompt surface path is not role-scoped under vault/prompts/{role}"
        )

    root = repository_root.resolve()
    prompt_root = (root / "vault" / "prompts").resolve()
    role_root = prompt_root / role
    canonical_role_root = role_root.resolve()
    target = (root / relative).resolve()
    if (
        inode_relative_parts_anchored(prompt_root, root) is None
        or canonical_role_root != role_root
        or inode_relative_parts_anchored(target, canonical_role_root) is None
    ):
        raise ChampionPromptError("prompt surface path escapes its canonical role directory")
    return target


def get_champion_prompt(
    store: ChampionPromptStore,
    role: str,
    *,
    discipline: str | None = None,
    repository_root: Path | None = None,
) -> ChampionPrompt | None:
    """Read and validate the promoted prompt champion for ``role``.

    ``discipline`` defaults to ``role`` for role-keyed champion registries.
    Callers whose lab discipline is broader (for example ``"swarm"``) pass it
    explicitly; the surface label and path are still required to match the
    requested role.

    This function invokes only ``get_champion`` and ``get_surface`` on the
    store.  It never seeds, promotes, updates, or otherwise mutates lab state.
    A missing pointer returns ``None``; inconsistent persisted data raises
    :class:`ChampionPromptError` so selection can safely report a fallback.
    """

    selected_role = _validated_component(role, field="role")
    selected_discipline = discipline if discipline is not None else selected_role
    if not selected_discipline:
        raise ChampionPromptError("discipline must not be empty")

    champion = store.get_champion(selected_discipline, SurfaceKind.PROMPT.value)
    if champion is None:
        return None

    champion_discipline = _required_text(champion, "discipline", record="champion")
    champion_kind = _required_text(champion, "surface_kind", record="champion")
    surface_id = _required_text(champion, "surface_id", record="champion")
    surface_version = _required_int(champion, "surface_version", record="champion")
    cas_version = _required_int(champion, "cas_version", record="champion")
    if champion_discipline != selected_discipline or champion_kind != SurfaceKind.PROMPT.value:
        raise ChampionPromptError("champion pointer does not match the requested prompt discipline")

    surface = store.get_surface(surface_id)
    if surface is None:
        raise ChampionPromptError(f"champion prompt surface does not exist: {surface_id}")

    if _required_text(surface, "id", record="prompt surface") != surface_id:
        raise ChampionPromptError("champion pointer and prompt surface ids differ")
    if _required_text(surface, "kind", record="prompt surface") != SurfaceKind.PROMPT.value:
        raise ChampionPromptError("champion surface is not a prompt")
    if _required_text(surface, "discipline", record="prompt surface") != selected_discipline:
        raise ChampionPromptError("champion prompt surface has the wrong discipline")
    if _required_text(surface, "label", record="prompt surface") != selected_role:
        raise ChampionPromptError("champion prompt surface has the wrong role")
    if _required_text(surface, "status", record="prompt surface") != SurfaceStatus.CHAMPION.value:
        raise ChampionPromptError("champion prompt surface is not in champion status")
    if _required_int(surface, "version", record="prompt surface") != surface_version:
        raise ChampionPromptError("champion pointer and prompt surface versions differ")

    expected_hash = _required_text(surface, "content_hash", record="prompt surface")
    target = _prompt_file(
        surface,
        role=selected_role,
        repository_root=repository_root or _repository_root(),
    )
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ChampionPromptError("could not read champion prompt surface") from exc
    if digest(content) != expected_hash:
        raise ChampionPromptError("champion prompt content hash does not match its surface")

    promoted_from = champion.get("promoted_from_experiment")
    if promoted_from is not None and not isinstance(promoted_from, str):
        raise ChampionPromptError("champion has invalid promoted_from_experiment")
    promoted_at = champion.get("promoted_at")
    if promoted_at is None:
        promoted_at = ""
    if not isinstance(promoted_at, str):
        raise ChampionPromptError("champion has invalid promoted_at")

    return ChampionPrompt(
        role=selected_role,
        discipline=selected_discipline,
        content=content,
        surface_id=surface_id,
        surface_version=surface_version,
        content_hash=expected_hash,
        cas_version=cas_version,
        promoted_from_experiment=promoted_from,
        promoted_at=promoted_at,
    )


def build_prompt_selection_diff(
    fallback_prompt: str,
    *,
    role: str,
    discipline: str,
    champion: ChampionPrompt | None,
    unavailable_reason: str | None = None,
) -> PromptSelectionDiff:
    """Build serializable shadow evidence without selecting the champion."""

    if champion is None:
        reason = unavailable_reason or "no_champion"
        would_select: PromptSource = "fallback"
        champion_hash = None
        champion_length = None
        champion_surface_id = None
        champion_surface_version = None
        changed = False
    else:
        would_select = "champion"
        champion_hash = champion.content_hash
        champion_length = len(champion.content)
        champion_surface_id = champion.surface_id
        champion_surface_version = champion.surface_version
        changed = champion.content != fallback_prompt
        reason = "champion_differs" if changed else "champion_matches_fallback"

    return PromptSelectionDiff(
        role=role,
        discipline=discipline,
        current_source="fallback",
        would_select=would_select,
        changed=changed,
        fallback_hash=digest(fallback_prompt),
        champion_hash=champion_hash,
        fallback_length=len(fallback_prompt),
        champion_length=champion_length,
        champion_surface_id=champion_surface_id,
        champion_surface_version=champion_surface_version,
        reason=reason,
    )


def select_champion_prompt(
    fallback_prompt: str,
    *,
    role: str,
    store: ChampionPromptStore | None = None,
    discipline: str | None = None,
    mode: ChampionPromptMode | str | None = None,
    repository_root: Path | None = None,
) -> PromptSelection:
    """Select a prompt according to the off→shadow→enforce feature ramp.

    In ``off`` mode this function deliberately returns before validating the
    role or touching ``store``.  That makes the shipped default behavior
    identical to the existing prompt path, even if the lab database is absent.
    """

    effective_mode = champion_prompt_mode() if mode is None else parse_champion_prompt_mode(mode)
    if effective_mode == "off":
        return PromptSelection(
            mode="off",
            prompt=fallback_prompt,
            source="fallback",
            champion=None,
            shadow_diff=None,
            reason="feature_off",
        )

    selected_discipline = discipline if discipline is not None else role
    champion: ChampionPrompt | None = None
    unavailable_reason: str | None = None
    if store is None:
        unavailable_reason = "store_unavailable"
    else:
        try:
            champion = get_champion_prompt(
                store,
                role,
                discipline=selected_discipline,
                repository_root=repository_root,
            )
        except Exception as exc:  # noqa: BLE001 - optional runtime data must not break launch
            unavailable_reason = f"invalid_champion:{exc}"

    if effective_mode == "shadow":
        shadow_diff = build_prompt_selection_diff(
            fallback_prompt,
            role=role,
            discipline=selected_discipline,
            champion=champion,
            unavailable_reason=unavailable_reason,
        )
        return PromptSelection(
            mode="shadow",
            prompt=fallback_prompt,
            source="fallback",
            champion=champion,
            shadow_diff=shadow_diff,
            reason="shadow_only",
        )

    if champion is None:
        return PromptSelection(
            mode="enforce",
            prompt=fallback_prompt,
            source="fallback",
            champion=None,
            shadow_diff=None,
            reason=unavailable_reason or "no_champion",
        )
    return PromptSelection(
        mode="enforce",
        prompt=champion.content,
        source="champion",
        champion=champion,
        shadow_diff=None,
        reason="champion_selected",
    )


__all__ = [
    "CHAMPION_PROMPT_MODES",
    "CHAMPION_PROMPT_MODE_ENV",
    "DEFAULT_CHAMPION_PROMPT_MODE",
    "ChampionPrompt",
    "ChampionPromptError",
    "ChampionPromptMode",
    "ChampionPromptStore",
    "PromptSelection",
    "PromptSelectionDiff",
    "PromptSource",
    "build_prompt_selection_diff",
    "champion_prompt_mode",
    "get_champion_prompt",
    "parse_champion_prompt_mode",
    "select_champion_prompt",
]
