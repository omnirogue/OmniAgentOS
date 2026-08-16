#!/usr/bin/env python3
"""Validate and atomically file a FINDING. The third sibling of file_proposal.py.

WHY THIS EXISTS, measured rather than assumed: queue publication is
ledger-derived — `integration.rebuild_queue()` iterates `ledger.status`, which
is populated from a fixed event->status map (`integration.LedgerView.build`),
so an artifact reaches `state/queue.json` **if and only if** it has a ledger
event in that map. Proposals never go missing because `file_proposal.py`
writes the artifact and appends its `proposed` event as ONE operation, so a
proposal cannot exist without its event. Findings had no such writer: they were
hand-written, and the append was a separate, forgettable step. The result,
measured on a prior revision, was 63 non-terminal findings on disk that no Implementer
was ever offered — 28 with no ledger event at all, 35 whose only events
(`observed`, `published`, `finding`, ...) are outside the status map.

This file closes the SOURCE of that class. `bridge/reconcile_queue.py` drains
the backlog it already produced; the two are deliberately separate tools,
because one is a writer used by producers and the other is a one-off repair
that appends to an append-only history.

Checks, in order:
  1. kind is "finding"; payload carries `symptom` (schema-enforced — a finding
     without an observable symptom is a complaint, not a finding).
  2. id equals the canonical hash of payload (canonical.content_id).
  3. drop-at-source: refuse if the id already sits in findings/, or carries a
     live (unexpired) marker in rejected/ — the CONTRACT §4 re-raise trap — or
     parked/.
  4. atomic write (tmp in-dir -> fsync -> exclusive link -> fsync dir), then one
     `found` ledger line through the shared transport (bridge/ledger_write.py).
     If the append is CONFIRMED absent afterwards, the artifact is rolled back,
     so a filing is all-or-nothing. An UNCONFIRMED append (unreadable ledger)
     KEEPS the artifact: an orphan artifact is visible and repairable by
     reconcile_queue.py, while a phantom ledger line pointing at nothing is
     neither (R3-FI-01).

Exit codes match file_proposal.py: 0 written · 1 refused-fixable ·
2 could-not-run · 3 refused-do-not-retry (Ruling #4, 2026-08-09). The constants
are imported, not restated, so this tool tracks any future swap automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from file_proposal import (  # noqa: E402
    EXIT_COULD_NOT_RUN,
    EXIT_REFUSED_DO_NOT_RETRY,
    EXIT_REFUSED_FIXABLE,
    LEDGER_EVENT_ABSENT,
    LEDGER_EVENT_PRESENT,
    LedgerNotDurable,
    Report,
    _append_ledger_line,
    _expired,
    _ledger_event_state,
    _ledger_fail_message,
    _now,
    _rollback,
    _stem,
    atomic_write,
    check_schema,
)

#: The event this tool appends. It MUST stay inside
#: `integration.LedgerView.build`'s status map, or a filing is invisible to
#: every status reader while reporting success — the exact defect this tool
#: exists to close, wearing a different label.
FOUND_EVENT = "found"

#: Every directory that can hold a LIVE artifact of some kind, keyed by
#: directory name. `canonical.content_id` deliberately does NOT include
#: `kind` in its hash input (changing that would re-key every artifact
#: already in the queue), so an identical payload filed as a finding and as a
#: proposal hashes to the SAME id. A duplicate check scoped to `findings/`
#: alone cannot see that id already lives in `proposals/` (or vice versa),
#: and both artifacts persist with a hybrid identity once `publish_queue.py`
#: collapses their ledger events into one queue item. Refuse against every
#: kind directory, not just this tool's own.
_ARTIFACT_KIND_DIRS = ("findings", "proposals", "inquiries", "candidates")

#: The three things a presence check can honestly conclude about a marker.
#: There is deliberately no boolean here: a boolean forces "cannot tell" to
#: collapse into one of the two answers, and every historical version of this
#: check collapsed it into ABSENT — the favourable-absence defect class.
PRESENCE_ABSENT = "absent"
PRESENCE_PRESENT = "present"
PRESENCE_UNDETERMINED = "undetermined"


def marker_presence(path) -> str:
    """Grade whether an on-disk marker is present, WITHOUT ever guessing.

    Both predecessors of this function answered the wrong question:

    - `Path.exists()` FOLLOWS symlinks, so a dangling rejection tombstone
      reported False — absent — and the caller filed straight past a live ban.
    - `os.path.lexists()` fixed the symlink half but swallows `OSError`
      internally and also returns False for it, so a marker inside a directory
      this process cannot search (EACCES/EPERM) STILL reports absent. Verified
      by execution 2026-08-12: with the parent chmod 000, `os.path.lexists()`
      is False while `os.lstat()` raises PermissionError(13).

    Both failures share one shape: an instrument that cannot answer reports the
    answer that happens to be convenient. `lstat` is called directly so the
    three outcomes stay distinguishable, and only `FileNotFoundError` (plus
    `NotADirectoryError`, where a path component is not a directory so no such
    marker can exist) is allowed to mean ABSENT. Every other `OSError` means
    UNDETERMINED, and every caller must fail CLOSED on it — refuse, never
    proceed as if the marker were not there.
    """
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return PRESENCE_ABSENT
    except OSError:
        return PRESENCE_UNDETERMINED
    return PRESENCE_PRESENT


def check_finding_identity(art: dict, queue: Path, rep: Report) -> None:
    """id == sha256(JCS(payload)), and the id is not already filed or banned."""
    from canonical import content_id

    payload = art.get("payload")
    if not isinstance(payload, dict):
        rep.refuse(EXIT_REFUSED_FIXABLE, "id.payload",
                   "payload is missing or not an object")
        return

    expected = content_id(payload)
    if art.get("id") != expected:
        rep.refuse(
            EXIT_REFUSED_FIXABLE, "id.hash",
            f"id does not match the canonical hash of payload "
            f"(declared {str(art.get('id'))[:19]}…, computed {expected[:19]}…)",
            f"set id to {expected}. An id hand-copied from an earlier draft is "
            "how a corrected finding gets refused as a duplicate of the one it "
            "corrects.",
        )
        art["id"] = expected

    ident = art["id"]
    # `marker_presence`, not `Path.exists()` and not `os.path.lexists()`:
    # a dangling symlink and an unsearchable parent directory must BOTH read as
    # "cannot tell", never as absent, or a re-file walks straight past an
    # artifact that is already on the board. Checked across EVERY kind
    # directory (see `_ARTIFACT_KIND_DIRS`), not just `findings/`, so an id
    # already used by a different artifact kind is caught too.
    for dirname in _ARTIFACT_KIND_DIRS:
        presence = marker_presence(queue / dirname / f"{_stem(ident)}.json")
        if presence == PRESENCE_UNDETERMINED:
            rep.refuse(
                EXIT_COULD_NOT_RUN, "id.duplicate_undetermined",
                f"cannot determine whether {ident[:19]}… is already filed in "
                f"{dirname}/ — the path exists but cannot be stat'd",
                f"fix the permissions on {queue / dirname} first. A duplicate "
                "check that cannot read the board must refuse, not file: "
                "an unreadable directory is an instrument error, not an absence.",
            )
            break
        if presence == PRESENCE_PRESENT:
            rep.refuse(
                EXIT_REFUSED_DO_NOT_RETRY, "id.duplicate",
                f"{ident[:19]}… is already filed in {dirname}/",
                "the same payload is already on the board — re-filing it adds queue "
                "depth, not signal. An identical payload hashes to an identical id "
                "(CONTRACT §7) REGARDLESS of artifact kind, so a re-file under a "
                "different kind is a collision, not a no-op; use reconcile_queue.py "
                "if the existing artifact is missing its ledger event.",
            )
            break

    for area in ("rejected", "parked"):
        marker = queue / area / f"{_stem(ident)}.json"
        presence = marker_presence(marker)
        if presence == PRESENCE_ABSENT:
            continue
        if presence == PRESENCE_UNDETERMINED:
            # Fail CLOSED, exactly as an unreadable marker below does: a ban we
            # cannot even stat is not a ban that is absent.
            rep.refuse(
                EXIT_COULD_NOT_RUN, f"id.unreadable_{area}",
                f"{area}/ marker for {ident[:19]}… cannot be stat'd, so it can "
                "be neither confirmed nor ruled out",
                f"fix the permissions on {marker.parent} first — a marker that "
                "cannot be examined is an instrument error, not an absence.",
            )
            continue
        try:
            record = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            # Fail CLOSED: a marker we cannot read might be a live ban, and
            # filing past it defeats the §4 trap invisibly.
            rep.refuse(
                EXIT_COULD_NOT_RUN, f"id.unreadable_{area}",
                f"{area}/ marker for {ident[:19]}… exists but cannot be read ({exc})",
                f"fix or remove {marker} first — an unreadable marker is an "
                "instrument error, not an absence.",
            )
            continue
        if not isinstance(record, dict):
            rep.refuse(
                EXIT_COULD_NOT_RUN, f"id.malformed_{area}",
                f"{area}/ marker for {ident[:19]}… is JSON but not an object "
                f"({type(record).__name__})",
                f"fix {marker} to a JSON object first.",
            )
            continue
        if area == "parked":
            # Parking never decays by TTL (CONTRACT §9): only an authenticated
            # human `unparked` event ends it.
            rep.refuse(
                EXIT_REFUSED_DO_NOT_RETRY, "id.live_parked",
                f"{ident[:19]}… is parked awaiting a human decision: "
                f"{str(record.get('reason', ''))[:200]}",
                "parking ends only with an authenticated unparked event.",
            )
            continue
        expires = record.get("expires_at")
        if _expired(expires):
            rep.warnings.append(
                f"{area}/ carries an EXPIRED marker for this id — re-raising is allowed")
            continue
        rep.refuse(
            EXIT_REFUSED_DO_NOT_RETRY, f"id.live_{area}",
            f"{ident[:19]}… carries a live {area} marker until {expires} "
            f"(class={record.get('class')!r}): {str(record.get('reason', ''))[:200]}",
            "this symptom was already answered or rejected (CONTRACT.md §4: both "
            "outcomes tombstone). Read the tombstone's reason; if new evidence "
            "exists after expiry, re-raise with `supersedes`.",
        )


def append_found(queue: Path, art: dict) -> None:
    """The one `found` line that makes the artifact visible to every reader."""
    payload = art.get("payload") or {}
    detail = {"title": art.get("title", ""),
              "symptom": str(payload.get("symptom", ""))[:200]}
    if payload.get("class"):
        detail["class"] = payload["class"]
    line = json.dumps({
        "ts": _now(),
        "role": (art.get("producer") or {}).get("role", "external"),
        "event": FOUND_EVENT,
        "id": art["id"],
        "actor": (art.get("producer") or {}).get("actor", "file_finding.py"),
        "detail": detail,
    }, separators=(",", ":")) + "\n"
    _append_ledger_line(queue, line)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Validate and atomically file a finding, with its `found` "
                    "ledger event, as one operation. Refuses rather than writing "
                    "an artifact no reader will ever see.")
    ap.add_argument("draft", help="path to the draft finding JSON")
    ap.add_argument("--queue", default=os.environ.get("LOOPQUEUE") or
                    str(Path.home() / "OmniAgentOS" / "var" / "loopqueue"))
    ap.add_argument("--check", action="store_true", help="validate only; write nothing")
    ap.add_argument("--no-ledger", action="store_true",
                    help="refused: a filing without its ledger event is invisible "
                         "to every status reader (use --check to validate only)")
    args = ap.parse_args(argv)

    if args.no_ledger:
        print(f"REFUSE[{EXIT_COULD_NOT_RUN}] no-ledger: status is ledger-derived "
              "(CONTRACT §8), so an artifact without its 'found' event is a "
              "contract-invalid invisible filing — that is precisely how 63 "
              "findings accumulated unseen. Use --check to validate without "
              "writing.", file=sys.stderr)
        return EXIT_COULD_NOT_RUN

    try:
        art = json.loads(Path(args.draft).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"REFUSE[{EXIT_COULD_NOT_RUN}] draft: cannot read {args.draft}: {exc}",
              file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    if not isinstance(art, dict):
        print(f"REFUSE[{EXIT_COULD_NOT_RUN}] draft: not a JSON object", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    if art.get("kind") != "finding":
        print(f"REFUSE[{EXIT_COULD_NOT_RUN}] kind: this tool files findings; got "
              f"{art.get('kind')!r}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN

    queue = Path(args.queue).expanduser()
    rep = Report()

    check_schema(art, rep)
    check_finding_identity(art, queue, rep)

    from contract_lens import _CONVERSATION_REFS
    hit = _CONVERSATION_REFS.search(json.dumps(art.get("payload") or {}, ensure_ascii=False))
    if hit:
        rep.warnings.append(
            f"lens.context: conversation-context reference {hit.group(0)!r} — "
            "meaningless to whoever picks this finding up cold")

    for warning in rep.warnings:
        print(f"warn: {warning}", file=sys.stderr)

    if rep.refusals:
        for refusal in rep.refusals:
            print(refusal.render(), file=sys.stderr)
        print(f"\nNOT WRITTEN. {len(rep.refusals)} refusal(s); exit {rep.exit_code}.",
              file=sys.stderr)
        return rep.exit_code

    art.setdefault("contract", "v1.1")

    if args.check:
        print(f"OK would write {art['id'][:19]}…")
        return 0

    target = queue / "findings" / f"{_stem(art['id'])}.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, art)
    except FileExistsError:
        print(f"REFUSE[{EXIT_REFUSED_DO_NOT_RETRY}] id.duplicate: another producer "
              f"published {art['id'][:19]}… between the duplicate check and the "
              f"write — the artifact on disk wins.", file=sys.stderr)
        return EXIT_REFUSED_DO_NOT_RETRY
    except OSError as exc:
        print(f"REFUSE[{EXIT_COULD_NOT_RUN}] write: could not publish to {target}: "
              f"{exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    try:
        append_found(queue, art)
    except (LedgerNotDurable, OSError) as exc:
        # Roll back ONLY on a confirmed ABSENT; an UNKNOWN (unreadable ledger)
        # keeps the artifact — a rollback we cannot justify creates a phantom
        # (R3-FI-01). Same discipline as file_proposal.main / file_inquiry.main.
        state = _ledger_event_state(queue, art["id"], FOUND_EVENT)
        if state == LEDGER_EVENT_ABSENT:
            rolled_back = _rollback(target)
            print(_ledger_fail_message(exc, target, queue, rolled_back), file=sys.stderr)
            return EXIT_COULD_NOT_RUN
        detail = ("is on disk" if state == LEDGER_EVENT_PRESENT
                  else "could not be confirmed (ledger unreadable)")
        print(f"warn: ledger: the 'found' event {detail} but the append reported "
              f"an error ({exc}); the artifact is KEPT (a rollback that cannot "
              f"confirm absence would risk a phantom). Verify the ledger.",
              file=sys.stderr)
    print(f"OK wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
