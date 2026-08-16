"""wq-server — the standalone FastAPI transport in front of :class:`WorkQueueStore`.

A separate process on a separate port (:8487 by default, loopback bind), never
mounted into ``omniagentos/api/main.py``: that app is a live serving surface with
~48 file-touches a week and this service's blast radius against it must be zero.

This module is a TRANSPORT. Every route is a one-line call into the store; no
decision, no state machine and no retry policy lives here. The three things it
does own are the ones that only make sense at the edge:

* the asyncio reaper, ticking every ``lease.reaper_interval_s`` seconds — one
  reaper on the primary, so a crashed worker's unit is requeued without anyone
  watching;
* alert TRANSPORT: the store returns an alert payload exactly when it won the
  one-alert CAS, and this process hands it to ``omniagentos.workqueue.alert``
  (Lane B). The import is guarded so the server keeps working before that module
  exists and if it is ever removed;
* the machine-role enforcement MODE (D8a, ``WQ_ROLE_ENFORCE``). The role
  DECISION is still the store's — ``claim_role_refusal`` owns the role column and
  the allowlist — and what lives here is only whether a refusal becomes a 403 or
  a log line. That split is the point: the rollout flips the mode on a running
  fleet without any change to the authz logic, and log-only mode leaves the claim
  path byte-identical to what it was before the flag existed.

Auth: bearer ``WQ_TOKEN`` compared with :func:`secrets.compare_digest` on every
route except ``GET /v1/health``. An unset token fails CLOSED — every authed route
answers 401 — because a queue that accepts anonymous claims on the LAN is worse
than a queue that is down.

The refusal WRITE routes (``POST /v1/refusals`` and
``POST /v1/refusals/{input_key}/clear``) were implemented here before the
contract described them, and were added to ``contract.schema.json`` on
2026-08-11 so the frozen file matches what actually serves. They exist because
:class:`~omniagentos.workqueue.client.HttpQueueClient` must expose the SAME
method surface as the store — which includes ``refusal_record`` and
``refusal_clear`` — and the pool-wide refusal ledger is the entire multi-machine
point of §4. Without them a remote gate would keep its refusals on its own box
and Mac B would happily re-run what Mac A refused five times.

Attribution rides through untouched: ``submitted_by`` is one more key in the
``POST /v1/units`` body, which this transport hands to the store verbatim, and
``offloads`` is one more key in the status payload it returns verbatim. A
transport that filtered either would be making a decision, which is the one
thing this module does not do.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from omniagentos.workqueue.schema import LeaseLost
from omniagentos.workqueue.store import WorkQueueStore, _default_config_path

__all__ = ["create_app", "main"]

#: Route bodies stay untyped dicts on purpose: contract.schema.json is the
#: authority for these shapes, and a second (pydantic) copy of it here would
#: be a second thing to keep in sync — and the one that silently wins.
JsonBody = Annotated[dict[str, Any], Body()]
OptionalJsonBody = Annotated[dict[str, Any] | None, Body()]

DEFAULT_PORT = 8487
DEFAULT_BIND = "127.0.0.1"
DEFAULT_DB = "var/workqueue.sqlite3"
DEFAULT_TOKEN_ENV = "WQ_TOKEN"
DEFAULT_REAPER_INTERVAL_S = 60

#: Machine-role enforcement (D8a). '1' = live 403s; unset or anything else =
#: LOG-ONLY, where a would-be refusal is printed and the claim proceeds exactly
#: as it does today. Default log-only because this is queue AUTHZ on a running
#: fleet: the failure mode of getting the allowlist wrong is every worker going
#: idle at once, and the only honest way to find that out is to watch the pool
#: produce zero would-deny lines for 72 h first (RUNBOOK §12).
ROLE_ENFORCE_ENV = "WQ_ROLE_ENFORCE"


def _load_config(config_path: str | Path | None) -> dict[str, Any]:
    path = Path(config_path) if config_path else _default_config_path()
    try:
        import yaml

        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _envelope(status: int, code: str, message: str, detail: Any = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "detail": detail}},
    )


#: What a client is told when the STORE raises. The store writes its messages for
#: an operator standing next to the DB: they carry unit ids, absolute paths,
#: schema constraints and remedies, and relaying them verbatim turns every 400
#: into a free map of the primary's filesystem and internals for anyone who can
#: reach the port. The full text goes to stderr instead — the status code and the
#: code string are the stable, machine-readable part, and they are unchanged.
_STORE_ERROR_MESSAGES = {
    400: "the queue rejected this request — see the wq-server log for the reason",
    404: "no such object",
}


def _role_enforced() -> bool:
    """Read ``WQ_ROLE_ENFORCE`` per request, not once at import.

    Per request because the value is read in exactly one place on one route, so
    the cost is a dict lookup, and because a snapshot taken at import time would
    make the flip untestable without a second process and un-revertable without a
    restart during the one window where reverting fast matters most.

    ONLY the exact string '1' enables enforcement. Unset, '0', '', 'true', 'yes'
    and every typo mean log-only: a flag that turns on a fleet-wide 403 must be
    turned on deliberately, and generous truthiness is how a stray value in a
    launchd plist idles the pool.
    """
    return os.environ.get(ROLE_ENFORCE_ENV, "") == "1"


def _log_role_refusal(machine_id: str, worker_id: str, reason: str, *, enforced: bool) -> None:
    """One structured line per role refusal — the entire log-only mode.

    This line IS the 72 h preflight evidence (RUNBOOK §12): the operator greps
    ``wq-role-deny`` and flips the flag only once the count has been zero for
    three clean days. Fixed ``key=value`` order so grep/awk over it stays stable;
    ``mode`` distinguishes a rehearsal from a live refusal in the same stream.
    """
    mode = "enforce" if enforced else "log-only"
    try:
        print(
            f"[wq-server] wq-role-deny mode={mode} machine_id={machine_id!r} "
            f"worker_id={worker_id!r} reason={reason}",
            file=sys.stderr,
        )
    except OSError:
        # A broken/full/closed stderr (log rotation, disk-full, closed pipe on a
        # long-lived server) must NEVER turn a log-only would-deny into a 500 —
        # that would break the "log-only leaves the claim path byte-identical"
        # promise and block a legitimate worker on a logging failure. The line is
        # 72h-preflight evidence, not a control; losing one is acceptable, failing
        # the claim is not. (Cross-lineage review: gemini/sol F5, 2026-08-13.)
        pass


def _log_store_error(request: Request, status: int, exc: BaseException) -> None:
    print(
        f"[wq-server] {status} {request.method} {request.url.path}: {type(exc).__name__}: {exc}",
        file=sys.stderr,
    )


async def require_token(request: Request, authorization: str | None = Header(default=None)) -> None:
    """Constant-time bearer check. Unset server token = fail closed."""
    expected = getattr(request.app.state, "token", "") or ""
    presented = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            presented = value.strip()
    if not expected or not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def create_app(
    store: WorkQueueStore | None = None,
    *,
    db_path: str | Path | None = None,
    config_path: str | Path | None = None,
    token: str | None = None,
    reaper: bool = True,
) -> FastAPI:
    config = _load_config(config_path)
    server_cfg = config.get("server") or {}
    lease_cfg = config.get("lease") or {}
    token_env = str(server_cfg.get("token_env") or DEFAULT_TOKEN_ENV)
    resolved_token = token if token is not None else os.environ.get(token_env, "")
    reaper_interval = int(lease_cfg.get("reaper_interval_s") or DEFAULT_REAPER_INTERVAL_S)
    resolved_db = str(db_path or server_cfg.get("db_path") or DEFAULT_DB)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if reaper:
            task = asyncio.create_task(_reaper_loop(app, reaper_interval))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="wq-server", version="1", lifespan=lifespan)
    app.state.store = (
        store if store is not None else WorkQueueStore(resolved_db, config_path=config_path)
    )
    app.state.token = resolved_token
    app.state.token_env = token_env

    @app.exception_handler(LeaseLost)
    async def _lease_lost(_request: Request, exc: LeaseLost) -> JSONResponse:
        # 409 is the wire form of "you were adopted, stop". The worker kills its
        # child tree on this and writes no result.
        return _envelope(409, "lease-lost", str(exc) or "lease lost")

    @app.exception_handler(ValueError)
    async def _bad_request(request: Request, exc: ValueError) -> JSONResponse:
        # Generic on the wire, complete in the log. Status and code keep their
        # meaning, so every client branch that mattered still works.
        _log_store_error(request, 400, exc)
        return _envelope(400, "bad-request", _STORE_ERROR_MESSAGES[400])

    @app.exception_handler(KeyError)
    async def _not_found(request: Request, exc: KeyError) -> JSONResponse:
        _log_store_error(request, 404, exc)
        return _envelope(404, "not-found", _STORE_ERROR_MESSAGES[404])

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return _envelope(exc.status_code, _code_for(exc.status_code), str(exc.detail))

    authed = [Depends(require_token)]

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/v1/status", dependencies=authed)
    async def status(request: Request) -> dict[str, Any]:
        return _store(request).status()

    @app.post("/v1/units", dependencies=authed)
    async def create_unit(request: Request, submit: JsonBody) -> dict[str, Any]:
        unit_id, deduped = _store(request).enqueue(submit)
        return {"id": unit_id, "deduped": deduped}

    @app.post("/v1/claim", dependencies=authed)
    async def claim(request: Request, body: JsonBody) -> Any:
        machine_id = str(body["machine_id"])
        worker_id = str(body["worker_id"])

        # ROLE GATE (D8a). The decision is the store's — it owns the role column
        # and the allowlist — and only the MODE is decided here, because the mode
        # is a rollout concern that must change without touching authz logic.
        # The gate runs BEFORE the claim so that in enforce mode no lease, no
        # attempt row and no generation bump ever happens for a refused machine.
        refusal = _store(request).claim_role_refusal(machine_id)
        if refusal is not None:
            enforced = _role_enforced()
            _log_role_refusal(machine_id, worker_id, refusal, enforced=enforced)
            if enforced:
                # 403, not 401: the token was fine, this machine is not permitted
                # to execute. The reason string is safe to relay — unlike a store
                # message it names only what the caller already told us — and the
                # worker needs it to say something truthful in its own log.
                raise HTTPException(status_code=403, detail=refusal)
            # Log-only: fall through and claim exactly as before the flag existed.

        # reserved_for is WORKER-DECLARED resource hygiene, not a server config.
        # Coerce with isinstance(str): a non-str/missing value is treated as
        # absent (unrestricted), never crashes — there is no config-map left for
        # a malformed value to DoS. A reserved worker can only claim FEWER units.
        reserved_for = body.get("reserved_for")
        claimed = _store(request).claim(
            machine_id,
            worker_id,
            list(body.get("labels") or []),
            reserved_for=reserved_for if isinstance(reserved_for, str) else None,
        )
        if claimed is None:
            return Response(status_code=204)
        return claimed

    @app.post("/v1/units/{unit_id}/heartbeat", dependencies=authed)
    async def heartbeat(request: Request, unit_id: str, body: JsonBody) -> dict[str, Any]:
        _store(request).heartbeat(unit_id, str(body["owner"]), int(body["lease_generation"]))
        return {"ok": True}

    @app.post("/v1/units/{unit_id}/result", dependencies=authed)
    async def result(request: Request, unit_id: str, body: JsonBody) -> dict[str, Any]:
        outcome = _store(request).record_result(
            unit_id,
            str(body["owner"]),
            int(body["lease_generation"]),
            str(body["outcome"]),
            exit_code=body.get("exit_code"),
            gate_exit_code=body.get("gate_exit_code"),
            input_key=body.get("input_key"),
            retryable=body.get("retryable"),
            remedy=body.get("remedy"),
            head_sha=body.get("head_sha"),
            result_branch=body.get("result_branch"),
            result_sha=body.get("result_sha"),
            log_path=body.get("log_path"),
        )
        _send_alert(outcome.get("alert"))
        return {"ok": True, "unit": outcome.get("unit"), "alert": outcome.get("alert")}

    @app.post("/v1/units/{unit_id}/cancel", dependencies=authed)
    async def cancel(request: Request, unit_id: str) -> dict[str, Any]:
        _store(request).cancel(unit_id)
        return {"ok": True}

    @app.post("/v1/units/{unit_id}/requeue", dependencies=authed)
    async def requeue(request: Request, unit_id: str) -> dict[str, Any]:
        # SOFT parks only, and no body: there is nothing to say, because nothing
        # ran. A terminal park raises ValueError → 400 pointing at unpark, and an
        # unknown unit raises KeyError → 404, both through the shared envelope.
        _store(request).requeue(unit_id)
        return {"ok": True}

    @app.post("/v1/units/{unit_id}/unpark", dependencies=authed)
    async def unpark(request: Request, unit_id: str, body: JsonBody) -> dict[str, Any]:
        _store(request).unpark(unit_id, str(body.get("because") or ""))
        return {"ok": True}

    @app.get("/v1/units/{unit_id}", dependencies=authed)
    async def get_unit(request: Request, unit_id: str) -> dict[str, Any]:
        store_ = _store(request)
        unit = store_.get_unit(unit_id)
        if unit is None:
            raise HTTPException(status_code=404, detail=f"unknown unit: {unit_id}")
        return {"unit": unit, "attempts": store_.list_attempts(unit_id)}

    @app.post("/v1/machines", dependencies=authed)
    async def enroll(request: Request, body: JsonBody) -> dict[str, Any]:
        _store(request).enroll_machine(body)
        return {"ok": True}

    @app.get("/v1/machines", dependencies=authed)
    async def machines(request: Request) -> dict[str, Any]:
        return {"machines": _store(request).list_machines()}

    @app.post("/v1/machines/{machine_id}/beat", dependencies=authed)
    async def beat(request: Request, machine_id: str, body: JsonBody) -> dict[str, Any]:
        return _store(request).machine_beat(machine_id, body)

    @app.post("/v1/machines/{machine_id}/drain", dependencies=authed)
    async def drain(
        request: Request, machine_id: str, body: OptionalJsonBody = None
    ) -> dict[str, Any]:
        undo = bool((body or {}).get("undo", False))
        _store(request).set_drain(machine_id, not undo)
        return {"ok": True}

    @app.get("/v1/refusals/{input_key}", dependencies=authed)
    async def refusal(request: Request, input_key: str, gate: str) -> dict[str, Any]:
        row = _store(request).refusal_check(input_key, gate)
        if row is None:
            raise HTTPException(status_code=404, detail="no refusal recorded")
        return row

    @app.post("/v1/refusals", dependencies=authed)
    async def record_refusal(request: Request, body: JsonBody) -> dict[str, Any]:
        row = _store(request).refusal_record(
            str(body["input_key"]),
            str(body["gate"]),
            str(body["refusal_class"]),
            int(body["retryable"]),
            str(body.get("remedy") or ""),
            unit_id=body.get("unit_id"),
        )
        _send_alert(row.get("alert"))
        return row

    @app.post("/v1/refusals/{input_key}/clear", dependencies=authed)
    async def clear_refusal(request: Request, input_key: str, gate: str) -> dict[str, Any]:
        _store(request).refusal_clear(input_key, gate)
        return {"ok": True}

    @app.get("/v1/alerts", dependencies=authed)
    async def alerts(request: Request) -> dict[str, Any]:
        return {"alerts": _store(request).alerts()}

    return app


def _store(request: Request) -> WorkQueueStore:
    return request.app.state.store


def _code_for(status: int) -> str:
    return {
        400: "bad-request",
        401: "unauthorized",
        # The envelope CODE for a role refusal is generic; the NAMED reason
        # ('dispatcher-role-cannot-claim' / 'machine-not-allowlisted') rides in
        # the envelope's message, which is where a client branches on it.
        403: "forbidden",
        404: "not-found",
        409: "lease-lost",
    }.get(status, "error")


def _send_alert(payload: dict[str, Any] | None) -> None:
    """Hand a won-CAS alert to Lane B's transport. Never raises, never blocks a result."""
    if not payload:
        return
    try:
        from omniagentos.workqueue.alert import send_alert
    except ImportError:
        print(f"[wq-server] ALERT (no transport): {payload}", file=sys.stderr)
        return
    try:
        send_alert(payload)
    except Exception as error:  # pragma: no cover - send_alert is contractually fail-soft
        print(f"[wq-server] alert transport failed: {error!r} payload={payload}", file=sys.stderr)


async def _reaper_loop(app: FastAPI, interval_s: int) -> None:
    """One reaper on the primary. A tick that raises must not kill the loop."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            reclaimed = await asyncio.to_thread(app.state.store.reap_expired)
        except Exception as error:  # pragma: no cover - defensive
            print(f"[wq-server] reaper tick failed: {error!r}", file=sys.stderr)
            continue
        if reclaimed:
            print(f"[wq-server] reaper reclaimed {reclaimed} expired lease(s)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="wq-server")
    parser.add_argument(
        "--db", default=None, help="queue DB path (default from configs/workqueue.yaml)"
    )
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    config = _load_config(args.config)
    server_cfg = config.get("server") or {}
    host = args.host or str(server_cfg.get("bind") or DEFAULT_BIND)
    port = args.port or int(server_cfg.get("port") or DEFAULT_PORT)
    app = create_app(db_path=args.db, config_path=args.config)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
