"""Classification service (P2) — deterministic + vocabulary match.

Grok Orchestrator is the declared classifier agent. High-confidence routine
labels auto-apply; protected dimensions (company/finance/legal risk) never
auto-apply from model confidence alone.
"""

from __future__ import annotations

import re
from typing import Any

from omniagentos.orgdims.contracts import (
    Classification,
    ClassificationResult,
    DimensionBundle,
    OrganizationContext,
    Provenance,
)
from omniagentos.orgdims.store import OrgDimsStore
from omniagentos.orgdims.taxonomy import (
    CHANNELS,
    LIFECYCLES,
    PRIORITIES,
    TAXONOMY_VERSION,
    WORKSTREAMS,
    resolve_workstream_alias,
)

# Confidence policy from the plan.
AUTO_APPLY = 0.90
PROVISIONAL = 0.70

# Fields that must never auto-apply from soft inference alone.
PROTECTED_FIELDS = frozenset(
    {
        "company_slug",
        "company_id",
        "product_slug",
        "product_id",
        "risk_class",  # only when irreversible
    }
)

# Classification / org / execution field names that can be locked or operator-owned.
_CLASSIFICATION_FIELDS = (
    "primary_workstream",
    "domains",
    "channels",
    "lifecycle",
    "priority",
    "risk_class",
)
_ORG_CONTEXT_FIELDS = (
    "company_id",
    "company_slug",
    "product_id",
    "product_slug",
    "initiative_id",
    "goal_ids",
    "epic_id",
)
_EXECUTION_LINK_FIELDS = (
    "preferred_agent_profiles",
    "loop_template_id",
)

_WORD = re.compile(r"[a-z0-9][a-z0-9\-/]+")
_MAX_CLASSIFICATION_TEXT_CHARS = 4096


def _bounded_classification_text(*parts: str | None) -> str:
    """Build the classifier corpus without copying unbounded caller input."""
    chunks: list[str] = []
    remaining = _MAX_CLASSIFICATION_TEXT_CHARS
    for part in parts:
        if remaining <= 0:
            break
        if chunks:
            chunks.append("\n")
            remaining -= 1
            if remaining <= 0:
                break
        value = str(part or "")
        chunks.append(value[:remaining])
        remaining -= min(len(value), remaining)
    return "".join(chunks).lower()


def _phrase_in_text(phrase: str, text: str) -> bool:
    """True when *phrase* appears as a whole token/phrase, not a substring.

    Mirrors channel matching (``meta`` ≠ ``metacog``). Bare ``alias in text``
    treated short aliases like ``bi`` / ``dev`` / ``ux`` as hits inside
    unrelated words (webinar, device, quux) — a non-result presented as a
    workstream label.
    """
    raw = str(phrase or "").strip().lower()
    if not raw:
        return False
    forms = {raw, raw.replace(" ", "-"), raw.replace("-", " ")}
    for form in forms:
        if not form:
            continue
        if " " in form:
            if re.search(rf"(?<![\w]){re.escape(form)}(?![\w])", text):
                return True
        elif re.search(rf"(?<![\w-]){re.escape(form)}(?![\w-])", text):
            return True
    return False


class ClassificationService:
    def __init__(self, store: OrgDimsStore) -> None:
        self.store = store

    def classify_text(
        self,
        *,
        object_type: str,
        object_id: str,
        title: str = "",
        description: str = "",
        discipline: str | None = None,
        priority: str | None = None,
        inherit: dict[str, Any] | None = None,
        apply: bool = True,
    ) -> ClassificationResult:
        text = _bounded_classification_text(title, description, discipline)
        tokens = set(_WORD.findall(text))
        evidence: list[str] = []
        field_conf: dict[str, float] = {}
        auto_applied: list[str] = []
        provisional: list[str] = []
        needs_review: list[str] = []

        inherit = inherit or {}
        org = OrganizationContext(
            company_slug=inherit.get("company_slug"),
            company_id=inherit.get("company_id"),
            product_slug=inherit.get("product_slug"),
            product_id=inherit.get("product_id"),
            initiative_id=inherit.get("initiative_id"),
            goal_ids=list(inherit.get("goal_ids") or []),
            epic_id=inherit.get("epic_id"),
        )

        # Resolve company/product from inherit only (protected).
        if org.company_slug and not org.company_id:
            co = self.store.get_company_by_slug(org.company_slug)
            if co:
                org.company_id = str(co["id"])
                evidence.append(f"inherit:company:{org.company_slug}")
                field_conf["company_slug"] = 1.0
        if org.company_id and org.product_slug and not org.product_id:
            pr = self.store.get_product_by_slug(org.company_id, org.product_slug)
            if pr:
                org.product_id = str(pr["id"])
                field_conf["product_slug"] = 1.0

        # Workstream — unmatched stays unknown (never invent a default label).
        # Three-valued: handle None explicitly (bare truthiness is the defect class).
        workstream, ws_conf, ws_ev = self._match_workstream(text, tokens, discipline)
        if workstream is not None:
            field_conf["primary_workstream"] = ws_conf
            evidence.extend(ws_ev)
        else:
            evidence.extend(ws_ev)
            needs_review.append("primary_workstream")

        # Domains under workstream (whole-token only — same trap as aliases).
        domains: list[str] = []
        if workstream is not None:
            for w in WORKSTREAMS:
                if w["slug"] == workstream:
                    for d in w.get("domains") or []:
                        if _phrase_in_text(str(d), text) or str(d) in tokens:
                            domains.append(d)
                            evidence.append(f"domain:{d}")
                    break
        if domains:
            field_conf["domains"] = 0.85

        # Channels (word-boundary for short tokens so "meta" ≠ "metacog")
        channels = []
        for c in CHANNELS:
            if _phrase_in_text(c, text):
                channels.append(c)
                evidence.append(f"channel:{c}")
        if channels:
            field_conf["channels"] = 0.88

        # Priority — unknown stays unknown (do not invent "normal" + flattering conf).
        pri: str | None = None
        if priority and priority.lower() in PRIORITIES:
            pri = priority.lower()
            field_conf["priority"] = 1.0
            evidence.append(f"explicit:priority:{pri}")
        else:
            for candidate in ("critical", "urgent", "high", "low"):
                if re.search(rf"\b{candidate}\b", text):
                    pri = "critical" if candidate == "critical" else candidate
                    field_conf["priority"] = 0.8
                    evidence.append(f"keyword:priority:{pri}")
                    break
        if pri is None:
            needs_review.append("priority")
            evidence.append("unmatched:priority")

        # Lifecycle — no keyword signal means unknown, not a free "ready".
        lifecycle: str | None = None
        for lc in LIFECYCLES:
            if re.search(rf"\b{lc}\b", text):
                lifecycle = lc
                field_conf["lifecycle"] = 0.85
                evidence.append(f"keyword:lifecycle:{lc}")
                break
        if lifecycle is None:
            needs_review.append("lifecycle")
            evidence.append("unmatched:lifecycle")

        # Risk class — no keyword signal means unknown, not "reversible_internal".
        risk: str | None = None
        if any(k in text for k in ("delete", "drop table", "payment", "wire", "irreversible")):
            risk = "irreversible"
            field_conf["risk_class"] = 0.75
            evidence.append("keyword:risk:irreversible")
            needs_review.append("risk_class")
        elif any(k in text for k in ("email send", "post to", "publish", "campaign", "stripe")):
            risk = "bounded_external"
            field_conf["risk_class"] = 0.8
            evidence.append("keyword:risk:bounded_external")
        elif any(k in text for k in ("read only", "research", "summarize", "review")):
            risk = "read_only"
            field_conf["risk_class"] = 0.82
            evidence.append("keyword:risk:read_only")
        else:
            needs_review.append("risk_class")
            evidence.append("unmatched:risk_class")

        # Prefer Grok orchestrator for engineering/metacog tasks
        classifier = "grok-orchestrator"
        agents = self._agent_for_workstream(workstream)
        loop_id = self._loop_for_workstream(workstream, risk)
        exec_links: dict[str, Any] = {
            "preferred_agent_profiles": agents,
        }
        if loop_id is not None:
            exec_links["loop_template_id"] = loop_id
        elif workstream is None:
            needs_review.append("loop_template_id")

        classification = Classification(
            primary_workstream=workstream,
            domains=domains[:6],
            channels=channels[:6],
            lifecycle=lifecycle,
            priority=pri,
            risk_class=risk,
        )

        # Confidence policy application
        confidences = field_conf
        overall = sum(confidences.values()) / len(confidences) if confidences else 0.0
        for field, conf in confidences.items():
            if field in PROTECTED_FIELDS and conf < 1.0:
                needs_review.append(field)
                continue
            if conf >= AUTO_APPLY:
                auto_applied.append(field)
            elif conf >= PROVISIONAL:
                provisional.append(field)
            else:
                needs_review.append(field)

        if risk == "irreversible":
            # Never auto-apply irreversible from soft match alone
            if "risk_class" in auto_applied:
                auto_applied.remove("risk_class")
            if "risk_class" not in needs_review:
                needs_review.append("risk_class")

        status = "applied"
        if needs_review and not auto_applied:
            status = "suggested"
        elif provisional or needs_review:
            status = "provisional"

        rationale = self._rationale(workstream, domains, channels, risk, evidence)
        bundle = DimensionBundle(
            organization_context=org,
            classification=classification,
            execution_links=exec_links,
            provenance=Provenance(
                source="deterministic+vocab",
                confidence=round(overall, 3),
                evidence_refs=evidence[:20],
                taxonomy_version=TAXONOMY_VERSION,
                rationale=rationale,
                classifier_agent=classifier,
            ),
        )

        # H-22: never wipe operator locks / human edits on reclassify (spawn,
        # board create, bulk). Merge before apply so callers with apply=False
        # (batched bulk) still receive a lock-safe bundle.
        preserved: list[str] = []
        if object_type == "board_task":
            existing = self.store.get_board_org(object_id)
            bundle, preserved = self.preserve_operator_and_locked(existing, bundle)
            if preserved:
                auto_applied = [f for f in auto_applied if f not in preserved]
                provisional = [f for f in provisional if f not in preserved]
                # Locked / operator-owned fields are settled; drop review noise.
                needs_review = [f for f in needs_review if f not in preserved]
                if preserved and "preserve:operator_or_locked" not in evidence:
                    evidence = list(evidence) + [f"preserve:{','.join(sorted(preserved)[:12])}"]
                    bundle.provenance.evidence_refs = evidence[:20]

        result = ClassificationResult(
            object_type=object_type,  # type: ignore[arg-type]
            object_id=object_id,
            bundle=bundle,
            status=status,  # type: ignore[arg-type]
            field_confidences=field_conf,
            auto_applied=sorted(set(auto_applied)),
            provisional=sorted(set(provisional)),
            needs_review=sorted(set(needs_review)),
        )

        if apply and object_type == "board_task":
            # M-40: the applied bundle and its audit row are one write. As two
            # autocommit statements a failure between them left a card
            # reclassified with no provenance row (or an audit row for a card
            # that was never changed), and re-running the classifier repairs
            # neither half.
            self.store.apply_classification_batch([(object_id, bundle, result)])
        elif apply:
            self.store.save_classification(result)

        return result

    @staticmethod
    def fields_to_preserve(existing: DimensionBundle) -> set[str]:
        """Return field names that auto-classification must not overwrite.

        - Explicit ``locked_fields`` always win (partial lock after a human edit).
        - A full human-edit envelope with no locks protects every set field so
          reclassify cannot silently undo operator work (H-22).
        """
        locked = {str(f) for f in (existing.locked_fields or []) if f}
        if locked:
            return locked
        if existing.provenance.source != "human_edit":
            return set()
        preserve: set[str] = set()
        cls = existing.classification
        for name in _CLASSIFICATION_FIELDS:
            val = getattr(cls, name, None)
            if val is None or val == [] or val == "":
                continue
            preserve.add(name)
        org = existing.organization_context
        for name in _ORG_CONTEXT_FIELDS:
            val = getattr(org, name, None)
            if val is None or val == [] or val == "":
                continue
            preserve.add(name)
        for name in _EXECUTION_LINK_FIELDS:
            if name in (existing.execution_links or {}):
                preserve.add(name)
        return preserve

    @classmethod
    def preserve_operator_and_locked(
        cls,
        existing: DimensionBundle | None,
        proposed: DimensionBundle,
    ) -> tuple[DimensionBundle, list[str]]:
        """Merge existing operator/locked values into a newly classified bundle.

        Always carries ``locked_fields`` forward (never reset by auto-classify).
        Returns ``(merged_bundle, preserved_field_names)``.
        """
        if existing is None:
            return proposed, []

        # Locks are sticky: classification must never clear them.
        proposed.locked_fields = list(existing.locked_fields or [])

        preserve = cls.fields_to_preserve(existing)
        if not preserve and not proposed.locked_fields:
            return proposed, []

        classification = proposed.classification.model_copy()
        org = proposed.organization_context.model_copy()
        links = dict(proposed.execution_links or {})
        applied: list[str] = []

        for field in sorted(preserve):
            if field in _CLASSIFICATION_FIELDS and hasattr(existing.classification, field):
                setattr(
                    classification,
                    field,
                    getattr(existing.classification, field),
                )
                applied.append(field)
            elif field in _ORG_CONTEXT_FIELDS and hasattr(existing.organization_context, field):
                setattr(
                    org,
                    field,
                    getattr(existing.organization_context, field),
                )
                applied.append(field)
            elif field in _EXECUTION_LINK_FIELDS:
                if field in (existing.execution_links or {}):
                    links[field] = existing.execution_links[field]
                    applied.append(field)
            elif field in (existing.execution_links or {}):
                links[field] = existing.execution_links[field]
                applied.append(field)

        proposed.classification = classification
        proposed.organization_context = org
        proposed.execution_links = links
        return proposed, applied

    def _match_workstream(
        self, text: str, tokens: set[str], discipline: str | None
    ) -> tuple[str | None, float, list[str]]:
        if discipline:
            resolved = resolve_workstream_alias(discipline)
            if resolved:
                return resolved, 0.95, [f"discipline:{discipline}->{resolved}"]

        best: str | None = None
        best_score = 0.0
        best_evidence: list[str] = []
        for w in WORKSTREAMS:
            score = 0.0
            slug = str(w["slug"])
            hits: list[str] = []
            if _phrase_in_text(slug, text) or slug in tokens:
                score += 0.9
                hits.append(f"workstream-slug:{slug}")
            for alias in w.get("aliases") or []:
                a = str(alias).lower()
                a_tok = a.replace(" ", "-")
                if _phrase_in_text(a, text) or a_tok in tokens or a in tokens:
                    score += 0.85
                    hits.append(f"alias:{a}->{slug}")
            for d in w.get("domains") or []:
                d_s = str(d)
                if _phrase_in_text(d_s, text) or d_s in tokens:
                    score += 0.35
                    hits.append(f"domain-hint:{d_s}->{slug}")
            if score > best_score:
                best_score = score
                best = slug
                best_evidence = hits
        if best and best_score >= 0.35:
            conf = min(0.97, 0.55 + best_score * 0.2)
            return best, conf, best_evidence[:5]
        # Unknown is unknown — do not invent a concrete workstream (was:
        # return "engineering", 0.55, ["default:engineering"]).
        return None, 0.0, ["unmatched:workstream"]

    @staticmethod
    def _agent_for_workstream(workstream: str | None) -> list[str]:
        # Grok owns metacog/control; domain workers still available.
        # Unknown workstream: classifier only — do not invent domain co-agents.
        base = ["grok-orchestrator"]
        if workstream is None:
            return list(base)
        if workstream in {"engineering", "product"}:
            return base + ["grok-verifier", "grok-self-repair"]
        if workstream in {"finance-admin", "legal-compliance"}:
            return base + ["grok-verifier"]  # high scrutiny
        if workstream in {"research", "data-analytics"}:
            return base + ["grok-self-learn", "grok-memory-curator"]
        if workstream in {"operations"}:
            return base + ["grok-self-repair", "grok-strategy-selector"]
        return base + ["grok-strategy-selector"]

    @staticmethod
    def _loop_for_workstream(workstream: str | None, risk: str | None) -> str | None:
        # Irreversible always escalates; unknown workstream must not invent solo_executor.
        if risk == "irreversible":
            return "generator_critic"
        if workstream is None:
            return None
        if workstream in {"creative", "advertising", "marketing"}:
            return "generate_critique_repair_verify"
        if workstream in {"finance-admin", "legal-compliance"}:
            return "verify_then_execute"
        if workstream == "research":
            return "hypothesis_portfolio"
        return "solo_executor"

    @staticmethod
    def _rationale(
        workstream: str | None,
        domains: list[str],
        channels: list[str],
        risk: str | None,
        evidence: list[str],
    ) -> str:
        parts = [
            f"workstream={workstream if workstream is not None else 'unset'}",
            f"domains={','.join(domains) or 'none'}",
            f"channels={','.join(channels) or 'none'}",
            f"risk={risk if risk is not None else 'unset'}",
            f"evidence_n={len(evidence)}",
            "classifier=grok-orchestrator",
        ]
        return "; ".join(parts)
