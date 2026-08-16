"""Company labels inferred from the session's project directory."""

from __future__ import annotations

import os
from pathlib import Path

_HOME = Path.home()

# First match wins. Keep the more-specific rules above any future broad rules.
DEFAULT_MAP: tuple[tuple[str, str], ...] = (
    (str(_HOME / "Work" / "AcmeUni"), "AcmeUni"),
    (str(_HOME / "Work" / "Globex"), "Globex"),
    (str(_HOME / "Work" / "Initech"), "Initech"),
    (str(_HOME / "Work" / "Hooli"), "Hooli"),
    (str(_HOME / "OmniAgentOS"), "Ops"),
    (str(_HOME / "Work" / "Ops"), "Ops"),
    (str(_HOME / "Work" / "Personal"), "Personal"),
)


def resolve_company(project_dir: str | None, override: str | None) -> str | None:
    """Return an explicit company override or the first matching path label."""
    if override:
        return override
    if not project_dir:
        return None
    expanded = str(Path(project_dir).expanduser())
    for prefix, company in DEFAULT_MAP:
        # Boundary-checked: ~/PersonalFinance must NOT match ~/Personal.
        if expanded == prefix or expanded.startswith(prefix + os.sep):
            return company
    return None
