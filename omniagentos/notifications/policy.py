"""The delivery allowlist: what is allowed to buzz a device.

This predicate lives in its own dependency-free module because it has TWO
enforcement points, at opposite ends of the stack:

* :func:`omniagentos.notifications.service._push` -- the write seam every
  notification SOURCE routes through; and
* :func:`omniagentos.sessions.notify.push` -- the TRANSPORT itself, which has
  direct legacy callers (the runner's escalation ping, the supervisor's default
  notifier, the health sentinel's blocked-session alert) that never pass through
  the seam and would otherwise reach the phone ungated.

Keeping it here rather than in ``service`` means the transport can consult the
allowlist without importing the seam (sqlite + DAL + the ``service`` module that
imports the transport back) -- the import direction stays acyclic and the
lowest-level transport keeps a minimal dependency surface. ``service`` re-exports
:func:`should_push`, ``PUSH_KINDS`` and ``PUSH_SEVERITIES`` so existing callers
and tests are unaffected.

Why an allowlist at all: measured on the live feed, 78% of 954 rows were
done/info/swarm_failed -- a task-finished bell, an informational row, a failed
swarm run. None of them needs the operator's attention within the minute, and a
channel that buzzes for all of them is a channel the operator mutes, which
silences the ~22% that genuinely does (an approval blocking a run, an
escalation, a blocked task, a high/critical alert). Push is therefore opt-IN by
kind, with a severity override so a high/critical row of ANY kind still gets
through. The DURABLE feed records EVERYTHING regardless -- this gates delivery
only.
"""

from __future__ import annotations

PUSH_KINDS = frozenset({"approval", "escalation", "blocked"})
PUSH_SEVERITIES = frozenset({"high", "critical"})


def should_push(kind: str | None, severity: str | None) -> bool:
    """Whether a notification of this kind/severity may be DELIVERED.

    Persistence is unaffected -- see the module docstring. Normalizes both
    inputs so a caller's casing/whitespace cannot smuggle a row past the gate
    or, worse, drop an approval push because it arrived as ``"Approval "``.

    Unlabelled calls (both arguments ``None``) fail CLOSED: an anonymous push is
    a silent row rather than an unfiltered buzz.
    """
    normalized_kind = str(kind or "").strip().lower()
    normalized_severity = str(severity or "").strip().lower()
    return normalized_kind in PUSH_KINDS or normalized_severity in PUSH_SEVERITIES


__all__ = ["PUSH_KINDS", "PUSH_SEVERITIES", "should_push"]
