#!/usr/bin/env python3
"""close-on-land: landed content closes its own PR, and retires its own plan.

Two sweeps, same principle and the same refusals — landed work must not stay
open. The default subject is a PR; ``retire-proposals`` is the queue-side one:

    # PR sweep (the original, unchanged)
    close_on_land.py --repo Globex/OmniAgentOS --git-dir ~/OmniAgentOS

    # one-shot backfill: proposals whose candidate already merged
    close_on_land.py retire-proposals --loops-root ~/OmniAgentOS/var/loopqueue
    close_on_land.py retire-proposals --loops-root ... --apply

See ``retire_proposals`` for what the second one refuses. The rest of this
docstring is the PR sweep.

-------------------------------------------------------------------------------

Run this after a train lands on ``main``. It asks, for every open PR, whether
that PR's work is now on main BY CONTENT -- not by branch name and not by SHA,
because PRs here land by cherry-pick, by rebase, and by being re-authored
inside a train branch, and all three break SHA comparison. Then it closes the
ones that are genuinely redundant, with a comment naming the commit that
superseded them.

    # what it WOULD do -- the default, and the only mode that needs no thought
    close_on_land.py --repo Globex/OmniAgentOS --git-dir ~/OmniAgentOS

    # after a train lands, restricting the attestation scan to the new range
    close_on_land.py --repo ... --git-dir ... --since-ref <previous-main-sha>

    # actually close them
    close_on_land.py --repo ... --git-dir ... --apply

Dry-run is the default and ``--apply`` is the only thing that changes it. There
is no config file, no environment variable, and no "auto" mode that can turn
closing on behind someone's back.

------------------------------------------------------------------ what it refuses

  * a PARTIAL landing -- some of the PR's files on main, some not. Reported in
    full, never closed. This is the sharp case: PR #42 has 5 of 9 files on main
    and one commit still missing, and a boolean "landed?" closes it and loses
    that commit.
  * an EMPTY diff. Zero matches is suspicious, not agreement.
  * a landing nobody can NAME -- no patch-id match, no attestation, no carrier
    commit. If the comment cannot say which commit superseded the PR, the
    evidence is not good enough to act on.
  * a whole RUN in which most PRs come back closable at once. That shape is a
    broken comparison, not a good day; see ``implausible_close_rate``.
  * anything at all, if the detector's own canaries do not pass first.

Exit codes follow the estate's gate convention (Ruling #4, 2026-08-09:
exit 2 = COULD NOT RUN, distinct from a candidate defect and from do-not-retry):
  0  clean -- nothing needs a human
  1  something needs a human (partial, empty, or per-PR error)
  2  COULD NOT RUN -- instrument fault, refused run, or terminal gh error; the
     detector could not complete. Do not re-run the same input until the cause
     clears (fix the mechanics / wait out the terminal error), then it re-runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import claim as _claim  # noqa: E402 -- shared alert_once
from land_detect import (  # noqa: E402
    InstrumentError,
    LandDetector,
    Verdict,
    implausible_close_rate,
)

TERMINAL_MARKERS = (
    "authentication", "bad credentials", "401", "403",
    "suspended", "rate limit", "quota",
)


class TerminalGhError(RuntimeError):
    """Auth/quota/suspension. Park, alert once, never retry in a loop."""


def _alert_could_not_run(loops_root: Path | None, key: str, msg: str) -> None:
    """Persist one unattended could-not-run alert without changing the exit."""
    if loops_root is None:
        return
    try:
        _claim.alert_once(loops_root, key, msg, source="close-on-land")
    except Exception:  # noqa: BLE001 -- alerting is best-effort, never fatal
        pass


def _gh(args: list[str]) -> object:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if any(m in err.lower() for m in TERMINAL_MARKERS):
            raise TerminalGhError(err)
        raise RuntimeError(err or f"gh exited {proc.returncode}")
    out = (proc.stdout or "").strip()
    if not out:
        # An empty list query is a real answer; a failed call is not. The
        # returncode above already separated them, so this is safe.
        return []
    return json.loads(out)


def _comment_body(v: Verdict) -> str:
    """The close comment. It must name the landing sha and show its working.

    Whoever opened this PR did not close it and did not merge it. The comment
    is the only thing standing between them and the impression that their work
    was thrown away, so it names the commit, lists the evidence per file, and
    says plainly how to undo this.
    """
    sha = v.landing_shas[0]
    how = {
        "patch-id": (
            "every commit on this branch has a patch-id equivalent already on `main` "
            "(detected with `git cherry` / `git patch-id`, so a cherry-pick or rebase "
            "is matched even though the SHAs differ)"
        ),
        "attestation": (
            "a commit on `main` names this branch's head SHA (or carries a "
            "`Closes #` trailer for it), and every line this PR adds is present on "
            "`main`, in order"
        ),
        "carrier": (
            "every file this PR touches is byte-identical on `main`, or its diff "
            "reverse-applies cleanly onto `main`"
        ),
    }[v.landing_kind]

    lines = [
        f"Closing: this landed on `main` as {sha}.",
        "",
        f"Detected by content equivalence, not by branch name — {how}.",
        "",
        f"Head `{v.head[:12]}` → `main`, file by file:",
        "",
    ]
    for f in v.files:
        lines.append(f"- `{f.path}` — {f.rung}: {f.detail}")
    if len(v.landing_shas) > 1:
        lines += ["", "Landing commits: " + ", ".join(s[:12] for s in v.landing_shas)]
    lines += [
        "",
        (
            "Nothing was deleted — the branch still exists and this PR can be reopened. "
            "**If any part of this is wrong, reopen it**; a partial landing is supposed "
            "to be left open, so a close here is a claim that *all* of the work is on "
            "`main`."
        ),
        "",
        "— closed automatically by `close-on-land`.",
    ]
    return "\n".join(lines)


def _render(verdicts: list[Verdict], *, apply: bool, refusal: str | None) -> str:
    order = {"landed": 0, "partial": 1, "empty": 2, "error": 3, "unlanded": 4}
    rows = sorted(verdicts, key=lambda v: (order.get(v.status, 9), v.number or 0))
    mode = "APPLY" if apply else "DRY RUN (nothing was closed)"
    out = ["", f"close-on-land — {mode}", ""]
    out.append("{:<7} {:<9} {:<9} {:<9} {:<13} {}".format(
        "PR", "STATUS", "CLOSABLE", "FILES", "VIA", "LANDING SHA"))
    out.append("-" * 78)
    for v in rows:
        out.append("{:<7} {:<9} {:<9} {:<9} {:<13} {}".format(
            f"#{v.number}" if v.number else v.head[:7],
            v.status,
            "yes" if v.closable else "no",
            f"{v.contained_count}/{len(v.files)}",
            v.landing_kind or "-",
            v.landing_shas[0][:12] if v.landing_shas else "-",
        ))
    needs_human = [v for v in rows if v.status in ("partial", "empty", "error")]
    if needs_human:
        out += ["", "NEEDS A HUMAN — reported, never closed:", ""]
        for v in needs_human:
            out.append(f"  #{v.number} [{v.status}] {v.reason}")
            for f in v.missing:
                out.append(f"      missing on main: {f.path} ({f.rung})")
    closable = [v for v in rows if v.closable]
    if closable:
        out += ["", ("CLOSED:" if apply else "WOULD CLOSE:"), ""]
        for v in closable:
            out.append(f"  #{v.number} -> {v.landing_shas[0][:12]} ({v.landing_kind})")
    if refusal:
        out += ["", refusal]
    out.append("")
    return "\n".join(out)


def run(
    repo: str,
    git_dir: str,
    *,
    main_ref: str = "main",
    numbers: list[int] | None = None,
    apply: bool = False,
    max_close: int = 5,
    max_close_fraction: float = 0.5,
    range_spec: str | None = None,
    limit: int = 100,
) -> tuple[int, dict]:
    detector = LandDetector(git_dir, main_ref, range_spec=range_spec)
    # Nothing below this line may run if the instrument cannot prove it is
    # still able to say NO.
    detector.self_test()

    prs = _gh([
        "pr", "list", "--repo", repo, "--state", "open",
        "--json", "number,title,url,headRefName,headRefOid",
        "--limit", str(limit),
    ])
    assert isinstance(prs, list)
    if numbers:
        wanted = set(numbers)
        prs = [p for p in prs if p["number"] in wanted]
        missing = wanted - {p["number"] for p in prs}
        for num in sorted(missing):
            pr = _gh(["pr", "view", str(num), "--repo", repo,
                      "--json", "number,title,url,headRefName,headRefOid"])
            if pr:
                prs.append(pr)

    verdicts: list[Verdict] = []
    for pr in sorted(prs, key=lambda p: p["number"]):
        try:
            v = detector.classify(
                pr["headRefOid"], number=pr["number"], branch=pr.get("headRefName", "")
            )
        except InstrumentError:
            raise
        except Exception as exc:  # noqa: BLE001 - see below
            # Deliberately broad. One PR that cannot be classified must not
            # abort the sweep, and it must not silently vanish either: it
            # becomes an `error` verdict, which is never closable and which
            # pushes the run's exit code to 1.
            v = Verdict(head=pr.get("headRefOid", ""), number=pr["number"],
                        branch=pr.get("headRefName", ""), status="error",
                        reason=f"classification failed: {exc}")
        verdicts.append(v)

    refusal = implausible_close_rate(verdicts, max_fraction=max_close_fraction)
    closable = [v for v in verdicts if v.closable]
    if not refusal and len(closable) > max_close:
        refusal = (
            f"REFUSING THE RUN: {len(closable)} closable PRs exceeds --max-close "
            f"{max_close}. Nothing was closed."
        )

    closed: list[int] = []
    if apply and not refusal:
        for v in closable:
            body = _comment_body(v)
            try:
                subprocess.run(
                    ["gh", "pr", "close", str(v.number), "--repo", repo, "--comment", body],
                    capture_output=True, text=True, timeout=180, check=True,
                )
                closed.append(v.number)
            except subprocess.CalledProcessError as exc:
                v.reason += f" | CLOSE FAILED: {(exc.stderr or '').strip()[:200]}"

    report = {
        "repo": repo,
        "main": detector.main_sha,
        "applied": bool(apply),
        "refused": refusal,
        "closed": closed,
        "would_close": [v.number for v in closable],
        "counts": {
            s: sum(1 for v in verdicts if v.status == s)
            for s in ("landed", "partial", "unlanded", "empty", "error")
        },
        "verdicts": [v.as_dict() for v in verdicts],
    }
    print(_render(verdicts, apply=apply, refusal=refusal))

    if refusal:
        return 2, report
    if any(v.status in ("partial", "empty", "error") for v in verdicts):
        return 1, report
    return 0, report


# ==================================================== retire-proposals (queue side)
#
# The same idea one stage earlier. A candidate that merges is the PLAN's
# delivery, but nothing retired the plan: the proposal stayed selectable, so
# planners re-selected it, builders rebuilt it (measured: one id four times),
# and reviewers re-refused it. 56 of 143 recent rejections were re-refusals of
# already-shipped work — the largest single rejection bucket.
#
# The land-time half of the cure lives in `gate_loop`. This is the BACKFILL for
# everything that landed before it existed, and for any train whose bookkeeping
# will never be replayed.

#: A backfill that wants to retire the world is a broken comparison, not a good
#: day — the same argument as `implausible_close_rate` on the PR side, sized for
#: a one-shot sweep of real history. Raise it deliberately, having read the
#: dry-run's list.
DEFAULT_MAX_RETIRE = 200

#: The one place a proposal id may come from is a landed candidate's OWN
#: `payload.resolves`, bounded exactly as the gate loop bounds it.
_MAX_RESOLVES = 32

#: Same event, different hand. The land-time half writes as the daemon; a
#: backfill line that claimed to be the daemon would make a one-shot sweep of
#: old history indistinguishable from a landing that happened at that instant.
RETIRE_ACTOR = "close-on-land-retire-proposals"


def _snapshot_of(pairs: list[dict]) -> dict:
    """The reviewable PLAN: the exact pairs, and a digest that pins them.

    `--apply` writes what a human read in the dry run and nothing else, so the
    dry run has to hand over something apply can be held to. The digest is over
    the sorted `(proposal, carrier, resolved_by)` triples, so a pair that
    appears, disappears, or changes its carrier between the two runs changes it.
    """
    triples = sorted((p["proposal"], p["carrier"], p["resolved_by"]) for p in pairs)
    blob = json.dumps(triples, separators=(",", ":"), ensure_ascii=False)
    return {"kind": "retire-proposals-snapshot",
            "digest": "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest(),
            "pairs": [{"proposal": a, "carrier": b, "resolved_by": c}
                      for a, b, c in triples]}


#: The queue-side names this module borrows, resolved on first use. Kept as
#: module attributes (PEP 562) so tests and reviewers can reach and patch them
#: — `close_on_land.LedgerView` is a real attribute — without the PR sweep
#: paying for the daemon's import graph at module import.
_LAZY = ("proposal_retirement_event", "retire_proposal_once", "envelope_id_is_bound",
         "envelope_identity_problem", "LedgerView", "append_event")


def _import_queue_bridge():
    """The queue modules, imported late and by package path.

    Late because the PR sweep must not pay for the daemon's import graph, and
    by package path because `gate_loop` imports its siblings as `bridge.*`.
    The resolved names are cached into this module's globals so a later
    `close_on_land.LedgerView` (or a monkeypatch of one) sees the same object.
    """
    pkg_root = str(Path(__file__).resolve().parents[1])
    if pkg_root not in sys.path:
        sys.path.insert(0, pkg_root)
    from bridge.gate_loop import (  # noqa: PLC0415
        envelope_id_is_bound,
        envelope_identity_problem,
        proposal_retirement_event,
        retire_proposal_once,
    )
    from bridge.integration import LedgerView  # noqa: PLC0415
    from bridge.ledger_write import append_event  # noqa: PLC0415
    resolved = {
        "proposal_retirement_event": proposal_retirement_event,
        "retire_proposal_once": retire_proposal_once,
        "envelope_id_is_bound": envelope_id_is_bound,
        "envelope_identity_problem": envelope_identity_problem,
        "LedgerView": LedgerView,
        "append_event": append_event,
    }
    globals().update(resolved)
    return resolved


def __getattr__(name: str):
    """Resolve the lazily-imported queue names on first attribute access."""
    if name in _LAZY:
        return _import_queue_bridge()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _id_from_filename(name: str) -> str | None:
    """The artifact id a candidate FILENAME pins, or None if it pins nothing.

    `sha256_<64 hex>.json` is the only shape the pipeline writes, and it is the
    only independent handle this sweep has on which artifact a file claims to
    be — it globs the directory, so the body's own `id` field is self-asserted
    and cannot pin anything by itself.
    """
    m = re.fullmatch(r"sha256_([0-9a-f]{64})\.json", name)
    return f"sha256:{m.group(1)}" if m else None


def _resolved_ids(payload: object) -> tuple[list[str], str]:
    """`(well-formed proposal ids, state)` from a candidate payload.

    Three outcomes, never folded together — a corrupt artifact that READS as a
    routine one is the favourable-absence failure this queue keeps paying for:

      * ``"ok"``            — at least one well-formed proposal id.
      * ``"no-link"``       — a healthy payload that simply never stamped
                              ``resolves``. A gap in the producer, and about
                              half of live candidates have it.
      * ``"corrupt"``       — there is no payload object at all, or ``resolves``
                              is present but nothing in it parses as an id.
                              That is EVIDENCE about the artifact, not a
                              statement that the candidate answered no plan.
    """
    if not isinstance(payload, dict):
        return [], "corrupt"
    if "resolves" not in payload or payload.get("resolves") is None:
        return [], "no-link"
    raw = payload["resolves"]
    refs = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for ref in refs[:_MAX_RESOLVES]:
        if isinstance(ref, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", ref.strip()):
            ident = ref.strip()
            if ident not in out:
                out.append(ident)
    return (out, "ok") if out else ([], "corrupt")


def _render_retirements(report: dict) -> str:
    mode = "APPLY" if report["applied"] else "DRY RUN (nothing was written)"
    out = ["", f"close-on-land retire-proposals — {mode}", ""]
    out.append("{:<70} {:<14} {}".format("PROPOSAL", "CARRIER", "RESOLVED BY"))
    out.append("-" * 100)
    for p in report["pairs"]:
        out.append("{:<70} {:<14} {}".format(
            p["proposal"], p["carrier"][:12], p["resolved_by"]))
    if not report["pairs"]:
        out.append("  (none)")
    if report["contradictions"]:
        out += ["", "NEEDS A HUMAN — landed, but the ledger says refused. Reported, "
                    "never overwritten (a second terminal event cannot be taken back "
                    "on an append-only ledger):", ""]
        for c in report["contradictions"]:
            out.append(f"  {c['proposal']} reads {c['event']!r} but "
                       f"{c['resolved_by']} landed at {c['carrier'][:12]}")
    if report["unnameable"]:
        out += ["", "NEEDS A HUMAN — a landing nobody can NAME (a `merged` event "
                    "with no full 40-hex carrier); nothing retired against it:", ""]
        for cid in report["unnameable"]:
            out.append(f"  {cid}")
    if report["id_unbound"]:
        out += ["", "REFUSED — the envelope does not hash to its own id "
                    "(content_id(payload) != id): edited in place after filing, or "
                    "forged. Its `resolves` claim cannot be trusted to terminalize "
                    "anyone's plan. Remedy: re-file the envelope. Nothing retired:", ""]
        for item in report["id_unbound"]:
            out.append(f"  {item}")
    out += ["", "  {} already retired · {} landed candidates named no proposal · "
                "{} unreadable/corrupt candidate artifact(s) · {} id-unbound".format(
                    len(report["already_retired"]), len(report["no_link"]),
                    len(report["unreadable"]), len(report["id_unbound"]))]
    if report.get("drift"):
        out += ["", "PLAN DRIFT since the reviewed dry run:", ""]
        for label in ("appeared", "vanished"):
            for pid in report["drift"][label]:
                out.append(f"  {label}: {pid}")
    if report["refused"]:
        out += ["", report["refused"]]
    elif report["applied"]:
        out += ["", f"RETIRED: {len(report['retired'])}"]
    out.append("")
    return "\n".join(out)


def retire_proposals(loops_root: Path, *, apply: bool = False,
                     max_retire: int = DEFAULT_MAX_RETIRE,
                     render: bool = True,
                     snapshot: dict | None = None) -> tuple[int, dict]:
    """Retire proposals whose candidate already merged. Dry-run by default.

    It replays `candidates/*.json` against the ledger and pairs each MERGED
    candidate with the proposals its own `payload.resolves` names, then writes
    one terminal `completed` per pair — the identical event the land-time half
    writes, minted by the same `gate_loop.proposal_retirement_event`, through
    the one sanctioned transport (`bridge/ledger_write.append_event`). There is
    no second writer and no second shape.

    ------------------------------------------------------------ what it refuses

      * a landing nobody can NAME — a `merged` event whose `detail.merge_sha`
        is missing or not a full 40-hex sha. Reported, never retired: a
        retirement whose carrier cannot be checked hides shipped work behind a
        commit no one can look at.
      * a proposal that ALREADY reached a terminal event of its own. If that
        event is `merged`/`completed` it is simply already retired. If it is
        `rejected`, that is THE KNOWN CASE — shipped work reading as refused
        (measured on the proposals resolved by candidate sha256:61b08c0e, which
        merged as 9d7bb022). It is reported with its carrier named and left to
        a human: `exactly_one_terminal_event` is a ledger invariant, the log is
        append-only, and stamping a second terminal to paper over the first
        would make the history less legible, not more.
      * a candidate that never landed. A gate pass, a park, a conflict or a
        rejection says nothing about `main`; only a `merged` event does.
      * AN ENVELOPE THAT DOES NOT PROVE ITS OWN ID. The id is
        `content_id(payload)` by construction (CONTRACT §7), and this sweep
        globs `candidates/` — so without that check a file dropped in under ANY
        name, claiming an already-merged candidate's id and naming any
        `resolves` it likes, terminalizes an arbitrary live proposal. Both the
        FILENAME and the payload hash must agree with the id, and neither is
        negotiable: the thing being bought with that trust is an unrepairable
        terminal event on somebody else's plan. Measured cost of failing closed
        here (live queue, 2026-08-12): 4 of 16 otherwise-retirable pairs are
        held back because their envelope was edited after filing. They are
        named, with the remedy, and they keep being offered — which is exactly
        today's behaviour and the safe direction.
      * a landed candidate whose `resolves` is absent or malformed. Counted and
        reported, never guessed at — inventing the link is worse than the bloat,
        and a CORRUPT artifact is counted separately from a healthy one that
        simply never stamped the link.
      * a whole RUN that wants to retire more than `--max-retire` at once.
      * AN APPLY THAT NOBODY REVIEWED, or one whose plan has since MOVED.
        `--apply` requires the `snapshot` a dry run produced, and writes exactly
        the pairs that snapshot names. Without it there is nothing stopping the
        sweep retiring a pair that arrived after the run a human actually read;
        with it, any drift between the reviewed plan and the current scan
        refuses the whole run rather than silently applying the newer one.

    Exit codes follow the same convention as the PR sweep: 0 clean · 1 something
    needs a human (contradictions, unnameable carriers, unbound envelopes, or
    UNREADABLE artifacts — an unknown population is not a clean no-op) · 2 could
    not run / do not re-run this input unchanged (refused run, plan drift).
    """
    bridge = _import_queue_bridge()
    retire_once = bridge["retire_proposal_once"]
    identity_problem = bridge["envelope_identity_problem"]
    ledger_view = bridge["LedgerView"]
    report: dict = {
        "loops_root": str(loops_root), "applied": bool(apply), "refused": None,
        "pairs": [], "retired": [], "already_retired": [], "contradictions": [],
        "unnameable": [], "no_link": [], "unreadable": [], "id_unbound": [],
        "write_failures": [], "raced": [],
    }
    try:
        view = ledger_view.build(loops_root)
    except OSError as exc:
        raise InstrumentError(f"ledger unreadable at {loops_root}: {exc}") from exc

    cdir = loops_root / "candidates"
    try:
        entries = sorted(cdir.glob("*.json")) if cdir.is_dir() else []
    except OSError as exc:
        raise InstrumentError(f"candidates/ could not be listed: {exc}") from exc

    seen: set[str] = set()
    for path in entries:
        try:
            art = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            report["unreadable"].append(path.name)
            continue
        if not isinstance(art, dict) or art.get("kind") != "candidate":
            continue
        # THE ID MUST BE PROVED BEFORE IT IS USED AS A KEY, and the proof is the
        # SAME one the daemon applies (`envelope_identity_problem`) — one
        # definition, so the two paths cannot drift into enforcing different
        # halves of it, which is exactly what happened once already. Here the
        # trusted key is the FILENAME: this sweep globs the directory, so
        # without it a second file impersonates a candidate that already
        # merged. Checked before `view.terminal.get(...)` — an unproved id must
        # never even be used as a lookup.
        expected = _id_from_filename(path.name)
        if expected is None:
            report["id_unbound"].append(
                f"{path.name} (filename is not sha256_<id>.json, so nothing "
                f"independent pins the id this file claims)")
            continue
        problem = identity_problem(art, expected)
        if problem is not None:
            report["id_unbound"].append(f"{expected} ({problem})")
            continue
        cid = expected
        terminal = view.terminal.get(cid)
        if not isinstance(terminal, dict) or terminal.get("event") != "merged":
            continue                       # never landed: proves nothing about main
        detail = terminal.get("detail")
        carrier = detail.get("merge_sha") if isinstance(detail, dict) else None
        if not (isinstance(carrier, str)
                and re.fullmatch(r"[0-9a-fA-F]{40}", carrier.strip())):
            report["unnameable"].append(cid)
            continue
        carrier = carrier.strip()
        proposals, state = _resolved_ids(art.get("payload"))
        if state == "corrupt":
            # Evidence about the ARTIFACT, never folded into `no_link`: a
            # corrupt envelope that reads as "this candidate answered no plan"
            # is the favourable-absence class this queue keeps paying for.
            report["unreadable"].append(
                f"{path.name} (corrupt payload / unreadable `resolves`)")
            continue
        if state == "no-link":
            report["no_link"].append(cid)
            continue
        for pid in proposals:
            if pid in seen:
                continue
            seen.add(pid)
            prior = view.terminal.get(pid)
            if isinstance(prior, dict):
                if prior.get("event") in ("merged", "completed"):
                    report["already_retired"].append(pid)
                else:
                    report["contradictions"].append(
                        {"proposal": pid, "carrier": carrier, "resolved_by": cid,
                         "event": prior.get("event")})
                continue
            report["pairs"].append(
                {"proposal": pid, "carrier": carrier, "resolved_by": cid})

    report["snapshot"] = _snapshot_of(report["pairs"])

    if apply and snapshot is None:
        report["refused"] = (
            "REFUSING THE RUN: --apply must carry the snapshot of a dry run that "
            "someone actually read. Run it without --apply (with --emit-snapshot), "
            "read the pairs, then apply THAT plan. Nothing was written.")
    elif apply and snapshot.get("digest") != report["snapshot"]["digest"]:
        current = {(p["proposal"], p["carrier"]) for p in report["pairs"]}
        reviewed = {(p["proposal"], p["carrier"])
                    for p in (snapshot.get("pairs") or [])
                    if isinstance(p, dict)}
        report["drift"] = {
            "appeared": sorted(a for a, _ in current - reviewed),
            "vanished": sorted(a for a, _ in reviewed - current),
        }
        report["refused"] = (
            "REFUSING THE RUN: the queue moved between the dry run and this apply "
            f"({len(current - reviewed)} pair(s) appeared, "
            f"{len(reviewed - current)} vanished). The plan a human approved is no "
            "longer the plan this would execute. Nothing was written; re-run the "
            "dry run and read it again.")
    elif apply and len(report["pairs"]) > max_retire:
        report["refused"] = (
            f"REFUSING THE RUN: {len(report['pairs'])} proposals to retire exceeds "
            f"--max-retire {max_retire}. Nothing was written. Read the dry-run list "
            "and raise the bound deliberately if it is right.")
    elif apply:
        for pair in report["pairs"]:
            try:
                # The terminal re-check happens INSIDE the retirement lock,
                # immediately before the append, so the pre-read above can only
                # save work — it can never authorize a second terminal event.
                written = retire_once(
                    loops_root, pair["proposal"], resolved_by=pair["resolved_by"],
                    carrier_sha=pair["carrier"], receipt=None, train=None,
                    actor=RETIRE_ACTOR)
            except (ValueError, OSError) as exc:
                # One refused or failed append must not abandon the rest: every
                # other pair is independently evidenced.
                report["write_failures"].append(
                    {"proposal": pair["proposal"],
                     "error": f"{type(exc).__name__}: {exc}"})
                continue
            if written is None:
                # Someone else terminalized it between the scan and the lock.
                # That is the guard WORKING, not an error.
                report["raced"].append(pair["proposal"])
                continue
            report["retired"].append(pair["proposal"])

    if render:
        print(_render_retirements(report))
    if report["refused"]:
        return 2, report
    # UNKNOWN POPULATION IS NOT A CLEAN RUN. `id_unbound` and `unreadable` are
    # in this list on purpose: an envelope that cannot be read, or that does not
    # prove its own id, means this sweep does not know what it did not retire —
    # and a sweep that finds one and still exits 0 reads to every operator and
    # every cron wrapper exactly like a healthy no-op.
    if (report["contradictions"] or report["unnameable"] or report["id_unbound"]
            or report["unreadable"] or report["write_failures"]):
        return 1, report
    return 0, report


def _retire_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="close_on_land.py retire-proposals",
        description=retire_proposals.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loops-root", required=True, type=Path,
                    help="var/loopqueue root (the ledger and candidates/ live here)")
    ap.add_argument("--apply", action="store_true",
                    help="actually append the terminal events. Requires "
                         "--snapshot: apply writes the plan a dry run named and "
                         "nothing else. Without this nothing is written.")
    ap.add_argument("--emit-snapshot", type=Path, default=None,
                    help="dry-run use: write the reviewed plan here, then pass it "
                         "back with --snapshot to apply exactly that plan")
    ap.add_argument("--snapshot", dest="snapshot_path", type=Path, default=None,
                    help="the plan from a previous --emit-snapshot. Apply refuses "
                         "if the queue has drifted away from it.")
    ap.add_argument("--max-retire", type=int, default=DEFAULT_MAX_RETIRE)
    ap.add_argument("--json", dest="json_path", type=Path, default=None)
    ap.add_argument("--loops-alerts-root", type=Path, default=None,
                    help="var/loopqueue root used for one-shot unattended alerts")
    args = ap.parse_args(argv)
    snapshot = None
    if args.snapshot_path:
        try:
            snapshot = json.loads(args.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"unreadable snapshot {args.snapshot_path}: {exc}", file=sys.stderr)
            return 2
        if not isinstance(snapshot, dict) or not snapshot.get("digest"):
            print(f"{args.snapshot_path} is not a retire-proposals snapshot",
                  file=sys.stderr)
            return 2
    try:
        code, report = retire_proposals(args.loops_root, apply=args.apply,
                                        max_retire=args.max_retire,
                                        snapshot=snapshot)
    except InstrumentError as exc:
        print(f"INSTRUMENT FAULT — nothing was graded and nothing was retired:\n  {exc}",
              file=sys.stderr)
        _alert_could_not_run(
            args.loops_alerts_root, "close-on-land:retire-instrument-fault",
            f"close-on-land retire-proposals COULD NOT RUN -- {exc}")
        return 2
    for target, body in ((args.json_path, report),
                         (args.emit_snapshot, report.get("snapshot"))):
        if not target:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
        tmp.rename(target)
    return code


def main(argv: list[str] | None = None) -> int:
    # The backfill is an ADDED entry point. Routing on the first token keeps the
    # existing `--repo/--git-dir` surface byte-identical for every caller and
    # every cron line that already exists; a subparser would not.
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "retire-proposals":
        return _retire_main(argv[1:])
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--git-dir", required=True, help="local checkout to read main from")
    ap.add_argument("--main", default="main", dest="main_ref")
    ap.add_argument("--pr", type=int, action="append", dest="numbers",
                    help="restrict to these PR numbers (repeatable)")
    ap.add_argument("--since-ref", default=None,
                    help="post-train use: only commits in <ref>..main may supply an "
                         "attestation. Narrower and faster; note that a PR whose "
                         "landing was attested in an OLDER commit will then report "
                         "rather than close, which is the safe direction.")
    ap.add_argument("--apply", action="store_true",
                    help="actually close. Without this nothing outward-facing happens.")
    ap.add_argument("--max-close", type=int, default=5)
    ap.add_argument("--max-close-fraction", type=float, default=0.5)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--json", dest="json_path", type=Path, default=None)
    ap.add_argument("--loops-root", type=Path, default=None,
                    help="var/loopqueue root used for one-shot unattended failure alerts")
    args = ap.parse_args(argv)

    range_spec = f"{args.since_ref}..{args.main_ref}" if args.since_ref else None
    try:
        code, report = run(
            args.repo, args.git_dir, main_ref=args.main_ref, numbers=args.numbers,
            apply=args.apply, max_close=args.max_close,
            max_close_fraction=args.max_close_fraction, range_spec=range_spec,
            limit=args.limit,
        )
    except InstrumentError as exc:
        print(f"INSTRUMENT FAULT — nothing was graded and nothing was closed:\n  {exc}",
              file=sys.stderr)
        _alert_could_not_run(
            args.loops_root, "close-on-land:instrument-fault",
            f"close-on-land ({args.repo}) COULD NOT RUN -- instrument fault, nothing was "
            f"graded and nothing was closed: {exc}",
        )
        return 2
    except TerminalGhError as exc:
        print(f"terminal gh error (auth/quota) — could not run; do not re-run the "
              f"same input until it clears: {exc}",
              file=sys.stderr)
        _alert_could_not_run(
            args.loops_root, "close-on-land:gh-terminal-error",
            f"close-on-land ({args.repo}) COULD NOT RUN -- terminal gh error (logged-out, "
            f"revoked, or rate-limited), NOT a real zero-closable run: {exc}. Fix gh auth "
            f"and it resumes on the next tick.",
        )
        return 2
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.json_path.with_suffix(args.json_path.suffix + ".tmp")
        tmp.write_text(json.dumps(report, indent=2), encoding="utf-8")
        tmp.rename(args.json_path)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
