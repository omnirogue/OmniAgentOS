"""Fail-closed path validation for CSI proposal and implementation writes."""

from __future__ import annotations

import fnmatch
import os
import re
import unicodedata
from pathlib import Path, PurePosixPath

from omniagentos.csi.config import load_csi_config
from omniagentos.path_containment import inode_relative_parts_anchored

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _invalid(reason: str, path: object) -> PermissionError:
    return PermissionError(f"invalid CSI path ({reason}): {path!s}")


def repo_relative(path: str | Path, repo_root: str | Path | None = None) -> str:
    """Return one *strictly lexical* repository-relative POSIX path.

    CSI plans are a data contract, not a shell/path convenience interface.
    Absolute paths, home expansion, URI forms, alternate separators and paths
    which only become safe after normalization are rejected rather than fixed.
    ``repo_root`` is accepted for API compatibility and to make the intended
    containment boundary explicit; it is not used to relativize an input.
    """

    del repo_root
    text = os.fspath(path)
    if not isinstance(text, str):
        raise _invalid("not_text", path)
    if not text:
        raise _invalid("empty", path)
    if text != text.strip():
        raise _invalid("surrounding_whitespace", path)
    if unicodedata.normalize("NFC", text) != text:
        raise _invalid("non_canonical_unicode", path)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise _invalid("control_character", path)
    if "\\" in text:
        raise _invalid("alternate_separator", path)
    if "%" in text:
        raise _invalid("encoded_component", path)
    if text.startswith("~"):
        raise _invalid("home_expansion", path)
    if text.startswith("/") or PurePosixPath(text).is_absolute():
        raise _invalid("absolute", path)
    if _WINDOWS_DRIVE.match(text) or _URI_SCHEME.match(text):
        raise _invalid("absolute_or_uri", path)

    parts = text.split("/")
    if any(part == "" for part in parts):
        raise _invalid("empty_component", path)
    if any(part in {".", ".."} for part in parts):
        raise _invalid("dot_component", path)
    if any(part.startswith("~") for part in parts):
        raise _invalid("home_component", path)

    normalized = str(PurePosixPath(*parts))
    if normalized != text:
        raise _invalid("non_canonical", path)
    return normalized


def _norm_pattern(pattern: str) -> str:
    return str(pattern).replace("\\", "/").strip("/").casefold()


def is_frozen_path(
    path: str,
    *,
    patterns: tuple[str, ...] | None = None,
    repo_root: str | Path | None = None,
) -> bool:
    """Return ``True`` for frozen paths *and for malformed/unsafe paths*.

    Matching is case-folded so a differently-cased spelling cannot bypass a
    frozen rule on a case-sensitive checkout and then become dangerous on a
    case-insensitive checkout.
    """

    try:
        text = repo_relative(path, repo_root)
    except (PermissionError, TypeError, ValueError):
        return True
    folded = text.casefold()
    pats = patterns if patterns is not None else load_csi_config().frozen_surfaces
    for pat in pats:
        p = _norm_pattern(pat)
        if not p:
            continue
        if p.endswith("/**"):
            prefix = p[:-3]
            if folded == prefix or folded.startswith(prefix + "/"):
                return True
        if fnmatch.fnmatch(folded, p):
            return True
        if folded.endswith("/" + p) or folded == p:
            return True
    return False


def reject_frozen_paths(
    paths: list[str] | tuple[str, ...],
    *,
    patterns: tuple[str, ...] | None = None,
    repo_root: str | Path | None = None,
) -> list[str]:
    """Return paths which are frozen or are not canonical relative paths."""

    return [p for p in paths if is_frozen_path(p, patterns=patterns, repo_root=repo_root)]


def assert_writable(
    paths: list[str] | tuple[str, ...],
    *,
    patterns: tuple[str, ...] | None = None,
    repo_root: str | Path | None = None,
) -> None:
    blocked = reject_frozen_paths(
        paths,
        patterns=patterns,
        repo_root=repo_root,
    )
    if blocked:
        raise PermissionError("frozen or unsafe surface write refused: " + ", ".join(blocked[:12]))


def assert_canonical_destination(
    path: str | Path,
    *,
    root: str | Path,
    allowed_prefix: str = "vault",
) -> tuple[str, Path]:
    """Resolve and prove one destination stays under ``root/allowed_prefix``.

    This check is intentionally repeated by the low-level writer.  It catches
    existing symlink escapes; the writer additionally walks with ``O_NOFOLLOW``
    so a link swapped in after this resolution is refused as well.
    """

    rel = repo_relative(path, root)
    assert_writable([rel], repo_root=root)
    parts = PurePosixPath(rel).parts
    if len(parts) < 2 or parts[0] != allowed_prefix:
        if parts and parts[0].casefold() == allowed_prefix.casefold():
            raise PermissionError(f"non-canonical {allowed_prefix}/ prefix refused: {rel}")
        raise PermissionError(f"implement limited to {allowed_prefix}/* in foundation ship: {rel}")

    root_path = Path(root).resolve(strict=True)
    allowed_lexical = root_path / allowed_prefix
    destination = root_path.joinpath(*parts)
    allowed_real = allowed_lexical.resolve(strict=False)
    destination_real = destination.resolve(strict=False)
    if (
        inode_relative_parts_anchored(allowed_real, root_path) is None
        or inode_relative_parts_anchored(destination_real, allowed_real) is None
    ):
        raise PermissionError(f"canonical destination escapes {allowed_prefix}/: {rel}")
    if allowed_real != allowed_lexical:
        raise PermissionError(f"{allowed_prefix}/ containment root is a symlink: {allowed_lexical}")
    current = root_path
    for part in parts:
        if not current.is_dir():
            break
        try:
            names = {entry.name for entry in os.scandir(current)}
        except OSError as exc:
            raise PermissionError(f"cannot verify destination component case: {current}") from exc
        folded_matches = {name for name in names if name.casefold() == part.casefold()}
        if folded_matches and part not in folded_matches:
            raise PermissionError(
                f"non-canonical case refused at {part!r}: " + ", ".join(sorted(folded_matches))
            )
        current = current / part
    return rel, destination_real


__all__ = [
    "assert_canonical_destination",
    "assert_writable",
    "is_frozen_path",
    "reject_frozen_paths",
    "repo_relative",
]
