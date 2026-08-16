#!/usr/bin/env python3
"""
WP11: Integration Stage Pipeline.
Parses GPT-5.6 verdicts, detects file conflict overlaps across approved lanes,
lands approved non-conflicting lanes into `integration/reviewed`, and triggers
a single aggregate Opus verification pass.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path("/Users/youruser/OmniAgentOS").resolve()
SOL_VERDICTS_DIR = REPO_ROOT / "var/swarm/sol-verdicts"
CLONES_DIR = REPO_ROOT / "var/swarm/clones"
INTEGRATION_BRANCH = "integration/reviewed"


def run_cmd(args, cwd=REPO_ROOT, env=None, check=True):
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    res = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=full_env,
        check=check,
    )
    return res


# THE VERDICT DECISION IS NOT RE-SPELLED HERE — it is delegated.
#
# This script used its own regex, and it carried the same defect the shell carriers did:
# `re.search(..., MULTILINE)` returns the FIRST match, so a file titled `# Verdict: Approve`
# over a body of `VERDICT: REJECT` returned APPROVED — on the LANDING path, which merges
# lanes. Sol's `# Verdict: Approve-flow lane` survived it only because that string is not an
# exact token, which is luck rather than a guard.
#
# A second implementation of the grammar is how two implementations drift, so there is no
# second implementation: this calls the one in scripts/lib/verdict-grammar.sh, which is the
# same code path `merge-if-only-known-blocker.sh` and `integrate.sh` decide with. It is a
# script (not a library) and already shells out to git, so the bash dependency costs nothing
# new — and unlike the shell gate, nothing here runs before a working shell exists.
# Resolved from THIS FILE, not from REPO_ROOT — the same rule the shell carriers use
# (`dirname "${BASH_SOURCE[0]}"`). A worktree must decide with the grammar it ships,
# not with whatever happens to be in the serving checkout.
VERDICT_GRAMMAR = Path(__file__).resolve().parent / "lib/verdict-grammar.sh"
_VERDICT_DECISIONS = frozenset({"APPROVE", "REJECT", "FAILED", "NONE"})


def verdict_decision(path):
    """APPROVE | REJECT | FAILED | NONE, from the one implementation.

    Raises RuntimeError when the grammar cannot be loaded or answers something
    unrecognised. Callers must treat that as "decides nothing" — never as approval.
    """
    proc = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1" || exit 1; command -v verdict_decision >/dev/null || exit 1; verdict_decision "$2"',
            "verdict-decision",
            str(VERDICT_GRAMMAR),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    answer = proc.stdout.strip()
    if proc.returncode != 0 or answer not in _VERDICT_DECISIONS:
        raise RuntimeError(
            f"{VERDICT_GRAMMAR} rc={proc.returncode} answer={answer!r} {proc.stderr.strip()[:120]}"
        )
    return answer


def clear_verdict_artifact(path):
    """Remove a previous run's artifact, and CHECK that it is gone.

    Same reason as scripts/dispatch-verifier.sh and scripts/review-integration.sh: a
    verifier that exits 0 writing nothing otherwise inherits the last run's file, and an
    `exists()` test cannot tell "this run wrote nothing" from "the last run wrote
    something". On this path that mistake merges to MAIN.

    EVERY failure leaves as RuntimeError. "Already gone" is the only success this may
    swallow. A PermissionError, an IsADirectoryError, a read-only filesystem — anything
    else unlink() raises — used to propagate past the caller, which catches RuntimeError
    only, so the documented refusal ("a stale APPROVE there would merge to main.
    Refusing.") never printed and a traceback replaced it. The net effect stayed
    fail-closed, but the operator was shown a crash instead of the reason.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RuntimeError(f"cannot clear a previous verdict at {path}: {exc}") from exc
    if path.exists():
        raise RuntimeError(f"cannot clear a previous verdict at {path}")


def aggregate_approved(verdict_file):
    """Did the AGGREGATE verifier approve? One implementation, refusal-first.

    This was `"VERDICT: APPROVE" in verdict_content` — a bare substring test whose True
    branch runs merge-gate.sh to main. Any occurrence anywhere satisfied it, including a
    REJECT that quotes the string while explaining why it is wrong (verified: such a file
    merged). The decision comes from the shared grammar, which reads the whole file and
    lets a refusal outrank an approval.
    """
    try:
        return verdict_decision(verdict_file) == "APPROVE"
    except RuntimeError as exc:
        print(f"Aggregate verdict grammar unusable ({exc}) — refusing to merge.")
        return False


def parse_sol_verdicts():
    approved = []
    rejected = []
    failed = []

    if not SOL_VERDICTS_DIR.is_dir():
        return approved, rejected, failed

    for f in sorted(SOL_VERDICTS_DIR.glob("*.md")):
        lane_name = f.stem
        try:
            verdict = verdict_decision(f)
        except RuntimeError as exc:
            # An unusable grammar decides nothing. It must not decide APPROVE.
            failed.append((lane_name, f"verdict grammar unusable: {exc}"))
            continue

        if verdict == "APPROVE":
            approved.append(lane_name)
        elif verdict == "REJECT":
            rejected.append((lane_name, "Rejected by GPT-5.6"))
        elif verdict == "FAILED":
            failed.append((lane_name, "Verifier failed"))
        else:
            failed.append((lane_name, "No VERDICT line found"))

    return approved, rejected, failed


def get_lane_branch_and_files(lane_name):
    lane_dir = CLONES_DIR / lane_name
    if not lane_dir.is_dir():
        return None, set()

    # Get active branch name
    res_branch = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=lane_dir, check=False)
    if res_branch.returncode != 0:
        return None, set()
    branch = res_branch.stdout.strip()

    # Get list of modified files relative to main
    res_diff = run_cmd(["git", "diff", "--name-only", "main...HEAD"], cwd=lane_dir, check=False)
    if res_diff.returncode != 0:
        # Try master...HEAD if main fails
        res_diff = run_cmd(
            ["git", "diff", "--name-only", "master...HEAD"], cwd=lane_dir, check=False
        )

    files = set()
    if res_diff.returncode == 0:
        files = {line.strip() for line in res_diff.stdout.splitlines() if line.strip()}

    return branch, files


def build_non_conflicting_batch(approved_lanes):
    batch = []
    landed_files = set()
    deferred = []

    for lane in approved_lanes:
        branch, files = get_lane_branch_and_files(lane)
        if not branch:
            continue

        # Check if lane files overlap with already processed files in this batch
        overlap = files.intersection(landed_files)
        if overlap:
            deferred.append((lane, overlap))
        else:
            batch.append((lane, branch, files))
            landed_files.update(files)

    return batch, deferred


def main():
    print("=== WP11: Starting Integration Stage Pipeline ===")

    # 1. Parse GPT-5.6 Sol verdicts
    approved, rejected, failed = parse_sol_verdicts()
    print(
        f"Parsed verdicts: Approved={len(approved)}, Rejected={len(rejected)}, Failed={len(failed)}"
    )

    if not approved:
        print("No approved lanes to merge. Exiting.")
        return 0

    # 2. Build conflict-free batch
    batch, deferred = build_non_conflicting_batch(approved)
    print(
        f"Non-conflicting batch size: {len(batch)} lane(s). Deferred due to conflicts: {len(deferred)}"
    )
    for lane, overlap in deferred:
        print(f"  - Deferred {lane} due to overlapping files: {overlap}")

    if not batch:
        print("No lanes eligible for conflict-free landing in this cycle.")
        return 0

    # Ensure main is up to date and create integration branch
    print("Preparing integration branch...")
    run_cmd(["git", "checkout", "main"])
    run_cmd(["git", "pull", "origin", "main"], check=False)
    run_cmd(["git", "checkout", "-B", INTEGRATION_BRANCH, "main"])

    landed_lanes = []

    # 3. Land and merge each lane in the batch
    for lane, branch, _files in batch:
        print(f"Landing lane: {lane} (branch: {branch})...")

        # Land the lane branch using scripts/land-lane.sh
        env = {"LANE_COMMIT_MSG": f"auto-merge: approved work from lane {lane}"}
        land_res = run_cmd(
            ["./scripts/land-lane.sh", lane, branch, "--commit"], env=env, check=False
        )
        if land_res.returncode != 0:
            print(
                f"  - Failed to land lane {lane}. stdout: {land_res.stdout} stderr: {land_res.stderr}"
            )
            continue

        # Merge the fetched branch
        print(f"  - Merging branch {branch} into {INTEGRATION_BRANCH}...")
        merge_res = run_cmd(["git", "merge", branch, "--no-edit"], check=False)
        if merge_res.returncode >= 128:
            # Sibling carrier of the merge-gate.sh trial-merge defect (same
            # VALUE: never blame the branch for the instrument's own failure).
            # git exits >=128 when it could not run the merge AT ALL -- no
            # committer identity, corrupt object, locked index -- and this used
            # to print "Conflict merging <branch>" for it, sending an operator
            # to rebase a branch git never examined. stderr was captured and
            # then dropped; it is the only description of what actually broke.
            print(
                f"  - NOT JUDGED: git merge exited {merge_res.returncode} for {branch} "
                f"(instrument failure, NOT a conflict): {merge_res.stderr.strip()[:300]}"
            )
            run_cmd(["git", "merge", "--abort"], check=False)
            continue
        if merge_res.returncode != 0:
            print(f"  - Conflict merging {branch}. Aborting merge.")
            run_cmd(["git", "merge", "--abort"], check=False)
            continue

        landed_lanes.append((lane, branch))
        print(f"  - Successfully merged {lane}")

    if not landed_lanes:
        print("Failed to land any approved lanes in this cycle.")
        return 1

    # 4. Trigger aggregate Anthropic/Opus verifier
    print(f"Triggering aggregate Opus verifier on {INTEGRATION_BRANCH}...")

    # We select an account deterministically
    accounts = []
    for a in [Path.home() / f".claude-account-{i}" for i in (1, 2, 3, 5)]:
        if a.is_dir():
            accounts.append(a)
    acct = accounts[0] if accounts else (Path.home() / ".claude")

    verdict_file = REPO_ROOT / "var/swarm/verdicts/aggregate_integration.md"
    verdict_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        clear_verdict_artifact(verdict_file)
    except RuntimeError as exc:
        print(f"{exc} — a stale APPROVE there would merge to main. Refusing.")
        return 1

    prompt = f"""AGGREGATE INTEGRATION VERIFIER (opus, anthropic).
You are reviewing the combined, merged integration branch containing these approved lanes:
{", ".join([lane for lane, _ in landed_lanes])}

Read the project tests and run the full test suite using ./.venv/bin/pytest and ./.venv/bin/ruff.
NEVER run uv run or npm.

Verify the integration does not introduce regression conflicts.
Write your verdict to {verdict_file}.
It MUST begin with a line 'VERDICT: APPROVE' or 'VERDICT: REJECT' with detailed logs.
Do NOT commit.
"""

    print(f"Running Claude Opus on account {acct.name}...")
    # Invoke Claude via subprocess (same logic as dispatch-verifier.sh)
    env = {"CLAUDE_CONFIG_DIR": str(acct)}
    verify_cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        "claude-opus-5",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Bash,Read,Grep,Glob,Edit,Write",
    ]

    # We redirect logs
    log_file = REPO_ROOT / "var/swarm/aggregate_verify.log"
    with log_file.open("w", encoding="utf-8") as lf:
        res = subprocess.run(
            verify_cmd,
            cwd=str(REPO_ROOT),
            stdout=lf,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )

    if res.returncode != 0:
        print(
            f"Opus verifier failed or exited non-zero (rc={res.returncode}). See var/aggregate_verify.log"
        )
        verdict_file.write_text(
            f"VERDICT: FAILED (Aggregate verifier returned non-zero rc={res.returncode})\n",
            encoding="utf-8",
        )
        return 1

    if not verdict_file.exists():
        print("Opus verifier finished but did not write any verdict file!")
        verdict_file.write_text("VERDICT: FAILED (No verdict file written)\n", encoding="utf-8")
        return 1

    verdict_content = verdict_file.read_text(encoding="utf-8")
    if aggregate_approved(verdict_file):
        print("=== INTEGRATION APPROVED BY OPUS ===")
        print("Running merge-gate.sh to main...")
        # Now run merge-gate to main
        merge_gate_res = run_cmd(["./scripts/merge-gate.sh"], check=False)
        if merge_gate_res.returncode == 0:
            print("Successfully merged integration into main!")
        else:
            print(f"merge-gate.sh failed (rc={merge_gate_res.returncode}). see logs.")
            return 1
    else:
        print("=== INTEGRATION REJECTED BY OPUS ===")
        print(verdict_content)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
