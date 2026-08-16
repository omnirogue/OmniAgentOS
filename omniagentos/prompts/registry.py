"""Resolve an agent role id to the system prompt that role actually runs on.

``system-prompts/ROLE-REGISTRY.yaml`` is the one place that records every agent
role in this estate, where its prompt text lives, and who owns it.  This module
reads that file and answers exactly one question: *given a role id, what is the
prompt text?*

Four ``location`` kinds are recorded, and only two of them resolve to text:

``registry``
    The body lives at ``system-prompts/<id>/v<version>.md``.  This is the home
    for any prompt added from now on, and the only kind that is version-copied.
``repo``
    The body lives at some other path already inside this checkout (for example
    ``vault/prompts/roles/implementer.md``).  The registry points at the LIVE
    file rather than a copy of it, because a copy nothing reads is a copy that
    drifts, and a drifted inventory is worse than no inventory.
``external``
    The body lives outside this repository (``~/.claude/agents/*.md``,
    ``~/.omniagentos/ops/ThreeLoops/PROMPT-*.md``).  Registered by reference only: the
    text is never vendored in, and resolution raises.
``embedded``
    The body is a string constant inside a Python module.  Registered by
    reference so the inventory is honest about it; resolution raises and names
    the module and constant so the caller can go read it.

**Nothing here ever falls back to an empty string.**  An unknown role, a missing
file, a whitespace-only file, and an unresolvable location each raise a distinct
:class:`PromptRegistryError`.  This is deliberately the opposite of
``omniagentos.promptshape.rolepack.role_pack``, which is fail-soft by design and
returns ``None`` on any problem: that behaviour is correct for an optional
prompt-cache optimisation and wrong for the system of record, because a role
silently running with no contract is the failure this registry exists to make
impossible.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import Any

import yaml

from omniagentos.path_containment import inode_path_is_within_anchored

__all__ = [
    "LOCATIONS",
    "REGISTRY_RELATIVE_PATH",
    "RESOLVABLE_LOCATIONS",
    "EmptyPromptError",
    "PromptFileMissingError",
    "PromptRegistry",
    "PromptRegistryError",
    "RegistryFileError",
    "RoleEntry",
    "UnknownRoleError",
    "UnresolvablePromptError",
    "get_prompt",
    "get_role",
    "list_roles",
    "load_registry",
    "repo_root",
]

REGISTRY_RELATIVE_PATH = "system-prompts/ROLE-REGISTRY.yaml"

#: Where a prompt body lives.  See the module docstring for the semantics.
LOCATIONS: frozenset[str] = frozenset({"registry", "repo", "external", "embedded"})

#: The subset of :data:`LOCATIONS` whose text this loader can return.
RESOLVABLE_LOCATIONS: frozenset[str] = frozenset({"registry", "repo"})

#: Coarse label for what the prompt is for.  Descriptive only — the loader does
#: not branch on it — but it keeps the inventory scannable.
KINDS: frozenset[str] = frozenset({"agent", "loop", "daemon", "task", "fragment"})

_REQUIRED_ENTRY_KEYS: frozenset[str] = frozenset({
    "id",
    "description",
    "owner",
    "version",
    "live",
    "kind",
    "location",
})

_OPTIONAL_ENTRY_KEYS: frozenset[str] = frozenset({
    "prompt_file",
    "source_ref",
    "consumers",
    "notes",
})

_REGISTRY_PREFIX = "system-prompts"


class PromptRegistryError(Exception):
    """Base class for every failure this module reports."""


class RegistryFileError(PromptRegistryError):
    """The registry file itself is missing, unreadable, or malformed."""


class UnknownRoleError(PromptRegistryError):
    """No entry in the registry carries the requested role id."""


class PromptFileMissingError(PromptRegistryError):
    """A registry entry points at a prompt file that is not on disk."""


class EmptyPromptError(PromptRegistryError):
    """A prompt file exists but holds no non-whitespace text."""


class UnresolvablePromptError(PromptRegistryError):
    """The role is registered by reference, so its text is not ours to return."""


@dataclass(frozen=True)
class RoleEntry:
    """One role as recorded in ``ROLE-REGISTRY.yaml``."""

    id: str
    description: str
    owner: str
    version: int
    live: bool
    kind: str
    location: str
    prompt_file: str | None = None
    source_ref: str | None = None
    consumers: tuple[str, ...] = ()
    notes: str | None = None

    @property
    def resolvable(self) -> bool:
        """Whether :meth:`PromptRegistry.prompt_text` can return this body."""
        return self.location in RESOLVABLE_LOCATIONS


@dataclass(frozen=True)
class PromptRegistry:
    """A validated, in-memory view of ``ROLE-REGISTRY.yaml``."""

    root: Path
    path: Path
    schema_version: int
    entries: Mapping[str, RoleEntry] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[RoleEntry]:
        return iter(self.entries.values())

    def __contains__(self, role_id: object) -> bool:
        return role_id in self.entries

    def role_ids(self) -> tuple[str, ...]:
        """Every registered role id, in registry order."""
        return tuple(self.entries)

    def get(self, role_id: str) -> RoleEntry:
        """Return one entry.

        Raises:
            UnknownRoleError: the id is not registered.  The message lists the
                closest-looking ids so a typo is obvious from the error alone.
        """
        try:
            return self.entries[role_id]
        except KeyError:
            raise UnknownRoleError(
                f"unknown role id {role_id!r}; {self._suggestion_text(role_id)} "
                f"(registry: {self.path})"
            ) from None

    def _suggestion_text(self, role_id: str) -> str:
        """Name the likely intended id, so a typo is diagnosable from the error alone."""
        needle = str(role_id).strip().lower()
        known = list(self.entries)
        near: list[str] = []
        if needle:
            near = sorted(k for k in known if needle in k.lower() or k.lower() in needle)
            if not near:
                near = get_close_matches(needle, [k.lower() for k in known], n=5, cutoff=0.6)
                lowered = {k.lower(): k for k in known}
                near = [lowered[match] for match in near]
            if not near:
                # Same namespace is a strong signal even when the leaf is far off.
                prefix = needle.split(".", 1)[0]
                near = sorted(k for k in known if k.lower().split(".", 1)[0] == prefix)[:8]
        if near:
            return "did you mean one of " + ", ".join(near[:8]) + "?"
        count = len(self.entries)
        plural = "role" if count == 1 else "roles"
        return f"the registry holds {count} {plural} — call list_roles() to see them"

    def prompt_path(self, role_id: str) -> Path:
        """Absolute path to a resolvable role's prompt file.

        Raises:
            UnknownRoleError: the id is not registered.
            UnresolvablePromptError: the role is registered by reference
                (``external`` or ``embedded``), so there is no in-repo file.
        """
        entry = self.get(role_id)
        if not entry.resolvable:
            raise UnresolvablePromptError(_unresolvable_message(entry))
        # Validated at load time, so prompt_file is present for resolvable kinds.
        assert entry.prompt_file is not None
        return self.root / entry.prompt_file

    def prompt_text(self, role_id: str) -> str:
        """Return the prompt text for ``role_id``, verbatim.

        Never returns an empty or placeholder string: every failure mode raises.

        Raises:
            UnknownRoleError: the id is not registered.
            UnresolvablePromptError: the role is registered by reference.
            PromptFileMissingError: the entry points at a file that is not there.
            EmptyPromptError: the file exists but is blank.
        """
        entry = self.get(role_id)
        path = self.prompt_path(role_id)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise PromptFileMissingError(
                f"role {entry.id!r} points at {entry.prompt_file!r} but no such file exists "
                f"under {self.root}. Either restore the file or correct "
                f"`prompt_file` in {REGISTRY_RELATIVE_PATH}."
            ) from None
        except (OSError, UnicodeDecodeError) as exc:
            raise PromptFileMissingError(
                f"role {entry.id!r}: cannot read {path}: {exc}"
            ) from exc
        if not text.strip():
            raise EmptyPromptError(
                f"role {entry.id!r}: {entry.prompt_file} holds no text. A blank prompt is "
                f"never a usable prompt — write the prompt, or mark the entry `live: false` "
                f"and remove it from the registry."
            )
        return text

    def live_roles(self) -> tuple[RoleEntry, ...]:
        """Entries whose ``live`` flag is true — the prompts actually in use."""
        return tuple(entry for entry in self.entries.values() if entry.live)

    def validate_files(self) -> None:
        """Read every resolvable entry, raising on the first broken one.

        This is what a test (or a pre-commit check) calls to prove the registry
        still describes reality.
        """
        for entry in self.entries.values():
            if entry.resolvable:
                self.prompt_text(entry.id)


def _unresolvable_message(entry: RoleEntry) -> str:
    if entry.location == "external":
        return (
            f"role {entry.id!r} is registered by REFERENCE, not by value: its prompt lives "
            f"outside this repository at {entry.source_ref!r} and is deliberately not "
            f"vendored in. Read it there. To bring it under version control, copy it to "
            f"{_REGISTRY_PREFIX}/{entry.id}/v1.md and change `location` to `registry`."
        )
    if entry.location == "embedded":
        return (
            f"role {entry.id!r} has no prompt file: its text is a string constant at "
            f"{entry.source_ref!r}. Read it there. To bring it into the registry, extract "
            f"the constant to {_REGISTRY_PREFIX}/{entry.id}/v1.md, have the module read it "
            f"through this loader, and change `location` to `registry`."
        )
    # Unreachable: load-time validation rejects any other non-resolvable value.
    return f"role {entry.id!r} has unresolvable location {entry.location!r}"


def repo_root() -> Path:
    """The checkout root, independent of the process working directory."""
    return Path(__file__).resolve().parents[2]


_cache: dict[Path, tuple[int, int, PromptRegistry]] = {}
_cache_lock = threading.Lock()


def _clear_cache() -> None:
    """Drop the parsed-registry cache.

    Private on purpose.  A public cache-invalidation hook with no production
    caller is precisely what ``scripts/reachability-gate.py`` refuses, and
    rightly: this tree's signature defect is capability that exists and is never
    invoked.  Promote it to a public name at the moment a long-lived process
    genuinely needs to invalidate mid-run — not in advance of one.
    """
    with _cache_lock:
        _cache.clear()


def load_registry(path: str | Path | None = None, *, root: str | Path | None = None) -> PromptRegistry:
    """Parse and validate ``ROLE-REGISTRY.yaml``.

    Args:
        path: registry file to read.  Defaults to
            ``<repo root>/system-prompts/ROLE-REGISTRY.yaml``.
        root: the directory that ``prompt_file`` values are relative to.
            Defaults to the checkout root, or — when ``path`` is given — the
            grandparent of ``path`` so a test fixture works without extra setup.

    Raises:
        RegistryFileError: the file is missing, unreadable, or does not satisfy
            the schema.  Every structural problem is reported here rather than
            being skipped, so a malformed entry can never become a silently
            absent role.
    """
    if path is None:
        resolved_root = Path(root).resolve() if root is not None else repo_root()
        registry_path = resolved_root / REGISTRY_RELATIVE_PATH
    else:
        registry_path = Path(path).resolve()
        resolved_root = (
            Path(root).resolve() if root is not None else registry_path.parent.parent
        )

    try:
        stat = registry_path.stat()
    except OSError as exc:
        raise RegistryFileError(
            f"no prompt registry at {registry_path}: {exc}. Expected "
            f"{REGISTRY_RELATIVE_PATH} relative to the checkout root."
        ) from exc

    key = registry_path
    signature = (stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and (cached[0], cached[1]) == signature:
            return cached[2]

    registry = _parse_registry(registry_path, resolved_root)
    with _cache_lock:
        _cache[key] = (signature[0], signature[1], registry)
    return registry


def _parse_registry(registry_path: Path, root: Path) -> PromptRegistry:
    try:
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise RegistryFileError(f"cannot read {registry_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise RegistryFileError(f"{registry_path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, Mapping):
        raise RegistryFileError(
            f"{registry_path} must contain a YAML mapping at the top level, got "
            f"{type(raw).__name__}"
        )

    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise RegistryFileError(
            f"{registry_path}: `schema_version` must be an integer, got {schema_version!r}"
        )

    roles = raw.get("roles")
    if not isinstance(roles, list):
        raise RegistryFileError(
            f"{registry_path}: `roles` must be a list, got {type(roles).__name__}. An empty "
            f"registry is written as `roles: []`, never by omitting the key."
        )

    entries: dict[str, RoleEntry] = {}
    for index, item in enumerate(roles):
        entry = _parse_entry(item, index=index, registry_path=registry_path, root=root)
        if entry.id in entries:
            raise RegistryFileError(
                f"{registry_path}: role id {entry.id!r} is declared twice (entry {index}). "
                f"Ids are the primary key; a duplicate silently shadows one of the two."
            )
        entries[entry.id] = entry

    return PromptRegistry(
        root=root,
        path=registry_path,
        schema_version=schema_version,
        entries=entries,
    )


def _parse_entry(item: Any, *, index: int, registry_path: Path, root: Path) -> RoleEntry:
    where = f"{registry_path}: roles[{index}]"
    if not isinstance(item, Mapping):
        raise RegistryFileError(f"{where} must be a mapping, got {type(item).__name__}")

    keys = set(item)
    missing = _REQUIRED_ENTRY_KEYS - keys
    if missing:
        raise RegistryFileError(f"{where} is missing required key(s): {sorted(missing)}")
    unknown = keys - _REQUIRED_ENTRY_KEYS - _OPTIONAL_ENTRY_KEYS
    if unknown:
        raise RegistryFileError(
            f"{where} has unrecognised key(s): {sorted(unknown)}. A misspelled key would "
            f"otherwise read as an absent value, so unknown keys are refused. Allowed: "
            f"{sorted(_REQUIRED_ENTRY_KEYS | _OPTIONAL_ENTRY_KEYS)}"
        )

    role_id = item["id"]
    if not isinstance(role_id, str) or not role_id.strip() or role_id != role_id.strip():
        raise RegistryFileError(f"{where}: `id` must be a non-empty, unpadded string, got {role_id!r}")
    if "/" in role_id or "\\" in role_id or role_id in {".", ".."}:
        raise RegistryFileError(
            f"{where}: `id` {role_id!r} must be a single path component — it names a "
            f"directory under {_REGISTRY_PREFIX}/."
        )

    description = item["description"]
    if not isinstance(description, str) or not description.strip():
        raise RegistryFileError(f"{where} ({role_id}): `description` must be a non-empty string")

    owner = item["owner"]
    if not isinstance(owner, str) or not owner.strip():
        raise RegistryFileError(
            f"{where} ({role_id}): `owner` must be a non-empty string — who to ask before "
            f"this prompt is changed"
        )

    version = item["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RegistryFileError(f"{where} ({role_id}): `version` must be an integer >= 1, got {version!r}")

    live = item["live"]
    if not isinstance(live, bool):
        raise RegistryFileError(
            f"{where} ({role_id}): `live` must be true or false, got {live!r}. A string like "
            f"'yes' would be truthy for the wrong reason."
        )

    kind = item["kind"]
    if kind not in KINDS:
        raise RegistryFileError(
            f"{where} ({role_id}): `kind` must be one of {sorted(KINDS)}, got {kind!r}"
        )

    location = item["location"]
    if location not in LOCATIONS:
        raise RegistryFileError(
            f"{where} ({role_id}): `location` must be one of {sorted(LOCATIONS)}, got {location!r}"
        )

    prompt_file = item.get("prompt_file")
    source_ref = item.get("source_ref")
    resolvable = location in RESOLVABLE_LOCATIONS

    if resolvable:
        if source_ref is not None:
            raise RegistryFileError(
                f"{where} ({role_id}): `location: {location}` resolves to a file, so it must "
                f"use `prompt_file`, not `source_ref`"
            )
        prompt_file = _validate_prompt_file(
            prompt_file, where=where, role_id=role_id, location=location, version=version, root=root
        )
    else:
        if prompt_file is not None:
            raise RegistryFileError(
                f"{where} ({role_id}): `location: {location}` is registered by reference, so it "
                f"must use `source_ref`, not `prompt_file`"
            )
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise RegistryFileError(
                f"{where} ({role_id}): `location: {location}` requires a non-empty `source_ref` "
                f"naming where the prompt really lives"
            )

    consumers_raw = item.get("consumers", [])
    if not isinstance(consumers_raw, list) or not all(
        isinstance(c, str) and c.strip() for c in consumers_raw
    ):
        raise RegistryFileError(
            f"{where} ({role_id}): `consumers` must be a list of non-empty strings"
        )

    notes = item.get("notes")
    if notes is not None and (not isinstance(notes, str) or not notes.strip()):
        raise RegistryFileError(f"{where} ({role_id}): `notes`, when present, must be a non-empty string")

    return RoleEntry(
        id=role_id,
        description=description.strip(),
        owner=owner.strip(),
        version=version,
        live=live,
        kind=kind,
        location=location,
        prompt_file=prompt_file,
        source_ref=source_ref.strip() if isinstance(source_ref, str) else None,
        consumers=tuple(consumers_raw),
        notes=notes.strip() if isinstance(notes, str) else None,
    )


def _validate_prompt_file(
    prompt_file: Any,
    *,
    where: str,
    role_id: str,
    location: str,
    version: int,
    root: Path,
) -> str:
    if not isinstance(prompt_file, str) or not prompt_file.strip():
        raise RegistryFileError(
            f"{where} ({role_id}): `location: {location}` requires a non-empty `prompt_file`"
        )
    prompt_file = prompt_file.strip()
    candidate = Path(prompt_file)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise RegistryFileError(
            f"{where} ({role_id}): `prompt_file` must be a repo-relative path without '..', "
            f"got {prompt_file!r}"
        )
    if location == "registry":
        expected = f"{_REGISTRY_PREFIX}/{role_id}/v{version}.md"
        if prompt_file != expected:
            raise RegistryFileError(
                f"{where} ({role_id}): a `registry` prompt must live at {expected!r} so the "
                f"file on disk and the `version` field can never disagree; got {prompt_file!r}. "
                f"To publish a new version, add v{version + 1}.md and bump `version` to "
                f"{version + 1}."
            )
    elif prompt_file.split("/", 1)[0] == _REGISTRY_PREFIX:
        raise RegistryFileError(
            f"{where} ({role_id}): {prompt_file!r} is under {_REGISTRY_PREFIX}/, so its "
            f"`location` should be `registry`, not `repo`"
        )
    # Defence in depth: prove by inode ancestry, not string prefix, that the
    # resolved path stays inside the checkout.  Anything other than a positive
    # proof is refused — an undeterminable answer is not a permission.
    if inode_path_is_within_anchored(root / prompt_file, root) is not True:
        raise RegistryFileError(
            f"{where} ({role_id}): `prompt_file` {prompt_file!r} does not resolve to a path "
            f"proved to be inside {root}"
        )
    return prompt_file


def get_role(role_id: str, *, registry: PromptRegistry | None = None) -> RoleEntry:
    """Look up one role's registry entry."""
    return (registry or load_registry()).get(role_id)


def get_prompt(role_id: str, *, registry: PromptRegistry | None = None) -> str:
    """Return the system prompt text for ``role_id``.

    The one-call entry point.  See :meth:`PromptRegistry.prompt_text` for the
    exceptions; none of them is ever replaced by an empty string.
    """
    return (registry or load_registry()).prompt_text(role_id)


def list_roles(
    *,
    registry: PromptRegistry | None = None,
    live_only: bool = False,
    kind: str | None = None,
) -> tuple[RoleEntry, ...]:
    """Every registered role, optionally filtered to live ones or one ``kind``."""
    loaded = registry or load_registry()
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}, got {kind!r}")
    return tuple(
        entry
        for entry in loaded
        if (not live_only or entry.live) and (kind is None or entry.kind == kind)
    )
