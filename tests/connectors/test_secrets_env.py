"""secrets_env loader tests (JG1-E6) — subprocess far side, zero secret logging."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_secrets_env_loads_only_when_unset_subprocess(tmp_path: Path) -> None:
    """Far side = os.environ after loader in a subprocess + zero secret bytes on streams."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    secret_value = "super-secret-token-xyz-9f3a"
    (secrets / "jira.env").write_text(
        f"# bot token\nJIRA_API_TOKEN={secret_value}\nJIRA_EMAIL=bot@example.com\n",
        encoding="utf-8",
    )
    # Pre-set email must survive unchanged; token is unset → loaded.
    script = textwrap.dedent(
        f"""
        import os
        import sys
        from omniagentos.connectors.secrets_env import load_secrets_env

        os.environ["JIRA_EMAIL"] = "preset@example.com"
        os.environ.pop("JIRA_API_TOKEN", None)
        loaded = load_secrets_env({str(secrets)!r})
        assert "JIRA_API_TOKEN" in loaded
        assert os.environ["JIRA_API_TOKEN"] == {secret_value!r}
        assert os.environ["JIRA_EMAIL"] == "preset@example.com"
        # Must not print the secret.
        print("ok keys=" + ",".join(sorted(loaded)))
        """
    )
    env = {**os.environ, "PYTHONPATH": str(Path.cwd())}
    # Drop any ambient Jira secrets so the child starts clean for the unset key.
    env.pop("JIRA_API_TOKEN", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=str(Path.cwd()),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    combined = proc.stdout + proc.stderr
    assert secret_value not in combined
    assert secret_value.encode() not in combined.encode()
    assert "ok keys=" in proc.stdout
    assert "JIRA_API_TOKEN" in proc.stdout


def test_secrets_env_preset_survives_and_missing_dir_is_noop(tmp_path: Path) -> None:
    from omniagentos.connectors.secrets_env import load_secrets_env

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "x.env").write_text("FOO_SECRET=should-not-win\n", encoding="utf-8")
    env = {"FOO_SECRET": "preset-wins"}
    loaded = load_secrets_env(secrets, environ=env)
    assert loaded == []
    assert env["FOO_SECRET"] == "preset-wins"
    assert load_secrets_env(tmp_path / "missing") == []
