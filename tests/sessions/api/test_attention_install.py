"""Attention-routing hook installer: merge, idempotence, uninstall."""

from __future__ import annotations

import copy
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from omniagentos.sessions import install


@pytest.fixture(autouse=True)
def _pin_attention_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probing runs cwd-neutral, so ambient importability can't be assumed in
    tests; pin the test venv explicitly (the documented operator override) AND
    pin PYTHONPATH to THIS checkout — the estate's global PYTHONPATH shadows
    every checkout with the main tree, which lacks forward_attention_hook
    until this merges, turning these tests red for environment reasons."""
    monkeypatch.setenv("OMNI_ATTENTION_PYTHONBIN", sys.executable)
    monkeypatch.setenv("PYTHONPATH", str(Path(install.__file__).resolve().parents[2]))


def _seed_real_profile_hooks() -> dict[str, Any]:
    """A fixture shaped like a live Claude Code profile.

    Real profiles already carry Notification -> notify-owner.sh, PreToolUse
    guards, and SessionStart entries. Install must leave those untouched.
    """
    return {
        "permissions": {"allow": ["Read"]},
        "hooks": {
            "Notification": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/Users/youruser/Work/OmniAgentOS/Ops/bin/notify-owner.sh",
                        }
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "omni-guard-pretool"}],
                }
            ],
            "SessionStart": [
                {
                    "matcher": ".*",
                    "hooks": [{"type": "command", "command": "omni-session-start"}],
                }
            ],
        },
    }


def _write_settings(project: Path, settings: dict[str, Any]) -> Path:
    path = project / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return path


def _read_settings(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _attention_commands(settings: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    hooks = settings.get("hooks")
    assert isinstance(hooks, dict)
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if install._is_attention_entry(entry):
                command = entry["hooks"][0]["command"]
                found[event] = command
    return found


def test_attention_install_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write_settings(project, _seed_real_profile_hooks())

    install.install_attention_hooks(project)
    first = _read_settings(install.settings_path(project))
    install.install_attention_hooks(project)
    second = _read_settings(install.settings_path(project))
    install.install_attention_hooks(project)
    third = _read_settings(install.settings_path(project))

    assert second == first
    assert third == second
    assert json.dumps(third, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_attention_install_preserves_preexisting_entries(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    seeded = _seed_real_profile_hooks()
    originals = copy.deepcopy(seeded["hooks"])
    _write_settings(project, seeded)

    install.install_attention_hooks(project)
    installed = _read_settings(install.settings_path(project))
    hooks = installed["hooks"]

    assert originals["Notification"][0] in hooks["Notification"]
    assert hooks["Notification"][0] == originals["Notification"][0]
    assert hooks["PreToolUse"] == originals["PreToolUse"]
    assert hooks["SessionStart"] == originals["SessionStart"]
    assert installed["permissions"] == {"allow": ["Read"]}

    # The original Notification command is still the notify-owner.sh banner.
    assert "notify-owner.sh" in hooks["Notification"][0]["hooks"][0]["command"]
    assert any(install._is_attention_entry(entry) for entry in hooks["Notification"])


def test_attention_install_writes_three_event_commands(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write_settings(project, _seed_real_profile_hooks())
    install.install_attention_hooks(project)
    commands = _attention_commands(_read_settings(install.settings_path(project)))
    assert set(commands) == {"Notification", "PermissionRequest", "Stop", "SessionEnd"}
    assert "OMNI_SESSION_ATTENTION_EVENT=notification" in commands["Notification"]
    assert "OMNI_SESSION_ATTENTION_EVENT=permission_request" in commands["PermissionRequest"]
    assert "OMNI_SESSION_ATTENTION_EVENT=stop" in commands["Stop"]
    assert "OMNI_SESSION_ATTENTION_EVENT=session_end" in commands["SessionEnd"]
    for command in commands.values():
        assert "forward_attention_hook" in command
        assert "except Exception" in command
    installed = _read_settings(install.settings_path(project))
    stop_hooks = installed["hooks"]["Stop"]
    attention = next(e for e in stop_hooks if install._is_attention_entry(e))
    assert attention["hooks"][0]["timeout"] == install.ATTENTION_HOOK_TIMEOUT_S


def test_attention_uninstall_removes_only_attention_entries(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    seeded = _seed_real_profile_hooks()
    originals = copy.deepcopy(seeded["hooks"])
    _write_settings(project, seeded)

    install.install_attention_hooks(project)
    install.uninstall_attention_hooks(project)
    removed = _read_settings(install.settings_path(project))

    assert removed["hooks"]["Notification"] == originals["Notification"]
    assert removed["hooks"]["PreToolUse"] == originals["PreToolUse"]
    assert removed["hooks"]["SessionStart"] == originals["SessionStart"]
    assert "Stop" not in removed["hooks"]
    assert "SessionEnd" not in removed["hooks"]
    assert "PermissionRequest" not in removed["hooks"]
    assert not _attention_commands(removed)


def test_report_only_install_is_unchanged_by_attention_helpers(tmp_path: Path) -> None:
    """Existing install() must not grow Stop/PostToolUse entries."""
    project = tmp_path / "proj"
    path = project / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Read"]},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]},
            }
        ),
        encoding="utf-8",
    )
    install.install(project)
    install.install(project)
    installed = _read_settings(path)
    assert len(installed["hooks"]["PostToolUse"]) == 1
    assert len(installed["hooks"]["Stop"]) == 2
    assert "Notification" not in installed["hooks"]
    assert "SessionEnd" not in installed["hooks"]


def test_forward_attention_hook_posts_mapped_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNI_ATTENTION_LEDGER", str(tmp_path / "ledger.jsonl"))
    posted: list[tuple[str, dict[str, Any], dict[str, str] | None]] = []

    def fake_post(
        path: str, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        posted.append((path, payload, headers))
        return {"ok": True}

    monkeypatch.setattr("omniagentos.sessions.hook_client._post", fake_post)
    monkeypatch.setattr(
        "omniagentos.sessions.hook_client._hook_eval_headers",
        lambda: {"X-Session-Hook-Token": "scoped"},
    )
    monkeypatch.setenv(install.ATTENTION_EVENT_ENV, "stop")
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "abc",
                    "cwd": "/proj",
                    "message": "done",
                    "title": "t",
                }
            )
        ),
    )
    install.forward_attention_hook()
    assert len(posted) == 1
    path, payload, headers = posted[0]
    assert path == "/api/session-events/hook"
    assert payload["event"] == "stop"
    assert payload["session_id"] == "abc"
    assert payload["cwd"] == "/proj"
    assert payload["message"] == "done"
    assert headers == {"X-Session-Hook-Token": "scoped"}


def test_uninstall_keeps_foreign_hook_in_compound_matcher(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    compound = {
        "matcher": ".*",
        "hooks": [
            {"type": "command", "command": "estate-host-guard"},
            {
                "type": "command",
                "command": (
                    "OMNI_SESSION_ATTENTION_EVENT=stop "
                    "python -c 'from omniagentos.sessions.install "
                    "import forward_attention_hook as _f; _f()'"
                ),
            },
        ],
    }
    _write_settings(
        project,
        {"hooks": {"Stop": [compound], "Notification": [], "SessionEnd": []}},
    )
    install.uninstall_attention_hooks(project)
    removed = _read_settings(install.settings_path(project))
    stop = removed["hooks"]["Stop"]
    assert len(stop) == 1
    assert stop[0]["matcher"] == ".*"
    assert stop[0]["hooks"] == [{"type": "command", "command": "estate-host-guard"}]
    assert not any(install._is_attention_hook(hook) for hook in stop[0]["hooks"])


def test_install_writes_dated_attention_bak(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    seeded = _seed_real_profile_hooks()
    path = _write_settings(project, seeded)
    original = path.read_text(encoding="utf-8")
    install.install_attention_hooks(project)
    bak = install._attention_backup_path(path)
    assert bak.is_file()
    assert bak.name.startswith("settings.json.bak-attention-")
    assert bak.read_text(encoding="utf-8") == original
    live = json.loads(path.read_text(encoding="utf-8"))
    live["hooks"].setdefault("PreToolUse", [])[0]["hooks"].append(
        {"type": "command", "command": "NEW-guard.py"}
    )
    path.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")
    install.install_attention_hooks(project)
    # Write-once per day: the second save must NOT overwrite the pristine
    # pre-mutation snapshot (an operator restoring "the backup" needs the
    # state before ANY attention mutation, not an intermediate one).
    assert bak.read_text(encoding="utf-8") == original
    assert "NEW-guard.py" not in bak.read_text(encoding="utf-8")


def test_forward_attention_hook_does_not_post_garbage_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNI_ATTENTION_LEDGER", str(tmp_path / "ledger.jsonl"))
    posted: list[object] = []
    monkeypatch.setattr(
        "omniagentos.sessions.hook_client._post",
        lambda *a, **k: posted.append((a, k)) or {"ok": True},
    )
    monkeypatch.setattr(
        "omniagentos.sessions.hook_client._hook_eval_headers",
        lambda: {"X-Session-Hook-Token": "scoped"},
    )
    monkeypatch.setenv(install.ATTENTION_EVENT_ENV, "notification")
    monkeypatch.setattr(sys, "stdin", io.StringIO("this is not json {"))
    install.forward_attention_hook()
    assert posted == []


def test_forward_attention_hook_does_not_post_empty_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNI_ATTENTION_LEDGER", str(tmp_path / "ledger.jsonl"))
    posted: list[object] = []
    monkeypatch.setattr(
        "omniagentos.sessions.hook_client._post",
        lambda *a, **k: posted.append((a, k)) or {"ok": True},
    )
    monkeypatch.setenv(install.ATTENTION_EVENT_ENV, "notification")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    install.forward_attention_hook()
    assert posted == []


def test_settings_path_project_and_config_dir_and_refusal(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write_settings(project, _seed_real_profile_hooks())
    assert install.settings_path(project) == project / ".claude" / "settings.json"

    profile = tmp_path / ".claude-account-5"
    profile.mkdir()
    (profile / "settings.json").write_text("{}", encoding="utf-8")
    assert install.settings_path(profile) == profile / "settings.json"
    written = install.install_attention_hooks(profile)
    assert written == profile / "settings.json"
    assert not (profile / ".claude" / "settings.json").exists()
    assert "Notification" in json.loads(written.read_text(encoding="utf-8"))["hooks"]

    stray = tmp_path / "notes.txt"
    stray.write_text("nope", encoding="utf-8")
    with pytest.raises(install.SettingsPathError, match="refused settings path"):
        install.settings_path(stray)


def test_attention_command_does_not_bake_worktree_venv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an operator override, the candidate loop must never bake an
    ephemeral worktree venv (the override fixture is cleared: it deliberately
    bypasses the ephemeral heuristic, which is exactly what this test guards)."""
    monkeypatch.delenv("OMNI_ATTENTION_PYTHONBIN", raising=False)
    monkeypatch.setattr(install, "_interpreter_reaches_hook", lambda c: True)
    command = install._attention_command("stop")
    assert "/Work/worktrees/scp-hooks-0815/.venv/" not in command
    assert "/worktrees/" not in command
    assert "except Exception" in command


def test_forward_maps_notification_type_permission_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNI_ATTENTION_LEDGER", str(tmp_path / "ledger.jsonl"))
    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "omniagentos.sessions.hook_client._post",
        lambda path, body, headers=None: posted.append(body) or {"ok": True},
    )
    monkeypatch.setattr(
        "omniagentos.sessions.hook_client._hook_eval_headers",
        lambda: {"X-Session-Hook-Token": "scoped"},
    )
    monkeypatch.setenv(install.ATTENTION_EVENT_ENV, "notification")
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "Notification",
                    "session_id": "aaaa",
                    "cwd": "/p",
                    "message": "needs Bash",
                    "notification_type": "permission_request",
                }
            )
        ),
    )
    install.forward_attention_hook()
    assert posted[0]["event"] == "permission_request"
    ledger = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8")
    assert '"outcome":"ok"' in ledger


def test_preflight_attention_rollout_on_fixture(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write_settings(project, _seed_real_profile_hooks())
    called = {"n": 0}

    def invoker() -> bool:
        called["n"] += 1
        return True

    result = install.preflight_attention_rollout(project, hook_invoker=invoker)
    assert result["guards_preserved"] is True
    assert called["n"] == 1
    assert "notify-owner.sh" in " ".join(result["guards_before"])


def test_merge_attention_hooks_is_pure_dict_api() -> None:
    settings = _seed_real_profile_hooks()
    notify_before = copy.deepcopy(settings["hooks"]["Notification"])
    install.merge_attention_hooks(settings)
    install.merge_attention_hooks(settings)
    assert settings["hooks"]["Notification"][0] == notify_before[0]
    assert (
        sum(1 for entry in settings["hooks"]["Notification"] if install._is_attention_entry(entry))
        == 1
    )


def test_settings_path_refuses_unresolvable_dirs(tmp_path: Path) -> None:
    """Neither-layout paths must refuse, never return a decoy path (F1c)."""
    plain = tmp_path / "just-a-dir"
    plain.mkdir()
    with pytest.raises(install.SettingsPathError):
        install.settings_path(plain)
    # Plausible profile typo: missing the leading dot, no settings.json inside.
    typo = tmp_path / "claude-account-5"
    typo.mkdir()
    with pytest.raises(install.SettingsPathError):
        install.settings_path(typo)
    with pytest.raises(install.SettingsPathError):
        install.settings_path(tmp_path / "does" / "not" / "exist")
    # And no decoy files were created anywhere by the refusals.
    assert not (plain / ".claude").exists()
    assert not (typo / ".claude").exists()


def test_attention_interpreter_probes_and_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The interpreter is chosen by probe, and all-fail refuses loudly (F4/N1)."""
    monkeypatch.delenv("OMNI_ATTENTION_PYTHONBIN", raising=False)
    fake_candidates = ["/durable/python3", "/other/python3"]
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    monkeypatch.setattr(install.sys, "executable", fake_candidates[0])

    probed: list[str] = []

    def fake_probe(candidate: str) -> bool:
        probed.append(candidate)
        return candidate == "/durable/python3"

    monkeypatch.setattr(install, "_interpreter_reaches_hook", fake_probe)
    assert install._attention_interpreter() == "/durable/python3"
    assert probed == ["/durable/python3"]

    monkeypatch.setattr(install, "_interpreter_reaches_hook", lambda c: False)
    with pytest.raises(install.SettingsPathError):
        install._attention_interpreter()


def test_attention_interpreter_never_returns_ephemeral(monkeypatch: pytest.MonkeyPatch) -> None:
    """All-ephemeral candidates refuse; there is no candidates[-1] fallback (N1)."""
    monkeypatch.delenv("OMNI_ATTENTION_PYTHONBIN", raising=False)
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    monkeypatch.setattr(install.sys, "executable", "/x/worktrees/lane/.venv/bin/python")
    monkeypatch.setattr(install, "_interpreter_reaches_hook", lambda c: True)
    with pytest.raises(install.SettingsPathError):
        install._attention_interpreter()


def test_installed_command_writes_nothing_to_stdout(tmp_path: Path) -> None:
    """PermissionRequest is a decision surface: stdout must be empty on EVERY
    path — success, bad stdin, and import failure (which must hit the ledger)."""
    import subprocess as sp
    import sys as _sys

    ledger = tmp_path / "ledger.jsonl"
    snippet_cmd = install._attention_command("notification")
    # Extract the -c snippet by splitting on the interpreter quoting is
    # brittle; instead rebuild the snippet exactly as _attention_command does
    # and run it under two interpreters.
    assert "OMNI_SESSION_ATTENTION_EVENT=notification" in snippet_cmd

    env = {
        "OMNI_ATTENTION_LEDGER": str(ledger),
        "OMNI_SESSION_ATTENTION_EVENT": "notification",
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    # Import-failure path: the system python genuinely cannot import
    # omniagentos; stdout must stay empty and the ledger must record it.
    snippet = snippet_cmd.split(" -c ", 1)[1]
    unquoted = install.shlex.split("x " + snippet)[1]
    system_python = "/usr/bin/python3"
    if Path(system_python).exists():
        broken = sp.run(
            [system_python, "-c", unquoted],
            input="{}",
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert broken.stdout == ""
        assert broken.returncode == 0
        assert ledger.is_file()
        assert "hook_error" in ledger.read_text(encoding="utf-8")

    # Success-shaped path (import works, stdin is garbage): still no stdout.
    ok = sp.run(
        [_sys.executable, "-c", unquoted],
        input="not-json",
        capture_output=True,
        text=True,
        timeout=30,
        env={**env, "PYTHONPATH": str(Path(install.__file__).resolve().parents[2])},
    )
    assert ok.stdout == ""
    assert ok.returncode == 0


def test_attention_interpreter_override_is_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    """OMNI_ATTENTION_PYTHONBIN is honored but still probed; a dead override refuses."""
    monkeypatch.setenv("OMNI_ATTENTION_PYTHONBIN", "/nonexistent/python3")
    with pytest.raises(install.SettingsPathError):
        install._attention_interpreter()
    monkeypatch.setenv("OMNI_ATTENTION_PYTHONBIN", sys.executable)
    assert install._attention_interpreter() == sys.executable
