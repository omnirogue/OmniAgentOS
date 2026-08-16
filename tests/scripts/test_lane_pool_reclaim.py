"""Fail-closed reclaim guard for pipeline/bridge/lane-pool.sh (R20 rebuild).

This is a REBUILD of a rejected prior candidate (sha256:064ffcbb, remedy
"replan", reason: "fail-open TTL reclaim of a live holder"). Its bug: a
lease past TTL_SECS was reclaimed purely on age, even when the recorded
holder pid was still alive and running -- stealing a worktree out from
under a builder that was merely slow.

These tests pin the fail-closed contract:
  (a) a lease whose holder pid is ALIVE (same host, matching pid-start-time)
      is NEVER reclaimed, no matter how far past TTL_SECS it is;
  (b) a lease recorded by a host other than this one is NEVER reclaimed --
      a pid number means nothing across machines, so liveness can never be
      proven and the lease must be refused rather than stolen;
  (c) a same-host lease whose pid is provably dead (gone, or reused by a
      different process -- start-time mismatch) IS reclaimed once past the
      TTL floor;
  (d) `checkout` end-to-end: handout completes in <15s and the handed-out
      venv is verified (`import omniagentos` succeeds) before handout;
  (e) a SILENCED liveness probe (empty result on a zero exit status, as
      opposed to a genuine `ps` miss which exits nonzero) must refuse the
      reclaim, never treat probe silence as proof of death;
  (f) an unreadable/malformed holder record must refuse the reclaim rather
      than being reaped unconditionally with no TTL/host/liveness check;
  (g) concurrent acquirers racing for the last free slot: exactly one wins.

Round 3 (structural rebuild -- see the "FAIL CLOSED ... structural" comments
at the top of lane-pool.sh) adds:
  (h) ACQUIRE-time fail-closed: if the liveness probe for our OWN CALLER_PID
      is silenced/broken/empty at acquire time, `_try_lock` must refuse
      (never write a record with an empty/partial pidstart and proceed);
  (i) the on-disk holder record format is structurally incapable of field
      collapse (ASCII Unit Separator, not space, joins fields) -- an empty
      pidstart field can never shift `ts` into its slot and make an age
      computation land on a ~1.7e9s value that then falsely "matches" a
      pid-reuse verdict once a probe recovers;
  (j) a genuinely slow acquirer (liveness probe blocked well past
      LANE_POOL_ACQUIRE_GRACE, as `ps` can be under real host load) must
      never race a legitimate reaper into a double-handout: reordering the
      probe ahead of `mkdir` means nothing that can block stands in the
      window between winning the lock and writing the holder record.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LANE_POOL = ROOT / "pipeline" / "bridge" / "lane-pool.sh"


def test_lane_pool_script_exists_and_parses() -> None:
    assert LANE_POOL.is_file(), LANE_POOL
    subprocess.run(["bash", "-n", str(LANE_POOL)], check=True)


def _run_snippet(body: str, pool_root: Path, ttl: int, timeout: int = 30) -> subprocess.CompletedProcess:
    """Source lane-pool.sh (functions only -- `main` runs once harmlessly
    with no args) into a bash subprocess, override REPO/POOL_ROOT/TTL_SECS,
    then execute `body` and capture output."""
    script = f"""
set -uo pipefail
source '{LANE_POOL}' >/dev/null 2>&1
REPO='{pool_root}'
POOL_ROOT='{pool_root}'
TTL_SECS={ttl}
{body}
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_cli(env: dict[str, str], *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env)
    return subprocess.run(
        ["bash", str(LANE_POOL), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=full_env,
    )


# --- (a) alive holder is never reclaimed, even far past TTL ----------------


def test_try_lock_never_reclaims_alive_holder_past_ttl(tmp_path: Path) -> None:
    pool_root = tmp_path / "pool"
    body = """
mkdir -p "$POOL_ROOT"
lock="$(_slot_lock 1)"
mkdir -p "$lock"
sleep 60 &
holder_pid=$!
printf '%s\\x1f%s\\x1f%s\\x1f%s\\n' "$holder_pid" "$(hostname)" "$(_pid_start "$holder_pid")" "$(( $(_now) - 999999 ))" > "$lock/holder"
_try_lock 1
rc=$?
kill "$holder_pid" 2>/dev/null
wait "$holder_pid" 2>/dev/null
echo "RC=$rc"
[ -d "$lock" ] && echo LOCK_INTACT || echo LOCK_GONE
exit "$rc"
"""
    result = _run_snippet(body, pool_root, ttl=1)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "RC=1" in result.stdout
    assert "LOCK_INTACT" in result.stdout


# --- (b) foreign-host lease is never reclaimed ------------------------------


def test_try_lock_never_reclaims_foreign_host_lease(tmp_path: Path) -> None:
    pool_root = tmp_path / "pool"
    body = """
mkdir -p "$POOL_ROOT"
lock="$(_slot_lock 2)"
mkdir -p "$lock"
printf '%s\\x1f%s\\x1f%s\\x1f%s\\n' 999999 "some-other-host-does-not-exist" "Mon_Jan_1_00_00_00_1970" "$(( $(_now) - 999999 ))" > "$lock/holder"
_try_lock 2
rc=$?
echo "RC=$rc"
[ -d "$lock" ] && echo LOCK_INTACT || echo LOCK_GONE
exit "$rc"
"""
    result = _run_snippet(body, pool_root, ttl=1)
    # pid 999999 almost certainly does not exist locally either -- this
    # specifically proves the HOST gate refuses before any pid check would
    # otherwise (wrongly) call it dead-and-reclaimable.
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "RC=1" in result.stdout
    assert "LOCK_INTACT" in result.stdout


# --- (c) a provably-dead same-host holder past TTL floor IS reclaimed ------


def test_try_lock_reclaims_provably_dead_same_host_holder(tmp_path: Path) -> None:
    pool_root = tmp_path / "pool"
    body = """
mkdir -p "$POOL_ROOT"
lock="$(_slot_lock 3)"
mkdir -p "$lock"
( exit 0 ) &
dead_pid=$!
wait "$dead_pid" 2>/dev/null
printf '%s\\x1f%s\\x1f%s\\x1f%s\\n' "$dead_pid" "$(hostname)" "definitely-does-not-match-any-real-start-time" "$(( $(_now) - 999999 ))" > "$lock/holder"
_try_lock 3
rc=$?
echo "RC=$rc"
[ -d "$lock" ] && echo LOCK_INTACT || echo LOCK_GONE
exit "$rc"
"""
    result = _run_snippet(body, pool_root, ttl=1)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "RC=2" in result.stdout
    assert "LOCK_GONE" in result.stdout


def test_try_lock_respects_ttl_floor_even_for_dead_holder(tmp_path: Path) -> None:
    """A fresh lease (age <= TTL_SECS) is never even probed for liveness,
    dead pid or not -- TTL_SECS is a floor, never itself grounds to reclaim."""
    pool_root = tmp_path / "pool"
    body = """
mkdir -p "$POOL_ROOT"
lock="$(_slot_lock 4)"
mkdir -p "$lock"
( exit 0 ) &
dead_pid=$!
wait "$dead_pid" 2>/dev/null
printf '%s\\x1f%s\\x1f%s\\x1f%s\\n' "$dead_pid" "$(hostname)" "irrelevant" "$(_now)" > "$lock/holder"
_try_lock 4
rc=$?
echo "RC=$rc"
exit "$rc"
"""
    result = _run_snippet(body, pool_root, ttl=5)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "RC=1" in result.stdout


# --- probe silence must never be treated as proof of death ------------------


def test_try_lock_refuses_when_liveness_probe_is_silenced(tmp_path: Path) -> None:
    """If the liveness probe (`ps`) is shadowed/broken and reports nothing
    for a genuinely-live pid, `_try_lock` must refuse the reclaim (rc=1),
    never reap it (rc=2): absence of proof of death is not proof of death.
    Shadows `ps` on PATH exactly like the reviewer's repro (N2) so a
    silenced probe is reproduced deterministically rather than asserted."""
    pool_root = tmp_path / "pool"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "ps").write_text(
        "#!/bin/sh\n"
        "# pretend every pid probe returns nothing, as if the probe binary\n"
        "# were broken/shadowed/silenced -- exits 0 (success) with no output,\n"
        "# which a genuine `ps` never does for a live pid (it would print a\n"
        "# line) or a dead one (it would exit nonzero).\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (fake_bin / "ps").chmod(0o755)
    body = f"""
mkdir -p "$POOL_ROOT"
lock="$(_slot_lock 6)"
mkdir -p "$lock"
sleep 60 &
holder_pid=$!
# Record a REAL start time via the real ps (before PATH is shadowed) so a
# working probe would find a match and keep the lease -- proving the
# refusal below is caused by probe silence, not a genuine mismatch.
real_start=$(/bin/ps -o lstart= -p "$holder_pid" | tr -s ' ' '_' | sed 's/^_//;s/_$//')
printf '%s\\x1f%s\\x1f%s\\x1f%s\\n' "$holder_pid" "$(hostname)" "$real_start" "$(( $(_now) - 999999 ))" > "$lock/holder"
export PATH='{fake_bin}':"$PATH"
_try_lock 6
rc=$?
kill "$holder_pid" 2>/dev/null
wait "$holder_pid" 2>/dev/null
echo "RC=$rc"
[ -d "$lock" ] && echo LOCK_INTACT || echo LOCK_GONE
exit "$rc"
"""
    result = _run_snippet(body, pool_root, ttl=1)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "RC=1" in result.stdout
    assert "LOCK_INTACT" in result.stdout


# --- an unreadable/malformed holder record must never be reaped blind -------


def test_try_lock_refuses_unreadable_or_malformed_holder_file(tmp_path: Path) -> None:
    """A holder file that EXISTS (so it skips the missing-file grace-window
    path) but cannot be parsed -- e.g. truncated mid-write, zero-byte --
    must be treated as "cannot prove the holder dead" and refuse reclaim,
    not reaped unconditionally with no TTL/host/liveness check at all (the
    widest fail-open door in this file, since it short-circuits every other
    guard)."""
    pool_root = tmp_path / "pool"
    body = """
mkdir -p "$POOL_ROOT"
lock="$(_slot_lock 7)"
mkdir -p "$lock"
# Zero-byte holder file: `read` hits EOF immediately and fails -- this must
# refuse, not reap unconditionally.
: > "$lock/holder"
_try_lock 7
rc=$?
echo "RC=$rc"
[ -d "$lock" ] && echo LOCK_INTACT || echo LOCK_GONE
exit "$rc"
"""
    result = _run_snippet(body, pool_root, ttl=1)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "RC=1" in result.stdout
    assert "LOCK_INTACT" in result.stdout


# --- end-to-end: checkout refuses (never steals) a slow-but-alive holder ---


# --- (d) full handout: <15s, and venv verified before handout --------------


@pytest.fixture(scope="module")
def scratch_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("lane_pool_scratch_repo")
    repo = base / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Lane Pool Test"], check=True)
    (repo / "README.md").write_text("scratch repo for lane-pool.sh tests\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    venv_dir = repo / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv_dir)], check=True)
    py = venv_dir / "bin" / "python"
    site_packages = subprocess.run(
        [str(py), "-c", "import site; print(site.getsitepackages()[0])"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    pkg = Path(site_packages) / "omniagentos"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text('"""test double for lane-pool venv-verify handout"""\n', encoding="utf-8")
    return repo


def test_checkout_refuses_rather_than_steals_slow_alive_holder(scratch_repo: Path, tmp_path: Path) -> None:
    """End-to-end version of the exact scenario the rejection named: a live
    builder that is merely slow must never have its worktree handed to
    someone else. Uses a REAL provisioned slot (via cmd_init against
    scratch_repo) so that, on the buggy prior implementation, a wrongful
    reclaim would actually succeed in handing the slot to a second caller --
    proving this is a true end-to-end regression guard, not just a unit test
    of the internal helper."""
    pool_root = tmp_path / "pool"
    body = f"""
REPO='{scratch_repo}'
BASE_REF='main'
VENV_TEMPLATE='{scratch_repo}/.venv'
cmd_init 1 >/dev/null 2>&1 || {{ echo INIT_FAILED; exit 9; }}
lock="$(_slot_lock 1)"
mkdir -p "$lock"
sleep 60 &
holder_pid=$!
printf '%s\\x1f%s\\x1f%s\\x1f%s\\n' "$holder_pid" "$(hostname)" "$(_pid_start "$holder_pid")" "$(( $(_now) - 999999 ))" > "$lock/holder"
cmd_checkout
rc=$?
kill "$holder_pid" 2>/dev/null
wait "$holder_pid" 2>/dev/null
echo "RC=$rc"
exit "$rc"
"""
    try:
        result = _run_snippet(body, pool_root, ttl=1, timeout=60)
        assert result.returncode == 3, (result.stdout, result.stderr)
        assert "RC=3" in result.stdout
        assert "INIT_FAILED" not in result.stdout
    finally:
        for slotdir in sorted(pool_root.glob("slot-*/wt")):
            subprocess.run(
                ["git", "-C", str(scratch_repo), "worktree", "remove", "--force", str(slotdir)],
                check=False,
            )
        subprocess.run(["git", "-C", str(scratch_repo), "worktree", "prune"], check=False)


def test_checkout_handout_under_15s_with_verified_venv(scratch_repo: Path, tmp_path: Path) -> None:
    pool_root = tmp_path / "pool"
    env = {
        "REPO": str(scratch_repo),
        "LANE_POOL_ROOT": str(pool_root),
        "LANE_POOL_SIZE": "1",
        "LANE_POOL_BASE": "main",
        "LANE_POOL_VENV_TEMPLATE": str(scratch_repo / ".venv"),
        "LANE_POOL_CALLER_PID": str(os.getpid()),
    }
    try:
        init = _run_cli(env, "init", "1")
        assert init.returncode == 0, (init.stdout, init.stderr)

        t0 = time.monotonic()
        co = _run_cli(env, "checkout")
        elapsed = time.monotonic() - t0
        assert co.returncode == 0, (co.stdout, co.stderr)
        assert elapsed < 15.0, f"checkout took {elapsed:.2f}s (falsifier: must be <15s)"

        parts = co.stdout.strip().split()
        assert len(parts) == 3, co.stdout
        slot_id, wt, venv = parts

        verify = subprocess.run(
            [f"{venv}/bin/python", "-c", "import omniagentos"],
            capture_output=True,
            text=True,
        )
        assert verify.returncode == 0, verify.stderr

        status = subprocess.run(
            ["git", "-C", wt, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert status.stdout.strip() == "", status.stdout

        ret = _run_cli(env, "return", slot_id)
        assert ret.returncode == 0, (ret.stdout, ret.stderr)
    finally:
        # Clean up worktree registrations in the scratch repo regardless of
        # test outcome, so a failed assertion never leaks a worktree entry.
        for slotdir in sorted((pool_root).glob("slot-*/wt")):
            subprocess.run(
                ["git", "-C", str(scratch_repo), "worktree", "remove", "--force", str(slotdir)],
                check=False,
            )
        subprocess.run(["git", "-C", str(scratch_repo), "worktree", "prune"], check=False)


# --- concurrent acquire: exactly one winner for the last free slot ---------


def test_concurrent_acquire_exclusive(scratch_repo: Path, tmp_path: Path) -> None:
    """N racers contend for a single free slot; the atomic `mkdir` primitive
    documented at the top of lane-pool.sh must give exactly one winner and
    refuse (rc=3) every other racer -- never two winners, never a hang.

    Racers MUST be separate processes (subprocess.Popen), not threads: a
    thread pool shares one pid, and the `mkdir` primitive under test is
    process-level exclusion, so threads would not exercise it faithfully.

    CALLER_PID is deliberately the SAME real, live pid (this test process's
    own) for every racer, matching the documented usage pattern elsewhere in
    this file (`test_checkout_handout_under_15s_with_verified_venv` does the
    same) and the realistic production shape -- N concurrent checkout calls
    issued by one live orchestrator process. A synthetic, non-existent
    CALLER_PID would make `_pid_start` return empty for the WINNER's own
    holder record; that specific field-shift/field-collapse defect was
    catalogued out of scope for this lane in rounds 1-2 and is now fixed and
    covered directly by test_try_lock_refuses_acquire_when_own_probe_is_silenced
    and test_try_lock_refuses_on_malformed_record_with_empty_pidstart_field
    below -- not what THIS test exists to exercise (process-level mkdir
    exclusion)."""
    pool_root = tmp_path / "pool"
    env_base = {
        "REPO": str(scratch_repo),
        "LANE_POOL_ROOT": str(pool_root),
        "LANE_POOL_SIZE": "1",
        "LANE_POOL_BASE": "main",
        "LANE_POOL_VENV_TEMPLATE": str(scratch_repo / ".venv"),
        "LANE_POOL_CALLER_PID": str(os.getpid()),
    }
    try:
        init = subprocess.run(
            ["bash", str(LANE_POOL), "init", "1"],
            capture_output=True,
            text=True,
            env={**os.environ, **env_base},
            timeout=120,
        )
        assert init.returncode == 0, (init.stdout, init.stderr)

        n_racers = 8
        procs = [
            subprocess.Popen(
                ["bash", str(LANE_POOL), "checkout"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, **env_base},
            )
            for _ in range(n_racers)
        ]
        results = [p.communicate(timeout=60) for p in procs]
        codes = [p.returncode for p in procs]

        winners = [out for (out, _err), rc in zip(results, codes, strict=True) if rc == 0]
        refusals = sum(1 for rc in codes if rc == 3)
        assert len(winners) == 1, (codes, results)
        assert refusals == n_racers - 1, (codes, results)
        assert winners[0].strip().split()[0] == "slot-1"
    finally:
        for slotdir in sorted(pool_root.glob("slot-*/wt")):
            subprocess.run(
                ["git", "-C", str(scratch_repo), "worktree", "remove", "--force", str(slotdir)],
                check=False,
            )
        subprocess.run(["git", "-C", str(scratch_repo), "worktree", "prune"], check=False)


# --- round 3: structural fixes (STOP PATCHING DOORS) ------------------------
#
# The three tests below pin the structural invariants demanded in round 3,
# each proven red-first against the pre-round-3 script in a throwaway
# scratch copy (never by mutating this worktree) before being fixed here.
# See lane-pool.sh's "FAIL CLOSED (... structural -- round 3 ...)" comments
# for the mechanism each one protects.


def test_try_lock_refuses_acquire_when_own_probe_is_silenced(tmp_path: Path) -> None:
    """(h) If the liveness probe for our OWN CALLER_PID is silenced/broken at
    ACQUIRE time (not reclaim time), `_try_lock` must refuse outright and
    must never mkdir-and-write a holder record with an empty/partial
    pidstart field. Proven red-first: run against the pre-round-3 script,
    this exact snippet acquires successfully (rc=0) and leaves a holder file
    reading "<pid> <host><space><space><ts>" (empty pidstart between two
    spaces) on disk."""
    pool_root = tmp_path / "pool"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "ps").write_text(
        "#!/bin/sh\n"
        "# Silenced probe: claims success, tells us nothing -- the reviewer's\n"
        "# N2 repro, exercised here at ACQUIRE time (own record), not reclaim\n"
        "# time (someone else's record).\n"
        "exit 0\n",
        encoding="utf-8",
    )
    (fake_bin / "ps").chmod(0o755)
    body = f"""
mkdir -p "$POOL_ROOT"
lock="$(_slot_lock 1)"
sleep 60 &
CALLER_PID=$!
export PATH='{fake_bin}':"$PATH"
_try_lock 1
rc=$?
kill "$CALLER_PID" 2>/dev/null
wait "$CALLER_PID" 2>/dev/null
echo "RC=$rc"
[ -f "$lock/holder" ] && echo HOLDER_EXISTS || echo HOLDER_ABSENT
[ -d "$lock" ] && echo LOCK_DIR_EXISTS || echo LOCK_DIR_ABSENT
exit "$rc"
"""
    result = _run_snippet(body, pool_root, ttl=1)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "RC=1" in result.stdout
    assert "HOLDER_ABSENT" in result.stdout, "must never write a partial record"
    assert "LOCK_DIR_ABSENT" in result.stdout, "must never leave a lock dir behind on refusal"


def test_try_lock_refuses_on_malformed_record_with_empty_pidstart_field(tmp_path: Path) -> None:
    """(i) A holder record with an empty pidstart field -- the exact shape
    the pre-round-3 acquire path could produce, and the exact shape a
    space-delimited format collapses an unrelated field into on read -- must
    never be read as "pid reused, provably dead" for a holder pid that is
    genuinely still alive. Proven red-first: the equivalent space-delimited
    record ("<pid> <host><space><space><ts>") against the pre-round-3 script
    is reaped (rc=2, LOCK_GONE) even though the recorded pid is a real,
    still-running process -- because the field shift makes `ts` read empty,
    age compute to ~1.7e9s, and a recovered/real probe's non-empty start
    time then mismatches the (actually-a-stale-ts-value) "pidstart" field,
    which old code reads as proof of pid reuse."""
    pool_root = tmp_path / "pool"
    body = r"""
mkdir -p "$POOL_ROOT"
lock="$(_slot_lock 5)"
mkdir -p "$lock"
sleep 60 &
holder_pid=$!
# Well-formed \x1f-delimited record EXCEPT the pidstart field is empty --
# this can no longer be produced by a legitimate acquire (see the other new
# test), but a corrupted/foreign-format record on disk must still refuse,
# not be reaped.
printf '%s\x1f%s\x1f\x1f%s\n' "$holder_pid" "$(hostname)" "$(( $(_now) - 999999 ))" > "$lock/holder"
_try_lock 5
rc=$?
kill "$holder_pid" 2>/dev/null
wait "$holder_pid" 2>/dev/null
echo "RC=$rc"
[ -d "$lock" ] && echo LOCK_INTACT || echo LOCK_GONE
exit "$rc"
"""
    result = _run_snippet(body, pool_root, ttl=1)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "RC=1" in result.stdout
    assert "LOCK_INTACT" in result.stdout, "must never reap a live holder off a malformed record"


def test_try_lock_no_double_handout_when_own_probe_blocks_past_grace(tmp_path: Path) -> None:
    """(j) round-2 MAJOR: a genuinely slow acquirer -- its OWN liveness probe
    blocked (e.g. `ps` under real host load) for LONGER than
    LANE_POOL_ACQUIRE_GRACE -- must never lose a race to a legitimate second
    acquirer and then clobber that second acquirer's holder record on
    wakeup. Proven red-first: against the pre-round-3 script, both the slow
    acquirer (A) and the racer that reaps its holder-less lock dir and
    re-acquires (B) return rc=0 -- a double-handout, not a leak. Reproduced
    with a real, live-blocking `ps` shim (not a timing guess): only pid
    probes for A's own CALLER_PID are slowed, everything else runs at
    normal speed, so the race is deterministic rather than flaky."""
    pool_root = tmp_path / "pool"
    pool_root.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    slow_holder = subprocess.Popen(["sleep", "60"])
    try:
        slow_pid = slow_holder.pid
        (fake_bin / "ps").write_text(
            "#!/bin/sh\n"
            f'case " $* " in\n  *" {slow_pid} "*) sleep 4 ;;\nesac\n'
            'exec /bin/ps "$@"\n',
            encoding="utf-8",
        )
        (fake_bin / "ps").chmod(0o755)

        a_snippet = f"""
set -uo pipefail
source '{LANE_POOL}' >/dev/null 2>&1
REPO='{pool_root}'
POOL_ROOT='{pool_root}'
LANE_POOL_ACQUIRE_GRACE=1
CALLER_PID={slow_pid}
export PATH='{fake_bin}':"$PATH"
_try_lock 1
echo "A_RC=$?"
"""
        b_snippet = f"""
set -uo pipefail
source '{LANE_POOL}' >/dev/null 2>&1
REPO='{pool_root}'
POOL_ROOT='{pool_root}'
LANE_POOL_ACQUIRE_GRACE=1
sleep 60 &
CALLER_PID=$!
sleep 0.2
rc1=1
attempt=0
while [ "$rc1" != "0" ] && [ "$attempt" -lt 40 ]; do
  _try_lock 1
  rc1=$?
  attempt=$((attempt+1))
  sleep 0.15
done
echo "B_RC=$rc1 (after $attempt attempts)"
kill "$CALLER_PID" 2>/dev/null
"""
        a_proc = subprocess.Popen(
            ["bash", "-c", a_snippet], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        b_proc = subprocess.Popen(
            ["bash", "-c", b_snippet], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        a_out, a_err = a_proc.communicate(timeout=30)
        b_out, b_err = b_proc.communicate(timeout=30)

        winners = sum(1 for out in (a_out, b_out) if "A_RC=0" in out or "B_RC=0" in out)
        assert winners == 1, (
            "expected exactly one winner, never zero and never both",
            a_out, a_err, b_out, b_err,
        )
    finally:
        slow_holder.kill()
        slow_holder.wait()
