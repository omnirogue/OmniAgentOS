"""Semantic Versioning (SemVer) validator module."""

from __future__ import annotations


class InvalidVersion(ValueError):
    """Exception raised when a semantic version string is malformed."""

    pass


def parse_version(raw: str) -> tuple[int, int, int, tuple[str, ...]]:
    """Parse a semantic version string into a tuple."""
    if "+" in raw:
        raise InvalidVersion("Build metadata '+' is not supported.")

    parts = raw.split("-", 1)
    version_part = parts[0]
    prerelease_part = parts[1] if len(parts) > 1 else None

    v_subparts = version_part.split(".")
    if len(v_subparts) != 3:
        raise InvalidVersion("Version must have exactly 3 dot-separated parts (MAJOR.MINOR.PATCH).")

    version_ints = []
    for part in v_subparts:
        if not part.isdigit():
            raise InvalidVersion("Version parts must be numeric.")
        if part.startswith("0") and len(part) > 1:
            raise InvalidVersion("Leading zeros are not allowed in version parts.")
        version_ints.append(int(part))

    prerelease_idents = []
    if prerelease_part is not None:
        if prerelease_part == "":
            raise InvalidVersion("Prerelease part cannot be empty.")
        idents = prerelease_part.split(".")
        for ident in idents:
            if not ident:
                raise InvalidVersion("Prerelease identifier cannot be empty.")
            if not all(c.isalnum() or c == "-" for c in ident):
                raise InvalidVersion(
                    "Prerelease identifier must only contain ASCII alphanumerics and hyphens."
                )
            if ident.isdigit():
                if ident.startswith("0") and len(ident) > 1:
                    raise InvalidVersion("Numeric prerelease identifier cannot have leading zeros.")
            prerelease_idents.append(ident)

    return (version_ints[0], version_ints[1], version_ints[2], tuple(prerelease_idents))


def compare_versions(a: str, b: str) -> int:
    """Compare two semantic version strings."""
    major_a, minor_a, patch_a, pre_a = parse_version(a)
    major_b, minor_b, patch_b, pre_b = parse_version(b)

    if major_a != major_b:
        return 1 if major_a > major_b else -1
    if minor_a != minor_b:
        return 1 if minor_a > minor_b else -1
    if patch_a != patch_b:
        return 1 if patch_a > patch_b else -1

    if pre_a and not pre_b:
        return -1
    if not pre_a and pre_b:
        return 1
    if not pre_a and not pre_b:
        return 0

    # strict=False on purpose: prerelease identifier lists may differ in length, and the
    # rule is that when every compared identifier is equal the SHORTER one ranks lower --
    # decided by the length comparison after this loop. strict=True would raise instead.
    for id_a, id_b in zip(pre_a, pre_b, strict=False):
        is_num_a = id_a.isdigit()
        is_num_b = id_b.isdigit()

        if is_num_a and is_num_b:
            val_a = int(id_a)
            val_b = int(id_b)
            if val_a != val_b:
                return 1 if val_a > val_b else -1
        elif is_num_a and not is_num_b:
            return -1
        elif not is_num_a and is_num_b:
            return 1
        else:
            if id_a != id_b:
                return 1 if id_a > id_b else -1

    if len(pre_a) < len(pre_b):
        return -1
    elif len(pre_a) > len(pre_b):
        return 1
    else:
        return 0


def latest(versions: list[str]) -> str:
    """Return the latest version from a list."""
    if not versions:
        raise ValueError("List of versions cannot be empty.")

    for v in versions:
        parse_version(v)

    current_max = versions[0]
    for v in versions[1:]:
        if compare_versions(v, current_max) > 0:
            current_max = v
    return current_max
