"""Tier2 live probe: a seeded skill's BODY TEXT reaches the brief of a REAL
provider spawn, verbatim.

RE-POINTED BY U-C12. This test previously pinned labels-only as the expected
behaviour — it asserted the brief contained "[skills selected: <name>@1]" and
asked for nothing more, which is precisely why "no skill content ever reaches a
model" survived as a green suite for so long. The label is still asserted (it is
the index line), but the load-bearing assertion is now that a SENTINEL SENTENCE
from the seeded skill's ``content_snapshot`` appears in the persisted prompt
character-for-character, inside the untrusted-DATA fence.

The injection site is UnifiedSpawner.spawn -> _skill_context_prompt ->
_resolved_skill_prompt (omniagentos/swarm/spawn.py), which resolves bodies
through omniagentos/skills/resolve.py and fences them via
swarm.prompt_safety.fence_data_block. That exact prompt is persisted on the
session row by ProviderSessionRunner.spawn. This is the narrowest real spawn
that exercises the injection: one gemini CLI worker on a trivial reply-only
brief. Spawner/DAL construction follows tests/swarm/test_spawn.py; the live
await follows tests/swarm/test_live_all_providers.py.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

_SKILL_SLUG = "fh-tier2-probe-skill"
_AWAIT_TERMINAL_S = 120.0
# The sentinel. Distinctive enough that it cannot appear by coincidence, and
# asserted VERBATIM: a paraphrase, a summary or a label would all fail.
_SENTINEL = "Extract structural skeletons, never sentences."
_SKILL_BODY = (
    "# Skill: FH tier2 probe skill\n\n"
    "## Purpose\nFeature-health tier2 probe for skill CONTENT injection.\n\n"
    f"## Preferred Method\n1. {_SENTINEL}\n"
)


class _SwarmDalStub:
    """Task/swarm_json surface UnifiedSpawner reads (test_spawn.py idiom)."""

    def __init__(self) -> None:
        self.tasks = {
            "task1": {
                "id": "task1",
                "title": "fh tier2 skill label probe",
                "description": "Reply with the single word OK. Do not edit any files.",
                "discipline": "coding",
            }
        }
        self.swarm_jsons = {
            "task1": {
                "task_key": "probe",
                "acceptance": "replies OK",
                "risk_class": "none",
                "owned_paths": ["README.md"],
            }
        }

    def tasks_for_run(self, run_id: str) -> list[dict[str, Any]]:
        del run_id
        return list(self.tasks.values())

    def get_swarm_json(self, task_id: str) -> dict[str, Any] | None:
        return self.swarm_jsons.get(task_id)

    def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        del task_id
        return []


def _load_gemini_env() -> None:
    gemini_env = Path.home() / ".gemini" / ".env"
    if not gemini_env.exists():
        return
    for line in gemini_env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def test_skill_label_reaches_real_spawn_brief(
    fh_budget, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("gemini") is None:
        pytest.skip("gemini CLI binary not on PATH — cannot run a live skill-label spawn")
    if not (Path.home() / ".gemini").is_dir():
        pytest.skip("gemini CLI auth dir ~/.gemini absent — cannot run a live skill-label spawn")
    fh_budget.require_headroom(cli=True)

    db_path = str(tmp_path / "skills.db")
    monkeypatch.setenv("OMNIAGENTOS_DB", db_path)
    # Deterministic legacy label injection ("[skills selected: name@version]").
    monkeypatch.setenv("OMNIAGENTOS_CORAL_CONTEXT_MODE", "off")
    _load_gemini_env()

    from omniagentos.skills import list_skills, upsert_skill

    # Seed one skill_library row; category "Coding" derives domain "coding",
    # matching the task's discipline so select_skills scores a hit. The body
    # carries the sentinel and upsert_skill records its approval digest, which
    # the resolver re-verifies before any of it reaches the brief.
    upsert_skill(
        {
            "slug": _SKILL_SLUG,
            "category": "Coding",
            "subcategory": "General",
            "title": "FH tier2 probe skill",
            "summary": "Feature-health tier2 probe skill for content injection.",
            "preferred_method": "reply politely",
            "status": "active",
            "content_snapshot": _SKILL_BODY,
        }
    )
    registry = list_skills(database=db_path)
    assert any(row["name"] == _SKILL_SLUG for row in registry), registry

    from omniagentos.sessions.dal import SessionsDal
    from omniagentos.swarm.provider_exec import ProviderSessionRunner
    from omniagentos.swarm.scheduler import SpawnRequest
    from omniagentos.swarm.spawn import UnifiedSpawner

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "README.md").write_text("# fh tier2 skill probe\n", encoding="utf-8")

    sessions = SessionsDal(db_path)
    runner = ProviderSessionRunner(db_path=db_path, wall_timeout_seconds=90)
    spawner = UnifiedSpawner(
        db_path=db_path,
        provider_runner=runner,
        swarm_dal=_SwarmDalStub(),
        sessions_dal=sessions,
        convert_reservation=lambda reservation_id, session_id: True,
        release_reservation=lambda reservation_id: True,
        var_root=tmp_path / "var" / "swarm",
    )

    fh_budget.record_cli_call()
    session_id = spawner.spawn(
        SpawnRequest(
            run_id="swr-fh-tier2",
            task_id="task1",
            task_key="probe",
            attempt_id="swa-fh-tier2",
            working_dir=str(workspace),
            prompt="Reply with the single word OK. Do not edit any files.",
            provider="gemini",
            model="gemini-2.5-flash",
            tier="standard",
            account_id=None,
            idle_minutes=10.0,
            budget_usd_max=1.0,
            reservation_id=None,
        )
    )

    row = sessions.get_session(session_id)
    assert row is not None, f"spawned session {session_id} has no persisted row"
    persisted_prompt = str(row.get("prompt") or "")
    # THE assertion (U-C12): the seeded skill's BODY reached the persisted
    # brief verbatim. A label-only injection — the behaviour this test used to
    # pin — fails here.
    assert _SENTINEL in persisted_prompt, persisted_prompt[:800]
    # The index line still names what was injected, and the body is fenced as
    # untrusted DATA rather than pasted in as instructions.
    assert "[skills selected:" in persisted_prompt, persisted_prompt[:800]
    assert f"{_SKILL_SLUG}@1" in persisted_prompt, persisted_prompt[:800]
    from omniagentos.skills.resolve import SKILL_DATA_LABEL
    from omniagentos.swarm.prompt_safety import contains_data_block

    assert contains_data_block(persisted_prompt, SKILL_DATA_LABEL), persisted_prompt[:800]

    # Await the live worker's terminal state so no CLI process outlives the
    # test (the 90s wall timeout above is the backstop).
    deadline = time.monotonic() + _AWAIT_TERMINAL_S
    terminal = {"completed", "failed", "cancelled", "killed"}
    state = ""
    while time.monotonic() < deadline:
        current = sessions.get_session(session_id) or {}
        state = str(current.get("state") or "")
        if state in terminal:
            break
        time.sleep(1.0)
    assert state in terminal, (
        f"live gemini session {session_id} did not terminalize within "
        f"{_AWAIT_TERMINAL_S:.0f}s (last state {state!r})"
    )
