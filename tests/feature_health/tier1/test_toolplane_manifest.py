"""Toolplane CLI subprocess — manifest gating, envelope shape, secret scrubbing.

Spawns the real console entry (``python -m omniagentos.toolplane.cli`` — the
module behind the ``omniagentos-tool`` script; the package has no __main__)
with ``env=fh_subprocess_env`` so the pytest-pinned isolation (tmp
OMNIAGENTOS_DB / VAR_DIR / LEDGER_DIR for the observation sink) is inherited
and never a login shell re-points anything at the product var/.

The manifest allows ONLY ``vault_search`` over a tmp vault.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET = "sk-fh-secret-token-1234567890abcd"


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "moc").mkdir(parents=True)
    # The secret sits right next to the query term so the returned snippet
    # window is guaranteed to cover it — the scrub assertion is not vacuous.
    (vault / "moc" / "notes.md").write_text(
        f"# Quantumkeyword notes\n\nquantumkeyword token={SECRET} quantumkeyword detail\n",
        encoding="utf-8",
    )
    return vault


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    read_root = tmp_path / "read"
    write_root = tmp_path / "write"
    read_root.mkdir()
    write_root.mkdir()
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "run_fh_toolplane",
                "session_id": "ses_fh_toolplane",
                "holder_generation": 1,
                "read_roots": [str(read_root)],
                "write_roots": [str(write_root)],
                "allowed_ops": ["vault_search"],
            }
        ),
        encoding="utf-8",
    )
    return path


def _run_tool(
    tool: str, manifest: Path, args: dict[str, object], env: dict[str, str]
) -> tuple[int, dict[str, object], str]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omniagentos.toolplane.cli",
            tool,
            "--manifest",
            str(manifest),
            "--args-json",
            json.dumps(args),
        ],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    stdout = completed.stdout.strip()
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"CLI must print exactly one JSON object, got: {stdout!r}"
    return completed.returncode, json.loads(lines[0]), completed.stdout + completed.stderr


def test_allowed_tool_envelope_and_secret_scrubbing(
    manifest_path: Path, vault_dir: Path, fh_subprocess_env: dict[str, str]
) -> None:
    code, envelope, raw = _run_tool(
        "vault_search",
        manifest_path,
        {"query": "quantumkeyword", "vault_dir": str(vault_dir), "limit": 5},
        fh_subprocess_env,
    )

    assert code == 0
    # Envelope shape: ok + result + the durable observation record.
    assert envelope["ok"] is True
    assert isinstance(envelope["result"], list) and len(envelope["result"]) == 1
    hit = envelope["result"][0]
    assert set(hit) >= {"relpath", "title", "snippet", "score"}
    assert hit["relpath"] == "moc/notes.md"
    observation = envelope["observation"]
    assert observation["tool"] == "vault_search"
    assert observation["ok"] is True

    # The snippet covered the secret's line, and the secret never left scrubbed.
    assert "quantumkeyword" in hit["snippet"]
    assert SECRET not in raw
    assert "[REDACTED]" in hit["snippet"]


def test_tool_not_in_manifest_is_refused(
    manifest_path: Path, tmp_path: Path, fh_subprocess_env: dict[str, str]
) -> None:
    inside = tmp_path / "read" / "file.txt"
    inside.write_text("hello", encoding="utf-8")

    code, envelope, _ = _run_tool(
        "read_file", manifest_path, {"path": str(inside)}, fh_subprocess_env
    )

    # read_file is a real tool but absent from allowed_ops -> deny, nonzero exit.
    assert code == 1
    assert envelope["ok"] is False
    assert envelope["error"] == "not_allowed"


def test_unknown_tool_denied_by_default(
    manifest_path: Path, fh_subprocess_env: dict[str, str]
) -> None:
    code, envelope, _ = _run_tool(
        "totally_made_up_tool", manifest_path, {}, fh_subprocess_env
    )
    assert code == 1
    assert envelope["ok"] is False
    assert envelope["error"] == "unknown_capability"
