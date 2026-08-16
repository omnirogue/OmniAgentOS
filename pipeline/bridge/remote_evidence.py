#!/usr/bin/env python3
"""Pin a candidate onto the twin, and bring the evidence home afterward.

These two helpers are the missing halves of remote gate dispatch. gate_host.py
decides WHERE a gate should run and how to invoke it; nothing existed to
(a) make the candidate ref resolvable in the twin's gate workspace before
dispatch, or (b) copy the signed receipt and evidence records back afterward.
Without (b), every remote verdict is receipt-less on this host, and
classify_gate correctly downgrades receipt-less defect claims to
instrument-error — so remote dispatch would silently convert real candidate
defects into instrument noise. That is why --allow-remote-gate stayed off.

Contracts honored here (measured doctrine, do not relax):
  * ONE ssh round-trip per operation, no retry storms.
  * Every failure is an INSTRUMENT fact. Callers must fall back to local or
    report instrument-error; nothing here may ever be recorded against a
    candidate.
  * Null-vs-zero: an unverifiable state is reported as not-ok with a reason,
    never as success by default.
  * The twin's rsync is Apple openrsync (protocol 29), not GNU rsync 3.x —
    only conservative flags (-a, -z, --timeout) are used.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

try:                                    # module or script
    from bridge.gate_host import SSH_PROBE_TIMEOUT_S
except ImportError:                     # pragma: no cover
    from gate_host import SSH_PROBE_TIMEOUT_S


def _ssh_argv(host: str, remote_cmd: str) -> list:
    return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_PROBE_TIMEOUT_S}",
            host, remote_cmd]


def _shquote(value) -> str:
    import shlex
    return shlex.quote(str(value))


def sync_forward_candidate_receipt(host: str, *, local_receipt: str,
                                   remote_receipt: str, timeout_s: int = 120,
                                   runner=subprocess.run) -> dict:
    """Put the pre-gate, candidate-bound receipt where the twin gate reads it.

    Section 0 of merge-gate requires ``records/merge-gate/<tip>.json`` to exist
    *before* the suites start. Sync-back alone is therefore too late. The
    receipt is minted and signed locally against the exact train tip, then this
    helper creates only its destination directory and copies that one record.
    Any transport failure is an instrument fact; callers must not run a remote
    gate that would misclassify the missing prerequisite as a code defect.
    """
    source = Path(local_receipt)
    if not source.is_file():
        return {"ok": False, "why": f"local candidate receipt absent: {source}"}
    remote_dir = str(Path(remote_receipt).parent)
    try:
        proc = runner(_ssh_argv(host, f"mkdir -p {_shquote(remote_dir)}"),
                      capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False,
                "why": f"remote receipt mkdir unrunnable: {type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "why": f"remote receipt mkdir failed rc={proc.returncode}: "
                f"{(proc.stderr or '').strip()[:200]}"}
    try:
        proc = runner(
            ["scp", "-o", "BatchMode=yes",
             "-o", f"ConnectTimeout={SSH_PROBE_TIMEOUT_S}",
             str(source), f"{host}:{remote_receipt}"],
            capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False,
                "why": f"candidate receipt scp unrunnable: {type(exc).__name__}: {exc}"}
    if proc.returncode != 0:
        return {"ok": False, "why": f"candidate receipt scp failed rc={proc.returncode}: "
                f"{(proc.stderr or '').strip()[:200]}"}
    return {"ok": True, "why": "candidate-bound receipt synced to twin"}


def pin_remote_candidate(host: str, workspace: str, branch: str, tip_sha: str,
                         *, local_repo: str, checkout: bool = False,
                         timeout_s: int = 120,
                         runner=subprocess.run) -> dict:
    """Make `branch` resolvable in the twin workspace at EXACTLY `tip_sha`.

    The objects travel by DIRECT PUSH from this host into the twin workspace
    (`git push ssh://<host><workspace> +<tip_sha>:refs/heads/<branch>`), not by
    a twin-side fetch from origin — measured 2026-08-08: the twin has no GitHub
    credentials, and the branches that matter (queued integration trains) are
    local-only anyway. This host has both the objects and the credentials; the
    twin needs neither. Pushing the SHA rather than the branch name also means
    a branch that MOVED between queueing and dispatch still pins the exact tip
    the candidate record names, never the mover.

    The gate workspaces sit on detached HEAD, so refs/heads/* are safe push
    targets. `checkout=True` additionally detaches the twin working tree onto
    the tip — for run-class workspaces (pytest) that must BE at the SHA; gate
    workspaces must keep their own pinned-main state, so gates pass False.

    A second, separate ssh round-trip verifies the ref landed at the exact
    tip. Returns {"ok": bool, "why": str, "remote_sha": str|None}.
    """
    if not branch or not tip_sha:
        return {"ok": False, "why": "pin requires both a branch and a tip sha",
                "remote_sha": None}
    push_cmd = ["git", "-C", local_repo, "push", "--quiet",
                f"ssh://{host}{workspace}", f"+{tip_sha}:refs/heads/{branch}"]
    try:
        proc = runner(push_cmd, capture_output=True, text=True,
                      timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "why": f"pin push unrunnable: {type(exc).__name__}: {exc}",
                "remote_sha": None}
    if proc.returncode != 0:
        return {"ok": False,
                "why": f"pin push failed rc={proc.returncode}: "
                       f"{(proc.stderr or '').strip()[:200]}",
                "remote_sha": None}

    b = _shquote(branch)
    ws = _shquote(workspace)
    verify = f"cd {ws} && git rev-parse --verify {b}^{{commit}}"
    if checkout:
        verify += f" && git checkout --quiet --detach {_shquote(tip_sha)} && git rev-parse HEAD"
    try:
        proc = runner(_ssh_argv(host, verify), capture_output=True, text=True,
                      timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "why": f"pin verify unrunnable: {type(exc).__name__}: {exc}",
                "remote_sha": None}
    if proc.returncode != 0:
        return {"ok": False,
                "why": f"pin verify failed rc={proc.returncode}: "
                       f"{(proc.stderr or '').strip()[:200]}",
                "remote_sha": None}
    remote_sha = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else ""
    if remote_sha != tip_sha:
        return {"ok": False,
                "why": (f"pin mismatch: twin resolved {branch} to "
                        f"{remote_sha[:12] or '<nothing>'} but this host queued "
                        f"{tip_sha[:12]} — refusing to grade a different SHA than "
                        "the record names"),
                "remote_sha": remote_sha or None}
    return {"ok": True, "why": "pinned by direct push", "remote_sha": remote_sha}


def sync_back_evidence(host: str, *, remote_receipt: str, local_receipt: str,
                       remote_records_dir: str, local_records_dir: str,
                       timeout_s: int = 300, runner=subprocess.run) -> dict:
    """Bring the remote receipt and evidence records back BEFORE anything lands.

    Two transfers, both mandatory:
      1. the adapter receipt written by the remote gate -> the exact local
         receipt path classify_gate() was handed, so verdict classification
         sees the same file a local run would have produced;
      2. the twin's evidence records dir -> the local store (doctrine:
         'after any twin gate, rsync var/gate-evidence/records/ back to the
         local store BEFORE landing').

    Returns {"ok": bool, "why": str, "synced": [str, ...]}. Any transfer
    failure is the caller's signal to classify the whole run as
    instrument-error: a remote verdict whose evidence did not come home is
    prose, not evidence.
    """
    synced: list = []

    scp_cmd = ["scp", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_PROBE_TIMEOUT_S}",
               f"{host}:{remote_receipt}", local_receipt]
    try:
        proc = runner(scp_cmd, capture_output=True, text=True,
                      timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "why": f"receipt scp unrunnable: {type(exc).__name__}: {exc}",
                "synced": synced}
    if proc.returncode != 0:
        return {"ok": False,
                "why": f"receipt scp failed rc={proc.returncode}: "
                       f"{(proc.stderr or '').strip()[:200]}",
                "synced": synced}
    synced.append(local_receipt)

    # Trailing slash on the source: copy the CONTENTS of records/ into the
    # local records dir. Conservative flags only — the twin ships openrsync.
    rsync_cmd = ["rsync", "-az", f"--timeout={timeout_s}",
                 "-e", f"ssh -o BatchMode=yes -o ConnectTimeout={SSH_PROBE_TIMEOUT_S}",
                 f"{host}:{remote_records_dir.rstrip('/')}/",
                 f"{local_records_dir.rstrip('/')}/"]
    try:
        proc = runner(rsync_cmd, capture_output=True, text=True,
                      timeout=timeout_s + 30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "why": f"records rsync unrunnable: {type(exc).__name__}: {exc}",
                "synced": synced}
    if proc.returncode != 0:
        return {"ok": False,
                "why": f"records rsync failed rc={proc.returncode}: "
                       f"{(proc.stderr or '').strip()[:200]}",
                "synced": synced}
    synced.append(local_records_dir)
    return {"ok": True, "why": "receipt and records synced", "synced": synced}
