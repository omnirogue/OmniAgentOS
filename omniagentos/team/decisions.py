"""Numbered operator decisions: confirm-first repairs from tracker bottlenecks.

The operator's protocol (the operator, 2026-08-11): when the system wants to act, it
posts NUMBERED proposals to the team channel and waits — "🔧 Repair proposals:
1. <action> — reply `1 yes` or `1 no`". Nothing executes unconfirmed. Numbers
are globally monotonic (never reused), each decision is one-shot (the first
authorized yes/no consumes it), and undecided proposals expire after 24h.

V1 repair action: file a ``finding`` into the loopqueue — the implementer
loop already drains findings, so a confirmed repair becomes robot work with
no new execution machinery. The filing goes through the pipeline's own
contract: envelope validated by ``validate_envelope.py``, id = canonical
``content_id(payload)``, atomic write into ``findings/``, and the ``found``
ledger event through ``ledger_write.py`` (the single ledger transport).

Replies are read from channel history by the 5-minute processor — the same
polling transport the tracker uses — so no Socket-Mode dependency. Existing
repair/overnight/balancer records remain open to any roster-mapped human; EDC
DM records additionally require the private decision's owner.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from omniagentos.team.notify import DEFAULT_CHANNEL, SlackNotifier, load_slack_env, load_slack_map
from omniagentos.team.session_collector import _write_atomic
from omniagentos.team.session_tracker import _iso, _parse_iso, _utcnow

REPO_ROOT = Path("/Users/youruser/OmniAgentOS")
STATE_FILENAME = "team-decisions.json"
EXPIRE_SECONDS = 24 * 3600
DEDUP_SECONDS = 6 * 3600
_BRIDGE = REPO_ROOT / "pipeline" / "bridge"

# "1 yes", "1. no", "repair 3 YES", "overnight 2 no", "balancer 4 yes",
# "edc 5 yes" — the
# number is the decision id; the whole message must be the reply (a trailing
# "…but wait no" makes the message NOT a decision rather than a half-read one).
# The kind prefix (group 1, optional) is captured so a caller can refuse to
# let a "repair 1 yes" reply authorize a decision that is actually stored
# under a DIFFERENT kind at number 1 — see ``collect_replies``/``process_replies``.
_REPLY_RE = re.compile(
    r"^\s*(repair|overnight|balancer|edc)?\s*(\d+)[.:]?\s+(yes|no)\s*[.!]?\s*$",
    re.IGNORECASE,
)

#: Per-kind glyph for the decline/confirm channel lines.
_KIND_GLYPHS = {"overnight": "🌙", "balancer": "⚖️", "repair": "🔧", "edc": "📨"}


class SlackHistoryError(RuntimeError):
    """Slack history could not be fetched; distinct from a valid empty history."""


def _state_path(repo_root: Path) -> Path:
    from omniagentos.runtime_paths import resolve_var_root

    try:
        return Path(resolve_var_root()) / STATE_FILENAME
    except Exception:  # noqa: BLE001 — sim-context refusal falls back to the repo tree
        return repo_root / "var" / STATE_FILENAME


def _lock_path(repo_root: Path) -> Path:
    """Stable lockfile path, sibling to the state file (never unlinked)."""
    return _state_path(repo_root).with_suffix(".lock")


@contextlib.contextmanager
def _state_lock(repo_root: Path) -> Iterator[None]:
    """Exclusive, blocking ``flock`` around the WHOLE shared decision
    namespace's load->modify->save cycle.

    Every caller that reads ``team-decisions.json``, decides something from
    it (next_number allocation, a status flip), and writes it back MUST hold
    this for the entire span -- including any intermediate saves along the
    way (e.g. ``process_replies``'s persist-before-execute step). Two
    concurrent registrars (``register_repair_proposals`` and a future
    ``register_balancer_proposals``) racing an unprotected load->modify->save
    would both read the same stale ``next_number`` and the second ``save``
    would silently clobber the first's decision (last-write-wins) --
    numbers collide and one registration is lost. Same idiom as
    ``omniagentos.improvement_chain``'s ``_with_parked_lock``/``_alert_once``:
    blocking ``LOCK_EX`` on a stable path, released but never unlinked, so
    every caller (this process, another tick's fresh process, a different
    registrar kind) keeps contending for the same file instead of racing an
    unlocked read/modify/write.
    """
    lock_path = _lock_path(repo_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_state(repo_root: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_state_path(repo_root).read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("decisions"), list):
            return payload
    except OSError:
        pass
    except ValueError:
        # Number reuse after corruption is protocol-visible — say so loudly.
        print("decisions: state_corrupted — starting a fresh numbered slate", file=sys.stderr)
    return {"next_number": 1, "decisions": []}


def save_state(repo_root: Path, state: dict[str, Any]) -> None:
    _write_atomic(_state_path(repo_root), state)


def _expire(state: dict[str, Any]) -> None:
    now = _utcnow()
    for decision in state["decisions"]:
        if (
            decision.get("status") == "pending"
            and now - _parse_iso(str(decision.get("proposed_at") or "")) > EXPIRE_SECONDS
        ):
            decision["status"] = "expired"


def _recent_same_kind(state: dict[str, Any], dedup_key: str) -> bool:
    now = _utcnow()
    for decision in state["decisions"]:
        if decision.get("dedup_key") != dedup_key:
            continue
        if decision.get("status") == "pending":
            return True
        decided = _parse_iso(str(decision.get("decided_at") or ""))
        if decided and now - decided < DEDUP_SECONDS:
            return True
    return False


def register_repair_proposals(repo_root: Path, overall: Any) -> list[dict[str, Any]]:
    """Turn a non-none bottleneck into numbered pending repairs (deduplicated).

    Returns every currently-pending decision (new and previously proposed) so
    the caller can render the full open slate in one place.

    The whole load->modify->save cycle -- including the next_number
    allocation -- runs inside :func:`_state_lock` so a concurrent registrar
    (this function called from another thread/process, or a future
    ``register_balancer_proposals``) can never read the same stale
    ``next_number``: distinct decisions always get distinct numbers, and
    neither registration is lost to a last-write-wins save.
    """
    with _state_lock(repo_root):
        return _register_repair_proposals_locked(repo_root, overall)


def _register_repair_proposals_locked(repo_root: Path, overall: Any) -> list[dict[str, Any]]:
    state = load_state(repo_root)
    _expire(state)
    bottleneck = str(getattr(overall, "bottleneck", "none"))
    proposals: list[tuple[str, str, dict[str, Any]]] = []
    if bottleneck.startswith("merge gate red"):
        proposals.append(
            (
                "repair:merge-gate-red",
                "File a loopqueue repair job for the merge gate "
                "(failed merges with zero successes this hour)",
                {
                    "classification": "instrument-error",
                    "paths": ["pipeline/bridge/gate_host.py"],
                    "symptom": (
                        "Session tracker observed failed merges with zero successful "
                        f"merges in the last hour (merge queue {overall.merge_queue}, "
                        f"failed {overall.failed_merges_last_hour}). Operator-confirmed repair."
                    ),
                },
            )
        )
    elif bottleneck.startswith("gate throughput"):
        proposals.append(
            (
                "repair:gate-throughput",
                f"File a loopqueue repair job for gate throughput ({overall.merge_queue} "
                "candidates waiting 48h+)",
                {
                    "classification": "instrument-error",
                    "paths": ["pipeline/bridge/train_assembler.py"],
                    "symptom": (
                        f"Merge queue has held {overall.merge_queue} submitted-not-terminal "
                        "candidates; throughput below arrival rate. Operator-confirmed repair."
                    ),
                },
            )
        )
    for dedup_key, title, payload in proposals:
        if _recent_same_kind(state, dedup_key):
            continue
        number = int(state["next_number"])
        state["next_number"] = number + 1
        state["decisions"].append(
            {
                "number": number,
                "kind": "repair",
                "dedup_key": dedup_key,
                "title": title,
                "payload": payload,
                "status": "pending",
                "proposed_at": _iso(_utcnow()),
                "decided_by": None,
                "decided_at": None,
                "result": None,
            }
        )
    save_state(repo_root, state)
    return [d for d in state["decisions"] if d.get("status") == "pending"]


def render_proposals(pending: list[dict[str, Any]]) -> str:
    if not pending:
        return ""
    lines = [
        "🔧 Repair proposals — reply IN THIS CHANNEL (not in a thread) with the "
        "number and yes/no (e.g. `1 yes`); the first reply decides:"
    ]
    lines.extend(f"repair {d['number']}. {d['title']}" for d in pending)
    return "\n".join(lines)


def _slack_history(token: str, channel: str, oldest: float) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"channel": channel, "limit": 100, "oldest": f"{oldest:.6f}"})
    request = urllib.request.Request(
        f"https://slack.com/api/conversations.history?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SlackHistoryError(f"channel {channel}: {exc}") from exc
    if not payload.get("ok"):
        raise SlackHistoryError(
            f"channel {channel}: Slack returned {payload.get('error') or 'unknown_error'}"
        )
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        raise SlackHistoryError(f"channel {channel}: malformed messages payload")
    return messages


def collect_replies(
    messages: list[dict[str, Any]], roster: dict[str, str]
) -> list[tuple[int, bool, str, str | None]]:
    """(number, approved, employee_id, kind_hint) per authorized reply, oldest first.

    ``kind_hint`` is the reply's own kind prefix, lowercased,
    or ``None`` when the reply is a bare "N yes"/"N no" with no prefix. The
    caller (``process_replies``) must refuse to authorize a decision whose
    STORED kind does not match a non-``None`` hint -- a "repair 1 yes" reply
    naming a kind must never decide a decision that is actually stored under
    a different kind at that same number.
    """
    replies: list[tuple[int, bool, str, str | None]] = []
    for message in reversed(messages):  # Slack returns newest first
        employee = roster.get(str(message.get("user") or ""))
        if employee is None:
            continue
        match = _REPLY_RE.match(str(message.get("text") or ""))
        if match is None:
            continue
        kind_hint = match.group(1).lower() if match.group(1) else None
        replies.append((int(match.group(2)), match.group(3).lower() == "yes", employee, kind_hint))
    return replies


def _file_repair_finding(repo_root: Path, decision: dict[str, Any]) -> str:
    """File the confirmed repair through the pipeline's own contract."""
    sys.path.insert(0, str(_BRIDGE))
    try:
        from canonical import content_id  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    observed_sha = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    payload = {
        "classification": decision["payload"]["classification"],
        "symptom": decision["payload"]["symptom"],
        "confirmed_by": decision.get("decided_by"),
        "decision_number": decision["number"],
    }
    envelope = {
        "contract": "v1.1",
        "kind": "finding",
        "created_at": _iso(_utcnow()),
        "producer": {
            "role": "reviewer",
            "actor": f"team-repair@decision-{decision['number']}",
            "lineage": "anthropic",
        },
        "observed_sha": observed_sha,
        "id": content_id(payload),
        "priority": 1,
        "title": decision["title"][:200],
        "paths": decision["payload"]["paths"],
        "payload": payload,
    }
    queue = repo_root / "var" / "loopqueue"
    draft = queue / "tmp" / f"repair-{decision['number']}.json"
    _write_atomic(draft, envelope)
    check = subprocess.run(
        [
            str(repo_root / ".venv" / "bin" / "python"),
            str(_BRIDGE / "validate_envelope.py"),
            str(draft),
        ],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        draft.unlink(missing_ok=True)  # no orphaned refused drafts in tmp/
        raise RuntimeError(f"envelope refused: {check.stdout} {check.stderr}"[:300])
    target = queue / "findings" / f"{envelope['id'].replace(':', '_')}.json"
    _write_atomic(target, envelope)
    event = {
        "ts": _iso(_utcnow()),
        "role": "reviewer",
        "event": "found",
        "id": envelope["id"],
        "actor": str(envelope["producer"]["actor"]),
        "detail": {"reason": payload["symptom"][:200], "class": payload["classification"]},
    }
    ledger = subprocess.run(
        [
            str(repo_root / ".venv" / "bin" / "python"),
            str(_BRIDGE / "ledger_write.py"),
            "append",
            "--queue",
            str(queue),
        ],
        input=json.dumps(event),
        capture_output=True,
        text=True,
    )
    if ledger.returncode != 0:
        raise RuntimeError(f"ledger append refused: {ledger.stderr}"[:300])
    return str(envelope["id"])


def process_replies(
    repo_root: Path,
    *,
    notifier: Any,
    token: str,
    channel: str,
    dry_run: bool = False,
) -> dict[str, int]:
    """One processor pass: read replies, decide one-shot, execute approvals.

    The whole pass -- load, every intermediate persist-before-execute save,
    and the final save -- runs inside :func:`_state_lock` so it can never
    interleave with a registrar's number allocation or another concurrent
    pass and clobber a just-written status.
    """
    stats = {"approved": 0, "declined": 0, "executed": 0, "failed": 0}
    with _state_lock(repo_root):
        state = load_state(repo_root)
        _expire(state)
        for expired in state["decisions"]:
            payload = expired.get("payload") or {}
            if (
                expired.get("kind") != "edc"
                or expired.get("status") != "expired"
                or payload.get("expiry_mirrored")
            ):
                continue
            try:
                from omniagentos.edc.approvals import expire_from_slack

                expire_from_slack(expired, notifier=notifier)
                payload["expiry_mirrored"] = True
            except Exception as exc:  # noqa: BLE001 — report and retry next scheduler pass
                stats["expiry_errors"] = stats.get("expiry_errors", 0) + 1
                print(
                    f"decisions: EDC expiry mirror failed for {expired.get('number')}: {exc}",
                    file=sys.stderr,
                )
        pending = {d["number"]: d for d in state["decisions"] if d["status"] == "pending"}
        if not pending:
            save_state(repo_root, state)
            return stats

        by_channel: dict[str, list[dict[str, Any]]] = {}
        for decision in pending.values():
            decision_channel = channel
            if decision.get("kind") == "edc":
                payload = decision.get("payload") or {}
                persisted = str(payload.get("dm_channel") or "")
                opener = getattr(notifier, "open_dm", None)
                reopened = (
                    opener(str(payload.get("owner_slack_id") or "")) if callable(opener) else None
                )
                decision_channel = str(reopened or persisted)
                if not decision_channel:
                    stats["history_errors"] = stats.get("history_errors", 0) + 1
                    print(
                        f"decisions: EDC {decision.get('number')} has no readable DM channel",
                        file=sys.stderr,
                    )
                    continue
                payload["dm_channel"] = decision_channel
            by_channel.setdefault(decision_channel, []).append(decision)

        roster = load_slack_map()
        for reply_channel, channel_decisions in by_channel.items():
            oldest = min(
                _parse_iso(str(item.get("proposed_at") or "")) for item in channel_decisions
            )
            try:
                messages = _slack_history(token, reply_channel, oldest)
            except SlackHistoryError as exc:
                stats["history_errors"] = stats.get("history_errors", 0) + 1
                print(f"decisions: history unavailable: {exc}", file=sys.stderr)
                continue
            channel_numbers = {int(item["number"]) for item in channel_decisions}
            replies = collect_replies(messages, roster)
            for number, approved, employee, kind_hint in replies:
                if number not in channel_numbers:
                    continue
                decision = pending.get(number)
                if decision is None or decision["status"] != "pending":
                    continue
                stored_kind = str(decision.get("kind") or "repair")
                if kind_hint is not None and kind_hint != stored_kind:
                    continue
                payload = decision.get("payload") or {}
                if stored_kind == "edc" and employee != payload.get("owner_employee_id"):
                    continue

                # Persist the one-shot before any effect. EDC then performs its
                # independent SQLite CAS, so a dashboard/Slack race has one winner.
                decision["status"] = "approved" if approved else "declined"
                decision["decided_by"] = employee
                decision["decided_at"] = _iso(_utcnow())
                save_state(repo_root, state)
                stats["approved" if approved else "declined"] += 1

                if stored_kind == "edc":
                    if dry_run:
                        print(json.dumps({"would_execute": number}, sort_keys=True))
                        continue
                    try:
                        from omniagentos.edc.approvals import resolve_from_slack

                        edc_result = resolve_from_slack(decision, approved=approved, actor=employee)
                        if edc_result is None:
                            decision["result"] = "already handled"
                            text = f"📨 edc {number}. already handled on another surface."
                        else:
                            decision["result"] = str(edc_result.get("status") or "resolved")
                            if approved:
                                stats["executed"] += 1
                            text = (
                                f"📨 edc {number}. "
                                f"{'approved and sent' if approved else 'declined'} by {employee}."
                            )
                        post_dm = getattr(notifier, "post_dm", None)
                        if callable(post_dm):
                            post_dm(str(payload.get("owner_slack_id") or ""), text)
                    except Exception as exc:  # noqa: BLE001 — EDC row carries recovery state
                        # F2: a non-secret type+reason label only — the raw
                        # exception message must never reach the JSON store or a DM.
                        from omniagentos.edc.actions import safe_error

                        label = safe_error(exc)
                        decision["status"] = "failed"
                        decision["result"] = label[:300]
                        stats["failed"] += 1
                        post_dm = getattr(notifier, "post_dm", None)
                        if callable(post_dm):
                            post_dm(
                                str(payload.get("owner_slack_id") or ""),
                                f"📨 edc {number}. action failed: {label[:140]}",
                            )
                    save_state(repo_root, state)
                    continue

                if not approved:
                    glyph = _KIND_GLYPHS.get(stored_kind, "🔧")
                    notifier.post_channel(
                        f"{glyph} {stored_kind} {number}. declined by {employee} — no action taken."
                    )
                    continue
                if dry_run:
                    print(json.dumps({"would_execute": number}, sort_keys=True))
                    continue
                if stored_kind == "overnight":
                    notifier.post_channel(
                        f"🌙 overnight {number}. confirmed by {employee} — reserved for "
                        "tonight's 22:00 run."
                    )
                    continue
                if stored_kind == "balancer":
                    remediation = str(
                        decision.get("payload", {}).get("remediation")
                        or decision.get("title")
                        or ""
                    )
                    decision["status"] = "executed"
                    decision["result"] = f"authorized: {remediation}"[:300]
                    stats["executed"] += 1
                    notifier.post_channel(
                        f"⚖️ balancer {number}. confirmed by {employee} — authorized: "
                        f"{remediation[:160]}. Carry out the pool action; this decision will not re-fire."
                    )
                    save_state(repo_root, state)
                    continue
                try:
                    repair_result = _file_repair_finding(repo_root, decision)
                    decision["status"] = "approved"
                    decision["result"] = repair_result
                    stats["executed"] += 1
                    notifier.post_channel(
                        f"🔧 repair {number}. confirmed by {employee} — filed into the "
                        f"loopqueue as `{repair_result[:23]}…`; the implementer loop takes it from here."
                    )
                except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
                    decision["status"] = "failed"
                    decision["result"] = str(exc)[:300]
                    stats["failed"] += 1
                    notifier.post_channel(
                        f"🔧 repair {number}. confirmed by {employee} but FILING FAILED: "
                        f"{str(exc)[:140]} — fix and re-propose; this decision will not re-fire."
                    )
                save_state(repo_root, state)
        save_state(repo_root, state)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root)
    load_slack_env()
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("decisions: no SLACK_BOT_TOKEN", file=sys.stderr)
        return 1
    channel = (
        os.environ.get("OMNI_TEAM_PULSE_CHANNEL")
        or os.environ.get("OMNI_TEAM_REPORT_CHANNEL")
        or DEFAULT_CHANNEL
    )
    notifier = SlackNotifier(token, channel=channel)
    stats = process_replies(
        repo_root, notifier=notifier, token=token, channel=channel, dry_run=args.dry_run
    )
    if not args.dry_run:
        from omniagentos.team.overnight import reserve_approved

        stats["reserved"] = reserve_approved(repo_root)
    print(json.dumps({"decisions": stats}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
