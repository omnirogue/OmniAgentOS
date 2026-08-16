"""Path builders for omniagentos.selfimprove (constraints/ and skills/ roots,
plus the `playbook/skill-<slug>.md` vault relpath convention).

Mirrors the `default_<x>_dir()` pattern in `omniagentos.contracts`
(env-var override, else a `<repo_root>/<dir>` default) but stays package-local
since `constraints/` and `skills/` are not part of the frozen cross-package
contract.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import IO

from omniagentos.path_containment import inode_relative_parts_anchored

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class PathEscapesRootError(ValueError):
    """Raised when writing to a computed path would escape its configured
    root directory — e.g. because a path component is a pre-existing symlink
    pointing outside the root (F3: no other package may write outside the
    roots this package is configured with)."""


def _repo_root() -> str:
    # omniagentos/selfimprove/paths.py -> omniagentos/selfimprove -> omniagentos -> repo root
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_constraints_dir() -> str:
    """`OMNIAGENTOS_CONSTRAINTS_DIR` env override, else `<repo_root>/constraints`.

    Deliberately NOT under `var/` (gitignored runtime scratch, `var/*` in
    .gitignore): a growing CONSTRAINTS.md is meant to persist and be read by
    future sessions, the same durability class as `vault/`.
    """
    return os.environ.get("OMNIAGENTOS_CONSTRAINTS_DIR") or os.path.join(
        _repo_root(), "constraints"
    )


def default_skills_dir() -> str:
    """`OMNIAGENTOS_SKILLS_DIR` env override, else `<repo_root>/skills`.

    Mirrors the folder-per-skill `SKILL.md` convention so a coding agent can
    load a captured skill directly, independent of its vault note mirror.
    """
    return os.environ.get("OMNIAGENTOS_SKILLS_DIR") or os.path.join(_repo_root(), "skills")


def safe_slug(value: str) -> str:
    """Filesystem-safe, INJECTIVE (collision-resistant) slug.

    Always suffixes a digest of the *full original* `value` — never only
    on the (fragile) condition that sanitization changed something. The
    previous "sanitize, then hash only if that changed the string" approach
    was not injective: an already-safe identifier (e.g. `foo-c0e0aaaea050`)
    and the hashed form of an unrelated, unsafe identifier (e.g. `foo!`,
    which sanitizes+hashes to that exact string) could collide and silently
    share one project's/skill's storage (F4). Hashing unconditionally makes
    two distinct inputs collide only on a sha256-prefix collision, which is
    cryptographically negligible, rather than on a crafted string match.

    Raises `ValueError` for a blank (empty or all-whitespace) `value` — an
    identifier this package stores something under must be a real,
    non-blank name, not a silently-substituted placeholder.
    """
    if not value or not value.strip():
        raise ValueError("identifier must be a non-blank string")
    sanitized = value.replace("..", "-")
    sanitized = re.sub(r"[\\/:\s]+", "-", sanitized)
    sanitized = _UNSAFE_FILENAME_CHARS.sub("-", sanitized)
    sanitized = sanitized.strip(".-") or "id"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{sanitized}-{digest}"


def skill_relpath(skill_id: str) -> str:
    """`playbook/skill-<slug>.md`.

    The `skill-` prefix keeps captured skill notes from colliding with H2
    lab's per-discipline `playbook/<discipline>.md` files
    (`omniagentos/lab/vault/paths.py:playbook_relpath`), which never use that
    prefix — both packages share the `playbook/` folder reserved by
    contracts/vault-frontmatter.md, but never the same filename.
    """
    return f"playbook/skill-{safe_slug(skill_id)}.md"


def constraints_path(project: str, *, constraints_dir: str | None = None) -> Path:
    """`<constraints_dir or default_constraints_dir()>/<project-slug>/CONSTRAINTS.md`."""
    base = Path(constraints_dir) if constraints_dir is not None else Path(default_constraints_dir())
    return base / safe_slug(project) / "CONSTRAINTS.md"


def skill_md_path(skill_id: str, *, skills_dir: str | None = None) -> Path:
    """`<skills_dir or default_skills_dir()>/<skill-id-slug>/SKILL.md`."""
    base = Path(skills_dir) if skills_dir is not None else Path(default_skills_dir())
    return base / safe_slug(skill_id) / "SKILL.md"


def ensure_safe_write_target(root: str | Path, target: Path) -> None:
    """Raise `PathEscapesRootError` before any write happens if a PRE-EXISTING
    symlink anywhere between `root` and `target` (inclusive of the leaf)
    would redirect that write outside `root` (F3).

    `target` must already be lexically inside `root` (built via `Path` joins
    of a `safe_slug()`-ed component, as every caller in this package does) —
    that is a precondition, not itself the thing this checks. What this
    checks is that walking `root`'s *resolved* location down through each
    path component of `target` never crosses a symlink: `Path.mkdir()` and
    `Path.write_text()` both follow symlinks silently, so a pre-existing
    symlink at any component (a project/skill directory, or the destination
    file itself) would otherwise let a write land outside `root` entirely.

    Call this BEFORE creating any parent directory and BEFORE opening the
    destination for writing — the whole point is to refuse before any bytes
    move, not to clean up after.
    """
    root_path = Path(root)
    root_resolved = root_path.resolve()
    rel_parts = inode_relative_parts_anchored(target, root_path)
    if rel_parts is None:
        raise PathEscapesRootError(f"{target} is not inside configured root {root_path}")

    current = root_resolved
    for part in rel_parts:
        current = current / part
        if current.is_symlink():
            raise PathEscapesRootError(
                f"refusing to write through symlink at {current!s}: pre-existing symlinks may "
                "not redirect a write outside the configured root (path-safety, F3)"
            )

    # Belt-and-suspenders: even if no single component tested positive above
    # (e.g. a symlink target itself containing further symlinks), the fully
    # resolved destination must still land inside the resolved root.
    resolved_final = current.resolve()
    if inode_relative_parts_anchored(resolved_final, root_resolved) is None:
        raise PathEscapesRootError(
            f"{target} resolves to {resolved_final}, outside configured root {root_resolved}"
        )


def _no_follow_opener(path: str, flags: int) -> int:
    # os.O_NOFOLLOW is POSIX-only (absent on Windows); fall back to 0 (no
    # extra protection there) rather than fail the whole open on a platform
    # that doesn't support the flag — ensure_safe_write_target's component
    # walk above is the cross-platform half of this defense.
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))


def open_no_follow(path: Path, mode: str, *, encoding: str = "utf-8") -> IO[str]:
    """`open(path, mode, encoding=encoding)` that refuses (`OSError`, ELOOP)
    to open through a symlink at the FINAL path component, via `O_NOFOLLOW`.

    Defense-in-depth alongside `ensure_safe_write_target`: that function
    checks (and is called before directory creation); this closes the
    remaining TOCTOU gap where the destination *file* specifically (as
    opposed to a parent directory) is swapped for a symlink between that
    check and the actual open — the exact `SKILL.md`/`CONSTRAINTS.md`
    overwrite-through-a-symlink scenario in F3's evidence.
    """
    return open(path, mode, encoding=encoding, opener=_no_follow_opener)
