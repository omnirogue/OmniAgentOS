"""Fail-closed errors for automation-pack governance.

Every error here means "refuse and explain", never "continue with a guess".
A pack is untrusted input and an operator decision is authority: neither may be
partially applied, so a parse or binding failure is terminal for that artifact.
"""

from __future__ import annotations

__all__ = [
    "AuthorityError",
    "ChecksumMismatch",
    "LineageError",
    "PackGovernanceError",
    "PackParseError",
]


class PackGovernanceError(ValueError):
    """Base error: an untrusted artifact could not be bound, parsed or governed."""


class ChecksumMismatch(PackGovernanceError):
    """The bytes on disk do not match the checksum the caller declared."""


class PackParseError(PackGovernanceError):
    """The pack could not be parsed into closed typed records."""


class AuthorityError(PackGovernanceError):
    """The operator decision could not be read as an unambiguous authority record."""


class LineageError(PackGovernanceError):
    """Repository identity, lineage or branch placement could not be resolved."""
