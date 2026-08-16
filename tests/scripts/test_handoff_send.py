"""``scripts/handoff_send.py`` — the handoff delivery loop.

The bug this file exists to prevent: a handoff doc that is WRITTEN but never
DELIVERED (two Alice docs sat unread for a day, 2026-08-14), and its sibling —
a doc delivered once, then edited, so the recipient holds a stale copy.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "handoff_send.py"

_spec = importlib.util.spec_from_file_location("handoff_send", SCRIPT)
assert _spec and _spec.loader
handoff_send = importlib.util.module_from_spec(_spec)
sys.modules["handoff_send"] = handoff_send
_spec.loader.exec_module(handoff_send)


DOC = """# Alice — Handoff (2026-08-13): the thing

Written by the operator's session. Verified against the live repo; nothing speculative.

More lede that belongs to a later paragraph and must not be captured.

## 1. First section

body

## 2. Second section

body
"""


@pytest.fixture()
def slack_map(tmp_path: Path) -> Path:
    path = tmp_path / "team_slack_map.yaml"
    path.write_text("U111ALICE: emp_alice\nU222TEAM: emp_owner\n", encoding="utf-8")
    return path


@pytest.fixture()
def doc_path(tmp_path: Path) -> Path:
    path = tmp_path / "alice-handoff-2026-08-13.md"
    path.write_text(DOC, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_parse_doc_extracts_title_lede_and_sections(doc_path: Path) -> None:
    doc = handoff_send.parse_doc(doc_path)
    assert doc.title == "Alice — Handoff (2026-08-13): the thing"
    assert doc.lede.startswith("Written by the operator's session.")
    # Only the FIRST paragraph is the lede — the second must not leak in.
    assert "later paragraph" not in doc.lede
    assert doc.sections == ("1. First section", "2. Second section")


def test_parse_doc_without_h1_falls_back_to_filename(tmp_path: Path) -> None:
    path = tmp_path / "alice-bare.md"
    path.write_text("no heading here\n", encoding="utf-8")
    assert handoff_send.parse_doc(path).title == "alice-bare"


def test_missing_doc_is_a_caller_error(tmp_path: Path) -> None:
    with pytest.raises(handoff_send.HandoffError):
        handoff_send.parse_doc(tmp_path / "nope.md")


# --------------------------------------------------------------------------
# recipient resolution
# --------------------------------------------------------------------------


def test_recipient_comes_from_the_filename(doc_path: Path) -> None:
    assert handoff_send.infer_recipient_token(doc_path) == "alice"


def test_filename_wins_over_the_heading(tmp_path: Path) -> None:
    """A doc ABOUT someone must not be routed TO them: bob's queue doc
    quotes "# Alice — …" internally, and routing on the heading would misdeliver."""
    path = tmp_path / "bob-queue.md"
    path.write_text("# Alice — notes about Alice\n\nlede\n", encoding="utf-8")
    doc = handoff_send.parse_doc(path)
    assert handoff_send.infer_recipient_token(path, doc) == "bob"


def test_roster_and_resolution_accept_handle_forms(slack_map: Path) -> None:
    people = handoff_send.roster(slack_map)
    assert people["alice"].slack_user_id == "U111ALICE"
    for form in ("alice", "Alice", "@alice", "emp_alice"):
        assert handoff_send.resolve_recipient(form, people).employee_id == "emp_alice"


def test_unknown_recipient_names_the_known_set(slack_map: Path) -> None:
    people = handoff_send.roster(slack_map)
    with pytest.raises(handoff_send.HandoffError) as excinfo:
        handoff_send.resolve_recipient("nobody", people)
    assert "alice" in str(excinfo.value)


def test_missing_recipient_is_an_error_not_a_broadcast(slack_map: Path) -> None:
    with pytest.raises(handoff_send.HandoffError):
        handoff_send.resolve_recipient(None, handoff_send.roster(slack_map))


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def test_summary_is_deterministic_and_points_at_the_doc(doc_path: Path, slack_map: Path) -> None:
    doc = handoff_send.parse_doc(doc_path)
    alice = handoff_send.roster(slack_map)["alice"]
    first = handoff_send.render_message(doc, alice)
    assert first == handoff_send.render_message(doc, alice)
    assert "Hi Alice" in first
    # The name prefix is stripped from the title — the greeting already says it.
    assert "*Handoff (2026-08-13): the thing*" in first
    assert str(doc_path) in first or doc.rel_path in first
    assert "• 1. First section" in first


def test_full_mode_truncates_rather_than_shipping_a_wall(tmp_path: Path, slack_map: Path) -> None:
    path = tmp_path / "alice-long.md"
    path.write_text(
        "# Alice — long\n\n" + ("x" * (handoff_send.MAX_FULL_CHARS * 2)), encoding="utf-8"
    )
    doc = handoff_send.parse_doc(path)
    alice = handoff_send.roster(slack_map)["alice"]
    text = handoff_send.render_message(doc, alice, full=True)
    assert "truncated" in text
    assert len(text) < handoff_send.MAX_FULL_CHARS * 2


def test_a_key_shape_in_the_doc_does_not_egress(doc_path: Path, slack_map: Path) -> None:
    """Handoff docs quote live config. The notifier scrubs on the way out and
    this asserts the delivery path actually goes through that scrub."""
    from omniagentos.team.notify import SlackNotifier

    doc_path.write_text(
        "# Alice — leaky\n\nRan against the MCP, key `apikey:mlk_c1ae2980abcdef`.\n",
        encoding="utf-8",
    )
    doc = handoff_send.parse_doc(doc_path)
    alice = handoff_send.roster(slack_map)["alice"]
    text = handoff_send.render_message(doc, alice)
    assert "mlk_c1ae2980abcdef" in text  # present before egress …
    payload = SlackNotifier._payload("D1", text, None, None)
    assert "mlk_c1ae2980abcdef" not in payload["text"]  # … scrubbed at egress
    assert "[token omitted]" in payload["text"]


# --------------------------------------------------------------------------
# the delivery ledger — the actual point of the tool
# --------------------------------------------------------------------------


def test_delivery_is_recorded_and_then_reads_as_delivered(
    doc_path: Path, slack_map: Path, tmp_path: Path
) -> None:
    ledger = tmp_path / ".delivered.jsonl"
    doc = handoff_send.parse_doc(doc_path)
    alice = handoff_send.roster(slack_map)["alice"]

    assert handoff_send.delivery_state(doc, handoff_send.read_ledger(ledger))[0] == "undelivered"
    handoff_send.record_delivery(
        doc, alice, mode="summary", ledger_path=ledger, now=datetime(2026, 8, 14, tzinfo=UTC)
    )
    state, record = handoff_send.delivery_state(doc, handoff_send.read_ledger(ledger))
    assert state == "delivered"
    assert record is not None and record["recipient"] == "emp_alice"
    assert record["ts"].startswith("2026-08-14")


def test_editing_a_delivered_doc_makes_it_stale(
    doc_path: Path, slack_map: Path, tmp_path: Path
) -> None:
    """A delivered-then-edited doc is as undelivered as one never sent."""
    ledger = tmp_path / ".delivered.jsonl"
    alice = handoff_send.roster(slack_map)["alice"]
    handoff_send.record_delivery(
        handoff_send.parse_doc(doc_path), alice, mode="summary", ledger_path=ledger
    )
    doc_path.write_text(DOC + "\n## 3. Added later\n\nnew work\n", encoding="utf-8")
    edited = handoff_send.parse_doc(doc_path)
    assert handoff_send.delivery_state(edited, handoff_send.read_ledger(ledger))[0] == "stale"


def test_a_corrupt_ledger_line_is_skipped_not_fatal(tmp_path: Path) -> None:
    ledger = tmp_path / ".delivered.jsonl"
    ledger.write_text('{"path": "a.md"}\nnot json\n\n{"path": "b.md"}\n', encoding="utf-8")
    assert [record["path"] for record in handoff_send.read_ledger(ledger)] == ["a.md", "b.md"]


def test_delivery_to_one_recipient_does_not_suppress_a_send_to_another(
    doc_path: Path, slack_map: Path, tmp_path: Path
) -> None:
    """Regression for delivery-scope-blindness: delivery_state matched only on
    (path, sha256), so a doc delivered to Alice read as "delivered" for a
    re-run addressed to Bob — Bob would never receive it."""
    ledger = tmp_path / ".delivered.jsonl"
    doc = handoff_send.parse_doc(doc_path)
    alice = handoff_send.roster(slack_map)["alice"]
    handoff_send.record_delivery(doc, alice, mode="summary", ledger_path=ledger)

    # Still unscoped-delivered (any recipient) — legacy/`--list` style check.
    assert handoff_send.delivery_state(doc, handoff_send.read_ledger(ledger))[0] == "delivered"

    # But scoped to a DIFFERENT recipient, it must read as undelivered.
    state, record = handoff_send.delivery_state(
        doc, handoff_send.read_ledger(ledger), recipient="emp_bob"
    )
    assert state == "undelivered"
    assert record is None

    # And scoped to the recipient it actually went to, still delivered.
    state, record = handoff_send.delivery_state(
        doc, handoff_send.read_ledger(ledger), recipient="emp_alice"
    )
    assert state == "delivered"
    assert record is not None and record["recipient"] == "emp_alice"


def test_summary_delivery_does_not_suppress_a_later_full_send(
    doc_path: Path, slack_map: Path, tmp_path: Path
) -> None:
    """Regression for delivery-scope-blindness: a doc delivered as a summary
    read as "delivered" for a later ``--full`` request — the full body would
    never go out. "full" satisfies an ask for "summary"; the reverse must not."""
    ledger = tmp_path / ".delivered.jsonl"
    doc = handoff_send.parse_doc(doc_path)
    alice = handoff_send.roster(slack_map)["alice"]
    handoff_send.record_delivery(doc, alice, mode="summary", ledger_path=ledger)

    # Scoped to mode="full", the prior summary send must not count.
    state, _ = handoff_send.delivery_state(
        doc, handoff_send.read_ledger(ledger), recipient="emp_alice", mode="full"
    )
    assert state == "undelivered"

    # Scoped to mode="summary" (equal), it still reads as delivered.
    state, _ = handoff_send.delivery_state(
        doc, handoff_send.read_ledger(ledger), recipient="emp_alice", mode="summary"
    )
    assert state == "delivered"

    # A full delivery satisfies a LATER ask for summary — fuller covers lesser.
    handoff_send.record_delivery(doc, alice, mode="full", ledger_path=ledger)
    state, record = handoff_send.delivery_state(
        doc, handoff_send.read_ledger(ledger), recipient="emp_alice", mode="summary"
    )
    assert state == "delivered"
    assert record is not None and record["mode"] == "full"


def test_legacy_ledger_record_without_recipient_or_mode_matches_its_own_default(
    doc_path: Path,
) -> None:
    """Records written before recipient/mode scoping existed lack those keys.
    They must be read as targeting the doc's OWN inferred recipient in
    "summary" mode — not as matching every recipient, and not as matching
    none (which would resend the whole ledger once)."""
    doc = handoff_send.parse_doc(doc_path)  # alice-handoff-2026-08-13.md → "alice"
    legacy_records = [{"path": doc.rel_path, "sha256": doc.sha256}]

    # Matches the doc's real default recipient (alice) in summary mode.
    state, record = handoff_send.delivery_state(
        doc, legacy_records, recipient="emp_alice", mode="summary"
    )
    assert state == "delivered"
    assert record is legacy_records[0]

    # Does NOT match a different recipient.
    state, _ = handoff_send.delivery_state(
        doc, legacy_records, recipient="emp_bob", mode="summary"
    )
    assert state == "undelivered"

    # Does NOT satisfy a "full" ask (legacy sends were summary-only).
    state, _ = handoff_send.delivery_state(
        doc, legacy_records, recipient="emp_alice", mode="full"
    )
    assert state == "undelivered"


def test_cli_resends_to_a_second_recipient_after_the_first_was_delivered(
    doc_path: Path, slack_map: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end regression: after Alice received the doc, re-running the CLI
    with --to bob must actually attempt a send, not report "already
    delivered" and exit 0 without ever reaching Bob."""
    ledger = tmp_path / ".delivered.jsonl"
    slack_map_with_bob = tmp_path / "team_slack_map2.yaml"
    slack_map_with_bob.write_text(
        "U111ALICE: emp_alice\nU333BOB: emp_bob\n", encoding="utf-8"
    )
    monkeypatch.setattr(handoff_send, "LEDGER_PATH", ledger)
    real_roster = handoff_send.roster
    real_read_ledger = handoff_send.read_ledger
    monkeypatch.setattr(handoff_send, "roster", lambda *a, **k: real_roster(slack_map_with_bob))
    monkeypatch.setattr(handoff_send, "read_ledger", lambda *a, **k: real_read_ledger(ledger))

    doc = handoff_send.parse_doc(doc_path)
    alice = real_roster(slack_map_with_bob)["alice"]
    handoff_send.record_delivery(doc, alice, mode="summary", ledger_path=ledger)

    sent: list[str] = []
    monkeypatch.setattr(
        handoff_send, "_send", lambda text, recipient: (sent.append(recipient.employee_id), (True, None))[1]
    )
    monkeypatch.setattr(handoff_send, "record_delivery", lambda *a, **k: None)

    rc = handoff_send.main([str(doc_path), "--to", "bob"])
    assert rc == 0
    assert sent == ["emp_bob"], "the second recipient must actually be sent to"


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------


def test_scan_lists_only_addressed_and_undelivered_docs(tmp_path: Path, slack_map: Path) -> None:
    handoff_dir = tmp_path / "HANDOFF"
    handoff_dir.mkdir()
    (handoff_dir / "alice-one.md").write_text("# Alice — one\n\nlede\n", encoding="utf-8")
    (handoff_dir / "owner-two.md").write_text("# the operator — two\n\nlede\n", encoding="utf-8")
    # Not addressed to anyone on the roster — must not be flagged as pending.
    (handoff_dir / "ROADMAP.md").write_text("# Roadmap\n\nlede\n", encoding="utf-8")
    ledger = tmp_path / ".delivered.jsonl"

    pending = handoff_send.scan_pending(handoff_dir, ledger, slack_map)
    assert {doc.path.name for doc, _, _ in pending} == {"alice-one.md", "owner-two.md"}
    assert all(state == "undelivered" for _, _, state in pending)

    delivered = handoff_send.parse_doc(handoff_dir / "alice-one.md")
    handoff_send.record_delivery(
        delivered, handoff_send.roster(slack_map)["alice"], mode="summary", ledger_path=ledger
    )
    remaining = handoff_send.scan_pending(handoff_dir, ledger, slack_map)
    assert {doc.path.name for doc, _, _ in remaining} == {"owner-two.md"}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_dry_run_sends_nothing_and_records_nothing(
    doc_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _explode(*args: object, **kwargs: object) -> None:  # pragma: no cover - must not run
        raise AssertionError("--dry-run must not touch Slack")

    monkeypatch.setattr(handoff_send, "_send", _explode)
    monkeypatch.setattr(handoff_send, "record_delivery", _explode)
    assert handoff_send.main([str(doc_path), "--dry-run"]) == 0
    assert "would DM Alice" in capsys.readouterr().out


def test_a_failed_send_is_not_recorded_as_delivered(
    doc_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording a delivery that did not happen is the one unrecoverable bug
    here — the doc would then never appear in --list again."""
    monkeypatch.setattr(handoff_send, "_send", lambda text, recipient: (False, "channel_not_found"))
    monkeypatch.setattr(
        handoff_send,
        "record_delivery",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not record a failed send")),
    )
    assert handoff_send.main([str(doc_path)]) == 1


def test_unknown_recipient_exits_two_without_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "nobody-thing.md"
    path.write_text("# Nobody — thing\n\nlede\n", encoding="utf-8")
    monkeypatch.setattr(
        handoff_send, "_send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no send"))
    )
    assert handoff_send.main([str(path)]) == 2
