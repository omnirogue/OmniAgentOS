"""Skill router: pick 3–8 relevant skills by task attributes (Volume II §7)."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# ONE status vocabulary, shared with the `skills.status` CHECK constraint
# (migration 109): active | deprecated | experimental | archived | quarantined.
#
# Before 109 this module allowlisted approved/canary/deprecated/experimental
# while the table permitted only active/archived, so half of this file's status
# logic was unreachable: nothing could ever produce an "approved" or "canary"
# row, and nothing could produce a "deprecated"/"experimental" one either.
# Migration 109 makes deprecated/experimental representable (a vault note's
# `status:` frontmatter or PUT /api/skills sets them) and adds `quarantined`;
# approved/canary are dropped here because they still have no producer, and a
# status branch with no producer is decoration.
#
# Fillers and ordinary matches must be explicitly good statuses. Absent /
# empty / unknown / archived / quarantined never render as selected.
_STATUS_FILLABLE = frozenset({"active"})
# Domain matches may still surface deprecated/experimental at half score.
_STATUS_MATCHABLE = _STATUS_FILLABLE | frozenset({"deprecated", "experimental"})


@dataclass(frozen=True, slots=True)
class SkillHit:
    name: str
    version: str
    score: float
    reason: str


def _row_status(row: Mapping[str, Any]) -> str | None:
    """Return normalized status, or None when absent/empty (never invent approved)."""
    raw = row.get("status")
    if raw is None:
        return None
    status = str(raw).strip().lower()
    return status or None


def _semantic_query(
    *,
    domain: str,
    risk_class: str,
    allowed_tools: set[str],
    expected_artifacts: set[str],
) -> str:
    """Render the taxonomy inputs as a short natural-language retrieval query."""
    parts = []
    if domain:
        parts.append(f"domain {domain}")
    if risk_class:
        parts.append(f"risk {risk_class}")
    if allowed_tools:
        parts.append("tools " + " ".join(sorted(allowed_tools)))
    if expected_artifacts:
        parts.append("artifacts " + " ".join(sorted(expected_artifacts)))
    return " ".join(parts)


def _semantic_rerank(
    hits: list[SkillHit],
    registry: Sequence[Mapping[str, Any]],
    *,
    query: str,
    limit: int,
) -> list[SkillHit]:
    """Blend taxonomy relevance (75%) with cosine relevance (25%).

    Taxonomy scores are divided by the candidate-set maximum. Cosine scores are
    shifted from ``[-1, 1]`` into ``[0, 1]`` and clamped. Missing semantic hits
    receive zero. Lexical-fallback results are deliberately ignored: dependency
    failure must preserve the existing taxonomy order and scores.
    """
    if not hits or not query:
        return hits
    try:
        from omniagentos.semsearch.search import search

        semantic_hits = search(query, kind="skill", limit=max(32, limit * 4))
    except Exception:
        return hits
    if not semantic_hits or any(hit.source != "semantic" for hit in semantic_hits):
        return hits

    identity_to_name: dict[str, str] = {}
    for row in registry:
        name = str(row.get("name") or row.get("id") or "").strip()
        if not name:
            continue
        for field in ("id", "slug", "name"):
            identity = str(row.get(field) or "").strip()
            if identity:
                identity_to_name[identity] = name
    semantic_by_name = {
        identity_to_name.get(hit.ref_id, hit.ref_id): max(0.0, min(1.0, (hit.score + 1.0) / 2.0))
        for hit in semantic_hits
    }
    taxonomy_max = max(hit.score for hit in hits)
    reranked = [
        SkillHit(
            name=hit.name,
            version=hit.version,
            score=(0.75 * (hit.score / taxonomy_max))
            + (0.25 * semantic_by_name.get(hit.name, 0.0)),
            reason=f"{hit.reason},semantic_blend",
        )
        for hit in hits
    ]
    reranked.sort(key=lambda hit: (-hit.score, hit.name))
    return reranked


def select_skills(
    registry: Sequence[Mapping[str, Any]],
    *,
    domain: str | None = None,
    risk_class: str | None = None,
    allowed_tools: Sequence[str] | None = None,
    expected_artifacts: Sequence[str] | None = None,
    max_skills: int = 32,
    min_skills: int = 0,
) -> list[SkillHit]:
    """Rank registry rows; return top N with score > 0 (capped at max_skills).

    Registry row shape (flexible):
      name, version, domains[], risk_classes[], tools[], artifacts[], triggers[]
    """
    # No hard production cap — callers set max_skills; default is generous.
    max_skills = max(1, int(max_skills))
    domain_l = (domain or "").strip().lower()
    risk_l = (risk_class or "").strip().upper()
    tools = {str(t).lower() for t in (allowed_tools or ())}
    arts = {str(a).lower() for a in (expected_artifacts or ())}

    hits: list[SkillHit] = []
    for row in registry:
        name = str(row.get("name") or row.get("id") or "").strip()
        if not name:
            continue
        status = _row_status(row)
        # Unknown / absent / unfavourable status must not appear as a match.
        if status is None or status not in _STATUS_MATCHABLE:
            continue
        version = str(row.get("version") or "0")
        score = 0.0
        reasons: list[str] = []
        domains = [str(d).lower() for d in (row.get("domains") or row.get("domain") or [])]
        if isinstance(row.get("domain"), str):
            domains = [str(row["domain"]).lower()]
        if domain_l and domain_l in domains:
            score += 2.0
            reasons.append(f"domain:{domain_l}")
        risks = [str(r).upper() for r in (row.get("risk_classes") or row.get("risk_class") or [])]
        if isinstance(row.get("risk_class"), str):
            risks = [str(row["risk_class"]).upper()]
        if risk_l and risk_l in risks:
            score += 1.0
            reasons.append(f"risk:{risk_l}")
        row_tools = {str(t).lower() for t in (row.get("tools") or row.get("required_tools") or [])}
        if tools and row_tools & tools:
            score += 1.5
            reasons.append("tool_overlap")
        row_arts = {
            str(a).lower() for a in (row.get("artifacts") or row.get("expected_artifacts") or [])
        }
        if arts and row_arts & arts:
            score += 1.0
            reasons.append("artifact_overlap")
        # Status: prefer approved/canary; soft-penalize deprecated/experimental.
        if status in {"deprecated", "experimental"}:
            score *= 0.5
            reasons.append(f"status:{status}")
        if score > 0:
            hits.append(
                SkillHit(
                    name=name,
                    version=version,
                    score=score,
                    reason=",".join(reasons) or "match",
                )
            )
    hits.sort(key=lambda h: (-h.score, h.name))
    if os.environ.get("OMNIAGENTOS_SEMSEARCH") == "1":
        query = _semantic_query(
            domain=domain_l,
            risk_class=risk_l,
            allowed_tools=tools,
            expected_artifacts=arts,
        )
        hits = _semantic_rerank(hits, registry, query=query, limit=max_skills)
    selected = hits[:max_skills]
    need = max(0, int(min_skills))
    if len(selected) < need:
        # Pad with zero-score fillable skills so callers that request a
        # floor (preflight uses min_skills=3) do not silently under-select.
        # Never invent names; never promote non-fillable statuses as fillers.
        selected_names = {h.name for h in selected}
        matched_names = {h.name for h in hits}
        fillers: list[SkillHit] = []
        seen_filler_names: set[str] = set()
        for row in registry:
            name = str(row.get("name") or row.get("id") or "").strip()
            if (
                not name
                or name in selected_names
                or name in matched_names
                or name in seen_filler_names
            ):
                continue
            status = _row_status(row)
            # Allowlist only — absent defaults nowhere; unknown/archived/rejected out.
            if status is None or status not in _STATUS_FILLABLE:
                continue
            fillers.append(
                SkillHit(
                    name=name,
                    version=str(row.get("version") or "0"),
                    score=0.0,
                    reason="min_skills_fill",
                )
            )
            seen_filler_names.add(name)
        fillers.sort(key=lambda h: h.name)
        for filler in fillers:
            if len(selected) >= need or len(selected) >= max_skills:
                break
            if filler.name in selected_names:
                continue
            selected.append(filler)
            selected_names.add(filler.name)
    return selected
