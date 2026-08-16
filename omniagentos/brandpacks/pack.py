"""Load brand constraints and materialize them as immutable scoped inputs."""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.scope.paths import resolve_into_realm, safe_component

__all__ = [
    "PROJECT_CONTRACT_MODE_ENV",
    "PROJECT_BRAND_TOKEN_CAP",
    "PROJECT_MEMORY_BYTE_CAP",
    "PROJECT_MEMORY_TOKEN_CAP",
    "BrandPack",
    "BrandPackError",
    "ProjectContract",
    "load_brand_pack",
    "materialize_to_inputs",
    "project_contract_mode",
    "render_project_contract",
    "resolve_project_contract",
    "resolve_project_from_roots",
]

_REQUIRED_FILES = ("voice.md", "offer.json", "banned_claims.txt")
_READ_ONLY_FILE_MODE = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_PROJECT_CONTRACT_MODES = frozenset({"off", "shadow", "enforce"})
_PROJECT_PACK_DIRS = ("brand", "brandpack", ".omniagentos/brand")

PROJECT_CONTRACT_MODE_ENV = "OMNIAGENTOS_PROJECT_CONTRACT_MODE"
PROJECT_BRAND_TOKEN_CAP = 800
"""Maximum approximate tokens of brand guidance included in a contract."""

PROJECT_MEMORY_TOKEN_CAP = 800
"""Maximum approximate tokens of project memory included in a contract."""

_CHARS_PER_TOKEN = 4
# The character budget is the ACTUAL enforced bound. It is a conservative
# approximation of PROJECT_MEMORY_TOKEN_CAP for ASCII prose; dense scripts can
# encode more tokens per character, so treat this as a byte-ish ceiling rather
# than a token guarantee.
PROJECT_MEMORY_BYTE_CAP = PROJECT_MEMORY_TOKEN_CAP * _CHARS_PER_TOKEN


class BrandPackError(ValueError):
    """A brand pack is missing, malformed, or cannot be safely materialized."""


@dataclass(frozen=True, slots=True)
class BrandPack:
    """Validated brand-source directory and its parsed content."""

    path: Path
    voice_md: str
    offer: dict[str, Any]
    banned_claims: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectContract:
    """Registry-resolved project identity and its bounded immutable inputs."""

    project: Mapping[str, Any]
    root: Path | None
    brand: BrandPack | None
    memory: str


def project_contract_mode() -> str:
    """Return the project-contract gate mode, defaulting safely to ``off``."""
    value = os.environ.get(PROJECT_CONTRACT_MODE_ENV, "off").strip().lower()
    return value if value in _PROJECT_CONTRACT_MODES else "off"


def load_brand_pack(path: Path | str) -> BrandPack:
    """Load a complete brand pack, failing closed on missing files or bad JSON."""
    root = Path(path).expanduser()
    if not root.is_dir():
        raise BrandPackError(f"brand pack directory does not exist: {root}")

    sources = {name: root / name for name in _REQUIRED_FILES}
    missing = [name for name, source in sources.items() if not source.is_file()]
    if missing:
        raise BrandPackError(f"brand pack missing required file(s): {', '.join(missing)}")

    # Brand-pack bodies are injected verbatim into worker prompts that reach
    # external model APIs, so a symlink inside the pack directory is an
    # arbitrary-file read. Containment is re-checked against the RESOLVED
    # target, not the declared path.
    root_real = _resolved_path(root)
    if root_real is None:
        raise BrandPackError(f"brand pack directory is unresolvable: {root}")
    for name, source in sources.items():
        source_real = _resolved_path(source)
        if source_real is None or not _contains(root_real, source_real):
            raise BrandPackError(
                f"brand pack file {name} resolves outside its pack directory: {root}"
            )

    try:
        voice_md = sources["voice.md"].read_text(encoding="utf-8")
        offer_value = json.loads(sources["offer.json"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BrandPackError(f"invalid brand pack at {root}: {exc}") from exc
    if not isinstance(offer_value, dict):
        raise BrandPackError("brand pack offer.json must contain a JSON object")

    try:
        banned_claims = tuple(
            line
            for raw_line in sources["banned_claims.txt"].read_text(encoding="utf-8").splitlines()
            if (line := raw_line.strip()) and not line.startswith("#")
        )
    except OSError as exc:
        raise BrandPackError(f"unable to read brand pack banned_claims.txt: {exc}") from exc
    return BrandPack(root.resolve(), voice_md, offer_value, banned_claims)


def _resolved_path(value: str | Path) -> Path | None:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _contains(parent: Path, child: Path) -> bool:
    return inode_relative_parts_anchored(child, parent) is not None


def _registered_roots(project: Mapping[str, Any]) -> list[Path]:
    raw_roots = project.get("root_dirs")
    if not isinstance(raw_roots, (list, tuple)):
        return []
    roots: list[Path] = []
    for raw_root in raw_roots:
        if not isinstance(raw_root, (str, Path)):
            continue
        raw_value = str(raw_root).strip()
        if not raw_value:
            continue
        root = _resolved_path(raw_value)
        if root is not None and root not in roots:
            roots.append(root)
    return roots


def resolve_project_from_roots(
    projects: Sequence[Mapping[str, Any]], working_dir: str | Path | None
) -> Mapping[str, Any] | None:
    """Return the most-specific registry project containing ``working_dir``.

    Registry roots are canonicalized before comparison, so symlink aliases
    cannot cause a broad project to win over a nested project.
    """
    if working_dir is None:
        return None
    task_root = _resolved_path(working_dir)
    if task_root is None:
        return None
    candidates: list[tuple[int, Mapping[str, Any]]] = []
    for project in projects:
        for root in _registered_roots(project):
            if _contains(root, task_root):
                candidates.append((len(root.parts), project))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _project_root(project: Mapping[str, Any], working_dir: str | Path | None = None) -> Path | None:
    roots = _registered_roots(project)
    task_root = _resolved_path(working_dir) if working_dir is not None else None
    if task_root is not None:
        matches = [root for root in roots if _contains(root, task_root)]
        if matches:
            return max(matches, key=lambda root: len(root.parts))
    return next((root for root in roots if root.is_dir()), None)


def _brand_for_root(root: Path | None) -> BrandPack | None:
    if root is None:
        return None
    for relative in _PROJECT_PACK_DIRS:
        try:
            return load_brand_pack(root / relative)
        except BrandPackError:
            continue
    return None


def _bounded_memory(project: Mapping[str, Any], repo_root: Path) -> str:
    project_id = str(project.get("id") or "").strip()
    if not project_id:
        return ""
    try:
        component = safe_component(project_id)
    except ValueError:
        return ""
    # safe_component is intentionally lossy for display-like strings. Memory
    # identity must not be: reject any id that would be rewritten, otherwise
    # distinct ids such as "customer a" and "customer_a" share one folder.
    if component != project_id:
        return ""
    memories_root = _resolved_path(repo_root / "var" / "memories")
    memory_path = _resolved_path(repo_root / "var" / "memories" / component / "MEMORY.md")
    # This content is injected into worker prompts that reach external model
    # APIs. A symlink at the MEMORY.md path would otherwise read any file on
    # disk, so containment is re-checked against the RESOLVED target.
    if memories_root is None or memory_path is None or not _contains(memories_root, memory_path):
        return ""
    try:
        content = memory_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return content[:PROJECT_MEMORY_BYTE_CAP].rstrip()


def resolve_project_contract(
    store: Any,
    *,
    project_id: str | None = None,
    working_dir: str | Path | None = None,
    repo_root: Path | None = None,
) -> ProjectContract | None:
    """Resolve a project by registry id or roots and bind its local brand pack.

    No process-global brand-pack setting is consulted. When an id is absent or
    stale, the most-specific registered root containing ``working_dir`` wins.
    Resolution is additive context and therefore fails soft if the registry is
    unavailable.
    """
    try:
        from omniagentos.projects import ProjectStore

        projects = ProjectStore(store).list_projects()
    except Exception:  # noqa: BLE001 -- optional context must not block dispatch.
        return None

    normalized_id = str(project_id or "").strip()
    project: Mapping[str, Any] | None = next(
        (item for item in projects if str(item.get("id") or "") == normalized_id),
        None,
    )
    if project is None:
        project = resolve_project_from_roots(projects, working_dir)
    if project is None:
        return None

    root = _project_root(project, working_dir)
    memory_root = repo_root or Path(__file__).resolve().parents[2]
    return ProjectContract(
        project=project,
        root=root,
        brand=_brand_for_root(root),
        memory=_bounded_memory(project, memory_root),
    )


def render_project_contract(
    contract: ProjectContract,
    *,
    objective: str,
    audience: str = "",
    output_format: str = "",
    deliverable_spec: str = "",
) -> str:
    """Render explicit planner/worker sections from one resolved contract."""
    project = contract.project
    facts = [
        f"- id: {project.get('id') or '(unknown)'}",
        f"- name: {project.get('name') or '(unnamed)'}",
    ]
    if contract.root is not None:
        facts.append(f"- registered root: {contract.root}")
    lines = [
        "## Project facts",
        *facts,
        "",
        "## Objective",
        objective.strip() or "(not specified)",
        "",
        "## Audience",
        audience.strip() or "(not specified)",
        "",
        "## Format",
        output_format.strip() or "(not specified)",
        "",
        "## Deliverable spec",
        deliverable_spec.strip() or "(not specified)",
    ]
    if contract.brand is not None:
        brand_char_cap = PROJECT_BRAND_TOKEN_CAP * _CHARS_PER_TOKEN
        banned_char_cap = brand_char_cap // 4
        voice_char_cap = (brand_char_cap * 3) // 8
        offer_char_cap = brand_char_cap - banned_char_cap - voice_char_cap
        banned = "\n".join(f"- {claim}" for claim in contract.brand.banned_claims)
        offer = json.dumps(
            contract.brand.offer,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        lines.extend(
            (
                "",
                "## Brand contract",
                f"- pack: {contract.brand.path}",
                "",
                "### Banned claims",
                banned[:banned_char_cap].rstrip() or "(none)",
                "",
                "### Voice",
                contract.brand.voice_md[:voice_char_cap].rstrip() or "(not specified)",
                "",
                "### Offer",
                offer[:offer_char_cap].rstrip() or "{}",
            )
        )
    memory = contract.memory[: PROJECT_MEMORY_TOKEN_CAP * _CHARS_PER_TOKEN].rstrip()
    if memory:
        lines.extend(("", "## Project memory", memory))
    return "\n".join(lines)


def materialize_to_inputs(pack: BrandPack, scope_root: Path | str, *, scope: str) -> Path:
    """Copy pack files read-only to ``var/inputs/<scope>/brand`` under ``scope_root``.

    Re-provision is intentional and must succeed: previous materializations leave
    0444 files, so a naive ``copyfile`` onto the existing target raises
    ``PermissionError`` and callers that treat that as soft failure silently keep
    the stale pack. Always replace via a same-dir temp file so overwrite works
    even when the previous copy is read-only.
    """
    root = Path(scope_root).expanduser().resolve()
    scope_dir = safe_component(scope)
    destination = root / "var" / "inputs" / scope_dir / "brand"
    destination.mkdir(parents=True, exist_ok=True)
    if resolve_into_realm(str(destination), str(root)) is None:
        raise BrandPackError("brand destination escapes the declared scope root")

    for name in _REQUIRED_FILES:
        source = pack.path / name
        target = destination / name
        if not source.is_file():
            raise BrandPackError(f"brand pack source disappeared: {source}")
        if target.is_symlink() or resolve_into_realm(str(target), str(root)) is None:
            raise BrandPackError(f"unsafe brand destination: {target}")
        try:
            _replace_read_only_copy(source, target)
        except OSError as exc:
            raise BrandPackError(f"unable to materialize brand file {name}: {exc}") from exc
    return destination


def _replace_read_only_copy(source: Path, target: Path) -> None:
    """Atomically-ish replace ``target`` with a read-only copy of ``source``.

    Writes to a temp sibling first, then replaces. If a previous read-only
    ``target`` blocks replace on this platform, relax its mode and retry the
    final rename so reprovision never no-ops on a stale pack.
    """
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        shutil.copyfile(source, tmp)
        os.chmod(tmp, _READ_ONLY_FILE_MODE)
        try:
            os.replace(tmp, target)
        except PermissionError:
            if target.is_file() and not target.is_symlink():
                os.chmod(target, stat.S_IWUSR | stat.S_IRUSR)
            os.replace(tmp, target)
        # Ensure final mode is read-only even if replace preserved old mode.
        os.chmod(target, _READ_ONLY_FILE_MODE)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
