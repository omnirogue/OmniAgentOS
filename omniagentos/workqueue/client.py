"""HttpQueueClient — the same method surface as :class:`WorkQueueStore`, over HTTP.

The point of this class is that Phase 1 upgrades to Phase 2 without rewriting a
line of claim logic: the worker takes either a store or a client and cannot tell
which it has. Everything that is a decision stays on the primary, next to the
one SQLite file that all the guarantees depend on.

Two behaviours are load-bearing:

* ``409`` raises :class:`LeaseLost`, exactly as the local fenced UPDATE does.
  The worker's reaction (kill the child tree, record nothing) must not depend on
  which transport it happened to be given.
* every call has a 10 s timeout. A hung primary must surface as an INSTRUMENT
  fact on the worker's next classify pass, not as a worker that waits forever
  holding a slot.

``reap_expired`` is deliberately NOT available over HTTP: there is exactly one
reaper and it runs inside the server process (SPEC §3.4).

Transport is stdlib ``urllib`` rather than ``httpx`` (which is in this repo's
venv): a joining machine is stateless apart from its git mirrors and its logs
(§5.2), so the fewer third-party imports the worker path needs on a fresh Linux
box, the fewer ways enrollment can fail with something that is not about the
queue.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from omniagentos.workqueue.schema import LeaseLost

__all__ = ["HttpQueueClient", "QueueServerError", "LeaseLost"]

DEFAULT_TIMEOUT_S = 10.0


class QueueServerError(RuntimeError):
    """Non-409 error from wq-server, carrying the {"error": {...}} envelope."""

    def __init__(self, status: int, code: str, message: str, detail: Any = None) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


class HttpQueueClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token if token is not None else os.environ.get("WQ_TOKEN", "")
        self.timeout_s = timeout_s

    # -- transport --------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode({k: v for k, v in params.items() if v is not None})}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url=url, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                if response.status == 204:
                    return None
                payload = response.read()
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                envelope = json.loads(raw) if raw else {}
            except ValueError:
                envelope = {}
            info = envelope.get("error") if isinstance(envelope, dict) else None
            code = str((info or {}).get("code") or "error")
            message = str((info or {}).get("message") or error.reason)
            if error.code == 409:
                raise LeaseLost(message) from error
            if error.code == 404 and method == "GET":
                # "no such unit / no refusal recorded" is an ANSWER on the read
                # paths (None), and a real error on the write paths.
                return None
            raise QueueServerError(error.code, code, message, (info or {}).get("detail")) from error

    # -- units ------------------------------------------------------------

    def enqueue(self, submit: dict[str, Any]) -> tuple[str, bool]:
        # The submit dict goes over the wire AS GIVEN — including the optional
        # ``submitted_by`` the CLI's --by fills in. Copying named fields here
        # would silently drop every key added to the contract afterwards, and
        # attribution is exactly the kind of field that would go missing.
        out = self._request("POST", "/v1/units", body=submit) or {}
        return str(out.get("id")), bool(out.get("deduped"))

    def claim(
        self,
        machine_id: str,
        worker_id: str,
        labels: list[str] | None,
        now: str | None = None,
        reserved_for: str | None = None,
    ) -> dict[str, Any] | None:
        # `now` is accepted for interface parity and deliberately not sent: the
        # server's clock is the only clock the lease is measured against.
        #
        # `reserved_for` is the worker's OWN self-restriction (resource hygiene,
        # never a security boundary — see store.claim). It is sent only when the
        # worker declared one; the server coerces a non-str/missing value to
        # "unrestricted", so a reserved worker can only ever claim FEWER units.
        body: dict[str, Any] = {
            "machine_id": machine_id,
            "worker_id": worker_id,
            "labels": list(labels or []),
        }
        if reserved_for is not None:
            body["reserved_for"] = reserved_for
        return self._request("POST", "/v1/claim", body=body)

    def heartbeat(
        self,
        unit_id: str,
        owner: str,
        lease_generation: int,
        now: str | None = None,
    ) -> None:
        self._request(
            "POST",
            f"/v1/units/{unit_id}/heartbeat",
            body={"owner": owner, "lease_generation": int(lease_generation)},
        )

    def record_result(
        self,
        unit_id: str,
        owner: str,
        lease_generation: int,
        outcome: str,
        *,
        exit_code: int | None = None,
        gate_exit_code: int | None = None,
        input_key: str | None = None,
        retryable: int | None = None,
        remedy: str | None = None,
        head_sha: str | None = None,
        result_branch: str | None = None,
        result_sha: str | None = None,
        log_path: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        out = (
            self._request(
                "POST",
                f"/v1/units/{unit_id}/result",
                body={
                    "owner": owner,
                    "lease_generation": int(lease_generation),
                    "outcome": outcome,
                    "exit_code": exit_code,
                    "gate_exit_code": gate_exit_code,
                    "input_key": input_key,
                    "retryable": retryable,
                    "remedy": remedy,
                    "head_sha": head_sha,
                    "result_branch": result_branch,
                    "result_sha": result_sha,
                    "log_path": log_path,
                },
            )
            or {}
        )
        # The SERVER sends the alert (it owns the transport); the payload comes
        # back only so a caller can log or assert on it.
        return {"unit": out.get("unit"), "alert": out.get("alert")}

    def cancel(self, unit_id: str) -> None:
        self._request("POST", f"/v1/units/{unit_id}/cancel")

    def requeue(self, unit_id: str) -> None:
        # No body and no reason: a soft park is "nothing ran", and the refusal
        # ledger deliberately keeps its count across it (§4.5). A terminal park
        # comes back 400 — use unpark(), which is the amnesty.
        self._request("POST", f"/v1/units/{unit_id}/requeue")

    def unpark(self, unit_id: str, because: str) -> None:
        self._request("POST", f"/v1/units/{unit_id}/unpark", body={"because": because})

    def get_unit(self, unit_id: str) -> dict[str, Any] | None:
        out = self._request("GET", f"/v1/units/{unit_id}")
        return (out or {}).get("unit") if out else None

    def list_attempts(self, unit_id: str) -> list[dict[str, Any]]:
        out = self._request("GET", f"/v1/units/{unit_id}")
        return list((out or {}).get("attempts") or [])

    def park(self, unit_id: str, terminal_reason: str, remedy: str, now: str | None = None) -> None:
        raise NotImplementedError(
            "park is a store-side decision; a worker reports an outcome and the "
            "store parks. Use record_result()."
        )

    def reap_expired(self, now: str | None = None) -> int:
        raise NotImplementedError(
            "the reaper runs in the server process — exactly one per pool (SPEC §3.4)"
        )

    # -- machines ---------------------------------------------------------

    def enroll_machine(self, m: dict[str, Any]) -> None:
        self._request("POST", "/v1/machines", body=m)

    def machine_beat(
        self, machine_id: str, beat: dict[str, Any], now: str | None = None
    ) -> dict[str, Any]:
        return self._request("POST", f"/v1/machines/{machine_id}/beat", body=beat) or {"drain": 0}

    def set_drain(self, machine_id: str, drain: bool) -> None:
        self._request("POST", f"/v1/machines/{machine_id}/drain", body={"undo": not drain})

    def list_machines(self) -> list[dict[str, Any]]:
        out = self._request("GET", "/v1/machines") or {}
        return list(out.get("machines") or [])

    # -- refusal ledger ---------------------------------------------------

    def refusal_check(self, input_key: str, gate: str) -> dict[str, Any] | None:
        return self._request("GET", f"/v1/refusals/{input_key}", params={"gate": gate})

    def refusal_record(
        self,
        input_key: str,
        gate: str,
        refusal_class: str,
        retryable: int,
        remedy: str,
        unit_id: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        return (
            self._request(
                "POST",
                "/v1/refusals",
                body={
                    "input_key": input_key,
                    "gate": gate,
                    "refusal_class": refusal_class,
                    "retryable": int(retryable),
                    "remedy": remedy,
                    "unit_id": unit_id,
                },
            )
            or {}
        )

    def refusal_clear(self, input_key: str, gate: str) -> None:
        self._request("POST", f"/v1/refusals/{input_key}/clear", params={"gate": gate})

    # -- observability ----------------------------------------------------

    def alerts(self) -> list[dict[str, Any]]:
        out = self._request("GET", "/v1/alerts") or {}
        return list(out.get("alerts") or [])

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/status") or {}
