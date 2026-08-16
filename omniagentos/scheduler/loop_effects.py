"""Parent-side execution of credentialed loop effects — the credential seam.

WHY THIS EXISTS
---------------

A loop worker holds no credentials, by design: ``loop_jobs._worker_env`` runs
the shared scrub and then a loops-only post-filter that deletes every remaining
credential-shaped name, and it re-applies that filter to its own enumerated
passthrough list precisely so a future edit cannot smuggle one in. That is the
right boundary and it is not being widened here.

The consequence is that ``broker.resolve_for`` — the capability-scoped parent
resolver — can never work inside the worker, so a loop can reach no
connector at all. Slack paging hit this first and was solved by moving the
credentialed call into the scheduler process (``loop_jobs._deliver_page``: "the
webhook URL *is* the credential; the scheduler already holds that secret
legitimately, so delivery belongs here"). This module generalises that one
precedent into a seam:

    the WORKER declares WHAT it wants — a typed capability id and typed
    arguments — and the PARENT decides whether it may, executes it with the
    credentials it already legitimately holds, and hands back the result.

``routines_settle``'s Class P docstring shapes the same idea for *verdicts*
(a credentialed probe executed parent-side at settlement). This is its
executive twin, and it obeys the same two rules that docstring makes
non-negotiable: the declaration is **per-instance, resolved by
``instance_id``, never read from a row** (see :data:`INSTANCE_CAPABILITIES`),
and an authority that could not be **reached** is ABSENCE, never failure (see
the outcome taxonomy below).

WHAT MOVED AND WHAT DID NOT
---------------------------

Only the *location of the credentialed call* moved. Everything that governs an
effect still runs in the worker, unchanged and on the same code path as before:
``policy_gate`` derives the verdict, a T2+ tool still parks for a human, the
idempotency receipt is still claimed before the call and completed with the
OUTCOME after it, ``LoopTool.verify`` still decides whether the effect took
effect, and the attempt-keyed retry budget still bounds it. A loop tool's
``call`` is simply a thin client that round-trips to this process.

The parent adds a SECOND, independent gate rather than trusting that one:

1. the capability id must be a member of :data:`CAPABILITIES` — a closed,
   in-code registry of typed identifiers. A free-text command string is never
   accepted, and there is no "run this shell/HTTP request" capability;
2. the instance must hold the capability in :data:`INSTANCE_CAPABILITIES`,
   which is keyed by ``instance_id`` and lives in source, not in the database;
3. the capability's ActionClass must be in ``connectors.AUTO_CLASSES``. Anything
   the broker classes as hard-human is refused HERE, with no config input, so a
   counterfeited template that reached this socket with no approval still
   executes nothing consequential;
4. arguments are validated field by field against a declared schema (type,
   bounds, closed choice sets, anchored patterns) before a handler sees them;
5. every brokered credential touch writes one intent/final pair from inside the
   broker. A seam that cannot persist intent refuses credential use, while the
   separately governed credential-free model path remains available. The seam
   no longer duplicates the broker's ALLOWED row — a caller vouching for its
   own call proves nothing — but it still records the refusals the broker
   structurally cannot see, i.e. the ones decided at gates 1-4 above, before
   any credential was reached (:func:`_audit_pre_broker_refusal`).

THE OUTCOME TAXONOMY (four values, and the third is the load-bearing one)
------------------------------------------------------------------------

=============  =========================================================
``ok``         the authority was reached and answered; ``result`` is real
``refused``    an authority was REACHED and said no (4xx, a broker denial,
               a budget refusal, an argument this seam will not accept).
               Adverse: it is a fact about the candidate.
``unavailable``the authority was NOT reached — dead credential, DNS,
               connection refused, no audit store. ABSENCE, never failure:
               it settles neutral and loud, out of the acceptance
               denominator, copying ``gate_evidence.GateWorkspaceUnusable``
               verbatim. "We could not ask" is not "the answer was no", and
               scoring it as failure is the defect that auto-paused four
               routines on 2026-07-31.
``unknown``    ambiguous — a request may have been issued and its fate is
               not established (a read timeout after the bytes went out, an
               unclassifiable exception). Fail CLOSED: the worker leaves its
               receipt claimed and the next tick refuses to re-run.
=============  =========================================================

``unavailable`` is deliberately NARROW. It is claimed only where this process
can show no request reached the external system; everything it cannot prove
falls to ``unknown``, because a wrongly-cheap ``unavailable`` would release an
idempotency claim for an effect that may have happened.

TRANSPORT
---------

One ``AF_UNIX`` stream socket per tick, created inside a ``0700`` temporary
directory, its path handed to the worker in **argv** (``--effect-socket``) —
not in the environment, so :data:`~omniagentos.scheduler.loop_jobs
._WORKER_ENV_PASSTHROUGH` is untouched and the credential-shape filter it
enforces is not asked to make an exception. The socket is destroyed when the
tick ends. No port is opened; the worker still "opens no port, serves nothing".
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniagentos.contracts import ActionClass
from omniagentos.runtime_paths import resolve_var_root
from omniagentos.scheduler.loop_budget import (
    BudgetRefused,
    LoopBudgetLedger,
    UnknownCostRefused,
)

logger = logging.getLogger(__name__)

#: Wire format version. A mismatch is refused rather than coerced: the worker
#: and the scheduler are separate venvs and can be separate deploys.
SEAM_PROTOCOL_VERSION = 1

#: Hard caps on the framing. A worker is a child of this process, but it is also
#: the least trusted code in it, so nothing it sends is unbounded.
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 512 * 1024

#: How long the parent will spend serving ONE request before giving up. The
#: worker's own socket timeout is deliberately larger (see
#: ``omniagentos_loops.parent_seam``), so a parent that gives up still gets to
#: answer rather than leaving the worker to guess.
DEFAULT_CALL_DEADLINE_S = 180.0

OUTCOME_OK = "ok"
OUTCOME_REFUSED = "refused"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_UNKNOWN = "unknown"

#: Capability ids are typed identifiers, never free text. ``<group>.<verb>``,
#: anchored, lowercase — the same discipline ``loop_jobs.SAFE_NAME_RE`` applies
#: to a routine row's template and instance names.
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

#: Mirrors ``loop_jobs.SAFE_NAME_RE`` / ``omniagentos_loops.paths.SAFE_NAME_RE``.
INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Artifact file names the seam will write. No separators, no dots leading, one
#: extension — the worker never gets to name a directory (see
#: :func:`artifact_path`), so this is the whole of its influence over the path.
#: Allowlist: image formats (png, jpg, jpeg, webp), documents (html, htm, txt, md,
#: json, csv). Enforced here; worker-side copy is a courtesy check only.
ARTIFACT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\.(png|jpg|jpeg|webp|html|htm|txt|md|json|csv)$")


class SeamError(Exception):
    """Base: carries the outcome class this failure settles as.

    TWO INDEPENDENT QUESTIONS
    -------------------------

    ``outcome`` answers *what should happen to the idempotency claim* — release
    it (absence), record it as an adverse attempt (refusal), or leave it
    claimed (unknown). ``may_have_billed`` answers a different question: *may a
    provider already have charged us for this work?* — which decides whether
    the open budget reservation is released or settled.

    They are not the same question and collapsing them leaks money in both
    directions. A prediction that Replicate rendered and billed, whose artifact
    this seam then refuses to download because the URL host is not allowlisted,
    is a REFUSAL whose money was spent. A connect-refused is an ABSENCE whose
    money provably was not. So the outcome supplies the DEFAULT and any raiser
    that can prove otherwise says so at the raise site.
    """

    outcome = OUTCOME_UNKNOWN
    #: Default for this class. ``True`` unless the outcome itself proves that
    #: no request ever reached a provider. Fail closed: we charge unless we can
    #: show we were not charged.
    may_have_billed = True

    def __init__(
        self, reason: str, detail: str = "", *, may_have_billed: bool | None = None
    ) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail
        if may_have_billed is not None:
            self.may_have_billed = may_have_billed


class SeamRefused(SeamError):
    """An authority was reached and said no. Adverse.

    Reached is not the same as unbilled: ``prediction_failed`` and every
    ``artifact_*`` refusal happen AFTER the provider did (and charged for) the
    work. A raiser that decided locally — the broker's own allowlist, the spend
    cap consulted before the socket was opened — passes
    ``may_have_billed=False`` and gets its reservation back.
    """

    outcome = OUTCOME_REFUSED


class SeamUnavailable(SeamError):
    """The authority was never reached. ABSENCE — never scored as failure.

    This is the ONLY outcome that releases the worker's idempotency claim, so
    it is the only one that can cause a paid call to be re-issued by the next
    tick. It may therefore be raised only where it can be SHOWN that no request
    reached the outside world: a refused TCP connection, a name that does not
    resolve, a TLS handshake that never completed, a credential that was
    missing before the socket was opened. "Probably fine" is not a proof, and
    the ambiguous case belongs to :class:`SeamUnknown`.
    """

    outcome = OUTCOME_UNAVAILABLE
    may_have_billed = False


class SeamUnknown(SeamError):
    """A request may have been issued; its fate is not established. Fail closed."""

    outcome = OUTCOME_UNKNOWN


# --------------------------------------------------------------------------
# Typed argument schema
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ArgSpec:
    """One declared argument. Everything about it is checked before a handler runs."""

    kind: str  # "str" | "int" | "bool" | "messages"
    required: bool = True
    default: Any = None
    choices: tuple[str, ...] = ()
    pattern: re.Pattern[str] | None = None
    min_len: int = 0
    max_len: int = 0
    minimum: int = 0
    maximum: int = 0


_ROLES = frozenset({"system", "user", "assistant"})


def _validate_messages(name: str, value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise SeamRefused("invalid_arguments", f"{name} must be a non-empty list")
    if len(value) > 32:
        raise SeamRefused("invalid_arguments", f"{name} may hold at most 32 messages")
    out: list[dict[str, str]] = []
    total = 0
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise SeamRefused("invalid_arguments", f"{name}[{index}] must be an object")
        role = str(item.get("role") or "")
        content = item.get("content")
        if role not in _ROLES:
            raise SeamRefused("invalid_arguments", f"{name}[{index}].role {role!r} is not allowed")
        if not isinstance(content, str) or not content:
            raise SeamRefused("invalid_arguments", f"{name}[{index}].content must be a string")
        total += len(content)
        if total > 32000:
            raise SeamRefused("invalid_arguments", f"{name} exceeds 32000 characters")
        out.append({"role": role, "content": content})
    return out


def _validate_args(
    capability: str, schema: Mapping[str, ArgSpec], args: Mapping[str, Any]
) -> dict[str, Any]:
    """Coerce *args* to the declared schema, or refuse. Unknown keys are refused.

    Refusing an unknown key rather than dropping it is the fail-closed reading:
    a worker that sends a field this seam does not understand is asking for
    something this seam cannot promise, and silently ignoring it would let a
    future field ("skip_safety_checker") look accepted while doing nothing.
    """
    extra = sorted(set(args) - set(schema))
    if extra:
        raise SeamRefused("invalid_arguments", f"{capability}: unknown argument(s) {extra}")

    out: dict[str, Any] = {}
    for name, spec in schema.items():
        if name not in args:
            if spec.required:
                raise SeamRefused("invalid_arguments", f"{capability}: {name} is required")
            if spec.default is not None:
                out[name] = spec.default
            continue
        value = args[name]

        if spec.kind == "messages":
            out[name] = _validate_messages(name, value)
            continue
        if spec.kind == "bool":
            if not isinstance(value, bool):
                raise SeamRefused("invalid_arguments", f"{capability}: {name} must be a boolean")
            out[name] = value
            continue
        if spec.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise SeamRefused("invalid_arguments", f"{capability}: {name} must be an integer")
            if not (spec.minimum <= value <= spec.maximum):
                raise SeamRefused(
                    "invalid_arguments",
                    f"{capability}: {name}={value} is outside [{spec.minimum}, {spec.maximum}]",
                )
            out[name] = value
            continue
        if spec.kind == "str":
            if not isinstance(value, str):
                raise SeamRefused("invalid_arguments", f"{capability}: {name} must be a string")
            if spec.choices and value not in spec.choices:
                raise SeamRefused(
                    "invalid_arguments",
                    f"{capability}: {name}={value!r} is not one of {list(spec.choices)}",
                )
            if spec.min_len and len(value) < spec.min_len:
                raise SeamRefused(
                    "invalid_arguments", f"{capability}: {name} is shorter than {spec.min_len}"
                )
            if spec.max_len and len(value) > spec.max_len:
                raise SeamRefused(
                    "invalid_arguments", f"{capability}: {name} is longer than {spec.max_len}"
                )
            if spec.pattern is not None and not spec.pattern.match(value):
                raise SeamRefused(
                    "invalid_arguments", f"{capability}: {name}={value!r} is not a legal identifier"
                )
            out[name] = value
            continue
        raise SeamRefused("invalid_arguments", f"{capability}: {name} has no validator")
    return out


# --------------------------------------------------------------------------
# The capability registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SeamRequest:
    """What the worker declared. Validated before any handler sees it."""

    instance_id: str
    capability: str
    args: dict[str, Any]
    var_dir: Path
    db_path: str


@dataclass(frozen=True)
class ParentCapability:
    """One credentialed effect this process will perform for a loop worker.

    ``broker_capability`` names the ``configs/connectors.yaml`` capability whose
    grant, HTTP allowlist and audit trail govern the call. It is empty only for
    :data:`MODEL_COMPLETE`, whose credential is an AI-provider key governed by
    the ``llm/`` hard spend cap rather than an HTTP allowlist. Every OTHER
    capability must be broker-proxied.
    """

    id: str
    action_class: ActionClass
    broker_capability: str
    args: Mapping[str, ArgSpec]
    run: Callable[[SeamRequest], dict[str, Any]]
    description: str = ""
    deadline_s: float = DEFAULT_CALL_DEADLINE_S


REPLICATE_GENERATE = "replicate.generate"
MODEL_COMPLETE = "model.complete"
WEB_FETCH = "web.fetch"

#: Capabilities that require budget checking and cost tracking.
PAID_CAPABILITIES: frozenset[str] = frozenset({REPLICATE_GENERATE, MODEL_COMPLETE})

#: Replicate models this process will pay to run. A model id is *code someone
#: else wrote that we are billed for*, so it is a closed set reviewed here, not
#: a string the worker chooses.
REPLICATE_MODELS: frozenset[str] = frozenset(
    {
        "black-forest-labs/flux-schnell",
        "black-forest-labs/flux-dev",
    }
)

#: Hosts the seam will download a rendered artifact from. The URL comes back in
#: the API's response, i.e. it is attacker-influenceable in the general case, so
#: it is checked against this list, https-only, with redirects disabled.
ARTIFACT_HOST_SUFFIXES: tuple[str, ...] = (".replicate.delivery", "replicate.delivery")


def _artifact_host_allowed(host: str) -> bool:
    """True only when *host* IS an allowlisted domain or sits under one of them.

    The anchor lives here rather than in the data above, because the data is the
    part that gets edited. Matching with a bare ``host.endswith(suffix)`` made
    the dot-less spelling of an entry admit ``evilreplicate.delivery`` — a
    registrable name under the ``.delivery`` gTLD — since ``str.endswith`` is a
    character operation and a hostname is a sequence of LABELS. Normalising each
    entry and requiring either equality or a ``.``-prefixed suffix makes both
    spellings mean the same thing, so adding a host in either form is safe.

    Same shape as ``omniagentos/lease/proxy.py``'s wildcard match, which anchors
    on ``"." + remainder`` for this reason.
    """
    for suffix in ARTIFACT_HOST_SUFFIXES:
        domain = suffix.lstrip(".").lower()
        if not domain:
            continue
        if host == domain or host.endswith(f".{domain}"):
            return True
    return False


#: Ceiling on a downloaded artifact.
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024


def artifact_root(var_dir: Path, instance_id: str) -> Path:
    """``<var>/loops/artifacts/<instance>`` — the ONLY place the seam writes.

    The worker never supplies a directory: it supplies a leaf file name matching
    :data:`ARTIFACT_NAME_RE`, and this function is the whole of the path
    derivation on both sides of the socket. ``omniagentos_loops.parent_seam``
    recomputes it identically so a tool's verification predicate can find the
    artifact from its ARGUMENTS alone, never from the API's answer.
    """
    return var_dir / "loops" / "artifacts" / instance_id


def artifact_path(var_dir: Path, instance_id: str, artifact_name: str) -> Path:
    if not ARTIFACT_NAME_RE.match(artifact_name):
        raise SeamRefused("invalid_arguments", f"illegal artifact name {artifact_name!r}")
    if not INSTANCE_RE.match(instance_id):
        raise SeamRefused("bad_instance", f"illegal instance id {instance_id!r}")
    return artifact_root(var_dir, instance_id) / artifact_name


def _contained(candidate: Path, root: Path) -> Path:
    """Second, independent containment check on the resolved path."""
    from omniagentos.workfs.containment import require_contained
    from omniagentos.workfs.errors import WorkfsPathError

    root.mkdir(parents=True, exist_ok=True)
    try:
        return require_contained(candidate, root)
    except WorkfsPathError as exc:
        raise SeamRefused("path_outside_root", str(exc)) from exc


# --- replicate.generate ---------------------------------------------------


_REPLICATE_ARGS: dict[str, ArgSpec] = {
    "model": ArgSpec(kind="str", choices=tuple(sorted(REPLICATE_MODELS))),
    "prompt": ArgSpec(kind="str", min_len=1, max_len=2000),
    "artifact_name": ArgSpec(kind="str", pattern=ARTIFACT_NAME_RE, max_len=72),
    "aspect_ratio": ArgSpec(
        kind="str", required=False, default="1:1", choices=("1:1", "16:9", "9:16", "4:3", "3:4")
    ),
    "output_format": ArgSpec(kind="str", required=False, default="png", choices=("png", "jpg")),
    "seed": ArgSpec(kind="int", required=False, minimum=0, maximum=2**31 - 1),
    # The loop's DECLARED post-condition, minted before the call and never sent
    # upstream. The verifier reads these from its arguments, so the API cannot
    # move the goalposts it is graded against.
    "expect_min_width": ArgSpec(kind="int", required=False, default=1, minimum=1, maximum=8192),
    "expect_min_height": ArgSpec(kind="int", required=False, default=1, minimum=1, maximum=8192),
}

_TERMINAL_PREDICTION_STATES = frozenset({"succeeded", "failed", "canceled"})

#: Attempts allowed for the CREATION post only (see the comment at its call
#: site). Polling and downloading are reads and are not covered by it.
CREATION_RETRIES = 3

#: Backoff before creation attempt 2 and 3. The observed 404 burst arrives in
#: clusters, so the second wait is materially longer than the first.
CREATION_BACKOFF_S = (2.0, 5.0)


def _broker_call(
    cap_id: str,
    *,
    audit_db_path: str,
    holder: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """One brokered call, with its exceptions mapped onto the seam taxonomy."""
    from omniagentos.connectors.broker import AuditContext, BrokerDenied, call
    from omniagentos.connectors.store import CapabilityStore
    from omniagentos.db.store import SqliteStore

    store = SqliteStore(audit_db_path)
    grant_store = CapabilityStore(store)
    try:
        return call(
            cap_id,
            grant_store=grant_store,
            grant_holder=holder,
            audit_store=grant_store,
            audit_context=AuditContext(holder=holder, run_id=holder),
            **kwargs,
        )
    except BrokerDenied as exc:
        raise _from_broker_denial(exc) from exc
    finally:
        store.close()


#: Broker denial reasons that mean "we never got to ask the outside world" AND
#: establish nothing adverse about the loop's own work: the operator has not
#: finished provisioning a credential this capability needs. Those settle
#: NEUTRAL (absence), which is safe precisely because no request was issued —
#: nothing can be double-bought by the next tick.
#:
#: U-R3 split what used to be one ``credential_missing`` code into three
#: (K9): one name unset, no name set at all, and a secret STORE the U-S1 guard
#: refused to read in ``enforce`` mode. They are one physical condition from
#: the loop's point of view — an operator-side provisioning gap — so all three
#: keep main's neutral settlement. Everything else is a REACHED authority (this
#: process's own allowlist, the grant check, the money/danger gates) answering
#: no, which is adverse.
_UNREACHED_DENIALS = frozenset(
    {"credential_missing", "capability_unprovisioned", "credential_unavailable"}
)


def _from_broker_denial(exc: Any) -> SeamError:
    reason = str(getattr(exc, "reason", "") or "broker_denied")
    detail = str(getattr(exc, "detail", "") or exc)
    if reason == "audit_finalization_failed":
        # The broker issued the request and then could not write its terminal
        # row. The provider may already have rendered (and charged for) the
        # effect, so this is the ambiguous case by construction: UNKNOWN keeps
        # the idempotency claim and ``may_have_billed`` (True by class) settles
        # the reservation instead of refunding it. Calling this "provably
        # unbilled" is the double-spend K1 found.
        return SeamUnknown(reason, detail)
    if reason in _UNREACHED_DENIALS:
        return SeamUnavailable(reason, detail)
    if reason == "transport_error":
        return _from_transport(exc.__cause__, detail)
    # Every other ``BrokerDenied`` is decided in THIS process before the
    # outbound request is built (``broker.call`` raises them above its httpx
    # call): unknown capability, not granted, no reviewed call path, method or
    # path outside the allowlist, approval gates. Adverse, and provably unbilled.
    return SeamRefused(reason, detail, may_have_billed=False)


def _from_transport(cause: BaseException | None, detail: str) -> SeamError:
    """Split "never connected" from "may have been received". Fail closed between.

    ``broker.call`` collapses every ``httpx.HTTPError`` into one denial, so the
    distinction that matters here — did our bytes reach the other end? — is
    recovered from the original exception type. A connect failure provably did
    not reach the authority (ABSENCE). A read timeout after the request was
    written may well have created a prediction, and calling that ABSENCE would
    release an idempotency claim for an effect that happened, so it is UNKNOWN.
    """
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a broker dependency
        return SeamUnknown("transport_error", detail)

    unreached = (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.UnsupportedProtocol,
        httpx.InvalidURL,
        httpx.ProxyError,
    )
    if isinstance(cause, unreached):
        return SeamUnavailable("transport_unreached", detail)
    return SeamUnknown("transport_ambiguous", detail)


def _after_billable_work(error: SeamError) -> SeamError:
    """Absence is a claim about the EFFECT, not about the last request it made.

    Once a capability has done work a provider charges for, no later failure
    inside it can be ABSENCE. ``_download_artifact`` runs only after a
    prediction has been created, polled and reported ``succeeded`` — the money
    is already gone — so a refused connection while fetching the image proves
    only that THIS request missed. Answering ``unavailable`` there would
    release the idempotency claim, and the next tick would go back to the top
    of ``_run_replicate_generate`` and pay for a second prediction.

    Downgraded to a refusal: adverse, bounded by the retry budget, and
    consistent with its neighbours — ``artifact_fetch_failed``,
    ``artifact_too_large`` and ``artifact_empty`` are all refusals for the same
    reason. Ambiguity stays ambiguous.
    """
    if isinstance(error, SeamUnavailable):
        return SeamRefused(error.reason, error.detail)
    return error


@contextlib.contextmanager
def _billable_scope() -> Iterator[None]:
    """Everything raised in here is past the point of no refund. SCOPE, not call site.

    Applying :func:`_after_billable_work` at each individual call site is an
    opt-in discipline, and it has now failed three times in the same way: a
    request added *after* the money is committed forgets to opt in, classifies
    an ordinary connect failure as ABSENCE, and hands the next tick a licence to
    buy the same thing again. The download was the instance we found by auditing
    the fetch; the poll GET was the instance that audit missed, and it is the
    likelier one in production because a prediction is polled repeatedly and
    downloaded once.

    So the guarantee is attached to the REGION instead. Enter this scope at the
    moment the spend becomes irreversible and every failure leaving it — from
    the calls that exist today and from the ones somebody adds later without
    reading this docstring — is classified as post-spend by construction. The
    author of the next request inside this block has to do nothing to be safe,
    which is the only version of this rule that survives the next edit.

    It converts absence and nothing else: an UNKNOWN stays unknown and a
    REFUSAL stays a refusal, both of which already fail closed.
    """
    try:
        yield
    except SeamError as exc:
        converted = _after_billable_work(exc)
        if converted is exc:
            raise
        raise converted from exc


def _from_llm_transport(exc: BaseException, detail: str) -> SeamError:
    """The same split as :func:`_from_transport`, for the urllib-based LLM client.

    ``ShortCallClient`` collapses three materially different events into one
    ``LLMTransportError``:

    * an HTTP 429 or 5xx — the server ANSWERED. A reached authority saying no
      is a REFUSAL. It is adverse, the claim is recorded as a failed attempt
      and the retry budget bounds it; it is never absence, because absence
      releases the claim and the next tick would re-issue a paid call.
    * a connect failure — refused connection, a name that does not resolve, a
      certificate this process refused. Provably no request reached a provider,
      so this is the one ABSENCE case.
    * a read timeout, or any other socket error, after the request was written.
      The call may have completed and been BILLED, and nothing observable here
      can tell. UNKNOWN: the claim stays and the next tick fails closed rather
      than silently paying twice.

    urllib does not label the phase an ``OSError`` came from, so the split is
    made on the exception TYPE of ``URLError.reason`` and is deliberately
    asymmetric: only types that CANNOT be raised once request bytes have been
    written count as absence. ``TimeoutError`` is excluded even though a
    connect timeout really is absence — urllib raises the same exception for a
    timeout while connecting and a timeout while reading the reply, and when
    the two cannot be told apart the ambiguous reading is the one that costs
    nothing to be wrong about. A bare ``ssl.SSLError`` that is not a
    certificate failure is excluded for the same reason: it can also be raised
    mid-write.
    """
    import socket as _socket
    import ssl
    import urllib.error

    cause = exc.__cause__ or exc.__context__

    # A server that answered — any status at all — is a reached authority.
    # HTTPError is a URLError subclass, so it has to be asked about first.
    if isinstance(cause, urllib.error.HTTPError):
        return SeamRefused("model_refused", detail)

    #: Raisable only BEFORE the first request byte leaves this process.
    unreached: tuple[type[BaseException], ...] = (
        _socket.gaierror,  # the name never resolved to an address
        _socket.herror,
        ConnectionRefusedError,  # the TCP handshake was refused
        ssl.SSLCertVerificationError,  # we refused the peer during the handshake
    )
    if isinstance(cause, urllib.error.URLError):
        reason = cause.reason
        if isinstance(reason, unreached) and not isinstance(reason, TimeoutError):
            return SeamUnavailable("model_unreached", detail)
        return SeamUnknown("model_ambiguous", detail)

    # Anything else the client wrapped (a bare socket error out of the response
    # read, or a transport error raised with no cause at all) is ambiguous by
    # construction.
    return SeamUnknown("model_ambiguous", detail)


def _download_artifact(url: str, destination: Path) -> dict[str, Any]:
    """Fetch one rendered artifact into *destination*, atomically.

    Not a broker call: the URL is minted by Replicate per prediction and is not
    a reviewable path on a fixed base_url. It is constrained instead —
    https-only, host-suffix allowlisted, redirects refused, size capped — and it
    carries no credential, so a mistake here leaks nothing.
    """
    import httpx

    parsed = httpx.URL(url)
    host = parsed.host.lower()
    if parsed.scheme != "https" or not _artifact_host_allowed(host):
        raise SeamRefused(
            "artifact_host_not_allowed", f"refusing to fetch {parsed.scheme}://{host}"
        )

    temporary = destination.with_name(f".{destination.name}.part")
    written = 0
    try:
        with httpx.Client(follow_redirects=False, timeout=60.0) as client:
            with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise SeamRefused(
                        "artifact_fetch_failed", f"artifact URL answered {response.status_code}"
                    )
                if response.status_code >= 300:
                    raise SeamRefused(
                        "artifact_redirected", f"artifact URL redirected ({response.status_code})"
                    )
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes(65536):
                        written += len(chunk)
                        if written > MAX_ARTIFACT_BYTES:
                            raise SeamRefused(
                                "artifact_too_large",
                                f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes",
                            )
                        handle.write(chunk)
    except httpx.HTTPError as exc:
        with contextlib.suppress(OSError):
            temporary.unlink()
        # Never absence: the prediction this is fetching has already been
        # created and paid for. See ``_after_billable_work``.
        raise _after_billable_work(_from_transport(exc, str(exc))) from exc
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise

    if written == 0:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise SeamRefused("artifact_empty", "the artifact URL returned zero bytes")
    os.replace(temporary, destination)
    return {"bytes": written}


def _run_replicate_generate(request: SeamRequest) -> dict[str, Any]:
    """POST one prediction, wait for it, download the artifact. No narrative.

    The returned mapping is deliberately thin. It is the ACTOR's account of
    itself (``EvidenceGrade.ACTOR_NARRATIVE``) and nothing downstream is allowed
    to grade the effect with it: the tool's verification predicate reads the
    file this wrote, from the path it derives out of its own ARGUMENTS.
    """
    args = request.args
    destination = artifact_path(request.var_dir, request.instance_id, str(args["artifact_name"]))
    root = artifact_root(request.var_dir, request.instance_id)
    destination = _contained(destination, root)

    body: dict[str, Any] = {
        "input": {
            "prompt": args["prompt"],
            "aspect_ratio": args.get("aspect_ratio", "1:1"),
            "output_format": args.get("output_format", "png"),
            "num_outputs": 1,
        }
    }
    if "seed" in args:
        body["input"]["seed"] = args["seed"]

    # A 4xx to a CREATION post provably created nothing (the response body is an
    # error, not a prediction), so re-issuing it cannot duplicate an effect —
    # which is what makes this bounded retry safe where a blind replay would not
    # be. It exists because Replicate's official-model endpoint returns a
    # spurious 404 "No adapter found for model" at roughly 1 request in 5, in
    # BURSTS (measured 2026-08-01 with byte-identical requests: 201/404/201/201/
    # 201, and separately two consecutive 404s two seconds apart). The retry is
    # narrow on purpose: only the two statuses that mean "ask again", never
    # 401/403/422, which are real answers about the candidate. If every attempt
    # is refused the effect settles ADVERSE — nothing here can launder it.
    attempts = 0
    while True:
        attempts += 1
        response = _broker_call(
            REPLICATE_GENERATE,
            audit_db_path=request.db_path,
            holder=f"loop:{request.instance_id}",
            method="POST",
            path=f"/models/{args['model']}/predictions",
            body=body,
            timeout=90.0,
        )
        payload = response.get("body")
        if not isinstance(payload, Mapping):
            raise SeamUnknown("prediction_unparseable", "Replicate returned a non-object body")
        if response.get("ok"):
            break
        status = int(response.get("status") or 0)
        message = str(payload.get("detail") or payload.get("title") or "")[:300]
        if status in (404, 429) and attempts < CREATION_RETRIES:
            backoff = CREATION_BACKOFF_S[min(attempts, len(CREATION_BACKOFF_S)) - 1]
            logger.warning(
                "replicate.generate creation answered %s (attempt %s/%s); retrying in %ss",
                status,
                attempts,
                CREATION_RETRIES,
                backoff,
            )
            time.sleep(backoff)
            continue
        if 400 <= status < 500:
            # ``may_have_billed=False`` for the same reason the retry above is
            # safe, and it would be incoherent to hold one and not the other:
            # this branch is a 4xx to a CREATION post, which provably created
            # no prediction, so nothing ran on Replicate's hardware and nothing
            # was billed. Re-issuing on that proof is the STRONGER commitment —
            # if the proof were wrong the retry would buy a second prediction —
            # so a codebase willing to retry here and unwilling to refund here
            # was trusting the same fact in one place and doubting it in the
            # other. Charging anyway is bounded ($0.10) but it is not
            # conservative in the direction that matters: it eats the loop's
            # daily cap for calls that cost nothing, which itself denies real
            # work later.
            raise SeamRefused(
                "prediction_rejected",
                f"Replicate answered {status}: {message}",
                may_have_billed=False,
            )
        # A 5xx is a reached authority that failed to answer. It cannot be
        # proved that no prediction was created, so it is UNKNOWN, not absence.
        raise SeamUnknown("prediction_upstream_error", f"Replicate answered {status}: {message}")

    # THE SPEND IS NOW IRREVERSIBLE. A 201 means the prediction exists on
    # Replicate's side and their hardware is rendering it; nothing this process
    # does from here makes that unpaid. Everything below is therefore a READ
    # about work already bought, and a read that fails proves something about
    # the read, never about the effect — so no failure below may settle as
    # ABSENCE, which is the one outcome that releases the idempotency claim and
    # lets the next tick buy this prediction a second time.
    #
    # The guard is a SCOPE and not a wrapper on each call for a reason: this is
    # the third time a request added after the spend was left unwrapped. Inside
    # this block the safe classification is the default, so the next request
    # somebody adds here is covered without having to know that it must be.
    with _billable_scope():
        prediction_id = str(payload.get("id") or "")
        state = str(payload.get("status") or "")
        deadline = time.monotonic() + 150.0
        while state not in _TERMINAL_PREDICTION_STATES:
            if time.monotonic() > deadline:
                raise SeamUnknown(
                    "prediction_timeout",
                    f"prediction {prediction_id} was still {state!r} after 150s",
                )
            time.sleep(2.0)
            # A dropped poll is the likeliest post-spend transport failure in
            # production — a prediction is polled many times and downloaded
            # once — and on its own it classified as ``transport_unreached``,
            # i.e. absence, i.e. a refund and a re-buy.
            polled = _broker_call(
                REPLICATE_GENERATE,
                audit_db_path=request.db_path,
                holder=f"loop:{request.instance_id}",
                method="GET",
                path=f"/predictions/{prediction_id}",
                timeout=30.0,
            )
            body_polled = polled.get("body")
            if not isinstance(body_polled, Mapping):
                raise SeamUnknown("prediction_unparseable", "Replicate returned a non-object body")
            payload = body_polled
            state = str(payload.get("status") or "")

        if state != "succeeded":
            raise SeamRefused(
                "prediction_failed",
                f"prediction {prediction_id} finished {state}: "
                f"{str(payload.get('error') or '')[:300]}",
            )

        output = payload.get("output")
        urls = [
            item
            for item in (output if isinstance(output, list) else [output])
            if isinstance(item, str)
        ]
        if not urls:
            raise SeamRefused(
                "prediction_without_output", f"prediction {prediction_id} produced no url"
            )

        # Keeps its own ``_after_billable_work`` as well: the download is also
        # reachable directly (and is the anchor of its own counterfeit), so the
        # inner guard is not redundant with this scope, it is the same rule
        # stated where that function can be entered from somewhere else.
        fetched = _download_artifact(urls[0], destination)
        return {
            "prediction_id": prediction_id,
            "model": args["model"],
            "artifact_path": str(destination),
            "artifact_name": args["artifact_name"],
            "bytes": fetched["bytes"],
        }


# --- model.complete -------------------------------------------------------


_MODEL_ARGS: dict[str, ArgSpec] = {
    "messages": ArgSpec(kind="messages"),
    "purpose": ArgSpec(kind="str", pattern=re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")),
    "model": ArgSpec(
        kind="str", required=False, pattern=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")
    ),
    "max_tokens": ArgSpec(kind="int", required=False, minimum=1, maximum=8192),
    "temperature_milli": ArgSpec(kind="int", required=False, minimum=0, maximum=2000),
}


def _capturing_guard() -> Any:
    """A real ``BudgetGuard`` that remembers the cost of the call it just recorded.

    Same trick as ``omniagentos_loops.models._CapturingGuard`` — needed again
    here because the call now happens in THIS process, and the loop's
    ``loop.model_call`` event must still describe the call that really ran
    rather than an estimate reconstructed on the far side of a socket. Built
    lazily so importing this module never drags in the llm stack.
    """
    from omniagentos.llm.budget import BudgetGuard

    class _Guard(BudgetGuard):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__()
            self.last_call: dict[str, Any] | None = None

        def record_spend(
            self,
            model: str,
            prompt_tokens: int | None = None,
            completion_tokens: int | None = None,
            purpose: str = "default",
        ) -> float:
            cost = super().record_spend(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                purpose=purpose,
            )
            self.last_call = {
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "estimated_usd_cost": cost,
                "purpose": purpose,
                "cost_quality": "estimated",
                "cost_source": "llm.budget.rates",
            }
            return cost

    return _Guard()


def _run_model_complete(request: SeamRequest) -> dict[str, Any]:
    """One short model call, executed HERE, under the existing hard spend cap.

    ``ctx.model()`` used to build a ``ShortCallClient`` inside the worker, where
    ``GEMINI_API_KEY`` has been scrubbed — so it silently fell back to the local
    proxy secret and a loop could not reliably call a model at all. Routing it
    through the seam keeps ONE budget guard, ONE ledger and ONE credential
    holder, which is the same argument that put ``replicate.generate`` here.
    """
    from omniagentos.llm.budget import (
        LLMBudgetExceededError,
        LLMClientError,
        LLMInvalidResponseError,
        LLMTransportError,
    )
    from omniagentos.llm.client import ShortCallClient

    args = request.args
    guard = _capturing_guard()
    # ``retry_transport=False`` because THIS call sits behind an idempotency
    # claim. The client's own retry re-sends the identical request after a read
    # timeout — i.e. after the one failure where the first request may already
    # have been received and billed — which is a duplicate paid call inside a
    # single tick that no claim can undo. Retrying is the seam's decision to
    # make, once the outcome has been classified, not the client's to make
    # blind.
    client = ShortCallClient(
        budget_guard=guard,
        default_model=args.get("model") or None,
        retry_transport=False,
    )
    kwargs: dict[str, Any] = {}
    if "max_tokens" in args:
        kwargs["max_tokens"] = args["max_tokens"]
    if "temperature_milli" in args:
        kwargs["temperature"] = float(args["temperature_milli"]) / 1000.0

    purpose = f"loop:{request.instance_id}:{args['purpose']}"
    try:
        text = client.complete(list(args["messages"]), purpose=purpose, **kwargs)
    except LLMBudgetExceededError as exc:
        # A reached authority (the spend cap) refusing. Adverse, and legible.
        # The guard is consulted BEFORE the request is built, so nothing was
        # sent and nothing can have been billed.
        raise SeamRefused("budget_exceeded", str(exc), may_have_billed=False) from exc
    except LLMTransportError as exc:
        # 429/5xx, connect failure and read timeout all arrive here as one
        # exception type. Splitting them is the whole point — see
        # ``_from_llm_transport``.
        raise _from_llm_transport(exc, str(exc)) from exc
    except (LLMInvalidResponseError, LLMClientError) as exc:
        # A 4xx that is not 429, or a 200 whose body could not be read as a
        # completion. Either way the provider answered, so it may have billed.
        raise SeamRefused("model_refused", str(exc)) from exc

    return {"text": text, "cost": getattr(guard, "last_call", None)}


_WEB_FETCH_ARGS: dict[str, ArgSpec] = {
    # 2 KiB is comfortably above any real article URL and well below what a
    # worker could use to smuggle a payload through the argument channel.
    "url": ArgSpec(kind="str", min_len=8, max_len=2048),
}


def _run_web_fetch(request: SeamRequest) -> dict[str, Any]:
    """One bounded, SSRF-guarded public read on a loop worker's behalf.

    This is the runtime caller ``web.fetch`` exists for. It is deliberately thin:
    every decision that matters — the standing grant, the per-hop SSRF refusal,
    the pinned address, the size/time/redirect bounds, the scrubbing and the
    U-A1 audit pair — belongs to ``connectors/web_read.py`` and is made there.
    Nothing here may widen any of them, and the ``granted`` argument that used
    to let a caller vouch for itself is refused rather than accepted.

    A non-2xx answer is a RESULT, not a seam failure: "that page is 404" is
    exactly the fact a research worker asked for, and hiding it behind a refusal
    would make the loop retry a page that will never exist.
    """
    from omniagentos.connectors.broker import AuditContext, BrokerDenied
    from omniagentos.connectors.store import CapabilityStore
    from omniagentos.connectors.web_read import fetch
    from omniagentos.db.store import SqliteStore

    holder = f"loop:{request.instance_id}"
    store = SqliteStore(request.db_path)
    grant_store = CapabilityStore(store)
    try:
        result = fetch(
            str(request.args["url"]),
            grant_store=grant_store,
            grant_holder=holder,
            audit_store=grant_store,
            audit_context=AuditContext(holder=holder, run_id=holder),
        )
    except BrokerDenied as exc:
        raise _from_broker_denial(exc) from exc
    finally:
        store.close()
    return {
        "content": result["body"]["content"],
        "status": result["status"],
        "receipt": result["receipt"],
    }


CAPABILITIES: dict[str, ParentCapability] = {
    REPLICATE_GENERATE: ParentCapability(
        id=REPLICATE_GENERATE,
        action_class=ActionClass.SANDBOXED_CREATION,
        broker_capability=REPLICATE_GENERATE,
        args=_REPLICATE_ARGS,
        run=_run_replicate_generate,
        description="Render one image with a reviewed Replicate model into var/loops/artifacts",
    ),
    # U-R9: Unbrokered exception (T4.8 owner). This capability deliberately sets
    # broker_capability="" to bypass the credential broker and instead reads
    # GEMINI_API_KEY directly from os.environ in llm/client.py. This is the sole
    # sanctioned exception to broker-only resolution, bounded by the llm/ budget
    # guard. See omniagentos/llm/client.py and RESIDUAL-RISKS.md (U-R9).
    # File-level on purpose: a line citation is wrong the first time anyone
    # edits above the line it names, and this one already was.
    MODEL_COMPLETE: ParentCapability(
        id=MODEL_COMPLETE,
        action_class=ActionClass.SANDBOXED_CREATION,
        broker_capability="",
        args=_MODEL_ARGS,
        run=_run_model_complete,
        description="One short model completion under the llm/ hard spend cap",
        deadline_s=60.0,
    ),
    WEB_FETCH: ParentCapability(
        id=WEB_FETCH,
        action_class=ActionClass.READ_ONLY,
        broker_capability=WEB_FETCH,
        args=_WEB_FETCH_ARGS,
        run=_run_web_fetch,
        description="Fetch one public URL under the U-N1 SSRF guard and bounds",
        deadline_s=30.0,
    ),
}


#: Which loop instance may ask for what. Keyed by ``instance_id``, declared in
#: SOURCE, resolved here — never read from the routine row, because a row is
#: data and this is an authorization decision. (``routines_settle``'s Class P
#: docstring states the same rule for probe declarations, and for the same
#: reason: an operator seeding a row must not be able to widen a loop's reach.)
#:
#: An instance absent from this table holds NOTHING; absence is denial.
#:
#: :data:`WEB_FETCH` is deliberately on NO floor yet. Turning a loop into a web
#: reader is a TWO-key operation by this module's own design — the source floor
#: below may only grant, and the broker-side ``agent_capabilities`` row (seeded
#: by a migration, e.g. 108 for ``replicate.generate``) may only deny — and
#: those two keys are held by two different changes. Listing an instance here
#: without its seeded row would give a loop a floor it cannot exercise and would
#: break ``test_migration_seeds_every_broker_backed_loop_floor``, which exists
#: precisely to stop a floor and a grant drifting apart. So the effect ships
#: callable and audited, and the day an operator wants a research loop to read
#: the web it is one floor line plus one seeding migration — reviewed together,
#: which is the point.
INSTANCE_CAPABILITIES: dict[str, frozenset[str]] = {
    # The seam's own end-to-end control. It renders one image per tick and
    # verifies it through an independent decoder; it exists to prove the chain,
    # and it is the shape any real render loop copies.
    "render_probe": frozenset({REPLICATE_GENERATE, MODEL_COMPLETE}),
    # Grandfather clock: T1 local file write, no external credentials needed.
    "grandfather_clock_html": frozenset(),
    "flowers_collection": frozenset({REPLICATE_GENERATE}),
}


def _granted_capabilities(instance_id: str) -> frozenset[str]:
    """The parent-side seam floor. SOURCE is the only widening authority.

    Phase-2 integration adjudication of the U-R8 note (`reviews/
    PHASE2-INTEGRATION-NOTES.md`, second open item). The note asked whether a
    store row may REVOKE what the in-source floor grants. It may not, and this
    function is the reason: it consults ``INSTANCE_CAPABILITIES`` and nothing
    else, so no row can subtract from the floor.

    U-R8 shipped a UNION (``floor | store_rows``) here, which preserves that
    property but buys it by adding the opposite one — a seeded
    ``agent_capabilities`` row could WIDEN a loop's reach. That is precisely
    what this table's own contract forbids ("never read from the routine row
    ... an operator seeding a row must not be able to widen a loop's reach";
    "absence is denial") and what PLAN.md §5 principle 5 restates. The union is
    therefore narrowed back to the floor.

    Store rows remain decisive in the direction migration 108 actually claims:
    they are broker-side facts, and deleting one denies the external call at
    ``connectors/broker.py:authorize_with_grant`` (U-R10) even while the floor
    still lists the capability. Two enforcement points, one direction each —
    source may only grant, rows may only deny.
    """
    return INSTANCE_CAPABILITIES.get(instance_id, frozenset())


# --------------------------------------------------------------------------
# The decision + execution path (socket-free, so it is directly testable)
# --------------------------------------------------------------------------


def _audit_seam_outcome(
    db_path: str,
    instance_id: str,
    capability_id: str,
    reason: str,
    *,
    decision: str = "refused",
    allowed: bool = False,
) -> None:
    """Record one seam-level outcome that the broker's audit spine cannot see.

    U-A1 moved the attempt record INTO the broker, which is the right owner: a
    caller attesting to its own call is exactly the self-vouching U-R10 removed
    elsewhere. That correctly deleted the seam's duplicate ``allowed`` row.

    But the broker can only record what passes through it, and three classes of
    attempt structurally do not:

    * ``decision="refused"`` — refusals decided BEFORE the broker: an unknown
      capability, a capability outside the instance's source floor, a
      consequential class refused by Gate 2, invalid arguments, a budget
      denial. These are the attempts most worth keeping; a loop reaching past
      its own floor would otherwise leave no durable trace whatsoever.
    * ``decision="error"`` — an unclassified handler crash. It happens AFTER a
      possibly-billed call, and the broker's rows (if any) say nothing about a
      failure at the handler level, so the one place that knows must say so.
    * ``decision="unavailable"`` — a local prerequisite was provably absent
      before the broker or provider was reached. It is operational evidence,
      not an adverse verdict about the candidate.
    * ``decision="allowed"`` on an UNBROKERED capability — ``model.complete``
      is the sanctioned non-broker path (U-R9), so the broker never sees it and
      "what did my agents do overnight" had lost it entirely.

    ``method="seam"`` keeps the provenance unambiguous in every case: these
    rows are outside the broker's intent/final vocabulary and can never be
    mistaken for a brokered decision.

    A dedicated connection, not the scheduler's: ``SqliteStore`` serialises
    writes behind an ``RLock`` held by the ticking thread, and borrowing it
    from the serving thread while the tick blocks on ``subprocess.run`` is a
    deadlock with the worker on the other side of it.
    """
    if not db_path:
        return
    from omniagentos.connectors.store import CapabilityStore
    from omniagentos.db.store import SqliteStore

    store = SqliteStore(db_path)
    try:
        CapabilityStore(store).log_call(
            f"loop:{instance_id}",
            f"loop:{instance_id}",
            capability_id,
            method="seam",
            path=capability_id,
            allowed=allowed,
            reason=reason,
            holder=f"loop:{instance_id}",
            action_mode=decision,
            decision=decision,
        )
    finally:
        with contextlib.suppress(Exception):
            store.close()


def _audit_pre_broker_refusal(
    db_path: str, instance_id: str, capability_id: str, reason: str
) -> None:
    """The refusal case of :func:`_audit_seam_outcome`, named for its call sites."""
    _audit_seam_outcome(db_path, instance_id, capability_id, reason, decision="refused")


def _estimate_cost_for_capability(capability_id: str, args: Mapping[str, Any]) -> float:
    """Estimate max USD cost for a capability before execution.

    This is used BEFORE the call to reserve budget. It should be conservative
    (never underestimate). Real costs are reconciled when the result arrives.
    """
    if capability_id == REPLICATE_GENERATE:
        # Flux models cost roughly $0.04-0.06 per image. Conservative estimate: $0.10
        return 0.10
    if capability_id == MODEL_COMPLETE:
        # Model completions vary by model, but use conservative estimate: $0.02
        return 0.02
    # Unknown capability, use conservative estimate
    return 1.0


def _extract_cost_from_result(
    capability_id: str, result: dict[str, Any]
) -> tuple[float | None, str]:
    """Extract actual cost and cost_quality from a successful result.

    Returns (actual_usd, cost_quality) where cost_quality is one of:
    - "exact": provider reported the exact cost
    - "estimated": we estimated or calculated it
    - "unknown": we cannot determine cost
    """
    if capability_id == MODEL_COMPLETE:
        # model.complete returns a "cost" dict with model/tokens/estimated_usd_cost
        cost_info = result.get("cost")
        if isinstance(cost_info, dict):
            actual = cost_info.get("estimated_usd_cost")
            if isinstance(actual, (int, float)):
                return float(actual), "estimated"
        return None, "unknown"
    if capability_id == REPLICATE_GENERATE:
        # Replicate doesn't currently return cost in the result, so we use our estimate
        # Once Replicate's usage object is exposed, we can parse the real cost here
        return None, "unknown"
    return None, "unknown"


def _var_dir() -> Path:
    # Preserve this call site's historical VAR_DIR-only precedence while using
    # the shared resolver so a simulation can never fall back into the operator
    # checkout.  The launcher always supplies VAR_DIR for a valid campaign.
    return resolve_var_root(env_keys=("OMNIAGENTOS_VAR_DIR",)).expanduser()


def execute(payload: Mapping[str, Any], *, db_path: str, budget_ledger: LoopBudgetLedger | None = None) -> dict[str, Any]:
    """Decide, execute, and answer ONE worker request. Never raises.

    Order matters and is fail-closed at every step: protocol, instance shape,
    capability membership, per-instance grant, action class, arguments, audit,
    and only then the credentialed call.
    """
    # Type guard: budget_ledger must be a LoopBudgetLedger or None
    if budget_ledger is not None and not isinstance(budget_ledger, LoopBudgetLedger):
        logger.error(
            "budget_ledger is not a LoopBudgetLedger: %s (%s)",
            type(budget_ledger).__name__,
            budget_ledger,
        )
        raise TypeError(
            f"budget_ledger must be LoopBudgetLedger or None, got {type(budget_ledger).__name__} "
            f"(wiring error: fixture object passed instead of fixture value?)"
        )

    instance_id = str(payload.get("instance") or "")
    capability_id = str(payload.get("capability") or "")

    def answer(outcome: str, reason: str, detail: str, result: Any = None) -> dict[str, Any]:
        return {
            "v": SEAM_PROTOCOL_VERSION,
            "outcome": outcome,
            "reason": reason,
            "detail": detail[:2000],
            "result": result,
        }

    try:
        if int(payload.get("v") or 0) != SEAM_PROTOCOL_VERSION:
            raise SeamRefused("bad_protocol", f"expected protocol v{SEAM_PROTOCOL_VERSION}")
        if not INSTANCE_RE.match(instance_id):
            raise SeamRefused("bad_instance", f"illegal instance id {instance_id!r}")
        if not CAPABILITY_RE.match(capability_id):
            raise SeamRefused("bad_capability", f"illegal capability id {capability_id!r}")
        capability = CAPABILITIES.get(capability_id)
        if capability is None:
            raise SeamRefused(
                "unknown_capability",
                f"{capability_id!r} is not a parent-side capability "
                f"(known: {sorted(CAPABILITIES)})",
            )
        if capability_id not in _granted_capabilities(instance_id):
            raise SeamRefused(
                "not_granted",
                f"loop instance {instance_id!r} does not hold {capability_id!r}",
            )

        # Gate 2, in code, with no config input — the same argument as
        # ``broker.HARD_HUMAN_CLASSES``. A consequential effect is never
        # executed from this socket, no matter what the worker claims about
        # its approval, because an approval is the WORKER's evidence and this
        # is the parent's own floor.
        from omniagentos.connectors import AUTO_CLASSES

        if capability.action_class not in AUTO_CLASSES:
            raise SeamRefused(
                "requires_human_approval",
                f"{capability_id} is {capability.action_class.value}; the parent seam "
                "executes only auto-class capabilities",
            )

        raw_args = payload.get("args")
        if not isinstance(raw_args, Mapping):
            raise SeamRefused("invalid_arguments", "args must be an object")
        args = _validate_args(capability_id, capability.args, raw_args)
    except SeamError as exc:
        with contextlib.suppress(Exception):
            _audit_pre_broker_refusal(
                db_path, instance_id or "unknown", capability_id or "unknown", exc.reason
            )
        return answer(exc.outcome, exc.reason, exc.detail)

    if not db_path and capability.broker_capability:
        # No audit trail is available, so the call does not happen. This is
        # ABSENCE (we never reached the outside world), not a verdict about the
        # loop — it settles neutral and loud rather than against the floor.
        return answer(
            OUTCOME_UNAVAILABLE,
            "audit_unavailable",
            "the seam is not bound to a control-plane database; refusing to call uncredited",
        )

    # Resolve state before reserving paid budget. A missing/incoherent campaign
    # root proves no provider was reached, so it is absence; doing this after
    # reserve would leak an open hold for a call that never started.
    try:
        var_dir = _var_dir()
    except Exception:  # noqa: BLE001 - fail closed on every resolver fault
        logger.exception("loop effect seam runtime path is unavailable")
        with contextlib.suppress(Exception):
            _audit_seam_outcome(
                db_path,
                instance_id,
                capability_id,
                "runtime_path_unavailable",
                decision=OUTCOME_UNAVAILABLE,
            )
        return answer(
            OUTCOME_UNAVAILABLE,
            "runtime_path_unavailable",
            "runtime state root is unavailable; refusing to execute the capability",
        )

    # Budget gate: reserve before the paid call
    reservation = None
    if capability_id in PAID_CAPABILITIES and budget_ledger:
        estimated_cost = _estimate_cost_for_capability(capability_id, args)
        try:
            reservation = budget_ledger.reserve(
                instance_id=instance_id,
                capability_id=capability_id,
                estimated_max_usd=estimated_cost,
            )
        except (BudgetRefused, UnknownCostRefused) as exc:
            with contextlib.suppress(Exception):
                _audit_pre_broker_refusal(db_path, instance_id, capability_id, exc.reason)
            return answer(OUTCOME_REFUSED, exc.reason, str(exc))

    # Fail CLOSED: paid capabilities require budget ledger (wiring safety)
    if capability_id in PAID_CAPABILITIES and budget_ledger is None:
        with contextlib.suppress(Exception):
            _audit_pre_broker_refusal(
                db_path, instance_id, capability_id, "budget_ledger_missing"
            )
        return answer(
            OUTCOME_REFUSED,
            "budget_ledger_unavailable",
            f"spend cap ledger not wired; paid capability {capability_id} cannot proceed safely",
        )

    request = SeamRequest(
        instance_id=instance_id,
        capability=capability_id,
        args=args,
        var_dir=var_dir,
        db_path=db_path,
    )
    def _dispose_reservation(*, may_have_billed: bool, reason: str) -> None:
        """Give the held budget back, or charge it — never silently drop it.

        Releasing a reservation whose call may already have been paid for is
        the same fail-open defect as letting a leaked reservation expire
        uncharged: money that was (or may have been) spent stops counting
        against the cap, and the loop gets to spend it a second time. So the
        release is reserved for failures that PROVE nothing was billed, and
        everything else settles at the full reserved maximum with
        ``cost_quality="unknown"`` — deliberately over-counting, because the
        other direction is unbounded.
        """
        if not (budget_ledger and reservation):
            return
        try:
            if may_have_billed:
                budget_ledger.settle(
                    reservation.id,
                    actual_usd=None,
                    cost_quality="unknown",
                    usage_available=False,
                )
                logger.warning(
                    "loop budget: charging reservation %s at its %.4f USD maximum — "
                    "%s may have been billed by the provider",
                    reservation.id,
                    reservation.max_usd,
                    reason,
                )
            else:
                budget_ledger.release(reservation.id)
        except Exception as ex:  # noqa: BLE001
            logger.exception("could not dispose of budget reservation: %s", ex)

    #: The broker audits only what it brokers. ``model.complete`` is U-R9's
    #: sanctioned unbrokered path, so nothing else will ever record it.
    unbrokered = not capability.broker_capability

    try:
        result = capability.run(request)
    except SeamError as exc:
        _dispose_reservation(may_have_billed=exc.may_have_billed, reason=exc.reason)
        if unbrokered:
            with contextlib.suppress(Exception):
                _audit_seam_outcome(
                    db_path, instance_id, capability_id, exc.reason, decision=exc.outcome
                )
        return answer(exc.outcome, exc.reason, exc.detail)
    except Exception as exc:  # noqa: BLE001 — fail CLOSED on anything unclassified
        # An unclassified crash establishes nothing about whether the provider
        # ran and charged, so the reservation is charged, not returned.
        _dispose_reservation(may_have_billed=True, reason="handler_error")
        logger.exception("loop effect seam handler raised")
        # A crash AFTER a possibly-billed call is exactly where a durable row
        # matters, and it is the one failure the broker's rows cannot describe:
        # they record the call, not the handler that fell over holding it.
        with contextlib.suppress(Exception):
            _audit_seam_outcome(
                db_path, instance_id, capability_id, "handler_error", decision="error"
            )
        return answer(OUTCOME_UNKNOWN, "handler_error", f"{type(exc).__name__}: {exc}")

    if unbrokered:
        with contextlib.suppress(Exception):
            _audit_seam_outcome(
                db_path, instance_id, capability_id, "", decision="allowed", allowed=True
            )

    # Settle the budget with actual cost
    if capability_id in PAID_CAPABILITIES and budget_ledger and reservation:
        actual_usd, cost_quality = _extract_cost_from_result(capability_id, result)
        try:
            budget_ledger.settle(
                reservation.id,
                actual_usd=actual_usd,
                cost_quality=cost_quality,
                usage_available=cost_quality != "unknown",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("loop effect seam could not settle budget for %s: %s", reservation.id, exc)
            # Log but don't fail the entire operation; settle failure doesn't undo the call

    return answer(OUTCOME_OK, "", "", result)


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


#: ``sun_path`` is 104 bytes on macOS and 108 on Linux, and it is a HARD limit —
#: ``bind`` fails outright. A deep ``TMPDIR`` (a sandboxed agent's scratch, a
#: worktree under /private/tmp/...) silently exceeded it, which would have
#: turned every credentialed effect into ``unavailable`` on some machines and
#: not others. So the length is checked and a short base is used when needed.
_MAX_SUN_PATH = 100


def _short_socket_dir() -> Path:
    """A 0700 temp directory whose socket path fits in ``sun_path``."""
    candidate = Path(tempfile.mkdtemp(prefix="omni-loop-seam-"))
    if len(str(candidate / "effects.sock").encode("utf-8")) < _MAX_SUN_PATH:
        return candidate
    shutil.rmtree(candidate, ignore_errors=True)
    return Path(tempfile.mkdtemp(prefix="oseam-", dir="/tmp"))  # noqa: S108


@dataclass
class EffectServer:
    """A per-tick AF_UNIX listener that serves worker requests in this process.

    One connection per request, served sequentially: a loop tick is a single
    thread of control, so concurrency here would buy nothing and would make the
    audit ordering harder to read.
    """

    db_path: str
    budget_ledger: LoopBudgetLedger | None = None
    _directory: Path | None = field(default=None, init=False)
    _socket: socket.socket | None = field(default=None, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    #: Requests served, for tests and for the tick's notes.
    served: list[tuple[str, str]] = field(default_factory=list, init=False)

    @property
    def path(self) -> str:
        if self._socket is None or self._directory is None:
            return ""
        return str(self._directory / "effects.sock")

    def start(self) -> str:
        """Bind, listen, and serve until :meth:`stop`. Returns the socket path.

        Returns ``""`` when a socket cannot be created. A tick whose seam did
        not start is not failed here: the worker simply finds no seam and every
        credentialed capability answers ``unavailable``, which is the absence
        rule applied to the transport itself.
        """
        try:
            directory = _short_socket_dir()
            os.chmod(directory, 0o700)
            path = directory / "effects.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(4)
            server.settimeout(0.25)
        except OSError as exc:
            logger.warning("loop effect seam could not start: %s", exc)
            with contextlib.suppress(Exception):
                shutil.rmtree(directory, ignore_errors=True)
            return ""
        self._directory = directory
        self._socket = server
        self._thread = threading.Thread(target=self._serve, name="loop-effect-seam", daemon=True)
        self._thread.start()
        return str(path)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=10.0)
        if self._socket is not None:
            with contextlib.suppress(OSError):
                self._socket.close()
            self._socket = None
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None

    def __enter__(self) -> EffectServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def _serve(self) -> None:
        server = self._socket
        if server is None:  # pragma: no cover - start() guarantees it
            return
        while not self._stop.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if exc.errno in (errno.EBADF, errno.EINVAL):
                    return
                continue
            with contextlib.closing(connection):
                try:
                    self._handle(connection)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("loop effect seam handler crashed on connection")
                    # Attempt to send an error response, but don't fail if the connection is bad
                    try:
                        self._write(
                            connection,
                            {
                                "v": SEAM_PROTOCOL_VERSION,
                                "outcome": OUTCOME_UNKNOWN,
                                "reason": "handler_error",
                                "detail": f"seam handler crashed: {type(exc).__name__}",
                                "result": None,
                            },
                        )
                    except Exception:  # noqa: BLE001
                        pass

    def _handle(self, connection: socket.socket) -> None:
        connection.settimeout(DEFAULT_CALL_DEADLINE_S + 60.0)
        chunks: list[bytes] = []
        size = 0
        while b"\n" not in b"".join(chunks[-1:]):
            chunk = connection.recv(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_REQUEST_BYTES:
                self._write(
                    connection,
                    {
                        "v": SEAM_PROTOCOL_VERSION,
                        "outcome": OUTCOME_REFUSED,
                        "reason": "request_too_large",
                        "detail": f"request exceeds {MAX_REQUEST_BYTES} bytes",
                        "result": None,
                    },
                )
                return
            chunks.append(chunk)
        raw = b"".join(chunks).split(b"\n", 1)[0]
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request must be a JSON object")
        except (UnicodeDecodeError, ValueError) as exc:
            self._write(
                connection,
                {
                    "v": SEAM_PROTOCOL_VERSION,
                    "outcome": OUTCOME_REFUSED,
                    "reason": "malformed_request",
                    "detail": str(exc)[:200],
                    "result": None,
                },
            )
            return
        response = execute(payload, db_path=self.db_path, budget_ledger=self.budget_ledger)
        self.served.append((str(payload.get("capability") or ""), str(response.get("outcome"))))
        self._write(connection, response)

    @staticmethod
    def _write(connection: socket.socket, response: Mapping[str, Any]) -> None:
        encoded = json.dumps(response, default=str).encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            encoded = json.dumps(
                {
                    "v": SEAM_PROTOCOL_VERSION,
                    "outcome": OUTCOME_REFUSED,
                    "reason": "response_too_large",
                    "detail": f"response exceeds {MAX_RESPONSE_BYTES} bytes",
                    "result": None,
                }
            ).encode("utf-8")
        connection.sendall(encoded + b"\n")


__all__ = [
    "ARTIFACT_NAME_RE",
    "CAPABILITIES",
    "CAPABILITY_RE",
    "INSTANCE_CAPABILITIES",
    "MAX_REQUEST_BYTES",
    "MODEL_COMPLETE",
    "OUTCOME_OK",
    "OUTCOME_REFUSED",
    "OUTCOME_UNAVAILABLE",
    "OUTCOME_UNKNOWN",
    "REPLICATE_GENERATE",
    "REPLICATE_MODELS",
    "SEAM_PROTOCOL_VERSION",
    "ArgSpec",
    "EffectServer",
    "ParentCapability",
    "SeamRefused",
    "SeamRequest",
    "SeamUnavailable",
    "SeamUnknown",
    "artifact_path",
    "artifact_root",
    "execute",
]
