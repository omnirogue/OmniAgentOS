#!/usr/bin/env python3
"""Validate and atomically file an INQUIRY. The sibling of file_proposal.py.

Why inquiries need a writer at all: the reverse edge's whole safety argument
(CONTRACT.md §4) is that an answered or rejected inquiry leaves a tombstone in
``rejected/`` so the SAME observation, re-noticed by a fresh context, is
dropped at source instead of making Planning research a question it already
answered. That check only runs if something runs it — and until this tool,
inquiries were written by hand (two were hand-rolled by the operator session on
2026-08-08 alone), which means no tombstone lookup, no duplicate check, no
schema validation, and a torn file if the writer dies mid-write.

Checks, in order:
  1. kind is "inquiry"; payload carries area / observation / why_not_a_fix
     (schema-enforced — why_not_a_fix is the point of the artifact).
  2. id equals the canonical hash of payload (canonical.content_id).
  3. drop-at-source: refuse if the id already sits in inquiries/, or carries a
     live (unexpired) marker in rejected/ — the §4 re-raise trap — or parked/.
  4. atomic write (tmp in-dir -> fsync -> rename -> fsync dir), then one
     O_APPEND "inquired" ledger line.

Exit codes match file_proposal.py: 0 written · 1 refused-fixable ·
2 could-not-run · 3 refused-do-not-retry (Ruling #4, 2026-08-09: exit 2 =
COULD NOT RUN estate-wide — the constants are imported from file_proposal.py so
this tool tracks that swap automatically). Warn-only contract-lens checks do not
apply here beyond the conversation-context class: an inquiry is deliberately
cheap, and demanding execution evidence of a QUESTION would stop people asking.
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


def check_inquiry_identity(art: dict, queue: Path, rep: Report) -> None:
    from canonical import content_id

    payload = art.get("payload")
    if not isinstance(payload, dict):
        rep.refuse(1, "id.payload", "payload is missing or not an object")
        return

    expected = content_id(payload)
    if art.get("id") != expected:
        rep.refuse(
            1, "id.hash",
            f"id does not match the canonical hash of payload "
            f"(declared {str(art.get('id'))[:19]}…, computed {expected[:19]}…)",
            f"set id to {expected}",
        )
        art["id"] = expected

    ident = art["id"]
    if (queue / "inquiries" / f"{_stem(ident)}.json").exists():
        rep.refuse(
            EXIT_REFUSED_DO_NOT_RETRY, "id.duplicate",
            f"{ident[:19]}… is already filed in inquiries/",
            "the same observation is already awaiting Planning — re-filing it "
            "adds queue depth, not signal.",
        )

    for area in ("rejected", "parked"):
        marker = queue / area / f"{_stem(ident)}.json"
        if not marker.exists():
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
            # Valid JSON but not an object: unreadable, not absent. Fail closed.
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
            "this observation was already answered or rejected (CONTRACT.md §4: "
            "both outcomes tombstone). Read the tombstone's reason; if new "
            "evidence exists after expiry, re-raise with `supersedes`.",
        )


def append_inquired(queue: Path, art: dict) -> None:
    line = json.dumps({
        "ts": _now(),
        "role": (art.get("producer") or {}).get("role", "external"),
        "event": "inquired",
        "id": art["id"],
        "actor": (art.get("producer") or {}).get("actor", "file_inquiry.py"),
        "detail": {"title": art.get("title", ""),
                   "area": (art.get("payload") or {}).get("area", "")},
    }, separators=(",", ":")) + "\n"
    _append_ledger_line(queue, line)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Validate and atomically file an inquiry. Refuses a re-raise "
                    "of an answered or rejected observation (the §4 trap).")
    ap.add_argument("draft", help="path to the draft inquiry JSON")
    ap.add_argument("--queue", default=os.environ.get("LOOPQUEUE") or
                    str(Path.home() / "OmniAgentOS" / "var" / "loopqueue"))
    ap.add_argument("--check", action="store_true", help="validate only; write nothing")
    ap.add_argument("--no-ledger", action="store_true",
                    help="refused: a filing without its ledger event is invisible "
                         "to every status reader (use --check to validate only)")
    args = ap.parse_args(argv)

    if args.no_ledger:
        print(f"REFUSE[{EXIT_COULD_NOT_RUN}] no-ledger: status is ledger-derived "
              "(CONTRACT §8), so an artifact without its 'inquired' event is a "
              "contract-invalid invisible filing. Use --check to validate "
              "without writing.", file=sys.stderr)
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
    if art.get("kind") != "inquiry":
        print(f"REFUSE[{EXIT_COULD_NOT_RUN}] kind: this tool files inquiries; got "
              f"{art.get('kind')!r}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN

    queue = Path(args.queue).expanduser()
    rep = Report()

    check_schema(art, rep)
    check_inquiry_identity(art, queue, rep)

    from contract_lens import _CONVERSATION_REFS
    hit = _CONVERSATION_REFS.search(json.dumps(art.get("payload") or {}, ensure_ascii=False))
    if hit:
        rep.warnings.append(
            f"lens.context: conversation-context reference {hit.group(0)!r} — "
            "meaningless to whoever researches this cold")

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

    target = queue / "inquiries" / f"{_stem(art['id'])}.json"
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
        append_inquired(queue, art)
    except (LedgerNotDurable, OSError) as exc:
        # Roll back ONLY on a confirmed ABSENT; an UNKNOWN (unreadable ledger)
        # keeps the artifact — a rollback we cannot justify creates a phantom
        # (R3-FI-01). Same discipline as file_proposal.main.
        state = _ledger_event_state(queue, art["id"], "inquired")
        if state == LEDGER_EVENT_ABSENT:
            rolled_back = _rollback(target)
            print(_ledger_fail_message(exc, target, queue, rolled_back), file=sys.stderr)
            return EXIT_COULD_NOT_RUN
        detail = ("is on disk" if state == LEDGER_EVENT_PRESENT
                  else "could not be confirmed (ledger unreadable)")
        print(f"warn: ledger: the 'inquired' event {detail} but the append "
              f"reported an error ({exc}); the artifact is KEPT (a rollback that "
              f"cannot confirm absence would risk a phantom). Verify the ledger.",
              file=sys.stderr)
    print(f"OK wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
