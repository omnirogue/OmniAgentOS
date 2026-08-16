import importlib.util
import json
import os
import pty
import subprocess
from pathlib import Path


def _patcher():
    path = Path(__file__).resolve().parents[2] / "omniagentos/fleetcap/hooks/settings-patch.py"
    spec = importlib.util.spec_from_file_location("settings_patch", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_settings_patch_preserves_existing_hooks_and_is_idempotent(tmp_path: Path) -> None:
    module = _patcher()
    settings = tmp_path / "settings.json"
    settings.write_text(
        '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"git-guard"}]}]}}'
    )
    hooks = Path("omniagentos/fleetcap/hooks").resolve()
    assert module.patch(settings, hooks) is True
    assert module.patch(settings, hooks) is False
    loaded = json.loads(settings.read_text())
    commands = [
        item["command"] for group in loaded["hooks"]["SessionStart"] for item in group["hooks"]
    ]
    assert "git-guard" in commands
    assert str(hooks / "session-start.sh") in commands


def test_settings_patch_tolerates_null_list_and_variant_entries(tmp_path: Path) -> None:
    module = _patcher()
    hooks = Path(__file__).resolve().parents[2] / "omniagentos/fleetcap/hooks"
    for index, payload in enumerate(
        ({"hooks": None}, {"hooks": []}, {"hooks": {"SessionStart": [None, {"hooks": None}]}})
    ):
        settings = tmp_path / f"settings-{index}.json"
        settings.write_text(json.dumps(payload))
        assert module.patch(settings, hooks) is True
        loaded = json.loads(settings.read_text())
        assert isinstance(loaded["hooks"]["SessionStart"], list)
        assert isinstance(loaded["hooks"]["SessionEnd"], list)


def test_hook_emits_normalized_headless_payload(tmp_path: Path) -> None:
    hook = Path(__file__).resolve().parents[2] / "omniagentos/fleetcap/hooks/session-start.sh"
    env = os.environ | {"FLEETCAP_SPOOL_DIR": str(tmp_path), "FLEETCAP_DEVICE": "test-host"}
    subprocess.run(
        [str(hook)],
        input='{"session_id":"captured","source":"startup","cwd":"/private/tmp/bld-job"}',
        text=True,
        env=env,
        check=True,
        capture_output=True,
    )
    payload = json.loads(next(tmp_path.glob("hooks-*.jsonl")).read_text())
    assert payload["interactive"] is False
    assert payload["tty"] == ""
    assert "tty_raw" in payload


def test_hook_uses_controlling_tty_when_stdio_is_piped(tmp_path: Path) -> None:
    hook = Path(__file__).resolve().parents[2] / "omniagentos/fleetcap/hooks/session-start.sh"
    env = os.environ | {"FLEETCAP_SPOOL_DIR": str(tmp_path), "FLEETCAP_DEVICE": "test-host"}
    command = f'printf \'%s\' \'{{"session_id":"pty-session","cwd":"/repo"}}\' | sh {hook} >/dev/null 2>&1'
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe("/bin/sh", ["/bin/sh", "-c", command], env)
    try:
        while os.read(fd, 1024):
            pass
    except OSError:
        pass
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    payload = json.loads(next(tmp_path.glob("hooks-*.jsonl")).read_text())
    assert payload["interactive"] is True
    from omniagentos.fleetcap.attribution import attribute

    assert (
        attribute({"cwd": "/repo", "n_user": 7, "device_owner": "emp_owner", "hook": payload})[0]
        == "human"
    )
