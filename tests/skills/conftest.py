from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate
from tests.skills.fixtures_scan import (  # noqa: F401
    clean_content,
    content_with_api_key,
    content_with_base64_pipe_sh,
    content_with_credential_path,
    content_with_curl_pipe_sh,
    content_with_database_url,
    content_with_git_force_push,
    content_with_jwt_token,
    content_with_multiple_issues,
    content_with_password,
    content_with_path_traversal,
    content_with_private_key,
    content_with_rm_rf,
    content_with_sudo,
    oversized_content,
)


@pytest.fixture
def skills_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    db_path = tmp_path / "skills.db"
    vault_path = tmp_path / "vault"
    (vault_path / "playbook").mkdir(parents=True)
    monkeypatch.setenv("OMNIAGENTOS_DB", str(db_path))
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(vault_path))
    migrate(str(db_path))
    return db_path, vault_path
