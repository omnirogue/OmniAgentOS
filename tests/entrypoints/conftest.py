"""Per-test isolation for production entry-point tests.

The root suite already pins session-scoped ``OMNIAGENTOS_DB`` /
``OMNIAGENTOS_VAR`` paths. That is not enough: several process-lifetime
singletons bind on first use, and ``sessions.token.TOKEN_PATH`` is a
module-level constant resolved from ``__file__`` (not env). This conftest
rebinds every known outside seam to a fresh campaign root under ``tmp_path``
for each test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _install_campaign_rankings(home_dir: Path) -> None:
    """Minimal fusion rankings under the campaign HOME.

    ``FUSION_RANKINGS`` is ``Path.home() / .claude/fusion/model-rankings.json``.
    Without this file the router falls back to a built-in claude candidate;
    Claude bridge sessions refuse to launch when the OS sandbox is unproven
    (``bridge_session_no_sandbox_refused``). Providing sol/grok/gemini here
    routes workers through ProviderSessionRunner (bare CLI on PATH), which is
    the outside seam these tests own.
    """
    fusion = home_dir / ".claude" / "fusion"
    fusion.mkdir(parents=True, exist_ok=True)
    agents = [
        {
            "id": "sol-coder",
            "provider": "codex",
            "model": "gpt-5.6-sol",
            "role": "coder",
            "capabilityTier": "hard",
            "maxReasoning": "xhigh",
            "defaultReasoning": "high",
            "available": True,
            "warmLatencyMs": 50,
            "fastLane": False,
            "codingScore": 0.95,
            "toolUseScore": 0.9,
            "concurrency": 8,
        },
        {
            "id": "fable-coder",
            "provider": "claude",
            "model": "fable",
            "role": "coder",
            "capabilityTier": "hard",
            "maxReasoning": "high",
            "defaultReasoning": "high",
            "available": True,
            "warmLatencyMs": 50,
            "fastLane": False,
            "codingScore": 0.99,
            "toolUseScore": 0.95,
            "concurrency": 4,
        },
        {
            "id": "grok-coder",
            "provider": "grok",
            "model": "grok-4.5",
            "role": "coder",
            "capabilityTier": "moderate",
            "maxReasoning": "high",
            "defaultReasoning": "medium",
            "available": True,
            "warmLatencyMs": 50,
            "fastLane": True,
            "codingScore": 0.85,
            "toolUseScore": 0.85,
            "concurrency": 8,
        },
        {
            "id": "gemini-coder",
            "provider": "gemini",
            "model": "gemini-3.6-flash",
            "role": "coder",
            "capabilityTier": "easy",
            "maxReasoning": "medium",
            "defaultReasoning": "low",
            "available": True,
            "warmLatencyMs": 40,
            "fastLane": True,
            "codingScore": 0.7,
            "toolUseScore": 0.7,
            "concurrency": 16,
        },
        {
            "id": "opus-coder",
            "provider": "claude",
            "model": "claude-opus-5",
            "role": "coder",
            "capabilityTier": "hard",
            "maxReasoning": "high",
            "defaultReasoning": "high",
            "available": True,
            "warmLatencyMs": 50,
            "fastLane": False,
            "codingScore": 0.93,
            "toolUseScore": 0.9,
            "concurrency": 4,
        },
    ]
    payload = {
        "schema": 1,
        "refreshedAt": "2026-07-28T00:00:00Z",
        "ttlHours": 24,
        "host": "entrypoint-tests",
        "providers": {},
        "agents": agents,
        "promotions": [],
    }
    (fusion / "model-rankings.json").write_text(
        __import__("json").dumps(payload), encoding="utf-8"
    )


@pytest.fixture
def campaign_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated campaign root: DB, var, ledger, HOME-adjacent token path.

    Also clears process-lifetime singletons so they re-read ``OMNIAGENTOS_DB``
    for *this* campaign. That is isolation hygiene, not a fake of the
    scheduler/router decision logic — the production classes are still used.
    """
    root = tmp_path / "campaign"
    db_path = root / "state.sqlite3"
    var_dir = root / "var"
    ledger_dir = root / "ledger"
    home_dir = root / "home"
    for path in (var_dir, ledger_dir, home_dir, root / "bin"):
        path.mkdir(parents=True, exist_ok=True)

    # A launched runtime root always carries the connector registry: scripts/
    # launch-env.sh syncs configs/connectors.yaml into $OMNIAGENTOS_VAR_DIR on
    # every load, and connectors._default_registry_path() reads it from there.
    # Without it the API's boot assertion (simgate.assert_registry_truth) refuses
    # to start, which is correct behaviour for a var dir no launcher produced —
    # so the fixture has to produce it the way the launcher does.
    _REPO_REGISTRY = Path(__file__).resolve().parents[2] / "configs" / "connectors.yaml"
    (var_dir / "connectors.yaml").write_bytes(_REPO_REGISTRY.read_bytes())

    monkeypatch.setenv("OMNIAGENTOS_DB", str(db_path))
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(var_dir))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_dir))
    monkeypatch.setenv("OMNIAGENTOS_LEDGER_DIR", str(ledger_dir))
    monkeypatch.setenv("HOME", str(home_dir))
    # Crash-recovery daemon: no artificial delay in tests.
    monkeypatch.setenv("OMNIAGENTOS_ORCH_RESUME_STARTUP_DELAY_SECONDS", "0")
    monkeypatch.setenv("OMNIAGENTOS_ORCH_RESUME_ON_STARTUP", "1")
    # Keep worktrees off so the HTTP→scheduler path stays seconds-scale.
    monkeypatch.setenv("OMNIAGENTOS_SWARM_WORKTREES", "0")

    _install_campaign_rankings(home_dir)

    # TOKEN_PATH is __file__-anchored to the worktree var/secrets — redirect it.
    from omniagentos.sessions import token as token_mod

    token_path = var_dir / "secrets" / "sessions-token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(token_mod, "TOKEN_PATH", token_path)

    # FUSION_RANKINGS is a module-level Path.home() snapshot taken at import
    # time — rebinding HOME alone does not move it. Point both fusion paths
    # at the campaign file so the real SwarmRouter ranks our agents.
    from omniagentos.modelintel import config as modelintel_config

    rankings_path = home_dir / ".claude" / "fusion" / "model-rankings.json"
    monkeypatch.setattr(modelintel_config, "FUSION_RANKINGS", rankings_path)
    monkeypatch.setattr(
        modelintel_config, "FUSION_DIGEST", home_dir / ".claude" / "fusion" / "model-intel.json"
    )

    # default_swarm_var_root is __file__-anchored to <repo>/var/swarm (not
    # OMNIAGENTOS_VAR). Redirect so workbook/TASK.md writes stay in the campaign.
    from omniagentos.swarm import spawn as spawn_mod

    campaign_swarm_var = var_dir / "swarm"
    campaign_swarm_var.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(spawn_mod, "default_swarm_var_root", lambda: campaign_swarm_var)

    # Process-lifetime singletons rebind to the campaign DB on next use.
    from omniagentos.api import deps as api_deps
    from omniagentos.api.routes import swarm as swarm_routes
    from omniagentos.swarm import scheduler as scheduler_mod

    api_deps.get_store.cache_clear()
    if hasattr(api_deps, "get_policy_config"):
        api_deps.get_policy_config.cache_clear()
    swarm_routes._SWARM_DAL = None
    scheduler_mod._DEFAULT_SCHEDULER = None

    yield root

    # Drop overrides + rebind so a later suite in the same process cannot
    # inherit this campaign's paths or dependency_overrides.
    try:
        from omniagentos.api.main import app

        app.dependency_overrides.clear()
    except Exception:
        pass
    api_deps.get_store.cache_clear()
    swarm_routes._SWARM_DAL = None
    scheduler_mod._DEFAULT_SCHEDULER = None


@pytest.fixture
def auth_headers(campaign_root: Path) -> dict[str, str]:
    """Session token for routes that use ``_authorized`` (swarm router)."""
    from omniagentos.sessions import token as token_mod

    return {"X-Session-Token": token_mod.load_or_create_token()}


def install_fake_provider_clis(bin_dir: Path, log_path: Path) -> Path:
    """Write executable fake ``grok``/``codex``/``claude``/``gemini`` scripts.

    These are the legitimate outside seam: provider CLIs are launched as bare
    binary names on ``PATH``. Every invocation appends one line to ``log_path``
    so tests can prove the fake — not a real CLI — ran.

    Reviewer prompts (CrossLineageSwarmReviewer) always get a DENY verdict so
    the escalation ladder must fire. Worker prompts get a short success text.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    # Shared body: detect review vs worker by prompt content; emit the envelope
    # each adapter's ``_parse`` expects. Codex reads the prompt from stdin.
    script = f"""#!/usr/bin/env python3
import json, os, signal, sys, time

LOG = {str(log_path)!r}
name = os.path.basename(sys.argv[0])
argv = sys.argv[1:]
prompt = ""
# codex: prompt on stdin; others: -p / prompt positional fragments
if name == "codex":
    try:
        prompt = sys.stdin.read()
    except Exception:
        prompt = ""
else:
    # grok/claude/gemini pass -p <prompt>
    if "-p" in argv:
        i = argv.index("-p")
        if i + 1 < len(argv):
            prompt = argv[i + 1]
    prompt = prompt or " ".join(argv)

is_review = (
    "CONFIRM or DENY" in prompt
    or '"verdict"' in prompt
    or "cross-lineage REVIEWER" in prompt
    or "Return JSON: {{\\"verdict\\"" in prompt
)

with open(LOG, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"cli": name, "review": is_review, "ts": time.time()}}) + "\\n")

sleep_s = float(os.environ.get("FAKE_PROVIDER_SLEEP_S", "0"))
if os.environ.get("FAKE_PROVIDER_IGNORE_TERM") == "1":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if sleep_s:
    time.sleep(sleep_s)

if is_review:
    # LLM reviewer path (when it runs): always DENY so the ladder must escalate.
    body = json.dumps({{"verdict": "deny", "feedback": "entrypoint fake: scripted deny"}})
else:
    # Empty worker output → production settle path records end_reason
    # review_denied via produced_nothing (scheduler mechanical gate), which is
    # the same end_reason the LLM deny path writes. No file writes either.
    body = ""

if name == "codex":
    events = [
        {{"type": "thread.started", "thread_id": "fake-codex-thread"}},
        {{"type": "item.completed", "item": {{"type": "agent_message", "text": body}}}},
        {{"type": "turn.completed", "usage": {{
            "input_tokens": 10, "cached_input_tokens": 0,
            "output_tokens": 5, "reasoning_output_tokens": 0,
        }}}},
    ]
    sys.stdout.write("\\n".join(json.dumps(e) for e in events) + "\\n")
elif name == "grok":
    sys.stdout.write(json.dumps({{"text": body, "sessionId": "fake-grok-session"}}) + "\\n")
elif name == "claude":
    sys.stdout.write(json.dumps({{
        "type": "result", "result": body, "session_id": "fake-claude-session",
        "num_turns": 1, "total_cost_usd": 0.0,
        "usage": {{"input_tokens": 10, "output_tokens": 5}},
    }}) + "\\n")
elif name == "gemini":
    sys.stdout.write(json.dumps({{
        "session_id": "fake-gemini-session",
        "response": body,
        "stats": {{"input_tokens": 10, "output_tokens": 5}},
    }}) + "\\n")
else:
    sys.stdout.write(body + "\\n")
sys.exit(0)
"""
    for name in ("grok", "codex", "claude", "gemini", "kimi", "qwen"):
        path = bin_dir / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    return bin_dir


@pytest.fixture
def fake_cli_path(
    campaign_root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Prepend fake provider CLIs to PATH. Returns (bin_dir, log_path)."""
    bin_dir = campaign_root / "bin"
    log_path = campaign_root / "fake-cli.log"
    install_fake_provider_clis(bin_dir, log_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # ClaudeAdapter prefers $HOME/.local/bin/claude, then OMNIAGENTOS_CLAUDE_CLI,
    # then bare "claude". Pin the override so a host install cannot win.
    monkeypatch.setenv("OMNIAGENTOS_CLAUDE_CLI", str(bin_dir / "claude"))
    return bin_dir, log_path
