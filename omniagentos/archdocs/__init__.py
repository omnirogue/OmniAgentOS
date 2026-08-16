"""Living architecture docs (§8): `ARCHI.md` + `docs/architecture/*.md` stay accurate
via deterministic regeneration, drift into agent prompts via ranked context extraction,
go stale-detectable via an embedded stamp, and are safely updatable only outside the
`## Notes (human)` section (Tier S — see `update.py` docstring for the governance note).

    from omniagentos.archdocs import load_arch_context, regenerate_archi, is_stale, check_staleness
    print(load_arch_context(["judge", "quorum"], max_tokens=400))
"""

from omniagentos.archdocs.context import load_arch_context
from omniagentos.archdocs.generate import regenerate_archi
from omniagentos.archdocs.staleness import check_staleness, is_stale
from omniagentos.archdocs.update import apply_update

__all__ = [
    "load_arch_context",
    "regenerate_archi",
    "is_stale",
    "check_staleness",
    "apply_update",
]
