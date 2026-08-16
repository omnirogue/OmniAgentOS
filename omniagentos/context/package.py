"""ContextPackage — pure domain model for a dispatch context manifest.

No I/O, no DB, no network. Stdlib only. Does not import swarm/db/api.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ContextExclusion",
    "ContextItem",
    "ContextPackage",
    "ContextPackageError",
]


class ContextPackageError(ValueError):
    """Invalid ContextPackage / ContextItem / ContextExclusion payload."""


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One artifact referenced by a context package."""

    id: str
    source: str
    version: str
    freshness: str
    kind: str
    required: bool = False
    size_tokens: int = 0
    critical: bool = False
    stale_labeled: bool = False
    contradicts: tuple[str, ...] = ()
    resolved: bool = False

    def __post_init__(self) -> None:
        if not self.id or not str(self.id).strip():
            raise ContextPackageError("ContextItem id must be non-empty")
        if not self.source or not str(self.source).strip():
            raise ContextPackageError(f"ContextItem {self.id!r} source must be non-empty")
        if not self.version or not str(self.version).strip():
            raise ContextPackageError(f"ContextItem {self.id!r} version must be non-empty")
        if self.size_tokens < 0:
            raise ContextPackageError(
                f"ContextItem {self.id!r} size_tokens must be non-negative, got {self.size_tokens}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "version": self.version,
            "freshness": self.freshness,
            "kind": self.kind,
            "required": self.required,
            "size_tokens": self.size_tokens,
            "critical": self.critical,
            "stale_labeled": self.stale_labeled,
            "contradicts": list(self.contradicts),
            "resolved": self.resolved,
        }


@dataclass(frozen=True, slots=True)
class ContextExclusion:
    """§7 explicit list entry: information intentionally excluded from the package."""

    id: str
    reason: str

    def __post_init__(self) -> None:
        if not self.id or not str(self.id).strip():
            raise ContextPackageError("ContextExclusion id must be non-empty")
        if not self.reason or not str(self.reason).strip():
            raise ContextPackageError(f"ContextExclusion {self.id!r} reason must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ContextPackage:
    """Manifest of context items (and intentional exclusions) for one dispatch.

    ``content_hash`` is the package identity over the stable manifest. It sorts
    items and exclusions by ``id`` so order does not affect the digest, and it
    **excludes** ``ack_token`` — the receiving agent's post-dispatch
    acknowledgment is volatile, exactly as ``TaskContract._hash_payload()``
    drops the whole ``lease`` object so renewals do not change contract identity.
    """

    package_id: str
    items: tuple[ContextItem, ...]
    excluded: tuple[ContextExclusion, ...] = ()
    task_id: str | None = None
    ack_token: str | None = None
    created_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.package_id or not str(self.package_id).strip():
            raise ContextPackageError("package_id must be non-empty")
        item_ids = [it.id for it in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ContextPackageError("ContextPackage item ids must be unique")
        excl_ids = [ex.id for ex in self.excluded]
        if len(excl_ids) != len(set(excl_ids)):
            raise ContextPackageError("ContextPackage exclusion ids must be unique")

    def item_ids(self) -> frozenset[str]:
        return frozenset(it.id for it in self.items)

    def item(self, item_id: str) -> ContextItem | None:
        for it in self.items:
            if it.id == item_id:
                return it
        return None

    def total_tokens(self) -> int:
        return sum(it.size_tokens for it in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "items": [it.to_dict() for it in self.items],
            "excluded": [ex.to_dict() for ex in self.excluded],
            "task_id": self.task_id,
            "ack_token": self.ack_token,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    def _hash_payload(self) -> dict[str, Any]:
        """Canonical manifest for hashing: items/exclusions sorted; ack_token omitted."""
        data = self.to_dict()
        data.pop("ack_token", None)
        data["items"] = sorted(data["items"], key=lambda d: d["id"])
        data["excluded"] = sorted(data["excluded"], key=lambda d: d["id"])
        return data

    def content_hash(self) -> str:
        """SHA-256 of canonical JSON manifest (order-independent; ack_token excluded).

        Mirrors ``TaskContract.contract_hash()``: ``sort_keys=True``,
        ``ensure_ascii=False``, ``separators=(",", ":")``, ``default=str``.
        Like lease exclusion on TaskContract, ``ack_token`` is volatile and
        must not change package identity when recorded post-dispatch.
        """
        raw = json.dumps(
            self._hash_payload(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
