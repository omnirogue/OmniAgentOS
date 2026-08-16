"""Idempotent org seed (§7), migrated to orgdims."""

from __future__ import annotations

import logging
from typing import Any

from omniagentos.contracts import NoteType, VaultFrontmatter, default_vault_dir, utc_now_iso
from omniagentos.orgdims.company_init import slugify
from omniagentos.reliability.contracts import ReliabilityStore
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.write import write_note

logger = logging.getLogger(__name__)

COMPANY_NAME = "OmniAgentOS"

DEPARTMENTS: list[dict[str, str]] = [
    {"name": "Engineering", "charter": "Ship and operate the runner, adapters, and API surface."},
    {
        "name": "Research",
        "charter": "Track model/tooling landscape; feed modelintel and capability bets.",
    },
    {
        "name": "Operations",
        "charter": "Scheduling, launchd jobs, queue health, day-to-day run hygiene.",
    },
    {
        "name": "Infrastructure",
        "charter": "DB, migrations, storage, server fleet, deploy pipeline.",
    },
    {"name": "Security", "charter": "Policy gates, secrets, sandboxing, injection defense."},
    {"name": "QA", "charter": "Test coverage, regression detection, sandbox/judge quality."},
    {"name": "Architecture", "charter": "Living docs, ARCHI.md, cross-system design coherence."},
    {"name": "Product", "charter": "Dashboard UX, agent-facing workflows, feature prioritization."},
    {
        "name": "Customer Experience",
        "charter": "the operator-facing notifications, briefings, approvals UX.",
    },
    {
        "name": "Cost Optimization",
        "charter": "Token/cost tracking, model routing efficiency, budgets.",
    },
    {"name": "Benchmark Lab", "charter": "Blind judging, champion evaluation, the judge panel."},
]

_AGENT_PLAN: list[dict[str, Any]] = [
    {
        "name": "CTO",
        "title": "Chief Technology Officer",
        "department": None,
        "org_role": "cto",
        "harness": "cli-claude",
        "charter": "Owns the daily backlog re-rank and the weekly deep architecture review; "
        "final narrative voice on what the company builds next.",
        "schedule_json": {"cadence": "daily", "callable": True},
    },
    {
        "name": "VP Engineering",
        "title": "VP of Engineering",
        "department": "Engineering",
        "org_role": "vp",
        "harness": "cli-claude",
        "charter": "Accountable for runner/adapter/API reliability and the engineering backlog.",
        "schedule_json": {"cadence": "weekly", "callable": True},
    },
    {
        "name": "VP Research",
        "title": "VP of Research",
        "department": "Research",
        "org_role": "vp",
        "harness": "cli-claude",
        "charter": "Accountable for model/tooling research feeding modelintel and capability bets.",
        "schedule_json": {"cadence": "weekly", "callable": True},
    },
    {
        "name": "VP Product",
        "title": "VP of Product",
        "department": "Product",
        "org_role": "vp",
        "harness": "cli-claude",
        "charter": "Accountable for dashboard UX and agent-facing workflow priorities.",
        "schedule_json": {"cadence": "weekly", "callable": True},
    },
    {
        "name": "VP Operations",
        "title": "VP of Operations",
        "department": "Operations",
        "org_role": "vp",
        "harness": "cli-claude",
        "charter": "Accountable for scheduling health, launchd jobs, and queue hygiene.",
        "schedule_json": {"cadence": "weekly", "callable": True},
    },
    {
        "name": "Chief Judge",
        "title": "Chief Judge",
        "department": "Benchmark Lab",
        "org_role": "manager",
        "harness": "cli-claude",
        "charter": "Runs the Benchmark Lab health review and owns judge-panel composition/availability.",
        "schedule_json": {"cadence": "twice_daily", "callable": True},
    },
    {
        "name": "Judge Anthropic",
        "title": "Panel Judge (anthropic family)",
        "department": "Benchmark Lab",
        "org_role": "judge",
        "harness": "cli-claude",
        "charter": "Blind judge, anthropic model family — default panel seat.",
        "schedule_json": {"cadence": "on_demand", "callable": True},
    },
    {
        "name": "Judge OpenAI",
        "title": "Panel Judge (openai family)",
        "department": "Benchmark Lab",
        "org_role": "judge",
        "harness": "cli-codex",
        "charter": "Blind judge, openai model family — default panel seat.",
        "schedule_json": {"cadence": "on_demand", "callable": True},
    },
    {
        "name": "Judge XAI",
        "title": "Panel Judge (xai family)",
        "department": "Benchmark Lab",
        "org_role": "judge",
        "harness": "cli-grok",
        "charter": "Blind judge, xai model family — default panel seat.",
        "schedule_json": {"cadence": "on_demand", "callable": True},
    },
    {
        "name": "Engineering Manager",
        "title": "Engineering Manager",
        "department": "Engineering",
        "org_role": "manager",
        "harness": "cli-claude",
        "charter": DEPARTMENTS[0]["charter"],
        "schedule_json": {"cadence": "twice_daily", "callable": True},
    },
    {
        "name": "Research Manager",
        "title": "Research Manager",
        "department": "Research",
        "org_role": "manager",
        "harness": "cli-claude",
        "charter": DEPARTMENTS[1]["charter"],
        "schedule_json": {"cadence": "twice_daily", "callable": True},
    },
    {
        "name": "Operations Manager",
        "title": "Operations Manager",
        "department": "Operations",
        "org_role": "manager",
        "harness": "cli-claude",
        "charter": DEPARTMENTS[2]["charter"],
        "schedule_json": {"cadence": "twice_daily", "callable": True},
    },
    {
        "name": "Infrastructure Manager",
        "title": "Infrastructure Manager",
        "department": "Infrastructure",
        "org_role": "manager",
        "harness": "cli-claude",
        "charter": DEPARTMENTS[3]["charter"],
        "schedule_json": {"cadence": "twice_daily", "callable": True},
    },
    {
        "name": "Security Manager",
        "title": "Security Manager",
        "department": "Security",
        "org_role": "manager",
        "harness": "cli-claude",
        "charter": DEPARTMENTS[4]["charter"],
        "schedule_json": {"cadence": "twice_daily", "callable": True},
    },
    {
        "name": "QA Manager",
        "title": "QA Manager",
        "department": "QA",
        "org_role": "manager",
        "harness": "cli-claude",
        "charter": DEPARTMENTS[5]["charter"],
        "schedule_json": {"cadence": "twice_daily", "callable": True},
    },
    {
        "name": "Architecture Manager",
        "title": "Architecture Manager",
        "department": "Architecture",
        "org_role": "manager",
        "harness": "cli-claude",
        "charter": DEPARTMENTS[6]["charter"],
        "schedule_json": {"cadence": "twice_daily", "callable": True},
    },
    {
        "name": "Product Manager",
        "title": "Product Manager",
        "department": "Product",
        "org_role": "manager",
        "harness": "cli-claude",
        "charter": DEPARTMENTS[7]["charter"],
        "schedule_json": {"cadence": "twice_daily", "callable": True},
    },
    {
        "name": "Customer Experience Manager",
        "title": "Customer Experience Manager",
        "department": "Customer Experience",
        "org_role": "manager",
        "harness": "cli-claude",
        "charter": DEPARTMENTS[8]["charter"],
        "schedule_json": {"cadence": "twice_daily", "callable": True},
    },
    {
        "name": "Cost Optimization Manager",
        "title": "Cost Optimization Manager",
        "department": "Cost Optimization",
        "org_role": "manager",
        "harness": "cli-claude",
        "charter": DEPARTMENTS[9]["charter"],
        "schedule_json": {"cadence": "twice_daily", "callable": True},
    },
    {
        "name": "Backend Reliability Engineer",
        "title": "Backend Reliability Engineer",
        "department": "Engineering",
        "org_role": "specialist",
        "harness": "cli-codex",
        "charter": "Deep-dives runner/adapter bugs surfaced by the Engineering manager's review.",
        "schedule_json": {"cadence": "on_demand", "callable": True},
    },
    {
        "name": "Research Analyst",
        "title": "Research Analyst",
        "department": "Research",
        "org_role": "specialist",
        "harness": "cli-grok",
        "charter": "Live web research on models/tooling for the Research manager's review.",
        "schedule_json": {"cadence": "on_demand", "callable": True},
    },
    {
        "name": "Infrastructure Engineer",
        "title": "Infrastructure Engineer",
        "department": "Infrastructure",
        "org_role": "specialist",
        "harness": "cli-codex",
        "charter": "DB/migration/deploy deep-dives surfaced by the Infrastructure manager's review.",
        "schedule_json": {"cadence": "on_demand", "callable": True},
    },
    {
        "name": "Security Analyst",
        "title": "Security Analyst",
        "department": "Security",
        "org_role": "specialist",
        "harness": "cli-claude",
        "charter": "Policy/sandbox/injection deep-dives surfaced by the Security manager's review.",
        "schedule_json": {"cadence": "on_demand", "callable": True},
    },
    {
        "name": "QA Engineer",
        "title": "QA Engineer",
        "department": "QA",
        "org_role": "specialist",
        "harness": "cli-codex",
        "charter": "Test-coverage and regression deep-dives surfaced by the QA manager's review.",
        "schedule_json": {"cadence": "on_demand", "callable": True},
    },
    {
        "name": "Cost Analyst",
        "title": "Cost Analyst",
        "department": "Cost Optimization",
        "org_role": "specialist",
        "harness": "cli-grok",
        "charter": "Token/cost trend deep-dives surfaced by the Cost Optimization manager's review.",
        "schedule_json": {"cadence": "on_demand", "callable": True},
    },
    {
        "name": "Benchmark Curator",
        "title": "Benchmark Curator",
        "department": "Benchmark Lab",
        "org_role": "specialist",
        "harness": "cli-gemini",
        "charter": "Curates held-out evals and champion history for the judge panel (fallback family).",
        "schedule_json": {"cadence": "on_demand", "callable": True},
    },
]


def _agent_note_body(agent_plan: dict[str, Any], department_name: str | None) -> str:
    lines = [
        f"# {agent_plan['title']}",
        "",
        f"- **Name:** {agent_plan['name']}",
        f"- **Org role:** {agent_plan['org_role']}",
        f"- **Department:** {department_name or COMPANY_NAME}",
        f"- **Harness:** `{agent_plan['harness']}`",
        f"- **Schedule:** `{agent_plan['schedule_json']}`",
        "",
        "## Charter",
        "",
        agent_plan["charter"],
        "",
        "## Notes (human)",
        "",
    ]
    return "\n".join(lines)


def seed(
    store: ReliabilityStore,
    *,
    vault_dir: str | None = None,
    vault_autocommit: bool | None = None,
) -> dict[str, Any]:
    """Idempotent org seed, migrated to orgdims."""
    vault = vault_dir or default_vault_dir()
    summary: dict[str, Any] = {
        "org_units_created": [],
        "org_units_existing": [],
        "agents_created": [],
        "agents_existing": [],
        "vault_notes_written": [],
    }

    existing_units = {u.name: u.id for u in store.list_org_units()}

    company_id = existing_units.get(COMPANY_NAME)
    if company_id is None:
        company_id = store.create_org_unit(name=COMPANY_NAME, kind="company", charter="OmniAgentOS")
        summary["org_units_created"].append(COMPANY_NAME)
    else:
        summary["org_units_existing"].append(COMPANY_NAME)

    dept_ids: dict[str, str] = {}
    for dept in DEPARTMENTS:
        name = dept["name"]
        existing_id = existing_units.get(name)
        if existing_id is None:
            dept_ids[name] = store.create_org_unit(
                name=name, kind="department", parent_id=company_id, charter=dept["charter"]
            )
            summary["org_units_created"].append(name)
        else:
            dept_ids[name] = existing_id
            summary["org_units_existing"].append(name)

    existing_agents = {a.name for a in store.list_agents()}

    for plan in _AGENT_PLAN:
        name = plan["name"]
        if name in existing_agents:
            summary["agents_existing"].append(name)
            continue

        dept_name = plan["department"]
        org_unit_id = dept_ids[dept_name] if dept_name else company_id
        agent_id = store.create_agent(
            name=name,
            org_unit_id=org_unit_id,
            org_role=plan["org_role"],
            title=plan["title"],
            charter=plan["charter"],
            harness=plan["harness"],
            schedule_json=plan["schedule_json"],
        )
        summary["agents_created"].append(name)

        slug = slugify(name)
        relpath = f"org/{slug}.md"
        try:
            fm = VaultFrontmatter(
                id=slug,
                type=NoteType.SOURCE,
                created=utc_now_iso(),
                status="active",
            )
            body = _agent_note_body(plan, dept_name)
            note_path = write_note(
                vault, relpath, render_frontmatter(fm) + "\n" + body, autocommit=vault_autocommit
            )
            store.update_agent(agent_id, vault_note_path=note_path)
            summary["vault_notes_written"].append(note_path)
        except Exception:  # pragma: no cover
            logger.exception("company.org: failed writing vault note for agent %s", name)

    return summary
