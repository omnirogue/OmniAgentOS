"""Run-start spend authorization — turn an owner's explicit in-prompt spend
instruction into BOUNDED, expiring, audited grants, so a long-horizon run can
execute its consequential steps (buy a domain, create a server, send an email)
without stopping to re-ask for an approval the owner already gave.

WHAT THIS IS NOT
It is not a bypass of the hard-human gate. The broker's ``HARD_HUMAN_CLASSES``
block and ``validate_approval_token`` are untouched; a consequential call is
still refused unless a *durable, bounded* grant approves it. This module only
changes the TRIGGER for minting those grants — from a human clicking Approve in
the dashboard to the same human stating the authorization in the run's original
prompt. Every grant it mints carries the identical bounds a dashboard grant does:
a spend cap, an action cap, an expiry, a generation pin, and the connector's
action-class pin.

THE THREE SAFETY PROPERTIES, IN ORDER OF HOW MUCH THEY MATTER
1. **Bounded blast radius, unconditionally.** Even if provenance were somehow
   forged, the total mintable spend for a run can never exceed ``effective
   ceiling`` = min(caller ceiling, ``ABSOLUTE_MAX_CEILING_USD``, the parsed
   cap). Every grant expires within ``horizon_hours`` and is action-capped. The
   worst case is capped at a number the owner sets, not "unlimited".
2. **Owner provenance.** An authorization is only recognised when it comes from
   the authenticated OWNER principal and carries an explicit spend phrase AND a
   parseable amount. No amount → no authorization (never a default cap). A
   non-owner principal → no authorization. The caller MUST pass the run's
   ORIGINAL human goal (the intake-captured prompt), never a sub-agent brief —
   this module hashes what it is given into the audit so a later reviewer can
   confirm what text authorized the spend.
3. **Auditability.** Every mint (and every refusal) appends one line to
   ``var/grants/run_authorizations.jsonl`` with the run id, the prompt hash, the
   authorizing principal, the ceiling, and the per-capability grants — so the
   whole "the owner authorized $X, we minted these bounded grants" chain is
   reconstructable after the fact.

FAIL-CLOSED EVERYWHERE
A missing authorization, an over-ceiling ask set, an unknown/auto action class,
or a broadcast capability with no audience all REFUSE and mint nothing (mints are
all-or-nothing per call: a partial set that silently drops the over-budget item
would be a surprise spend). Absence of an authorization is never read favourably.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

#: The one principal whose in-prompt instruction can authorize spend. The owner.
OWNER_PRINCIPAL = "emp_owner"

#: Default per-run ceiling when the caller does not pass one. Deliberately small:
#: a domain (~$10-15) plus a small VPS billed by the hour (cents over a demo)
#: fits comfortably under it, and a worst-case forge cannot exceed it.
DEFAULT_CEILING_USD = 100.0

#: Absolute ceiling. No caller override and no parsed cap can raise the mintable
#: total above this — the last backstop on blast radius. Raising it is a
#: deliberate, reviewed source edit, never a config or prompt value.
ABSOLUTE_MAX_CEILING_USD = 1000.0

#: Env override for the per-run ceiling, itself clamped to ABSOLUTE_MAX.
CEILING_ENV = "OMNIAGENTOS_RUN_SPEND_CEILING_USD"

#: Action classes a run-authorization grant may cover. These are exactly the
#: classes the broker hard-blocks; auto-tier capabilities never need a grant, so
#: an ask naming one is a caller error, not something to mint for.
_GRANTABLE_CLASSES = frozenset({"consequential", "irreversible"})

#: An explicit spend authorization must contain BOTH an intent phrase and an
#: amount. The phrase list is deliberately narrow: an offhand mention of a price
#: ("domains cost about $12") must not read as an authorization.
_INTENT_PATTERNS = (
    # authori[sz]e covers BOTH spellings — "authorize" (US) and "authorise"
    # (UK); `spend` (no boundary) also matches inside "spending". The demo's own
    # example prompt is "I authorize spending up to $50", which the s-only form
    # silently failed to recognise (→ no grant minted, everything parks).
    r"authori[sz]e[d]?\s+(?:you\s+)?(?:to\s+)?spend",
    r"spend\s+up\s+to",
    r"you\s+(?:may|can|are\s+authori[sz]ed\s+to)\s+spend",
    r"approv\w*\s+(?:a\s+)?(?:budget|spend)",
    r"budget\s+of",
    r"i\s+authori[sz]e\s+(?:a\s+)?(?:spend|budget|purchase|payment)",
)
_INTENT_RE = re.compile("|".join(_INTENT_PATTERNS), re.IGNORECASE)

#: How far after the spend-intent phrase a currency figure may sit and still be
#: read as the authorization amount. The amount must be ADJACENT to the verb
#: ("spend up to $30"), not merely somewhere later in the prompt — otherwise an
#: incidental price mentioned in a trailing sentence ("the enterprise server
#: costs $800") would be misread as the cap. A figure outside this window is
#: ignored, so the worst case is under-granting (a refusal), never an over-grant.
_AMOUNT_WINDOW_CHARS = 40

#: A USD amount: $50, $1,000, $12.50, 50 dollars, 1000 usd.
_AMOUNT_RE = re.compile(
    r"(?:\$\s*(?P<sym>[0-9][0-9,]*(?:\.[0-9]{1,2})?))"
    r"|(?:(?P<word>[0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:usd|dollars?|bucks?))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SpendAuthorization:
    """An owner's explicit, bounded spend authorization for one run."""

    cap_usd: float
    authorized_by: str
    prompt_sha256: str
    created_at: str
    source: str = "human_prompt"


@dataclass(frozen=True)
class CapabilityAsk:
    """One consequential capability the run wants pre-authorized, with its bounds."""

    capability: str
    action_class: str
    max_spend_usd: float
    max_actions: int
    target_set: list[str] | None = None


@dataclass(frozen=True)
class MintedGrant:
    capability: str
    grant_id: str
    max_spend_usd: float
    max_actions: int
    expires_at: str


@dataclass(frozen=True)
class MintResult:
    minted: list[MintedGrant] = field(default_factory=list)
    refused_reason: str | None = None
    refused_detail: str = ""
    audit_ref: str = ""

    @property
    def ok(self) -> bool:
        return self.refused_reason is None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_amount(text: str, *, start: int, end: int) -> float | None:
    """The authorization amount is the FIRST currency figure inside the tight
    window ``text[start:end]`` immediately following the spend-intent phrase.

    Binding the amount to a bounded window right after the verb — rather than
    taking the first/largest figure anywhere later — is a deliberate fail-closed
    choice for money code. Two failure modes it closes:

    * an incidental price in a trailing sentence ("...By the way, the enterprise
      server costs $800.") is OUTSIDE the window, so it can never become the cap;
    * a legitimately-adjacent incidental ("spend up to $30, the domain is ~$12")
      still yields the authorized figure because the FIRST in-window match wins.

    The FIRST successfully-parsed currency figure in the window is authoritative
    and final. If that figure parses to ``<= 0`` we return ``None`` (fail closed):
    an explicit ``$0``/negative is the owner DENYING spend, not an invitation to
    keep scanning for a larger later figure ("spend up to $0, but the server is
    $500/mo" must never adopt $500). Only a NON-parseable match (garbage that is
    not a real number) is skipped. A window with no currency figure returns
    ``None`` too. The result is clamped to ``ABSOLUTE_MAX_CEILING_USD`` by the
    caller.
    """
    for m in _AMOUNT_RE.finditer(text, start, end):
        raw = m.group("sym") or m.group("word")
        if not raw:
            continue
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            # Not a real number (garbage match) -> skip and keep scanning.
            continue
        # First real figure decides: <= 0 is an explicit deny, not "keep looking".
        return val if val > 0 else None
    return None


def parse_spend_authorization(
    human_goal: str,
    *,
    authorized_by: str,
    now_iso: str,
) -> SpendAuthorization | None:
    """Recognise an explicit owner spend authorization in the run's ORIGINAL goal.

    Returns ``None`` (fail-closed) unless ALL hold: the principal is the owner,
    the text carries an explicit spend-intent phrase, and a USD amount parses.
    The returned cap is clamped to ``ABSOLUTE_MAX_CEILING_USD``.
    """
    if authorized_by != OWNER_PRINCIPAL:
        LOG.info("run_authorization: principal %r is not the owner; no authorization", authorized_by)
        return None
    if not human_goal:
        return None
    intent = _INTENT_RE.search(human_goal)
    if intent is None:
        return None
    # Only a figure ADJACENT to the intent phrase (within the window that starts
    # at the phrase's end) may set the cap; a distant incidental price does not.
    amount = _parse_amount(
        human_goal, start=intent.end(), end=intent.end() + _AMOUNT_WINDOW_CHARS
    )
    if amount is None or amount <= 0:
        return None
    cap = min(amount, ABSOLUTE_MAX_CEILING_USD)
    return SpendAuthorization(
        cap_usd=cap,
        authorized_by=authorized_by,
        prompt_sha256=_sha256(human_goal),
        created_at=now_iso,
    )


def _effective_ceiling(authorization: SpendAuthorization, ceiling_usd: float | None) -> float:
    if ceiling_usd is None:
        env_val = os.environ.get(CEILING_ENV)
        if env_val:
            try:
                ceiling_usd = float(env_val)
            except ValueError:
                ceiling_usd = DEFAULT_CEILING_USD
        else:
            ceiling_usd = DEFAULT_CEILING_USD
    return min(ceiling_usd, ABSOLUTE_MAX_CEILING_USD, authorization.cap_usd)


def _is_broadcast(capability: str) -> bool:
    try:
        from omniagentos.connectors.extractors import is_broadcast_capability

        return bool(is_broadcast_capability(capability))
    except Exception:  # noqa: BLE001 -- if we cannot prove it is NOT a broadcast, treat it as one.
        return capability.endswith(".send") or "conversation_send" in capability or "broadcast" in capability


def _audit_path() -> Path:
    root = os.environ.get("OMNIAGENTOS_VAR_DIR") or "var"
    p = Path(root) / "grants" / "run_authorizations.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _write_audit(record: dict[str, Any]) -> str:
    try:
        path = _audit_path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        return str(path)
    except Exception:  # noqa: BLE001 -- audit best-effort must never block a mint decision, but log loudly.
        LOG.warning("run_authorization: failed to append audit record", exc_info=True)
        return ""


def mint_run_grants(
    store: Any,
    *,
    authorization: SpendAuthorization | None,
    asks: Sequence[CapabilityAsk],
    project_id: str,
    run_id: str,
    now_iso: str,
    horizon_hours: float = 6.0,
    ceiling_usd: float | None = None,
) -> MintResult:
    """Mint bounded, expiring grants for ``asks`` under the owner's authorization.

    All-or-nothing: if the ask set is inadmissible (no authorization, over
    ceiling, an ungrantable class, or a broadcast capability with no audience),
    nothing is minted and the reason is returned and audited.
    """
    base_audit: dict[str, Any] = {
        "run_id": run_id,
        "project_id": project_id,
        "at": now_iso,
        "prompt_sha256": authorization.prompt_sha256 if authorization else None,
        "authorized_by": authorization.authorized_by if authorization else None,
    }

    if authorization is None:
        ref = _write_audit({**base_audit, "decision": "refused", "reason": "no_authorization"})
        return MintResult(refused_reason="no_authorization", audit_ref=ref)

    if authorization.authorized_by != OWNER_PRINCIPAL:
        ref = _write_audit({**base_audit, "decision": "refused", "reason": "not_owner"})
        return MintResult(refused_reason="not_owner", audit_ref=ref)

    ceiling = _effective_ceiling(authorization, ceiling_usd)

    # Validate the whole ask set BEFORE minting anything.
    total = 0.0
    for ask in asks:
        if ask.action_class not in _GRANTABLE_CLASSES:
            ref = _write_audit(
                {**base_audit, "decision": "refused", "reason": "ungrantable_class",
                 "detail": f"{ask.capability}:{ask.action_class}"}
            )
            return MintResult(
                refused_reason="ungrantable_class",
                refused_detail=f"{ask.capability} is {ask.action_class}, not consequential/irreversible",
                audit_ref=ref,
            )
        # ``max_spend_usd`` must be strictly positive AND ``max_actions >= 1``.
        # A grant with a $0 spend cap is dead-on-arrival: the store refuses it at
        # consume time (``is_grant_live``: ``spend_used >= max_spend`` is already
        # true when ``max_spend == 0``), so minting one would report success and
        # hand back an unusable grant — an all-or-nothing violation deferred to
        # the consume boundary. Refuse it here instead. A genuinely zero-cost
        # consequential action must be granted with a small positive cap (the
        # smallest billable unit), exactly as every other grant in the store is.
        if ask.max_spend_usd <= 0 or ask.max_actions < 1:
            ref = _write_audit(
                {**base_audit, "decision": "refused", "reason": "bad_bounds", "detail": ask.capability}
            )
            return MintResult(
                refused_reason="bad_bounds",
                refused_detail=(
                    f"{ask.capability}: max_spend_usd must be > 0 (a $0-cap grant can "
                    "never be consumed) and max_actions must be >= 1"
                ),
                audit_ref=ref,
            )
        if _is_broadcast(ask.capability) and not (ask.target_set):
            ref = _write_audit(
                {**base_audit, "decision": "refused", "reason": "broadcast_needs_audience",
                 "detail": ask.capability}
            )
            return MintResult(
                refused_reason="broadcast_needs_audience",
                refused_detail=f"{ask.capability} requires a concrete target_set audience",
                audit_ref=ref,
            )
        total += ask.max_spend_usd

    if total > ceiling:
        ref = _write_audit(
            {**base_audit, "decision": "refused", "reason": "over_ceiling",
             "detail": f"asked {total:.2f} > ceiling {ceiling:.2f}"}
        )
        return MintResult(
            refused_reason="over_ceiling",
            refused_detail=f"total asked ${total:.2f} exceeds effective ceiling ${ceiling:.2f}",
            audit_ref=ref,
        )

    expires_at = (
        datetime.fromisoformat(now_iso.replace("Z", "+00:00")).astimezone(UTC)
        + timedelta(hours=horizon_hours)
    ).isoformat().replace("+00:00", "Z")

    approval_id = f"runauth:{run_id}:{authorization.prompt_sha256[:12]}"
    minted: list[MintedGrant] = []
    for ask in asks:
        grant = store.create_grant(
            ask.capability,
            label=f"run-authorized spend ({run_id})",
            target_set=list(ask.target_set) if ask.target_set else [],
            project_id=project_id,
            approval_id=approval_id,
            max_actions=ask.max_actions,
            max_spend_usd=ask.max_spend_usd,
            expires_at=expires_at,
            metadata={
                "generation": 0,
                "action_class": ask.action_class,
                "source": "run_authorization",
                "run_id": run_id,
                "authorized_by": authorization.authorized_by,
                "prompt_sha256": authorization.prompt_sha256,
            },
        )
        minted.append(
            MintedGrant(
                capability=ask.capability,
                grant_id=str(grant.get("id") or grant.get("grant_id") or ""),
                max_spend_usd=ask.max_spend_usd,
                max_actions=ask.max_actions,
                expires_at=expires_at,
            )
        )

    ref = _write_audit(
        {
            **base_audit,
            "decision": "minted",
            "ceiling_usd": ceiling,
            "total_usd": total,
            "expires_at": expires_at,
            "grants": [{"capability": g.capability, "grant_id": g.grant_id,
                        "max_spend_usd": g.max_spend_usd, "max_actions": g.max_actions} for g in minted],
        }
    )
    return MintResult(minted=minted, audit_ref=ref)
