"""AD-15 — finance-only approve-safe / escalate-hard.

Auto-approve ordinary work. Park money writes, customer writes, and production/
unresolved deletes. Permanently refuse bank writes. Secret reads auto-approve with a
truthful audit reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from omniagentos.orchestrator.approvals import (
    ESCALATION_DELIVERY_FAILED,
    ApprovalGateway,
    classify_hard_stop,
    resolve_approval,
)
from omniagentos.orchestrator.contracts import ApprovalRequest, HardStop


@dataclass
class _RecordingNotifier:
    calls: list[tuple[str, str]] = field(default_factory=list)

    def escalate(self, request: ApprovalRequest, category: HardStop) -> str | None:
        self.calls.append((category, request.proposed_action))
        return f"notif-{len(self.calls)}"


def test_plain_consequential_write_auto_approves() -> None:
    req = ApprovalRequest(
        "write hello.txt", "consequential", "Write", {"file_path": "/w/hello.txt"}
    )
    assert classify_hard_stop(req) is None
    decision = resolve_approval(req)
    assert decision.approved is True
    assert decision.escalated is False
    assert decision.category is None


def test_money_move_escalates() -> None:
    for action in ("transfer $500 to the vendor", "issue a refund via stripe", "wire the payment"):
        req = ApprovalRequest(action, "consequential")
        assert classify_hard_stop(req) == "money"


def test_file_deletion_escalates() -> None:
    req = ApprovalRequest("clean the build", "consequential", "Bash", {"command": "rm -rf build"})
    assert classify_hard_stop(req) == "delete"
    assert classify_hard_stop(ApprovalRequest("delete the file", "consequential")) == "delete"
    assert (
        classify_hard_stop(ApprovalRequest("shutil.rmtree(path)", "external_reversible"))
        == "delete"
    )


def test_read_only_label_alone_does_not_prove_a_money_read() -> None:
    # A stale class/prose label cannot substitute for GET/read operation structure.
    assert classify_hard_stop(ApprovalRequest("read the payment ledger", "read_only")) == "money"
    assert (
        classify_hard_stop(ApprovalRequest("summarise refunds", "internal_reversible")) == "money"
    )


def test_delete_takes_precedence_over_money() -> None:
    req = ApprovalRequest("rm the invoice payment file", "consequential")
    assert classify_hard_stop(req) == "delete"


def test_ordinary_words_do_not_false_trigger_a_hard_stop() -> None:
    """Regression: a signal must not fire as the SUFFIX of an unrelated word. A clock
    that writes CSS ``transform: rotate()`` (contains "rm") or a step described as
    "Confirm ..." (contains "rm") must run hands-off, not park on a phantom deletion.
    Word-boundary matching + not scanning free-text content is what guarantees this."""
    # CSS transform in a Write's CONTENT -> content is not scanned at all.
    assert (
        classify_hard_stop(
            ApprovalRequest(
                "Write",
                "irreversible",
                "Write",
                {"file_path": "/w/clock.html", "content": "el.style.transform='rotate(6deg)'"},
            )
        )
        is None
    )
    # A heredoc write carries the content in its COMMAND -> word-boundary saves it.
    assert (
        classify_hard_stop(
            ApprovalRequest(
                "Bash",
                "irreversible",
                "Bash",
                {"command": "cat > clock.html <<'EOF'\n.tick{transform:rotate(6deg)}\nEOF"},
            )
        )
        is None
    )
    # Ordinary prose in a proposed_action: perform/confirm/transform/platform.
    assert (
        classify_hard_stop(
            ApprovalRequest("Confirm and transform the platform", "consequential", "Task", {})
        )
        is None
    )


def test_delete_keyword_only_in_file_content_is_not_a_deletion() -> None:
    """The ACTION is writing a file; its content mentioning rm/delete is not a delete."""
    assert (
        classify_hard_stop(
            ApprovalRequest(
                "Write",
                "irreversible",
                "Write",
                {"file_path": "/w/todo.txt", "content": "later: rm -rf the cache, delete from db"},
            )
        )
        is None
    )


def test_real_deletes_still_escalate_with_word_boundaries() -> None:
    for cmd in ["rm -rf build", "unlink /w/f", "git rm secret", "rmdir /w/d"]:
        assert (
            classify_hard_stop(ApprovalRequest("Bash", "irreversible", "Bash", {"command": cmd}))
            == "delete"
        ), cmd


def test_gateway_records_and_escalates_via_notifier() -> None:
    notifier = _RecordingNotifier()
    gateway = ApprovalGateway(notifier=notifier)

    safe = gateway.resolve(
        ApprovalRequest("edit config", "consequential", "Edit", {"file_path": "/w/c"})
    )
    money = gateway.resolve(ApprovalRequest("send money to acct", "consequential"))
    delete = gateway.resolve(
        ApprovalRequest(
            "rm -rf node_modules", "consequential", "Bash", {"command": "rm -rf node_modules"}
        )
    )

    assert safe.approved and not safe.escalated
    assert money.escalated and money.category == "money" and not money.approved
    assert delete.escalated and delete.category == "delete"
    # Only the two hard stops were escalated to the operator.
    assert [c for c, _ in notifier.calls] == ["money", "delete"]
    assert money.notification_id == "notif-1"
    assert delete.notification_id == "notif-2"
    # Every decision is recorded for the run report.
    assert len(gateway.decisions) == 3
    assert sum(1 for d in gateway.decisions if d.escalated) == 2


def test_resolve_approval_never_raises_when_notifier_fails() -> None:
    class _Boom:
        def escalate(self, request: ApprovalRequest, category: Any) -> str | None:
            raise RuntimeError("delivery down")

    decision = resolve_approval(ApprovalRequest("wire transfer", "consequential"), _Boom())
    assert decision.escalated is True
    assert decision.category == "money"
    # LSC-04: this used to assert ``is None``, which is also what "no notifier was
    # supplied" reports -- so a dropped operator page was indistinguishable from
    # the by-design path that api/routes/sessions.py takes. A delivery failure now
    # says so.
    assert decision.notification_id == ESCALATION_DELIVERY_FAILED
