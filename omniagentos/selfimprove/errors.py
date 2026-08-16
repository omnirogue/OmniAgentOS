"""Exceptions raised by omniagentos.selfimprove."""

from __future__ import annotations


class SelfImproveError(Exception):
    """Base class for all omniagentos.selfimprove errors."""


class UnverifiedCaptureError(SelfImproveError):
    """Raised by capture_skill / append_constraint when the supplied
    VerificationGate has not passed (self-improving-loop method HARD RULE:
    only capture skills/constraints after a VERIFIED gate — capturing
    unverified output poisons the library for every future run that reuses
    it). Nothing is written to disk when this is raised."""
