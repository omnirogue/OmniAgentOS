from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from omniagentos.packgovernance.cli import main
from omniagentos.packgovernance.errors import ChecksumMismatch

PACK = """# Automation Pack

**Scope:** `Globex/OmniAgentOS`

### A1. Report-only audit

**Owner:** Bob

Read-only report generation.
"""

DECISION = """Target repository: `Globex/OmniAgentOS`

Decision:
- Approve only A1.
"""


def _write(path: Path, text: str) -> str:
    payload = text.encode()
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def test_cli_binds_and_reports_without_execution(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pack_path = tmp_path / "pack.md"
    decision_path = tmp_path / "decision.md"
    pack_sha = _write(pack_path, PACK)
    decision_sha = _write(decision_path, DECISION)

    result = main(
        [
            "--pack",
            str(pack_path),
            "--pack-sha256",
            pack_sha,
            "--decision",
            str(decision_path),
            "--decision-sha256",
            decision_sha,
            "--capability",
            "contents:read",
        ]
    )

    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["pack"]["item_ids"] == ["A1"]
    assert report["decision"]["target_repository"] == "Globex/OmniAgentOS"
    assert report["decision"]["approval_is_exclusive"] is True
    assert report["declared_capabilities"] == ["contents:read"]
    assert report["execution_performed"] is False


def test_cli_refuses_changed_pack_bytes(tmp_path: Path) -> None:
    pack_path = tmp_path / "pack.md"
    decision_path = tmp_path / "decision.md"
    pack_sha = _write(pack_path, PACK)
    decision_sha = _write(decision_path, DECISION)
    pack_path.write_text(PACK + "\nchanged", encoding="utf-8")

    with pytest.raises(ChecksumMismatch):
        main(
            [
                "--pack",
                str(pack_path),
                "--pack-sha256",
                pack_sha,
                "--decision",
                str(decision_path),
                "--decision-sha256",
                decision_sha,
            ]
        )
