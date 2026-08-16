"""Facade implementing Phases 1–8 of the metacognition upgrade."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from omniagentos.contracts import utc_now_iso
from omniagentos.metacog.config import (
    artifacts_root,
    context_config,
    memory_promotion_mode,
    metacog_mode,
    skill_canary_mode,
    stall_config,
    strategy_switch_mode,
)
from omniagentos.metacog.contracts import (
    BUILTIN_STRATEGIES,
    ArtifactEnvelope,
    CheckpointPacket,
    ContextPacket,
    ControlAction,
    MemoryRecord,
    MetacognitiveState,
    ReflectionReport,
    SkillVersion,
    StrategyDef,
)
from omniagentos.metacog.store import MetacogStore

# Supported ArtifactEnvelope.schema_version values for on-read verification.
_SUPPORTED_ARTIFACT_SCHEMA_VERSIONS = frozenset({1})


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _estimate_tokens(text: str) -> int:
    # Cheap heuristic matching contracts.estimate_tokens spirit (~4 chars/token).
    return max(1, (len(text) + 3) // 4) if text else 0


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write bytes atomically: temp in same dir → fsync → os.replace.

    A crash or error before the final rename leaves either the previous good
    blob or nothing at ``target`` — never a truncated final path (M-03 / F-32).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _blob_matches_hash(path: Path, content_hash: str) -> bool:
    """Return True only when path exists and its bytes match content_hash."""
    try:
        if not path.is_file():
            return False
        return _sha256_bytes(path.read_bytes()) == content_hash
    except OSError:
        return False


def _normalize_action(action: str) -> str:
    """Normalize an action token for repeated-action comparison."""
    return re.sub(r"\s+", " ", str(action).strip().lower())


def _action_repetition_score(previous_actions: list[str] | None) -> float:
    """Score how stuck recent control/tool actions are on the same choice.

    M-19 / OA-020: ``evaluate(previous_actions=...)`` must drive a real
    repeated-action signal. Returns 0.0 with fewer than two non-empty actions;
    otherwise the fraction of earlier actions that match the most recent one.
    """
    acts = [_normalize_action(a) for a in (previous_actions or []) if a and str(a).strip()]
    if len(acts) < 2:
        return 0.0
    last = acts[-1]
    same = sum(1 for a in acts[:-1] if a == last)
    return same / max(1, len(acts) - 1)


def _output_repetition_score(recent_outputs: list[str] | None) -> float:
    """Score how similar recent free-text outputs are (existing output path)."""
    outs = [o.strip().lower() for o in (recent_outputs or []) if o and o.strip()]
    if len(outs) < 2:
        return 0.0
    last = outs[-1]
    same = sum(1 for o in outs[:-1] if o == last or (len(o) > 20 and o[:80] == last[:80]))
    return same / max(1, len(outs) - 1)


class MetacogService:
    """Single entrypoint for artifact/memory/metacog operations."""

    def __init__(self, store: MetacogStore | None = None, db_path: str | None = None) -> None:
        self.store = store or MetacogStore(db_path)
        self.store.ensure_builtin_strategies(list(BUILTIN_STRATEGIES))

    # ------------------------------------------------------------------
    # Phase 1 — artifacts + checkpoints
    # ------------------------------------------------------------------

    def register_artifact(
        self,
        *,
        artifact_type: str,
        content: str | bytes,
        format: str = "json",
        company_id: str = "default",
        project_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        producer_agent_id: str | None = None,
        provenance: dict[str, Any] | None = None,
        quality: dict[str, Any] | None = None,
        retention_class: str = "project",
        access_scope: str = "task",
        supersedes: list[str] | None = None,
        base_repo: str | None = None,
        base_sha: str | None = None,
        schema_version: int = 1,
    ) -> ArtifactEnvelope:
        if not artifact_type or not str(artifact_type).strip():
            raise ValueError("artifact_type is required")
        raw = content.encode("utf-8") if isinstance(content, str) else content
        if not raw:
            raise ValueError("content is required")
        if int(schema_version) not in _SUPPORTED_ARTIFACT_SCHEMA_VERSIONS:
            raise ValueError(
                f"unsupported artifact schema_version={schema_version}; "
                f"supported={sorted(_SUPPORTED_ARTIFACT_SCHEMA_VERSIONS)}"
            )
        content_hash = _sha256_bytes(raw)
        root = artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        # Content-addressed blob store: bytes are shared by hash, but envelope
        # registration is per-task (see MetacogStore.register_artifact).
        rel = f"{content_hash[:2]}/{content_hash}"
        path = root / rel
        # Never reuse a path that is missing or whose bytes fail the hash —
        # a mid-write crash can leave a truncated blob that would otherwise
        # permanently poison the content-addressed path (M-03 / F-32).
        if not _blob_matches_hash(path, content_hash):
            _atomic_write_bytes(path, raw)
            if not _blob_matches_hash(path, content_hash):
                raise ValueError(
                    f"artifact blob integrity check failed after write "
                    f"(path={path}, expected_hash={content_hash})"
                )

        art = ArtifactEnvelope(
            artifact_type=artifact_type.strip(),
            company_id=company_id,
            project_id=project_id,
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            producer_agent_id=producer_agent_id,
            content_uri=str(path),
            content_hash=content_hash,
            format=format,
            schema_version=int(schema_version),
            provenance=provenance or {},
            quality=quality or {"verification_status": "pending", "confidence": 0.0},
            retention_class=retention_class,
            access_scope=access_scope,
            supersedes=list(supersedes or []),
            base_repo=base_repo,
            base_sha=base_sha,
        )
        if not art.provenance.get("inputs") and not isinstance(art.provenance.get("inputs"), list):
            art.provenance.setdefault("inputs", [])
        return self.store.register_artifact(art)

    def get_artifact(self, artifact_id: str) -> ArtifactEnvelope | None:
        return self.store.get_artifact(artifact_id)

    def read_artifact_content(self, artifact_id: str) -> bytes:
        """Read artifact blob bytes, verifying content hash and schema version.

        Fail-closed (M-03 / F-32): missing, truncated, or corrupted blobs and
        unsupported schema versions raise ValueError rather than returning
        untrusted content.
        """
        art = self.store.get_artifact(artifact_id)
        if art is None:
            raise KeyError(f"artifact {artifact_id} not found")
        if int(art.schema_version) not in _SUPPORTED_ARTIFACT_SCHEMA_VERSIONS:
            raise ValueError(
                f"artifact {artifact_id} has unsupported schema_version={art.schema_version}"
            )
        if not art.content_uri:
            raise ValueError(f"artifact {artifact_id} has empty content_uri")
        if not art.content_hash or len(art.content_hash) != 64:
            raise ValueError(f"artifact {artifact_id} has invalid content_hash")
        path = Path(art.content_uri)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise ValueError(f"artifact {artifact_id} blob missing at {path}") from exc
        except OSError as exc:
            raise ValueError(f"artifact {artifact_id} blob unreadable at {path}: {exc}") from exc
        actual = _sha256_bytes(data)
        if actual != art.content_hash:
            raise ValueError(
                f"artifact {artifact_id} content hash mismatch: "
                f"expected={art.content_hash} actual={actual}"
            )
        return data

    def create_checkpoint(
        self,
        *,
        state: dict[str, Any],
        run_id: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        company_id: str = "default",
        project_id: str | None = None,
        completed_criteria: list[str] | None = None,
        pending_criteria: list[str] | None = None,
        side_effects: list[dict[str, Any]] | None = None,
        artifact_ids: list[str] | None = None,
        context_digest: str | None = None,
        safe_to_resume: bool | None = None,
        replay_risk: str = "none",
    ) -> CheckpointPacket:
        effects = list(side_effects or [])
        # Fail closed on high replay risk unless caller marks safe.
        high_risk = any(
            str(e.get("idempotency") or "") in {"", "none", "unknown"}
            and str(e.get("kind") or "") in {"external_write", "payment", "deploy", "delete"}
            for e in effects
        )
        if safe_to_resume is None:
            safe_to_resume = not high_risk
        if high_risk:
            replay_risk = "high"
            safe_to_resume = False
        ckp = CheckpointPacket(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            company_id=company_id,
            project_id=project_id,
            state=state,
            completed_criteria=list(completed_criteria or []),
            pending_criteria=list(pending_criteria or []),
            side_effects=effects,
            artifact_ids=list(artifact_ids or []),
            context_digest=context_digest,
            safe_to_resume=bool(safe_to_resume),
            replay_risk=replay_risk,
        )
        return self.store.save_checkpoint(ckp)

    def get_checkpoint(self, checkpoint_id: str) -> CheckpointPacket | None:
        return self.store.get_checkpoint(checkpoint_id)

    def resume_from_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        ckp = self.store.get_checkpoint(checkpoint_id)
        if ckp is None:
            raise KeyError(f"checkpoint {checkpoint_id} not found")
        if not ckp.safe_to_resume:
            raise ValueError(
                f"checkpoint {checkpoint_id} is not safe to resume (replay_risk={ckp.replay_risk})"
            )
        return {
            "checkpoint_id": ckp.id,
            "task_id": ckp.task_id,
            "run_id": ckp.run_id,
            "state": ckp.state,
            "completed_criteria": ckp.completed_criteria,
            "pending_criteria": ckp.pending_criteria,
            "artifact_ids": ckp.artifact_ids,
            "side_effects": ckp.side_effects,
            "context_digest": ckp.context_digest,
        }

    # ------------------------------------------------------------------
    # Phase 2 — context compiler
    # ------------------------------------------------------------------

    def compile_context(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        objective: str = "",
        acceptance_criteria: list[str] | None = None,
        company_id: str = "default",
        project_id: str | None = None,
        domain: str = "coding",
        include_shadow: bool = False,
    ) -> ContextPacket:
        """Compile metacog context for an agent or operator-facing caller.

        Agent-facing callers receive only promoted memory by default.  Operators and
        evaluators may opt into shadow candidates with ``include_shadow=True`` while
        reviewing promotion decisions.
        """
        cfg = context_config()
        budget = int(cfg["max_tokens"])
        omissions: list[str] = []
        sections: list[str] = []

        # Acceptance criteria must never be omitted.
        criteria = list(acceptance_criteria or [])
        if objective:
            sections.append(f"## Objective\n{objective.strip()}")
        if criteria:
            bullets = "\n".join(f"- {c}" for c in criteria)
            sections.append(f"## Acceptance criteria\n{bullets}")
        else:
            omissions.append("no_acceptance_criteria_provided")

        arts = self.store.list_artifacts(
            task_id=task_id, run_id=run_id, limit=int(cfg["max_artifacts"])
        )
        art_ids = [a.id for a in arts]
        if arts:
            lines = [
                f"- {a.id} type={a.artifact_type} hash={a.content_hash[:12]} "
                f"verify={a.quality.get('verification_status', 'pending')}"
                for a in arts
            ]
            sections.append("## Artifacts\n" + "\n".join(lines))
        else:
            omissions.append("no_artifacts")

        memories = self.store.search_memory(
            objective or task_id or "",
            company_id=company_id,
            project_id=project_id,
            statuses=["promoted"] if not include_shadow else ["promoted", "shadow"],
            limit=int(cfg["max_memories"]),
        )
        mem_ids = [m.id for m in memories]
        if memories:
            lines = [
                f"- {m.id} [{m.promotion_status}] conf={m.confidence:.2f}: {m.statement[:200]}"
                for m in memories
            ]
            sections.append("## Memory\n" + "\n".join(lines))
        else:
            omissions.append("no_memory_hits")

        skills = self.store.list_skill_versions()
        skill_slugs: list[str] = []
        skill_lines: list[str] = []
        for sk in skills:
            if sk.status not in {"shadow", "canary", "promoted"}:
                continue
            if domain and domain not in sk.body_md and sk.status == "promoted":
                pass
            skill_slugs.append(sk.skill_slug)
            skill_lines.append(f"- {sk.skill_slug}@{sk.version} status={sk.status}")
            if len(skill_slugs) >= int(cfg["max_skills"]):
                break
        if skill_lines:
            sections.append("## Skills\n" + "\n".join(skill_lines))
        else:
            omissions.append("no_skills")

        block = "\n\n".join(sections)
        tokens = _estimate_tokens(block)
        # Trim from bottom (skills first) if over budget — never drop objective/criteria.
        while tokens > budget and len(sections) > 2:
            sections.pop()
            omissions.append("truncated_for_budget")
            block = "\n\n".join(sections)
            tokens = _estimate_tokens(block)

        return ContextPacket(
            task_id=task_id,
            run_id=run_id,
            block=block,
            artifact_ids=art_ids,
            memory_ids=mem_ids,
            skill_slugs=skill_slugs,
            omissions=omissions,
            estimated_tokens=tokens,
            budget_tokens=budget,
        )

    # ------------------------------------------------------------------
    # Phase 3 — typed memory + promotion
    # ------------------------------------------------------------------

    def create_memory_candidate(
        self,
        *,
        statement: str,
        memory_type: str = "lesson",
        evidence: list[str],
        company_id: str = "default",
        project_id: str | None = None,
        domain: str | None = None,
        applicability: dict[str, Any] | None = None,
        confidence: float = 0.5,
    ) -> MemoryRecord:
        if not statement.strip():
            raise ValueError("statement is required")
        if not evidence:
            raise ValueError("memory candidates require linked evidence artifacts")
        # Verify at least one evidence artifact exists (fail closed).
        found = False
        for eid in evidence:
            if self.store.get_artifact(eid) is not None:
                found = True
                break
        if not found:
            raise ValueError("no evidence artifacts found for candidate")
        mem = MemoryRecord(
            type=memory_type,  # type: ignore[arg-type]
            statement=statement.strip(),
            evidence=list(evidence),
            confidence=max(0.0, min(1.0, confidence)),
            promotion_status="pending",
            company_id=company_id,
            project_id=project_id,
            domain=domain,
            applicability=applicability or {},
            sample_count=1,
        )
        return self.store.upsert_memory(mem)

    def promote_memory(self, memory_id: str, *, force: bool = False) -> MemoryRecord:
        mem = self.store.get_memory(memory_id)
        if mem is None:
            raise KeyError(f"memory {memory_id} not found")
        mode = memory_promotion_mode()
        if mode == "off" and not force:
            raise PermissionError("memory promotion is off")
        if not mem.evidence:
            raise ValueError("cannot promote memory without evidence")
        # Failed / weak candidates never promote without explicit force.
        if mem.promotion_status == "rejected" and not force:
            raise ValueError("cannot promote rejected memory candidate")
        if mem.invalidated_by:
            raise ValueError("cannot promote invalidated memory")
        if mem.confidence < 0.5 and not force:
            raise ValueError("cannot promote low-confidence memory candidate without force")

        if mode == "shadow" and not force:
            mem.promotion_status = "shadow"
        else:
            # enforce (or force): actually promote
            mem.promotion_status = "promoted"
        mem.valid_from = mem.valid_from or utc_now_iso()
        return self.store.upsert_memory(mem)

    def reject_memory(self, memory_id: str, *, reason: str = "failed_validation") -> MemoryRecord:
        """Mark a candidate rejected so the promoter will not advance it.

        Rejected is a promoter decision (not the same as invalidation).
        """
        mem = self.store.get_memory(memory_id)
        if mem is None:
            raise KeyError(f"memory {memory_id} not found")
        mem.promotion_status = "rejected"
        # Stash reason without using invalidated_by (that path is for stale/wrong facts).
        applicability = dict(mem.applicability or {})
        applicability["reject_reason"] = reason
        applicability["rejected_at"] = utc_now_iso()
        applicability["rejected_by"] = "promoter"
        mem.applicability = applicability
        return self.store.upsert_memory(mem)

    def invalidate_memory(
        self,
        memory_id: str,
        *,
        reason: str,
        file_paths: list[str] | None = None,
    ) -> MemoryRecord:
        mem = self.store.get_memory(memory_id)
        if mem is None:
            raise KeyError(f"memory {memory_id} not found")
        mem.invalidated_by = {
            "reason": reason,
            "file_paths": list(file_paths or []),
            "at": utc_now_iso(),
        }
        mem.confidence = min(mem.confidence, 0.2)
        if mem.promotion_status == "promoted":
            mem.promotion_status = "superseded"
        return self.store.upsert_memory(mem)

    def search_memory(self, query: str, **kwargs: Any) -> list[MemoryRecord]:
        return self.store.search_memory(query, **kwargs)

    def list_memories(
        self,
        *,
        promotion_status: str | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        return self.store.list_memories(
            promotion_status=promotion_status,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # Phase 4 — metacognitive monitoring
    # ------------------------------------------------------------------

    def evaluate(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        criteria_total: int = 0,
        criteria_passed: int = 0,
        progress_measured: bool = True,
        previous_progress: float | None = None,
        previous_actions: list[str] | None = None,
        strategy_id: str | None = None,
        strategy_age_iterations: int = 0,
        cost_used: float = 0.0,
        time_used: float = 0.0,
        fanout_active: int = 0,
        verifier_capacity_reserved: int = 0,
        context_pressure: float = 0.0,
        recent_outputs: list[str] | None = None,
    ) -> MetacognitiveState:
        stall_cfg = stall_config()
        total = max(0, int(criteria_total))
        if progress_measured and total == 0:
            raise ValueError("measured progress requires criteria_total > 0")
        passed = max(0, min(int(criteria_passed), total if total else int(criteria_passed)))
        progress = (passed / total) if total else 0.0
        prev = previous_progress if previous_progress is not None else progress
        evidence_delta = max(0.0, progress - prev)

        # Repetition: similar recent outputs AND/OR repeated prior actions (M-19).
        output_repetition = _output_repetition_score(recent_outputs)
        action_repetition = _action_repetition_score(previous_actions)
        repetition = max(output_repetition, action_repetition)

        # Prior snapshots for stall_count
        stall_count = 0
        if task_id and progress_measured:
            snaps = self.store.list_snapshots(
                task_id, limit=int(stall_cfg["max_no_progress_cycles"]) + 1
            )
            for snap in snaps:
                st = snap.get("state") or {}
                # Snapshots created before this field existed are measured for
                # backward compatibility. Unmeasured samples are neutral, not
                # no-progress observations, so skip rather than count or break.
                if not bool(st.get("progress_measured", True)):
                    continue
                if float(st.get("new_evidence_delta") or 0) < float(
                    stall_cfg["min_evidence_delta"]
                ):
                    stall_count += 1
                else:
                    break

        if progress_measured and evidence_delta < float(stall_cfg["min_evidence_delta"]):
            stall_count = max(stall_count, 1 if previous_progress is not None else 0)
            if previous_progress is not None and evidence_delta < float(
                stall_cfg["min_evidence_delta"]
            ):
                # Count this evaluation as no-progress too
                stall_count = max(stall_count, 1)
                # Look at consecutive
                if task_id:
                    snaps = self.store.list_snapshots(task_id, limit=5)
                    consecutive = 1  # this one
                    for snap in snaps:
                        st = snap.get("state") or {}
                        if not bool(st.get("progress_measured", True)):
                            continue
                        if float(st.get("new_evidence_delta") or 0) < float(
                            stall_cfg["min_evidence_delta"]
                        ):
                            consecutive += 1
                        else:
                            break
                    stall_count = consecutive

        quality: str = "flat"
        if evidence_delta > 0.05:
            quality = "improving"
        elif evidence_delta < 0.0 or repetition > float(stall_cfg["repetition_threshold"]):
            quality = "degrading"

        reason_codes: list[str] = []
        action: ControlAction = "continue"
        rep_threshold = float(stall_cfg["repetition_threshold"])

        max_stall = int(stall_cfg["max_no_progress_cycles"])
        if stall_count >= max_stall:
            reason_codes.append("no_progress_cycles")
            action = "replan" if strategy_age_iterations < 3 else "stop"
        # Output-text loop signal (existing path).
        if output_repetition >= rep_threshold:
            reason_codes.append("repetition")
            if action == "continue":
                action = "switch"
        # Action-history loop signal (M-19): previous_actions actually used.
        if action_repetition >= rep_threshold:
            reason_codes.append("repeated_action")
            if action == "continue":
                action = "switch"
        if context_pressure >= 0.85:
            reason_codes.append("context_pressure")
            if action == "continue":
                action = "checkpoint"
        if progress >= 1.0 and total > 0:
            reason_codes.append("criteria_met")
            action = "stop"
        if fanout_active > 0 and verifier_capacity_reserved <= 0:
            reason_codes.append("fanout_without_verifier")
            if action in {"continue", "widen"}:
                action = "prune"

        # Gate: two no-progress cycles cannot return CONTINUE unchanged.
        if stall_count >= max_stall and action == "continue":
            action = "replan"
            reason_codes.append("forced_no_continue")

        from omniagentos.swarm.cbm_wiring import guard_control_action

        guarded_action, guard_reason = guard_control_action(
            action,
            reason_codes=reason_codes,
            evidence_delta=evidence_delta,
            stall_count=stall_count,
            progress=progress,
        )
        if guard_reason != "passthrough" and f"guard:{guard_reason}" not in reason_codes:
            reason_codes.append(f"guard:{guard_reason}")
        action = guarded_action  # type: ignore[assignment]

        confidence = min(0.95, 0.4 + progress * 0.5 - repetition * 0.2)
        state = MetacognitiveState(
            objective_progress=progress,
            acceptance_criteria_passed=f"{passed}/{total}" if total else f"{passed}/?",
            progress_measured=progress_measured,
            strategy_id=strategy_id,
            strategy_age_iterations=strategy_age_iterations,
            new_evidence_delta=evidence_delta,
            quality_trend=quality,  # type: ignore[arg-type]
            confidence=max(0.05, confidence),
            uncertainty_type="implementation" if progress < 1.0 else "evaluation",
            stall_count=stall_count,
            repetition_score=repetition,
            context_pressure=context_pressure,
            cost_used=cost_used,
            time_used=time_used,
            fanout_active=fanout_active,
            verifier_capacity_reserved=verifier_capacity_reserved,
            next_control_action=action,
            reason_codes=reason_codes or ["ok"],
        )
        self.store.save_snapshot(state, run_id=run_id, task_id=task_id)
        return state

    # ------------------------------------------------------------------
    # Phase 5 — strategy control
    # ------------------------------------------------------------------

    def select_strategy(
        self,
        *,
        domain: str = "coding",
        novel: bool = False,
        failed_strategy: str | None = None,
        prefer_verifier: bool = False,
    ) -> StrategyDef:
        strategies = self.store.list_strategies(enabled_only=True)
        eligible = [
            s for s in strategies if domain in s.eligible_domains or not s.eligible_domains
        ] or strategies
        if novel:
            for s in eligible:
                if s.id == "hypothesis_portfolio":
                    return s
        if failed_strategy:
            for s in eligible:
                if s.id != failed_strategy and (not prefer_verifier or s.requires_verifier):
                    return s
        if prefer_verifier:
            for s in eligible:
                if s.requires_verifier:
                    return s
        return eligible[0] if eligible else BUILTIN_STRATEGIES[0]

    def switch_strategy(
        self,
        *,
        from_strategy: str | None,
        to_strategy: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        reason_codes: list[str] | None = None,
        evidence: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        mode = strategy_switch_mode()
        if mode == "off" and not force:
            raise PermissionError("strategy switching is off")
        target = to_strategy
        if not target:
            selected = self.select_strategy(failed_strategy=from_strategy)
            target = selected.id
        applied = mode == "enforce" or force
        decision_id = self.store.record_decision(
            action="switch",
            run_id=run_id,
            task_id=task_id,
            from_strategy=from_strategy,
            to_strategy=target,
            evidence=evidence,
            reason_codes=reason_codes or ["strategy_switch"],
            mode=mode if not force else "enforce",
            applied=applied,
        )
        return {
            "decision_id": decision_id,
            "from_strategy": from_strategy,
            "to_strategy": target,
            "mode": mode if not force else "enforce",
            "applied": applied,
            "note": None
            if applied
            else "shadow mode: switch recorded but not applied to live control plane",
        }

    # ------------------------------------------------------------------
    # Phase 6 — reflection + skill synthesis
    # ------------------------------------------------------------------

    def reflect(
        self,
        *,
        outcome: str,
        run_id: str | None = None,
        task_id: str | None = None,
        evidence_artifact_ids: list[str] | None = None,
        failure_summary: str = "",
        taxonomy: str | None = None,
    ) -> ReflectionReport:
        evidence = list(evidence_artifact_ids or [])
        taxonomy = taxonomy or self._classify_failure(failure_summary, outcome)
        candidates: list[str] = []
        report_body = {
            "outcome": outcome,
            "taxonomy": taxonomy,
            "failure_summary": failure_summary[:2000],
            "evidence": evidence,
        }
        # Only create memory candidates when evidence exists.
        if evidence and outcome in {"failed", "repaired", "incident", "completed"}:
            try:
                statement = self._lesson_from_outcome(outcome, taxonomy, failure_summary)
                mem = self.create_memory_candidate(
                    statement=statement,
                    memory_type="lesson" if outcome != "completed" else "procedure",
                    evidence=evidence,
                    confidence=0.55 if outcome != "completed" else 0.7,
                    applicability={"task_id": task_id, "run_id": run_id},
                )
                # Always route through promote_memory so one guard set applies
                # (no self-promote bypass of low-confidence / evidence rules).
                try:
                    mem = self.promote_memory(mem.id)
                except (ValueError, PermissionError, KeyError):
                    # Leave as pending; do not force promote.
                    pass
                candidates.append(mem.id)
            except ValueError:
                report_body["candidate_skipped"] = "no_valid_evidence"
        else:
            report_body["candidate_skipped"] = "no_evidence"

        report = ReflectionReport(
            run_id=run_id,
            task_id=task_id,
            outcome=outcome,
            taxonomy=taxonomy,
            report=report_body,
            candidate_ids=candidates,
        )
        return self.store.save_reflection(report)

    def synthesize_skill(
        self,
        *,
        skill_slug: str,
        memory_ids: list[str],
        version: str = "1.0.0",
        body_md: str | None = None,
        force: bool = False,
    ) -> SkillVersion:
        mode = skill_canary_mode()
        if mode == "off" and not force:
            raise PermissionError("skill synthesis is off")
        if not memory_ids:
            raise ValueError("skill synthesis requires source memory ids")
        memories = []
        for mid in memory_ids:
            m = self.store.get_memory(mid)
            if m is None:
                raise KeyError(f"memory {mid} not found")
            if m.promotion_status not in {"shadow", "promoted"}:
                raise ValueError(f"memory {mid} not eligible (status={m.promotion_status})")
            if not m.evidence:
                raise ValueError(f"memory {mid} lacks evidence")
            memories.append(m)
        # One-off success cannot become a global skill without sample_count.
        if (
            all(m.sample_count < 2 and m.promotion_status != "promoted" for m in memories)
            and not force
        ):
            raise ValueError("insufficient recurrence to synthesize a global skill")

        if body_md is None:
            body_md = self._skill_md_template(skill_slug, memories)

        if mode == "enforce" or force:
            status = "promoted"
        elif mode == "canary":
            status = "canary"
        else:
            status = "shadow"

        skill = SkillVersion(
            skill_slug=skill_slug,
            version=version,
            body_md=body_md,
            status=status,
            source_memory_ids=memory_ids,
        )
        return self.store.save_skill_version(skill)

    # ------------------------------------------------------------------
    # Phase 7 — domain helpers
    # ------------------------------------------------------------------

    def coding_task_fingerprint(
        self,
        *,
        objective: str,
        write_set: list[str] | None = None,
        repo: str | None = None,
    ) -> str:
        parts = [
            "coding",
            re.sub(r"\s+", " ", objective.strip().lower())[:200],
            ",".join(sorted(write_set or [])),
            repo or "",
        ]
        return _sha256_bytes("|".join(parts).encode("utf-8"))[:24]

    # ------------------------------------------------------------------
    # Phase 8 — health / mode surface
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": metacog_mode(),
            "memory_promotion": memory_promotion_mode(),
            "strategy_switch": strategy_switch_mode(),
            "skill_canary": skill_canary_mode(),
            "artifacts_root": str(artifacts_root()),
            "strategies": [s.id for s in self.store.list_strategies()],
        }

    # --- internals --------------------------------------------------------

    @staticmethod
    def _classify_failure(summary: str, outcome: str) -> str:
        text = (summary or "").lower()
        if outcome == "completed":
            return "success"
        if any(k in text for k in ("rate limit", "429", "quota")):
            return "rate_limit"
        if any(k in text for k in ("auth", "401", "403", "permission")):
            return "auth"
        if any(k in text for k in ("timeout", "deadline")):
            return "timeout"
        if any(k in text for k in ("assert", "test failed", "traceback")):
            return "verification_failure"
        if any(k in text for k in ("merge", "conflict")):
            return "integration_conflict"
        return "unknown"

    @staticmethod
    def _lesson_from_outcome(outcome: str, taxonomy: str, summary: str) -> str:
        base = summary.strip() or f"outcome={outcome}"
        return f"[{taxonomy}] {base[:400]}"

    @staticmethod
    def _skill_md_template(slug: str, memories: list[MemoryRecord]) -> str:
        lessons = "\n".join(f"- {m.statement}" for m in memories)
        evidence = sorted({e for m in memories for e in m.evidence})
        return (
            f"---\nname: {slug}\nversion: auto\nrisk_class: R1\n"
            f"evidence_artifacts: {evidence}\n---\n\n"
            f"# {slug}\n\n## Lessons\n{lessons}\n\n"
            "## Procedure\n1. Load context packet.\n2. Apply lessons above.\n"
            "3. Emit artifacts and request verification.\n\n"
            "## Verification\nIndependent verifier must pass acceptance criteria.\n"
        )
