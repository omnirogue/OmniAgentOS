#!/usr/bin/env python3
"""Mechanically-verified lane ledger — evidence, not self-reports.

WHY THIS SHAPE
--------------
The received wisdom is that one agent can manage about five agents. That is true
only when the manager holds each agent's state in its own context: the cost is
O(context) per managed agent, and context is the scarce resource.

If agents instead write to a ledger and the manager QUERIES it, the manager holds
nothing. The cost becomes O(1) per query and the fan-out limit stops being about
management capacity at all.

But a ledger of agent SELF-REPORTS buys nothing, because a self-report is a claim.
Tonight, in this repo: a verdict was recorded before the verifier finished; three
code mutations silently failed to apply and produced green runs against unmodified
files; the coordinator reported "3 verifiers running" when zero were; a lane was
created with an empty tree and returned success. Every one of those would have
entered a self-report ledger as good news.

So every field below is produced by EXECUTING something. The ledger never asks an
agent how it is doing; it looks. `state` is derived from bytes on disk and exit
codes, and anything undetermined is recorded as `unknown` — never as a favourable
default, which is the defect class this entire repo exists to eliminate.

USAGE
    fleet-ledger.py scan            # refresh the ledger (runs the checks)
    fleet-ledger.py query <state>   # e.g. `awaiting_verdict`, `mergeable`
    fleet-ledger.py summary         # counts by state
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(os.environ.get("REPO", "/Users/youruser/OmniAgentOS"))
CLONES = REPO / "var" / "swarm" / "clones"
VERDICTS = REPO / "var" / "swarm" / "verdicts"
LEDGER = REPO / "var" / "swarm" / "fleet-ledger.jsonl"
STALL_SECONDS = int(os.environ.get("STALL_SECONDS", "720"))
IGNORE = {".git", "node_modules", ".venv", "__pycache__"}


def _run(args: list[str], cwd: Path | None = None, timeout: int = 60):
    """Execute and return (rc, stdout). Never raises — a failed probe is data."""
    try:
        p = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as exc:  # noqa: BLE001 — a probe that cannot run is 'unknown', not 'fine'
        return None, f"probe failed: {exc}"


def _changed_files(lane: Path) -> int | None:
    rc, out = _run(["git", "status", "--porcelain"], cwd=lane)
    if rc != 0:
        return None  # unknown — NOT zero. Zero would read as "clean".
    return sum(
        1
        for line in out.splitlines()
        if line.strip() and not any(tok in line for tok in (".venv", "node_modules"))
    )


def _head_sha(lane: Path) -> str | None:
    rc, out = _run(["git", "rev-parse", "HEAD"], cwd=lane)
    return out.strip() if rc == 0 and out.strip() else None


def _ahead_and_merged(lane: Path, branch: str | None) -> tuple[int | None, bool]:
    """Position of the lane relative to main, asked of the PRIMARY repo.

    Lanes are full clones, each carrying its own `main` ref that goes stale the moment the
    real main advances. Asking `main..branch` INSIDE the clone therefore reported lanes as
    1 commit ahead when their work was already an ancestor of the real main — five of six
    sampled lanes were queued for a merge that had already happened.

    The lane's HEAD sha is the portable fact. The primary repo is the only authority on
    where that sha sits, so ask it there. A sha the primary cannot see is `unknown` — the
    lane needs fetching — and never 'not merged', which would re-merge landed work.
    """
    sha = _head_sha(lane)
    if not sha or branch == "main":
        return None, False
    if _run(["git", "cat-file", "-e", sha], cwd=REPO)[0] != 0:
        return None, False  # unreachable from primary: unknown, not a conclusion
    merged = _run(["git", "merge-base", "--is-ancestor", sha, "main"], cwd=REPO)[0] == 0
    rc, out = _run(["git", "rev-list", "--count", f"main..{sha}"], cwd=REPO)
    try:
        ahead = int(out.strip()) if rc == 0 else None
    except ValueError:
        ahead = None
    return ahead, merged


def _last_write_age(lane: Path) -> float | None:
    """Seconds since the newest real file write. Detects the STALL that a live
    process hides: two leads sat blocked on prompts for hours looking alive."""
    newest = None
    for root, dirs, files in os.walk(lane):
        dirs[:] = [d for d in dirs if d not in IGNORE]
        for f in files:
            try:
                m = os.path.getmtime(os.path.join(root, f))
            except OSError:
                continue
            if newest is None or m > newest:
                newest = m
    return None if newest is None else time.time() - newest


def _branch(lane: Path) -> str | None:
    rc, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=lane)
    return out if rc == 0 else None


def _verdict(branch: str | None) -> dict:
    """A verdict is only real if a file exists AND names an anthropic verifier AND
    carries a VERDICT line. Absent or malformed is NOT approval."""
    if not branch:
        return {"present": False, "reason": "no branch"}
    f = VERDICTS / (branch.replace("/", "_") + ".md")
    if not f.exists():
        return {"present": False, "reason": "no verdict file"}
    text = f.read_text(errors="ignore")
    word = _verdict_word(text)
    names_anthropic = any(m in text.lower() for m in ("opus", "claude", "sonnet", "fable", "haiku"))
    ok = word == "APPROVE" and names_anthropic
    return {
        "present": ok,
        "reason": (
            "ok" if ok
            else "no VERDICT line" if word is None
            else "REJECTED" if word in ("REJECT", "FAIL")
            else "unclear VERDICT line" if word == "UNCLEAR"
            else "names no anthropic verifier"
        ),
    }


VERDICT_RE = re.compile(r"^\**\s*VERDICT\**\s*[:=-]\s*\**\s*([A-Z]+)", re.I)


def _verdict_word(text: str) -> str | None:
    """Return the verdict from its LINE, or None if no line declares one.

    Scanning the whole document for "REJECT" matches reviewers' prose — "I found no reason
    to reject this" — and 24 of 44 approved verdicts in this repo contain exactly that.

    THIS IS A SECOND IMPLEMENTATION AND IT DISAGREES WITH THE FIRST. An earlier version of
    this docstring claimed the parse "now lives in one place"; it does not, and the claim
    actively misled a review. `scripts/lib/verdict-grammar.sh` is the one every merge
    carrier uses. Measured against it (executed, 2026-08-06): 7 of 9 grammar cases
    disagree, 3 of them FAIL-OPEN —

        VERDICT: APPROVE / later VERDICT: REJECT  ledger APPROVE, grammar REJECT
                                                  (first match wins here; there is no
                                                   refusal precedence)
        Verdict: Approve-flow lane                ledger APPROVE, grammar NONE
                                                  (values are matched by PREFIX here)
          VERDICT: APPROVE  (indented)            ledger APPROVE, grammar NONE
                                                  (it strips the line first, so quoted
                                                   material decides)

    and 4 more where a REFUSAL is invisible to it (`## VERDICT: REJECT`,
    `_VERDICT: REJECT_`, `- VERDICT: REJECT`, `VERDICT: REWORK` all read as "no VERDICT
    line"). Over the 63 live artifacts in var/swarm/verdicts today: 1 disagreement
    (fix_modelintel-sweep.md, ledger NONE vs grammar APPROVE), 0 fail-open. The exposure
    is latent, not currently realised.

    WHY IT IS STILL HERE. Converting it is deliberately deferred: `_verdict()` above turns
    `present` into `state="mergeable"`, and that state has a WIDER blast radius than the
    ledger —

        scripts/commit-verified-lanes.sh:31  queries `mergeable` and COMMITS the lane's
                                             working tree
        scripts/standing-roles.sh:153        announces "N lane(s) verified and ready" to
                                             the operator

    so a lane this parser misreads is dropped from both pump queues, has its work
    committed, AND is presented to a human as verified. A change here therefore needs its
    own lane with its own before/after over the live corpus and those two consumers — not
    a drive-by edit inside a grammar lane.
    """
    for line in text.splitlines():
        m = VERDICT_RE.match(line.strip())
        if m:
            w = m.group(1).upper()
            if w.startswith("APPROVE") or w in ("PASS", "CONFIRM"):
                return "APPROVE"
            if w.startswith("REJECT"):
                return "REJECT"
            if w.startswith("FAIL"):
                return "FAIL"
            return "UNCLEAR"
    return None


def _sol_verdict(lane_name: str) -> str | None:
    """Classify the first-pass (gpt-5.6-sol) review, from its VERDICT LINE only.

    The first version of this scanned the whole document for the string "REJECT" and so
    matched reviewers' prose ("no reason to reject"), classifying 21 approvals as zero. The
    verdict is a LINE, not a substring: parse it as one, and return None when there is no
    line to parse rather than guessing.
    """
    for f in (REPO / "var" / "swarm" / "sol-verdicts" / f"{lane_name}.md",
              CLONES / lane_name / "var" / "sol-verdict.md"):
        if not f.exists() or f.stat().st_size == 0:
            continue
        w = _verdict_word(f.read_text(errors="ignore"))
        if w:
            return w
    return None


def _agent_alive(lane_name: str) -> bool:
    rc, out = _run(["pgrep", "-f", f"clones/{lane_name}"])
    return bool(out.strip())


def scan() -> list[dict]:
    rows = []
    for lane in sorted(p for p in CLONES.glob("*") if (p / ".git").exists()):
        changed = _changed_files(lane)
        age = _last_write_age(lane)
        branch = _branch(lane)
        ahead, merged = _ahead_and_merged(lane, branch)
        # A lane has DELIVERED if it has commits main lacks, or uncommitted work. Either is
        # output; only neither is emptiness.
        delivered = (ahead or 0) > 0 or (changed or 0) > 0
        verdict = _verdict(branch)
        alive = _agent_alive(lane.name)

        # State is DERIVED from evidence. Order matters: the most actionable
        # condition wins, and 'unknown' is a real outcome rather than a fallback
        # to something comfortable.
        sol = _sol_verdict(lane.name)

        if changed is None:
            state = "unknown"
        elif alive and age is not None and age > STALL_SECONDS:
            # A LIVE agent outranks everything below. Checking `merged` first classified a
            # lane whose branch was already in main as "landed" even while an agent was
            # actively working in it — hiding running work behind a finished label.
            state = "silent"
        elif alive:
            state = "working"
        elif merged and not delivered:
            # Its commits are ancestors of main AND it has nothing new. Done; keep it out of
            # every queue. `merged` alone is not enough — a merged lane can carry fresh
            # uncommitted work, and that work is not landed just because its history is.
            state = "landed"
        elif verdict["present"] and delivered:
            state = "mergeable"
        elif verdict["reason"] == "REJECTED":
            # An anthropic REJECT used to fall through to "awaiting_verdict" — refused work
            # rendered as un-reviewed work. That is the favourable-default defect this file
            # exists to refuse, sitting in the file itself.
            state = "rejected"
        elif delivered and not alive and sol == "APPROVE":
            # First-pass reviewed. Needs BATCHING into an integration branch and then ONE
            # anthropic verdict on the aggregate — not one anthropic verdict per lane.
            state = "ready_to_integrate"
        elif delivered and not alive and sol in ("REJECT", "FAIL"):
            state = "needs_rework"
        elif delivered and not alive:
            state = "awaiting_verdict"
        elif not delivered:
            state = "empty"
        else:
            state = "unknown"

        rows.append(
            {
                "lane": lane.name,
                "branch": branch,
                "changed_files": changed,
                "commits_ahead": ahead,
                "merged_into_main": merged,
                "seconds_since_write": None if age is None else int(age),
                "agent_alive": alive,
                "verdict_ok": verdict["present"],
                "verdict_reason": verdict["reason"],
                "sol_verdict": sol,
                "state": state,
            }
        )
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return rows


def _load() -> list[dict]:
    if not LEDGER.exists():
        return scan()
    return [json.loads(x) for x in LEDGER.read_text().splitlines() if x.strip()]


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd == "scan":
        rows = scan()
    else:
        rows = _load()

    if cmd == "query":
        want = sys.argv[2] if len(sys.argv) > 2 else "awaiting_verdict"
        hits = [r for r in rows if r["state"] == want]
        for r in hits:
            print(f"{r['lane']:<22} {r['branch'] or '?':<26} ahead={r.get('commits_ahead')} files={r['changed_files']} {r['verdict_reason']}")
        print(f"({len(hits)} in state {want!r})")
        return 0

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    total = len(rows)
    print(f"lanes: {total}")
    for k in ("working", "ready_to_integrate", "awaiting_verdict", "needs_rework", "rejected", "mergeable", "silent", "landed", "empty", "unknown"):
        if counts.get(k):
            print(f"  {k:<18} {counts[k]}")
    # Say what needs a human/manager decision, rather than leaving it implicit.
    if counts.get("ready_to_integrate"):
        print(f"\nACTION: {counts['ready_to_integrate']} lane(s) sol-reviewed — BATCH into one integration branch, then ONE anthropic verdict.")
    if counts.get("awaiting_verdict"):
        print(f"ACTION: {counts['awaiting_verdict']} lane(s) finished and NEVER reviewed — dispatch sol.")
    if counts.get("needs_rework") or counts.get("rejected"):
        print(f"ACTION: {counts.get('needs_rework',0)+counts.get('rejected',0)} lane(s) REFUSED by a reviewer — dispatch rework, do not batch.")
    if counts.get("mergeable"):
        print(f"ACTION: {counts['mergeable']} lane(s) verified and ready to merge.")
    if counts.get("silent"):
        print(f"ACTION: {counts['silent']} lane(s) alive but silent >{STALL_SECONDS//60}m — check for a prompt or retry loop.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
