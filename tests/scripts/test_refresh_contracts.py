"""`scripts/refresh-contracts.sh` must be safe to run mid-conflict-resolution.

contracts/openapi.json and contracts/fixtures/mission/v1/fixture-parity.json are
DERIVED artifacts that every route-touching lane regenerates, so every multi-lane
rebase conflicts on them with no semantic content. The documented resolution is
"take either side, then run this script" — which is only sound if the script is
idempotent. These tests hold it to that:

1. It exists, is executable, and is valid shell.
2. Its header still carries the conflict-resolution recipe (the recipe is the
   deliverable; a script nobody knows how to use fixes nothing).
3. Run on a clean tree it leaves both artifacts BYTE-identical — the property
   that makes it safe to run when you are unsure of the tree's state.
4. Run on a drifted parity file it restores the exact committed bytes, rather
   than reformatting the file (a previous ad-hoc refresh re-serialised the JSON
   and appended a trailing newline the file does not have).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "refresh-contracts.sh"
ARTIFACTS = (
    REPO_ROOT / "contracts" / "openapi.json",
    REPO_ROOT / "contracts" / "fixtures" / "mission" / "v1" / "fixture-parity.json",
)
PARITY = ARTIFACTS[1]


def _run_refresh() -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Same guard the script asserts: this checkout's sources, not an editable
    # install pointing at the product tree.
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return subprocess.run(
        [str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )


@pytest.fixture
def restored_artifacts() -> Iterator[dict[Path, bytes]]:
    """Snapshot the two artifacts and put them back whatever the test does."""
    snapshot = {path: path.read_bytes() for path in ARTIFACTS}
    try:
        yield snapshot
    finally:
        for path, content in snapshot.items():
            if path.read_bytes() != content:
                path.write_bytes(content)


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), SCRIPT
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"
    syntax = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr


def test_header_documents_the_conflict_resolution_recipe() -> None:
    header = SCRIPT.read_text()
    assert "CONFLICT-RESOLUTION RECIPE" in header
    for token in (
        "git checkout --ours",
        "contracts/openapi.json",
        "fixture-parity.json",
        "./scripts/refresh-contracts.sh",
    ):
        assert token in header, f"recipe no longer mentions {token!r}"


def test_refresh_on_a_clean_tree_is_byte_identical(
    restored_artifacts: dict[Path, bytes],
) -> None:
    """Idempotence: the property that makes the script safe to run at any time."""
    result = _run_refresh()
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    for path, content in restored_artifacts.items():
        assert path.read_bytes() == content, (
            f"{path.relative_to(REPO_ROOT)} changed on a clean tree — "
            f"the refresh is not idempotent\n{result.stdout}"
        )
    assert "unchanged  contracts/openapi.json" in result.stdout
    assert "unchanged  contracts/fixtures/mission/v1/fixture-parity.json" in result.stdout


def test_refresh_restores_a_drifted_parity_digest_byte_for_byte(
    restored_artifacts: dict[Path, bytes],
) -> None:
    """The conflict case: a stale openapi_sha is repaired, nothing else moves."""
    committed = restored_artifacts[PARITY]
    drifted = PARITY.read_text(encoding="utf-8")
    stale = drifted.replace(
        drifted.split('"openapi_sha": "')[1].split('"')[0],
        "0" * 64,
        1,
    )
    assert stale != drifted, "could not construct the drifted fixture"
    PARITY.write_text(stale, encoding="utf-8")

    result = _run_refresh()
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert PARITY.read_bytes() == committed, (
        "refresh did not restore the committed bytes exactly (formatting drift?)"
    )
    assert "openapi_sha" in result.stdout and "CHANGED" in result.stdout
    # The file genuinely has no trailing newline; preserving that is the point.
    assert not committed.endswith(b"\n")


# --- REWORK (Sol review MAJOR 6): the script must never half-write ----------
# It used to write openapi.json immediately and then run the digest stage, so a
# failure there left openapi.json updated and parity stale — the exact half-state
# the script exists to prevent, and it would strand whoever ran it mid-conflict.


def test_a_failing_digest_stage_leaves_both_artifacts_untouched(
    restored_artifacts: dict[Path, bytes],
) -> None:
    openapi = ARTIFACTS[0]

    # 1. Make openapi.json genuinely stale, so a non-atomic run WOULD rewrite it.
    #    (If it were already current, "unchanged" would prove nothing.)
    stale_openapi = b'{"stale": "would-be-regenerated-by-stage-1"}'
    openapi.write_bytes(stale_openapi)

    # 2. Break the digest stage: remove the field it must rewrite.
    broken_parity = re.sub(
        r'\s*"openapi_sha": "[0-9a-f]{64}",\n',
        "\n",
        PARITY.read_text(encoding="utf-8"),
        count=1,
    )
    assert "openapi_sha" not in broken_parity, "could not construct the broken fixture"
    PARITY.write_text(broken_parity, encoding="utf-8")

    result = _run_refresh()

    assert result.returncode != 0, "a broken parity doc must not be a success"
    assert "no rewritable 'openapi_sha'" in result.stdout + result.stderr
    assert openapi.read_bytes() == stale_openapi, (
        "openapi.json was installed even though the digest stage failed — "
        "that is the half-state this script must never produce"
    )
    assert PARITY.read_text(encoding="utf-8") == broken_parity
    assert restored_artifacts  # the fixture restores the committed bytes


def test_header_documents_atomicity() -> None:
    header = SCRIPT.read_text()
    assert "ATOMIC" in header
    assert "half-write" in header or "half-state" in header
