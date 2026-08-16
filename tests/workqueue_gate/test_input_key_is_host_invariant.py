"""SPEC §4.1: the fingerprint must be identical on every machine in the pool.

If a single host fact leaks into the key, Mac B computes a different key for the
same input, its refusal_check misses, and the entire anti-retry contract
silently evaporates — no error, no symptom, just the 28x storm coming back.

The strongest available form of that assertion is used here: the same input is
fingerprinted from two checkouts at DIFFERENT ABSOLUTE PATHS (which is what
"another machine" really means on this estate — ``/Users/...`` vs
``/home/...``), with different ``machine_id`` / ``worker_id`` / ``WQ_HOME``
environments, and the two keys must be equal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.workqueue.fingerprint import InputKeyError, input_key, input_key_from_specs
from tests.workqueue_cli.demorepo import make_checkout, make_mirror, make_repo

CMD = "python3 -c 'print(1)'"


@pytest.fixture
def pool(tmp_path: Path) -> dict[str, object]:
    repo, sha = make_repo(tmp_path / "src")
    mirror = make_mirror(repo, tmp_path / "mirrors" / "demo.git")
    gate_script = tmp_path / "accurate-gate.py"
    gate_script.write_text("# v1\n")
    gate_cfg = tmp_path / "unit-acceptance.yaml"
    gate_cfg.write_text("command: {acceptance_cmd}\n")
    return {"repo": repo, "sha": sha, "mirror": mirror, "script": gate_script, "cfg": gate_cfg}


def _key(worktree: Path, pool: dict, cmd: str = CMD) -> str:
    return input_key(
        "unit-acceptance",
        str(worktree),
        str(pool["script"]),
        [str(pool["cfg"])],
        cmd,
    )


def test_two_machines_two_paths_one_key(pool: dict, tmp_path: Path, monkeypatch) -> None:
    a = make_checkout(
        pool["mirror"], tmp_path / "Users" / "owner" / "wq" / "work" / "u1", pool["sha"]
    )
    b = make_checkout(pool["mirror"], tmp_path / "home" / "omniworker" / "wq" / "u1", pool["sha"])

    monkeypatch.setenv("WQ_MACHINE_ID", "mac-studio")
    monkeypatch.setenv("WQ_HOME", "/Users/owner/wq")
    key_a = _key(a, pool)
    monkeypatch.setenv("WQ_MACHINE_ID", "initech-roi-calculator")
    monkeypatch.setenv("WQ_HOME", "/home/omniworker/wq")
    key_b = _key(b, pool)

    assert key_a == key_b, "host identity or a host PATH leaked into the fingerprint"
    assert len(key_a) == 64


def test_pinned_key_equals_fresh_checkout_key(pool: dict, tmp_path: Path) -> None:
    """The pre-clone path must be exact, not approximate.

    The worker consults the ledger BEFORE cloning, using the pinned commit in the
    bare mirror. If that key differed from the post-clone key by even one byte,
    every refusal would be recorded under a key no worker ever checks.
    """
    checkout = make_checkout(pool["mirror"], tmp_path / "wt", pool["sha"])
    pinned = input_key(
        "unit-acceptance",
        "",
        str(pool["script"]),
        [str(pool["cfg"])],
        CMD,
        pinned_at=(str(pool["mirror"]), pool["sha"]),
    )
    assert pinned == _key(checkout, pool)


def test_uncommitted_change_changes_the_key(pool: dict, tmp_path: Path) -> None:
    checkout = make_checkout(pool["mirror"], tmp_path / "wt", pool["sha"])
    before = _key(checkout, pool)
    (checkout / "demo" / "junk").write_text("dirty\n")
    assert _key(checkout, pool) != before


def test_content_alone_changes_the_key(pool: dict, tmp_path: Path) -> None:
    """The dirty-tree component must cover CONTENT, not just which paths are dirty.

    ``git status --porcelain`` prints ``?? demo/agent.txt`` whatever the file
    says, so a status-only fingerprint gave an agent's second, different attempt
    the same key as its first — and the ledger refused the retry before it ran
    (caught live, 2026-08-11). Rewriting one file with different bytes, at the
    same path, must move the key.
    """
    checkout = make_checkout(pool["mirror"], tmp_path / "wt", pool["sha"])
    (checkout / "demo" / "agent.txt").write_text("attempt one\n")
    first = _key(checkout, pool)
    (checkout / "demo" / "agent.txt").write_text("attempt two — a different fix\n")
    assert _key(checkout, pool) != first

    tracked = checkout / "README.md"
    before_tracked = _key(checkout, pool)
    tracked.write_text(tracked.read_text() + "edited\n")
    assert _key(checkout, pool) != before_tracked


def test_ignored_files_do_not_readmit_a_refused_input(pool: dict, tmp_path: Path) -> None:
    """An ignored build artefact must not silently re-admit a refused input."""
    checkout = make_checkout(pool["mirror"], tmp_path / "wt", pool["sha"])
    (checkout / ".gitignore").write_text("ignored.txt\n")
    baseline = _key(checkout, pool)
    (checkout / "ignored.txt").write_text("a stray build artefact\n")
    assert _key(checkout, pool) == baseline


def test_gate_upgrade_readmits(pool: dict, tmp_path: Path) -> None:
    """A gate UPGRADE legitimately re-admits every previously refused input."""
    checkout = make_checkout(pool["mirror"], tmp_path / "wt", pool["sha"])
    before = _key(checkout, pool)
    Path(pool["script"]).write_text("# v2 — one byte more\n")
    assert _key(checkout, pool) != before


def test_command_and_gate_name_are_part_of_the_key(pool: dict, tmp_path: Path) -> None:
    checkout = make_checkout(pool["mirror"], tmp_path / "wt", pool["sha"])
    base = _key(checkout, pool)
    assert _key(checkout, pool, "python3 -c 'print(2)'") != base
    other_gate = input_key(
        "merge-gate", str(checkout), str(pool["script"]), [str(pool["cfg"])], CMD
    )
    assert other_gate != base


def test_fields_cannot_alias_into_one() -> None:
    """Length-prefixed, kind-tagged framing: no two field splits share a digest."""
    assert input_key_from_specs(["gate:ab", "cmd:c"]) != input_key_from_specs(["gate:a", "cmd:bc"])


def test_unknown_kind_raises_rather_than_skipping() -> None:
    with pytest.raises(InputKeyError):
        input_key_from_specs(["machine:mac-studio"])
