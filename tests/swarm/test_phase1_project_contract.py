"""B3 project-aware contracts: registry roots, bounded memory, and prompts."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.brandpacks.pack import (
    PROJECT_MEMORY_TOKEN_CAP,
    ProjectContract,
    project_contract_mode,
    render_project_contract,
    resolve_project_contract,
    resolve_project_from_roots,
)
from omniagentos.collab.store import CollabStore
from omniagentos.intake.planner import _plan_prompt, plan_goal, project_planning_context
from omniagentos.projects import ProjectStore
from omniagentos.swarm.scheduler import build_worker_brief


def _write_pack(root: Path) -> Path:
    pack = root / "brand"
    pack.mkdir(parents=True)
    (pack / "voice.md").write_text("Direct and useful.\n", encoding="utf-8")
    (pack / "offer.json").write_text('{"sku":"launch"}', encoding="utf-8")
    (pack / "banned_claims.txt").write_text("guaranteed\n", encoding="utf-8")
    return pack


def test_project_contract_mode_defaults_off_and_rejects_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", raising=False)
    assert project_contract_mode() == "off"
    monkeypatch.setenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", "unknown")
    assert project_contract_mode() == "off"


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_project_contract_mode_accepts_supported_values(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", mode.upper())
    assert project_contract_mode() == mode


def test_registry_root_resolution_prefers_most_specific(tmp_path: Path) -> None:
    nested = tmp_path / "project" / "nested"
    nested.mkdir(parents=True)
    projects = [
        {"id": "parent", "root_dirs": [str(tmp_path / "project")]},
        {"id": "child", "root_dirs": [str(nested)]},
    ]

    assert resolve_project_from_roots(projects, nested / "work") == projects[1]


def test_registry_root_resolution_rejects_blank_and_non_path_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    projects = [{"id": "unrelated", "root_dirs": ["", None, 7]}]

    assert resolve_project_from_roots(projects, tmp_path / "work") is None


def test_contract_resolves_brand_and_capped_memory_from_registry_root(
    tmp_path: Path,
) -> None:
    collab = CollabStore(str(tmp_path / "contract.db"))
    project_root = tmp_path / "project"
    project_root.mkdir()
    pack = _write_pack(project_root)
    project = ProjectStore(collab._store).create_project(
        {"id": "proj_brand", "name": "Brand", "root_dirs": [str(project_root)]}
    )
    memory = tmp_path / "var" / "memories" / "proj_brand" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("m" * (PROJECT_MEMORY_TOKEN_CAP * 6), encoding="utf-8")

    contract = resolve_project_contract(
        collab._store,
        working_dir=project_root / "deliverables",
        repo_root=tmp_path,
    )

    assert contract is not None
    assert contract.project["id"] == project["id"]
    assert contract.root == project_root.resolve()
    assert contract.brand is not None
    assert contract.brand.path == pack.resolve()
    assert len(contract.memory) == PROJECT_MEMORY_TOKEN_CAP * 4
    rendered = render_project_contract(contract, objective="Launch")
    assert "### Voice\nDirect and useful." in rendered
    assert '"sku": "launch"' in rendered
    assert "### Banned claims\n- guaranteed" in rendered


def test_contract_memory_rejects_lossy_project_id_collision(tmp_path: Path) -> None:
    collab = CollabStore(str(tmp_path / "collision.db"))
    project_root = tmp_path / "project"
    project_root.mkdir()
    projects = ProjectStore(collab._store)
    projects.create_project(
        {"id": "customer a", "name": "Spaced", "root_dirs": [str(project_root)]}
    )
    projects.create_project(
        {"id": "customer_a", "name": "Underscore", "root_dirs": [str(project_root)]}
    )
    memory = tmp_path / "var" / "memories" / "customer_a" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("underscore memory", encoding="utf-8")

    spaced = resolve_project_contract(
        collab._store,
        project_id="customer a",
        repo_root=tmp_path,
    )
    underscore = resolve_project_contract(
        collab._store,
        project_id="customer_a",
        repo_root=tmp_path,
    )

    assert spaced is not None
    assert spaced.memory == ""
    assert underscore is not None
    assert underscore.memory == "underscore memory"


def test_enforce_worker_brief_injects_content_not_memory_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", "enforce")
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "brief.db"))
    contract = ProjectContract(
        project={"id": "proj_1", "name": "Example"},
        root=tmp_path,
        brand=None,
        memory="remember the audience",
    )
    configured_store = object()

    def resolve(store: object, **_kwargs: object) -> ProjectContract:
        assert store is configured_store
        return contract

    monkeypatch.setattr("omniagentos.brandpacks.pack.resolve_project_contract", resolve)

    brief = build_worker_brief(
        {"project_id": "proj_1", "working_dir": str(tmp_path)},
        {"title": "Write launch", "description": "Produce the email", "audience": "founders"},
        {"owned_paths": ["copy.md"], "format": "email", "deliverable_spec": "one draft"},
        {},
        project_store=configured_store,
    )

    assert "## Project facts" in brief
    assert "## Objective\nProduce the email" in brief
    assert "## Audience\nfounders" in brief
    assert "## Format\nemail" in brief
    assert "## Deliverable spec\none draft" in brief
    assert "## Project memory\nremember the audience" in brief
    assert "MEMORY.md" not in brief


def test_worker_brief_root_resolution_uses_original_run_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", "enforce")
    original = tmp_path / "project"
    generated_worktree = tmp_path / "generated-worktrees" / "task"
    contract = ProjectContract(
        project={"id": "proj_1", "name": "Example"},
        root=original,
        brand=None,
        memory="",
    )

    def resolve(_store: object, **kwargs: object) -> ProjectContract:
        assert kwargs["working_dir"] == str(original)
        return contract

    monkeypatch.setattr("omniagentos.brandpacks.pack.resolve_project_contract", resolve)

    build_worker_brief(
        {"working_dir": str(original)},
        {"title": "Task", "description": "Do it"},
        {"owned_paths": ["x"], "execution_dir": str(generated_worktree)},
        {},
        project_store=object(),
    )


def test_planner_input_contains_all_project_contract_sections() -> None:
    contract = ProjectContract(
        project={"id": "proj_1", "name": "Example"},
        root=None,
        brand=None,
        memory="remember",
    )
    rendered = render_project_contract(
        contract,
        objective="Ship",
        audience="founders",
        output_format="email",
        deliverable_spec="one draft",
    )
    prompt = _plan_prompt("ship", "simple", {"contract": rendered})

    for heading in (
        "## Project facts",
        "## Objective",
        "## Audience",
        "## Format",
        "## Deliverable spec",
    ):
        assert heading in prompt


def test_project_planning_context_off_does_not_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", raising=False)
    monkeypatch.setattr(
        "omniagentos.brandpacks.pack.resolve_project_contract",
        lambda *_args, **_kwargs: pytest.fail("off mode consulted the registry"),
    )
    assert project_planning_context(object(), objective="ship") is None


def test_project_planning_context_enforce_renders_required_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", "enforce")
    contract = ProjectContract(
        project={"id": "proj_1", "name": "Example"},
        root=None,
        brand=None,
        memory="m" * (PROJECT_MEMORY_TOKEN_CAP * 6),
    )
    monkeypatch.setattr(
        "omniagentos.brandpacks.pack.resolve_project_contract",
        lambda *_args, **_kwargs: contract,
    )

    context = project_planning_context(
        object(),
        objective="Ship",
        audience="founders",
        output_format="email",
        deliverable_spec="one draft",
    )

    assert context is not None
    rendered = context["contract"]
    assert "## Objective\nShip" in rendered
    assert "## Audience\nfounders" in rendered
    assert "## Format\nemail" in rendered
    assert "## Deliverable spec\none draft" in rendered
    assert "m" * (PROJECT_MEMORY_TOKEN_CAP * 4 + 1) not in rendered


def test_plan_goal_automatically_injects_project_context_in_enforce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", "enforce")
    monkeypatch.setattr(
        "omniagentos.intake.planner.project_planning_context",
        lambda *_args, **_kwargs: {
            "contract": "## Project facts\n- name: Example\n\n## Objective\nShip"
        },
    )
    prompts: list[str] = []

    def llm(prompt: str, _schema: dict[str, object], _effort: str) -> dict[str, object]:
        prompts.append(prompt)
        return {"project_name": "Example", "tasks": [{"title": "Ship"}]}

    plan_goal("Ship", llm=llm, project_store=object())

    assert prompts
    assert "## Project facts\n- name: Example" in prompts[0]


def test_worker_brief_without_project_avoids_invalid_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_PROJECT_CONTRACT_MODE", raising=False)

    brief = build_worker_brief(
        {},
        {
            "title": "Write launch",
            "description": "Produce the email",
            "project_id": "invalid_board_column",
        },
        {"owned_paths": ["copy.md"]},
        {},
    )

    assert "<project_id>" not in brief
    assert "invalid_board_column" not in brief
    assert "No project is registered" in brief
