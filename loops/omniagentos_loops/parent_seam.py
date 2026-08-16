"""Worker side of the credential seam: declare an effect, never hold a secret.

The worker is launched with an environment from which every credential-shaped
name has been deleted (``loop_jobs._worker_env``), so a loop tool cannot resolve
a connector secret and must not try. What it does instead is *declare*: it names
a typed capability id and typed arguments, and the scheduler process — which
already holds those credentials legitimately, and is where ``_deliver_page``
already posts to Slack for exactly this reason — decides whether the loop may,
performs the call, and hands back the result.

WHAT THIS MODULE IS NOT
-----------------------

It is not a second execution seam. Everything that governs an effect still runs
here, before this client is ever reached: ``policy_gate`` derives the verdict,
``execute_effect`` re-reads a T2+ approval, ``receipts.guarded`` claims the
idempotency row and records the OUTCOME, and ``LoopTool.verify`` decides whether
the effect took effect. This module is what a tool's ``call`` *does*; it changes
where the credentialed request is issued and nothing else.

THE FOUR ANSWERS
----------------

``ok``           the result, returned to the tool.
``refused``      an authority was reached and said no. Returned as a
                 self-declaring failure mapping, so ``receipts.declared_failure``
                 records a FAILED attempt, the retry budget applies, and the
                 template's verify node renders it. Adverse.
``unavailable``  the authority was never reached. Raises
                 :class:`~omniagentos_loops.contracts.EffectUnavailable`, which
                 the receipt guard treats as ABSENCE: the claim is released
                 (nothing happened, so no receipt should survive) and the tick
                 settles NEUTRAL and loud, out of the acceptance denominator.
``unknown``      a request may have been issued and its fate is not established.
                 Raises ``EffectStateUnknown`` — the receipt stays claimed and
                 the next tick refuses to re-run, unchanged from a crash.

The narrow definition of ``unavailable`` is the whole safety argument: the
parent claims it only where it can show no request reached the outside world,
and this client claims it only where it can show no request left this process.
Everything either side cannot prove is ``unknown``.

PROTOCOL DUPLICATION, ON PURPOSE
--------------------------------

The constants below mirror ``omniagentos.scheduler.loop_effects``. Same reason
that module mirrors ``omniagentos_loops.paths.SAFE_NAME_RE`` rather than
importing it: the two run in different venvs and the boundary between them is a
wire, not an import. CRITICAL: ``ARTIFACT_NAME_RE`` is duplicated by design
so the worker can early-validate before sending to the parent, BUT the parent
side (omniagentos.scheduler.loop_effects:150) is the AUTHORITATIVE definition
and ALWAYS re-validates every name. Drift between the two definitions breaks
containment — ``tests/test_parent_seam.py::test_worker_and_parent_agree_on_the_protocol``
pins the duplication so it cannot drift.
"""

from __future__ import annotations

import json
import os
import re
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omniagentos_loops.contracts import EffectStateUnknown, EffectUnavailable

SEAM_PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 512 * 1024

OUTCOME_OK = "ok"
OUTCOME_REFUSED = "refused"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_UNKNOWN = "unknown"

ARTIFACT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\.(png|jpg|jpeg|webp|html|htm|txt|md|json|csv)$")

REPLICATE_GENERATE = "replicate.generate"
MODEL_COMPLETE = "model.complete"

#: Generous: the parent's own deadline is what really bounds a call, and a
#: client that gives up first turns a completed effect into an UNKNOWN.
CLIENT_TIMEOUT_S = 300.0

#: Set once, from the worker's argv. Module state rather than an environment
#: variable so the socket path can never be inherited by a grandchild process.
_SOCKET_PATH: str = ""


def configure(socket_path: str) -> None:
    """Bind this worker to the seam the scheduler opened for THIS tick."""
    global _SOCKET_PATH
    _SOCKET_PATH = str(socket_path or "")


def socket_path() -> str:
    return _SOCKET_PATH


def available() -> bool:
    return bool(_SOCKET_PATH)


def artifact_root(var_dir: Path, instance_id: str) -> Path:
    """``<var>/loops/artifacts/<instance>``. Mirrors ``loop_effects.artifact_root``."""
    return var_dir / "loops" / "artifacts" / instance_id


def artifact_path(var_dir: Path, instance_id: str, artifact_name: str) -> Path:
    """Where the parent WILL have written *artifact_name* for *instance_id*.

    Pure, and derived from ARGUMENTS alone. That is what lets a verification
    predicate find the artifact without reading a single field of the actor's
    answer (Rule E: the actor's narrative is never the verdict).
    """
    if not ARTIFACT_NAME_RE.match(artifact_name):
        raise ValueError(f"illegal artifact name {artifact_name!r}")
    return artifact_root(var_dir, instance_id) / artifact_name


def var_dir() -> Path:
    """The runtime root both sides resolve the artifact path against."""
    configured = os.environ.get("OMNIAGENTOS_VAR_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser()
    from omniagentos_loops import REPO_ROOT

    return REPO_ROOT / "var"


def _refusal(response: Mapping[str, Any]) -> dict[str, Any]:
    """A reached-and-refused answer, shaped so the receipt records it as failed."""
    return {
        "success": False,
        "error": str(response.get("detail") or response.get("reason") or "refused"),
        "seam_outcome": OUTCOME_REFUSED,
        "seam_reason": str(response.get("reason") or ""),
    }


def request_effect(instance_id: str, capability: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """Ask the parent to perform one credentialed effect. See the module docstring.

    Returns the parent's ``result`` on success, or a self-declaring failure
    mapping on a refusal. Raises ``EffectUnavailable`` (absence) or
    ``EffectStateUnknown`` (fail closed) — never both meanings in one value.
    """
    if not _SOCKET_PATH:
        raise EffectUnavailable(
            "no_seam",
            f"{capability}: this worker was started without a parent effect seam, so no "
            "credentialed call is possible — nothing was attempted",
        )

    request = json.dumps(
        {
            "v": SEAM_PROTOCOL_VERSION,
            "instance": instance_id,
            "capability": capability,
            "args": dict(args),
        },
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    if len(request) > MAX_REQUEST_BYTES:
        # Refusing here rather than sending is not merely polite: nothing left
        # this process, so it is a clean adverse answer instead of an UNKNOWN.
        return _refusal({"reason": "request_too_large", "detail": "request exceeds the seam cap"})

    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(CLIENT_TIMEOUT_S)
    try:
        connection.connect(_SOCKET_PATH)
    except OSError as exc:
        connection.close()
        # Provably nothing was sent. ABSENCE.
        raise EffectUnavailable(
            "seam_unreachable",
            f"{capability}: the parent effect seam did not accept a connection ({exc})",
        ) from exc

    try:
        try:
            connection.sendall(request + b"\n")
        except OSError as exc:
            # The request was partially written; the parent may have parsed it.
            raise EffectStateUnknown(
                f"{capability}: the seam request could not be delivered intact ({exc}) — "
                "refusing to assume it did not run"
            ) from exc

        chunks: list[bytes] = []
        size = 0
        while b"\n" not in b"".join(chunks[-1:]):
            try:
                chunk = connection.recv(65536)
            except (TimeoutError, OSError) as exc:
                raise EffectStateUnknown(
                    f"{capability}: the parent seam did not answer ({type(exc).__name__}) — "
                    "the effect may have run; refusing to re-run"
                ) from exc
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise EffectStateUnknown(
                    f"{capability}: the parent seam answer exceeded {MAX_RESPONSE_BYTES} bytes"
                )
            chunks.append(chunk)
    finally:
        connection.close()

    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise EffectStateUnknown(
            f"{capability}: the parent seam closed without answering — the effect may have run"
        )
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EffectStateUnknown(f"{capability}: unparseable seam answer ({exc})") from exc
    if not isinstance(response, Mapping) or int(response.get("v") or 0) != SEAM_PROTOCOL_VERSION:
        raise EffectStateUnknown(
            f"{capability}: seam answer is not protocol v{SEAM_PROTOCOL_VERSION}"
        )

    outcome = str(response.get("outcome") or "")
    if outcome == OUTCOME_OK:
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise EffectStateUnknown(f"{capability}: seam reported ok with no result object")
        return dict(result)
    if outcome == OUTCOME_REFUSED:
        return _refusal(response)
    if outcome == OUTCOME_UNAVAILABLE:
        raise EffectUnavailable(
            str(response.get("reason") or "unavailable"),
            f"{capability}: {response.get('detail') or 'the parent could not reach its authority'}",
        )
    # OUTCOME_UNKNOWN and anything this client does not recognise. An
    # unrecognised outcome is emphatically not a success.
    raise EffectStateUnknown(
        f"{capability}: seam outcome {outcome!r} — {response.get('detail') or 'no detail'}"
    )


__all__ = [
    "ARTIFACT_NAME_RE",
    "CLIENT_TIMEOUT_S",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "MODEL_COMPLETE",
    "OUTCOME_OK",
    "OUTCOME_REFUSED",
    "OUTCOME_UNAVAILABLE",
    "OUTCOME_UNKNOWN",
    "REPLICATE_GENERATE",
    "SEAM_PROTOCOL_VERSION",
    "artifact_path",
    "artifact_root",
    "available",
    "request_effect",
    "configure",
    "socket_path",
    "var_dir",
]
