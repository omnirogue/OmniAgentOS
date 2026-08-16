"""Contract tests for bridge/remote_evidence.py — the pin + sync-back halves
of remote gate dispatch.

Everything here runs against an injected `runner`; no test touches ssh, git,
the network, or the twin. The contracts pinned:

  * pin is a DIRECT PUSH from the local repo (the twin holds no credentials
    and queued train branches are local-only), pushing the exact SHA — never
    the branch name — followed by a separate remote verify. It refuses on
    missing branch/sha, push failure, verify failure, SHA mismatch, and
    unrunnable transport, and never raises: every refusal is an instrument
    fact the caller reports as one.
  * checkout=True (run-class workspaces) detaches the twin tree onto the tip;
    gate pins must NOT pass it — the gate workspace keeps its own pinned-main
    state.
  * sync-back is two mandatory transfers in order (receipt scp, records
    rsync); the first failure aborts and reports which transfers landed.
  * null-vs-zero: no code path returns ok=True without positive evidence.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.remote_evidence import (
    pin_remote_candidate,
    sync_back_evidence,
    sync_forward_candidate_receipt,
)


class FakeProc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def make_runner(script):
    """script: list of FakeProc or Exception, consumed per call; records argv."""
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        item = script[min(len(calls) - 1, len(script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    runner.calls = calls
    return runner


TIP = "a" * 40
KW = dict(local_repo="/local/repo")


# ----------------------------------------------------------- sync-forward --


def test_sync_forward_candidate_receipt_mkdir_then_scp(tmp_path):
    local = tmp_path / "candidate.json"
    local.write_text('{"signed":true}')
    r = make_runner([FakeProc(0), FakeProc(0)])

    res = sync_forward_candidate_receipt(
        "twin", local_receipt=str(local),
        remote_receipt="/ev/records/merge-gate/tip.json", runner=r)

    assert res["ok"] is True
    assert len(r.calls) == 2
    assert r.calls[0][0] == "ssh" and "mkdir -p" in r.calls[0][-1]
    assert r.calls[1][0] == "scp"
    assert r.calls[1][-2:] == [str(local), "twin:/ev/records/merge-gate/tip.json"]


def test_sync_forward_refuses_absent_local_receipt_without_transport(tmp_path):
    r = make_runner([FakeProc(0)])
    res = sync_forward_candidate_receipt(
        "twin", local_receipt=str(tmp_path / "absent.json"),
        remote_receipt="/ev/records/merge-gate/tip.json", runner=r)
    assert res["ok"] is False
    assert r.calls == []


def test_sync_forward_transport_failure_is_not_success(tmp_path):
    local = tmp_path / "candidate.json"
    local.write_text('{"signed":true}')
    r = make_runner([FakeProc(0), FakeProc(1, err="no route")])
    res = sync_forward_candidate_receipt(
        "twin", local_receipt=str(local),
        remote_receipt="/ev/records/merge-gate/tip.json", runner=r)
    assert res["ok"] is False
    assert "scp failed" in res["why"]


# ------------------------------------------------------------------- pin --


def test_pin_requires_branch_and_sha():
    r = make_runner([FakeProc()])
    assert pin_remote_candidate("twin", "/ws", "", TIP, runner=r, **KW)["ok"] is False
    assert pin_remote_candidate("twin", "/ws", "b", "", runner=r, **KW)["ok"] is False
    assert r.calls == []          # refused before any transport


def test_pin_happy_path_pushes_sha_then_verifies():
    r = make_runner([FakeProc(0), FakeProc(0, out=TIP + "\n")])
    res = pin_remote_candidate("twin", "/ws", "feat/x", TIP, runner=r, **KW)
    assert res["ok"] is True and res["remote_sha"] == TIP
    assert len(r.calls) == 2
    push, verify = r.calls
    assert push[0] == "git" and "push" in push
    # the refspec pushes the exact SHA, not the branch name, to the twin ref
    assert push[-1] == f"+{TIP}:refs/heads/feat/x"
    assert push[-2] == "ssh://twin/ws"
    assert verify[0] == "ssh" and "BatchMode=yes" in verify


def test_pin_gate_mode_never_touches_the_working_tree():
    r = make_runner([FakeProc(0), FakeProc(0, out=TIP + "\n")])
    pin_remote_candidate("twin", "/ws", "feat/x", TIP, runner=r, **KW)
    assert "checkout" not in r.calls[1][-1]   # checkout defaults to False


def test_pin_checkout_mode_detaches_onto_the_tip():
    out = TIP + "\n" + TIP + "\n"
    r = make_runner([FakeProc(0), FakeProc(0, out=out)])
    res = pin_remote_candidate("twin", "/ws", "feat/x", TIP, runner=r,
                               checkout=True, **KW)
    assert res["ok"] is True
    assert "git checkout --quiet --detach" in r.calls[1][-1]


def test_pin_mismatch_is_a_refusal():
    r = make_runner([FakeProc(0), FakeProc(0, out="b" * 40 + "\n")])
    res = pin_remote_candidate("twin", "/ws", "feat/x", TIP, runner=r, **KW)
    assert res["ok"] is False and "mismatch" in res["why"]


def test_pin_push_failure_is_not_ok_and_never_raises():
    r = make_runner([FakeProc(128, err="fatal: not a git repository")])
    res = pin_remote_candidate("twin", "/ws", "feat/x", TIP, runner=r, **KW)
    assert res["ok"] is False and "push failed rc=128" in res["why"]
    assert len(r.calls) == 1      # verify never attempted after a failed push


def test_pin_verify_failure_is_not_ok():
    r = make_runner([FakeProc(0), FakeProc(1, err="unknown revision")])
    res = pin_remote_candidate("twin", "/ws", "feat/x", TIP, runner=r, **KW)
    assert res["ok"] is False and "verify failed" in res["why"]


def test_pin_transport_exception_is_contained():
    r = make_runner([subprocess.TimeoutExpired(cmd="git", timeout=120)])
    res = pin_remote_candidate("twin", "/ws", "feat/x", TIP, runner=r, **KW)
    assert res["ok"] is False and "unrunnable" in res["why"]


def test_pin_quotes_hostile_refs_in_the_remote_verify():
    import shlex
    hostile = "feat/x; rm -rf /"
    r = make_runner([FakeProc(0), FakeProc(0, out=TIP + "\n")])
    pin_remote_candidate("twin", "/ws", hostile, TIP, runner=r, **KW)
    # push side: argv list, no shell — the hostile ref is a single argv element
    assert r.calls[0][-1] == f"+{TIP}:refs/heads/{hostile}"
    # verify side: the ref appears only as quoted data; `rm` never becomes a
    # command word of its own after shell tokenization
    remote_cmd = r.calls[1][-1]
    assert f"'{hostile}'" in remote_cmd
    assert "rm" not in [tok for tok in shlex.split(remote_cmd)
                        if "feat/x" not in tok]


# -------------------------------------------------------------- sync-back --


def test_sync_back_happy_path_two_transfers_in_order():
    r = make_runner([FakeProc(0), FakeProc(0)])
    res = sync_back_evidence("twin", remote_receipt="/ev/r.json",
                             local_receipt="/tmp/r.json",
                             remote_records_dir="/ev/records/merge-gate",
                             local_records_dir="/local/records/merge-gate",
                             runner=r)
    assert res["ok"] is True and len(res["synced"]) == 2
    assert r.calls[0][0] == "scp" and r.calls[1][0] == "rsync"
    # trailing slash on the rsync SOURCE: contents, not the dir itself
    assert r.calls[1][-2].endswith("/merge-gate/")


def test_sync_back_receipt_failure_aborts_before_records():
    r = make_runner([FakeProc(1, err="scp: no such file")])
    res = sync_back_evidence("twin", remote_receipt="/ev/r.json",
                             local_receipt="/tmp/r.json",
                             remote_records_dir="/ev/records/merge-gate",
                             local_records_dir="/local/records/merge-gate",
                             runner=r)
    assert res["ok"] is False and res["synced"] == []
    assert len(r.calls) == 1      # rsync never attempted after a failed receipt


def test_sync_back_records_failure_reports_partial():
    r = make_runner([FakeProc(0), FakeProc(23, err="rsync: link_stat failed")])
    res = sync_back_evidence("twin", remote_receipt="/ev/r.json",
                             local_receipt="/tmp/r.json",
                             remote_records_dir="/ev/records/merge-gate",
                             local_records_dir="/local/records/merge-gate",
                             runner=r)
    assert res["ok"] is False
    assert res["synced"] == ["/tmp/r.json"]   # receipt landed, records did not
    assert "rc=23" in res["why"]


def test_sync_back_transport_exception_is_contained():
    r = make_runner([OSError("scp missing")])
    res = sync_back_evidence("twin", remote_receipt="/ev/r.json",
                             local_receipt="/tmp/r.json",
                             remote_records_dir="/ev/records/merge-gate",
                             local_records_dir="/local/records/merge-gate",
                             runner=r)
    assert res["ok"] is False and "unrunnable" in res["why"]
