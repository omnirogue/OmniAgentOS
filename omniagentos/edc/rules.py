"""Rule authoring for the Executive Decision Center (synthesis §9 / §0.5).

Three things live here, all sharing ONE proposal code path so there is exactly
zero silent activation anywhere:

* :func:`file_proposed_rule` — the single seam that writes a ``decision_rules``
  row in ``state='proposed'`` AND the ordinary ``rule_proposal`` Decision that
  gates its activation (§0.5). BOTH natural-language parsing and the nightly
  learner go through it, and it refuses to write an automation kind — automation
  authority is only ever minted by :meth:`DecisionStore.promote_rule`.
* :func:`parse_nl_rule` — a dashboard note / Slack instruction →
  ``complete_json(purpose="edc_rule_parse")`` → a STRUCTURED, owner-reviewable
  proposal (never an executed change).
* :func:`approve_rule_proposal` / :func:`decline_rule_proposal` — the decide
  endpoint's rule-proposal handlers: approve flips the rule ``proposed→active``,
  decline flips it ``declined`` (30-day re-proposal suppression, honoured by the
  learner). Plus :func:`seed_bootstrap_rules`, the one-shot idempotent seeder that
  copies ``steward.yaml`` VIP senders / urgent patterns into ``emp_owner``'s rules.

Authority separation (spec §15.7/§15.12, F11): NOTHING here can write an
``auto_delegate``/``auto_send`` kind — :data:`AUTOMATION_RULE_KINDS` is refused at
the proposal boundary. Those kinds exist ONLY after an explicit per-rule owner
promotion, which is a separate, deliberate action.
"""

from __future__ import annotations

import re
import sys
from typing import Any, Protocol

from omniagentos.edc.store import (
    AUTOMATION_RULE_KINDS,
    DecisionStore,
)

__all__ = [
    "NL_RULE_KINDS",
    "approve_rule_proposal",
    "decline_rule_proposal",
    "file_proposed_rule",
    "parse_nl_rule",
    "propose_nl_rule",
    "render_rule",
    "seed_bootstrap_rules",
]

#: Kinds a natural-language instruction (or the learner) may PROPOSE. Deliberately
#: excludes every automation kind — a plain-English "always send these to Alice"
#: lands as a ``delegate`` PRE-FILL proposal, never an unattended ``auto_delegate``.
NL_RULE_KINDS = frozenset({"suppress", "surface", "classify_hint", "delegate", "snooze_default"})

#: Upper bound on a parsed ``subject_re`` — a longer pattern is dropped unmatched
#: rather than compiled (ReDoS / catastrophic-backtracking defence, review minor).
_MAX_SUBJECT_RE_LEN = 200


class _JsonClient(Protocol):
    def complete_json(
        self,
        messages: list[dict[str, str]],
        required_keys: list[str],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str = "default",
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Rendering — a proposal always shows the owner the matcher + behavior delta.
# ---------------------------------------------------------------------------


def _matcher_desc(matcher: dict[str, Any]) -> str:
    sender = str(matcher.get("sender") or "").strip()
    domain = str(matcher.get("sender_domain") or "").strip()
    subject_re = str(matcher.get("subject_re") or "").strip()
    if sender:
        return f"sender={sender}"
    if domain:
        return f"sender-domain={domain}"
    if subject_re:
        return f"subject~/{subject_re}/"
    return "these messages"


def render_rule(kind: str, matcher: dict[str, Any], action: dict[str, Any]) -> str:
    """A one-line, owner-readable summary of a proposed rule (matcher + delta)."""
    desc = _matcher_desc(matcher)
    if kind == "suppress":
        return f"suppress {desc} — stop surfacing these (only when there is no material harm)"
    if kind in ("surface", "classify_hint"):
        return f"always surface {desc} for review"
    if kind == "delegate":
        assignee = str(action.get("assignee") or "someone")
        return f"assign {desc} to {assignee} (pre-filled for one-tap approval)"
    if kind == "snooze_default":
        hours = action.get("hours")
        span = f"{hours}h" if hours else "the default window"
        return f"default-snooze {desc} for {span}"
    return f"{kind} {desc}"


# ---------------------------------------------------------------------------
# The ONE proposal seam — proposed rule + its gating rule_proposal Decision.
# ---------------------------------------------------------------------------


def file_proposed_rule(
    store: DecisionStore,
    *,
    owner_employee_id: str,
    kind: str,
    matcher: dict[str, Any],
    action: dict[str, Any] | None = None,
    category: str = "",
    created_from: str,
    source_ref: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Write a PROPOSED rule + its ``rule_proposal`` Decision (the only such path).

    Returns ``(rule, decision)``. The Decision is ``source='rule_proposal'``,
    ``source_ref=<rule id>``, ``classification='needs_owner'``, with the rendered
    rule as its recommended action and ``available_actions=['approve','deny',
    'edit']`` (§0.5). Approving it later flips the rule active; nothing is
    activated here. Refuses an automation ``kind`` — those are minted only by an
    explicit per-rule promotion (F11).
    """
    if kind in AUTOMATION_RULE_KINDS:
        raise ValueError(
            f"a rule proposal may not carry the automation kind {kind!r}; automation "
            "authority is only minted by an explicit per-rule promotion (spec §15.12)"
        )
    if kind not in NL_RULE_KINDS:
        raise ValueError(f"unsupported proposed rule kind: {kind!r}")
    action = dict(action or {})
    rule = store.create_rule(
        {
            "owner_employee_id": owner_employee_id,
            "kind": kind,
            "category": category,
            "matcher": dict(matcher),
            "action": action,
            "state": "proposed",
            "created_from": created_from,
        }
    )
    human_line = render_rule(kind, matcher, action)
    decision, _created = store.create_decision(
        {
            "owner_employee_id": owner_employee_id,
            "source": "rule_proposal",
            "source_ref": source_ref or rule["id"],
            "title": f"Rule proposal: {human_line}",
            "context": f"Proposed from {created_from}",
            "classification": "needs_owner",
            "consequence": "none",
            "confidence": 0.7,
            "likelihood": 0.7,
            "reason": f"{created_from} proposed a decision rule for your approval",
            "classifier": "rule",
            "recommended": {
                "kind": "rule_proposal",
                "human_line": human_line,
                "params": {
                    "rule_id": rule["id"],
                    "rule_kind": kind,
                    "matcher": dict(matcher),
                    "action": action,
                },
            },
            "available_actions": ["approve", "deny", "edit"],
            "status": "open",
        }
    )
    return rule, decision


# ---------------------------------------------------------------------------
# Natural-language rule creation.
# ---------------------------------------------------------------------------


def _client(llm_client: _JsonClient | None) -> _JsonClient:
    if llm_client is not None:
        return llm_client
    from omniagentos.llm.client import ShortCallClient

    return ShortCallClient()


def parse_nl_rule(text: str, *, llm_client: _JsonClient | None = None) -> dict[str, Any] | None:
    """Parse a plain-English rule instruction into a structured proposal payload.

    Returns ``{"kind", "matcher", "action", "category"}`` or ``None`` if the
    model could not extract a usable rule. NEVER returns an automation kind — an
    ``auto_*`` guess is clamped to its pre-fill peer (``auto_delegate`` →
    ``delegate``) so a sentence can never mint unattended authority.
    """
    if not (text or "").strip():
        return None
    client = _client(llm_client)
    required = ["kind", "sender", "sender_domain", "subject_re", "assignee", "hours"]
    try:
        parsed = client.complete_json(
            [
                {
                    "role": "system",
                    "content": (
                        "Extract ONE decision rule from the user's instruction. "
                        "kind is one of suppress|surface|classify_hint|delegate|snooze_default. "
                        "Fill sender (exact email) OR sender_domain OR subject_re as the matcher; "
                        "leave the others empty. assignee is an employee id for delegate; hours is "
                        "an integer for snooze_default. Return empty strings for anything absent. "
                        "Never invent an automation or unattended kind."
                    ),
                },
                {"role": "user", "content": text.strip()[:1000]},
            ],
            required_keys=required,
            purpose="edc_rule_parse",
            temperature=0.0,
            max_tokens=300,
        )
    except Exception as exc:
        # Favourable-absence receipt (review minor): a silent ``None`` hid whether
        # the model was unavailable, timed out, or returned malformed JSON. Log the
        # exception TYPE (never str(exc) — it may echo untrusted text) so an
        # operator can tell a real outage from an un-parseable instruction.
        print(
            f"edc.rules.parse_nl_rule: complete_json failed ({type(exc).__name__}); "
            "no rule extracted",
            file=sys.stderr,
        )
        return None

    kind = str(parsed.get("kind") or "").strip().lower()
    # Clamp any automation guess down to its pre-fill peer — a sentence can never
    # request unattended authority (that is an explicit per-rule promotion only).
    if kind in ("auto_delegate",):
        kind = "delegate"
    if kind in ("auto_send",):
        kind = "surface"
    if kind not in NL_RULE_KINDS:
        return None

    matcher: dict[str, Any] = {}
    sender = str(parsed.get("sender") or "").strip()
    domain = str(parsed.get("sender_domain") or "").strip().lstrip("@")
    subject_re = str(parsed.get("subject_re") or "").strip()
    if sender:
        matcher["sender"] = sender
    if domain:
        matcher["sender_domain"] = domain
    if subject_re:
        # Bound the pattern (review minor): an over-long or pathological regex can
        # cost catastrophic backtracking every classify. Reject anything past a
        # sane length, and drop a pattern that will not compile — the matcher just
        # loses its subject clause rather than admitting a ReDoS vector.
        if len(subject_re) <= _MAX_SUBJECT_RE_LEN:
            try:
                re.compile(subject_re)
                matcher["subject_re"] = subject_re
            except re.error:
                pass
    if not matcher:
        return None

    action: dict[str, Any] = {}
    if kind == "delegate":
        assignee = str(parsed.get("assignee") or "").strip()
        if not assignee:
            return None
        action["assignee"] = assignee
    if kind == "snooze_default":
        try:
            action["hours"] = int(parsed.get("hours") or 0) or 24
        except (TypeError, ValueError):
            action["hours"] = 24
    return {"kind": kind, "matcher": matcher, "action": action, "category": ""}


def propose_nl_rule(
    store: DecisionStore,
    *,
    owner_employee_id: str,
    text: str,
    llm_client: _JsonClient | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """End-to-end NL rule creation: parse → file a PROPOSED rule + its Decision.

    Returns ``(rule, decision)`` or ``None`` when the instruction could not be
    parsed. Every kind lands ``proposed`` — one code path, zero silent activation.
    """
    parsed = parse_nl_rule(text, llm_client=llm_client)
    if parsed is None:
        return None
    return file_proposed_rule(
        store,
        owner_employee_id=owner_employee_id,
        kind=str(parsed["kind"]),
        matcher=dict(parsed["matcher"]),
        action=dict(parsed["action"]),
        category=str(parsed.get("category") or ""),
        created_from="nl",
    )


# ---------------------------------------------------------------------------
# Rule-proposal Decision approval / decline (decide-endpoint handlers).
# ---------------------------------------------------------------------------


def approve_rule_proposal(
    store: DecisionStore, decision: dict[str, Any], *, actor: str
) -> dict[str, Any]:
    """Approve a ``rule_proposal`` Decision: resolve it, then activate its rule.

    The Decision is resolved FIRST (one-shot CAS → a single winner across the
    dashboard/Slack race); only then is the linked rule flipped ``proposed→active``
    (idempotent — a second attempt is a no-op). The rule keeps its behavior kind:
    approving a ``delegate`` proposal makes it a PRE-FILL rule, not automation.
    """
    resolved = store.decide_rule_proposal(
        decision["id"],
        owner_employee_id=actor,
        actor=actor,
        approved=True,
    )
    rule_id = str(decision.get("source_ref") or "")
    if rule_id:
        store.activate_rule(rule_id, owner_employee_id=actor, approved_by=actor)
    return resolved


def decline_rule_proposal(
    store: DecisionStore, decision: dict[str, Any], *, actor: str
) -> dict[str, Any]:
    """Decline a ``rule_proposal`` Decision: resolve it, then decline its rule."""
    resolved = store.decide_rule_proposal(
        decision["id"],
        owner_employee_id=actor,
        actor=actor,
        approved=False,
    )
    rule_id = str(decision.get("source_ref") or "")
    if rule_id:
        store.decline_rule(rule_id, owner_employee_id=actor, declined_by=actor)
    return resolved


# ---------------------------------------------------------------------------
# Bootstrap seeder — steward.yaml alerts → emp_owner classify_hint rules.
# ---------------------------------------------------------------------------

_BOOTSTRAP_OWNER = "emp_owner"


def _bootstrap_matcher(entry: str) -> dict[str, Any] | None:
    value = (entry or "").strip()
    if not value:
        return None
    if value.startswith("@"):
        return {"sender_domain": value[1:].lower()}
    if "@" in value:
        return {"sender": value.lower()}
    return {"sender_domain": value.lower()}


def seed_bootstrap_rules(
    store: DecisionStore, cfg: Any, *, owner_employee_id: str = _BOOTSTRAP_OWNER
) -> int:
    """One-shot, idempotent seed of ``classify_hint`` rules from ``steward.yaml``.

    Copies ``alerts.vip_senders`` (as sender/domain hints) and
    ``alerts.urgent_patterns`` (as subject-regex hints) into the owner's ACTIVE
    rules with ``created_from='bootstrap'``. Idempotent: an equivalent bootstrap
    rule already present is skipped, so re-running the triage tick never twins a
    seed. Returns the number of rules created this call.
    """
    alerts = getattr(cfg, "alerts", None)
    vip_senders = list(getattr(alerts, "vip_senders", []) or [])
    urgent_patterns = list(getattr(alerts, "urgent_patterns", []) or [])

    existing = store.list_rules(owner_employee_id=owner_employee_id)
    seeded: set[str] = {
        _signature(rule.get("matcher") or {})
        for rule in existing
        if rule.get("created_from") == "bootstrap"
    }

    to_seed: list[dict[str, Any]] = []
    for entry in vip_senders:
        matcher = _bootstrap_matcher(str(entry))
        if matcher is not None:
            to_seed.append(matcher)
    for pattern in urgent_patterns:
        token = str(pattern or "").strip()
        if token:
            to_seed.append({"subject_re": re.escape(token)})

    created = 0
    for matcher in to_seed:
        sig = _signature(matcher)
        if sig in seeded:
            continue
        seeded.add(sig)
        store.create_rule(
            {
                "owner_employee_id": owner_employee_id,
                "kind": "classify_hint",
                "category": "bootstrap",
                "matcher": matcher,
                "action": {},
                "state": "active",
                "created_from": "bootstrap",
            }
        )
        created += 1
    return created


def _signature(matcher: dict[str, Any]) -> str:
    return "|".join(
        f"{key}={str(matcher.get(key) or '').lower()}"
        for key in ("sender", "sender_domain", "subject_re")
    )
