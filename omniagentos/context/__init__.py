"""Context compilation helpers (ghost / hermetic packets / capsule)."""

from __future__ import annotations

from omniagentos.context.capsule import (
    CapsuleContractTruncated,
    CapsuleManifest,
    CapsuleSecretSource,
    CapsuleSlice,
    CapsuleSource,
    build_capsule_manifest,
    context_capsule_mode,
    observed_sources_from_prompt,
    write_capsule_manifest,
)
from omniagentos.context.ghost import (
    AmbientLeakUnreadable,
    GhostPacket,
    assert_no_ambient_leak,
    build_ghost_packet,
)

__all__ = [
    "AmbientLeakUnreadable",
    "CapsuleContractTruncated",
    "CapsuleManifest",
    "CapsuleSecretSource",
    "CapsuleSlice",
    "CapsuleSource",
    "GhostPacket",
    "assert_no_ambient_leak",
    "build_capsule_manifest",
    "build_ghost_packet",
    "context_capsule_mode",
    "observed_sources_from_prompt",
    "write_capsule_manifest",
]
