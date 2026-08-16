"""Simulated-429 proof (brief's "Validate" step): demonstrates, with printed
routing decisions, that a provider whose every account keeps returning
rate-limited gets rotated account-by-account and then correctly signals the
caller to fall back to the next MODEL lineage -- entirely offline, no real
API calls, no credentials anywhere in sight (only fake `/fake/*` paths).

Run with `pytest -q -s tests/routing/test_proof_429.py` to see the printed
trace; `pytest -q` (no `-s`) still runs and asserts it, just captures the
output.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

# Import order matters here (pre-existing, not introduced by this module):
# omniagentos.intake.__init__ -> omniagentos.intake.service ->
# omniagentos.api.services -> omniagentos.api -> omniagentos.api.main ->
# omniagentos.api.routes.intake -> omniagentos.intake.service is a genuine
# import cycle. Importing omniagentos.api to completion FIRST (as the full
# suite's collection order already does via tests/api/*, which sort before
# tests/intake/* and tests/routing/*) avoids re-entering
# omniagentos.intake.service while it's still mid-import. Same latent
# fragility already exists for the untouched tests/intake/test_fallback.py
# when collected in isolation -- reproduced here defensively so this file
# doesn't depend on suite-wide collection order either.
import omniagentos.api  # noqa: F401
from omniagentos.adapters.claude import ClaudeAdapter
from omniagentos.adapters.codex import CodexAdapter
from omniagentos.contracts import HarnessType
from omniagentos.intake.fallback import run_with_fallback
from omniagentos.routing.account_pool import AccountPool, Outcome, reset_default_pool
from omniagentos.routing.config import Account, AccountPoolConfig, ProviderPool
from tests.adapters.conftest import FakePopen


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _pool_config(*account_ids: str, cooldown_seconds: int = 60) -> AccountPoolConfig:
    accounts = [
        Account(id=account_id, config_dir=f"/fake/{account_id}", priority=index)
        for index, account_id in enumerate(account_ids)
    ]
    return AccountPoolConfig(
        providers={"claude": ProviderPool(cooldown_seconds=cooldown_seconds, accounts=accounts)}
    )


def test_pure_router_429_rotation_proof() -> None:
    """A, B, C every account rate-limited when tried -> pick() rotates
    A -> B -> C, then returns None once all three are cooling (the exact
    all_cooling() signal a caller uses to fall back to the next model).
    Cooldown recovery is then demonstrated by advancing the fake clock."""
    clock = FakeClock()
    pool = AccountPool(_pool_config("A", "B", "C"), now=clock)

    print("\n=== simulated-429 proof: every account rate-limited ===")
    decisions: list[str] = []
    for attempt in range(1, 5):
        account = pool.pick("claude")
        if account is None:
            line = f"attempt {attempt}: pick('claude') -> None  (all_cooling={pool.all_cooling('claude')})"
            decisions.append(line)
            print(line)
            break
        pool.report(account.id, Outcome.RATE_LIMITED)
        line = (
            f"attempt {attempt}: pick('claude') -> {account.id}  "
            f"-> simulated 429 -> report({account.id}, rate_limited)  "
            f"(all_cooling={pool.all_cooling('claude')})"
        )
        decisions.append(line)
        print(line)

    assert len(decisions) == 4
    assert decisions[0].startswith("attempt 1: pick('claude') -> A")
    assert decisions[1].startswith("attempt 2: pick('claude') -> B")
    assert decisions[2].startswith("attempt 3: pick('claude') -> C")
    assert decisions[3] == "attempt 4: pick('claude') -> None  (all_cooling=True)"
    assert pool.all_cooling("claude") is True
    print(
        ">>> signal: all_cooling('claude') is True -> caller falls back to the next MODEL lineage"
    )

    clock.advance(61)
    recovered = pool.pick("claude")
    assert recovered is not None
    assert recovered.id == "A"
    print(
        f"after cooldown clock-advance(+61s): pick('claude') -> {recovered.id}  (rotation resumed)"
    )
    print("=== proof complete ===\n")


def _install_fake_popen(monkeypatch: pytest.MonkeyPatch) -> type[FakePopen]:
    """Same wiring as the `fake_popen` fixture in tests/adapters/conftest.py,
    reproduced here because pytest fixtures aren't visible across sibling
    test directories -- see FakePopen's own docstring for what it stands in
    for."""
    from omniagentos.adapters import common

    FakePopen.queued = []
    FakePopen.commands = []
    FakePopen.prompts = []
    FakePopen.envs = []
    FakePopen.instances = {}
    FakePopen.signals = []
    monkeypatch.setattr(common.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(common.os, "getpgid", lambda pid: pid)
    # Deterministic sandbox state regardless of host: the unattended fail-
    # closed gate (sandbox_available) must pass, and argv must reach FakePopen
    # unwrapped (wrap_available False keeps the adapters' own --sandbox flags,
    # mirroring tests/adapters/conftest.py's default pin).
    from omniagentos.runner import sandbox as runner_sandbox

    monkeypatch.setattr(runner_sandbox, "sandbox_available", lambda: True)
    monkeypatch.setattr(runner_sandbox, "wrap_available", lambda command, workspace: False)
    monkeypatch.setattr(runner_sandbox, "wrap_command", lambda command, workspace, **kw: command)

    def killpg(pgid: int, signum: int) -> None:
        FakePopen.signals.append(signum)
        process = FakePopen.instances.get(pgid)
        if process is not None:
            process.terminated = True

    monkeypatch.setattr(common.os, "killpg", killpg)
    return FakePopen


def _claude_envelope(result: str) -> str:
    return json.dumps(
        {
            "type": "result",
            "result": result,
            "session_id": "claude-session",
            "num_turns": 1,
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 5, "output_tokens": 5},
        }
    )


def _codex_success_envelope(text: str) -> str:
    events = [
        {"type": "thread.started", "thread_id": "codex-thread-1"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 4}},
    ]
    return "\n".join(json.dumps(event) for event in events)


class TestComposesWithModelFallback:
    """The exact composition the brief asks for, proven end to end with the
    REAL ClaudeAdapter and CodexAdapter (only the subprocess layer is faked):
    a rate-limited Claude call retries on the next ACCOUNT first (same
    model); only once all_cooling('claude') does run_with_fallback -- totally
    UNCHANGED, its retry classification is untouched -- advance to the next
    MODEL lineage. The 'opus' attempt in the middle proves it costs ZERO
    wasted CLI spawns once the provider is already exhausted."""

    def teardown_method(self) -> None:
        reset_default_pool(None)

    def test_fable_exhausts_every_account_opus_skips_sol_serves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_ACCOUNT_POOL", "1")
        fake_popen = _install_fake_popen(monkeypatch)

        pool = AccountPool(_pool_config("A", "B", "C"))
        reset_default_pool(pool)

        claude_adapter = ClaudeAdapter()
        codex_adapter = CodexAdapter()

        def fake_resolve(harness: HarnessType) -> Any:
            if harness == HarnessType.CLI_CLAUDE:
                return claude_adapter
            if harness == HarnessType.CLI_CODEX:
                return codex_adapter
            raise AssertionError(f"unexpected harness requested: {harness}")

        monkeypatch.setattr("omniagentos.adapters.registry.resolve_adapter", fake_resolve)

        # fable tries A, B, C (all rate-limited) -> exhausted. opus is tried
        # next by run_with_fallback's existing chain, but the pool is ALREADY
        # all_cooling by then, so it makes ZERO subprocess calls. sol (codex)
        # is tried last and succeeds.
        fake_popen.queued = [
            ("", "Error: rate limit exceeded for this account", 1, False),  # fable @ A
            ("", "Error: rate limit exceeded for this account", 1, False),  # fable @ B
            ("", "Error: 429 too many requests for this account", 1, False),  # fable @ C
            (_codex_success_envelope('{"served_by": "sol"}'), "", 0, False),  # sol
        ]

        print("\n=== end-to-end proof: account exhaustion -> model fallback ===")
        # Explicit chain: bypasses default_chain_rungs()'s ambient-host
        # availability probe (shutil.which("codex") etc.) so this test does
        # not depend on which CLI binaries happen to be installed on the
        # machine running pytest -- an explicit chain string skips
        # default_chain_rungs() entirely. Mirrors the _GIT_IDENTITY_ENV
        # premise-injection pattern in test_merge_gate_trial_merge_instrument.
        result = run_with_fallback(
            "simulated prompt", {}, effort="medium", chain="fable:opus:sol"
        )
        print(f"run_with_fallback(...) -> {result}")
        print(
            f"real CLI subprocess spawns: {len(fake_popen.commands)} (3 claude accounts + 1 codex)"
        )
        print(f"pool.all_cooling('claude') after the run: {pool.all_cooling('claude')}")
        print("=== proof complete ===\n")

        assert result == {"served_by": "sol"}
        # Exactly 4 real spawns: account-1/2/3 for "fable", then codex for
        # "sol". The "opus" attempt (same CLI_CLAUDE harness, same cached
        # adapter) never touched the subprocess layer at all.
        assert len(fake_popen.commands) == 4
        assert pool.all_cooling("claude") is True
