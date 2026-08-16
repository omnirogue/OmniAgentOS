"""``run_curation_agent`` -- an OPTIONAL, opt-in live Sonnet-high pass over
`curation_prompt`'s context (contracts/lab-interfaces.md §L06-curator).

`curate()` is fully self-sufficient without this: the leaderboard/judge-notes
digest/playbook rollup it computes and (non-dry-run) persists/writes is
correct whether or not a model ever sees `curation_prompt`'s output. This
function exists to ADDITIONALLY invoke a real Sonnet-high pass for a nicer
narrative -- gated behind an explicit opt-in env var so it can never fire
during tests/CI/``--dry-run``, and always run with a SCRUBBED environment
(BINDING, HD-001) since `AgentAdapter` subprocesses inherit the calling
process's environment verbatim (see `omniagentos.lab.curator.env`).

Not wired by default: an automated 2x/day launchd job silently attempting a
live CLI call (needing real credentials/network) is exactly the kind of
surprise this repo avoids elsewhere (autocommit is flag-gated OFF by default
too). An operator turns this on once their environment is ready for it.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omniagentos.contracts import AgentResult

_LIVE_AGENT_ENV = "OMNIAGENTOS_CURATOR_LIVE_AGENT"
_DEFAULT_HARNESS = "cli-claude"
_DEFAULT_MODEL = "claude-sonnet-high"
_TASK_ID = "lab-curator"


def live_agent_enabled() -> bool:
    """True only when the operator explicitly opted in (never on by default)."""
    return os.environ.get(_LIVE_AGENT_ENV) == "1"


def run_curation_agent(
    prompt: str,
    *,
    dry_run: bool = False,
    harness: str = _DEFAULT_HARNESS,
    model: str = _DEFAULT_MODEL,
    run_id: str | None = None,
    working_dir: str | None = None,
) -> AgentResult | None:
    """Invoke the curator agent with *prompt* in a scrubbed environment.

    Returns ``None`` (a documented no-op) when `dry_run` is True or the
    operator has not opted in via ``OMNIAGENTOS_CURATOR_LIVE_AGENT=1`` --
    `curate()`'s own deterministic rollup already stands on its own. Otherwise
    resolves an `AgentAdapter` (the H1 registry) and runs it with
    `omniagentos.lab.curator.env.scrubbed_environ` in effect for the duration
    of the call, so the child process cannot read the L02 protected-store
    pointer even if it happened to be set in this process's environment.
    """
    if dry_run or not live_agent_enabled():
        return None

    from omniagentos.adapters.registry import resolve_adapter
    from omniagentos.contracts import AgentInput, HarnessType, new_id
    from omniagentos.lab.curator.env import scrubbed_environ

    adapter = resolve_adapter(HarnessType(harness))
    agent_input = AgentInput(
        run_id=run_id or new_id("run"),
        task_id=_TASK_ID,
        prompt=prompt,
        working_dir=working_dir or os.getcwd(),
        model=model,
        metadata={"role": "lab-curator"},
    )
    with scrubbed_environ():
        return adapter.run(agent_input)
