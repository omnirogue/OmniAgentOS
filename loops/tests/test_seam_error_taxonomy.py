"""Two ticks, one real socket, one real spend ledger: does the loop pay twice?

Everything else about the seam's error taxonomy is argued one layer at a time —
the parent classifies, the worker translates, the receipt guard acts. This file
runs the whole chain against a REAL ``EffectServer`` over a REAL unix socket
with a REAL ``LoopBudgetLedger``, fails the provider's HTTP call in a specific
way, and then counts the only two numbers that decide whether money was lost:

* how many times the provider was called, across TWO ticks;
* what state the idempotency claim and the budget reservation are left in.

On main every one of these failures mapped to ``SeamUnavailable``, which
RELEASES the claim, so tick 2 re-ran the identical paid call against a freed
reservation — indefinitely, because a released claim never consumes an attempt.
"""

from __future__ import annotations

import email.message
import socket
import ssl
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from omniagentos_loops import parent_seam, receipts
from omniagentos_loops.contracts import (
    EffectAttemptsExhausted,
    EffectStateUnknown,
    EffectUnavailable,
    RiskTier,
)
from omniagentos_loops.tools import LoopTool, execute_effect

from omniagentos.contracts import ActionClass
from omniagentos.scheduler.loop_budget import (
    STATE_RELEASED,
    STATE_SETTLED,
    LoopBudgetLedger,
)
from omniagentos.scheduler.loop_effects import EffectServer

INSTANCE = "render_probe"


class _Provider:
    """Stands in for ``urllib.request.urlopen`` and counts every request."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise self.failure


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://localhost:4000/v1/chat/completions",
        code=code,
        msg="synthetic",
        hdrs=email.message.Message(),
        fp=None,
    )


@pytest.fixture
def seam(db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A live parent seam whose provider always fails in a scripted way."""
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    ledger = LoopBudgetLedger(db_path, instance_caps={INSTANCE: 50.0})

    def factory(failure: BaseException) -> tuple[_Provider, LoopBudgetLedger]:
        provider = _Provider(failure)
        monkeypatch.setattr("urllib.request.urlopen", provider)
        server = EffectServer(db_path=db_path, budget_ledger=ledger)
        path = server.start()
        assert path, "the seam must bind, or every outcome below is vacuous"
        monkeypatch.setattr(parent_seam, "_SOCKET_PATH", path)
        started.append(server)
        return provider, ledger

    started: list[EffectServer] = []
    yield factory
    for server in started:
        server.stop()


def _tool(max_attempts: int | None = None) -> LoopTool:
    def implementation(**kwargs: Any) -> dict[str, Any]:
        return parent_seam.request_effect(INSTANCE, "model.complete", kwargs)

    return LoopTool(
        name="think",
        tier=RiskTier.T1,
        action_class=ActionClass.SANDBOXED_CREATION,
        idempotency_key=lambda args: "one-thought",
        call=implementation,
        max_attempts=max_attempts,
    )


_ARGS = {
    "messages": [{"role": "user", "content": "one word"}],
    "purpose": "taxonomy-probe",
}


def _tick(ctx: Any, tool: LoopTool) -> dict[str, Any]:
    return execute_effect(
        ctx, node="act", tool=tool, args=_ARGS, business_key="k", gate_token=None
    )


def _reservations(ledger: LoopBudgetLedger) -> list[Any]:
    rows = ledger._conn.execute(
        "SELECT id FROM loop_reservations ORDER BY created_at"
    ).fetchall()
    return [ledger.get_reservation(row["id"]) for row in rows]


# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", [429, 500])
def test_a_server_that_answered_is_adverse_and_is_not_called_again(
    code: int, seam, make_ctx
) -> None:
    """A 429 or a 500 came from a REACHED authority. Adverse; claim retained.

    ``max_attempts=1`` is the tool declaring "one shot per business key", which
    is what makes "the next tick does not re-call" literally true: attempt 1's
    row is a recorded FAILURE, so tick 2 finds the budget spent and escalates
    without reaching the provider. On main this was ``unavailable``, the row
    was DELETED, and tick 2 paid again.
    """
    provider, ledger = seam(_http_error(code))
    ctx = make_ctx(instance_id=INSTANCE, template="poll_classify_act_verify")
    tool = _tool(max_attempts=1)
    ctx.tools.register(tool)
    key = receipts.receipt_key(ctx.instance_id, ctx.template, "act", tool.name, "k")

    first = _tick(ctx, tool)
    assert first["succeeded"] is False
    assert receipts.receipt_state(ctx, key, 1) == receipts.FAILED

    with pytest.raises(EffectAttemptsExhausted):
        _tick(ctx, tool)

    assert provider.calls == 1, (
        f"the provider was called {provider.calls} times across two ticks; a "
        f"HTTP {code} must not release its claim"
    )
    assert [r.state for r in _reservations(ledger)] == [STATE_SETTLED]


def test_a_refusal_is_bounded_instead_of_repeating_forever(seam, make_ctx) -> None:
    """The retry budget is the bound, and a refusal has to consume it.

    With the default T1 budget a refusal is retried on later ticks — that is
    the shipped contract and it is fine, because it TERMINATES. The defect was
    that ``unavailable`` released the row, so attempt 1 was re-run every tick
    for ever: unbounded paid calls that never escalate to a human.
    """
    provider, _ = seam(_http_error(503))
    ctx = make_ctx(instance_id=INSTANCE, template="poll_classify_act_verify")
    tool = _tool(max_attempts=2)
    ctx.tools.register(tool)

    for _ in range(2):
        assert _tick(ctx, tool)["succeeded"] is False
    for _ in range(3):
        with pytest.raises(EffectAttemptsExhausted):
            _tick(ctx, tool)

    assert provider.calls == 2, (
        f"five ticks produced {provider.calls} provider calls against a budget "
        "of two; the refusal is not consuming its attempts"
    )


def test_a_read_timeout_leaves_the_claim_and_fails_closed_next_tick(
    seam, make_ctx
) -> None:
    """The one failure where the call may already have been made and BILLED.

    Nothing observable distinguishes "timed out before the server saw it" from
    "timed out after it generated 4,000 tokens", so the claim stays exactly as
    it would after a crash and the next tick refuses rather than paying again.
    """
    provider, ledger = seam(TimeoutError("timed out"))
    ctx = make_ctx(instance_id=INSTANCE, template="poll_classify_act_verify")
    tool = _tool()
    ctx.tools.register(tool)
    key = receipts.receipt_key(ctx.instance_id, ctx.template, "act", tool.name, "k")

    with pytest.raises(EffectStateUnknown):
        _tick(ctx, tool)
    assert receipts.receipt_state(ctx, key, 1) == receipts.CLAIMED

    with pytest.raises(EffectStateUnknown):
        _tick(ctx, tool)

    assert provider.calls == 1, (
        f"the provider was called {provider.calls} times; an ambiguous outcome "
        "must never be re-run"
    )
    assert [r.state for r in _reservations(ledger)] == [STATE_SETTLED], (
        "a call that may have been billed must keep its reservation charged"
    )


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
            id="connection-refused",
        ),
        pytest.param(
            urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided")),
            id="dns-failure",
        ),
        pytest.param(
            urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")),
            id="tls-handshake-refused",
        ),
    ],
)
def test_a_provably_unreached_provider_releases_the_claim_as_absence(
    failure: BaseException, seam, make_ctx
) -> None:
    """The ONLY shape that releases: nothing left this process, so nothing ran.

    Absence is neutral and loud, the claim is removed and the reservation is
    returned — all of which is safe precisely because it can be shown no
    request reached a provider.
    """
    provider, ledger = seam(failure)
    ctx = make_ctx(instance_id=INSTANCE, template="poll_classify_act_verify")
    tool = _tool()
    ctx.tools.register(tool)
    key = receipts.receipt_key(ctx.instance_id, ctx.template, "act", tool.name, "k")

    with pytest.raises(EffectUnavailable):
        _tick(ctx, tool)

    assert receipts.receipt_state(ctx, key, 1) == "absent"
    assert provider.calls == 1
    assert [r.state for r in _reservations(ledger)] == [STATE_RELEASED]

    # ... and because it is absence, the next tick legitimately retries the
    # SAME attempt slot rather than spending one.
    with pytest.raises(EffectUnavailable):
        _tick(ctx, tool)
    assert provider.calls == 2
    assert receipts.receipt_state(ctx, key, 1) == "absent", (
        "an absence must not consume an attempt slot — nothing failed"
    )
