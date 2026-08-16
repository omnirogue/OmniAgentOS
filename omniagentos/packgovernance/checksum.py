"""Checksum binding for untrusted intake artifacts.

Binding happens before parsing, never after: a pack whose bytes are not the
bytes the operator hashed is a different document, and nothing downstream may
see it. There is no "warn and continue" path here by design.
"""

from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path

from omniagentos.packgovernance.contracts import ArtifactKind, BoundArtifact
from omniagentos.packgovernance.errors import ChecksumMismatch, PackParseError

__all__ = [
    "MAX_ARTIFACT_BYTES",
    "bind_artifact",
    "bind_bytes",
    "matching_view",
    "sha256_bytes",
    "sha256_file",
]

# An automation pack is a document, not a corpus. The uploaded fixture is 247 KiB;
# eight megabytes is generous headroom that still bounds a hostile upload.
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024

_SHA256_LENGTH = 64
_HEX = frozenset("0123456789abcdef")

# Characters that render as nothing (or reorder what follows) in a review UI.
# They are stripped only from the MATCHING view; the raw text keeps them so an
# audit can see exactly what was uploaded.
_INVISIBLE = frozenset(
    "​‌‍⁠﻿"  # zero-width space/non-joiner/joiner/word-joiner/BOM
    "‪‫‬‭‮"  # bidirectional embedding and override
    "⁦⁧⁨⁩"  # bidirectional isolates
    "­"  # soft hyphen
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Hash a file in bounded chunks without loading it whole."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_declared(declared_sha256: str) -> str:
    normalized = declared_sha256.strip().lower()
    if len(normalized) != _SHA256_LENGTH or not set(normalized) <= _HEX:
        raise ChecksumMismatch(
            f"declared checksum is not a sha256 hex digest: {declared_sha256!r}"
        )
    return normalized


def _decode(payload: bytes, path: str) -> tuple[str, bool]:
    if b"\x00" in payload:
        raise PackParseError(f"{path}: artifact contains NUL bytes; refusing to parse")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - exercised via fixtures
        raise PackParseError(f"{path}: artifact is not valid UTF-8 ({exc.reason})") from exc
    normalized_endings = "\r\n" in text or "\r" in text
    if normalized_endings:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, normalized_endings


def bind_bytes(
    payload: bytes,
    *,
    path: str,
    kind: ArtifactKind,
    declared_sha256: str,
) -> BoundArtifact:
    """Bind in-memory bytes to a declared checksum.

    Raises :class:`ChecksumMismatch` when the bytes are not the declared bytes,
    and :class:`PackParseError` when they are not decodable text at all.
    """
    expected = _validate_declared(declared_sha256)
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise PackParseError(
            f"{path}: artifact is {len(payload)} bytes, over the {MAX_ARTIFACT_BYTES} limit"
        )
    actual = sha256_bytes(payload)
    if actual != expected:
        raise ChecksumMismatch(
            f"{path}: checksum mismatch — declared {expected}, actual {actual}"
        )
    text, normalized_endings = _decode(payload, path)
    return BoundArtifact(
        path=path,
        kind=kind,
        sha256=actual,
        byte_length=len(payload),
        text=text,
        normalized_line_endings=normalized_endings,
    )


def bind_artifact(
    path: str | Path,
    *,
    kind: ArtifactKind,
    declared_sha256: str,
) -> BoundArtifact:
    """Read a file and bind it to a declared checksum, or refuse."""
    resolved = Path(path)
    if not resolved.is_file():
        raise PackParseError(f"{resolved}: intake artifact is missing")
    payload = resolved.read_bytes()
    return bind_bytes(payload, path=str(resolved), kind=kind, declared_sha256=declared_sha256)


def matching_view(text: str) -> str:
    """Return the text used for pattern matching only.

    Invisible and bidirectional-override characters are removed and the string is
    NFKC-normalised so that ``git​push`` and ``ｇit push`` cannot slip a verb
    past the capability classifier. The raw text is never modified.
    """
    stripped = "".join(ch for ch in text if ch not in _INVISIBLE)
    return unicodedata.normalize("NFKC", stripped)
