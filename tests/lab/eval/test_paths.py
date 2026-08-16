from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from omniagentos.lab.eval.paths import (
    PROTECTED_DB_ENV,
    assert_env_scrubbed,
    default_protected_db_path,
    scrubbed_env,
)


def test_default_protected_db_path_is_separate_from_shared_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(PROTECTED_DB_ENV, raising=False)
    monkeypatch.setattr("omniagentos.lab.eval.paths._DEFAULT_PROTECTED_DB_PATH", None)
    from omniagentos.contracts import default_db_path
    from omniagentos.lab.eval.paths import _repo_root

    protected = default_protected_db_path()
    shared = default_db_path()
    assert protected != shared
    assert protected == default_protected_db_path()
    protected_path = Path(protected).resolve()
    repo_root = Path(_repo_root()).resolve()
    # Containment must be path-component aware.  Aggregate runs intentionally
    # use a sibling such as ``backend-tmp``; it lexically starts with
    # ``backend`` but is not inside the checkout.
    assert not protected_path.is_relative_to(repo_root)


def test_default_protected_db_path_honors_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROTECTED_DB_ENV, "/tmp/somewhere/custom_protected.db")
    assert default_protected_db_path() == "/tmp/somewhere/custom_protected.db"


def test_scrubbed_env_strips_only_the_protected_pointer() -> None:
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/agent",
        PROTECTED_DB_ENV: "/secret/path/eval_protected.db",
        "OTHER_KEY": "unrelated",
    }
    scrubbed = scrubbed_env(base)
    assert PROTECTED_DB_ENV not in scrubbed
    assert scrubbed == {"PATH": "/usr/bin", "HOME": "/home/agent", "OTHER_KEY": "unrelated"}
    # original mapping is untouched (no accidental mutation of the caller's dict)
    assert PROTECTED_DB_ENV in base


def test_scrubbed_env_defaults_to_the_real_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PROTECTED_DB_ENV, "/wherever.db")
    monkeypatch.setenv("SOME_OTHER_VAR", "kept")
    scrubbed = scrubbed_env()
    assert PROTECTED_DB_ENV not in scrubbed
    assert scrubbed["SOME_OTHER_VAR"] == "kept"


def test_assert_env_scrubbed_raises_when_the_pointer_is_still_present() -> None:
    with pytest.raises(RuntimeError, match=PROTECTED_DB_ENV):
        assert_env_scrubbed({PROTECTED_DB_ENV: "/x"})


def test_assert_env_scrubbed_passes_a_clean_env() -> None:
    assert_env_scrubbed({"PATH": "/usr/bin"})  # must not raise


def test_default_protected_directory_is_removed_at_process_exit() -> None:
    env = os.environ.copy()
    env.pop(PROTECTED_DB_ENV, None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omniagentos.lab.eval.paths import default_protected_db_path; "
            "print(default_protected_db_path())",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    protected_path = Path(completed.stdout.strip())
    assert not protected_path.parent.exists()
