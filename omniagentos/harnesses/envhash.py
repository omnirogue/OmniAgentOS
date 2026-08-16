"""Environment hash for harness runs (FROZEN, Wave 0 r3).

Pins the execution environment identity recorded on every run
(contracts.HarnessProfile.env_hash). Deterministic for a given interpreter +
installed harness packages + platform; cheap enough to call per run.
"""

from __future__ import annotations

import platform
import sys
from importlib import metadata

from omniagentos.contracts import digest

_HARNESS_DISTS = ("mini-swe-agent", "openhands-sdk", "litellm")


def env_hash() -> str:
    parts: list[str] = [
        f"python={sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        f"platform={platform.system()}-{platform.machine()}",
    ]
    for dist in _HARNESS_DISTS:
        try:
            parts.append(f"{dist}=={metadata.version(dist)}")
        except metadata.PackageNotFoundError:
            parts.append(f"{dist}=absent")
    return digest("|".join(parts))[:16]
