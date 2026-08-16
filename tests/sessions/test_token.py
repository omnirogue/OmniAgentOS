"""Regression coverage for the bridge hook's sandboxed token read."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omniagentos.runner import sandbox
from omniagentos.sessions import token


def test_existing_token_read_does_not_attempt_a_fatal_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "session-token"
    token_path.write_text("existing-token\n", encoding="utf-8")
    monkeypatch.setattr(token, "TOKEN_PATH", token_path)

    def deny_chmod(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("sandbox denied chmod")

    monkeypatch.setattr("omniagentos.sessions.token.os.chmod", deny_chmod)

    assert token.load_or_create_token() == "existing-token"


def test_existing_token_loads_from_a_sandboxed_hook_subprocess(tmp_path: Path) -> None:
    """The hook may read an existing token even though chmod is sandbox-denied."""
    if not sandbox.sandbox_available():
        pytest.skip("requires a working macOS sandbox-exec")

    repo_root = Path(__file__).resolve().parents[2]
    token_dir = tmp_path / "outside-workspace"
    token_dir.mkdir()
    token_path = token_dir / "session-token"
    token_path.write_text("sandbox-token\n", encoding="utf-8")
    script = (
        "import sys; "
        "from pathlib import Path; "
        "from omniagentos.sessions import token; "
        "token.TOKEN_PATH = Path(sys.argv[1]); "
        "print(token.load_or_create_token())"
    )
    argv = sandbox.wrap_command(
        [sys.executable, "-c", script, str(token_path)],
        str(repo_root),
    )
    assert argv[0].endswith("sandbox-exec")

    completed = subprocess.run(
        argv,
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "sandbox-token"
