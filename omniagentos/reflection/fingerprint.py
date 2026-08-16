"""Content fingerprints binding a validation verdict to exact proposal content.

The validate stage runs over in-memory proposals while apply re-reads rows from
the database. Binding acceptance by id alone leaves a window where a row's
content changes between validation and apply (propose's update-existing-if-
pending path, or overlapping reflection runs) and unvalidated content
auto-applies under a validated id. Apply therefore verifies the row's
fingerprint against the one computed at validation time and skips on mismatch.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def proposal_strings(target: Any, proposed: Any) -> tuple[str, str]:
    """Normalize target/proposed exactly as propose-stage persistence does."""
    target_str = json.dumps(target) if isinstance(target, (dict, list)) else str(target)
    proposed_str = json.dumps(proposed) if isinstance(proposed, (dict, list)) else str(proposed)
    return target_str, proposed_str


def proposal_fingerprint(kind: str, target_str: str, proposed_str: str) -> str:
    """Digest over the fields apply acts on; inputs are the persisted string forms."""
    payload = f"{kind}\x00{target_str}\x00{proposed_str}".encode()
    return hashlib.sha256(payload).hexdigest()
