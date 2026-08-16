"""Boot composition receipt — what actually came up in this API process.

The FastAPI lifespan in :mod:`omniagentos.api.main` composes the runtime out of
six optional subsystems (swarm crash-recovery, the routine seeds, the W3 health
monitor, the employee roster seed, the vault index). Every one of them is
deliberately fail-open: a failure is caught, logged, and boot continues, because
none of them is worth refusing to serve over. That policy is right, and this
module does not change it.

What it changes is the *evidence*. Before this, a degraded boot existed only as
a line in a log file nobody reads (dsh-audit C-21: "six lifespan subsystems fail
silent-to-logs"). A process that came up with no swarm resume and no routines
seeded is a materially different machine from one that came up whole, and until
now nothing could tell an operator — or an agent — which of the two it was
talking to.

So each lifespan step now records its outcome here, and the whole record is
readable at ``GET /api/ops/boot-receipt``:

* ``ok``       — the step ran and finished.
* ``degraded`` — the step raised; the exception TYPE and a bounded message are
  kept. Boot continued exactly as before.
* ``skipped``  — a precondition was not met (e.g. the store was unavailable, so
  the routine seeds that need it never ran). Not the same fact as ``ok`` and
  never rendered as one.
* ``disabled`` — the step's ``OMNIAGENTOS_*_ON_STARTUP`` flag was off. Also not
  the same fact as ``ok``: a subsystem that was never asked to run has not
  failed, but it is not running either.

Design constraints, all load-bearing:

* **In-process only.** No DB writes, no files. The receipt describes THIS
  process; a receipt that outlived its process would describe a machine that no
  longer exists.
* **Recording never raises.** A registry that can throw inside a startup
  ``except`` block would convert a logged degradation into a hard boot failure —
  the precise outcome this module exists to avoid. Every public method swallows
  its own errors.
* **Redacted, bounded, typed detail.** Exception text is passed through
  :func:`omniagentos.sessions.lifecycle_capture.redact_secrets` and only THEN
  truncated (:data:`_MAX_DETAIL_CHARS`); the exception type is kept separately.
  The clip is a size bound, not a secrecy control — the redactor is. This
  matters because the recorded sites aggregate exception text they do not
  author. The endpoint is gated as a whole namespace in ``main.py``
  (``_GATED_READ_NAMESPACES``), which also denies the raw machine principal,
  because a receipt discloses machine state of the same class as
  ``/api/workfs/tree`` and ``/api/accounts``.
* **Never an absence that reads as health.** ``snapshot()`` leads with
  ``status``/``measured``: a process whose lifespan never ran must not be
  indistinguishable from one that booted clean (see :meth:`BootReceipt.snapshot`).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: Boot-step outcomes. ``ok`` is the only one that means "this subsystem is up".
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_SKIPPED = "skipped"
STATUS_DISABLED = "disabled"

STATUSES = frozenset({STATUS_OK, STATUS_DEGRADED, STATUS_SKIPPED, STATUS_DISABLED})

#: Exception messages are operator diagnostics, not a log relay: keep enough to
#: identify the failure, not enough to turn this endpoint into a log tail.
_MAX_DETAIL_CHARS = 500


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class BootStep:
    """One composed subsystem and how it came up."""

    subsystem: str
    status: str
    detail: str = ""
    error_type: str | None = None
    recorded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "subsystem": self.subsystem,
            "status": self.status,
            "detail": self.detail,
            "error_type": self.error_type,
            "recorded_at": self.recorded_at,
        }


class BootReceipt:
    """Thread-safe, in-process record of the boot composition.

    Steps are keyed by subsystem name and kept in first-recorded order, so the
    receipt reads as the lifespan reads. Re-recording a subsystem replaces its
    row in place (a step that is retried reports its LAST outcome, never two
    contradictory ones).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._steps: dict[str, BootStep] = {}
        self._started_at: str | None = None
        self._completed_at: str | None = None

    # -- recording -----------------------------------------------------------

    def start(self) -> None:
        """Mark the beginning of a lifespan startup, clearing any prior run."""
        with self._lock:
            self._steps = {}
            self._started_at = _now_iso()
            self._completed_at = None

    def complete(self) -> None:
        """Mark startup composition as finished (the lifespan reached ``yield``)."""
        with self._lock:
            self._completed_at = _now_iso()

    # EVERY public recorder below wraps its WHOLE body, not just the store.
    # These run inside lifespan ``except`` blocks, so any escape converts a
    # logged degradation into a hard boot crash. Formatting is part of the
    # body: ``record_ok(sub, detail=obj)`` calls ``str(obj)`` before anything
    # touches the registry, and an object whose ``__str__`` raises is exactly
    # the payload an already-failing boot step is likely to hand over
    # (Class-A review F1). Guarding only the store left that path open.

    def record(self, subsystem: str, status: str, detail: str = "") -> None:
        """Record a non-exception outcome (``ok`` / ``skipped`` / ``disabled``)."""
        try:
            self._put(BootStep(_safe_text(subsystem), status, _clip(detail), None, _now_iso()))
        except Exception:  # noqa: BLE001 -- recording must never break boot
            return

    def record_ok(self, subsystem: str, detail: str = "") -> None:
        try:
            self.record(subsystem, STATUS_OK, detail)
        except Exception:  # noqa: BLE001 -- recording must never break boot
            return

    def record_skipped(self, subsystem: str, reason: str) -> None:
        try:
            self.record(subsystem, STATUS_SKIPPED, reason)
        except Exception:  # noqa: BLE001 -- recording must never break boot
            return

    def record_disabled(self, subsystem: str, flag: str) -> None:
        try:
            self.record(subsystem, STATUS_DISABLED, f"{_safe_text(flag)} is not '1'")
        except Exception:  # noqa: BLE001 -- recording must never break boot
            return

    def record_degraded(
        self,
        subsystem: str,
        exc: BaseException,
        detail: str = "",
        *,
        message_override: str | None = None,
    ) -> None:
        """Record a swallowed boot failure. Callers keep their own logging.

        The message is REDACTED before it is stored. The 500-char clip is a SIZE
        bound and was never a secrecy control; ``redact_secrets`` is, and it
        never raises, which suits a recorder that must not throw.

        Redaction is pattern-based, so it is sufficient for CREDENTIAL shapes and
        NOT for arbitrary relayed file content. A caller that knows its exception
        aggregates third-party text passes ``message_override``, and then the
        exception's own message is never read at all — only its type is kept.
        ``vault-index`` does exactly that: ``index_vault_playbook`` joins one
        ``f"{name}: {type(exc).__name__}: {exc}"`` per bad note, and PyYAML's
        ``ScannerError`` embeds a snippet of the offending source LINE, so
        relaying it would serve vault-note contents over HTTP (review A3).
        """
        try:
            try:
                error_type = type(exc).__name__
            except Exception:  # noqa: BLE001 -- a hostile type must not break boot
                error_type = "Exception"
            message = _safe_text(exc) if message_override is None else _safe_text(message_override)
            summary = f"{_safe_text(detail)}: {message}" if detail else message
            self._put(
                BootStep(
                    _safe_text(subsystem),
                    STATUS_DEGRADED,
                    _clip(_redact(summary)),
                    error_type,
                    _now_iso(),
                )
            )
        except Exception:  # noqa: BLE001 -- recording must never break boot
            return

    def _put(self, step: BootStep) -> None:
        # Recording is best-effort by construction: this runs inside startup
        # ``except`` blocks, where raising would turn a logged degradation into
        # a failed boot.
        try:
            with self._lock:
                self._steps[step.subsystem] = step
        except Exception:  # noqa: BLE001 -- see above
            return

    # -- reading -------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The full composition receipt, JSON-ready.

        **Read ``status`` first, never ``degraded_count``.** A process whose
        lifespan never ran — ``uvicorn --lifespan off``, an ASGI test client
        mounted without it, a worker that imported the app but did not boot it —
        has recorded nothing, so ``degraded_count == 0`` and ``counts`` are all
        zero: byte-identical on that field to a process that booted whole. Using
        it as the health predicate makes an UNMEASURED process read as a healthy
        one, which is the favourable absence this whole module exists to delete
        (Class-A review C1). Hence:

        * ``measured`` — did a lifespan ever start here at all?
        * ``status`` — ``unmeasured`` (nothing recorded) / ``composing``
          (started, not finished) / ``degraded`` (finished with ≥1 degraded
          step) / ``ok`` (finished, nothing degraded).

        ``ok`` is likewise never inferred for an absent STEP: a subsystem
        nothing recorded is absent, and reported as absent.
        """
        with self._lock:
            steps = [step.to_dict() for step in self._steps.values()]
            started_at = self._started_at
            completed_at = self._completed_at
        counts: dict[str, int] = {status: 0 for status in sorted(STATUSES)}
        for step in steps:
            counts[step["status"]] = counts.get(step["status"], 0) + 1
        measured = started_at is not None
        degraded_count = counts.get(STATUS_DEGRADED, 0)
        if not measured:
            status = "unmeasured"
        elif completed_at is None:
            status = "composing"
        elif degraded_count:
            status = STATUS_DEGRADED
        else:
            status = STATUS_OK
        return {
            "status": status,
            "measured": measured,
            "started_at": started_at,
            "completed_at": completed_at,
            "steps": steps,
            "counts": counts,
            "degraded": [s["subsystem"] for s in steps if s["status"] == STATUS_DEGRADED],
            "degraded_count": degraded_count,
        }


def _safe_text(value: object) -> str:
    """``str()`` that cannot raise. A hostile ``__str__`` yields a placeholder."""
    try:
        return str(value)
    except Exception:  # noqa: BLE001 -- see the recorders' contract
        return "<unrenderable value>"


def _redact(text: str) -> str:
    """Strip credential-shaped substrings. Never raises (see ``redact_secrets``)."""
    try:
        from omniagentos.sessions.lifecycle_capture import redact_secrets

        redacted = redact_secrets(text)
        return redacted if isinstance(redacted, str) else _safe_text(redacted)
    except Exception:  # noqa: BLE001 -- an unimportable redactor must fail CLOSED
        return "[REDACT_UNAVAILABLE]"


def _clip(text: object) -> str:
    """Bound the stored detail. This is a SIZE bound, never a secrecy control."""
    clipped = " ".join(_safe_text(text).split())
    if len(clipped) <= _MAX_DETAIL_CHARS:
        return clipped
    return clipped[: _MAX_DETAIL_CHARS - 1] + "…"


#: The process-wide receipt. One per API process, by design (see module docstring).
BOOT_RECEIPT = BootReceipt()


def boot_receipt() -> BootReceipt:
    """Return the process-wide boot receipt."""
    return BOOT_RECEIPT
