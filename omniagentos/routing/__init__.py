"""Account-pool routing: round-robin, rate-limit-aware selection across
multiple subscription accounts per provider, with fallback-to-next-model
composition when a whole provider is exhausted.

Public surface:

- ``omniagentos.routing.config``: ``Account``, ``AccountPoolConfig``,
  ``load_accounts_config()`` -- typed load of ``configs/accounts.yaml`` plus
  glob-based account discovery.
- ``omniagentos.routing.account_pool``: ``AccountPool`` (``pick`` /
  ``report`` / ``all_cooling``), ``Outcome``, ``classify_outcome()``,
  ``get_default_pool()``, ``account_pool_enabled()``.
- ``omniagentos.routing.cascade``: the verification-gated router cascade --
  ``run_cascade()``, ``load_ladder()``, ``CascadeTier``, ``AttemptOutcome``,
  ``CascadeAttempt``, ``CascadeResult`` (FrugalGPT arXiv:2305.05176 style).
- ``omniagentos.routing.learn``: pure trace-mining helpers --
  ``read_traces()``, ``recommend_start_tier()``, ``wilson_lower_bound()``.

See ``omniagentos/adapters/claude.py`` for the (opt-in,
``OMNIAGENTOS_ACCOUNT_POOL=1``) integration point.
"""

from __future__ import annotations

from omniagentos.routing.account_pool import (
    AccountPool,
    Outcome,
    account_pool_enabled,
    classify_outcome,
    get_default_pool,
    reset_default_pool,
)
from omniagentos.routing.cascade import (
    AttemptOutcome,
    CascadeAttempt,
    CascadeResult,
    CascadeTier,
    cascade_enabled,
    default_cascade_config_path,
    default_trace_path,
    load_ladder,
    record_trace,
    run_cascade,
)
from omniagentos.routing.config import (
    Account,
    AccountPoolConfig,
    ProviderPool,
    load_accounts_config,
)
from omniagentos.routing.decision_center import decision_center_contract
from omniagentos.routing.learn import (
    read_traces,
    recommend_start_tier,
    wilson_lower_bound,
)
from omniagentos.routing.workers import (
    WorkerEndpoint,
    WorkerSelection,
    list_terminal_workers,
    select_worker,
)

__all__ = [
    "Account",
    "AccountPool",
    "AccountPoolConfig",
    "AttemptOutcome",
    "CascadeAttempt",
    "CascadeResult",
    "CascadeTier",
    "Outcome",
    "ProviderPool",
    "WorkerEndpoint",
    "WorkerSelection",
    "account_pool_enabled",
    "cascade_enabled",
    "classify_outcome",
    "decision_center_contract",
    "default_cascade_config_path",
    "default_trace_path",
    "get_default_pool",
    "list_terminal_workers",
    "load_accounts_config",
    "load_ladder",
    "read_traces",
    "recommend_start_tier",
    "record_trace",
    "reset_default_pool",
    "run_cascade",
    "select_worker",
    "wilson_lower_bound",
]
