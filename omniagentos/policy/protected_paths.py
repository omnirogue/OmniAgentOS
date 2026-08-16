"""Strict protected-path registry loader — the only parser for the authority.

``configs/protected-paths.yaml`` is versioned data. Immutable safety floors live
here in code so a malicious or accidental registry edit cannot delete or demote
current critical coverage. Load failures raise; they never produce empty
protection.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from omniagentos.path_containment import inode_path_is_within_anchored

__all__ = [
    "PROTECTED_PATHS_SCHEMA_VERSION",
    "ProtectedPathsError",
    "ProtectedPathsRegistry",
    "get_protected_paths",
    "path_tier",
]

PROTECTED_PATHS_SCHEMA_VERSION = 1

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_REGISTRY_REL = "configs/protected-paths.yaml"

# Immutable floors — current critical coverage that the registry must preserve.
_FLOOR_TIER_P_FILES: frozenset[str] = frozenset(
    {
        "omniagentos/reliability/governance.py",
        "configs/governance.yaml",
        "configs/reliability.yaml",
        "configs/policy.yaml",
        "configs/protected-paths.yaml",
        "omniagentos/contracts.py",
        "omniagentos/reliability/judges.py",
        "omniagentos/api/routes/autonomy.py",
    }
)
_FLOOR_TIER_P_DIRS: frozenset[str] = frozenset(
    {
        "omniagentos/policy",
        "contracts",
        "omniagentos/notifications",
        "scripts/reliability",
        "omniagentos/db/migrations",
    }
)
_FLOOR_TIER_S_FILES: frozenset[str] = frozenset(
    {
        "omniagentos/api/routes/reliability.py",
        "omniagentos/api/routes/improvements.py",
        "omniagentos/api/routes/org.py",
        "omniagentos/steward/alerts/monitor.py",
        "omniagentos/steward/alerts/rules.py",
        "ARCHI.md",
    }
)
_FLOOR_TIER_S_DIRS: frozenset[str] = frozenset(
    {
        "omniagentos/reliability",
        "omniagentos/orgdims",
        "omniagentos/archdocs",
        "docs/architecture",
    }
)
_FLOOR_PROTECTED_P_MODULES: frozenset[str] = frozenset(
    {"governance", "judges", "contracts", "autonomy"}
)
_FLOOR_PROTECTED_S_MODULES: frozenset[str] = frozenset(
    {"pipeline", "detector", "analyzer", "sandbox", "recovery", "scorecards", "memory"}
)
_FLOOR_LAUNCHAGENT_PATH_CONTAINS = "Library/LaunchAgents/com.omniagentos."
_FLOOR_LAUNCHAGENT_BASENAME_CONTAINS = "com.omniagentos."

_TOP_KEYS = frozenset({"version", "tier_p", "tier_s"})
_TIER_P_KEYS = frozenset({"files", "directories", "modules", "launchagents"})
_TIER_S_KEYS = frozenset({"files", "directories", "modules"})
_LAUNCHAGENT_KEYS = frozenset({"path_contains", "basename_contains", "require_launchagents_dir"})


class ProtectedPathsError(ValueError):
    """Registry missing, malformed, demoted, or otherwise unsafe."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(
            None,
            None,
            f"expected a mapping node, got {node.id}",
            node.start_mark,
        )
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class ProtectedPathsRegistry:
    """Validated protected-path authority consumed by governance."""

    version: int
    tier_p_files: frozenset[str]
    tier_p_dirs: tuple[str, ...]
    tier_s_files: frozenset[str]
    tier_s_dirs: tuple[str, ...]
    protected_p_modules: frozenset[str]
    protected_s_modules: frozenset[str]
    launchagent_path_contains: str
    launchagent_basename_contains: str
    launchagent_require_dir: bool
    source_path: str

    def path_tier(self, path: str) -> str | None:
        """Return 'P', 'S', or None. Tier P is checked first (it wins)."""
        p = _norm_runtime_path(path)
        if not p:
            return None
        if self._launchagent_match(p):
            return "P"
        if os.path.isabs(p):
            return None
        if p in self.tier_p_files:
            return "P"
        for d in self.tier_p_dirs:
            if p == d or p.startswith(d + "/"):
                return "P"
        if p in self.tier_s_files:
            return "S"
        for d in self.tier_s_dirs:
            if p == d or p.startswith(d + "/"):
                return "S"
        return None

    def _launchagent_match(self, p: str) -> bool:
        if self.launchagent_path_contains and self.launchagent_path_contains in p:
            return True
        base = os.path.basename(p)
        if self.launchagent_basename_contains and self.launchagent_basename_contains in base:
            if not self.launchagent_require_dir:
                return True
            if "LaunchAgents" in p:
                return True
        return False


def _default_registry_path() -> Path:
    return _REPO_ROOT / _DEFAULT_REGISTRY_REL


@lru_cache(maxsize=1)
def _cached_default(path_key: str) -> ProtectedPathsRegistry:
    return _load_protected_paths_uncached(Path(path_key))


def get_protected_paths() -> ProtectedPathsRegistry:
    """Load and cache the checked-in registry. Fail-closed on any error."""
    return _cached_default(str(_default_registry_path()))


def _clear_protected_paths_cache() -> None:
    """Drop the cached default registry (test helper)."""
    _cached_default.cache_clear()


def _load_protected_paths(path: str | Path | None = None) -> ProtectedPathsRegistry:
    """Strict load of a protected-paths registry. Raises on any safety violation."""
    if path is None:
        return get_protected_paths()
    return _load_protected_paths_uncached(Path(path))


def path_tier(path: str) -> str | None:
    """Classify a path using the default checked-in registry."""
    return get_protected_paths().path_tier(path)


def _load_protected_paths_uncached(path: Path) -> ProtectedPathsRegistry:
    reg_path = Path(path)
    _assert_registry_location_safe(reg_path)

    try:
        if not reg_path.exists():
            raise ProtectedPathsError(f"protected-paths registry missing: {reg_path}")
    except OSError as exc:
        raise ProtectedPathsError(
            f"protected-paths registry unreadable: {reg_path}: {exc}"
        ) from exc

    raw_text = _read_registry_text(reg_path)

    try:
        data = yaml.load(raw_text, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        raise ProtectedPathsError(f"malformed protected-paths registry: {exc}") from exc

    if not isinstance(data, Mapping):
        raise ProtectedPathsError("protected-paths registry must be a mapping")

    _validate_mapping_keys(data, _TOP_KEYS, "top-level")
    if "version" not in data or "tier_p" not in data or "tier_s" not in data:
        raise ProtectedPathsError("protected-paths registry requires version, tier_p, tier_s")

    version = data["version"]
    if type(version) is not int:
        raise ProtectedPathsError(f"version must be int, got {version!r}")
    if version != PROTECTED_PATHS_SCHEMA_VERSION:
        raise ProtectedPathsError(
            f"unsupported protected-paths version {version}; "
            f"expected {PROTECTED_PATHS_SCHEMA_VERSION}"
        )

    tier_p = data["tier_p"]
    tier_s = data["tier_s"]
    if not isinstance(tier_p, Mapping) or not isinstance(tier_s, Mapping):
        raise ProtectedPathsError("tier_p and tier_s must be mappings")

    _validate_mapping_keys(tier_p, _TIER_P_KEYS, "tier_p")
    _validate_mapping_keys(tier_s, _TIER_S_KEYS, "tier_s")

    p_files = _parse_path_list(tier_p.get("files"), "tier_p.files")
    p_dirs = _parse_path_list(tier_p.get("directories"), "tier_p.directories")
    s_files = _parse_path_list(tier_s.get("files"), "tier_s.files")
    s_dirs = _parse_path_list(tier_s.get("directories"), "tier_s.directories")
    p_modules = _parse_module_list(tier_p.get("modules"), "tier_p.modules")
    s_modules = _parse_module_list(tier_s.get("modules"), "tier_s.modules")
    la_path, la_base, la_require = _parse_launchagents(tier_p.get("launchagents"))

    _reject_duplicates(p_files, "tier_p.files")
    _reject_duplicates(p_dirs, "tier_p.directories")
    _reject_duplicates(s_files, "tier_s.files")
    _reject_duplicates(s_dirs, "tier_s.directories")
    _reject_duplicates(p_modules, "tier_p.modules")
    _reject_duplicates(s_modules, "tier_s.modules")

    p_file_set = frozenset(p_files)
    s_file_set = frozenset(s_files)
    p_dir_set = frozenset(p_dirs)
    s_dir_set = frozenset(s_dirs)
    p_mod_set = frozenset(p_modules)
    s_mod_set = frozenset(s_modules)

    _reject_symlink_confused_entries(p_file_set | s_file_set | p_dir_set | s_dir_set)
    _reject_cross_tier_overlap(p_file_set, s_file_set, p_dir_set, s_dir_set, p_mod_set, s_mod_set)
    _enforce_floors(
        p_file_set,
        s_file_set,
        p_dir_set,
        s_dir_set,
        p_mod_set,
        s_mod_set,
        la_path,
        la_base,
    )

    return ProtectedPathsRegistry(
        version=version,
        tier_p_files=p_file_set,
        tier_p_dirs=tuple(sorted(p_dir_set)),
        tier_s_files=s_file_set,
        tier_s_dirs=tuple(sorted(s_dir_set)),
        protected_p_modules=p_mod_set,
        protected_s_modules=s_mod_set,
        launchagent_path_contains=la_path,
        launchagent_basename_contains=la_base,
        launchagent_require_dir=la_require,
        source_path=str(reg_path),
    )


def _assert_registry_location_safe(reg_path: Path) -> None:
    """Refuse symlink-confused or out-of-repo registry locations under the repo."""
    if reg_path.is_symlink():
        raise ProtectedPathsError(f"protected-paths registry must not be a symlink: {reg_path}")

    try:
        resolved = Path(os.path.realpath(reg_path))
    except OSError as exc:
        raise ProtectedPathsError(f"protected-paths registry unresolvable: {exc}") from exc

    absolute_spelling = Path(os.path.abspath(reg_path))
    try:
        absolute_spelling.relative_to(_REPO_ROOT)
    except ValueError:
        under_repo_spelling = False
    else:
        under_repo_spelling = True

    # The path-decision registry binds this non-security lexical comparison to
    # its exact source text. Keep that expression stable while the underlying
    # test-support helper remains private.
    default_registry_path = _default_registry_path
    if under_repo_spelling or absolute_spelling == default_registry_path():
        if inode_path_is_within_anchored(resolved, _REPO_ROOT.resolve()) is not True:
            raise ProtectedPathsError(
                f"protected-paths registry escapes repo via symlink/confusion: {reg_path}"
            )


def _read_registry_text(reg_path: Path) -> str:
    """Read one regular file without following a swapped leaf symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(reg_path, flags)
    except OSError as exc:
        raise ProtectedPathsError(
            f"protected-paths registry unreadable or symlink-confused: {reg_path}: {exc}"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ProtectedPathsError(f"protected-paths registry is not a regular file: {reg_path}")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return handle.read()
    except (OSError, UnicodeError) as exc:
        raise ProtectedPathsError(
            f"protected-paths registry unreadable: {reg_path}: {exc}"
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _validate_mapping_keys(
    raw: Mapping[Any, Any],
    allowed: frozenset[str],
    field: str,
) -> None:
    if any(not isinstance(key, str) for key in raw):
        raise ProtectedPathsError(f"{field} mapping keys must be strings")
    unknown = set(raw) - allowed
    if unknown:
        raise ProtectedPathsError(f"unexpected {field} key(s): {sorted(unknown)}")


def _norm_registry_entry(raw: Any, field: str) -> str:
    if not isinstance(raw, str):
        raise ProtectedPathsError(f"{field} entries must be strings, got {raw!r}")
    s = raw.replace("\\", "/").strip()
    if not s:
        raise ProtectedPathsError(f"{field} contains an empty path")
    if s.startswith("~"):
        raise ProtectedPathsError(f"{field} rejects home-relative path: {raw!r}")
    if s.startswith("/") or s.startswith("//") or (len(s) >= 2 and s[1] == ":"):
        raise ProtectedPathsError(f"{field} rejects absolute/foreign path: {raw!r}")
    parts = s.split("/")
    if any(p == "" for p in parts):
        raise ProtectedPathsError(f"{field} rejects empty path segment: {raw!r}")
    if any(p in (".", "..") for p in parts):
        raise ProtectedPathsError(f"{field} rejects traversal segment: {raw!r}")
    return "/".join(parts)


def _norm_runtime_path(path: str | None) -> str:
    """Normalize a runtime path for matching (never resolves symlinks)."""
    if not path:
        return ""
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def _parse_path_list(raw: Any, field: str) -> list[str]:
    if raw is None:
        raise ProtectedPathsError(f"{field} is required")
    if not isinstance(raw, list):
        raise ProtectedPathsError(f"{field} must be a list of strings")
    return [_norm_registry_entry(item, field) for item in raw]


def _parse_module_list(raw: Any, field: str) -> list[str]:
    if raw is None:
        raise ProtectedPathsError(f"{field} is required")
    if not isinstance(raw, list):
        raise ProtectedPathsError(f"{field} must be a list of strings")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ProtectedPathsError(f"{field} entries must be strings, got {item!r}")
        name = item.strip()
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ProtectedPathsError(f"{field} invalid module stem: {item!r}")
        if not name.isidentifier():
            raise ProtectedPathsError(f"{field} invalid module stem: {item!r}")
        out.append(name)
    return out


def _parse_launchagents(raw: Any) -> tuple[str, str, bool]:
    field = "tier_p.launchagents"
    if raw is None:
        raise ProtectedPathsError(f"{field} is required")
    if not isinstance(raw, Mapping):
        raise ProtectedPathsError(f"{field} must be a mapping")
    _validate_mapping_keys(raw, _LAUNCHAGENT_KEYS, "launchagents")
    for key in ("path_contains", "basename_contains", "require_launchagents_dir"):
        if key not in raw:
            raise ProtectedPathsError(f"{field} missing required key {key}")
    path_contains = raw["path_contains"]
    basename_contains = raw["basename_contains"]
    require_dir = raw["require_launchagents_dir"]
    if not isinstance(path_contains, str) or not path_contains:
        raise ProtectedPathsError(f"{field}.path_contains must be a non-empty string")
    if not isinstance(basename_contains, str) or not basename_contains:
        raise ProtectedPathsError(f"{field}.basename_contains must be a non-empty string")
    if type(require_dir) is not bool:
        raise ProtectedPathsError(f"{field}.require_launchagents_dir must be a boolean")
    path_contains = path_contains.replace("\\", "/")
    return path_contains, basename_contains, require_dir


def _reject_duplicates(items: list[str], field: str) -> None:
    seen: set[str] = set()
    for item in items:
        if item in seen:
            raise ProtectedPathsError(f"duplicate entry in {field}: {item}")
        seen.add(item)


def _under_any(path: str, dirs: frozenset[str]) -> bool:
    for d in dirs:
        if path == d or path.startswith(d + "/"):
            return True
    return False


def _reject_symlink_confused_entries(entries: frozenset[str]) -> None:
    """Reject a registry path whose existing prefix crosses a symlink."""
    repo_root = _REPO_ROOT.resolve()
    for entry in entries:
        candidate = repo_root / entry
        if inode_path_is_within_anchored(candidate, repo_root) is not True:
            raise ProtectedPathsError(
                f"protected path entry escapes repo or is unresolvable: {entry}"
            )
        cursor = repo_root
        for part in entry.split("/"):
            cursor /= part
            try:
                mode = os.lstat(cursor).st_mode
            except FileNotFoundError:
                break
            except OSError as exc:
                raise ProtectedPathsError(
                    f"protected path entry is unresolvable: {entry}: {exc}"
                ) from exc
            if stat.S_ISLNK(mode):
                raise ProtectedPathsError(f"protected path entry crosses symlink: {entry}")


def _reject_cross_tier_overlap(
    p_files: frozenset[str],
    s_files: frozenset[str],
    p_dirs: frozenset[str],
    s_dirs: frozenset[str],
    p_mods: frozenset[str],
    s_mods: frozenset[str],
) -> None:
    both_files = p_files & s_files
    if both_files:
        raise ProtectedPathsError(f"cross-tier file ambiguity: {sorted(both_files)}")
    both_dirs = p_dirs & s_dirs
    if both_dirs:
        raise ProtectedPathsError(f"cross-tier directory ambiguity: {sorted(both_dirs)}")
    both_mods = p_mods & s_mods
    if both_mods:
        raise ProtectedPathsError(f"cross-tier module ambiguity: {sorted(both_mods)}")

    all_files = p_files | s_files
    all_dirs = p_dirs | s_dirs
    clash = all_files & all_dirs
    if clash:
        raise ProtectedPathsError(f"path listed as both file and directory: {sorted(clash)}")

    for pd in p_dirs:
        for sd in s_dirs:
            if pd.startswith(sd + "/") or sd.startswith(pd + "/"):
                raise ProtectedPathsError(
                    f"cross-tier nested directory ambiguity: {pd!r} vs {sd!r}"
                )

    for sf in s_files:
        if _under_any(sf, p_dirs):
            raise ProtectedPathsError(
                f"tier_s file under tier_p directory (demotion/ambiguity): {sf}"
            )

    for field, files, dirs in (
        ("tier_p", p_files, p_dirs),
        ("tier_s", s_files, s_dirs),
    ):
        redundant = sorted(path for path in files if _under_any(path, dirs))
        if redundant:
            raise ProtectedPathsError(f"overlapping {field} file/directory entries: {redundant}")

    for group_name, group in (("tier_p.directories", p_dirs), ("tier_s.directories", s_dirs)):
        ordered = sorted(group)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                if b.startswith(a + "/") or a.startswith(b + "/"):
                    raise ProtectedPathsError(
                        f"nested directory overlap in {group_name}: {a!r} vs {b!r}"
                    )


def _enforce_floors(
    p_files: frozenset[str],
    s_files: frozenset[str],
    p_dirs: frozenset[str],
    s_dirs: frozenset[str],
    p_mods: frozenset[str],
    s_mods: frozenset[str],
    la_path: str,
    la_base: str,
) -> None:
    missing_p = _FLOOR_TIER_P_FILES - p_files
    if missing_p:
        raise ProtectedPathsError(f"floor removal of tier_p files: {sorted(missing_p)}")
    missing_pd = _FLOOR_TIER_P_DIRS - p_dirs
    if missing_pd:
        raise ProtectedPathsError(f"floor removal of tier_p directories: {sorted(missing_pd)}")
    missing_s = _FLOOR_TIER_S_FILES - s_files
    if missing_s:
        raise ProtectedPathsError(f"floor removal of tier_s files: {sorted(missing_s)}")
    missing_sd = _FLOOR_TIER_S_DIRS - s_dirs
    if missing_sd:
        raise ProtectedPathsError(f"floor removal of tier_s directories: {sorted(missing_sd)}")
    missing_pm = _FLOOR_PROTECTED_P_MODULES - p_mods
    if missing_pm:
        raise ProtectedPathsError(f"floor removal of tier_p modules: {sorted(missing_pm)}")
    missing_sm = _FLOOR_PROTECTED_S_MODULES - s_mods
    if missing_sm:
        raise ProtectedPathsError(f"floor removal of tier_s modules: {sorted(missing_sm)}")

    demoted_files = _FLOOR_TIER_P_FILES & s_files
    if demoted_files:
        raise ProtectedPathsError(f"floor demotion of tier_p files: {sorted(demoted_files)}")
    demoted_dirs = _FLOOR_TIER_P_DIRS & s_dirs
    if demoted_dirs:
        raise ProtectedPathsError(f"floor demotion of tier_p directories: {sorted(demoted_dirs)}")
    demoted_mods = _FLOOR_PROTECTED_P_MODULES & s_mods
    if demoted_mods:
        raise ProtectedPathsError(f"floor demotion of tier_p modules: {sorted(demoted_mods)}")

    if la_path not in _FLOOR_LAUNCHAGENT_PATH_CONTAINS:
        raise ProtectedPathsError(f"floor demotion of launchagent path_contains: {la_path!r}")
    if la_base not in _FLOOR_LAUNCHAGENT_BASENAME_CONTAINS:
        raise ProtectedPathsError(f"floor demotion of launchagent basename_contains: {la_base!r}")
