"""Executable fan-in pipeline: context → adjudicate → verify → learning (M-18).

Foundations in ``fanin/adjudicate`` stay pure. This module is the end-to-end
path that compiled context, optional domain packs, post-fan-in verification,
and learning exits actually run through — not just import.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniagentos.fanin.adjudicate import adjudicate
from omniagentos.fanin.types import Candidate, FanInMode, FanInResult

__all__ = [
    "FanInPipelineResult",
    "VerifyOutcome",
    "run_fanin_pipeline",
]


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    ok: bool
    decision: str
    evidence: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class FanInPipelineResult:
    """End-to-end fan-in outcome with context, verify, and learning stamps."""

    fanin: FanInResult
    context_fingerprint: str | None
    context_text: str
    domain_pack_path: str | None
    verify: VerifyOutcome | None
    learning_decision_id: str | None
    learning_exit: str
    needs_replan: bool
    stages: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fanin": self.fanin.to_dict(),
            "context_fingerprint": self.context_fingerprint,
            "context_text": self.context_text,
            "domain_pack_path": self.domain_pack_path,
            "verify": None
            if self.verify is None
            else {
                "ok": self.verify.ok,
                "decision": self.verify.decision,
                "evidence": dict(self.verify.evidence),
                "rationale": self.verify.rationale,
            },
            "learning_decision_id": self.learning_decision_id,
            "learning_exit": self.learning_exit,
            "needs_replan": self.needs_replan,
            "stages": list(self.stages),
        }


def _default_verify(selected: Sequence[Any], evidence: Mapping[str, Any]) -> VerifyOutcome:
    """Gate-G5 style mechanical verify after fan-in selection."""
    from omniagentos.gates.service import GateService

    if not selected:
        g5 = GateService().g5_local_verify(
            {"verify_ok": False, "mechanical_pass": False, "reason": "no_selection"}
        )
        return VerifyOutcome(
            ok=False,
            decision=g5.decision,
            evidence=dict(g5.evidence),
            rationale="No candidate selected for verification.",
        )

    ev = dict(evidence or {})
    self_attested = bool(
        ev.get("self_attested")
        or ev.get("self_attested_pass")
        or any(
            isinstance(getattr(c, "content", None), dict)
            and getattr(c, "content", {}).get("self_attested")
            for c in selected
        )
    )

    payload = {
        "verify_ok": not self_attested,
        "mechanical_pass": not self_attested,
        "selected_count": len(selected),
        "winner_id": getattr(selected[0], "id", None),
        "self_attested": self_attested,
        **ev,
    }
    # Explicit fail markers from criteria/adjudication evidence or self-attestation
    if ev.get("force_verify_fail") or self_attested:
        payload["verify_ok"] = False
        payload["mechanical_pass"] = False
        if self_attested and not ev.get("independent_pass"):
            payload["reason"] = "self_attested_verification_rejected"

    g5 = GateService().g5_local_verify(payload)
    return VerifyOutcome(
        ok=g5.decision == "allow",
        decision=g5.decision,
        evidence=dict(g5.evidence),
        rationale="G5 local verify after fan-in"
        if g5.decision == "allow"
        else "G5 local verify rejected self-attested or failed verification",
    )


def run_fanin_pipeline(
    candidates: Sequence[Candidate | Mapping[str, Any]],
    *,
    mode: FanInMode | str = FanInMode.ADJUDICATE,
    criteria: Mapping[str, Any] | None = None,
    context_layers: Sequence[tuple[str, str]] | None = None,
    domain_pack: str | Path | None = None,
    scope_root: str | Path | None = None,
    scope: str = "fanin",
    verify: Callable[[Sequence[Any], Mapping[str, Any]], VerifyOutcome] | None = None,
    learning_path: str | Path | None = None,
    decision_id: str | None = None,
    skip_verify_on_empty: bool = False,
) -> FanInPipelineResult:
    """Run the full fan-in path and return a structured multi-stage result.

    Stages (always recorded, even when a stage no-ops):

    1. ``compile_context`` — ghost packet fingerprint
    2. ``domain_pack`` — optional brand/domain pack materialize
    3. ``adjudicate`` — pure fan-in
    4. ``verify`` — post-fan-in gate (G5 by default)
    5. ``learning_exit`` — decision/outcome append
    """
    stages: list[str] = []

    # 1. Compiled context (ghost packet — hermetic)
    from omniagentos.context.ghost import build_ghost_packet

    layers = list(context_layers or ())
    if not layers:
        layers = [("task", "fan-in adjudication"), ("policy", "prefer scores over votes")]
    packet = build_ghost_packet(layers, metadata={"pipeline": "fanin", "scope": scope})
    stages.append("compile_context")
    context_text = packet.text()
    context_fp = packet.fingerprint

    # 2. Domain pack (optional, fail closed when path given but invalid)
    domain_path: str | None = None
    if domain_pack is not None:
        from omniagentos.brandpacks.pack import (
            load_brand_pack,
            materialize_to_inputs,
        )

        pack = load_brand_pack(domain_pack)
        if scope_root is not None:
            dest = materialize_to_inputs(pack, scope_root, scope=scope)
            domain_path = str(dest)
        else:
            domain_path = str(pack.path)
        # Inject domain constraints into context for observability
        banned = ", ".join(pack.banned_claims[:12]) if pack.banned_claims else "(none)"
        domain_layer = (
            "domain_pack",
            f"voice_chars={len(pack.voice_md)} offer_keys={sorted(pack.offer.keys())} banned={banned}",
        )
        packet = build_ghost_packet(
            [*layers, domain_layer],
            metadata={"pipeline": "fanin", "scope": scope, "domain_pack": domain_path},
        )
        context_text = packet.text()
        context_fp = packet.fingerprint
        stages.append("domain_pack")

    # 3. Adjudicate
    fanin = adjudicate(candidates, criteria, mode=mode)
    stages.append("adjudicate")

    # 4. Verify after fan-in (skip only when empty and explicitly allowed)
    verify_outcome: VerifyOutcome | None = None
    if fanin.selected or not skip_verify_on_empty:
        verifier = verify or _default_verify
        verify_outcome = verifier(fanin.selected, fanin.evidence)
        stages.append("verify")
    else:
        stages.append("verify_skipped")

    # Determine replan: adjudication may request it; verify failure also forces it
    # unless adjudication already selected and verify passed.
    needs_replan = bool(fanin.needs_replan)
    if verify_outcome is not None and not verify_outcome.ok:
        needs_replan = True
    if fanin.selected and verify_outcome is not None and verify_outcome.ok:
        # Successful verified selection must not false-replan.
        needs_replan = False

    # M-38: production false-replan guard. Verified selection is always final;
    # replan is only retained when guard allows it under real failure evidence.
    from omniagentos.swarm.cbm_wiring import guard_control_action

    reason_codes: list[str] = []
    if fanin.needs_replan:
        reason_codes.append("no_progress_cycles")  # adjudicate reject_replan
    if verify_outcome is not None and not verify_outcome.ok:
        reason_codes.append("verify_failed")
        reason_codes.append("no_progress_cycles")
    if fanin.selected and verify_outcome is not None and verify_outcome.ok:
        reason_codes.append("verified_selection")

    # Authoritative real evidence computed from candidates & verify outcome
    candidate_scores = [
        float(c.score) for c in candidates if hasattr(c, "score") and c.score is not None
    ]
    best_score = max(candidate_scores) if candidate_scores else 0.0
    verified_pass = bool(fanin.selected and verify_outcome is not None and verify_outcome.ok)
    real_progress = 1.0 if verified_pass else (0.5 * best_score if fanin.selected else 0.0)
    real_evidence_delta = best_score if verified_pass else 0.0
    real_stall_count = len([c for c in candidates if float(getattr(c, "score", 0) or 0) < 0.5])
    if verify_outcome is not None and not verify_outcome.ok:
        real_stall_count += 1

    if needs_replan:
        guarded_action, guard_reason = guard_control_action(
            "replan",
            reason_codes=reason_codes or ["no_progress_cycles"],
            evidence_delta=real_evidence_delta,
            stall_count=real_stall_count,
            progress=real_progress,
        )
        needs_replan = guarded_action == "replan"
    else:
        # Progressing/verified path: a decorative replan would be rewritten.
        guarded_action, guard_reason = guard_control_action(
            "continue",
            reason_codes=reason_codes or ["ok"],
            evidence_delta=real_evidence_delta or 0.2,
            progress=real_progress if fanin.selected else 0.0,
        )
        needs_replan = False
    stages.append(f"guard:{guard_reason}")

    # 5. Learning exit
    from omniagentos.contracts import new_id
    from omniagentos.learning.api import attach_outcome, log_decision

    did = decision_id or new_id("fin")
    learning_exit = (
        "accepted"
        if (fanin.selected and not needs_replan)
        else ("replan" if needs_replan else "rejected")
    )
    log_decision(
        {
            "decision_id": did,
            "decision_kind": "fanin_pipeline",
            "mode": fanin.mode.value if hasattr(fanin.mode, "value") else str(fanin.mode),
            "decision": fanin.decision,
            "needs_replan": needs_replan,
            "context_fingerprint": context_fp,
            "domain_pack_path": domain_path,
            "verify_ok": None if verify_outcome is None else verify_outcome.ok,
            "selected_ids": [c.id for c in fanin.selected],
        },
        path=learning_path,
    )
    attach_outcome(
        did,
        {
            "learning_exit": learning_exit,
            "rationale": fanin.rationale,
            "evidence": dict(fanin.evidence),
            "verify": None
            if verify_outcome is None
            else {"ok": verify_outcome.ok, "decision": verify_outcome.decision},
        },
        path=learning_path,
    )
    stages.append("learning_exit")

    return FanInPipelineResult(
        fanin=fanin,
        context_fingerprint=context_fp,
        context_text=context_text,
        domain_pack_path=domain_path,
        verify=verify_outcome,
        learning_decision_id=did,
        learning_exit=learning_exit,
        needs_replan=needs_replan,
        stages=tuple(stages),
    )
