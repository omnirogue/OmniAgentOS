"""Prove a swarm can dispatch work across every supported model provider.

Uses the real ``UnifiedSpawner`` with fake supervisor/provider runners — no
live CLI traffic. Covers claude (bridge) + codex/grok/gemini/kimi/qwen (provider_exec).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omniagentos.swarm.provider_exec import SUPPORTED_PROVIDERS
from omniagentos.swarm.scheduler import SpawnRequest
from omniagentos.swarm.spawn import UnifiedSpawner

# Every lineage Grok routes: bridge + provider-exec set.
ALL_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("claude", "sonnet"),
    ("codex", "gpt-5.6-sol"),
    ("grok", "grok-4.5"),
    ("gemini", "gemini-2.5-pro"),
    ("kimi", "kimi-k2"),
    ("qwen", "qwen3-coder-plus"),
)


class _Supervisor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def spawn(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return f"ses_claude_{len(self.calls)}"


class _ProviderRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def spawn(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        provider = str(kwargs.get("provider") or "provider")
        return f"ses_{provider}_{len(self.calls)}"


class _SwarmDal:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.swarm_jsons: dict[str, dict[str, Any]] = {}
        self.attempts: dict[str, list[dict[str, Any]]] = {}

    def tasks_for_run(self, run_id: str) -> list[dict[str, Any]]:
        del run_id
        return list(self.tasks.values())

    def get_swarm_json(self, task_id: str) -> dict[str, Any] | None:
        return self.swarm_jsons.get(task_id)

    def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        return list(self.attempts.get(task_id, []))


class _SessionsDal:
    def __init__(self) -> None:
        self.idle_writes: list[tuple[str, float | None]] = []

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return None

    def set_idle_minutes(self, session_id: str, idle_minutes: float | None) -> bool:
        self.idle_writes.append((session_id, idle_minutes))
        return True


class _Reservations:
    def __init__(self) -> None:
        self.converted: list[tuple[str, str]] = []
        self.released: list[str] = []

    def convert(self, reservation_id: str, session_id: str) -> bool:
        self.converted.append((reservation_id, session_id))
        return True

    def release(self, reservation_id: str) -> bool:
        self.released.append(reservation_id)
        return True


def test_supported_providers_cover_non_claude_lineages() -> None:
    assert SUPPORTED_PROVIDERS == frozenset({"codex", "grok", "gemini", "kimi", "qwen"})
    assert {p for p, _ in ALL_PROVIDERS} == {"claude"} | set(SUPPORTED_PROVIDERS)


def test_swarm_spawns_across_all_providers(tmp_path: Path) -> None:
    """One spawn per provider: claude via supervisor, rest via provider_exec."""
    supervisor = _Supervisor()
    runner = _ProviderRunner()
    swarm_dal = _SwarmDal()
    sessions_dal = _SessionsDal()
    reservations = _Reservations()

    spawner = UnifiedSpawner(
        supervisor=supervisor,
        provider_runner=runner,
        swarm_dal=swarm_dal,
        sessions_dal=sessions_dal,
        convert_reservation=reservations.convert,
        release_reservation=reservations.release,
        var_root=tmp_path / "var" / "swarm",
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    session_ids: dict[str, str] = {}

    for provider, model in ALL_PROVIDERS:
        task_id = f"task_{provider}"
        swarm_dal.tasks[task_id] = {
            "id": task_id,
            "title": f"{provider} slice",
            "description": f"Implement the {provider} slice of the multi-provider swarm.",
        }
        swarm_dal.swarm_jsons[task_id] = {
            "task_key": provider,
            "risk_class": "internal",
            "acceptance": f"{provider} unit tests pass",
            "owned_paths": [f"src/{provider}/"],
        }
        swarm_dal.attempts[task_id] = [
            {
                "id": f"swa_{provider}",
                "seq": 0,
                "session_id": None,
                "ended_at": None,
            }
        ]
        req = SpawnRequest(
            run_id="swr_all_providers",
            task_id=task_id,
            task_key=provider,
            attempt_id=f"swa_{provider}",
            working_dir=str(workspace),
            prompt=f"Implement the {provider} slice.",
            provider=provider,
            model=model,
            tier="standard",
            account_id=f"acct_{provider}",
            idle_minutes=20.0,
            budget_usd_max=5.0,
            reservation_id=f"rsv_{provider}",
            effort="medium",
        )
        sid = spawner.spawn(req)
        session_ids[provider] = sid
        assert sid.startswith("ses_")

    # Claude went through the bridge supervisor once.
    assert len(supervisor.calls) == 1
    assert supervisor.calls[0]["model"] == "sonnet"
    assert supervisor.calls[0]["orchestrator_owned"] is True
    assert "[swarm:swa_claude]" in supervisor.calls[0]["title_prefix"]

    # Non-claude providers each hit provider_exec exactly once.
    assert len(runner.calls) == len(SUPPORTED_PROVIDERS)
    got_providers = {c["provider"] for c in runner.calls}
    assert got_providers == set(SUPPORTED_PROVIDERS)
    for call in runner.calls:
        assert call["model"]
        assert call["swarm_run_id"] == "swr_all_providers"
        assert call["account_id"] == f"acct_{call['provider']}"
        # H-05: the live CBM recommendation overrides the stale request pre-pin.
        assert call.get("effort") == "low"
        assert call.get("effort") != "medium"

    # Every provider got a session id + reservation conversion.
    assert set(session_ids) == {p for p, _ in ALL_PROVIDERS}
    assert len(reservations.converted) == len(ALL_PROVIDERS)
    assert {r[0] for r in reservations.converted} == {f"rsv_{p}" for p, _ in ALL_PROVIDERS}
    assert reservations.released == []


def test_planner_config_maps_every_lineage_to_a_provider() -> None:
    """configs/swarm.yaml router.lineage_providers covers all five lineages."""
    from omniagentos.swarm.router import lineage_provider_map

    mapping = lineage_provider_map()
    for lineage, expected in (
        ("claude", "claude"),
        ("codex", "codex"),
        ("grok", "grok"),
        ("gemini", "gemini"),
        ("kimi", "kimi"),
    ):
        assert mapping.get(lineage) == expected, f"missing/wrong map for {lineage}: {mapping}"
