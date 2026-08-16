"""TN.12 revision loop foundation — same mode/scope, never widen write_set."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "RevisionError",
    "RevisionSpec",
    "create_revision_spec",
    "revision_write_set",
]


class RevisionError(ValueError):
    """Invalid revision inputs."""


@dataclass(frozen=True, slots=True)
class RevisionSpec:
    """Inputs for a revision attempt spawned from operator feedback."""

    mode: str
    scope: str
    lineage_id: str
    version: int
    write_set: tuple[str, ...]
    read_set: tuple[str, ...]
    feedback: str
    prior_manifest_hash: str | None = None
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "scope": self.scope,
            "lineage_id": self.lineage_id,
            "version": self.version,
            "write_set": list(self.write_set),
            "read_set": list(self.read_set),
            "feedback": self.feedback,
            "prior_manifest_hash": self.prior_manifest_hash,
            "rationale": self.rationale,
            "metadata": dict(self.metadata),
        }


def revision_write_set(
    prior_write_set: Sequence[str],
    requested_write_set: Sequence[str] | None,
) -> tuple[str, ...]:
    """Intersect requested paths with prior; never widen.

    - ``requested is None`` → prior write_set unchanged (prior order).
    - otherwise → paths present in both, preserving prior order.
    """
    prior = tuple(str(p).strip() for p in prior_write_set if str(p).strip())
    if requested_write_set is None:
        return prior
    allowed = {str(p).strip() for p in requested_write_set if str(p).strip()}
    return tuple(p for p in prior if p in allowed)


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def _path_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, Sequence):
        return tuple(str(p).strip() for p in value if str(p).strip())
    return ()


def _prior_write_set(prior_manifest: Any) -> tuple[str, ...]:
    """The prior write_set, from whichever shape carries it — normalized ONCE.

    All three branches return through :func:`_path_tuple` on purpose.
    :func:`revision_write_set` strips every path it is given, and
    :func:`create_revision_spec` then tests membership of that stripped result
    against ``set(prior_write)``; a branch that returns an unstripped path makes
    those two disagree and the caller is told its write_set "widened illegally"
    over a space character it did not send. One normalizer, so a fourth shape
    cannot reintroduce the disagreement.
    """
    explicit = _attr(prior_manifest, "write_set")
    if explicit is not None:
        return _path_tuple(explicit)
    entries = _attr(prior_manifest, "entries")
    if entries is not None:
        rels: list[str] = []
        for entry in entries:
            rel = _attr(entry, "rel_path")
            if rel:
                rels.append(str(rel))
        if rels:
            return _path_tuple(rels)
    rel_paths = _attr(prior_manifest, "rel_paths")
    if rel_paths is not None:
        return _path_tuple(rel_paths)
    return ()


def _prior_read_set(prior_manifest: Any) -> tuple[str, ...]:
    return _path_tuple(_attr(prior_manifest, "read_set", default=()))


def _derive_lineage_id(mode: str, scope: str) -> str:
    digest = hashlib.sha256(f"{mode}|{scope}|v1".encode()).hexdigest()[:16]
    return f"lin_{digest}"


def _prior_manifest_hash(prior_manifest: Any) -> str | None:
    for key in ("content_hash", "manifest_hash", "sha256", "hash"):
        value = _attr(prior_manifest, key)
        if value:
            return str(value)
    return None


def _union_read_set(
    prior_read: Sequence[str],
    requested_read_set: Sequence[str] | None,
) -> tuple[str, ...]:
    prior = tuple(str(p).strip() for p in prior_read if str(p).strip())
    if requested_read_set is None:
        return prior
    seen = set(prior)
    extra: list[str] = []
    for path in _path_tuple(requested_read_set):
        if path not in seen:
            seen.add(path)
            extra.append(path)
    return prior + tuple(extra)


def create_revision_spec(
    prior_manifest: Any,
    feedback: str,
    *,
    requested_write_set: Sequence[str] | None = None,
    requested_read_set: Sequence[str] | None = None,
    rationale: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> RevisionSpec:
    """Build a revision_spec from a prior manifest + operator feedback.

    Rules:
    - same mode/scope as prior
    - lineage_id preserved (or derived deterministically if missing)
    - version = prior.version + 1
    - write_set never widens beyond prior (intersection)
    - read_set may grow (prior first, then new paths)
    """
    text = str(feedback).strip()
    if not text:
        raise RevisionError("revision feedback must be non-empty")

    mode = str(_attr(prior_manifest, "mode", default="") or "").strip()
    scope = str(_attr(prior_manifest, "scope", default="") or "").strip()
    if not mode:
        raise RevisionError("prior_manifest.mode is required")
    if not scope:
        raise RevisionError("prior_manifest.scope is required")

    prior_version = _attr(prior_manifest, "version", default=1)
    try:
        version_i = int(prior_version)
    except (TypeError, ValueError) as exc:
        raise RevisionError(f"prior version is not an int: {prior_version!r}") from exc
    if version_i < 1:
        raise RevisionError("prior version must be >= 1")

    lineage = _attr(prior_manifest, "lineage_id")
    lineage_id = str(lineage).strip() if lineage else _derive_lineage_id(mode, scope)

    prior_write = _prior_write_set(prior_manifest)
    new_write = revision_write_set(prior_write, requested_write_set)
    prior_norm = set(prior_write)
    for path in new_write:
        if path not in prior_norm:
            raise RevisionError(f"revision write_set widened illegally: {path!r}")

    new_read = _union_read_set(_prior_read_set(prior_manifest), requested_read_set)

    return RevisionSpec(
        mode=mode,
        scope=scope,
        lineage_id=lineage_id,
        version=version_i + 1,
        write_set=new_write,
        read_set=new_read,
        feedback=text,
        prior_manifest_hash=_prior_manifest_hash(prior_manifest),
        rationale=str(rationale or "").strip(),
        metadata=dict(metadata or {}),
    )
