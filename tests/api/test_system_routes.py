"""Tests for the System transparency API (omniagentos/api/routes/system.py).

Covers the token gate, absent-file → null/empty envelopes, defensive frontmatter
parsing, malformed-jsonl skipping, launchd loop discovery, the agent-activity
filters, and the PATCH whitelist / path-traversal refusal / byte-exact body
preservation. Path resolution is monkeypatched to tmp dirs (the module reads
`_repo_root`/`_claude_dir`/`_launch_agents_dir`/`_home_git_dir` on every call for
exactly this reason), so nothing touches the real machine estate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes import system
from omniagentos.db.store import SqliteStore
from omniagentos.sessions import token


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return {"X-Session-Token": token.load_or_create_token()}


@pytest.fixture
def mem_store() -> SqliteStore:
    store = SqliteStore(":memory:")
    app.dependency_overrides[get_store] = lambda: store
    try:
        yield store
    finally:
        app.dependency_overrides.pop(get_store, None)


AGENT_MD = """---
name: opus-coder
description: A strong implementer on the Opus lineage. Second sentence.
tools: Read, Write, Edit, Bash
model: opus
category: coders
---

You are the Opus coder. Body content that must never change.
"""

CRITIC_MD = """---
name: codex-critic
description: Independent critic via Codex.
tools: Bash, Read, Grep, Glob
model: haiku
---

Run `codex exec -m gpt-5.6-sol --sandbox read-only`. Sol writes the review.
"""


def _write_agents(claude_dir: Path) -> None:
    agents = claude_dir / "agents"
    agents.mkdir(parents=True)
    (agents / "opus-coder.md").write_text(AGENT_MD, encoding="utf-8")
    (agents / "codex-critic.md").write_text(CRITIC_MD, encoding="utf-8")


# ── token gate ──────────────────────────────────────────────────────────────


def test_map_requires_token(client: TestClient) -> None:
    assert client.get("/api/system/map").status_code == 401


def test_agents_requires_token(client: TestClient, mem_store: SqliteStore) -> None:
    assert client.get("/api/system/agents").status_code == 401


def test_patch_requires_token(client: TestClient, mem_store: SqliteStore) -> None:
    resp = client.patch("/api/system/agents/opus-coder", json={"model": "sonnet"})
    assert resp.status_code == 401


# ── /map ────────────────────────────────────────────────────────────────────


def test_map_absent_files_null(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(system, "_repo_root", lambda: tmp_path)
    body = client.get("/api/system/map", headers=auth).json()
    assert body["archi_md"] is None
    assert body["diagram_mmd"] is None
    assert body["generated_at"] is None


def test_map_reads_archi_and_stamp(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stamp = (
        "<!-- archdocs:stamp git_head=abc123 max_migration=42 route_count=139 "
        "generated_at=2026-07-23T12:32:26Z -->"
    )
    (tmp_path / "ARCHI.md").write_text(f"{stamp}\n# ARCHI\n", encoding="utf-8")
    arch = tmp_path / "docs" / "architecture"
    arch.mkdir(parents=True)
    (arch / "system-map.mmd").write_text("graph TD\n  A-->B\n", encoding="utf-8")
    monkeypatch.setattr(system, "_repo_root", lambda: tmp_path)

    body = client.get("/api/system/map", headers=auth).json()
    assert "# ARCHI" in body["archi_md"]
    assert "graph TD" in body["diagram_mmd"]
    assert body["generated_at"] == "2026-07-23T12:32:26Z"


# ── /agents ─────────────────────────────────────────────────────────────────


def test_agents_frontmatter_parse(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mem_store: SqliteStore,
) -> None:
    claude = tmp_path / ".claude"
    _write_agents(claude)
    monkeypatch.setattr(system, "_claude_dir", lambda: claude)
    monkeypatch.setattr(system, "_repo_root", lambda: tmp_path)  # no improvement log

    body = client.get("/api/system/agents", headers=auth).json()
    agents = {a["name"]: a for a in body["agents"]}
    assert set(agents) == {"opus-coder", "codex-critic"}

    opus = agents["opus-coder"]
    assert opus["model"] == "opus"
    assert opus["category"] == "coders"
    assert opus["tools"] == ["Read", "Write", "Edit", "Bash"]
    assert opus["tools_count"] == 4
    assert opus["description"].startswith("A strong implementer")
    assert "never change" in opus["body_md"]

    critic = agents["codex-critic"]
    # delegated model body-detected (no frontmatter key) → read-only
    assert critic["delegated_model"] == "gpt-5.6-sol"
    assert critic["delegated_model_editable"] is False
    assert "coders" in body["categories"]
    assert "uncategorized" in body["categories"]


def test_agents_absent_dir_empty(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mem_store: SqliteStore,
) -> None:
    monkeypatch.setattr(system, "_claude_dir", lambda: tmp_path / "nope")
    monkeypatch.setattr(system, "_repo_root", lambda: tmp_path)
    body = client.get("/api/system/agents", headers=auth).json()
    assert body["agents"] == []


def test_agents_last_activated_from_db(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mem_store: SqliteStore,
) -> None:
    claude = tmp_path / ".claude"
    _write_agents(claude)
    monkeypatch.setattr(system, "_claude_dir", lambda: claude)
    monkeypatch.setattr(system, "_repo_root", lambda: tmp_path)

    mem_store._connection.execute(
        "INSERT INTO sessions (id, source, project_dir, provider, state, model, "
        "created_at, updated_at) VALUES "
        "('ses_1','bridge','/x','claude','completed','claude-opus-4-8',"
        "'2026-07-20T10:00:00Z','2026-07-20T10:00:00Z')"
    )
    mem_store._connection.execute(
        "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, seq, provider, "
        "model, started_at) VALUES "
        "('swa_1','swr_1','bt_1',1,'codex','gpt-5.6-sol','2026-07-22T09:00:00Z')"
    )

    body = client.get("/api/system/agents", headers=auth).json()
    agents = {a["name"]: a for a in body["agents"]}
    assert agents["opus-coder"]["last_activated"] == "2026-07-20T10:00:00Z"
    assert "opus" in agents["opus-coder"]["last_activated_source"]
    assert agents["codex-critic"]["last_activated"] == "2026-07-22T09:00:00Z"


# ── /skills ─────────────────────────────────────────────────────────────────


def test_skills_list(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    claude = tmp_path / ".claude"
    skills = claude / "skills"
    (skills / "fusion").mkdir(parents=True)
    (skills / "fusion" / "SKILL.md").write_text(
        "---\nname: fusion\ndescription: Fusion auto mode.\n---\n# body\n", encoding="utf-8"
    )
    (skills / "no-skill-md").mkdir()  # dir without SKILL.md → skipped
    monkeypatch.setattr(system, "_claude_dir", lambda: claude)

    body = client.get("/api/system/skills", headers=auth).json()
    assert len(body["skills"]) == 1
    assert body["skills"][0]["name"] == "fusion"
    assert body["skills"][0]["description"] == "Fusion auto mode."
    assert body["skills"][0]["path"].endswith("fusion/SKILL.md")


# ── /improvers ──────────────────────────────────────────────────────────────


def test_improvers_malformed_jsonl_skipped(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    var = tmp_path / "var"
    var.mkdir()
    (var / "improvement-log.jsonl").write_text(
        '{"ts":"2026-07-22T01:00:00Z","improver":"fable-curator","changes":[]}\n'
        "this is not json\n"
        '{"ts":"2026-07-23T01:00:00Z","improver":"fable-curator","changes":['
        '{"path":"a","kind":"skill","summary":"x"}]}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(system, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(system, "_launch_agents_dir", lambda: tmp_path / "no-la")
    monkeypatch.setattr(system, "_claude_dir", lambda: tmp_path / "no-claude")

    body = client.get("/api/system/improvers", headers=auth).json()
    log = body["improvement_log"]
    assert len(log) == 2  # malformed middle line skipped
    assert log[0]["ts"] == "2026-07-22T01:00:00Z"
    assert body["loops"] == []
    assert body["optimizer_playbook_tail"] is None


def test_improvers_loop_discovery(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts" / "fable-curator").mkdir(parents=True)
    (repo / "scripts" / "fable-curator" / "prompt.md").write_text(
        "ultracode\nYou are the nightly curator.\n", encoding="utf-8"
    )
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.omniagentos.fable-curator</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>scripts/fable-curator/fable-curator.sh</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>23</integer><key>Minute</key><integer>0</integer></dict>
</dict></plist>
"""
    (la / "com.omniagentos.fable-curator.plist").write_text(plist, encoding="utf-8")
    # An infra job that must be filtered out (no prompt, non-improver label).
    (la / "com.omniagentos.dashboard.plist").write_text(
        '<?xml version="1.0"?><plist version="1.0"><dict>'
        "<key>Label</key><string>com.omniagentos.dashboard</string></dict></plist>",
        encoding="utf-8",
    )
    monkeypatch.setattr(system, "_repo_root", lambda: repo)
    monkeypatch.setattr(system, "_launch_agents_dir", lambda: la)
    monkeypatch.setattr(system, "_claude_dir", lambda: tmp_path / "no-claude")

    body = client.get("/api/system/improvers", headers=auth).json()
    loops = {loop["label"]: loop for loop in body["loops"]}
    assert set(loops) == {"com.omniagentos.fable-curator"}
    curator = loops["com.omniagentos.fable-curator"]
    assert curator["schedule_human"] == "daily at 23:00"
    assert "nightly curator" in curator["prompt_md"]


# ── improver fleet: prompt-bearing discovery + guard entries ────────────────

_IMPROVER_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.omniagentos.golden-suite</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>scripts/golden-suite/run.sh</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>1</integer><key>Minute</key><integer>0</integer></dict>
</dict></plist>
"""

_GUARD_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.omniagentos.github-backup</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>scripts/backup/github-backup.sh</string>
  </array>
  <key>StartInterval</key><integer>3600</integer>
</dict></plist>
"""

# An infra job (no prompt, no improver/guard keyword) that must stay hidden.
_INFRA_PLIST = """<?xml version="1.0"?><plist version="1.0"><dict>
  <key>Label</key><string>com.omniagentos.sessions</string></dict></plist>
"""


def _setup_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_log: bool = True
) -> tuple[Path, Path]:
    """Build a repo + LaunchAgents estate: one prompt-bearing improver, one guard,
    one infra job; monkeypatch the module path resolvers to it. Returns (repo, prompt)."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "golden-suite").mkdir(parents=True)
    prompt = repo / "scripts" / "golden-suite" / "prompt.md"
    prompt.write_text("golden\nYou are the golden-suite runner.\n", encoding="utf-8")
    la = tmp_path / "LaunchAgents"
    la.mkdir()
    (la / "com.omniagentos.golden-suite.plist").write_text(_IMPROVER_PLIST, encoding="utf-8")
    (la / "com.omniagentos.github-backup.plist").write_text(_GUARD_PLIST, encoding="utf-8")
    (la / "com.omniagentos.sessions.plist").write_text(_INFRA_PLIST, encoding="utf-8")
    if with_log:
        (repo / "var").mkdir(parents=True, exist_ok=True)
        (repo / "var" / "improvement-log.jsonl").write_text(
            '{"ts":"2026-07-22T01:00:00Z","improver":"golden-suite","notes":"first","changes":[]}\n'
            '{"ts":"2026-07-23T01:00:00Z","improver":"golden-suite","notes":"second",'
            '"changes":[{"kind":"test","path":"x","summary":"y"}]}\n'
            '{"ts":"2026-07-23T02:00:00Z","improver":"fable-curator","notes":"other","changes":[]}\n',
            encoding="utf-8",
        )
    monkeypatch.setattr(system, "_repo_root", lambda: repo)
    monkeypatch.setattr(system, "_launch_agents_dir", lambda: la)
    monkeypatch.setattr(system, "_claude_dir", lambda: tmp_path / "no-claude")
    return repo, prompt


def test_improvers_guard_entry_and_kind(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_fleet(tmp_path, monkeypatch)
    body = client.get("/api/system/improvers", headers=auth).json()
    loops = {loop["label"]: loop for loop in body["loops"]}
    # Prompt-bearing job included; guard included; infra job hidden.
    assert set(loops) == {"com.omniagentos.golden-suite", "com.omniagentos.github-backup"}

    gold = loops["com.omniagentos.golden-suite"]
    assert gold["kind"] == "improver"
    assert gold["editable"] is True
    assert "golden-suite runner" in gold["prompt_md"]
    assert gold["next_fire"]  # calendar job → estimate present

    guard = loops["com.omniagentos.github-backup"]
    assert guard["kind"] == "guard"
    assert guard["editable"] is False
    assert guard["prompt_md"] is None
    assert guard["next_fire"]  # interval job → estimate present


def test_improvers_recent_activity_per_entry(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_fleet(tmp_path, monkeypatch)
    body = client.get("/api/system/improvers", headers=auth).json()
    loops = {loop["label"]: loop for loop in body["loops"]}

    ra = loops["com.omniagentos.golden-suite"]["recent_activity"]
    # Newest-first, only this improver's entries (fable-curator's excluded).
    assert [e["ts"] for e in ra] == ["2026-07-23T01:00:00Z", "2026-07-22T01:00:00Z"]
    assert all(e["improver"] == "golden-suite" for e in ra)
    # Guard has no matching improver-log entries.
    assert loops["com.omniagentos.github-backup"]["recent_activity"] == []


# ── PATCH /improvers/{label}/prompt ─────────────────────────────────────────


def test_patch_prompt_requires_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_fleet(tmp_path, monkeypatch)
    resp = client.patch(
        "/api/system/improvers/com.omniagentos.golden-suite/prompt", json={"content": "x"}
    )
    assert resp.status_code == 401


def test_patch_prompt_unknown_label_404(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_fleet(tmp_path, monkeypatch)
    resp = client.patch(
        "/api/system/improvers/com.omniagentos.ghost/prompt", headers=auth, json={"content": "x"}
    )
    assert resp.status_code == 404


def test_patch_prompt_guard_not_editable(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup_fleet(tmp_path, monkeypatch)
    resp = client.patch(
        "/api/system/improvers/com.omniagentos.github-backup/prompt",
        headers=auth,
        json={"content": "x"},
    )
    assert resp.status_code == 409


def test_patch_prompt_size_cap(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, prompt = _setup_fleet(tmp_path, monkeypatch)
    resp = client.patch(
        "/api/system/improvers/com.omniagentos.golden-suite/prompt",
        headers=auth,
        json={"content": "x" * (64 * 1024 + 1)},
    )
    assert resp.status_code == 413
    # File untouched by an over-cap request.
    assert "golden-suite runner" in prompt.read_text(encoding="utf-8")


def test_patch_prompt_writes_and_commits_server_resolved_path(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo, prompt = _setup_fleet(tmp_path, monkeypatch)

    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(cmd: list[str], **_kwargs: object) -> _Result:
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(system.subprocess, "run", fake_run)

    new_content = "golden\nYou are the UPDATED golden-suite runner.\n"
    # Bogus path fields in the body must be ignored — the server resolves the
    # target from launchd discovery, never the client.
    resp = client.patch(
        "/api/system/improvers/com.omniagentos.golden-suite/prompt",
        headers=auth,
        json={"content": new_content, "prompt_path": "/etc/passwd", "path": "/etc/passwd"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["committed"] is True
    assert payload["prompt_path"] == str(prompt)
    assert payload["bytes"] == len(new_content.encode("utf-8"))

    # Only the discovered file was written; the client-supplied path was ignored.
    assert prompt.read_text(encoding="utf-8") == new_content

    # git add + git commit both invoked against the repo root.
    assert ["git", "-C", str(repo), "add", str(prompt)] in calls
    commit = next(c for c in calls if "commit" in c)
    assert "system-ui: improver prompt edit com.omniagentos.golden-suite" in commit


# ── /agent-activity ─────────────────────────────────────────────────────────


def test_agent_activity_filters(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mem_store: SqliteStore,
) -> None:
    claude = tmp_path / ".claude"
    _write_agents(claude)
    monkeypatch.setattr(system, "_claude_dir", lambda: claude)
    monkeypatch.setattr(system, "_repo_root", lambda: tmp_path)

    conn = mem_store._connection
    conn.execute(
        "INSERT INTO sessions (id, source, project_dir, provider, state, model, title, "
        "created_at, updated_at) VALUES "
        "('ses_a','bridge','/x','claude','completed','claude-opus-4-8','opus job',"
        "'2026-07-20T10:00:00Z','2026-07-20T10:00:00Z')"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, project_dir, provider, state, model, title, "
        "created_at, updated_at) VALUES "
        "('ses_b','bridge','/x','claude','completed','claude-haiku-4','haiku job',"
        "'2026-07-21T10:00:00Z','2026-07-21T10:00:00Z')"
    )

    # Filter by agent: opus-coder matches only the opus session.
    body = client.get("/api/system/agent-activity?agent=opus-coder", headers=auth).json()
    assert [r["ref_id"] for r in body["activity"]] == ["ses_a"]

    # Filter by day: only the 07-21 row.
    body = client.get("/api/system/agent-activity?day=2026-07-21", headers=auth).json()
    assert [r["ref_id"] for r in body["activity"]] == ["ses_b"]

    # No filter → both sessions, newest first.
    body = client.get("/api/system/agent-activity", headers=auth).json()
    assert [r["ref_id"] for r in body["activity"]] == ["ses_b", "ses_a"]


# ── PATCH /agents/{name} ────────────────────────────────────────────────────


def test_patch_rejects_traversal(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mem_store: SqliteStore,
) -> None:
    claude = tmp_path / ".claude"
    _write_agents(claude)
    monkeypatch.setattr(system, "_claude_dir", lambda: claude)
    # A traversal-y name is rejected by the name regex before touching disk.
    resp = client.patch(
        "/api/system/agents/..%2f..%2fsecret", headers=auth, json={"model": "sonnet"}
    )
    assert resp.status_code in (404, 422)
    # A well-formed but non-existent agent → 404.
    resp = client.patch("/api/system/agents/ghost", headers=auth, json={"model": "sonnet"})
    assert resp.status_code == 404


def test_patch_rejects_bad_values(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mem_store: SqliteStore,
) -> None:
    claude = tmp_path / ".claude"
    _write_agents(claude)
    monkeypatch.setattr(system, "_claude_dir", lambda: claude)
    monkeypatch.setattr(system, "_home_git_dir", lambda: tmp_path)

    assert (
        client.patch(
            "/api/system/agents/opus-coder", headers=auth, json={"model": "gpt4"}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            "/api/system/agents/opus-coder", headers=auth, json={"effort": "ultra"}
        ).status_code
        == 422
    )
    # delegated_model body-detected (no frontmatter key) → not editable.
    resp = client.patch(
        "/api/system/agents/codex-critic", headers=auth, json={"delegated_model": "terra"}
    )
    assert resp.status_code == 409


def test_patch_rewrites_frontmatter_preserves_body(
    client: TestClient,
    auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mem_store: SqliteStore,
) -> None:
    claude = tmp_path / ".claude"
    _write_agents(claude)
    monkeypatch.setattr(system, "_claude_dir", lambda: claude)
    monkeypatch.setattr(system, "_home_git_dir", lambda: tmp_path)  # not a git repo → commit no-op

    resp = client.patch(
        "/api/system/agents/opus-coder",
        headers=auth,
        json={"model": "sonnet", "effort": "high", "category": "reviewers"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["agent"]["model"] == "sonnet"
    assert payload["agent"]["effort"] == "high"
    assert payload["agent"]["category"] == "reviewers"
    assert set(payload["changed"]) == {"model", "effort", "category"}

    # Body preserved byte-exact; effort newly appended; model rewritten in place.
    new_text = (claude / "agents" / "opus-coder.md").read_text(encoding="utf-8")
    body = new_text.split("---\n", 2)[2]
    assert body == AGENT_MD.split("---\n", 2)[2]
    assert "model: sonnet" in new_text
    assert "effort: high" in new_text
    assert "category: reviewers" in new_text
    # Untouched keys preserved.
    assert "tools: Read, Write, Edit, Bash" in new_text
