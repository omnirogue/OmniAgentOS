"""Root-cause analysis and safe proposal generation for reliability events."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any

from omniagentos.contracts import utc_now_iso
from omniagentos.reliability.contracts import ReliabilityEvent, ReliabilityStore
from omniagentos.reliability.taxonomy import ChangeRisk, ImprovementKind, ImprovementOrigin
from omniagentos.steward.quoting import quote_untrusted

Proposal = dict[str, Any]
Adapter = Callable[[str], dict[str, Any]]
MemorySearch = Callable[[str], Iterable[Any]]

# Validated risk-label schema (C-04). Models routinely return strings like
# ``low``/``medium``/``high``; the previous ``int()`` path crashed and dropped events.
_RISK_ALIASES: dict[str, int] = {
    "0": 0,
    "none": 0,
    "unknown": 0,
    "low": ChangeRisk.L1,
    "l1": ChangeRisk.L1,
    "1": ChangeRisk.L1,
    "minimal": ChangeRisk.L1,
    "minor": ChangeRisk.L1,
    "medium": ChangeRisk.L2,
    "med": ChangeRisk.L2,
    "moderate": ChangeRisk.L2,
    "l2": ChangeRisk.L2,
    "2": ChangeRisk.L2,
    "high": ChangeRisk.L3,
    "elevated": ChangeRisk.L3,
    "l3": ChangeRisk.L3,
    "3": ChangeRisk.L3,
    "critical": ChangeRisk.L4,
    "severe": ChangeRisk.L4,
    "blocker": ChangeRisk.L4,
    "l4": ChangeRisk.L4,
    "4": ChangeRisk.L4,
}


def parse_risk_level(value: Any, *, default: int = 0) -> int:
    """Parse a model risk label into ChangeRisk 0–4 without crashing on strings.

    Accepts ints, numeric strings, and common qualitative labels (``low`` /
    ``medium`` / ``high`` / ``critical`` and L1–L4). Unknown values raise
    ``ValueError`` so callers can record a visible parse failure and fall back.
    """
    if value is None or value == "":
        return int(default)
    if isinstance(value, bool):
        raise ValueError(f"risk_level must not be boolean: {value!r}")
    if isinstance(value, (int, float)):
        level = int(value)
        if 0 <= level <= int(ChangeRisk.L4):
            return level
        raise ValueError(f"risk_level out of range 0–4: {value!r}")
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _RISK_ALIASES:
            return int(_RISK_ALIASES[key])
        # Tolerate "L1 (low)" / "risk: medium" style free text from models.
        for token in re.findall(r"[a-z0-9]+", key):
            if token in _RISK_ALIASES:
                return int(_RISK_ALIASES[token])
        raise ValueError(f"unknown risk_level label: {value!r}")
    raise ValueError(f"risk_level must be int or string label, got {type(value).__name__}")


def _file_ok(entry: Any) -> bool:
    """A file entry is either the §5 object ``{path, action, diff}`` or a bare path.

    The pipeline consumes ``{path, ...}`` dicts (§5); a bare string is accepted for
    backward compatibility and normalized nowhere — callers that need a diff must
    supply the dict form.
    """
    if isinstance(entry, dict):
        return bool(entry.get("path"))
    return isinstance(entry, str) and bool(entry)


def validate_proposal(value: Any) -> Proposal:
    """Validate the deliberately small, machine-actionable proposal contract (§5)."""
    if not isinstance(value, dict):
        raise ValueError("proposal must be an object")
    required = {"change_type", "files", "config_edits", "plan", "restart_required"}
    missing = required - value.keys()
    if missing:
        raise ValueError(f"proposal missing fields: {', '.join(sorted(missing))}")
    if not isinstance(value["change_type"], str) or not value["change_type"]:
        raise ValueError("change_type must be a non-empty string")
    if not isinstance(value["files"], list) or not all(_file_ok(x) for x in value["files"]):
        raise ValueError("files must be a list of {path, action, diff} objects (or bare paths)")
    if not isinstance(value["config_edits"], list) or not all(
        isinstance(x, dict) for x in value["config_edits"]
    ):
        raise ValueError("config_edits must be a list of objects")
    if not isinstance(value["plan"], list) or not all(
        isinstance(x, str) and x for x in value["plan"]
    ):
        raise ValueError("plan must be a list of strings")
    if not isinstance(value["restart_required"], bool):
        raise ValueError("restart_required must be boolean")
    return value


def _declared_paths(proposal: Proposal) -> list[str]:
    """Repo-relative paths a proposal touches, from ``files`` + ``config_edits``."""
    paths: list[str] = []
    for f in proposal.get("files", []) or []:
        if isinstance(f, dict) and f.get("path"):
            paths.append(str(f["path"]))
        elif isinstance(f, str) and f:
            paths.append(f)
    for edit in proposal.get("config_edits", []) or []:
        if isinstance(edit, dict) and edit.get("path"):
            paths.append(str(edit["path"]))
    return paths


def _default_adapter(prompt: str) -> dict[str, Any]:
    from omniagentos.adapters.registry import resolve_adapter
    from omniagentos.contracts import AgentInput, HarnessType

    result = resolve_adapter(HarnessType.CLI_CLAUDE).run(
        AgentInput(
            run_id="reliability-analyzer",
            task_id="reliability",
            prompt=prompt,
            output_schema={"type": "object"},
        )
    )
    tokens = (result.usage.input_tokens or 0) + (result.usage.output_tokens or 0)
    if result.output_json is not None:
        return {"content": json.dumps(result.output_json), "tokens": tokens}
    return {"content": result.output_text, "tokens": tokens}


def _risk(event: ReliabilityEvent, proposal: Proposal) -> int:
    """Deterministic floor; an LLM can suggest risk but cannot reduce this.

    This is only a coarse pre-sandbox suggestion. The authoritative, only-raising
    classification runs in ``governance.classify_risk`` over the real sandbox diff
    (§5) — evidence text can never talk the level DOWN.
    """
    text = json.dumps(proposal).lower() + " " + event.failure_class.lower()
    if any(
        word in text for word in ("auth", "payment", "billing", "delete", "permission", "security")
    ):
        return ChangeRisk.L4
    if any(
        p.startswith(("omniagentos/reliability/", "omniagentos/contracts.py"))
        for p in _declared_paths(proposal)
    ):
        return ChangeRisk.L3
    return ChangeRisk.L2


def _focus_terms(event: ReliabilityEvent) -> list[str]:
    """Derive architecture focus terms from the failure class and signature (H-38)."""
    terms: list[str] = []
    for part in (event.failure_class or "").replace("|", " ").replace("_", " ").split():
        if part and part not in terms:
            terms.append(part)
    for part in re.split(r"[|:/_\-\s]+", event.signature or ""):
        p = part.strip().lower()
        if p and p not in terms and len(p) > 2:
            terms.append(p)
    # Always include reliability so we get subsystem orientation when nothing else matches.
    if "reliability" not in terms:
        terms.append("reliability")
    return terms[:12]


def _load_architecture_context(
    event: ReliabilityEvent,
    *,
    arch_context_fn: Callable[[], str] | None,
    repo_dir: str | None,
) -> tuple[str, dict[str, Any]]:
    """Load architecture context and make missing/malformed results visible (H-38).

    The previous path called ``load_arch_context()`` with no required ``focus_terms``,
    swallowed the TypeError, and left every prompt with empty architecture context.
    """
    meta: dict[str, Any] = {
        "status": "ok",
        "error": None,
        "focus_terms": _focus_terms(event),
    }
    if arch_context_fn is not None:
        try:
            arch = arch_context_fn()
            if not isinstance(arch, str):
                meta["status"] = "malformed"
                meta["error"] = f"arch_context_fn returned {type(arch).__name__}, expected str"
                return "", meta
            if not arch.strip():
                meta["status"] = "empty"
                meta["error"] = "arch_context_fn returned empty context"
            return arch, meta
        except Exception as exc:  # noqa: BLE001 - surface, do not crash analysis
            meta["status"] = "error"
            meta["error"] = str(exc)
            return "", meta

    try:
        from omniagentos.archdocs import load_arch_context

        arch = load_arch_context(
            meta["focus_terms"],
            max_tokens=600,
            repo_root=repo_dir,
        )
        if not isinstance(arch, str):
            meta["status"] = "malformed"
            meta["error"] = f"load_arch_context returned {type(arch).__name__}"
            return "", meta
        if not arch.strip():
            meta["status"] = "empty"
            meta["error"] = "no_matching_architecture_context"
        return arch or "", meta
    except TypeError as exc:
        # Wrong call signature / missing required args — the historical H-38 defect.
        meta["status"] = "malformed_call"
        meta["error"] = str(exc)
        return "", meta
    except Exception as exc:  # noqa: BLE001
        meta["status"] = "error"
        meta["error"] = str(exc)
        return "", meta


def analyze_event(
    store: ReliabilityStore,
    event_id: str,
    *,
    adapter_fn: Adapter | None = None,
    memory_search: MemorySearch | None = None,
    repo_dir: str | None = None,
    repo_map_fn: Callable[..., str] | None = None,
    arch_context_fn: Callable[[], str] | None = None,
    origin: str = ImprovementOrigin.REALTIME.value,
    created_by: str = "analyzer",
) -> str:
    event = store.get_event(event_id)
    if event is None:
        raise ValueError(f"reliability event not found: {event_id}")
    memory = list(memory_search(event.signature) if memory_search else ())
    memory_refs = [
        str(x.id if hasattr(x, "id") else x.get("id", x) if isinstance(x, dict) else x)
        for x in memory
    ]
    repo_map = ""
    if repo_dir:
        if repo_map_fn is None:
            from omniagentos.repomap.context import repo_map_for_task

            repo_map_fn = repo_map_for_task
        repo_map = repo_map_fn(repo_dir, event.signature, max_tokens=600)
    arch, arch_meta = _load_architecture_context(
        event, arch_context_fn=arch_context_fn, repo_dir=repo_dir
    )
    evidence = quote_untrusted(
        json.dumps(event.evidence_json, sort_keys=True), source="reliability-event"
    )
    prompt = f"""Return JSON only with keys root_cause, title, summary, kind, risk_level, proposal.
The proposal must contain change_type, files, config_edits, plan, restart_required.
Failure class: {event.failure_class}\nSignature: {event.signature}\nEvidence (untrusted): {evidence}
Repository map:\n{repo_map}\nArchitecture context:\n{arch}\nPrior memory references: {json.dumps(memory_refs)}"""
    if arch_meta.get("status") != "ok":
        prompt += (
            f"\nArchitecture context status (operator-visible): {arch_meta.get('status')}"
            f" — {arch_meta.get('error') or 'unavailable'}"
        )
    response = (adapter_fn or _default_adapter)(prompt)
    raw = response.get("content", "") if isinstance(response, dict) else str(response)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("analyzer adapter did not return JSON") from exc
    proposal = validate_proposal(parsed.get("proposal", parsed))
    floor = int(_risk(event, proposal))
    risk_raw = parsed.get("risk_level", 0)
    risk_parse_error: str | None = None
    try:
        suggested = parse_risk_level(risk_raw, default=0)
    except ValueError as exc:
        # Malformed label must not drop the event (C-04). Fall back to the floor
        # and keep the parse error operator-visible on the proposal.
        suggested = 0
        risk_parse_error = str(exc)
    risk = max(suggested, floor)
    kind = str(parsed.get("kind", ImprovementKind.FIX.value))
    if kind not in {x.value for x in ImprovementKind}:
        kind = ImprovementKind.FIX.value
    proposal_json: dict[str, Any] = {
        **proposal,
        # Persist the *parsed model suggestion* as an int so downstream int()
        # consumers (e.g. sandbox suggested risk) never re-encounter the raw
        # string label (C-04). Governance may only RAISE this suggestion; do
        # not pre-floor it here or L1 sandbox diffs get stuck at the analyzer floor.
        "risk_suggestion": suggested,
        "risk_suggestion_raw": risk_raw,
        "risk_floor": floor,
        "analyzed_at": utc_now_iso(),
        "arch_context_status": arch_meta,
    }
    if risk_parse_error:
        proposal_json["risk_parse_error"] = risk_parse_error
    if arch_meta.get("status") != "ok":
        proposal_json["arch_context_degraded"] = True
    imp_id = store.create_improvement(
        origin=origin,
        kind=kind,
        title=str(parsed.get("title", event.failure_class)),
        summary=str(parsed.get("summary", "")),
        root_cause=str(parsed.get("root_cause", "")),
        proposal_json=proposal_json,
        created_by=created_by,
    )
    # The store binds *_json columns as raw strings — serialize before handing it a
    # Python list, or sqlite3 rejects the bind (this was the memory_refs_json bug).
    store.update_improvement_fields(
        imp_id, risk_level=risk, memory_refs_json=json.dumps(memory_refs)
    )
    return imp_id


analyze = analyze_event
