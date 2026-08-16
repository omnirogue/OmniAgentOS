"""Direct tests for pack parsing retention and record immutability.

Covers the reviewer-reproduced defects from PR #236: unlabeled in-item prose
was silently discarded before the classifier could scan it, and the "frozen"
records exposed mutable dicts a later phase could edit behind the checksum.
"""

from __future__ import annotations

import hashlib

import pytest

from omniagentos.packgovernance.checksum import bind_bytes
from omniagentos.packgovernance.contracts import ArtifactKind
from omniagentos.packgovernance.packparse import UNLABELED_PROSE_FIELD, parse_pack

PACK = """# Automation Pack

**Scope:** `Globex/OmniAgentOS`

### A1. Report-only audit

**Owner:** Bob

This bare line says push to main.
"""


def _pack():
    payload = PACK.encode()
    artifact = bind_bytes(
        payload,
        path="pack.md",
        kind=ArtifactKind.UNTRUSTED_PACK_FIXTURE,
        declared_sha256=hashlib.sha256(payload).hexdigest(),
    )
    return parse_pack(artifact)


def test_unlabeled_in_item_prose_is_retained() -> None:
    item = _pack().item("A1")
    assert item is not None
    assert "This bare line says push to main." in item.all_text()
    assert UNLABELED_PROSE_FIELD in item.prose


def test_pack_item_mappings_cannot_be_mutated_after_binding() -> None:
    item = _pack().item("A1")
    assert item is not None
    with pytest.raises(TypeError):
        item.metadata["owner"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        item.prose[UNLABELED_PROSE_FIELD] = None  # type: ignore[index]
