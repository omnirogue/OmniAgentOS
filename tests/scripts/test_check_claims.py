"""Regression tests for scripts/check-claims.sh — the mechanized claims contract.

RED FIRST: before scripts/check-claims.sh existed, every test below failed with
FileNotFoundError. The dangerous case is test_unparseable_marker_younger_than_grace_
is_left_alone: a checker that "cleans up" a claim file mid-creation destroys a live
claim, which is exactly the failure mode the estate doctrine calls out (destructive
rules must be mechanisms, not sentences, and a reaper must never eat a live write).
"""

import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check-claims.sh"


def _run(claims_dir: Path, claim_id: str, agent: str, grace: int = 600):
    env = dict(os.environ)
    env["UNPARSEABLE_GRACE_SECS"] = str(grace)
    return subprocess.run(
        [str(SCRIPT), "--id", claim_id, "--agent", agent, "--claims-dir", str(claims_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _write_marker(claims_dir: Path, claim_id: str, body: str, age_secs: int = 0):
    claims_dir.mkdir(parents=True, exist_ok=True)
    name = claim_id.replace(":", "_") + ".claim"
    path = claims_dir / name
    path.write_text(body)
    if age_secs:
        old = time.time() - age_secs
        os.utime(path, (old, old))
    return path


def test_script_exists_and_is_executable():
    assert SCRIPT.exists(), "scripts/check-claims.sh must exist"
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/check-claims.sh must be executable"


def test_no_marker_is_not_held(tmp_path):
    result = _run(tmp_path, "sha256:deadbeef", "some-agent")
    assert result.returncode == 1
    assert "not-held" in result.stderr


def test_valid_marker_held_by_caller_exits_0(tmp_path):
    claim_id = "sha256:aaaa"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    _write_marker(
        tmp_path,
        claim_id,
        json.dumps({"actor": "implementer-loop@claude-account-4", "at": "x", "expires_at": future}),
    )
    result = _run(tmp_path, claim_id, "implementer-loop@claude-account-4")
    assert result.returncode == 0
    assert "held" in result.stderr


def test_valid_marker_held_by_someone_else_is_not_held(tmp_path):
    claim_id = "sha256:bbbb"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    _write_marker(
        tmp_path,
        claim_id,
        json.dumps({"actor": "other-agent", "at": "x", "expires_at": future}),
    )
    result = _run(tmp_path, claim_id, "implementer-loop@claude-account-4")
    assert result.returncode == 1
    assert "other-agent" in result.stderr
    # Marker must NOT be deleted just because someone else asked about it.
    assert (tmp_path / (claim_id.replace(":", "_") + ".claim")).exists()


def test_expired_marker_is_reaped_and_reported_not_held(tmp_path):
    claim_id = "sha256:cccc"
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    marker = _write_marker(
        tmp_path,
        claim_id,
        json.dumps({"actor": "implementer-loop@claude-account-4", "at": "x", "expires_at": past}),
    )
    result = _run(tmp_path, claim_id, "implementer-loop@claude-account-4")
    assert result.returncode == 1
    assert "expired" in result.stderr
    assert not marker.exists(), "expired claim markers must be deleted on sight"


def test_unparseable_marker_younger_than_grace_is_left_alone(tmp_path):
    """The dangerous case: a marker mid-write (invalid JSON, brand new) must survive."""
    claim_id = "sha256:dddd"
    marker = _write_marker(tmp_path, claim_id, '{"actor": "mid-writ', age_secs=5)
    result = _run(tmp_path, claim_id, "implementer-loop@claude-account-4", grace=600)
    assert result.returncode == 2, "young unparseable marker must be reported malformed, not deleted"
    assert marker.exists(), "a claim file mid-creation must NEVER be deleted"


def test_unparseable_marker_older_than_grace_is_reaped(tmp_path):
    claim_id = "sha256:eeee"
    marker = _write_marker(tmp_path, claim_id, "{not json at all", age_secs=900)
    result = _run(tmp_path, claim_id, "implementer-loop@claude-account-4", grace=600)
    assert result.returncode == 1
    assert "reaped" in result.stderr
    assert not marker.exists(), "an orphaned unparseable marker past the grace window must be reaped"


def test_missing_required_args_exits_nonzero():
    result = subprocess.run([str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0


# --- Repair round 2 (cross-lineage review, grok-4.5) ---------------------------
#
# F1 (BLOCKER, TOCTOU): a reap decision made from a stale read must never delete a
# marker that changed identity (device/inode/mtime) underneath it — e.g. a fresh
# O_CREAT|O_EXCL claim landing on the same path between our snapshot and our unlink.
# F2 (BLOCKER, path traversal): --id must not be able to make the resolved marker
# path escape --claims-dir.
# F3 (MAJOR): alternate-but-valid ISO-8601 expires_at forms (fractional seconds,
# explicit +00:00 offset) must not be treated as unparseable/reaped.
# F4 (MAJOR, residual limit documented in the script header): forward clock skew is
# mitigated (negative AGE clamped to 0) but not fully solvable without a trusted
# time source; not independently testable without moving the system clock.
# F5 (MINOR): PARSE_RC/PY_RC must be a real captured exit status, not always 0.


def _run_with_env(claims_dir: Path, claim_id: str, agent: str, extra_env: dict, grace: int = 600):
    env = dict(os.environ)
    env["UNPARSEABLE_GRACE_SECS"] = str(grace)
    env.update(extra_env)
    return subprocess.run(
        [str(SCRIPT), "--id", claim_id, "--agent", agent, "--claims-dir", str(claims_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_f1_toctou_reap_aborts_when_marker_identity_changes_underneath_it(tmp_path):
    """Deterministic TOCTOU proof: use the test-only injection hook (inert unless
    CHECK_CLAIMS_TEST_TOCTOU_HOOK is set) to simulate a fresh claim landing on the
    same path in the exact window between the reap decision and the unlink call.
    This does not depend on wall-clock process racing, which is flaky under
    subprocess-startup jitter.

    HOST-INDEPENDENT SINCE 2026-08-11, and the reason it was not is a defect in
    the guard rather than in the test. ``marker_identity`` was ``device:inode:
    SECONDS``, and a reclaim is precisely the case that lands in the SAME second:
    on ext4/overlayfs the just-unlinked inode is handed straight back, so the
    replacement below fingerprinted IDENTICALLY to the file it replaced and the
    reap ate the live claim. This node was red on every GitHub PR (#222 #224 #231
    #236 #240) and green on macOS only because APFS does not recycle inode
    numbers — a green bought by the filesystem, not by the guard. The fingerprint
    now carries size and nanosecond mtime/ctime, so the replacement differs on
    every axis a reclaim can differ on, on every filesystem.
    """
    claim_id = "sha256:toctou"
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    original = json.dumps({"actor": "old", "at": "x", "expires_at": past})
    # SAME BYTE LENGTH ON PURPOSE (review round 2). Both timestamps are the
    # fixed-width Z form and both actors are three characters, so the two files
    # differ in NOTHING the old whole-second fingerprint could see and nothing
    # the size field can see either. That leaves inode and nanosecond
    # mtime/ctime to do the work — which is the point: with unequal payload
    # sizes this node would still pass if the timestamps were reverted to
    # whole seconds, i.e. it would stop testing the thing it is named for.
    reclaimed = json.dumps({"actor": "new", "at": "x", "expires_at": future})
    assert len(original.encode()) == len(reclaimed.encode()), (
        "the fixtures must be byte-identical in length or `size` alone "
        f"distinguishes them: {len(original)} vs {len(reclaimed)}"
    )
    marker = _write_marker(tmp_path, claim_id, original)
    # Right before the identity re-check inside safe_reap, replace the marker with a
    # brand-new file (different inode) simulating an O_CREAT|O_EXCL reclaim.
    hook = f'rm -f -- "{marker}"; printf %s \'{reclaimed}\' > "{marker}"'
    result = _run_with_env(tmp_path, claim_id, "old", {"CHECK_CLAIMS_TEST_TOCTOU_HOOK": hook})
    assert result.returncode == 1
    assert "changed underneath us" in result.stderr or "abort-reap" in result.stderr
    assert marker.exists(), "the reclaimed marker must survive a stale reap decision"
    assert json.loads(marker.read_text())["actor"] == "new"

    # The fingerprint is about THIS FILE, and it says so on one line. `-f` is a
    # format flag to BSD stat and a "report on the FILE SYSTEM" flag to GNU stat,
    # so the BSD-first probe printed a filesystem status block — block size, FREE
    # BLOCKS, FREE INODES — into the identity on Linux. That made this guard fire
    # on unrelated filesystem churn and, worse, meant the comparison it reported
    # was never the file comparison its own message claimed. Asserted on the
    # message because that string is the only place the fingerprint is observable.
    abort_lines = [ln for ln in result.stderr.splitlines() if "abort-reap" in ln]
    assert len(abort_lines) == 1, result.stderr
    for filesystem_noise in ("Block size", "Inodes:", "Namelen", "Blocks: Total"):
        assert filesystem_noise not in result.stderr, (
            "the identity fingerprint carries filesystem status, not file "
            f"identity — {filesystem_noise!r} in:\n{result.stderr}"
        )


def test_f1_toctou_reap_aborts_on_device_inode_size_whole_second_collision(tmp_path):
    """Minor 1 (cross-lineage review round 2, gpt-5.6-sol): the test above relies
    on a REAL filesystem handing back a genuinely different inode for the
    replacement file, so it can pass on inode difference alone even if the
    nanosecond mtime/ctime fields the F1 round-2 fix added were reverted to
    whole seconds — it never forces dependence on the fields it claims to
    protect. Reviewer repro: F4-toctou-size-collision-confirm.sh.

    This test removes that escape hatch with a stat shim that reports the
    SAME device, inode and size for both the snapshot read and the delete-time
    read (the exact ext4/overlayfs same-second reclaim collision the F1 header
    describes), differing ONLY in the fractional (nanosecond) mtime/ctime. If
    check-claims.sh ever drops back to whole-second timestamps, this collision
    becomes indistinguishable and the reap would proceed on the live marker.
    """
    claim_id = "sha256:toctou-ns-only"
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    original = json.dumps({"actor": "old", "at": "x", "expires_at": past})
    reclaimed = json.dumps({"actor": "new", "at": "x", "expires_at": future})
    assert len(original.encode()) == len(reclaimed.encode()), (
        "fixtures must be byte-identical in length or `size` alone distinguishes "
        f"them: {len(original)} vs {len(reclaimed)}"
    )
    payload_bytes = len(original.encode())
    marker = _write_marker(tmp_path, claim_id, original)
    count_file = tmp_path / "toctou-ns-only-stat-count"
    # Same generic both-dialect shim design as F4-toctou-size-collision-confirm.sh:
    # switches on "$1|$2" so it recognizes GNU's `-c '...%.9Y...%.9Z...'` and
    # BSD's `-f '...%Fm...%Fc...'` probes without caring which platform this
    # test runs on. Every invocation reports device=7, inode=11 and the SAME
    # size, and whole-second mtime/ctime of 100/200 on both calls; only the
    # fractional part changes between the first call (snapshot) and every call
    # after it (delete-time re-check), exactly the axis the F1 round-2 fix
    # exists to make load-bearing.
    shim_body = (
        "#!/bin/sh\n"
        "n=0\n"
        '[ ! -f "' + str(count_file) + '" ] || n=$(sed -n "1p" "' + str(count_file) + '")\n'
        "n=$((n + 1))\n"
        'printf "%s\\n" "$n" > "' + str(count_file) + '"\n'
        'case "${1-}|${2-}" in\n'
        "  -c\\|*%.9Y*%.9Z*)\n"
        '    if [ "$n" -eq 1 ]; then frac=000000001; else frac=000000002; fi\n'
        '    printf "7:11:' + str(payload_bytes) + ':100.$frac:200.$frac\\n" ;;\n'
        "  -f\\|*%Fm*%Fc*)\n"
        '    if [ "$n" -eq 1 ]; then frac=000000001; else frac=000000002; fi\n'
        '    printf "7:11:' + str(payload_bytes) + ':100.$frac:200.$frac\\n" ;;\n'
        '  *) printf "7:11:' + str(payload_bytes) + ':100:200\\n" ;;\n'
        "esac\n"
        "exit 0\n"
    )
    path = _stat_shim(tmp_path, shim_body)
    hook = f'rm -f -- "{marker}"; printf %s \'{reclaimed}\' > "{marker}"'
    result = _run_with_env(
        tmp_path,
        claim_id,
        "old",
        {"PATH": path, "CHECK_CLAIMS_TEST_TOCTOU_HOOK": hook},
    )
    assert result.returncode == 1, (
        "same device+inode+size+whole-second collision, differing only in "
        f"nanosecond mtime/ctime, must still abort the reap: rc={result.returncode} "
        f"stderr={result.stderr!r}"
    )
    assert "changed underneath us" in result.stderr or "abort-reap" in result.stderr
    assert marker.exists(), (
        "the reclaimed marker must survive a nanosecond-only identity difference"
    )
    assert json.loads(marker.read_text())["actor"] == "new"
    assert int(count_file.read_text().strip()) >= 2, (
        "the shim must have been consulted for both the snapshot and the delete-time re-check"
    )


def _stat_shim(tmp_path: Path, body: str) -> str:
    """A directory holding a fake ``stat``, for PATH-prepending in ``extra_env``.

    check-claims.sh discriminates GNU from BSD by probing both dialects, so the
    only way to test what it does when a probe misbehaves is to hand it a
    misbehaving one. ``/usr/bin/stat`` stays reachable for the passthrough cases.
    """
    shim_dir = tmp_path / f"shim-{abs(hash(body)) % 10**8}"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "stat"
    shim.write_text(body, encoding="utf-8")
    shim.chmod(0o755)
    return f"{shim_dir}:{os.environ.get('PATH', '')}"


def test_a_failed_stat_probe_never_contributes_to_the_fingerprint(tmp_path):
    """A probe's output is only its output if the probe SUCCEEDED.

    The first version of the GNU-first fix rejected the wrong dialect by exit
    status but streamed each attempt straight to stdout, so a ``stat`` that
    prints a file-INDEPENDENT blob and then exits non-zero — exactly what GNU
    ``stat -f`` does — still contributed its bytes. Two different files then
    fingerprinted identically, the identity check became a tautology, and
    safe_reap deleted the live claim that had just landed on the path.

    Nothing is deleted and nothing is reported as ``not-held``: an identity we
    cannot establish must produce the conservative malformed verdict, because
    "not held" is the answer that lets a second builder start.
    """
    claim_id = "sha256:blind-stat"
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    marker = _write_marker(
        tmp_path,
        claim_id,
        json.dumps({"actor": "old-owner", "at": "x", "expires_at": past}),
    )
    # -c fails silently; -f prints a constant, file-independent status blob and
    # then fails. Both failures, one of them noisy.
    path = _stat_shim(
        tmp_path,
        "#!/bin/sh\n"
        'case "${1-}" in\n'
        "  -c) exit 1 ;;\n"
        "  -f)\n"
        "    printf '%s\\n' 'Block size: 4096 Fundamental block size: 4096' \\\n"
        "      'Blocks: Total: 1 Free: 1 Available: 1' 'Inodes: Total: 1 Free: 1'\n"
        "    exit 1 ;;\n"
        "esac\n"
        "exit 1\n",
    )
    replacement = '{"actor":"live-owner","at":"x","expires_at":"2099-01-01T00:00:00Z"}'
    hook = f'rm -f -- "{marker}"; printf %s \'{replacement}\' > "{marker}"'
    result = _run_with_env(
        tmp_path,
        claim_id,
        "old-owner",
        {"PATH": path, "CHECK_CLAIMS_TEST_TOCTOU_HOOK": hook},
    )

    assert result.returncode == 2, (
        "an unfingerprintable marker must be malformed (2), never not-held (1): "
        f"rc={result.returncode} stderr={result.stderr!r}"
    )
    assert marker.exists(), "a claim we could not identify must never be deleted"
    assert json.loads(marker.read_text())["actor"] == "live-owner"
    assert "could not be fingerprinted" in result.stderr, result.stderr


def test_an_unstattable_mtime_ages_a_malformed_marker_as_brand_new(tmp_path):
    """Unknown age takes the young/preserve branch, never the reap branch.

    ``file_mtime_epoch`` returning nothing used to flow into ``$(( now - ))``,
    and any repair that renders the absent mtime as epoch 0 makes an unreadable
    marker look maximally old — i.e. reapable — which is absence rendering as
    the favourable value. The marker here IS past the grace window by mtime; it
    is only the mtime probe that is unavailable.
    """
    claim_id = "sha256:no-mtime"
    marker = _write_marker(tmp_path, claim_id, "{malformed and old", age_secs=3600)
    # Identity probes still work (passthrough); both mtime probes do not.
    path = _stat_shim(
        tmp_path,
        "#!/bin/sh\n"
        'if [ "${1-}" = "-c" ] && [ "${2-}" = "%Y" ]; then exit 1; fi\n'
        'if [ "${1-}" = "-f" ] && [ "${2-}" = "%m" ]; then exit 1; fi\n'
        'exec /usr/bin/stat "$@"\n',
    )
    result = _run_with_env(tmp_path, claim_id, "reviewer", {"PATH": path})

    assert result.returncode == 2, (
        f"unknown age must refuse, not reap: rc={result.returncode} {result.stderr!r}"
    )
    assert marker.exists(), "a marker of unknown age must never be reaped"
    # Behavior, not a specific clock reading: two `now_epoch` calls a wall-clock
    # second apart (a real, valid rollover, not a bug) would otherwise report
    # "only 1s old" and fail this exact-string assertion on a timing flake, not
    # a defect (confirmed reproducible: PR #253 round-2 confirm, F2). Match the
    # malformed/preserved behavior's shape instead of pinning the digit.
    assert re.search(r"only [0-9]+s old", result.stderr), result.stderr


def test_f1_toctou_reap_still_deletes_when_identity_is_unchanged(tmp_path):
    """Sanity check for the same hook: when nothing changes, the legitimate reap
    still happens (the hook must not just always abort)."""
    claim_id = "sha256:toctou-clean"
    past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    marker = _write_marker(
        tmp_path,
        claim_id,
        json.dumps({"actor": "old", "at": "x", "expires_at": past}),
    )
    result = _run_with_env(tmp_path, claim_id, "old", {"CHECK_CLAIMS_TEST_TOCTOU_HOOK": ":"})
    assert result.returncode == 1
    assert "reaped" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("bad_id", ["../victim", "a/../../etc/passwd", "sub/dir", "id;rm -rf"])
def test_f2_path_traversal_id_is_rejected_before_touching_disk(tmp_path, bad_id):
    victim = tmp_path.parent / "victim-outside-claims-dir.claim"
    victim.write_text(json.dumps({"actor": "x", "at": "x", "expires_at": "2000-01-01T00:00:00Z"}))
    claims_dir = tmp_path / "claims"
    claims_dir.mkdir()
    try:
        result = _run(claims_dir, bad_id, "someone")
        assert result.returncode == 2, f"expected malformed exit for id={bad_id!r}, got {result.returncode}"
        assert victim.exists(), "a malformed/traversal id must never delete anything outside claims-dir"
    finally:
        if victim.exists():
            victim.unlink()


def test_f2_realpath_containment_still_holds_for_resolved_marker(tmp_path):
    """Even an id that passes the allowlist must resolve to a path under the
    resolved claims dir (defense in depth beyond the character allowlist)."""
    claims_dir = tmp_path / "claims"
    claims_dir.mkdir()
    # A perfectly boring, allowlisted id must resolve under claims_dir and be usable.
    claim_id = "sha256:boring-ok"
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    _write_marker(claims_dir, claim_id, json.dumps({"actor": "a", "at": "x", "expires_at": future}))
    result = _run(claims_dir, claim_id, "a")
    assert result.returncode == 0


@pytest.mark.parametrize(
    "expires_suffix",
    [
        ".000Z",           # fractional seconds + bare Z
        ".123456Z",        # microsecond precision
        "+00:00",          # explicit UTC offset instead of Z
    ],
)
def test_f3_alternate_iso8601_expires_at_forms_are_understood(tmp_path, expires_suffix):
    claim_id = "sha256:iso-" + expires_suffix.strip(".:+")
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 3600))
    expires_at = base + expires_suffix
    marker = _write_marker(
        tmp_path,
        claim_id,
        json.dumps({"actor": "a", "at": "x", "expires_at": expires_at}),
    )
    result = _run(tmp_path, claim_id, "a")
    assert result.returncode == 0, (
        f"a future expires_at ({expires_at!r}) in a valid alternate ISO-8601 form "
        f"must be understood as held, not treated as unparseable. stderr={result.stderr!r}"
    )
    assert marker.exists(), "a valid, unexpired claim must never be deleted"


def test_f3_alternate_iso8601_form_not_reaped_even_when_marker_file_is_old(tmp_path):
    """The dangerous variant of F3: a far-future claim written in a format the old
    date(1)-sniffing code could not parse, aged past the grace window, must still be
    recognized as a valid held claim and NOT reaped."""
    claim_id = "sha256:iso-old-file-future-claim"
    marker = _write_marker(
        tmp_path,
        claim_id,
        json.dumps({"actor": "a", "at": "x", "expires_at": "2099-01-01T00:00:00.000Z"}),
        age_secs=900,
    )
    result = _run(tmp_path, claim_id, "a", grace=600)
    assert result.returncode == 0
    assert marker.exists(), "a far-future claim must survive regardless of on-disk file age"


def test_f5_malformed_json_is_reported_malformed_not_silently_treated_as_valid(tmp_path):
    """Regression for the dead PARSE_RC/PY_RC check: totally invalid JSON, aged
    young, must be reported malformed (exit 2) and left alone — not silently fall
    through as if it parsed."""
    claim_id = "sha256:garbage"
    marker = _write_marker(tmp_path, claim_id, "not even close to json", age_secs=1)
    result = _run(tmp_path, claim_id, "someone")
    assert result.returncode == 2
    assert marker.exists()


def test_age_equals_grace_boundary_is_reaped_not_kept(tmp_path):
    """Documented boundary: AGE >= grace reaps (the check is strictly `<` for the
    keep-alive branch), so AGE == grace exactly falls on the reap side."""
    claim_id = "sha256:boundary"
    marker = _write_marker(tmp_path, claim_id, "{not json", age_secs=600)
    result = _run(tmp_path, claim_id, "someone", grace=600)
    assert result.returncode == 1
    assert not marker.exists()
    assert "reaped" in result.stderr
