"""scripts/provider-sentinel/sentinel.py -- loaded by file path, not by
normal package import: `scripts/provider-sentinel` is a hyphenated
directory (the ARCHI.md-documented launchd-job convention, matching
`scripts/fable-curator`, `scripts/archi-morning`), so
`from scripts.provider-sentinel import sentinel` is not valid Python syntax.
`importlib.util.spec_from_file_location` loads it directly instead -- the
module must be registered in ``sys.modules`` BEFORE ``exec_module`` runs, or
its own ``@dataclass`` definitions fail to resolve (dataclasses looks the
defining module up via ``sys.modules[cls.__module__]``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SENTINEL_PATH = REPO_ROOT / "scripts" / "provider-sentinel" / "sentinel.py"


def _load_sentinel_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("provider_sentinel_module", SENTINEL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def sentinel() -> ModuleType:
    return _load_sentinel_module()
