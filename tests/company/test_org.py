"""org.seed() — idempotency, org structure, vault notes, human-edit safety."""

from __future__ import annotations

from pathlib import Path

from omniagentos.orgdims import company_org as org


def test_seed_creates_company_and_departments(store, vault_dir):
    summary = org.seed(store, vault_dir=vault_dir, vault_autocommit=False)

    units = store.list_org_units()
    names = {u.name for u in units}
    assert org.COMPANY_NAME in names
    for dept in org.DEPARTMENTS:
        assert dept["name"] in names

    company = next(u for u in units if u.name == org.COMPANY_NAME)
    assert company.kind == "company"
    for dept in org.DEPARTMENTS:
        unit = next(u for u in units if u.name == dept["name"])
        assert unit.kind == "department"
        assert unit.parent_id == company.id

    assert len(summary["org_units_created"]) == 1 + len(org.DEPARTMENTS)


def test_seed_creates_expected_agent_roster(store, vault_dir):
    org.seed(store, vault_dir=vault_dir, vault_autocommit=False)

    agents = store.list_agents()
    names = {a.name for a in agents}

    assert "CTO" in names
    for vp in ("VP Engineering", "VP Research", "VP Product", "VP Operations"):
        assert vp in names
    assert "Chief Judge" in names
    for judge in ("Judge Anthropic", "Judge OpenAI", "Judge XAI"):
        assert judge in names

    cto = next(a for a in agents if a.name == "CTO")
    assert cto.org_role == "cto"
    assert cto.harness == "cli-claude"

    judges = [a for a in agents if a.org_role == "judge"]
    assert len(judges) == 3
    assert {j.harness for j in judges} == {"cli-claude", "cli-codex", "cli-grok"}

    # exactly one org_role=manager agent per department (Chief Judge doubles as
    # Benchmark Lab's manager — no separate generic manager role there, so
    # department review never double-runs for that department).
    managers = [a for a in agents if a.org_role == "manager"]
    assert len(managers) == len(org.DEPARTMENTS)
    manager_depts = set()
    for m in managers:
        dept = store.get_org_unit(m.org_unit_id)
        manager_depts.add(dept.name)
    assert manager_depts == {d["name"] for d in org.DEPARTMENTS}


def test_seed_writes_vault_notes_for_new_agents(store, vault_dir):
    summary = org.seed(store, vault_dir=vault_dir, vault_autocommit=False)

    assert summary["vault_notes_written"]
    for path in summary["vault_notes_written"]:
        assert Path(path).is_file()

    # Migration 108 seeds broker grant-holder identities (``loop:<instance>``)
    # into ``agents``; they are not org agents and get no org vault note.
    #
    # Filtering here is only honest if the contract it assumes is asserted
    # somewhere, so both halves are stated: machine holders ARE present (they
    # are the identities grants foreign-key to) and are NOT org agents.
    from omniagentos.context.lanes import is_machine_identity

    everyone = store.list_agents()
    machines = [a for a in everyone if is_machine_identity(a.id)]
    assert machines, "migration 108's grant holders must be visible in `agents`"
    assert all(not a.vault_note_path for a in machines), (
        "a machine grant holder is not staff and must not acquire an org note"
    )

    agents = [a for a in everyone if not is_machine_identity(a.id)]
    with_notes = [a for a in agents if a.vault_note_path]
    assert len(with_notes) == len(agents)  # every seeded agent got a note path recorded


def test_seed_is_idempotent(store, vault_dir):
    first = org.seed(store, vault_dir=vault_dir, vault_autocommit=False)
    units_after_first = len(store.list_org_units())
    agents_after_first = len(store.list_agents())

    second = org.seed(store, vault_dir=vault_dir, vault_autocommit=False)

    assert len(store.list_org_units()) == units_after_first
    assert len(store.list_agents()) == agents_after_first
    assert second["org_units_created"] == []
    assert second["agents_created"] == []
    assert len(second["org_units_existing"]) == len(first["org_units_created"])
    assert len(second["agents_existing"]) == len(first["agents_created"])


def test_seed_never_overwrites_human_edited_charter(store, vault_dir):
    org.seed(store, vault_dir=vault_dir, vault_autocommit=False)

    cto = next(a for a in store.list_agents() if a.name == "CTO")
    store.update_agent(cto.id, charter="HUMAN EDITED — do not touch")

    org.seed(store, vault_dir=vault_dir, vault_autocommit=False)

    cto_after = store.get_agent(cto.id)
    assert cto_after.charter == "HUMAN EDITED — do not touch"


def test_seed_never_duplicates_names_on_unique_constraint(store, vault_dir):
    """Belt-and-suspenders: even if seed() were called concurrently-ish (name
    already present at create time), the DB's UNIQUE(name) is the backstop —
    but seed()'s own pre-check should mean create_agent is never even attempted
    for an existing name."""
    org.seed(store, vault_dir=vault_dir, vault_autocommit=False)
    agents_before = {a.name: a.id for a in store.list_agents()}

    org.seed(store, vault_dir=vault_dir, vault_autocommit=False)
    agents_after = {a.name: a.id for a in store.list_agents()}

    assert agents_before == agents_after
