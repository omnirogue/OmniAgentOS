"""omniagentos.lab.curator -- the 2x-daily Sonnet-high curation loop
(contracts/lab-interfaces.md §L06-curator).

Public API:
    curate(store, ledger_dir, vault_dir, *, dry_run=False) -> dict
    curation_prompt(context) -> str
    run_curation_agent(prompt, *, dry_run=False, ...) -> AgentResult | None

BINDING (design review HD-006): `curate()` NEVER calls `store.set_champion` --
it only recommends; promotion goes through `surfaces.promote()` (L03). The
curator agent (`omniagentos.lab.curator.agent`) runs with a SCRUBBED env (no
`OMNIAGENTOS_EVAL_PROTECTED`) and reads only the shared, sanitized `LabStore`
-- never the L02 protected store. The curator prompt itself is not a mutable
surface (`omniagentos.lab.curator.prompt` is not versioned via surfaces.py).

The 2x-daily loop: `scripts/curator/run.sh` installs a launchd job (venv
python, like the H1 scheduler) whose `ProgramArguments` run
``python -m omniagentos.lab.curator`` twice a day.
"""

from __future__ import annotations

from omniagentos.lab.curator.agent import live_agent_enabled, run_curation_agent
from omniagentos.lab.curator.curate import DEFAULT_TOP_N, curate
from omniagentos.lab.curator.env import (
    PROTECTED_ENV_KEY,
    is_protected_key,
    scrubbed_env,
    scrubbed_environ,
)
from omniagentos.lab.curator.prompt import curation_prompt

__all__ = [
    "DEFAULT_TOP_N",
    "PROTECTED_ENV_KEY",
    "curate",
    "curation_prompt",
    "is_protected_key",
    "live_agent_enabled",
    "run_curation_agent",
    "scrubbed_env",
    "scrubbed_environ",
]
