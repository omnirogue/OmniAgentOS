"""Hermetic ghost-context assembly (U4 / Phase 9).

Workers start from an *empty* generated packet: no ambient CLAUDE.md / AGENTS.md
inheritance, no provider memory, no unrelated artifacts. Only planner-approved
layers are materialised; the full packet is fingerprinted for reproducibility.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Filenames that must never load ambiently into a governed worker.
AMBIENT_INSTRUCTION_NAMES: frozenset[str] = frozenset(
    {
        "CLAUDE.md",
        "AGENTS.md",
        "AGENT.md",
        ".cursorrules",
        "copilot-instructions.md",
    }
)


class AmbientLeakUnreadable(OSError):
    """Raised when an ambient instruction file exists but cannot be measured.

    Fail-closed: an unreadable or undecodable ambient source must never collapse
    to the same empty-list success path as a verified-clean leak scan.
    """


@dataclass(frozen=True, slots=True)
class GhostPacket:
    """Versioned, fingerprinted context packet for one worker attempt."""

    layers: tuple[tuple[str, str], ...]  # (name, body)
    fingerprint: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        parts = [f"## {name}\n{body.strip()}" for name, body in self.layers if body.strip()]
        return "\n\n".join(parts).strip() + ("\n" if parts else "")


def build_ghost_packet(
    layers: Sequence[tuple[str, str]],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> GhostPacket:
    """Assemble layers in order and fingerprint the canonical encoding."""
    normalized: list[tuple[str, str]] = [(str(n), str(b)) for n, b in layers]
    canonical = json.dumps(
        {"layers": normalized, "metadata": dict(metadata or {})},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return GhostPacket(layers=tuple(normalized), fingerprint=digest, metadata=dict(metadata or {}))


def assert_no_ambient_leak(packet_text: str, *, repo_root: Path | None = None) -> list[str]:
    """Return names of ambient instruction files whose *contents* appear in packet.

    When ``repo_root`` is provided, loads each ambient file and checks for
    exact inclusion. Without a root, only flags if the bare filename appears
    as a loaded path reference (weaker).

    Empty list means **measured** clean (no leak detected among readable ambient
    files). Missing ambient files are skipped (nothing to leak from). An ambient
    file that exists but cannot be read or decoded raises
    :class:`AmbientLeakUnreadable` — never the same ``[]`` as a clean scan.
    """
    leaks: list[str] = []
    if repo_root is not None:
        for name in sorted(AMBIENT_INSTRUCTION_NAMES):
            path = repo_root / name
            if not path.is_file():
                continue
            try:
                body = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise AmbientLeakUnreadable(
                    f"cannot verify ambient leak for {name!r}: unreadable ({exc})"
                ) from exc
            # Non-empty ambient body whose contents appear in the packet is a
            # leak — including short files. Skipping bodies under 40 chars
            # rendered full-content short leaks as a favourable clean [].
            sample = body.strip()
            if sample and sample[:200] in packet_text:
                leaks.append(name)
    else:
        for name in sorted(AMBIENT_INSTRUCTION_NAMES):
            if name in packet_text:
                leaks.append(name)
    return leaks
