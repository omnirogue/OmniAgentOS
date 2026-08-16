#!/usr/bin/env python3
"""READ-ONLY base-red false-red detector. See devtasks/spend-breaker-fastfollow
(proposal sha256:4f9469143d65a80a1c4d7f08aa0c3df1b8ca06d0dd2a43e634da63d2c75e00cd).

WHY THIS FILE EXISTS: when main goes base-red on a required check and the fix
lands, an open PR's merge-ref check status was computed against pre-fix main
and nothing re-triggers it. Measured over the CI workflow's full life
(2026-08-06 -> 2026-08-13): 9 base-red episodes, 4 with a post-fix false-red
tail (0.19h, 0.43h, 7.49h, 8.77h; median 7.49h). The 2026-08-12 episode
stranded PRs #336-#339 on a `type` failure computed against a base that
predated fix 003b8378a, for up to 8.77h, none of which touched the indicted
file. CI status is an INPUT TO ROUTING, so a false red misroutes implementer
attention at code that is not broken and hides PRs that are genuinely
defective.

The public entry point is `detect_base_red(repo)`. It is READ-ONLY by
construction: `_gh` never passes a `gh` verb that mutates GitHub state (no
`pr merge`, no `pr close`, no `workflow run`, no `api -X POST/PUT/DELETE`) --
only `gh api ... -X GET` (the default) and `gh pr` read subcommands.

`test` is EXCLUDED by name, not by omission from REQUIRED alone: ci.yml's
`test` job carries `continue-on-error: true` (.github/workflows/ci.yml
~line 159) precisely because `make test-pr` fails today on main for
pre-existing reasons unrelated to any one PR (docs/ci/README.md), so its
'fail' rendering says nothing about a PR's correctness and reporting it would
manufacture false alarms this detector exists to eliminate.

FAIL-CLOSED ON API ERROR (the favourable-absence class this estate keeps
re-discovering, see advice_writer.py:186-200 and github_bridge.py's docstring
"API failure is instrument_error, never silence and never a defect"): a `gh`
error, timeout, or unparseable body never renders as an empty
`false_red_prs` list -- that is indistinguishable from "checked, found
nothing wrong" to a consumer that only tests truthiness. On error,
`false_red_prs` is explicitly `None` and `error` carries the message.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from typing import Any

# Required checks, from docs/ci/README.md's own frozen list ("green as of
# this change: lint, type, dashboard"). A module-level frozenset, never a
# live list read off GitHub branch protection -- branch protection could be
# widened by someone else's change without this detector's author reviewing
# it, silently changing what this module reports on.
REQUIRED: frozenset[str] = frozenset({"lint", "type", "dashboard"})

# `test` (.github/workflows/ci.yml ~line 159) runs with continue-on-error:
# true and is NOT in docs/ci/README.md's required list. Named explicitly
# here (rather than relying on it simply not appearing in REQUIRED) so a
# future widening of REQUIRED that accidentally includes "test" is still
# caught by this exclusion -- belt and braces, per the plan's own step 2.
EXCLUDED_CHECKS: frozenset[str] = frozenset({"test"})

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _iso_now() -> str:
    return datetime.now(UTC).strftime(TS_FMT)


class GhApiError(RuntimeError):
    """A `gh` call failed, timed out, or returned unparseable output.

    Never swallowed into an empty result -- see module docstring."""


def _gh(args: list[str]) -> Any:
    """One read-only `gh` call. Raises GhApiError on any failure; never
    returns a silent empty value that could be mistaken for "nothing found".

    READ-ONLY CONTRACT: this is the only place in this module that shells
    out to `gh`. It must never be called with a mutating verb -- callers in
    this module only ever pass `api ... -X GET` (or omit -X, GET is the gh
    default) and `pr list` / `pr view`. A grep for "mutating gh subcommand"
    over this file should find nothing.
    """
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhApiError(f"gh invocation failed: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        raise GhApiError(err or f"gh exited {proc.returncode} with no stderr")
    out = (proc.stdout or "").strip()
    if not out:
        # An empty body from a failed/misbehaving call must never be read as
        # "the query legitimately returned nothing" -- same class as the
        # detector's own error handling one level up.
        raise GhApiError("gh returned empty output")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise GhApiError(f"unparseable gh output: {exc}") from exc


def _required_check_fix_shas(repo: str) -> dict[str, str]:
    """For each REQUIRED check, the sha of the most recent commit on main at
    which that check went failure -> success (the "fix" commit). Checks with
    no observed failure->success transition in the queried window are
    simply absent from the returned dict (nothing to compare a PR's base
    against for that check) -- that is a legitimate empty result, not an
    error, so it is NOT wrapped in a way that looks like the error case.
    """
    runs = _gh([
        "api", f"repos/{repo}/actions/runs",
        "-X", "GET",
        "-f", "branch=main",
        "-f", "event=push",
        "--paginate",
        "--jq", ".workflow_runs",
    ])
    if not isinstance(runs, list):
        raise GhApiError(f"unexpected runs shape from gh api: {type(runs).__name__}")

    # gh returns newest-first; walk oldest-first so we can detect the
    # failure->success transition for each check in chronological order.
    ordered = list(reversed(runs))
    fixes: dict[str, str] = {}
    last_conclusion: dict[str, str] = {}
    for run in ordered:
        name = run.get("name")
        conclusion = run.get("conclusion")
        sha = run.get("head_sha")
        if not name or not sha or conclusion is None:
            continue
        if name not in REQUIRED or name in EXCLUDED_CHECKS:
            continue
        prev = last_conclusion.get(name)
        if prev == "failure" and conclusion == "success":
            fixes[name] = sha  # keep updating: last one wins == most recent
        last_conclusion[name] = conclusion
    return fixes


def _open_pr_numbers(repo: str) -> set[int]:
    """PR numbers currently OPEN on GitHub. Cross-referenced against workflow
    runs below so a run belonging to a now-closed/merged PR is never
    reported -- this module only ever judges PRs that are still open."""
    prs = _gh([
        "pr", "list", "--repo", repo, "--state", "open",
        "--json", "number", "--limit", "200",
    ])
    if not isinstance(prs, list):
        raise GhApiError(f"unexpected pr list shape from gh: {type(prs).__name__}")
    return {p["number"] for p in prs if isinstance(p, dict) and "number" in p}


def _open_pr_failures(repo: str) -> list[dict]:
    """One entry per (open PR, failing check) among REQUIRED checks, each
    carrying the PR number, the check name, and the base sha the FAILING
    RUN was computed against (never the live/current base -- that is what
    makes false-red detection possible at all, per the plan's evidence that
    `compare` reads behind_by=0 NOW even though the run's own base was
    behind at the time it executed).
    """
    open_numbers = _open_pr_numbers(repo)

    runs = _gh([
        "api", f"repos/{repo}/actions/runs",
        "-X", "GET",
        "-f", "event=pull_request",
        "-f", "status=completed",
        "--paginate",
        "--jq", ".workflow_runs",
    ])
    if not isinstance(runs, list):
        raise GhApiError(f"unexpected runs shape from gh api: {type(runs).__name__}")

    # Keep only the most recent completed run per (pr number, check name) --
    # an older stale-base run must never shadow a newer one for the same PR.
    latest: dict[tuple[int, str], dict] = {}
    for run in runs:
        name = run.get("name")
        if not name or name in EXCLUDED_CHECKS or name not in REQUIRED:
            continue
        conclusion = run.get("conclusion")
        created_at = run.get("created_at") or ""
        for pr_ref in run.get("pull_requests") or []:
            number = pr_ref.get("number")
            base_sha = (pr_ref.get("base") or {}).get("sha")
            if number is None or number not in open_numbers or not base_sha:
                continue
            key = (number, name)
            existing = latest.get(key)
            if existing is None or created_at > existing["created_at"]:
                latest[key] = {
                    "pr": number, "check": name, "base_sha": base_sha,
                    "conclusion": conclusion, "created_at": created_at,
                }

    return [
        {"pr": v["pr"], "check": v["check"], "base_sha": v["base_sha"]}
        for v in latest.values() if v["conclusion"] == "failure"
    ]


def _base_contains_fix(repo: str, base_sha: str, fix_sha: str) -> bool:
    """True if `base_sha`'s history already contains `fix_sha` -- i.e. the
    check run's recorded base was computed AFTER the fix landed on main, so
    a failure on it is real, not a stale-base false red.
    """
    cmp = _gh(["api", f"repos/{repo}/compare/{fix_sha}...{base_sha}", "-X", "GET"])
    if not isinstance(cmp, dict):
        raise GhApiError(f"unexpected compare shape from gh api: {type(cmp).__name__}")
    status = cmp.get("status")
    if status not in ("identical", "ahead", "behind", "diverged"):
        raise GhApiError(f"unexpected compare status: {status!r}")
    # base_sha...fix_sha direction: "identical"/"ahead" means fix_sha is an
    # ancestor of (or equal to) base_sha, i.e. base already contains the fix.
    return status in ("identical", "ahead")


def detect_base_red(repo: str) -> dict:
    """READ-ONLY. Returns a report dict:

        {"generated_at": <iso>, "false_red_prs": [...] | None,
         "error": <str> | None}

    On ANY api error, `false_red_prs` is explicitly None and `error` is a
    non-empty string -- never an empty list, which a consumer would read as
    "checked, nothing wrong" (favourable absence). This function never calls
    a mutating `gh` subcommand -- see `_gh`'s READ-ONLY CONTRACT.
    """
    generated_at = _iso_now()
    try:
        fixes = _required_check_fix_shas(repo)
        failures = _open_pr_failures(repo)
    except GhApiError as exc:
        return {"generated_at": generated_at, "false_red_prs": None, "error": str(exc)}

    false_red: list[dict] = []
    partial_errors: list[str] = []
    for f in failures:
        check = f["check"]
        if check in EXCLUDED_CHECKS or check not in REQUIRED:
            continue  # belt-and-braces; _open_pr_failures already filters this
        fix_sha = fixes.get(check)
        base_sha = f.get("base_sha")
        if not fix_sha or not base_sha:
            continue  # no known fix to compare against, or no recorded base
        try:
            contains_fix = _base_contains_fix(repo, base_sha, fix_sha)
        except GhApiError as exc:
            partial_errors.append(f"pr #{f['pr']} ({check}): {exc}")
            continue
        if not contains_fix:
            false_red.append({
                "pr": f["pr"],
                "check": check,
                "base_sha": base_sha,
                "fix_sha": fix_sha,
            })

    result: dict = {"generated_at": generated_at, "false_red_prs": false_red, "error": None}
    if partial_errors:
        result["partial_errors"] = partial_errors
    return result
