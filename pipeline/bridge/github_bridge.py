#!/usr/bin/env python3
"""GitHub -> loop-queue bridge. See BRIDGE.md.

External contributors never touch ``var/loopqueue/``: it is git-ignored, so it exists
only on the loop host, and it holds the queue, the rejection reasoning and the
full history. GitHub is the boundary. This polls it.

  issues labelled `suggestion`  -> inquiries/   (a research question)
  issues labelled `pipeline`    -> inquiries/   (area: tooling, urgency: high)
  new pull requests             -> findings/    (source: external-pr)
  PR review comments on ours    -> findings/    (source: pr-review-comment)

Design rules this file exists to honour, each of which has cost someone a day:

* **Artifacts are written by role `external`, never `integration`.** A finding
  attributed to Integration looks self-generated; nothing downstream would know
  a person wrote it.
* **No timestamp, poll counter or run id in ``payload``.** ``id`` is the hash of
  the payload, so a clock in there makes every poll a brand-new artifact and
  floods the queue. The GitHub URL is the identity; the poll time is not.
* **API failure is `instrument_error`, never silence and never a defect.** A
  bridge that dies quietly turns the boundary off, and the operator's only
  signal is that suggestions stopped arriving.
* **Terminal errors are terminal.** Auth, quota, suspension: park after
  MAX_ATTEMPTS, alert once. Never blind-retry — 3,951 launches at a terminal
  error cost $600 and completed nothing.

Usage:
    github_bridge.py --repo owner/name --loops-root /path/to/var/loopqueue [--once] [--dry-run] [--reconcile]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from canonical import content_id  # noqa: E402
from ledger_write import append_event  # noqa: E402

MAX_ATTEMPTS = 5
POLL_SECONDS = 900  # 15 minutes; see BRIDGE.md
TERMINAL_MARKERS = (
    "authentication",
    "bad credentials",
    "401",
    "403",
    "suspended",
    "rate limit",
    "quota",
)


class TerminalError(RuntimeError):
    """Auth/quota/suspension — retrying cannot help. Park and alert once."""


# ---------------------------------------------------------------- primitives


def _gh(args: list[str], *, dry_run: bool = False) -> Any:
    """One `gh` call. Classifies its own failures; never returns a silent empty."""
    if dry_run and args and args[0] not in {"issue", "pr", "api"}:
        return []
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=120, check=False
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        low = err.lower()
        if any(m in low for m in TERMINAL_MARKERS):
            raise TerminalError(err)
        raise RuntimeError(err or f"gh exited {proc.returncode}")
    out = (proc.stdout or "").strip()
    if not out:
        # An empty body is a real answer for a list query, but we must not let
        # "the call failed and printed nothing" read as "there is no work".
        return []
    return json.loads(out)


def _atomic_write(path: Path, obj: dict) -> None:
    """Write via tmp+rename in the same directory (CONTRACT.md §2).

    A half-written JSON file read by a loop mid-poll is exactly what this stops.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.rename(path)


def _append_ledger(root: Path, event: dict) -> None:
    append_event(root, event)


def _alert(root: Path, message: str) -> None:
    """One line, once. Repeated alerts train the operator to ignore them."""
    with open(root / "ALERTS.md", "a", encoding="utf-8") as fh:
        fh.write(f"- {message}\n")


def _already_present(root: Path, artifact_id: str) -> bool:
    """True if this artifact exists anywhere terminal — including rejected/.

    Checking `rejected/` is what stops a closed-and-reopened issue re-entering
    the queue every 15 minutes forever.
    """
    stem = artifact_id.replace(":", "_")
    return any(
        (root / d / f"{stem}.json").exists()
        for d in ("inquiries", "findings", "rejected", "parked")
    )


# ---------------------------------------------------------------- envelopes


def _envelope(kind: str, title: str, payload: dict, *, url: str) -> dict:
    """Build an artifact. `payload` carries NO time-varying field, by contract."""
    return {
        "contract": "v1.1",
        "id": content_id(payload),
        "kind": kind,
        "title": title[:200],
        # created_at sits OUTSIDE payload, so it never perturbs the id.
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "producer": {"role": "external", "actor": "github-bridge"},
        "origin": "external",
        "source_url": url,
        "payload": payload,
    }


def inquiry_from_issue(issue: dict, *, pipeline: bool = False) -> dict:
    body = (issue.get("body") or "").strip()
    payload = {
        "area": "tooling" if pipeline else (issue.get("title") or "unspecified"),
        "observation": body or "(no body supplied)",
        # Required by the schema, and the point of the artifact: what the raiser
        # does NOT know. External issues rarely state it, so say so honestly
        # rather than inventing a confident-sounding gap.
        "why_not_a_fix": (
            "raised externally via GitHub; the reporter did not state what they "
            "are unsure of — treat the scope as unestablished"
        ),
        "evidence_refs": [issue["url"]],
        "urgency": "high" if pipeline else "normal",
    }
    return _envelope("inquiry", issue.get("title") or "external suggestion", payload, url=issue["url"])


def finding_from_pr(pr: dict) -> dict:
    payload = {
        "symptom": (pr.get("title") or "external pull request").strip(),
        "source": "external-pr",
        "source_ref": pr["url"],
        # No class: the bridge saw a PR, it cannot know whether the change is
        # correct. Whoever picks it up classifies it (CONTRACT.md §3).
    }
    return _envelope("finding", f"PR: {pr.get('title', '')}", payload, url=pr["url"])


def finding_from_review_comment(comment: dict, pr_url: str) -> dict:
    payload = {
        "symptom": (comment.get("body") or "").strip()[:2000],
        "source": "pr-review-comment",
        "source_ref": comment.get("html_url") or pr_url,
    }
    title = f"review comment on {pr_url.rsplit('/', 1)[-1]}"
    return _envelope("finding", title, payload, url=comment.get("html_url") or pr_url)


# ---------------------------------------------------------------- the poll


def poll_once(repo: str, root: Path, *, dry_run: bool = False) -> dict[str, int]:
    counts = {"inquiries": 0, "findings": 0, "skipped": 0, "replied": 0}

    def _emit(artifact: dict, directory: str, event: str) -> None:
        if _already_present(root, artifact["id"]):
            counts["skipped"] += 1
            return
        counts["inquiries" if directory == "inquiries" else "findings"] += 1
        if dry_run:
            return
        _atomic_write(root / directory / f"{artifact['id'].replace(':', '_')}.json", artifact)
        _append_ledger(
            root,
            {
                "ts": artifact["created_at"],
                "role": "external",
                "event": event,
                "id": artifact["id"],
                "actor": "github-bridge",
                "detail": {"url": artifact["source_url"]},
            },
        )

    for label, is_pipeline in (("suggestion", False), ("pipeline", True)):
        issues = _gh(
            ["issue", "list", "--repo", repo, "--label", label, "--state", "open",
             "--json", "number,title,body,url", "--limit", "50"]
        )
        for issue in issues:
            art = inquiry_from_issue(issue, pipeline=is_pipeline)
            before = counts["skipped"]
            _emit(art, "inquiries", "inquired")
            # Every inbound suggestion gets a reply. A suggestion that vanishes
            # silently teaches people to stop sending them.
            if counts["skipped"] == before and not dry_run:
                try:
                    _gh(["issue", "comment", str(issue["number"]), "--repo", repo,
                         "--body", "Received and queued for planning. You'll get a "
                                   "reply here with either a plan or the reason it "
                                   "isn't being pursued."])
                    counts["replied"] += 1
                except (RuntimeError, TerminalError):
                    # The artifact landed; failing to comment must not lose it.
                    _alert(root, f"bridge: queued {art['id'][:19]} but could not reply on issue #{issue['number']}")

    prs = _gh(["pr", "list", "--repo", repo, "--state", "open",
               "--json", "number,title,url,author", "--limit", "50"])
    for pr in prs:
        _emit(finding_from_pr(pr), "findings", "found")

    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--loops-root", required=True, type=Path)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--reconcile", action="store_true",
        help="after polling, close findings/ whose PR was merged or closed on "
             "GitHub (see pr_reconcile.py). Off by default so the 15-min "
             "launchd job is unaffected until the operator flips the plist. A "
             "terminal (auth/quota) reconcile error is a hard failure: ok:false "
             "and a nonzero exit — the launchd job must treat nonzero as failed, "
             "never retry-in-a-loop.",
    )
    args = ap.parse_args()

    root: Path = args.loops_root
    if not root.is_dir():
        print(f"loops root does not exist: {root}", file=sys.stderr)
        return 2

    attempts = 0
    while True:
        try:
            counts = poll_once(args.repo, root, dry_run=args.dry_run)
            attempts = 0
            if args.reconcile:
                # Local import: pr_reconcile imports helpers back out of this
                # module, so importing it at module load time would be circular.
                from pr_reconcile import reconcile_once

                try:
                    counts["reconcile"] = reconcile_once(
                        args.repo, root, dry_run=args.dry_run
                    )
                except Exception as exc:
                    # Any exception escaping the reconcile pass — not just
                    # TerminalError, which reconcile_once already reports via
                    # counts["error"] below — must never fall through to the
                    # outer generic `except Exception` handler further down:
                    # THAT one logs an instrument_error and, under --once,
                    # still returns 0. A tombstone/ledger write failure here
                    # means this run's output cannot be trusted; that is a
                    # hard failure, distinct from the TerminalError exit-2
                    # path below, but never 0 and never silent either way.
                    _append_ledger(root, {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "role": "external", "event": "instrument_error",
                        "actor": "github-reconciler",
                        "detail": {"reason": f"{type(exc).__name__}: {exc}"},
                    })
                    print(json.dumps({
                        "ok": False, **counts,
                        "reconcile_exception": f"{type(exc).__name__}: {exc}",
                    }))
                    print(f"reconcile pass raised: {exc}", file=sys.stderr)
                    # NOTE (Ruling #4, 2026-08-09): this reconciler's exit codes
                    # are launchd nonzero-fail signals, NOT the estate
                    # producer-tool convention (0/1/2 = pass/candidate-defect/
                    # could-not-run + 3 do-not-retry). A reconcile that raised is
                    # an instrument error (could-not-run); do not read the literal
                    # 2/3 here through the producer-tool table.
                    return 3  # instrument error — nonzero fail; never 0, never silent
                if counts["reconcile"].get("error"):
                    # Terminal (auth/quota) reconcile failure: never a silent
                    # ok:true. Exit nonzero immediately -- a terminal error, not
                    # the poll's own attempts-then-park loop, so it does not
                    # borrow that counter. The launchd job (StandardOutPath/
                    # ErrorPath) must treat nonzero as a failed run.
                    print(json.dumps({"ok": False, **counts}))
                    print(
                        f"reconcile terminal error: {counts['reconcile']['error']}",
                        file=sys.stderr,
                    )
                    return 2  # terminal/instrument error — nonzero fail (launchd signal, not the producer-tool convention)
            print(json.dumps({"ok": True, **counts}))
        except TerminalError as exc:
            attempts += 1
            if attempts >= MAX_ATTEMPTS:
                _alert(root, f"bridge PARKED after {attempts} terminal errors: {exc}")
                _append_ledger(root, {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "role": "external", "event": "instrument_error",
                    "actor": "github-bridge",
                    "detail": {"reason": str(exc), "attempts": attempts, "terminal": True},
                })
                print(f"parked after {attempts} terminal errors: {exc}", file=sys.stderr)
                return 2  # terminal/instrument error — nonzero fail (launchd signal, not the producer-tool convention)
            print(f"terminal error {attempts}/{MAX_ATTEMPTS}: {exc}", file=sys.stderr)
        except Exception as exc:  # transient: log it, never treat as "no work"
            _append_ledger(root, {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "role": "external", "event": "instrument_error",
                "actor": "github-bridge", "detail": {"reason": str(exc)},
            })
            print(f"poll failed (instrument-error): {exc}", file=sys.stderr)

        if args.once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
