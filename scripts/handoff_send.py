#!/usr/bin/env python3
"""Deliver a ``HANDOFF/*.md`` doc to the teammate it is addressed to, on Slack.

Why this exists: handoff docs were being WRITTEN but never DELIVERED. Two docs
addressed to Alice sat in ``HANDOFF/`` for a day, unread, because "handoff
written" and "handoff received" were separate facts with nothing connecting
them (2026-08-14). This closes the loop mechanically:

    scripts/handoff_send.py HANDOFF/alice-foo-2026-08-13.md      # send summary
    scripts/handoff_send.py HANDOFF/alice-foo-2026-08-13.md --full
    scripts/handoff_send.py --list                              # what is undelivered

The recipient is inferred from the filename (``alice-…``) or the ``# Alice — …``
H1, then resolved through ``configs/team_slack_map.yaml`` — the same map the
Slack ingest verbs use, so there is exactly one roster of record.

The rendered summary is DETERMINISTIC (title + lede + section headers + path).
No model is in this path: delivery must not depend on an LLM being available,
and a summary that varies run to run cannot be diffed or trusted.

Deliveries are recorded in ``HANDOFF/.delivered.jsonl`` with the doc's content
hash, so ``--list`` re-flags a doc that CHANGED after it was sent — a stale
delivery is as bad as no delivery. Delivery state is also scoped by recipient
and mode (summary vs. full) — see ``delivery_state`` — so a doc delivered to
one person, or as a summary, is never mistaken for having reached someone
else, or having gone out in full.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omniagentos.team.notify import SlackNotifier  # noqa: E402
from omniagentos.team.report import load_slack_env  # noqa: E402
from omniagentos.team.slack_updates import load_slack_map  # noqa: E402

HANDOFF_DIR = REPO_ROOT / "HANDOFF"
LEDGER_PATH = HANDOFF_DIR / ".delivered.jsonl"

#: Slack hard-limits a message near 40k chars, but a wall that long is not read.
#: ``--full`` truncates here and points at the doc rather than shipping a wall.
MAX_FULL_CHARS = 3500

#: ``# Alice — Handoff (2026-08-13): …`` → recipient token ``Alice``.
_H1_RE = re.compile(r"^#\s+([A-Za-z][A-Za-z0-9_-]*)\s*[—–:-]", re.MULTILINE)
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class HandoffError(RuntimeError):
    """A caller-facing failure: unresolvable recipient, missing doc, bad map."""


@dataclass(frozen=True)
class Recipient:
    employee_id: str
    slack_user_id: str

    @property
    def name(self) -> str:
        return self.employee_id.removeprefix("emp_").capitalize()


@dataclass(frozen=True)
class HandoffDoc:
    path: Path
    title: str
    lede: str
    sections: tuple[str, ...]
    body: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    @property
    def rel_path(self) -> str:
        try:
            return str(self.path.resolve().relative_to(REPO_ROOT))
        except ValueError:
            return str(self.path)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def parse_doc(path: Path) -> HandoffDoc:
    """Read a handoff markdown file into its title, lede, and section headers."""
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HandoffError(f"cannot read {path}: {exc}") from exc

    lines = body.splitlines()
    title = ""
    lede_start = 0
    for index, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip()
            lede_start = index + 1
            break
    if not title:
        title = path.stem

    # Lede = the first non-empty paragraph after the H1, joined to one line.
    lede_lines: list[str] = []
    for line in lines[lede_start:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            break
        if not stripped:
            if lede_lines:
                break
            continue
        lede_lines.append(stripped)
    lede = " ".join(lede_lines)

    sections = tuple(match.group(1).strip() for match in _SECTION_RE.finditer(body))
    return HandoffDoc(path=path, title=title, lede=lede, sections=sections, body=body)


def infer_recipient_token(path: Path, doc: HandoffDoc | None = None) -> str | None:
    """Recipient name from the filename prefix, falling back to the H1.

    ``alice-globex-…md`` → ``alice``. The filename wins over the heading: a
    doc ABOUT someone ("# Alice — …" inside bob-queue.md) must not be routed
    to them by accident, and the filename is the thing an author picks
    deliberately.
    """
    stem_head = path.stem.split("-", 1)[0].strip().lower()
    if stem_head:
        return stem_head
    if doc is not None:
        match = _H1_RE.search(doc.body)
        if match:
            return match.group(1).strip().lower()
    return None


# ---------------------------------------------------------------------------
# roster
# ---------------------------------------------------------------------------


def roster(slack_map_path: Path | None = None) -> dict[str, Recipient]:
    """``{"alice": Recipient(emp_alice, U0A1…)}`` from the team Slack map."""
    try:
        mapping = load_slack_map(slack_map_path) if slack_map_path else load_slack_map()
    except ValueError as exc:  # unknown employee id in the map — fail loudly
        raise HandoffError(str(exc)) from exc
    people: dict[str, Recipient] = {}
    for slack_user_id, employee_id in mapping.items():
        token = employee_id.removeprefix("emp_").lower()
        if token in people:
            print(
                f"handoff-send: duplicate Slack id for {employee_id!r}; keeping "
                f"{people[token].slack_user_id!r}, ignoring {slack_user_id!r}",
                file=sys.stderr,
            )
            continue
        people[token] = Recipient(employee_id=employee_id, slack_user_id=slack_user_id)
    return people


def resolve_recipient(token: str | None, people: dict[str, Recipient]) -> Recipient:
    if not token:
        raise HandoffError(
            "no recipient in the filename or heading — pass --to <name> "
            f"(known: {', '.join(sorted(people)) or 'none'})"
        )
    normalized = token.removeprefix("emp_").strip().lower().lstrip("@")
    person = people.get(normalized)
    if person is None:
        raise HandoffError(
            f"{token!r} is not in configs/team_slack_map.yaml "
            f"(known: {', '.join(sorted(people)) or 'none'})"
        )
    return person


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def render_message(doc: HandoffDoc, recipient: Recipient, *, full: bool = False) -> str:
    """The DM text. Deterministic — same doc in, same bytes out."""
    # "Alice — Handoff (…): X" → "Handoff (…): X"; the greeting already names them.
    title = re.sub(rf"^{re.escape(recipient.name)}\s*[—–-]\s*", "", doc.title, flags=re.IGNORECASE)
    header = f"Hi {recipient.name} — handoff for you: *{title}*"
    parts = [header, "", f"Full doc: {doc.rel_path} (in OmniAgentOS)"]

    if full:
        body = doc.body.strip()
        if len(body) > MAX_FULL_CHARS:
            body = body[:MAX_FULL_CHARS].rstrip() + f"\n\n… truncated — read {doc.rel_path}"
        parts += ["", body]
        return "\n".join(parts)

    if doc.lede:
        parts += ["", doc.lede]
    if doc.sections:
        parts += ["", "What's in it:"]
        parts += [f"• {section}" for section in doc.sections]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# delivery ledger
# ---------------------------------------------------------------------------


def read_ledger(ledger_path: Path = LEDGER_PATH) -> list[dict]:
    """Every recorded delivery. A corrupt line is skipped, never fatal — the
    ledger is a convenience index, and losing it must not block a send."""
    if not ledger_path.exists():
        return []
    records: list[dict] = []
    for raw in ledger_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            print(f"handoff-send: skipping unparsable ledger line: {line[:80]}", file=sys.stderr)
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def record_delivery(
    doc: HandoffDoc,
    recipient: Recipient,
    *,
    mode: str,
    ledger_path: Path = LEDGER_PATH,
    now: datetime | None = None,
) -> dict:
    record = {
        "ts": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "path": doc.rel_path,
        "sha256": doc.sha256,
        "recipient": recipient.employee_id,
        "slack_user_id": recipient.slack_user_id,
        "mode": mode,
    }
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return record


#: Fullness ordering for ``mode`` — "full" satisfies an ask for "summary", but
#: not the reverse. Unknown/missing modes rank as "summary", the weakest mode.
_MODE_RANK = {"summary": 0, "full": 1}


def _normalize_recipient(value: str | None) -> str | None:
    """``"emp_alice"``, ``"Alice"``, ``"@alice"`` all normalize to ``"alice"``."""
    if not value:
        return None
    return value.removeprefix("emp_").strip().lower().lstrip("@")


def delivery_state(
    doc: HandoffDoc,
    records: Iterable[dict],
    *,
    recipient: str | None = None,
    mode: str = "summary",
) -> tuple[str, dict | None]:
    """``("delivered"|"stale"|"undelivered", latest_record_or_None)``.

    ``stale`` = delivered, but the doc changed afterwards. That is the case the
    ledger exists to catch; a resend is warranted.

    A record only counts towards this check when it was sent to the SAME
    ``recipient`` (any employee-id/handle form, normalized) in an
    equal-or-fuller ``mode`` — "full" satisfies an ask for "summary", but
    "summary" never satisfies an ask for "full". Without this scoping, a doc
    sent to Alice read as "delivered" for a re-run addressed to Bob (they
    would never receive it), and a doc sent as a summary read as "delivered"
    for a later ``--full`` request (the full body would never go out).

    ``recipient=None`` (the default) leaves the check unscoped by recipient —
    for callers that only want "has ANY delivery of this doc happened, in at
    least this mode".

    Legacy ledger records written before this scoping existed (missing the
    ``recipient``/``mode`` keys) are read as if they had targeted the doc's
    OWN inferred default recipient in "summary" mode — the only recipient and
    mode a legacy send ever used. Treating a missing recipient as matching NO
    recipient would make every legacy delivery look undelivered again and
    trigger a one-time resend across the whole ledger the first time this
    scoped check runs; treating it as matching its real original recipient is
    both accurate and avoids that resend storm.
    """
    wanted_recipient = _normalize_recipient(recipient)
    wanted_rank = _MODE_RANK.get(mode, 0)
    default_recipient = (
        _normalize_recipient(infer_recipient_token(doc.path, doc))
        if wanted_recipient is not None
        else None
    )

    latest: dict | None = None
    for record in records:
        if record.get("path") != doc.rel_path:
            continue
        record_recipient = _normalize_recipient(record.get("recipient")) or default_recipient
        if wanted_recipient is not None and record_recipient != wanted_recipient:
            continue
        record_rank = _MODE_RANK.get(record.get("mode") or "summary", 0)
        if record_rank < wanted_rank:
            continue
        latest = record
    if latest is None:
        return "undelivered", None
    return ("delivered" if latest.get("sha256") == doc.sha256 else "stale"), latest


def scan_pending(
    handoff_dir: Path = HANDOFF_DIR,
    ledger_path: Path = LEDGER_PATH,
    slack_map_path: Path | None = None,
) -> list[tuple[HandoffDoc, Recipient, str]]:
    """Handoff docs addressed to a mapped person that are undelivered or stale."""
    people = roster(slack_map_path)
    records = read_ledger(ledger_path)
    pending: list[tuple[HandoffDoc, Recipient, str]] = []
    for path in sorted(handoff_dir.glob("*.md")):
        token = infer_recipient_token(path)
        if token not in people:
            continue
        doc = parse_doc(path)
        recipient = people[token]
        state, _ = delivery_state(doc, records, recipient=recipient.employee_id)
        if state != "delivered":
            pending.append((doc, recipient, state))
    return pending


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _send(text: str, recipient: Recipient) -> tuple[bool, str | None]:
    load_slack_env()
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return False, "SLACK_BOT_TOKEN not set (checked env and ~/.config/omni/connections.env)"
    notifier = SlackNotifier(token)
    ok = notifier.post_dm(recipient.slack_user_id, text)
    return ok, None if ok else (notifier.last_error or "unknown")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="handoff-send",
        description="DM a HANDOFF/*.md doc to the teammate it is addressed to.",
    )
    parser.add_argument("doc", nargs="?", help="path to the handoff markdown file")
    parser.add_argument("--to", help="recipient name, overriding the filename/heading")
    parser.add_argument("--full", action="store_true", help="send the doc body, not a summary")
    parser.add_argument("--dry-run", action="store_true", help="print the message, send nothing")
    parser.add_argument(
        "--list",
        dest="list_pending",
        action="store_true",
        help="list handoff docs that are undelivered or changed since delivery",
    )
    args = parser.parse_args(argv)

    try:
        if args.list_pending:
            pending = scan_pending()
            if not pending:
                print("handoff-send: nothing pending — every addressed doc is delivered.")
                return 0
            print(f"{len(pending)} handoff doc(s) pending:")
            for doc, recipient, state in pending:
                flag = "CHANGED since delivery" if state == "stale" else "never delivered"
                print(f"  [{flag}] {doc.rel_path} → {recipient.name}")
            return 0

        if not args.doc:
            parser.error("a doc path is required unless --list is given")

        path = Path(args.doc)
        if not path.is_absolute() and not path.exists():
            path = REPO_ROOT / args.doc
        if not path.is_file():
            raise HandoffError(f"no such handoff doc: {args.doc}")

        doc = parse_doc(path)
        people = roster()
        recipient = resolve_recipient(args.to or infer_recipient_token(path, doc), people)
        text = render_message(doc, recipient, full=args.full)

        mode = "full" if args.full else "summary"
        state, latest = delivery_state(
            doc, read_ledger(), recipient=recipient.employee_id, mode=mode
        )
        if state == "delivered" and not args.dry_run:
            print(
                f"handoff-send: {doc.rel_path} was already delivered to {recipient.name} "
                f"at {latest.get('ts') if latest else '?'} and has not changed since — "
                "not resending. Edit the doc, or use --dry-run to review the message.",
                file=sys.stderr,
            )
            return 0

        if args.dry_run:
            print(f"--- would DM {recipient.name} ({recipient.slack_user_id}) ---")
            print(text)
            return 0

        ok, error = _send(text, recipient)
        if not ok:
            print(f"handoff-send: Slack delivery failed: {error}", file=sys.stderr)
            return 1
        record_delivery(doc, recipient, mode=mode)
        print(f"handoff-send: delivered {doc.rel_path} to {recipient.name}")
        return 0
    except HandoffError as exc:
        print(f"handoff-send: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
