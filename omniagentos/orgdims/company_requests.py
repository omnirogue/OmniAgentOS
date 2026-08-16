"""Agent-request lifecycle, migrated to orgdims.

Also the ``python -m omniagentos.orgdims.company_requests`` CLI entry point
(including the detached ``design`` worker spawned by ``api/routes/org.py``).
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any, cast

from omniagentos.contracts import (
    NoteType,
    ResultStatus,
    VaultFrontmatter,
    default_db_path,
    default_vault_dir,
    utc_now_iso,
)
from omniagentos.orgdims.company_init import (
    DEFAULT_BUDGET,
    AdapterFn,
    adapter_text,
    default_adapter_fn,
    parse_json_maybe,
    slugify,
)
from omniagentos.reliability.contracts import ReliabilityStore
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.write import write_note

logger = logging.getLogger(__name__)

_VALID_ROLES = {"vp", "manager", "specialist", "judge"}
_VALID_HARNESSES = {
    "cli-claude",
    "cli-codex",
    "cli-grok",
    "cli-kimi",
    "cli-gemini",
    "cli-qwen",
}

_DESIGN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "title": {"type": "string"},
        "department": {"type": "string"},
        "role": {"type": "string"},
        "harness": {"type": "string"},
        "model": {"type": "string"},
        "charter": {"type": "string"},
        "schedule": {"type": "object"},
        "expertise": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "department", "role"],
}


def _design_prompt(store: ReliabilityStore, description: str) -> str:
    departments = ", ".join(sorted(u.name for u in store.list_org_units(kind="department")))
    return (
        "You are designing a new agent for an AI engineering company, from this request:\n\n"
        f'"{description}"\n\n'
        f"Existing departments: {departments}\n"
        f"Available harnesses: {', '.join(sorted(_VALID_HARNESSES))}\n"
        "Respond with STRICT JSON only, matching: "
        '{"name": str (short, unique, Title Case), "title": str (job title), '
        '"department": str (one of the existing departments above), '
        '"role": "vp|manager|specialist|judge", '
        '"harness": str (one of the available harnesses above), '
        '"model": str (optional model hint, or empty string), '
        '"charter": str (one or two sentences on scope), '
        '"schedule": {"cadence": "twice_daily|daily|weekly|on_demand", "callable": true}, '
        '"expertise": [str, ...]}'
    )


def _sanitize_design(
    store: ReliabilityStore, design: dict[str, Any], description: str
) -> dict[str, Any]:
    name = str(design.get("name") or "").strip() or f"Agent {utc_now_iso()[:10]}"
    existing_names = {a.name for a in store.list_agents()}
    if name in existing_names:
        suffix = 2
        while f"{name} {suffix}" in existing_names:
            suffix += 1
        name = f"{name} {suffix}"

    dept_names = {u.name.lower(): u.name for u in store.list_org_units(kind="department")}
    department = dept_names.get(str(design.get("department", "")).strip().lower())

    role_raw = design.get("role")
    role = str(role_raw) if role_raw in _VALID_ROLES else "specialist"
    harness = design.get("harness") if design.get("harness") in _VALID_HARNESSES else "cli-claude"
    schedule_raw = design.get("schedule")
    schedule = schedule_raw if isinstance(schedule_raw, dict) else {}
    if "cadence" not in schedule:
        schedule = {**schedule, "cadence": "on_demand"}
    if "callable" not in schedule:
        schedule = {**schedule, "callable": True}
    expertise = design.get("expertise") if isinstance(design.get("expertise"), list) else []

    return {
        "name": name,
        "title": str(design.get("title") or f"{role.title()}"),
        "department": department,
        "role": role,
        "harness": harness,
        "model": str(design.get("model") or "") or None,
        "charter": str(design.get("charter") or description)[:2000],
        "schedule": schedule,
        "expertise": expertise,
    }


def _canonical_operator_identity(requested_by: str | None) -> str:
    """Map a CLI-supplied operator name onto the canonical identity grammar.

    ``--requested-by`` used to be handed to ``create_agent_request(requested_by=)``,
    a parameter the store stopped reading when U-E7 made attribution canonical —
    so the flag silently did nothing and every CLI request was recorded as
    ``system``. A bare name is an operator, so it becomes ``human:<name>``; an
    already-canonical spelling is passed through untouched and validated at the
    write path.

    Module-internal: :func:`create` is the one production caller and the only
    place a CLI-supplied name enters this module.
    """
    claimed = (requested_by or "").strip()
    if not claimed:
        return "system"
    if claimed == "system" or claimed.split(":", 1)[0] in {"lane", "loop", "job", "human"}:
        return claimed
    return f"human:{claimed}"


def create(
    store: ReliabilityStore,
    description: str,
    requested_by: str = "owner",
    adapter_fn: AdapterFn | None = None,
    budget: dict[str, Any] | None = None,
) -> str:
    """Start an agent request."""
    fn = adapter_fn or default_adapter_fn
    budget = budget or DEFAULT_BUDGET

    req_id = store.create_agent_request(
        description=description,
        from_agent_id=_canonical_operator_identity(requested_by),
    )
    store.update_agent_request_status(req_id, status="designing")

    try:
        prompt = _design_prompt(store, description)
        agent_result = fn("cli-claude", prompt, output_schema=_DESIGN_SCHEMA, budget=budget)
        if agent_result.status != ResultStatus.OK:
            logger.warning(
                "company.requests: design call failed for %s: %s", req_id, agent_result.error
            )
            store.update_agent_request_status(
                req_id,
                status="failed",
                design_json={"error": agent_result.error or "adapter error"},
            )
            return req_id

        raw_design = parse_json_maybe(adapter_text(agent_result))
        if raw_design is None:
            logger.warning("company.requests: unparseable design output for %s", req_id)
            store.update_agent_request_status(
                req_id, status="failed", design_json={"error": "unparseable design output"}
            )
            return req_id

        design = _sanitize_design(store, raw_design, description)
        imp_id = store.create_improvement(
            origin="agent_request",
            kind="new_agent",
            title=f"New agent: {design['name']}",
            summary=f"Agent-requested by {requested_by}: {description}",
            root_cause="",
            proposal_json={
                "change_type": "new_agent",
                "files": [],
                "plan": [
                    f"Create agent {design['name']} ({design['title']}) in {design['department']}"
                ],
                "restart_required": False,
                "expected_impact": ", ".join(design.get("expertise", [])),
                "repro": "",
                "design": design,
                "agent_request_id": req_id,
            },
            created_by="company.requests",
        )
        store.update_agent_request_status(
            req_id, status="awaiting_approval", design_json=design, improvement_id=imp_id
        )
    except Exception:  # pragma: no cover
        logger.exception("company.requests: design step raised for %s", req_id)
        store.update_agent_request_status(
            req_id, status="failed", design_json={"error": "internal exception during design"}
        )

    return req_id


def mark_approved(store: ReliabilityStore, req_id: str, decided_by: str = "owner") -> None:
    """Record human/quorum approval."""
    req = store.get_agent_request(req_id)
    if req is None:
        raise ValueError(f"agent_request {req_id} not found")
    store.update_agent_request_status(
        req_id, status="approved", design_json=req.design_json, improvement_id=req.improvement_id
    )
    logger.info("company.requests: %s approved by %s", req_id, decided_by)


def reject(store: ReliabilityStore, req_id: str, decided_by: str = "owner", reason: str = "") -> None:
    """Record a rejection."""
    req = store.get_agent_request(req_id)
    if req is None:
        raise ValueError(f"agent_request {req_id} not found")
    design_json = dict(req.design_json or {})
    if reason:
        design_json["reject_reason"] = reason
    store.update_agent_request_status(
        req_id, status="rejected", design_json=design_json, improvement_id=req.improvement_id
    )
    logger.info("company.requests: %s rejected by %s (%s)", req_id, decided_by, reason)


def _design_note_body(design: dict[str, Any]) -> str:
    lines = [
        f"# {design.get('title') or design.get('name')}",
        "",
        f"- **Name:** {design.get('name')}",
        f"- **Org role:** {design.get('role')}",
        f"- **Department:** {design.get('department') or '(unplaced)'}",
        f"- **Harness:** `{design.get('harness')}`",
        f"- **Model:** {design.get('model') or '(harness default)'}",
        f"- **Schedule:** `{design.get('schedule')}`",
        f"- **Expertise:** {', '.join(design.get('expertise', [])) or '(none listed)'}",
        "",
        "## Charter",
        "",
        str(design.get("charter", "")),
        "",
        "## Notes (human)",
        "",
    ]
    return "\n".join(lines)


def create_agent_from_request(
    store: ReliabilityStore,
    req_id: str,
    vault_dir: str | None = None,
    vault_autocommit: bool | None = None,
) -> str:
    """Materialize the agent row from the approved design."""
    req = store.get_agent_request(req_id)
    if req is None:
        raise ValueError(f"agent_request {req_id} not found")
    if req.status != "approved":
        raise ValueError(f"agent_request {req_id} is not approved (status={req.status})")

    design = req.design_json or {}
    org_unit_id: str | None = None
    dept_name = design.get("department")
    if dept_name:
        for unit in store.list_org_units(kind="department"):
            if unit.name == dept_name:
                org_unit_id = unit.id
                break
    if org_unit_id is None:
        for unit in store.list_org_units(kind="company"):
            org_unit_id = unit.id
            break

    schedule = design.get("schedule") if isinstance(design.get("schedule"), dict) else {}
    agent_id = store.create_agent(
        name=design.get("name") or f"Agent-{req_id[-6:]}",
        org_unit_id=org_unit_id,
        org_role=design.get("role") or "specialist",
        title=design.get("title", ""),
        charter=design.get("charter", ""),
        model=design.get("model") or None,
        harness=design.get("harness") or "cli-claude",
        schedule_json=schedule,
    )

    vault = vault_dir or default_vault_dir()
    try:
        slug = slugify(design.get("name") or agent_id)
        fm = VaultFrontmatter(id=slug, type=NoteType.SOURCE, created=utc_now_iso(), status="active")
        note_path = write_note(
            vault,
            f"org/{slug}.md",
            render_frontmatter(fm) + "\n" + _design_note_body(design),
            autocommit=vault_autocommit,
        )
        store.update_agent(agent_id, vault_note_path=note_path)
    except Exception:  # pragma: no cover
        logger.exception("company.requests: failed writing vault note for new agent %s", agent_id)

    store.update_agent_request_status(
        req_id,
        status="created",
        design_json=design,
        improvement_id=req.improvement_id,
        agent_id=agent_id,
    )
    return agent_id


def approve_and_create(
    store: ReliabilityStore,
    req_id: str,
    decided_by: str = "owner",
    vault_dir: str | None = None,
    vault_autocommit: bool | None = None,
) -> str:
    """Approve + materialize in one call."""
    mark_approved(store, req_id, decided_by=decided_by)
    return create_agent_from_request(
        store, req_id, vault_dir=vault_dir, vault_autocommit=vault_autocommit
    )


def _make_store(db_path: str | None = None) -> ReliabilityStore:
    from omniagentos.reliability.store import SqliteReliabilityStore

    # cast: the concrete store's ``get_watch_cursor`` returns ``str | None`` while
    # the frozen protocol declares ``str``; everything this module calls matches.
    return cast(ReliabilityStore, SqliteReliabilityStore(db_path or default_db_path()))


class _ExistingRequestStore:
    """Make ``create`` consume one pre-created request row (API design worker)."""

    def __init__(self, store: Any, request_id: str) -> None:
        self._store = store
        self._request_id = request_id
        self._claimed = False

    def create_agent_request(self, description: str, **_kwargs: Any) -> str:
        if self._claimed:
            raise RuntimeError("design command attempted to create more than one request")
        self._claimed = True
        return self._request_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


def _design_existing_request(
    store: ReliabilityStore,
    request_id: str,
    *,
    adapter_fn: AdapterFn | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the design step against an existing pending/designing request row.

    Internal helper for the ``design`` CLI subcommand (``python -m`` worker).
    Far-side artifact: the returned dict includes ``status`` and ``design_json``
    from the updated row (not merely a process exit code).
    """
    request = store.get_agent_request(request_id)
    if request is None:
        raise ValueError(f"agent request {request_id} not found")
    if request.status not in {"pending", "designing"}:
        raise ValueError(
            f"agent request {request_id} cannot be designed from status {request.status}"
        )

    proxy = _ExistingRequestStore(store, request_id)
    create(
        cast(ReliabilityStore, proxy),
        request.description,
        requested_by=request.requested_by,
        adapter_fn=adapter_fn,
        budget=budget,
    )
    updated = store.get_agent_request(request_id)
    if updated is None:  # pragma: no cover -- the row existed above and is never deleted
        raise RuntimeError(f"agent request {request_id} disappeared during design")
    return {
        "id": updated.id,
        "status": updated.status,
        "design_json": updated.design_json,
        "improvement_id": updated.improvement_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omniagentos.orgdims.company_requests")
    parser.add_argument("--db", default=None, help="override the sqlite db path")
    sub = parser.add_subparsers(dest="command", required=True)

    # Legacy company.__main__ accepted `--db` after the subcommand
    # (e.g. `request "desc" --db PATH`, `design --request ID --db PATH`).
    # Parent `--db` still covers `--db PATH <subcommand> ...`. Each
    # subparser gets its own `--db` with SUPPRESS default so an
    # unspecified sub-level flag does not clobber a parent-provided value.
    def _add_legacy_db_flag(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--db", default=argparse.SUPPRESS)

    req_p = sub.add_parser("request", help="design a new agent from a description")
    req_p.add_argument("description")
    req_p.add_argument("--requested-by", default="owner")
    _add_legacy_db_flag(req_p)

    design_p = sub.add_parser(
        "design",
        help="run design on an existing pending/designing request (detached worker)",
    )
    design_p.add_argument("--request", required=True, dest="request_id")
    _add_legacy_db_flag(design_p)

    seed_p = sub.add_parser("seed", help="idempotent org seed")
    _add_legacy_db_flag(seed_p)

    review_p = sub.add_parser("review", help="run department health review(s)")
    review_p.add_argument("--department", default=None)
    review_p.add_argument("--audit-kind", default="twice_daily")
    _add_legacy_db_flag(review_p)

    approve_p = sub.add_parser(
        "approve", help="approve an awaiting_approval/pending request + create the agent"
    )
    approve_p.add_argument("request_id")
    approve_p.add_argument("--decided-by", default="owner")
    _add_legacy_db_flag(approve_p)

    reject_p = sub.add_parser("reject", help="reject a request")
    reject_p.add_argument("request_id")
    reject_p.add_argument("--reason", default="")
    reject_p.add_argument("--decided-by", default="owner")
    _add_legacy_db_flag(reject_p)

    list_p = sub.add_parser("list-requests", help="list agent requests")
    list_p.add_argument("--status", default=None)
    _add_legacy_db_flag(list_p)

    args = parser.parse_args(argv)
    store = _make_store(args.db)

    if args.command == "request":
        req_id = create(store, args.description, requested_by=args.requested_by)
        req = store.get_agent_request(req_id)
        if req is None:  # pragma: no cover -- create() always leaves a row behind
            raise RuntimeError(f"agent_request {req_id} missing after create")
        print(
            json.dumps(
                {"id": req.id, "status": req.status, "design_json": req.design_json}, indent=2
            )
        )
        return 0 if req.status != "failed" else 1

    if args.command == "design":
        try:
            payload = _design_existing_request(store, args.request_id)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(payload, indent=2))
        return 0 if payload["status"] != "failed" else 1

    if args.command == "seed":
        from omniagentos.orgdims import company_org

        print(json.dumps(company_org.seed(store), indent=2))
        return 0

    if args.command == "review":
        from omniagentos.orgdims import company_departments

        result = company_departments.run_department_reviews(
            store, department=args.department, audit_kind=args.audit_kind
        )
        print(json.dumps(result, indent=2))
        return 0 if not result["errors"] else 1

    if args.command == "approve":
        agent_id = approve_and_create(store, args.request_id, decided_by=args.decided_by)
        print(json.dumps({"agent_id": agent_id}, indent=2))
        return 0

    if args.command == "reject":
        reject(store, args.request_id, decided_by=args.decided_by, reason=args.reason)
        return 0

    if args.command == "list-requests":
        reqs = store.list_agent_requests(status=args.status)
        print(
            json.dumps(
                [{"id": r.id, "status": r.status, "description": r.description} for r in reqs],
                indent=2,
            )
        )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
