"""LiveSim: graceful degradation & recovery — dependency-down behaviour.

Live context (verified 2026-08-06): LiteLLM on :4000 is DOWN and the metered
Moonshot/Kimi org is PAUSED. The cheap-LLM probe (`scripts/livesim/cheap_llm.py`)
must degrade gracefully: litellm -> claude-cli fallback, and `available=False`
(never an exception, never a hang) when nothing is reachable. The live API's
`GET /api/health` exposes the event_hub degradation contract
(`event_hub.contract_version, state, degraded, consecutive_failures,
degraded_after_failures`).

These tests are non-destructive: live reads only, LLM probes only through
cheap_llm.probe() (which never touches a metered Kimi org), and dead-port
probes go to a port we verified is closed — no live service is disturbed.
Environment-dependent values (latencies, failure counts) are recorded as data;
assertions are structural invariants.
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

pytestmark = pytest.mark.livesim

sys.path.insert(0, "/Users/youruser/OmniAgentOS-worktrees/LIVESIM/scripts/livesim")
import cheap_llm  # noqa: E402

REPO = Path(__file__).resolve().parents[3]


def _dead_port() -> int:
    """A port that was just free — connecting to it must fail fast (refused)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _record_probe(livesim, res: cheap_llm.LlmResult) -> None:
    """Attach model/provider/cost telemetry for a probe that actually ran."""
    livesim.record(
        model=res.model,
        provider=res.provider,
        cost_usd=res.cost_usd,
        cost_quality=res.cost_quality,
        tokens_in=res.tokens_in,
        tokens_out=res.tokens_out,
    )


# ---------------------------------------------------------------------------
# recovery: the health endpoint's event_hub degradation contract
# ---------------------------------------------------------------------------


@pytest.mark.positive
@pytest.mark.recovery
def test_health_event_hub_degradation_contract(livesim, live_api):
    """GET /api/health carries the full event_hub degradation contract with a
    coherent degraded flag: contract_version is a positive int, degraded is a
    bool, and when NOT degraded the consecutive_failures count sits below the
    degraded_after_failures threshold."""
    livesim.target("api")
    status, body, _ = live_api.get("/api/health")
    if status == 0:
        livesim.note(f"live API unreachable: {body}")
        pytest.skip(f"live API :8485 unreachable ({body})")
    livesim.record(inputs={"path": "/api/health"}, outputs=body)
    assert status == 200
    assert isinstance(body, dict)
    hub = body.get("event_hub")
    assert isinstance(hub, dict), "health must expose an event_hub block"
    # contract fields present and well-typed
    assert isinstance(hub.get("contract_version"), int) and hub["contract_version"] >= 1
    assert isinstance(hub.get("degraded"), bool)
    assert isinstance(hub.get("consecutive_failures"), int) and hub["consecutive_failures"] >= 0
    assert isinstance(hub.get("degraded_after_failures"), int) and hub["degraded_after_failures"] > 0
    assert isinstance(hub.get("state"), str) and hub["state"]
    # coherence: an un-degraded hub is below its own threshold
    if not hub["degraded"]:
        assert hub["consecutive_failures"] < hub["degraded_after_failures"], (
            "degraded=False but consecutive_failures >= degraded_after_failures"
        )
    else:
        livesim.note(
            f"event_hub DEGRADED live: consecutive_failures={hub['consecutive_failures']}"
        )
    # environment-dependent extras recorded as data, not asserted exactly
    livesim.extra(
        event_hub_state=hub["state"],
        tailer_alive=hub.get("tailer_alive"),
        subscriber_count=hub.get("subscriber_count"),
        consecutive_failures=hub["consecutive_failures"],
    )
    # CORRECTED 2026-08-06 (was LS-011, logged as a possible product defect:
    # "event_hub reports state=ok while tailer_alive=false"). Read
    # omniagentos/api/eventbus.py::EventHub: the tailer thread is started in
    # subscribe() and stopped in unsubscribe() once the last subscriber drops
    # (_start_locked/_stop_locked under self._lock) — it is lazy-start BY
    # DESIGN, not an accident this test should stay agnostic about. The old
    # test only left a note either way, which meant a real regression (tailer
    # dead WITH live subscribers) would also just print a note and pass. Fix
    # the test to assert the actual invariant: tailer_alive=false is only
    # ever coherent with subscriber_count==0; a dead tailer with subscribers
    # attached is a real product defect and must fail here.
    if hub["state"] == "ok" and hub.get("tailer_alive") is False:
        livesim.note(
            "event_hub state=ok, tailer_alive=false, "
            f"subscriber_count={hub.get('subscriber_count')} — lazy-start (no subscribers), "
            "confirmed by design in omniagentos/api/eventbus.py::EventHub.subscribe/unsubscribe"
        )
        sc = hub.get("subscriber_count")
        # CORRECTED 2026-08-06 (LS-TEST-007): `sc in (0, None)` made a MISSING
        # subscriber_count key (None) indistinguishable from the healthy 0 --
        # if /api/health ever stopped emitting the field, this would keep
        # passing. Require it present AND exactly 0, not merely falsy/absent.
        assert isinstance(sc, int) and sc == 0, (
            "event_hub tailer is dead but subscriber_count is not verifiably zero "
            f"(missing, None, or non-zero) -- this is NOT the lazy-start case, it is "
            f"either a real tailer failure or the health payload dropped the field: "
            f"subscriber_count={sc!r}"
        )


@pytest.mark.boundary
@pytest.mark.recovery
def test_health_contract_stable_across_consecutive_reads(livesim, live_api):
    """Two back-to-back health reads agree on the immutable contract fields
    (contract_version, degraded_after_failures) — the degradation contract must
    not flap between requests even while counters move."""
    livesim.target("api")
    s1, b1, _ = live_api.get("/api/health")
    s2, b2, _ = live_api.get("/api/health")
    if s1 == 0 or s2 == 0:
        pytest.skip("live API :8485 unreachable")
    livesim.record(inputs={"path": "/api/health", "reads": 2},
                   outputs={"first": b1.get("event_hub"), "second": b2.get("event_hub")})
    assert s1 == 200 and s2 == 200
    h1, h2 = b1["event_hub"], b2["event_hub"]
    assert h1["contract_version"] == h2["contract_version"]
    assert h1["degraded_after_failures"] == h2["degraded_after_failures"]
    assert isinstance(h1["degraded"], bool) and isinstance(h2["degraded"], bool)
    # counters may move between reads; record them as data
    livesim.extra(failures_read1=h1["consecutive_failures"], failures_read2=h2["consecutive_failures"])


# ---------------------------------------------------------------------------
# negative: dead dependencies must fail CLOSED and FAST, never hang
# ---------------------------------------------------------------------------


@pytest.mark.negative
@pytest.mark.degradation
def test_litellm_probe_to_dead_port_fails_closed_fast(livesim):
    """The litellm leg of cheap_llm pointed at a verified-dead port returns
    available=False with an error — no exception, no hang. Deterministic
    regardless of whether the real :4000 ever comes back."""
    livesim.target("llm")
    port = _dead_port()
    dead_base = f"http://127.0.0.1:{port}"
    t0 = time.perf_counter()
    with mock.patch.object(cheap_llm, "LITELLM_BASE", dead_base):
        res = cheap_llm._try_litellm("Reply with exactly: ok")
    elapsed = time.perf_counter() - t0
    livesim.record(inputs={"base": dead_base},
                   outputs={"available": res.available, "error": res.error,
                            "elapsed_s": round(elapsed, 3)})
    assert res.available is False
    assert res.error, "a dead-port probe must carry an error, not silence"
    assert res.source == "litellm"
    assert elapsed < 10.0, f"dead-port probe took {elapsed:.1f}s — must fail fast, not hang"


@pytest.mark.negative
@pytest.mark.boundary
def test_live_api_helper_fails_closed_on_dead_port(livesim, live_api):
    """The suite's own LiveApi helper against a dead port degrades to
    (status=0, error payload) instead of raising or hanging — the harness's
    dependency-down contract that every other category relies on."""
    livesim.target("api")
    port = _dead_port()
    api = type(live_api)(f"http://127.0.0.1:{port}")  # same LiveApi class, dead base
    t0 = time.perf_counter()
    status, body, headers = api.get("/api/health", timeout=3.0)
    elapsed = time.perf_counter() - t0
    livesim.record(inputs={"base": api.base},
                   outputs={"status": status, "body": body, "elapsed_s": round(elapsed, 3)})
    assert status == 0
    assert isinstance(body, dict) and body.get("error")
    assert headers == {}
    assert elapsed < 8.0, f"dead-port GET took {elapsed:.1f}s — must fail fast"


# ---------------------------------------------------------------------------
# degradation: the cheap-LLM probe itself (litellm down NOW; Kimi paused)
# ---------------------------------------------------------------------------


@pytest.mark.degradation
@pytest.mark.live_cli  # may legitimately spawn the claude CLI fallback leg
def test_cheap_llm_probe_degrades_gracefully(livesim):
    """cheap_llm.probe() with LiteLLM down must either serve a well-formed
    result from a fallback (claude-cli) or return available=False — never
    raise. Skips (recording why) only when no LLM at all is reachable."""
    livesim.target("llm")
    prompt = "Reply with exactly: ok"
    res = cheap_llm.probe(prompt)
    livesim.record(inputs={"prompt": prompt},
                   outputs={"available": res.available, "source": res.source,
                            "text": res.text[:120], "error": res.error})
    if not res.available:
        # graceful unavailability IS the degradation contract; well-formed even so
        assert isinstance(res.error, str) and res.error
        assert res.source in ("litellm", "claude-cli")
        livesim.note(f"no cheap LLM reachable (last leg {res.source}: {res.error}) — skipping")
        pytest.skip(f"no cheap LLM reachable: {res.source}: {res.error}")
    # well-formed live result
    _record_probe(livesim, res)
    assert isinstance(res.text, str) and res.text.strip()
    assert res.model and res.provider
    assert res.provider != "moonshot", "metered Kimi org must never serve a probe"
    assert isinstance(res.cost_usd, float) and res.cost_usd >= 0.0
    assert res.cost_quality in ("exact", "approximate", "unreported")
    assert res.latency_ms > 0
    livesim.note(f"probe served by {res.source} ({res.model}) in {res.latency_ms}ms")


@pytest.mark.degradation
@pytest.mark.recovery
@pytest.mark.live_cli  # exercises the real claude CLI failover leg
def test_probe_falls_back_past_dead_litellm(livesim):
    """With the litellm leg pinned to a dead port, probe() must recover via the
    next leg in the chain — any result it serves comes from claude-cli, never
    a phantom litellm success. Documents the litellm->claude-cli failover."""
    livesim.target("llm")
    port = _dead_port()
    prompt = "Reply with exactly: ok"
    with mock.patch.object(cheap_llm, "LITELLM_BASE", f"http://127.0.0.1:{port}"):
        res = cheap_llm.probe(prompt)
    livesim.record(inputs={"prompt": prompt, "litellm_base": f"dead:{port}"},
                   outputs={"available": res.available, "source": res.source,
                            "error": res.error})
    if res.available:
        _record_probe(livesim, res)
        assert res.source == "claude-cli", "with litellm dead, only claude-cli may serve"
        assert res.provider == "anthropic-cli"
        assert res.text.strip()
        livesim.note(f"failover litellm->claude-cli worked ({res.latency_ms}ms)")
    else:
        # both legs down: the terminal result must still be well-formed
        assert res.source in ("litellm", "claude-cli")
        assert res.error
        livesim.note(f"both LLM legs down; graceful available=False from {res.source}")
        pytest.skip(f"no fallback LLM reachable: {res.source}: {res.error}")


@pytest.mark.degradation
@pytest.mark.negative
def test_probe_all_providers_down_returns_available_false(livesim):
    """Total blackout: litellm dead AND claude CLI absent from PATH. probe()
    must return available=False without raising — and must NOT fall through to
    any third provider (the paused metered Kimi org). Fully deterministic."""
    livesim.target("llm")
    port = _dead_port()
    with mock.patch.object(cheap_llm, "LITELLM_BASE", f"http://127.0.0.1:{port}"), \
         mock.patch("shutil.which", return_value=None):
        t0 = time.perf_counter()
        res = cheap_llm.probe("Reply with exactly: ok")
        elapsed = time.perf_counter() - t0
    livesim.record(inputs={"litellm": "dead", "claude_cli": "absent"},
                   outputs={"available": res.available, "source": res.source,
                            "error": res.error, "elapsed_s": round(elapsed, 3)})
    assert res.available is False
    assert res.source in ("litellm", "claude-cli"), (
        f"blackout fell through to unexpected provider {res.source!r} — "
        "must never reach a metered org"
    )
    assert res.error
    assert res.model is None and res.cost_usd == 0.0
    assert elapsed < 10.0, "total-blackout probe must fail fast, not hang"
