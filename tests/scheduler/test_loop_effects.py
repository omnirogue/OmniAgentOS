"""The credential seam, from the PARENT's side.

The worker declares; this process decides. Everything below is about the
deciding: what the parent refuses without ever touching a credential, what it
records while it does, and how it classifies the three ways a call can fail to
produce a result.

The load-bearing distinction under test is ``refused`` vs ``unavailable`` vs
``unknown``:

* ``refused``    — an authority was reached and said no. ADVERSE.
* ``unavailable``— the authority was never reached. ABSENCE: neutral and loud,
  out of the acceptance denominator, exactly as ``GateWorkspaceUnusable``.
  Getting this wrong in the *other* direction auto-paused four routines on
  2026-07-31, and getting it wrong in this direction would release an
  idempotency claim for an effect that happened.
* ``unknown``    — ambiguous. FAIL CLOSED.
"""

from __future__ import annotations

import ast
import inspect
import json
import socket
import textwrap
from pathlib import Path
from typing import Any

import pytest

from omniagentos.connectors import AUTO_CLASSES
from omniagentos.connectors.broker import BrokerDenied
from omniagentos.contracts import ActionClass
from omniagentos.db.store import SqliteStore
from omniagentos.scheduler import loop_effects
from omniagentos.scheduler.loop_budget import LoopBudgetLedger
from omniagentos.scheduler.loop_effects import (
    OUTCOME_OK,
    OUTCOME_REFUSED,
    OUTCOME_UNAVAILABLE,
    OUTCOME_UNKNOWN,
    ArgSpec,
    EffectServer,
    ParentCapability,
    SeamRequest,
    execute,
)
from tests.support.db_template import migrated_db


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "control.sqlite3")
    return migrated_db(SqliteStore, path)


@pytest.fixture
def budget_ledger(db_path: str):
    """Budget ledger with realistic caps for paid capabilities (replicate, model)."""
    return LoopBudgetLedger(
        db_path,
        instance_caps={"render_probe": 50.0},  # Generous for testing
        global_ceiling_usd=200.0,
    )


def _request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "v": 1,
        "instance": "render_probe",
        "capability": "replicate.generate",
        "args": {
            "model": "black-forest-labs/flux-schnell",
            "prompt": "a single red rose",
            "artifact_name": "rose.png",
        },
    }
    payload.update(overrides)
    return payload


def _calls(db_path: str) -> list[dict[str, Any]]:
    from omniagentos.connectors.store import CapabilityStore

    store = SqliteStore(db_path)
    try:
        return CapabilityStore(store).call_log()
    finally:
        store.close()


@pytest.fixture
def stub_capability(monkeypatch):
    """Replace a capability's handler; the DECISION path is what is under test."""

    def install(cap_id: str, run, *, action_class: ActionClass | None = None):
        original = loop_effects.CAPABILITIES[cap_id]
        replacement = ParentCapability(
            id=original.id,
            action_class=action_class or original.action_class,
            broker_capability=original.broker_capability,
            args=original.args,
            run=run,
        )
        monkeypatch.setitem(loop_effects.CAPABILITIES, cap_id, replacement)
        return replacement

    return install


# --------------------------------------------------------------------------
# what the parent refuses, before any credential is touched
# --------------------------------------------------------------------------


def test_a_capability_outside_the_closed_registry_is_refused(db_path, budget_ledger):
    answer = execute(
        _request(capability="shell.exec"), db_path=db_path, budget_ledger=budget_ledger
    )
    assert answer["outcome"] == OUTCOME_REFUSED
    assert answer["reason"] == "unknown_capability"


def test_a_free_text_command_is_not_even_a_capability_id(db_path, budget_ledger):
    for hostile in ("rm -rf /", "replicate.generate; id", "../../etc/passwd", "Replicate.Generate"):
        answer = execute(_request(capability=hostile), db_path=db_path, budget_ledger=budget_ledger)
        assert answer["outcome"] == OUTCOME_REFUSED
        assert answer["reason"] == "bad_capability", hostile


def test_an_instance_that_holds_no_grant_is_refused(db_path, budget_ledger):
    answer = execute(
        _request(instance="w2_inbox_triage"), db_path=db_path, budget_ledger=budget_ledger
    )
    assert answer["outcome"] == OUTCOME_REFUSED
    assert answer["reason"] == "not_granted"


def test_the_grant_is_resolved_by_instance_id_in_source_not_from_the_request(
    db_path, budget_ledger
):
    """A row is data; a grant is an authorization decision. Only source declares it."""
    assert loop_effects._granted_capabilities("render_probe")
    assert loop_effects._granted_capabilities("anything-else") == frozenset()
    # There is no field a caller could add to widen its own grant.
    answer = execute(
        _request(granted=["replicate.generate"], instance="attacker"),
        db_path=db_path,
        budget_ledger=budget_ledger,
    )
    assert answer["outcome"] == OUTCOME_REFUSED
    assert answer["reason"] in {"not_granted", "bad_instance"}


def test_loop_broker_call_requires_the_database_grant_even_with_source_floor(db_path, monkeypatch):
    """The source floor and broker grant are independent, conjunctive facts."""
    import httpx

    from omniagentos.connectors.store import CapabilityStore

    holder = "loop:render_probe"
    capability = "replicate.generate"
    assert capability in loop_effects.INSTANCE_CAPABILITIES["render_probe"]

    store = SqliteStore(db_path)
    grant_store = CapabilityStore(store)
    try:
        assert grant_store.get_grant(holder) == [capability]
        grant_store.set_grant(holder, [])
        grant_store.set_grant(holder, [capability], actor="test")
    finally:
        store.close()

    class Response:
        status_code = 200
        is_success = True
        text = ""

        @staticmethod
        def json() -> dict[str, str]:
            return {"id": "generated-test-prediction"}

    monkeypatch.setenv("REPLICATE_API_TOKEN", "generated-in-test")
    monkeypatch.setattr(httpx, "request", lambda *_args, **_kwargs: Response())

    result = loop_effects._broker_call(
        capability,
        audit_db_path=db_path,
        holder=holder,
        method="POST",
        path="/models/black-forest-labs/flux-schnell/predictions",
        body={"input": {"prompt": "generated in test"}},
    )
    assert result["ok"] is True

    store = SqliteStore(db_path)
    try:
        CapabilityStore(store).set_grant(holder, [], actor="test")
    finally:
        store.close()

    assert capability in loop_effects.INSTANCE_CAPABILITIES["render_probe"]
    with pytest.raises(loop_effects.SeamRefused) as denied:
        loop_effects._broker_call(
            capability,
            audit_db_path=db_path,
            holder=holder,
            method="POST",
            path="/models/black-forest-labs/flux-schnell/predictions",
            body={"input": {"prompt": "generated in test"}},
        )
    assert denied.value.reason == "not_granted"


def test_migration_seeds_every_broker_backed_loop_floor(db_path):
    """Existing loop instances retain their brokered behavior after migration."""
    from omniagentos.connectors.store import CapabilityStore

    store = SqliteStore(db_path)
    try:
        grant_store = CapabilityStore(store)
        for instance_id, floor in loop_effects.INSTANCE_CAPABILITIES.items():
            broker_grants = sorted(
                {
                    loop_effects.CAPABILITIES[capability_id].broker_capability
                    for capability_id in floor
                    if loop_effects.CAPABILITIES[capability_id].broker_capability
                }
            )
            assert grant_store.get_grant(f"loop:{instance_id}") == broker_grants
    finally:
        store.close()


def test_loop_broker_caller_cannot_construct_a_granted_list():
    """Counterfeit: the production seam cannot pass ``[cap_id]`` or any list."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(loop_effects._broker_call)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "call"
    ]
    assert len(calls) == 1
    broker_call = calls[0]
    assert len(broker_call.args) == 1
    assert "granted" not in {keyword.arg for keyword in broker_call.keywords}
    assert {"grant_store", "grant_holder"} <= {keyword.arg for keyword in broker_call.keywords}


def test_grant_backed_broker_rejects_a_caller_supplied_granted_list(db_path):
    """Counterfeit: even a listed capability cannot vouch for itself."""
    from omniagentos.connectors.broker import authorize_with_grant
    from omniagentos.connectors.store import CapabilityStore

    store = SqliteStore(db_path)
    try:
        with pytest.raises(BrokerDenied) as denied:
            authorize_with_grant(
                "replicate.generate",
                ["replicate.generate"],
                CapabilityStore(store),
                grant_holder="loop:render_probe",
            )
        assert denied.value.reason == "caller_supplied_grant"
    finally:
        store.close()


def test_a_hard_human_capability_is_refused_by_the_parent_itself(
    db_path, stub_capability, budget_ledger
):
    """Gate 2, in code: an approval is the WORKER's evidence, not the parent's floor."""
    stub_capability(
        "replicate.generate",
        lambda request: {"ran": True},
        action_class=ActionClass.CONSEQUENTIAL,
    )
    answer = execute(_request(), db_path=db_path, budget_ledger=budget_ledger)
    assert answer["outcome"] == OUTCOME_REFUSED
    assert answer["reason"] == "requires_human_approval"


def test_every_shipped_capability_is_auto_class():
    for capability in loop_effects.CAPABILITIES.values():
        assert capability.action_class in AUTO_CLASSES, capability.id


def test_a_wrong_protocol_version_is_refused(db_path, budget_ledger):
    answer = execute(_request(v=99), db_path=db_path, budget_ledger=budget_ledger)
    assert answer["outcome"] == OUTCOME_REFUSED
    assert answer["reason"] == "bad_protocol"


# --------------------------------------------------------------------------
# typed arguments
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "why"),
    [
        ({"model": "evil/model", "prompt": "p", "artifact_name": "a.png"}, "unreviewed model"),
        ({"model": "black-forest-labs/flux-schnell", "prompt": "p"}, "missing artifact_name"),
        (
            {
                "model": "black-forest-labs/flux-schnell",
                "prompt": "p",
                "artifact_name": "../../escape.png",
            },
            "path traversal",
        ),
        (
            {"model": "black-forest-labs/flux-schnell", "prompt": "p", "artifact_name": "rose.sh"},
            "not an allowed artifact extension",
        ),
        (
            {
                "model": "black-forest-labs/flux-schnell",
                "prompt": "p",
                "artifact_name": "rose.png",
                "disable_safety_checker": True,
            },
            "unknown argument",
        ),
        (
            {"model": "black-forest-labs/flux-schnell", "prompt": "", "artifact_name": "rose.png"},
            "empty prompt",
        ),
        (
            {
                "model": "black-forest-labs/flux-schnell",
                "prompt": "p",
                "artifact_name": "rose.png",
                "expect_min_width": 999999,
            },
            "out of range",
        ),
    ],
)
def test_arguments_are_typed_and_refused_field_by_field(db_path, args, why, budget_ledger):
    answer = execute(_request(args=args), db_path=db_path, budget_ledger=budget_ledger)
    assert answer["outcome"] == OUTCOME_REFUSED, why
    assert answer["reason"] == "invalid_arguments", why


def test_html_artifacts_are_accepted(db_path, stub_capability, budget_ledger):
    """HTML and other document formats are now accepted artifact types."""
    stub_capability("replicate.generate", lambda request: {"artifact_path": "/tmp/x.html"})
    # clock.html should now be accepted (this was the original defect)
    answer = execute(
        _request(
            args={
                "model": "black-forest-labs/flux-schnell",
                "prompt": "p",
                "artifact_name": "clock.html",
            }
        ),
        db_path=db_path,
        budget_ledger=budget_ledger,
    )
    assert answer["outcome"] == OUTCOME_OK
    # JSON, CSV, markdown should also work
    for ext in ("json", "txt", "md", "csv", "htm"):
        answer = execute(
            _request(
                args={
                    "model": "black-forest-labs/flux-schnell",
                    "prompt": "p",
                    "artifact_name": f"data.{ext}",
                }
            ),
            db_path=db_path,
            budget_ledger=budget_ledger,
        )
        assert answer["outcome"] == OUTCOME_OK, f"Failed for .{ext}"


def test_the_worker_never_names_a_directory(tmp_path):
    """It names a leaf; the parent derives the whole path and contains it."""
    assert loop_effects.artifact_path(tmp_path, "render_probe", "rose.png") == (
        tmp_path / "loops" / "artifacts" / "render_probe" / "rose.png"
    )
    for hostile in ("../rose.png", "a/b.png", "/etc/rose.png", ".rose.png", "rose.png "):
        with pytest.raises(loop_effects.SeamRefused):
            loop_effects.artifact_path(tmp_path, "render_probe", hostile)


def test_a_symlinked_artifact_name_cannot_escape_the_root(tmp_path, monkeypatch, db_path):
    """Containment is re-checked on the RESOLVED path, not on the string."""
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    root = loop_effects.artifact_root(tmp_path, "render_probe")
    root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "rose.png").symlink_to(outside / "rose.png")
    with pytest.raises(loop_effects.SeamRefused):
        loop_effects._contained(root / "rose.png", root)


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------


def test_non_broker_handlers_do_not_forge_broker_audit_rows(
    db_path, stub_capability, budget_ledger
):
    """A handler that never reached the broker cannot leave an ``allowed`` row.

    U-A1's point: the ALLOWED attestation belongs to the component that held
    the credential. A stubbed handler makes no brokered call, so nothing may
    claim one happened.
    """
    stub_capability("replicate.generate", lambda request: {"artifact_path": "/tmp/x.png"})
    assert (
        execute(_request(), db_path=db_path, budget_ledger=budget_ledger)["outcome"] == OUTCOME_OK
    )

    assert [row for row in _calls(db_path) if row["allowed"] == 1] == []
    assert _calls(db_path) == []


def test_every_attempt_is_audited_allowed_and_denied(
    db_path, stub_capability, budget_ledger, monkeypatch
):
    """Both halves of the attempt record survive U-A1's move into the broker.

    ALLOWED is now emitted by the broker (intent → final) rather than asserted
    by the caller, which is strictly stronger. DENIED is split by provenance: a
    refusal the broker made is a broker row, and a refusal that never reached
    the broker is still recorded by the seam — otherwise a loop reaching past
    its own source floor would leave no durable trace at all.
    """
    import httpx

    class _Response:
        status_code = 200
        is_success = True
        text = ""

        @staticmethod
        def json() -> dict[str, str]:
            return {"id": "generated-test-prediction", "status": "succeeded", "output": []}

    monkeypatch.setenv("REPLICATE_API_TOKEN", "generated-in-test")
    monkeypatch.setattr(httpx, "request", lambda *_a, **_kw: _Response())
    stub_capability(
        "replicate.generate",
        lambda request: (
            loop_effects._broker_call(
                "replicate.generate",
                audit_db_path=request.db_path,
                holder=f"loop:{request.instance_id}",
                method="POST",
                path="/models/black-forest-labs/flux-schnell/predictions",
                body={"input": {"prompt": "generated in test"}},
            )
            and {"artifact_path": "/tmp/x.png"}
        ),
    )

    outcome = execute(_request(), db_path=db_path, budget_ledger=budget_ledger)
    assert outcome["outcome"] == OUTCOME_OK, outcome
    allowed = [row for row in _calls(db_path) if row["allowed"] == 1]
    assert allowed, "a real brokered call must leave a broker-emitted allowed row"
    assert allowed[0]["capability_id"] == "replicate.generate"
    assert allowed[0]["agent_id"] == "loop:render_probe"
    assert allowed[0]["decision"] == "allowed"

    # A capability outside the instance's source floor never reaches the broker.
    execute(_request(instance="w2_inbox_triage"), db_path=db_path, budget_ledger=budget_ledger)
    denied = [row for row in _calls(db_path) if row["allowed"] == 0]
    assert denied and denied[0]["reason"] == "not_granted"
    assert denied[0]["agent_id"] == "loop:w2_inbox_triage"
    # Provenance is unambiguous: the seam refused, the broker did not decide.
    assert denied[0]["method"] == "seam"
    assert denied[0]["decision"] == "refused"


def test_a_seam_with_no_audit_store_refuses_to_call_and_calls_it_absence(
    stub_capability, budget_ledger
):
    """No audit trail, no call — and the refusal is ABSENCE, not the loop's fault."""
    ran: list[int] = []
    stub_capability("replicate.generate", lambda request: ran.append(1) or {})
    answer = execute(_request(), db_path="")
    assert answer["outcome"] == OUTCOME_UNAVAILABLE
    assert answer["reason"] == "audit_unavailable"
    assert ran == []


def test_runtime_path_refusal_happens_before_budget_reservation(
    db_path,
    budget_ledger,
    stub_capability,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A malformed campaign cannot write to checkout state or consume a hold."""
    sim_root = tmp_path / "simulations"
    campaign_root = sim_root / "loop-effects"
    campaign_root.mkdir(parents=True)
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "loop-effects")
    monkeypatch.setenv("OMNIAGENTOS_SIM_ROOT", str(sim_root))
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN_ROOT", str(campaign_root))
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(campaign_root))
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    ran: list[int] = []
    stub_capability("replicate.generate", lambda request: ran.append(1) or {})

    answer = execute(_request(), db_path=db_path, budget_ledger=budget_ledger)

    assert answer["outcome"] == OUTCOME_UNAVAILABLE
    assert answer["reason"] == "runtime_path_unavailable"
    assert str(tmp_path) not in answer["detail"]
    assert ran == []
    reservations = budget_ledger._conn.execute("SELECT COUNT(*) FROM loop_reservations").fetchone()[
        0
    ]
    assert reservations == 0
    rows = _calls(db_path)
    assert len(rows) == 1
    assert rows[0]["decision"] == OUTCOME_UNAVAILABLE
    assert rows[0]["reason"] == "runtime_path_unavailable"


def test_a_paid_capability_without_budget_ledger_fails_closed(
    db_path, stub_capability, budget_ledger
):
    """Regression guard: paid capability without ledger REFUSES (wiring safety check)."""
    stub_capability("replicate.generate", lambda request: {"artifact_path": "/tmp/x.png"})
    # Intentionally call with budget_ledger=None to test the guard
    answer = execute(_request(), db_path=db_path, budget_ledger=None)
    # Must REFUSE, not proceed silently
    assert answer["outcome"] == OUTCOME_REFUSED
    assert "budget_ledger" in answer["reason"].lower()


# --------------------------------------------------------------------------
# the three failure classes
# --------------------------------------------------------------------------


def test_a_reached_and_refused_authority_is_adverse(db_path, stub_capability, budget_ledger):
    def refuse(request: SeamRequest) -> dict[str, Any]:
        raise loop_effects.SeamRefused("prediction_rejected", "Replicate answered 422")

    stub_capability("replicate.generate", refuse)
    answer = execute(_request(), db_path=db_path, budget_ledger=budget_ledger)
    assert answer["outcome"] == OUTCOME_REFUSED
    assert answer["reason"] == "prediction_rejected"


def test_a_dead_credential_is_absence_not_failure(db_path, stub_capability, budget_ledger):
    """The Class P rule, verbatim: "we could not ask" is not "the answer was no"."""

    def dead(request: SeamRequest) -> dict[str, Any]:
        raise loop_effects._from_broker_denial(
            BrokerDenied("credential_missing", "replicate.generate", "not present")
        )

    stub_capability("replicate.generate", dead)
    answer = execute(_request(), db_path=db_path, budget_ledger=budget_ledger)
    assert answer["outcome"] == OUTCOME_UNAVAILABLE
    assert answer["reason"] == "credential_missing"


def test_a_grant_denial_is_a_reached_authority_and_stays_adverse(
    db_path, stub_capability, budget_ledger
):
    def denied(request: SeamRequest) -> dict[str, Any]:
        raise loop_effects._from_broker_denial(
            BrokerDenied("not_granted", "replicate.generate", "nope")
        )

    stub_capability("replicate.generate", denied)
    answer = execute(_request(), db_path=db_path, budget_ledger=budget_ledger)
    assert answer["outcome"] == OUTCOME_REFUSED


def test_a_connect_failure_is_absence_but_a_read_timeout_is_unknown():
    """The split that stops a maybe-sent request from releasing an idempotency claim."""
    import httpx

    connect = loop_effects._from_transport(httpx.ConnectError("refused"), "refused")
    read = loop_effects._from_transport(httpx.ReadTimeout("timed out"), "timed out")
    assert connect.outcome == OUTCOME_UNAVAILABLE
    assert read.outcome == OUTCOME_UNKNOWN
    assert loop_effects._from_transport(None, "?").outcome == OUTCOME_UNKNOWN


def test_an_unclassified_handler_exception_fails_closed(db_path, stub_capability, budget_ledger):
    """A crash after a possibly-billed call leaves a durable, outcome-level row.

    This assertion used to be ``_calls(db_path) == []`` — it PINNED the absence.
    A handler crash is exactly where a trace matters: the reservation is charged
    on the assumption the provider may have run, and the broker's rows (if any)
    describe the CALL, never the handler that fell over holding it. So the seam
    writes one row in its own ``method="seam"`` vocabulary, which cannot be
    mistaken for a brokered decision.
    """

    def explode(request: SeamRequest) -> dict[str, Any]:
        raise RuntimeError("something nobody thought about")

    stub_capability("replicate.generate", explode)
    answer = execute(_request(), db_path=db_path, budget_ledger=budget_ledger)
    assert answer["outcome"] == OUTCOME_UNKNOWN
    assert answer["reason"] == "handler_error"

    rows = _calls(db_path)
    assert [row["reason"] for row in rows] == ["handler_error"]
    assert rows[0]["method"] == "seam"
    assert rows[0]["decision"] == "error"
    assert rows[0]["allowed"] == 0
    assert rows[0]["holder"] == "loop:render_probe"


def test_an_artifact_url_off_the_allowlist_is_refused(tmp_path, budget_ledger):
    with pytest.raises(loop_effects.SeamRefused) as refused:
        loop_effects._download_artifact("https://evil.example.com/x.png", tmp_path / "x.png")
    assert refused.value.reason == "artifact_host_not_allowed"
    with pytest.raises(loop_effects.SeamRefused):
        loop_effects._download_artifact("http://replicate.delivery/x.png", tmp_path / "x.png")
    with pytest.raises(loop_effects.SeamRefused):
        loop_effects._download_artifact(
            "https://replicate.delivery.evil.com/x.png", tmp_path / "x.png"
        )


# --------------------------------------------------------------------------
# the connector catalogue entry this capability rides on
# --------------------------------------------------------------------------


def test_replicate_generate_is_broker_callable_and_narrowly_scoped(budget_ledger):
    import re

    from omniagentos.connectors import load_registry

    registry = load_registry(str(Path(__file__).resolve().parents[2] / "configs/connectors.yaml"))
    cap = registry.capability("replicate.generate")
    assert cap.action_class is ActionClass.SANDBOXED_CREATION
    assert cap.callable_now, "a declared-but-uncallable capability is refused by the broker"
    assert cap.http.base_url == "https://api.replicate.com/v1"
    assert set(cap.http.methods) == {"GET", "POST"}
    assert cap.http.auth == "bearer:REPLICATE_API_TOKEN"
    allowed = "/models/black-forest-labs/flux-schnell/predictions"
    assert re.fullmatch(cap.http.path_regex, allowed)
    assert re.fullmatch(cap.http.path_regex, "/predictions/abc-123")
    for refused in ("/models/x/y/anything", "/trainings", "/models/x/y/predictions/cancel", "/"):
        assert not re.fullmatch(cap.http.path_regex, refused), refused


def test_every_broker_backed_capability_names_a_real_one(budget_ledger):
    from omniagentos.connectors import load_registry

    registry = load_registry(str(Path(__file__).resolve().parents[2] / "configs/connectors.yaml"))
    for capability in loop_effects.CAPABILITIES.values():
        if not capability.broker_capability:
            continue
        cap = registry.capability(capability.broker_capability)
        assert cap.action_class == capability.action_class, capability.id
        assert cap.callable_now, capability.id


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


def _round_trip(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(15.0)
    connection.connect(path)
    try:
        connection.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        chunks = b""
        while b"\n" not in chunks:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks += chunk
    finally:
        connection.close()
    return json.loads(chunks.split(b"\n", 1)[0].decode("utf-8"))


def test_the_server_round_trips_over_a_real_unix_socket(db_path, stub_capability, budget_ledger):
    stub_capability("replicate.generate", lambda request: {"artifact_path": "/tmp/x.png"})
    with EffectServer(db_path=db_path, budget_ledger=budget_ledger) as seam:
        assert seam.path
        answer = _round_trip(seam.path, _request())
        assert answer["outcome"] == OUTCOME_OK
        assert answer["result"] == {"artifact_path": "/tmp/x.png"}
        assert seam.served == [("replicate.generate", OUTCOME_OK)]
    assert not Path(seam.path or "/nonexistent").exists()


def test_the_socket_is_private_to_this_user(db_path, budget_ledger):
    with EffectServer(db_path=db_path, budget_ledger=budget_ledger) as seam:
        directory = Path(seam.path).parent
        assert directory.stat().st_mode & 0o077 == 0, "the seam directory must be 0700"
        # AF_UNIX sun_path is 104 bytes on macOS; a bind that would overflow it
        # must have been avoided, not discovered in production.
        assert len(seam.path.encode("utf-8")) < 104


def test_malformed_frames_are_refused_not_executed(db_path, stub_capability, budget_ledger):
    ran: list[int] = []
    stub_capability("replicate.generate", lambda request: ran.append(1) or {})
    with EffectServer(db_path=db_path, budget_ledger=budget_ledger) as seam:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(15.0)
        connection.connect(seam.path)
        connection.sendall(b"not json at all\n")
        answer = json.loads(connection.recv(65536).decode("utf-8").split("\n", 1)[0])
        connection.close()
    assert answer["outcome"] == OUTCOME_REFUSED
    assert answer["reason"] == "malformed_request"
    assert ran == []


def test_an_oversized_request_is_refused_not_buffered(db_path, stub_capability, budget_ledger):
    ran: list[int] = []
    stub_capability("replicate.generate", lambda request: ran.append(1) or {})
    with EffectServer(db_path=db_path, budget_ledger=budget_ledger) as seam:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(15.0)
        connection.connect(seam.path)
        try:
            connection.sendall(b"x" * (loop_effects.MAX_REQUEST_BYTES + 4096))
            answer = json.loads(connection.recv(65536).decode("utf-8").split("\n", 1)[0])
        finally:
            connection.close()
    assert answer["reason"] == "request_too_large"
    assert ran == []


def test_validate_args_refuses_a_shape_it_has_no_validator_for():
    with pytest.raises(loop_effects.SeamRefused):
        loop_effects._validate_args("x.y", {"a": ArgSpec(kind="mystery")}, {"a": 1})


# --------------------------------------------------------------------------
# K10 — the residual audit-coverage gaps
# --------------------------------------------------------------------------


def _model_request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "v": 1,
        "instance": "render_probe",
        "capability": "model.complete",
        "args": {
            "messages": [{"role": "user", "content": "one word"}],
            "purpose": "audit-coverage-probe",
        },
    }
    payload.update(overrides)
    return payload


def test_the_unbrokered_model_path_still_leaves_an_attempt_record(
    db_path, stub_capability, budget_ledger
):
    """``model.complete`` is U-R9's sanctioned NON-broker path.

    Its spend is llm-ledger governed rather than grant governed, which is why
    the broker never sees it — and why, after U-A1 moved the attempt record
    inside the broker, it stopped appearing in ``broker_calls`` at all. "What
    did my agents do overnight" lost the one capability that runs on every
    tick. The seam writes it in its own ``method="seam"`` vocabulary, so it can
    never be mistaken for a brokered decision.
    """
    stub_capability("model.complete", lambda request: {"text": "ok", "cost": {}})

    answer = execute(_model_request(), db_path=db_path, budget_ledger=budget_ledger)
    assert answer["outcome"] == OUTCOME_OK

    rows = _calls(db_path)
    assert len(rows) == 1, f"expected exactly one seam row, got {len(rows)}"
    assert rows[0]["capability_id"] == "model.complete"
    assert rows[0]["method"] == "seam"
    assert rows[0]["decision"] == "allowed"
    assert rows[0]["allowed"] == 1
    assert rows[0]["holder"] == "loop:render_probe"


def test_a_failing_unbrokered_model_call_is_recorded_with_its_outcome(
    db_path, stub_capability, budget_ledger
):
    """The refusal half of the same gap, and still exactly one row."""

    def refuse(request: SeamRequest) -> dict[str, Any]:
        raise loop_effects.SeamRefused("model_refused", "the provider said no")

    stub_capability("model.complete", refuse)
    answer = execute(_model_request(), db_path=db_path, budget_ledger=budget_ledger)
    assert answer["outcome"] == OUTCOME_REFUSED

    rows = _calls(db_path)
    assert len(rows) == 1
    assert rows[0]["reason"] == "model_refused"
    assert rows[0]["method"] == "seam"
    assert rows[0]["decision"] == OUTCOME_REFUSED


def test_a_brokered_capability_does_not_also_get_a_seam_outcome_row(
    db_path, stub_capability, budget_ledger, monkeypatch
):
    """The dedup that makes the row above safe to add.

    If the seam recorded outcomes for brokered capabilities too, every
    ``replicate.generate`` would produce three rows and U-A1's whole point
    would be undone.
    """
    import httpx

    class _Response:
        status_code = 200
        is_success = True
        text = ""

        @staticmethod
        def json() -> dict[str, str]:
            return {"id": "generated-test-prediction", "status": "succeeded", "output": []}

    monkeypatch.setenv("REPLICATE_API_TOKEN", "generated-in-test")
    monkeypatch.setattr(httpx, "request", lambda *_a, **_kw: _Response())
    stub_capability(
        "replicate.generate",
        lambda request: (
            loop_effects._broker_call(
                "replicate.generate",
                audit_db_path=request.db_path,
                holder=f"loop:{request.instance_id}",
                method="POST",
                path="/models/black-forest-labs/flux-schnell/predictions",
                body={"input": {"prompt": "generated in test"}},
            )
            and {"artifact_path": "/tmp/x.png"}
        ),
    )

    assert execute(_request(), db_path=db_path, budget_ledger=budget_ledger)["outcome"] == (
        OUTCOME_OK
    )

    rows = _calls(db_path)
    assert [row["decision"] for row in sorted(rows, key=lambda r: r["id"])] == [
        "intent",
        "allowed",
    ]
    assert [row["method"] for row in rows] != ["seam", "seam"]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        # Malformed capability id: rejected by the surface regex.
        ({"capability": "not.a.capability"}, "bad_capability"),
        # Unknown capability: well-formed, but never in CAPABILITIES at all.
        ({"capability": "fixture.unknown"}, "unknown_capability"),
        # Source floor: the instance does not hold this capability.
        ({"instance": "w2_inbox_triage"}, "not_granted"),
        # Argument schema: rejected before anything is reserved or called.
        ({"args": "not-an-object"}, "invalid_arguments"),
    ],
)
def test_each_pre_broker_refusal_class_writes_exactly_one_seam_row(
    db_path, budget_ledger, payload, reason
):
    """One row per refusal, counted — not ``denied[0]`` on a DESC-ordered log.

    Asserting the first row of a descending log cannot see a duplicate, which
    is the failure mode this parametrization exists to catch. ``COUNT(*)`` can.
    """
    answer = execute(_request(**payload), db_path=db_path, budget_ledger=budget_ledger)
    assert answer["reason"] == reason

    rows = _calls(db_path)
    assert len(rows) == 1, f"{reason}: expected exactly one row, got {len(rows)}"
    assert rows[0]["method"] == "seam"
    assert rows[0]["decision"] == "refused"
    assert rows[0]["reason"] == reason
    assert rows[0]["allowed"] == 0


def test_a_gate_two_refusal_writes_exactly_one_seam_row(db_path, stub_capability, budget_ledger):
    """The class that needs a stubbed action_class to reach."""
    stub_capability(
        "replicate.generate",
        lambda request: {},
        action_class=ActionClass.CONSEQUENTIAL,
    )
    answer = execute(_request(), db_path=db_path, budget_ledger=budget_ledger)
    assert answer["reason"] == "requires_human_approval"

    rows = _calls(db_path)
    assert len(rows) == 1
    assert rows[0]["decision"] == "refused"
    assert rows[0]["method"] == "seam"


def test_a_budget_refusal_writes_exactly_one_seam_row(db_path, stub_capability):
    """``BudgetRefused`` had no coverage at all, despite writing a row.

    A zero cap refuses the reservation before the call is built, which is the
    production path at loop_effects' budget gate.
    """
    ran: list[int] = []
    stub_capability("replicate.generate", lambda request: ran.append(1) or {})
    broke = LoopBudgetLedger(db_path, instance_caps={"render_probe": 0.0})

    answer = execute(_request(), db_path=db_path, budget_ledger=broke)

    assert answer["outcome"] == OUTCOME_REFUSED
    assert ran == [], "the capability must not run when the reservation is refused"
    rows = _calls(db_path)
    assert len(rows) == 1, f"expected exactly one row, got {len(rows)}"
    assert rows[0]["method"] == "seam"
    assert rows[0]["decision"] == "refused"
    assert rows[0]["allowed"] == 0
