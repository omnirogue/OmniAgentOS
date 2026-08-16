"""``model.complete``: which transport failure is absence, and who keeps the money.

``ShortCallClient`` raises ONE exception type, ``LLMTransportError``, for three
events that are not alike:

* HTTP 429 and 5xx — the server ANSWERED. A reached authority.
* connect refused / DNS failure / a certificate this process refused — nothing
  reached a provider.
* a read timeout after the request was written — the call may have completed
  and been BILLED, and nothing observable can say.

Until this lane, all three mapped to ``SeamUnavailable``. ``unavailable`` is
ABSENCE, and absence is the one outcome that RELEASES the worker's idempotency
claim (``receipts._attempt`` -> ``store.idem_release``), so a rate-limited or
timed-out paid model call erased its own receipt and the next tick paid again.

The bar for every test here is "would this have stopped a double charge",
which is why the assertions are about the CLIENT CALL COUNT and the
RESERVATION STATE, not about the string in a reason field.
"""

from __future__ import annotations

import email.message
import socket
import ssl
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.scheduler import loop_effects
from omniagentos.scheduler.loop_budget import (
    STATE_RELEASED,
    STATE_SETTLED,
    LoopBudgetLedger,
)
from omniagentos.scheduler.loop_effects import (
    OUTCOME_REFUSED,
    OUTCOME_UNAVAILABLE,
    OUTCOME_UNKNOWN,
    SeamRefused,
    SeamUnavailable,
    SeamUnknown,
    execute,
)
from tests.support.db_template import migrated_db


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "control.sqlite3")
    return migrated_db(SqliteStore, path)


@pytest.fixture
def isolated_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep ``BudgetGuard``'s short-call ledger out of the real ``var/``."""
    var = tmp_path / "var"
    var.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var))
    return var


@pytest.fixture
def ledger(db_path: str) -> LoopBudgetLedger:
    return LoopBudgetLedger(db_path, instance_caps={"render_probe": 50.0})


def _model_request() -> dict[str, Any]:
    return {
        "v": 1,
        "instance": "render_probe",
        "capability": "model.complete",
        "args": {
            "messages": [{"role": "user", "content": "one word"}],
            "purpose": "taxonomy-probe",
        },
    }


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://localhost:4000/v1/chat/completions",
        code=code,
        msg="synthetic",
        hdrs=email.message.Message(),
        fp=None,
    )


class _Urlopen:
    """A stand-in for ``urllib.request.urlopen`` that counts every call."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise self.failure


def _run(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    *,
    db_path: str,
    ledger: LoopBudgetLedger,
) -> tuple[dict[str, Any], _Urlopen]:
    fake = _Urlopen(failure)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    answer = execute(_model_request(), db_path=db_path, budget_ledger=ledger)
    return answer, fake


def _only_reservation(ledger: LoopBudgetLedger) -> Any:
    rows = ledger._conn.execute("SELECT * FROM loop_reservations").fetchall()
    assert len(rows) == 1, f"expected exactly one reservation, got {len(rows)}"
    return ledger.get_reservation(rows[0]["id"])


# --------------------------------------------------------------------------
# The mapping itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", [429, 500, 502, 503, 599])
def test_a_server_that_answered_is_a_refusal_not_absence(
    code: int,
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 and every 5xx: the authority was REACHED and said no. Adverse."""
    answer, _ = _run(monkeypatch, _http_error(code), db_path=db_path, ledger=ledger)

    assert answer["outcome"] == OUTCOME_REFUSED, (
        f"HTTP {code} came from a server that answered; calling it "
        f"{answer['outcome']!r} releases the idempotency claim and the next "
        "tick re-issues a paid call"
    )


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(TimeoutError("timed out"), id="bare-read-timeout"),
        pytest.param(urllib.error.URLError(TimeoutError("timed out")), id="wrapped-timeout"),
        pytest.param(
            urllib.error.URLError(ConnectionResetError(54, "reset")), id="reset-mid-stream"
        ),
        pytest.param(urllib.error.URLError(ssl.SSLEOFError("eof")), id="ssl-eof"),
        pytest.param(BrokenPipeError(32, "broken pipe"), id="broken-pipe"),
    ],
)
def test_an_ambiguous_transport_failure_fails_closed_as_unknown(
    failure: BaseException,
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request may have been received and BILLED. Never absence.

    ``TimeoutError`` is the load-bearing one: urllib raises the same exception
    for a timeout while connecting (really absence) and a timeout while reading
    the reply (possibly a completed, billed call), so the two cannot be told
    apart and the ambiguous reading is the only safe one.
    """
    answer, _ = _run(monkeypatch, failure, db_path=db_path, ledger=ledger)

    assert answer["outcome"] == OUTCOME_UNKNOWN, (
        f"{type(failure).__name__} cannot be shown to have missed the provider; "
        f"{answer['outcome']!r} would release the claim for a call that may have run"
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
def test_only_a_provable_never_reached_failure_is_absence(
    failure: BaseException,
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused connection, an unresolvable name, a refused certificate.

    These are raisable only BEFORE the first request byte leaves this process,
    which is exactly the proof ``unavailable`` requires: the claim is released
    and the next tick re-runs, and that is safe only because nothing ran.
    """
    answer, _ = _run(monkeypatch, failure, db_path=db_path, ledger=ledger)

    assert answer["outcome"] == OUTCOME_UNAVAILABLE


def test_the_seam_does_not_let_the_client_re_send_a_possibly_billed_request(
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One seam call is one provider request. Never two.

    ``ShortCallClient._execute_with_retry`` retries once on any
    ``LLMTransportError``, which includes the read timeout — the one failure
    where the first request may already have been received and billed. Behind
    an idempotency claim that blind re-send pays twice inside a single attempt,
    where no receipt and no reservation can undo it.
    """
    _, fake = _run(monkeypatch, TimeoutError("timed out"), db_path=db_path, ledger=ledger)
    assert fake.calls == 1, (
        f"the client issued {fake.calls} provider requests for one seam call; "
        "a read timeout must not be re-sent blind"
    )

    _, fake_429 = _run(monkeypatch, _http_error(429), db_path=db_path, ledger=ledger)
    assert fake_429.calls == 1


def test_a_client_outside_the_seam_keeps_its_retry(
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opt-out is the seam's, not a global behaviour change.

    Every other caller of ``ShortCallClient`` is unclaimed and unbilled-by-us
    work where retrying a 5xx is the right thing; only the seam turns it off.
    """
    from omniagentos.llm.budget import LLMTransportError
    from omniagentos.llm.client import ShortCallClient

    fake = _Urlopen(_http_error(503))
    monkeypatch.setattr("urllib.request.urlopen", fake)
    with pytest.raises(LLMTransportError):
        ShortCallClient().complete([{"role": "user", "content": "hi"}])
    assert fake.calls == 2


# --------------------------------------------------------------------------
# Who keeps the money
# --------------------------------------------------------------------------


def test_a_reached_authority_keeps_the_reservation_charged(
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 may have been billed, so its reservation settles, never releases.

    Releasing here is the same fail-open shape as an expired reservation
    counting as zero: money that may have been spent stops counting against
    the cap, and the loop gets to spend it again.
    """
    _run(monkeypatch, _http_error(500), db_path=db_path, ledger=ledger)

    reservation = _only_reservation(ledger)
    assert reservation.state == STATE_SETTLED
    assert reservation.actual_usd == pytest.approx(reservation.max_usd)
    assert reservation.cost_quality == "unknown"


def test_an_ambiguous_failure_keeps_the_reservation_charged(
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run(monkeypatch, TimeoutError("timed out"), db_path=db_path, ledger=ledger)

    reservation = _only_reservation(ledger)
    assert reservation.state == STATE_SETTLED
    assert reservation.actual_usd == pytest.approx(reservation.max_usd)


def test_a_provable_absence_gives_the_reservation_back(
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing reached a provider, so nothing was billed. The hold is freed."""
    _run(
        monkeypatch,
        urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")),
        db_path=db_path,
        ledger=ledger,
    )

    reservation = _only_reservation(ledger)
    assert reservation.state == STATE_RELEASED
    assert reservation.actual_usd == 0.0


def test_a_handler_crash_charges_rather_than_refunds(
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unclassified crash establishes nothing about whether the provider ran."""

    import dataclasses

    def _boom(request: Any) -> dict[str, Any]:
        raise RuntimeError("handler exploded after the call went out")

    monkeypatch.setitem(
        loop_effects.CAPABILITIES,
        "model.complete",
        dataclasses.replace(loop_effects.CAPABILITIES["model.complete"], run=_boom),
    )
    answer = execute(_model_request(), db_path=db_path, budget_ledger=ledger)

    assert answer["outcome"] == OUTCOME_UNKNOWN
    assert _only_reservation(ledger).state == STATE_SETTLED


def test_a_locally_decided_refusal_is_provably_unbilled(
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spend cap says no before the request is built; that money is not spent.

    ``may_have_billed`` is a claim about the PROVIDER, not about whether the
    call succeeded, so the raiser that can prove it says so and gets the hold
    back.
    """
    from omniagentos.llm.budget import LLMBudgetExceededError

    def _capped(*args: Any, **kwargs: Any) -> None:
        raise LLMBudgetExceededError("daily cap reached")

    monkeypatch.setattr("omniagentos.llm.budget.BudgetGuard.check_budget", _capped)
    answer = execute(_model_request(), db_path=db_path, budget_ledger=ledger)

    assert answer["outcome"] == OUTCOME_REFUSED
    assert answer["reason"] == "budget_exceeded"
    assert _only_reservation(ledger).state == STATE_RELEASED


# --------------------------------------------------------------------------
# Absence is a claim about the effect, not about the last request
# --------------------------------------------------------------------------


def _failing_download(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException, destination: Path
) -> BaseException:
    import httpx

    def _refuse(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(httpx.Client, "stream", _refuse)
    with pytest.raises(Exception) as raised:  # noqa: PT011 — the TYPE is the assertion
        loop_effects._download_artifact("https://replicate.delivery/x/rose.png", destination)
    return raised.value


def test_a_failed_artifact_fetch_is_never_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prediction is already created, polled and BILLED by the time we fetch.

    ``_from_transport`` calls a refused connection absence, and for the
    prediction POST that is right. For the artifact download it is not: the
    money is already gone, so releasing the claim sends the next tick back to
    the top of ``_run_replicate_generate`` to create — and pay for — a second
    prediction. The proof "no request reached an authority" is true here and
    about the wrong request.
    """
    import httpx

    for index, failure in enumerate(
        (
            httpx.ConnectError("connection refused"),
            httpx.ConnectTimeout("connect timed out"),
            httpx.ProxyError("proxy refused"),
        )
    ):
        raised = _failing_download(monkeypatch, failure, tmp_path / f"a{index}.png")
        assert not isinstance(raised, SeamUnavailable), (
            f"{type(failure).__name__} while downloading a paid artifact was "
            "classified as absence; the next tick would buy a second prediction"
        )
        assert isinstance(raised, SeamRefused)


def test_the_downgrade_does_not_launder_ambiguity_into_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only absence is downgraded. An unknown is still an unknown."""
    import httpx

    raised = _failing_download(
        monkeypatch, httpx.ReadTimeout("read timed out"), tmp_path / "rose.png"
    )
    assert isinstance(raised, SeamUnknown)


# --------------------------------------------------------------------------
# The same rule, one request earlier: POLLING a prediction we already bought
# --------------------------------------------------------------------------
#
# ``_download_artifact`` was the instance of this bug we found by auditing the
# artifact fetch. It is not the only request ``replicate.generate`` issues after
# the money is committed. The creation POST returning 201 is the moment the
# spend happens — Replicate is running the job from that point, whatever we do
# next — and EVERY request after it (the poll GETs, then the download) is a read
# about work already paid for. A transport failure on any of them proves only
# that THIS read missed.


def _render_request() -> dict[str, Any]:
    return {
        "v": 1,
        "instance": "render_probe",
        "capability": "replicate.generate",
        "args": {
            "model": "black-forest-labs/flux-schnell",
            "prompt": "one rose, studio light",
            "artifact_name": "rose.png",
        },
    }


def _transport_denial(cause: BaseException) -> BaseException:
    """The exact object ``broker.call`` raises for an httpx failure.

    ``broker.py:774`` does ``raise BrokerDenied("transport_error", cap_id,
    str(exc)) from exc``, and ``_from_broker_denial`` recovers the phase from
    ``__cause__``. Setting ``__cause__`` here is what ``raise ... from`` does.
    """
    from omniagentos.connectors.broker import BrokerDenied

    denial = BrokerDenied("transport_error", loop_effects.REPLICATE_GENERATE, str(cause))
    denial.__cause__ = cause
    return denial


def _assert_grant_backed(granted: Any, kwargs: dict[str, Any]) -> None:
    """Pin U-R10's shape on every broker stand-in in this file.

    These doubles stand in for ``broker.call``. A double with a looser contract
    than the real function is how a caller regression hides, so the seam's
    inability to vouch for itself is asserted HERE too: production must pass no
    capability list and must instead name a holder the broker looks up itself.
    """
    assert granted is None, "the loop seam must not supply its own capability list"
    assert kwargs.get("grant_holder"), "the loop seam must name a grant holder"
    assert kwargs.get("grant_store") is not None, "the holder must be resolved against a store"


class _Replicate:
    """Broker stand-in: the creation POST is BILLED, then a read fails.

    ``ok=True`` with ``status="processing"`` is the real 201 body — the
    prediction now exists on Replicate's side and is being rendered on their
    hardware. Nothing this process does afterwards makes that unpaid.
    """

    def __init__(self, read_failure: BaseException) -> None:
        self.read_failure = read_failure
        self.posts = 0
        self.polls = 0

    def __call__(self, cap_id: str, granted: Any = None, **kwargs: Any) -> dict[str, Any]:
        _assert_grant_backed(granted, kwargs)
        if str(kwargs.get("method") or "").upper() == "POST":
            self.posts += 1
            return {
                "capability": cap_id,
                "status": 201,
                "ok": True,
                "body": {"id": "pred_already_paid_for", "status": "processing"},
            }
        self.polls += 1
        raise self.read_failure


def _poll_fails_with(monkeypatch: pytest.MonkeyPatch, read_failure: BaseException) -> _Replicate:
    fake = _Replicate(read_failure)
    monkeypatch.setattr("omniagentos.connectors.broker.call", fake)
    # The poll loop sleeps 2s between reads; the test is about classification.
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    return fake


@pytest.mark.parametrize(
    "failure_name",
    ["ConnectError", "ConnectTimeout", "ProxyError"],
)
def test_a_failed_prediction_poll_is_never_absence(
    failure_name: str,
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The POST succeeded. The money is gone. A dropped poll is not "nothing happened".

    This is the artifact-fetch defect one request earlier, and it is reachable
    by an ordinary network transient rather than by anything exotic: create the
    prediction (201, billed), then have the very first polling GET hit a refused
    connection. ``_from_transport`` correctly observes that this GET never
    reached Replicate and returns ``SeamUnavailable`` — a true proof about the
    wrong request. ``unavailable`` RELEASES the worker's idempotency claim and
    RELEASES the budget reservation, so the next tick re-enters
    ``_run_replicate_generate`` at the top and buys a second prediction.
    """
    import httpx

    fake = _poll_fails_with(
        monkeypatch, _transport_denial(getattr(httpx, failure_name)("connection refused"))
    )
    answer = execute(_render_request(), db_path=db_path, budget_ledger=ledger)

    assert fake.posts == 1, "the prediction must actually have been created (and billed)"
    assert fake.polls == 1, "the failure must be on a POLL, not on the creation POST"
    assert answer["outcome"] != OUTCOME_UNAVAILABLE, (
        f"{failure_name} while polling a prediction we already paid for was "
        f"classified as absence ({answer['reason']}); the claim is released and "
        "the next tick would buy a second prediction"
    )
    assert answer["outcome"] == OUTCOME_REFUSED

    reservation = _only_reservation(ledger)
    assert reservation.state != STATE_RELEASED, (
        "the reservation for a prediction Replicate is already rendering was "
        "refunded; that money is free to be spent a second time"
    )
    assert reservation.state == STATE_SETTLED


def test_a_credential_that_vanishes_mid_prediction_is_not_absence(
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other absence-producing denial on the poll path, not just the transport one.

    ``_UNREACHED_DENIALS`` maps ``credential_missing`` to ``SeamUnavailable``,
    and before the creation POST that is exactly right — no socket was opened.
    Read AFTER the POST (a rotated or revoked token, a keychain that stopped
    answering) the same denial proves only that we can no longer ASK about a
    prediction we have already bought.
    """
    from omniagentos.connectors.broker import BrokerDenied

    fake = _poll_fails_with(
        monkeypatch,
        BrokerDenied("credential_missing", loop_effects.REPLICATE_GENERATE, "token rotated"),
    )
    answer = execute(_render_request(), db_path=db_path, budget_ledger=ledger)

    assert fake.posts == 1
    assert answer["outcome"] != OUTCOME_UNAVAILABLE, (
        "a credential that vanished AFTER the prediction was created was "
        "classified as absence; the next tick would buy a second prediction"
    )
    assert _only_reservation(ledger).state != STATE_RELEASED


def test_an_ambiguous_poll_failure_is_still_only_ambiguous(
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The downgrade converts absence, and nothing else. A read timeout stays UNKNOWN."""
    import httpx

    _poll_fails_with(monkeypatch, _transport_denial(httpx.ReadTimeout("read timed out")))
    answer = execute(_render_request(), db_path=db_path, budget_ledger=ledger)

    assert answer["outcome"] == OUTCOME_UNKNOWN
    assert _only_reservation(ledger).state == STATE_SETTLED


def test_a_creation_post_that_never_connected_is_still_absence(
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix must not swallow the case absence is FOR.

    Nothing has been bought when the creation POST itself is refused a
    connection, so this one really is "nothing happened": the claim is released
    so the next tick may try again, and the hold is given back. A fix that
    downgraded every transport failure in the capability would quietly turn a
    provider outage into a scored failure and burn the retry budget.
    """
    import httpx

    fake = _Replicate(RuntimeError("unreachable"))

    def _refuse(cap_id: str, granted: Any = None, **kwargs: Any) -> dict[str, Any]:
        _assert_grant_backed(granted, kwargs)
        fake.posts += 1
        raise _transport_denial(httpx.ConnectError("connection refused"))

    monkeypatch.setattr("omniagentos.connectors.broker.call", _refuse)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    answer = execute(_render_request(), db_path=db_path, budget_ledger=ledger)

    assert answer["outcome"] == OUTCOME_UNAVAILABLE
    assert answer["reason"] == "transport_unreached"
    assert _only_reservation(ledger).state == STATE_RELEASED


# --------------------------------------------------------------------------
# The two questions are two questions
# --------------------------------------------------------------------------


def test_the_outcome_supplies_the_default_billing_answer() -> None:
    """Absence proves nothing was billed; the other two outcomes prove nothing."""
    assert SeamUnavailable("x").may_have_billed is False
    assert SeamRefused("x").may_have_billed is True
    assert SeamUnknown("x").may_have_billed is True
    assert SeamRefused("x", may_have_billed=False).may_have_billed is False
    # The override is per-instance and must not leak onto the class.
    assert SeamRefused("y").may_have_billed is True


# --------------------------------------------------------------------------
# The creation POST's own proof, applied to the money as well as the retry
# --------------------------------------------------------------------------


class _RejectingReplicate:
    """Every creation POST is answered with one 4xx. Nothing is ever created."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.posts = 0

    def __call__(self, cap_id: str, granted: Any = None, **kwargs: Any) -> dict[str, Any]:
        _assert_grant_backed(granted, kwargs)
        assert str(kwargs.get("method") or "").upper() == "POST", "must never get past creation"
        self.posts += 1
        return {
            "capability": cap_id,
            "status": self.status,
            "ok": False,
            "body": {"detail": "no"},
        }


@pytest.mark.parametrize("status", [422, 401, 403])
def test_a_rejected_creation_post_is_provably_unbilled(
    status: int,
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 4xx to the CREATION post created nothing, so it also billed nothing.

    The module already stakes something much stronger on that proof: it RETRIES
    404/429 creations, which would buy a second prediction if the proof were
    wrong. Holding the proof strongly enough to re-issue the call and not
    strongly enough to give the reservation back is the same fact trusted in one
    place and doubted in another. Charging is bounded but not free — it eats the
    instance's daily cap for calls that cost nothing.
    """
    fake = _RejectingReplicate(status)
    monkeypatch.setattr("omniagentos.connectors.broker.call", fake)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    answer = execute(_render_request(), db_path=db_path, budget_ledger=ledger)

    assert answer["outcome"] == OUTCOME_REFUSED
    assert answer["reason"] == "prediction_rejected"
    assert _only_reservation(ledger).state == STATE_RELEASED, (
        "a creation POST that Replicate rejected created no prediction and billed "
        "nothing; holding its reservation denies the loop real work later"
    )


def test_the_creation_proof_does_not_extend_past_the_creation_post(
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx is NOT the same proof: it cannot be shown that nothing was created."""
    fake = _RejectingReplicate(503)
    monkeypatch.setattr("omniagentos.connectors.broker.call", fake)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    answer = execute(_render_request(), db_path=db_path, budget_ledger=ledger)

    assert answer["outcome"] == OUTCOME_UNKNOWN
    assert _only_reservation(ledger).state == STATE_SETTLED


# --------------------------------------------------------------------------
# K1 — the audit spine's OWN failure, after the money is gone
# --------------------------------------------------------------------------
#
# U-A1 fails closed when it cannot write a durable row. That is right, but it
# raised ONE reason at three positions, and one of those positions is AFTER a
# 201 whose prediction Replicate is already rendering. The seam read that code
# as "decided locally, provably unbilled", released the reservation, and the
# next tick bought the same prediction again — the exact defect class
# ``_after_billable_work`` exists to kill, re-introduced one layer down.


class _BilledThenAuditLost:
    """The creation POST succeeded; the broker then lost its terminal row.

    This is the real broker's post-success finalization failure, reproduced at
    the seam boundary: the outbound POST was issued and answered 201, so the
    prediction exists and is billed, and only the audit write failed.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.posts = 0

    def __call__(self, cap_id: str, granted: Any = None, **kwargs: Any) -> dict[str, Any]:
        from omniagentos.connectors.broker import BrokerDenied

        _assert_grant_backed(granted, kwargs)
        assert str(kwargs.get("method") or "").upper() == "POST"
        self.posts += 1
        raise BrokerDenied(self.reason, cap_id, "broker finalization could not be written")


def test_a_lost_audit_row_after_a_billed_creation_never_refunds(
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prediction is rendering and paid for; the hold must SETTLE, not release."""
    fake = _BilledThenAuditLost("audit_finalization_failed")
    monkeypatch.setattr("omniagentos.connectors.broker.call", fake)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    answer = execute(_render_request(), db_path=db_path, budget_ledger=ledger)

    assert fake.posts == 1
    assert answer["outcome"] == OUTCOME_UNKNOWN, (
        "a request that was issued and whose fate is unrecorded is ambiguous; "
        "calling it a locally-decided refusal claims a proof we do not have"
    )
    assert answer["reason"] == "audit_finalization_failed"
    reservation = _only_reservation(ledger)
    assert reservation.state == STATE_SETTLED, (
        "the creation POST was answered 201 before the audit write failed, so "
        "Replicate is rendering a prediction we paid for; releasing the "
        "reservation stops that money counting against the cap and lets the "
        "next tick buy it a second time"
    )
    assert reservation.actual_usd == pytest.approx(reservation.max_usd)


def test_the_pre_request_audit_failure_keeps_its_provable_absence(
    db_path: str,
    ledger: LoopBudgetLedger,
    isolated_var: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the split: an INTENT write that failed issued nothing.

    Without this the fix could be "mark every audit failure may-have-billed",
    which would over-charge the cap for calls that never opened a socket.
    """
    fake = _BilledThenAuditLost("audit_unavailable")
    monkeypatch.setattr("omniagentos.connectors.broker.call", fake)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    answer = execute(_render_request(), db_path=db_path, budget_ledger=ledger)

    assert answer["reason"] == "audit_unavailable"
    assert _only_reservation(ledger).state == STATE_RELEASED


def test_the_broker_names_a_post_request_audit_failure_differently() -> None:
    """The seam's fix is only real if the BROKER still raises the two codes.

    ``_from_broker_denial`` can only tell the positions apart because
    ``broker.call`` gives them different reasons. Pin the classifier that does
    it, so collapsing the two codes back into one cannot pass.
    """
    from omniagentos.connectors.broker import (
        BrokerDenied,
        _audit_failure_reason,
        _reached_provider,
    )

    assert _audit_failure_reason(reached=True) == "audit_finalization_failed"
    assert _audit_failure_reason(reached=False) == "audit_unavailable"
    # A locally decided denial never reached a provider...
    assert _reached_provider(BrokerDenied("not_granted", "x")) is False
    assert _reached_provider(BrokerDenied("credential_missing", "x")) is False
    # ...but a transport error and anything unclassified may have.
    assert _reached_provider(BrokerDenied("transport_error", "x")) is True
    assert _reached_provider(RuntimeError("boom")) is True
    # And the two codes must carry DIFFERENT remedies, or a caller routing on
    # the code alone learns nothing from the split.
    assert (
        BrokerDenied("audit_finalization_failed", "x").next_action
        != BrokerDenied("audit_unavailable", "x").next_action
    )


# --------------------------------------------------------------------------
# K9 — which side of the floor an operator-side provisioning gap settles on
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "expected_outcome"),
    [
        # Provisioning gaps. U-R3 split main's single ``credential_missing`` into
        # three codes for three different operator remedies; all three are the
        # same fact about the LOOP — it never got to ask anyone anything, and
        # nothing adverse about its work has been established. They settle
        # NEUTRAL, as the undivided code did on main.
        ("credential_missing", OUTCOME_UNAVAILABLE),
        ("capability_unprovisioned", OUTCOME_UNAVAILABLE),
        ("credential_unavailable", OUTCOME_UNAVAILABLE),
        # Reached authorities (this process's own gates) saying no. Adverse.
        ("not_granted", OUTCOME_REFUSED),
        ("no_call_path", OUTCOME_REFUSED),
        ("env_name_out_of_scope", OUTCOME_REFUSED),
        ("mode_denied", OUTCOME_REFUSED),
        # The audit spine's own faults, split by whether bytes left the process.
        ("audit_unavailable", OUTCOME_REFUSED),
        ("audit_finalization_failed", OUTCOME_UNKNOWN),
    ],
)
def test_each_broker_denial_settles_on_a_deliberate_side(
    reason: str, expected_outcome: str
) -> None:
    """One table, adjudicated once, so a later code split cannot drift silently.

    U-R3 added denial codes without extending ``_UNREACHED_DENIALS``, which
    flipped the one live loop capability's provisioning gap from neutral to
    adverse without anyone deciding to. Every reason this seam can receive is
    listed here on purpose.
    """
    from omniagentos.connectors.broker import BrokerDenied

    error = loop_effects._from_broker_denial(BrokerDenied(reason, "replicate.generate", "d"))
    assert error.outcome == expected_outcome, (
        f"{reason!r} settled {error.outcome!r}; if that is now the intended "
        "reading, change this table deliberately rather than the mapping"
    )


def test_a_provisioning_gap_never_charges_the_loop_for_a_call_it_did_not_make() -> None:
    """The other half of neutral: absence also gives the reservation back."""
    from omniagentos.connectors.broker import BrokerDenied

    for reason in sorted(loop_effects._UNREACHED_DENIALS):
        error = loop_effects._from_broker_denial(BrokerDenied(reason, "replicate.generate", "d"))
        assert error.may_have_billed is False, (
            f"{reason!r} is classified as never having reached a provider; it "
            "cannot simultaneously be treated as possibly billed"
        )
