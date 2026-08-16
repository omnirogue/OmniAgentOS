"""Scoped capability capture, ambient recall, and gated estate promotion.

Capability notes are ordinary Synapse facts with additional queryable metadata.  This
keeps one vector store and one recall algorithm while making the tenant predicate a
database concern rather than a prompt/frontmatter convention.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from omniagentos.contracts import default_db_path
from omniagentos.db.store import SqliteStore
from omniagentos.knowledge.contracts import Fact, PromotionGate
from omniagentos.knowledge.store import KnowledgeStore
from omniagentos.vault.sections import merge_human_section
from omniagentos.vault.write import _confined_target, write_note

CAPABILITY_RECALL_K = 6
CAPABILITY_RECALL_BUDGET = 320
STALE_AFTER_DAYS = 180
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_COMPANY_KEY_RE = re.compile(r"^co_[a-z0-9][a-z0-9_-]{0,251}$")
_FRONTMATTER_END = "---\n"
_PRIVATE_PROMOTION_MARKERS = (
    "customer",
    "price",
    "offer",
    "funnel",
    "campaign",
    "account",
    "@",
    "$",
)
_NUMERIC_DETAIL_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?(?!\w)")
_PROPER_NOUN_WORD = r"(?:[A-Z][a-z]{1,}|[A-Z]{2,})"
_PROPER_NOUN_SPAN_RE = re.compile(rf"\b{_PROPER_NOUN_WORD}(?:\s+{_PROPER_NOUN_WORD})+\b")
_PRIVATE_PROMOTION_SYMBOLS = ("%", "€", "£", "¥")


class CapabilityScope(StrEnum):
    COMPANY = "company"
    ESTATE = "estate"


class CapabilityKind(StrEnum):
    TOOL = "tool"
    TECHNIQUE = "technique"
    GOTCHA = "gotcha"
    VENDOR = "vendor"


def normalize_company_id(company_id: str | None) -> str | None:
    """Return the canonical ``org_companies.id`` spelling used by capture and recall.

    Company manifests historically carried either the slug (``initech``) or the
    canonical project key (``co_initech``). Normalizing both sides of the boundary
    prevents a company's own note from becoming silently invisible while retaining a
    strict, bounded identifier shape.
    """
    if company_id is None:
        return None
    normalized = str(company_id).strip().casefold()
    if not normalized:
        return None
    if not normalized.startswith("co_"):
        normalized = f"co_{normalized}"
    if not _COMPANY_KEY_RE.fullmatch(normalized):
        raise ValueError("company_id must be a canonical company id or slug")
    return normalized


class CapabilityProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    brand: str
    date: date

    @field_validator("run_id", "brand")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("capability provenance fields must not be blank")
        if "|" in normalized:
            raise ValueError("capability provenance fields must not contain '|'")
        return normalized

    def render(self) -> str:
        return f"{self.run_id} | {self.brand} | {self.date.isoformat()}"


class CapabilityDraft(BaseModel):
    """One—and only one—capability emitted by the learning distiller."""

    model_config = ConfigDict(frozen=True)

    statement: str
    domains: list[str]
    kind: CapabilityKind

    @field_validator("statement")
    @classmethod
    def _atomic_statement(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("capability statement must not be blank")
        if "\n" in value or "\r" in value:
            raise ValueError("one capability note must contain exactly one line")
        if len(normalized) > 600:
            raise ValueError("capability statement must be at most 600 characters")
        return normalized

    @field_validator("domains")
    @classmethod
    def _domains(cls, values: list[str]) -> list[str]:
        normalized = sorted({str(value).strip().lower() for value in values if str(value).strip()})
        if not normalized:
            raise ValueError("every capability requires at least one domain")
        if any(not _DOMAIN_RE.fullmatch(value) for value in normalized):
            raise ValueError("capability domains must be lowercase slug identifiers")
        return normalized


class CapabilityNote(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    statement: str
    scope: CapabilityScope
    company: str | None
    domains: list[str]
    kind: CapabilityKind
    provenance: CapabilityProvenance
    last_verified: date
    fact_id: int | None = None
    promoted_from: int | None = None

    @model_validator(mode="after")
    def _scope_shape(self) -> CapabilityNote:
        if self.scope is CapabilityScope.COMPANY and not (self.company or "").strip():
            raise ValueError("company-scoped capability requires company")
        if self.scope is CapabilityScope.ESTATE and self.company is not None:
            raise ValueError("estate-scoped capability must have company=null")
        return self


def classify_scope(
    *, company_id: str | None, requested_scope: CapabilityScope | str | None = None
) -> CapabilityScope:
    """Classify capture scope, defaulting uncertainty to the company boundary.

    Estate is accepted only as an explicit caller decision (seed/import workflows).
    The self-improve capture path never supplies it; company→estate is a later gate.
    """
    if requested_scope is None:
        resolved = CapabilityScope.COMPANY
    else:
        resolved = CapabilityScope(requested_scope)
    if resolved is CapabilityScope.COMPANY and not (company_id or "").strip():
        raise ValueError("classifier defaulted to company but no company_id was supplied")
    if resolved is CapabilityScope.ESTATE and company_id is not None:
        raise ValueError("explicit estate seed must not carry a company_id")
    return resolved


def _note_id(draft: CapabilityDraft, provenance: CapabilityProvenance) -> str:
    digest = hashlib.sha256(
        f"{draft.kind.value}\0{draft.statement}\0{provenance.render()}".encode()
    ).hexdigest()[:16]
    return f"cap-{digest}"


def _embed(store: KnowledgeStore, statement: str) -> list[float] | None:
    if store._embedder is None:
        return None
    try:
        vectors = store._embedder.embed([statement])
        return vectors[0] if vectors else None
    except Exception:
        # Match ordinary knowledge ingestion: an embedding outage queues an
        # unembedded note for later backfill; it never loses the learning.
        return None


def note_from_fact(fact: Fact) -> CapabilityNote:
    if not fact.capability_scope or not fact.capability_kind or not fact.capability_provenance:
        raise ValueError(f"fact {fact.id} is not a capability note")
    run_id, brand, observed = [part.strip() for part in fact.capability_provenance.split("|", 2)]
    return CapabilityNote(
        id=f"cap-fact-{fact.id}",
        statement=fact.statement,
        scope=CapabilityScope(fact.capability_scope),
        company=fact.company_id,
        domains=fact.domains,
        kind=CapabilityKind(fact.capability_kind),
        provenance=CapabilityProvenance(
            run_id=run_id, brand=brand, date=date.fromisoformat(observed)
        ),
        last_verified=fact.last_verified or fact.recorded_at.date(),
        fact_id=fact.id,
        promoted_from=fact.promoted_from,
    )


def render_capability_note(note: CapabilityNote) -> str:
    """Render the required leak-safety metadata as YAML frontmatter."""
    frontmatter = {
        "id": note.id,
        "type": "playbook",
        "discipline": note.domains[0],
        "created": note.provenance.date.isoformat(),
        "source_run": note.provenance.run_id,
        "confidence": "high",
        "status": "active",
        "supersedes": None,
        "scope": note.scope.value,
        "company": note.company,
        "domains": note.domains,
        "kind": note.kind.value,
        "provenance": note.provenance.render(),
        "last_verified": note.last_verified.isoformat(),
    }
    dumped = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    return (
        f"---\n{dumped}{_FRONTMATTER_END}\n"
        f"# Capability: {note.statement}\n\n"
        f"{note.statement}\n\n"
        "## Notes (human)\n"
    )


def _write_capability_note(vault_dir: str, note: CapabilityNote) -> str:
    """Write extended capability frontmatter through the vault's confinement rules.

    ``vault.write_note`` deliberately accepts only the frozen eight-field frontmatter
    contract. Capability notes have six mandatory security fields, so this narrow writer
    reuses its target confinement and human-section preservation without weakening the
    parser for every existing vault note.
    """
    target = _confined_target(vault_dir, f"capabilities/{note.id}.md")
    old_content = target.read_text(encoding="utf-8") if target.is_file() else None
    content = merge_human_section(render_capability_note(note), old_content)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return str(target.resolve())


def capture_capability(
    store: KnowledgeStore,
    draft: CapabilityDraft | Mapping[str, Any],
    *,
    run_id: str,
    brand: str,
    company_id: str | None,
    verified_at: date | str | None = None,
    requested_scope: CapabilityScope | str | None = None,
    vault_dir: str | None = None,
    autocommit: bool | None = None,
) -> CapabilityNote:
    """Capture one verified atomic capability through the existing facts store."""
    atomic = draft if isinstance(draft, CapabilityDraft) else CapabilityDraft.model_validate(draft)
    # Classify against the raw presence first: an explicit estate seed carrying even a
    # blank company field remains invalid rather than being normalized into ``None``.
    scope = classify_scope(company_id=company_id, requested_scope=requested_scope)
    normalized_company = (
        normalize_company_id(company_id) if scope is CapabilityScope.COMPANY else None
    )
    checked = (
        date.fromisoformat(verified_at)
        if isinstance(verified_at, str)
        else verified_at or datetime.now(UTC).date()
    )
    provenance = CapabilityProvenance(run_id=run_id, brand=brand, date=checked)
    note = CapabilityNote(
        id=_note_id(atomic, provenance),
        statement=atomic.statement,
        scope=scope,
        company=normalized_company if scope is CapabilityScope.COMPANY else None,
        domains=atomic.domains,
        kind=atomic.kind,
        provenance=provenance,
        last_verified=checked,
    )
    rendered = render_capability_note(note)
    episode_id = store.add_episode(
        source="curator",
        source_ref=run_id,
        content=rendered,
        discipline=atomic.domains[0],
    )
    embedding = _embed(store, atomic.statement)
    fact_id = store.add_fact(
        statement=atomic.statement,
        episode_id=episode_id,
        discipline=atomic.domains[0],
        scope="global",
        provenance="extracted",
        trust=0.8,
        confidence=0.9,
        importance=0.7,
        embedding=embedding,
        capability_scope=scope.value,
        company_id=note.company,
        domains=atomic.domains,
        capability_kind=atomic.kind.value,
        capability_provenance=provenance.render(),
        last_verified=checked.isoformat(),
    )
    # Activation is verification, not company→estate promotion. The scope columns
    # remain unchanged; only ``promote_to_estate`` may mint estate visibility.
    store.activate_capability(fact_id)
    persisted = note.model_copy(update={"fact_id": fact_id})
    if vault_dir is not None:
        # Capability notes are extended-frontmatter notes; see the writer's contract.
        del autocommit  # capability-note autocommit is intentionally not implicit
        _write_capability_note(vault_dir, persisted)
    return persisted


def capture_capabilities(
    store: KnowledgeStore,
    drafts: Sequence[CapabilityDraft | Mapping[str, Any]],
    **kwargs: Any,
) -> list[CapabilityNote]:
    """Validate the full distiller batch, then write one note per capability."""
    atomic = [
        draft if isinstance(draft, CapabilityDraft) else CapabilityDraft.model_validate(draft)
        for draft in drafts
    ]
    identities = {(draft.kind.value, draft.statement.casefold()) for draft in atomic}
    if len(identities) != len(atomic):
        raise ValueError("distiller batch contains duplicate capability notes")
    return [capture_capability(store, draft, **kwargs) for draft in atomic]


_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "video": ("video", "promo", "webinar", "reel", "animation"),
    "audio": ("audio", "voice", "music", "podcast", "sound"),
    "marketing": ("promo", "campaign", "funnel", "offer", "ad copy", "webinar"),
    "engineering": ("code", "api", "database", "test", "deploy", "repository"),
}


def infer_domains(text: str, paths: Sequence[str] = ()) -> list[str]:
    haystack = " ".join((text, *paths)).casefold()
    matched = [
        domain
        for domain, keywords in _DOMAIN_KEYWORDS.items()
        if any(word in haystack for word in keywords)
    ]
    return matched or ["general"]


def resolve_company_id(connection_or_store: Any, project_id: str | None) -> str | None:
    """Resolve ``projects.org_company_id`` without inventing a tenant on failure."""
    if not project_id:
        return None
    connection = getattr(connection_or_store, "_connection", connection_or_store)
    try:
        row = connection.execute(
            "SELECT org_company_id FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return None
        value = row["org_company_id"] if hasattr(row, "keys") else row[0]
        return normalize_company_id(str(value)) if value else None
    except Exception:
        return None


def ambient_capability_block(
    task_summary: str,
    *,
    company_id: str | None,
    paths: Sequence[str] = (),
    domains: Sequence[str] | None = None,
    budget_tokens: int = CAPABILITY_RECALL_BUDGET,
    k: int = CAPABILITY_RECALL_K,
    store: KnowledgeStore | None = None,
) -> str:
    """Recall company+estate namespaces from the brief itself, side-effect free."""
    from omniagentos.knowledge.brief_recall import compose_prompt
    from omniagentos.knowledge.recall import _get_store, recall, render_recall_block

    resolved_domains = list(domains or infer_domains(task_summary, paths))
    result = recall(
        store or _get_store(),
        prompt=compose_prompt(task_summary, paths, role="capability recall"),
        budget_tokens=budget_tokens,
        run_id=None,
        company_id=normalize_company_id(company_id),
        domains=resolved_domains,
        capability_only=True,
        k=k,
    )
    return render_recall_block(result, budget_tokens=budget_tokens) or ""


def safe_ambient_capability_block(*args: Any, **kwargs: Any) -> str:
    """Fail-open wrapper used by planning and spawn-time brief assembly."""
    try:
        return ambient_capability_block(*args, **kwargs)
    except Exception:
        return ""


def is_stale(last_verified: date | None, *, now: date | None = None) -> bool:
    if last_verified is None:
        return True
    today = now or datetime.now(UTC).date()
    return last_verified < today - timedelta(days=STALE_AFTER_DAYS)


def _sanitize_for_estate(statement: str, *, brand: str, company_id: str) -> str:
    """Validate an operator-authored estate statement; never derive one from company text."""
    cleaned = " ".join(statement.split()).strip(" .;,-")
    if not cleaned:
        raise ValueError("estate_statement must not be blank")
    for private_token in (brand, company_id):
        if private_token and private_token.casefold() in cleaned.casefold():
            raise ValueError("estate_statement contains company context")
    lowered = cleaned.casefold()
    if any(marker in lowered for marker in _PRIVATE_PROMOTION_MARKERS):
        raise ValueError("estate_statement contains a private promotion marker")
    if _NUMERIC_DETAIL_RE.search(cleaned):
        raise ValueError("estate_statement contains numeric detail")
    if any(symbol in cleaned for symbol in _PRIVATE_PROMOTION_SYMBOLS):
        raise ValueError("estate_statement contains financial or percentage detail")
    if _PROPER_NOUN_SPAN_RE.search(cleaned):
        raise ValueError("estate_statement contains a capitalized proper-noun span")
    return cleaned


def _playbook_content(domain: str, statements: Sequence[str]) -> str:
    frontmatter = {
        "id": f"estate-capabilities-{domain}",
        "type": "playbook",
        "discipline": domain,
        "created": datetime.now(UTC).date().isoformat(),
        "source_run": None,
        "confidence": "high",
        "status": "active",
        "supersedes": None,
    }
    dumped = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    lines = [
        f"---\n{dumped}---",
        "",
        f"# {domain} capability playbook",
        "",
        "## Estate capabilities (generated)",
        "",
    ]
    lines.extend(f"- {statement}" for statement in sorted(set(statements), key=str.casefold))
    lines.extend(["", "## Notes (human)", ""])
    return "\n".join(lines)


def push_to_domain_playbooks(note: CapabilityNote, vault_dir: str) -> list[str]:
    """Materialize estate knowledge on deterministic per-domain push paths."""
    if note.scope is not CapabilityScope.ESTATE:
        raise ValueError("only estate capabilities may enter shared domain playbooks")
    written: list[str] = []
    for domain in note.domains:
        relpath = f"playbook/domains/{domain}.md"
        target = Path(vault_dir) / relpath
        statements: list[str] = [note.statement]
        if target.is_file():
            in_generated = False
            for line in target.read_text(encoding="utf-8").splitlines():
                if line == "## Estate capabilities (generated)":
                    in_generated = True
                    continue
                if in_generated and line.startswith("## "):
                    in_generated = False
                if in_generated and line.startswith("- "):
                    statements.append(line[2:].strip())
        written.append(
            write_note(vault_dir, relpath, _playbook_content(domain, statements), autocommit=False)
        )
    return written


def promote_to_estate(
    store: KnowledgeStore,
    source_fact_id: int,
    *,
    promotion_gate: PromotionGate,
    actor: str,
    vault_dir: str,
    estate_statement: str | None = None,
    ledger_store: SqliteStore | None = None,
) -> CapabilityNote:
    """Redact, promote, audit, and push one company note after an explicit gate."""
    if not isinstance(promotion_gate, PromotionGate):
        raise TypeError("PromotionGate required for company-to-estate promotion")
    source = store.get_fact(source_fact_id)
    if source is None:
        raise ValueError(f"unknown capability fact {source_fact_id}")
    note = note_from_fact(source)
    if note.scope is not CapabilityScope.COMPANY or note.company is None:
        raise ValueError("only company capability notes may be promoted")
    if estate_statement is None:
        raise ValueError("promotion requires an operator-supplied estate_statement")
    statement = _sanitize_for_estate(
        estate_statement, brand=note.provenance.brand, company_id=note.company
    )
    checked = datetime.now(UTC).date()
    embedding = _embed(store, statement)
    estate_fact_id = store.promote_capability_to_estate(
        source_fact_id,
        statement=statement,
        actor=actor,
        last_verified=checked.isoformat(),
        embedding=embedding,
    )
    promoted = note.model_copy(
        update={
            "id": f"cap-fact-{estate_fact_id}",
            "statement": statement,
            "scope": CapabilityScope.ESTATE,
            "company": None,
            "last_verified": checked,
            "fact_id": estate_fact_id,
            "promoted_from": source_fact_id,
        }
    )
    # Promotion creates its own estate note; the company source remains intact.
    # This ensures every visible estate fact has the full frontmatter contract,
    # independently of the deterministic aggregate playbook push below.
    _write_capability_note(vault_dir, promoted)
    push_to_domain_playbooks(promoted, vault_dir)
    owned_ledger = ledger_store is None
    audit = ledger_store or SqliteStore(default_db_path())
    try:
        audit.insert_event(
            "capability.promoted",
            actor,
            "company_to_estate",
            "knowledge_fact",
            str(estate_fact_id),
            {
                "source_fact_id": source_fact_id,
                "source_company_id": note.company,
                "domains": note.domains,
                "kind": note.kind.value,
                "provenance": note.provenance.render(),
                "last_verified": checked.isoformat(),
            },
        )
    finally:
        if owned_ledger:
            audit.close()
    return promoted


__all__ = [
    "CapabilityDraft",
    "CapabilityKind",
    "CapabilityNote",
    "CapabilityProvenance",
    "CapabilityScope",
    "ambient_capability_block",
    "capture_capabilities",
    "capture_capability",
    "classify_scope",
    "infer_domains",
    "is_stale",
    "normalize_company_id",
    "promote_to_estate",
    "push_to_domain_playbooks",
    "resolve_company_id",
    "safe_ambient_capability_block",
]
