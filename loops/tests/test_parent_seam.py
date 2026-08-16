"""The credential seam, from the worker's side.

Each test pins one of the four properties the seam is not allowed to lose:

1. approval + receipt semantics survive the move (a T2 seam tool still parks;
   a refused call still records a FAILED receipt against the retry budget);
2. verification comes from a different channel than the actor (the image
   verdict does not move when the renderer's answer is replaced by a lie);
3. absence is never failure (an unreachable authority settles NEUTRAL, releases
   its claim, and does not spend an attempt);
4. ambiguity fails closed (an ``unknown`` answer leaves the receipt claimed).

The seam is exercised against a REAL ``EffectServer`` over a REAL unix socket
wherever the point is the wire; the capability handlers themselves are stubbed,
because paying Replicate is the end-to-end proof's job, not a unit test's.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest
from omniagentos_loops import parent_seam, receipts
from omniagentos_loops.artifacts import MAGIC, image_verification, probe_image
from omniagentos_loops.contracts import (
    EffectStateUnknown,
    EffectUnavailable,
    EvidenceGrade,
    LoopStatus,
    RiskTier,
)
from omniagentos_loops.instances import render_probe
from omniagentos_loops.tools import LoopTool, execute_effect

from omniagentos.contracts import ActionClass

# A 2x2 PNG, produced once by an encoder and pasted as bytes so the fixture
# needs no encoder of its own.
PNG_2X2 = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000200000002080600000072b60d24"
    "0000001849444154789c6360606060f80f0430626001060c1801000f2e01fd7a5c"
    "7c1c0000000049454e44ae426082"
)


class _FakeSeam:
    """A parent that answers a scripted outcome. Speaks the real wire format."""

    def __init__(self, _tmp_path: Path, responses: list[dict[str, Any]]) -> None:
        # NOT pytest's tmp_path: AF_UNIX sun_path is 104 bytes on macOS and a
        # deep temp root overflows it (the same trap loop_effects._short_socket_dir
        # exists for).
        self.directory = tempfile.mkdtemp(prefix="fseam-", dir="/tmp")
        self.path = str(Path(self.directory) / "s.sock")
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(4)
        self._server.settimeout(0.25)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except (TimeoutError, OSError):
                continue
            with connection:
                try:
                    raw = connection.recv(65536)
                    self.requests.append(json.loads(raw.decode("utf-8").split("\n", 1)[0]))
                    response = (
                        self._responses.pop(0)
                        if self._responses
                        else {
                            "v": 1,
                            "outcome": "unknown",
                            "reason": "script_exhausted",
                            "detail": "",
                            "result": None,
                        }
                    )
                    connection.sendall(json.dumps(response).encode("utf-8") + b"\n")
                except Exception:  # noqa: BLE001 - a broken fake must not hang a test
                    continue

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._server.close()
        shutil.rmtree(self.directory, ignore_errors=True)


@pytest.fixture
def fake_seam(tmp_path: Path):
    made: list[_FakeSeam] = []

    def factory(responses: list[dict[str, Any]]) -> _FakeSeam:
        seam = _FakeSeam(tmp_path, responses)
        made.append(seam)
        parent_seam.configure(seam.path)
        return seam

    yield factory
    parent_seam.configure("")
    for seam in made:
        seam.close()


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    return {"v": 1, "outcome": "ok", "reason": "", "detail": "", "result": result}


def _seam_tool(name: str = "act", tier: RiskTier = RiskTier.T1, **extra: Any) -> LoopTool:
    def implementation(**kwargs: Any) -> dict[str, Any]:
        return parent_seam.request_effect("render_probe", "replicate.generate", kwargs)

    return LoopTool(
        name=name,
        tier=tier,
        action_class=ActionClass.SANDBOXED_CREATION,
        idempotency_key=lambda args: "one-image",
        call=implementation,
        **extra,
    )


# --------------------------------------------------------------------------
# protocol
# --------------------------------------------------------------------------


def test_worker_and_parent_agree_on_the_protocol():
    """The two sides duplicate the wire constants; nothing may let them drift."""
    from omniagentos.scheduler import loop_effects

    assert parent_seam.SEAM_PROTOCOL_VERSION == loop_effects.SEAM_PROTOCOL_VERSION
    assert parent_seam.MAX_REQUEST_BYTES == loop_effects.MAX_REQUEST_BYTES
    assert parent_seam.MAX_RESPONSE_BYTES == loop_effects.MAX_RESPONSE_BYTES
    assert parent_seam.ARTIFACT_NAME_RE.pattern == loop_effects.ARTIFACT_NAME_RE.pattern
    assert parent_seam.REPLICATE_GENERATE == loop_effects.REPLICATE_GENERATE
    assert parent_seam.MODEL_COMPLETE == loop_effects.MODEL_COMPLETE
    assert {
        parent_seam.OUTCOME_OK,
        parent_seam.OUTCOME_REFUSED,
        parent_seam.OUTCOME_UNAVAILABLE,
        parent_seam.OUTCOME_UNKNOWN,
    } == {
        loop_effects.OUTCOME_OK,
        loop_effects.OUTCOME_REFUSED,
        loop_effects.OUTCOME_UNAVAILABLE,
        loop_effects.OUTCOME_UNKNOWN,
    }
    # And the artifact path both sides resolve independently must be the same
    # path, or a verifier would grade a file nobody wrote.
    assert parent_seam.artifact_path(Path("/v"), "inst", "a.png") == loop_effects.artifact_path(
        Path("/v"), "inst", "a.png"
    )
    # The client's patience must exceed the parent's, or a completed effect
    # becomes an UNKNOWN because we hung up first.
    assert parent_seam.CLIENT_TIMEOUT_S > loop_effects.DEFAULT_CALL_DEADLINE_S


def test_the_worker_sends_a_declaration_and_never_a_credential(fake_seam):
    seam = fake_seam([_ok({"artifact_path": "/tmp/x.png"})])
    parent_seam.request_effect(
        "render_probe", "replicate.generate", {"model": "m", "prompt": "a rose"}
    )
    sent = seam.requests[0]
    assert sent == {
        "v": 1,
        "instance": "render_probe",
        "capability": "replicate.generate",
        "args": {"model": "m", "prompt": "a rose"},
    }
    # No URL, no header, no path, no token: a declaration, not a request.
    flat = json.dumps(sent).lower()
    for forbidden in ("authorization", "bearer", "http", "token", "secret", "/"):
        assert forbidden not in flat, forbidden


# --------------------------------------------------------------------------
# property 2 — the verification channel differs from the actor
# --------------------------------------------------------------------------


def test_image_verdict_ignores_a_fabricated_result(tmp_path, monkeypatch):
    """A glowing lie from the renderer must not move the verdict by one bit.

    This is the mechanical form of "the API's own response is the actor
    narrating itself". The predicate is handed the truth, then a fabricated
    success, then a fabricated failure; all three verdicts must be identical,
    because the predicate never reads that argument at all.
    """
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    target = parent_seam.artifact_path(tmp_path, render_probe.INSTANCE_ID, "rose.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_2X2)

    args = {
        "item": {
            "model": "black-forest-labs/flux-schnell",
            "prompt": "a rose",
            "artifact_name": "rose.png",
            "output_format": "png",
            "aspect_ratio": "1:1",
            "expect_min_width": 2,
            "expect_min_height": 2,
        }
    }
    truthful = render_probe.verify_render({"prediction_id": "p1"}, args)
    fabricated = render_probe.verify_render(
        {"status": "succeeded", "verified": True, "ok": True, "artifact_path": "/dev/null"}, args
    )
    disavowed = render_probe.verify_render({"success": False, "error": "nope"}, args)
    assert truthful.ok is True
    assert (fabricated.ok, fabricated.detail) == (truthful.ok, truthful.detail)
    assert (disavowed.ok, disavowed.detail) == (truthful.ok, truthful.detail)


def test_the_verdict_is_grade_two_and_names_its_decoder(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    target = parent_seam.artifact_path(tmp_path, "render_probe", "rose.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_2X2)
    verdict = image_verification(
        {
            "artifact_name": "rose.png",
            "output_format": "png",
            "expect_min_width": 2,
            "expect_min_height": 2,
        },
        instance_id="render_probe",
    )
    assert verdict["verified"] is True
    assert verdict["evidence_grade"] == int(EvidenceGrade.INDEPENDENT_DECODER)
    assert verdict["width"] == 2 and verdict["height"] == 2
    assert verdict["decoder"]
    assert verdict["sha256"]


def test_a_json_error_page_named_png_is_not_an_image(tmp_path, monkeypatch):
    """Existence and non-zero size are grade 1 and are not enough."""
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    target = parent_seam.artifact_path(tmp_path, "render_probe", "rose.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'{"detail": "unauthorized"}')
    verdict = image_verification(
        {"artifact_name": "rose.png", "output_format": "png"}, instance_id="render_probe"
    )
    assert verdict["verified"] is False
    assert "magic number" in verdict["detail"]


def test_dimensions_below_the_declared_minimum_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    target = parent_seam.artifact_path(tmp_path, "render_probe", "rose.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG_2X2)
    verdict = image_verification(
        {
            "artifact_name": "rose.png",
            "output_format": "png",
            "expect_min_width": 512,
            "expect_min_height": 512,
        },
        instance_id="render_probe",
    )
    assert verdict["verified"] is False
    assert "below the declared minimum" in verdict["detail"]


def test_probe_image_declares_a_magic_number_for_every_format_the_seam_writes():
    from omniagentos.scheduler import loop_effects

    formats = set(loop_effects._REPLICATE_ARGS["output_format"].choices)
    assert formats and formats <= set(MAGIC)
    with pytest.raises(FileNotFoundError):
        probe_image(Path("/nonexistent/nope.png"), expect_format="png")


# --------------------------------------------------------------------------
# property 1 — approval and receipt semantics survive the move
# --------------------------------------------------------------------------


def test_a_t2_seam_tool_still_parks_for_a_human(make_ctx, fake_seam):
    """Moving WHERE the call happens changes nothing about what governs it."""
    from omniagentos_loops.contracts import EffectNotApproved
    from omniagentos_loops.policy_gate import evaluate_tool

    fake_seam([_ok({"artifact_path": "/tmp/x.png"})])
    tool = _seam_tool(name="act", tier=RiskTier.T2)
    ctx = make_ctx(template="poll_classify_act_verify")
    ctx.tools.register(tool)

    assert evaluate_tool(ctx, tool, {}).decision == "approve"
    with pytest.raises(EffectNotApproved):
        execute_effect(ctx, node="act", tool=tool, args={}, business_key="k", gate_token=None)
    # And the tool was never reached, so the parent was never asked.
    assert (
        receipts.receipt_state(
            ctx, receipts.receipt_key(ctx.instance_id, ctx.template, "act", "act", "k")
        )
        == "absent"
    )


def test_a_refused_call_is_a_failed_receipt_against_the_budget(make_ctx, fake_seam):
    """Reached-and-refused is ADVERSE, recorded, and bounded by max_attempts."""
    refusal = {
        "v": 1,
        "outcome": "refused",
        "reason": "prediction_rejected",
        "detail": "Replicate answered 422: bad prompt",
        "result": None,
    }
    fake_seam([refusal, refusal, refusal])
    tool = _seam_tool(max_attempts=2)
    ctx = make_ctx(template="poll_classify_act_verify")
    ctx.tools.register(tool)
    key = receipts.receipt_key(ctx.instance_id, ctx.template, "act", "act", "k")

    first = execute_effect(ctx, node="act", tool=tool, args={}, business_key="k", gate_token=None)
    assert first["succeeded"] is False
    assert receipts.receipt_state(ctx, key, 1) == receipts.FAILED

    second = execute_effect(ctx, node="act", tool=tool, args={}, business_key="k", gate_token=None)
    assert second["attempt"] == 2
    with pytest.raises(Exception) as exhausted:
        execute_effect(ctx, node="act", tool=tool, args={}, business_key="k", gate_token=None)
    assert "attempts" in str(exhausted.value)


def test_a_successful_call_is_replayed_not_re_paid(make_ctx, fake_seam):
    """The receipt still short-circuits: two ticks, one call to the parent."""
    seam = fake_seam([_ok({"artifact_path": "/tmp/x.png", "prediction_id": "p1"})])
    tool = _seam_tool()
    ctx = make_ctx(template="poll_classify_act_verify")
    ctx.tools.register(tool)

    first = execute_effect(ctx, node="act", tool=tool, args={}, business_key="k", gate_token=None)
    second = execute_effect(ctx, node="act", tool=tool, args={}, business_key="k", gate_token=None)
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert len(seam.requests) == 1


# --------------------------------------------------------------------------
# property 3 — absence is never failure
# --------------------------------------------------------------------------


def test_an_unreachable_authority_is_absence_not_failure(make_ctx, fake_seam):
    """The 2026-07-31 defect, re-armed and refused.

    ``unavailable`` releases the claim, spends no attempt, records no failure
    and emits a loud event. Rendering it as ``gate_passed=0`` is what auto-paused
    four routines, and rendering it as success is worse.
    """
    fake_seam(
        [
            {
                "v": 1,
                "outcome": "unavailable",
                "reason": "credential_missing",
                "detail": "REPLICATE_API_TOKEN is not present",
                "result": None,
            }
        ]
    )
    tool = _seam_tool()
    ctx = make_ctx(template="poll_classify_act_verify")
    ctx.tools.register(tool)
    key = receipts.receipt_key(ctx.instance_id, ctx.template, "act", "act", "k")

    with pytest.raises(EffectUnavailable) as absent:
        execute_effect(ctx, node="act", tool=tool, args={}, business_key="k", gate_token=None)
    assert absent.value.reason == "credential_missing"

    # No receipt survives: nothing happened, so nothing is recorded, so the
    # next tick gets attempt 1 again rather than failing closed on a claim.
    assert receipts.receipt_state(ctx, key, 1) == "absent"
    assert receipts.receipt_exists(ctx, key) is False

    events = ctx.store.get_events_for_target("loop", ctx.instance_id)
    unavailable = [e for e in events if e.get("action") == "loop.effect.unavailable"]
    assert unavailable, "an unreachable authority must be LOUD"
    payload = json.loads(unavailable[0]["payload_json"])
    assert payload["reason"] == "credential_missing"
    assert payload["claim_released"] is True


def test_a_missing_seam_is_absence_and_nothing_is_attempted():
    parent_seam.configure("")
    with pytest.raises(EffectUnavailable) as absent:
        parent_seam.request_effect("render_probe", "replicate.generate", {})
    assert absent.value.reason == "no_seam"


def test_a_refused_connection_is_absence(tmp_path):
    parent_seam.configure(str(tmp_path / "not-a-socket.sock"))
    try:
        with pytest.raises(EffectUnavailable) as absent:
            parent_seam.request_effect("render_probe", "replicate.generate", {})
        assert absent.value.reason == "seam_unreachable"
    finally:
        parent_seam.configure("")


def test_an_unavailable_effect_settles_the_tick_neutral_and_loud(make_ctx, fake_seam):
    """End of the absence chain: a NEUTRAL tick, out of the acceptance floor."""
    fake_seam(
        [
            {
                "v": 1,
                "outcome": "unavailable",
                "reason": "transport_unreached",
                "detail": "connection refused",
                "result": None,
            }
        ]
    )
    ctx = make_ctx(
        instance_id="render_probe",
        template="poll_classify_act_verify",
        params={
            "prompt": "a single red rose",
            "artifact_name": "rose.png",
            "expect_min_width": 2,
            "expect_min_height": 2,
        },
    )
    # The REAL instance module, not a stand-in: the point is that the shipped
    # render tool's unreachable authority settles neutral.
    render_probe.register(ctx)

    from omniagentos_loops.runtime import run_once
    from omniagentos_loops.templates import get_template

    report = run_once(ctx, get_template("poll_classify_act_verify"))
    assert report.status is LoopStatus.IDLE
    assert report.outcome == "neutral"
    assert report.as_dict()["accepted"] is False
    assert report.detail.startswith("unavailable:"), report.detail


# --------------------------------------------------------------------------
# property 4 — ambiguity fails closed
# --------------------------------------------------------------------------


def test_an_ambiguous_answer_leaves_the_receipt_claimed(make_ctx, fake_seam):
    fake_seam(
        [
            {
                "v": 1,
                "outcome": "unknown",
                "reason": "transport_ambiguous",
                "detail": "read timeout after the request was written",
                "result": None,
            }
        ]
    )
    tool = _seam_tool()
    ctx = make_ctx(template="poll_classify_act_verify")
    ctx.tools.register(tool)
    key = receipts.receipt_key(ctx.instance_id, ctx.template, "act", "act", "k")

    with pytest.raises(EffectStateUnknown):
        execute_effect(ctx, node="act", tool=tool, args={}, business_key="k", gate_token=None)
    assert receipts.receipt_state(ctx, key, 1) == receipts.CLAIMED

    # The next tick refuses to re-run, exactly as after a crash.
    fake_seam([_ok({"artifact_path": "/tmp/x.png"})])
    with pytest.raises(EffectStateUnknown):
        execute_effect(ctx, node="act", tool=tool, args={}, business_key="k", gate_token=None)


def test_an_unrecognised_outcome_is_not_a_success(fake_seam):
    fake_seam([{"v": 1, "outcome": "fine", "reason": "", "detail": "", "result": {"ok": True}}])
    with pytest.raises(EffectStateUnknown):
        parent_seam.request_effect("render_probe", "replicate.generate", {})


def test_an_ok_answer_without_a_result_object_is_not_a_success(fake_seam):
    fake_seam([{"v": 1, "outcome": "ok", "reason": "", "detail": "", "result": None}])
    with pytest.raises(EffectStateUnknown):
        parent_seam.request_effect("render_probe", "replicate.generate", {})


def test_a_wrong_protocol_version_is_not_a_success(fake_seam):
    fake_seam([{"v": 99, "outcome": "ok", "reason": "", "detail": "", "result": {}}])
    with pytest.raises(EffectStateUnknown):
        parent_seam.request_effect("render_probe", "replicate.generate", {})


# --------------------------------------------------------------------------
# ctx.model() through the seam
# --------------------------------------------------------------------------


def test_ctx_model_executes_parent_side_when_a_seam_exists(make_ctx, fake_seam):
    seam = fake_seam([_ok({"text": "hello", "cost": {"model": "m", "estimated_usd_cost": 0.001}})])
    ctx = make_ctx(instance_id="render_probe")
    text = ctx.model().complete([{"role": "user", "content": "hi"}], purpose="probe")
    assert text == "hello"
    sent = seam.requests[0]
    assert sent["capability"] == "model.complete"
    assert sent["args"]["messages"] == [{"role": "user", "content": "hi"}]
    # The caller's free-text tag is normalised into an anchored identifier the
    # parent can validate; it is never passed through as prose.
    assert sent["args"]["purpose"] == "loop:draft_approve_send:probe"
    events = [
        e
        for e in ctx.store.get_events_for_target("loop", ctx.instance_id)
        if e["action"] == "loop.model_call"
    ]
    assert events, "a parent-side model call must still land on the cost ledger"
    assert json.loads(events[0]["payload_json"])["estimated_usd_cost"] == 0.001


def test_ctx_model_uses_the_local_client_when_there_is_no_seam(make_ctx):
    parent_seam.configure("")
    ctx = make_ctx()
    from omniagentos.llm.client import ShortCallClient

    assert isinstance(ctx.model()._client, ShortCallClient)
