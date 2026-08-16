"""T6.4 chain-read protocol.

``test_concurrent_hooks_do_not_lose_recorded_paths`` carries the weight here.
Every PostToolUse hook is its OWN PROCESS -- ``sessions/hook_client.py`` is a
``__main__`` script -- so chain-read state is a cross-process read-modify-write,
and threads would prove nothing about it. That is also why ``save_state``
reached for ``fcntl`` in the first place.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from omniagentos.sessions.chain_read import (
    _state_lock,
    extract_paths,
    load_state,
    record_post_read,
    relevance_verdict,
)


def test_noise_blacklisted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path))
    assert relevance_verdict("src/node_modules/x.js") == "blacklist"
    assert relevance_verdict("omniagentos/foo.py") == "relevant"


def test_record_accumulates(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path))
    sid = "ses_test_chain"
    ctx = record_post_read(sid, "Read", {"file_path": "omniagentos/api/main.py"})
    assert ctx is not None
    assert "omniagentos/api/main.py" in ctx
    state = load_state(sid)
    assert "omniagentos/api/main.py" in state.relevant


def test_extract_paths() -> None:
    assert extract_paths("Read", {"file_path": "/a/b.py"}) == ["/a/b.py"]


# ---------------------------------------------------------------------------
# Cross-process state integrity
# ---------------------------------------------------------------------------

_LOCK_PROBE = """\
import os, sys, time
from omniagentos.sessions.chain_read import _state_lock

# Try to enter the same session's lock while the parent holds it. A lock that
# excludes prints WOULD-BLOCK (we time out); one that does not prints ENTERED.
deadline = time.time() + float(sys.argv[2])
import threading
entered = threading.Event()


def _enter() -> None:
    with _state_lock(sys.argv[1]):
        entered.set()
        time.sleep(5.0)


threading.Thread(target=_enter, daemon=True).start()
while time.time() < deadline:
    if entered.is_set():
        print("ENTERED")
        sys.exit(0)
    time.sleep(0.01)
print("WOULD-BLOCK")
sys.exit(0)
"""

_RECORD_WORKER = """\
import os, sys, time
from omniagentos.sessions.chain_read import record_post_read

index, barrier_dir, start_at, n_ops = sys.argv[1], sys.argv[2], float(sys.argv[3]), int(sys.argv[4])
# Announce readiness, then every worker starts on the same wall clock so the
# read-modify-write windows genuinely overlap. A race test with no race is a
# green test that proves nothing.
open(os.path.join(barrier_dir, index), "w").close()
while time.time() < start_at:
    time.sleep(0.001)
for op in range(n_ops):
    record_post_read("ses_race", "Read", {"file_path": "/proj/w%s_%d.py" % (index, op)})
print("OK")
sys.exit(0)
"""


def _worker_env(var_root: Path) -> dict[str, str]:
    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(repo_root), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    # Never let a worker inherit an operator's live var root and write real state.
    env["OMNIAGENTOS_VAR"] = str(var_root)
    env.pop("OMNIAGENTOS_VAR_DIR", None)
    return env


def test_state_lock_excludes_a_second_process(tmp_path: Path, monkeypatch) -> None:
    """The lock must be held on a STABLE inode, or it excludes nobody.

    ``flock`` is scoped to the inode. ``save_state`` used to lock the fresh
    ``mkstemp`` temp it was about to write -- a new inode every call -- so two
    hook processes locked two different files and both proceeded. This is the
    deterministic half of the proof: while the parent holds the lock, a child
    asking for the same session's lock must NOT get in.
    """
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path))
    probe = tmp_path / "lock_probe.py"
    probe.write_text(_LOCK_PROBE, encoding="utf-8")

    with _state_lock("ses_race"):
        result = subprocess.run(
            [sys.executable, str(probe), "ses_race", "2.0"],
            capture_output=True,
            text=True,
            timeout=60,
            env=_worker_env(tmp_path),
            cwd=str(Path(__file__).resolve().parents[2]),
        )
    assert result.returncode == 0, f"probe failed:\n{result.stdout}\n{result.stderr}"
    assert result.stdout.strip() == "WOULD-BLOCK", (
        "a second process entered the same session's chain-read lock while it was "
        f"held -- the lock excludes nobody. stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_concurrent_hooks_do_not_lose_recorded_paths(tmp_path: Path, monkeypatch) -> None:
    """N real hook PROCESSES recording distinct paths must lose none of them.

    Without a lock on a stable path this is a lost update: each process reads
    the state file, appends its own path, and replaces the file, so all but the
    last writer in each overlapping window are discarded silently. Measured on
    the unfixed code: 8 processes recording 8 distinct paths left 1.
    """
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path))
    worker = tmp_path / "record_worker.py"
    worker.write_text(_RECORD_WORKER, encoding="utf-8")
    barrier = tmp_path / "barrier"
    barrier.mkdir()

    n_procs, n_ops = 6, 12
    start_at = time.time() + 3.0
    env = _worker_env(tmp_path)
    procs = [
        subprocess.Popen(
            [sys.executable, str(worker), str(index), str(barrier), str(start_at), str(n_ops)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
        for index in range(n_procs)
    ]
    outputs = [proc.communicate(timeout=120) for proc in procs]
    for proc, (out, err) in zip(procs, outputs, strict=True):
        assert proc.returncode == 0, f"worker failed:\nstdout={out}\nstderr={err}"

    # Contention has to have been real: every worker must have reached the
    # barrier before the shared start time.
    assert len(list(barrier.iterdir())) == n_procs

    expected = {f"/proj/w{index}_{op}.py" for index in range(n_procs) for op in range(n_ops)}
    survived = set(load_state("ses_race").relevant)
    lost = expected - survived
    assert not lost, f"{len(lost)} of {len(expected)} recorded paths were lost: {sorted(lost)[:8]}"
