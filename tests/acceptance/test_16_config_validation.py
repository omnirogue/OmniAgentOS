"""AT-16 — Configuration validation / preflight.

Before a simulation starts, eight prerequisites must hold. Each one is checked
here against whatever real mechanism the repo has today, and where the repo has
*no* mechanism the test is a strict xfail naming the missing seam rather than a
green test proving nothing.

    1. models available          -> configs/swarm.yaml router.lineage_providers
    2. prompts exist             -> promptshape.rolepack.role_pack over JOB_ROLES
    3. tools exist               -> adapters.registry.resolve_adapter
    4. API keys valid            -> accounts.service (PRESENCE/SHAPE ONLY — this
                                    file never reads, prints, or asserts on a
                                    real secret value)
    5. worktrees can be created  -> worktrees.git.SubprocessWorktrees on a real
                                    temp git repo
    6. MCP servers connected     -> nothing exists (xfail)
    7. limits configured         -> routing.limit_state + routing.fleet_preflight
    8. benchmark inputs exist    -> harnesses.bench.runner.load_tasks and
                                    scripts.benchmarks.fixtures.load_fixtures

The suite-level requirement — *a missing prerequisite must fail IMMEDIATELY with
a clear diagnostic naming what is missing, not halfway through a run* — is
asserted on the diagnostic text of each mechanism, and its absence at the
aggregate level is recorded as the headline gap.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from omniagentos.adapters.registry import resolve_adapter
from omniagentos.promptshape.rolepack import JOB_ROLES, clear_role_pack_cache, role_pack
from omniagentos.routing import fleet_preflight
from omniagentos.routing.limit_state import (
    load_swarm_config,
    max_inflight_per_account,
    reservation_ttl_seconds,
    swarm_config_path,
)
from omniagentos.swarm.router import lineage_provider_map
from omniagentos.worktrees.git import SubprocessWorktrees

REPO_ROOT = Path(__file__).resolve().parents[2]

# Harnesses the shipped org roster and formations actually route to. A harness
# here that cannot be resolved means a seeded agent can never run.
REQUIRED_HARNESSES = ("cli-claude", "cli-codex", "cli-grok", "cli-gemini")


# ---------------------------------------------------------------------------
# 1. Models available
# ---------------------------------------------------------------------------


class TestModelsAvailable:
    def test_every_routed_lineage_maps_to_a_resolvable_provider_adapter(self) -> None:
        mapping = lineage_provider_map()
        assert mapping, f"{swarm_config_path()} declares no router.lineage_providers"

        unresolvable: list[str] = []
        for lineage, provider in mapping.items():
            try:
                resolve_adapter(f"cli-{provider}")
            except KeyError:
                unresolvable.append(f"{lineage}->{provider}")
        assert not unresolvable, (
            f"router.lineage_providers routes to providers with no adapter: {unresolvable}"
        )

    def test_every_formation_implementer_names_a_routable_provider(self) -> None:
        # SwarmRouter._apply_formation_implementers matches these names against
        # the candidate's PROVIDER. An implementer that names no provider
        # filters nothing, so the formation silently stops binding to execution.
        from omniagentos.formation.selector import _all, clear_formation_cache

        clear_formation_cache()
        providers = set(lineage_provider_map().values())
        unroutable = [
            f"{formation.id}:{implementer}"
            for formation in _all().values()
            for implementer in formation.implementers
            if implementer.strip().lower() not in providers
        ]
        assert not unroutable, (
            "configs/formations.yaml names implementers matching no routable provider "
            f"{sorted(providers)}: {sorted(set(unroutable))}"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: configs/formations.yaml sets planner: sol and reviewer: fable, and "
            "neither name resolves through router.lineage_providers OR "
            "default_model_lineage_index (built from configs/modelintel.yaml). They are "
            "fusion-agent aliases with no provider binding, so a typo in a formation's "
            "planner/reviewer is undetectable before a run. "
            "See docs/acceptance/gaps-AT1.md."
        ),
    )
    def test_every_formation_reviewer_and_planner_resolves_to_a_lineage(self) -> None:
        from omniagentos.formation.selector import _all, clear_formation_cache
        from omniagentos.swarm.router import default_model_lineage_index

        clear_formation_cache()
        lineages = set(lineage_provider_map())
        index = default_model_lineage_index()
        unknown = [
            f"{formation.id}:{model}"
            for formation in _all().values()
            for model in (formation.reviewer, formation.planner)
            if model and model.lower() not in lineages and index.get(model.lower()) not in lineages
        ]
        assert not unknown, f"unresolvable formation reviewer/planner: {sorted(set(unknown))}"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: there is no model-availability check anywhere. No configs/models.yaml, "
            "no 'is this model id known' function. A typo'd model in "
            "configs/swarm.yaml router.lane_floors is absorbed at ROUTE time by "
            "SwarmRouter._apply_lane_floors with a LOG.warning and dropped, so a bad "
            "model id is discovered mid-run instead of before it. "
            "See docs/acceptance/gaps-AT1.md."
        ),
    )
    def test_an_unknown_model_id_is_rejected_before_a_run_starts(self) -> None:
        from omniagentos.swarm import router as swarm_router

        assert hasattr(swarm_router, "assert_models_available"), (
            "no preflight entry point validates configured model ids"
        )


# ---------------------------------------------------------------------------
# 2. Prompts exist
# ---------------------------------------------------------------------------


class TestPromptsExist:
    @pytest.fixture(autouse=True)
    def _clear_cache(self) -> Any:
        clear_role_pack_cache()
        yield
        clear_role_pack_cache()

    def test_every_declared_job_role_has_a_loadable_prompt_pack(self) -> None:
        missing = [role for role in JOB_ROLES if role_pack(role) is None]
        assert not missing, (
            f"vault/prompts/roles/ is missing a loadable prompt for: {missing} "
            "(role_pack is fail-soft and returns None, so this would otherwise be silent)"
        )

    def test_the_universal_base_prompt_exists_and_is_not_empty(self) -> None:
        base = REPO_ROOT / "vault" / "prompts" / "universal-base.md"
        assert base.is_file(), f"missing required prompt: {base}"
        assert base.read_text(encoding="utf-8").strip(), f"{base} is empty"

    def test_a_deleted_role_prompt_is_detected_not_silently_skipped(self, tmp_path: Path) -> None:
        # Reproduce the failure the real checkout must never be in: the pack
        # for a declared role is simply absent.
        root = tmp_path / "checkout"
        (root / "vault" / "prompts" / "roles").mkdir(parents=True)
        (root / "vault" / "prompts" / "universal-base.md").write_text("base", encoding="utf-8")
        for role in JOB_ROLES:
            if role == "reviewer":
                continue
            (root / "vault" / "prompts" / "roles" / f"{role}.md").write_text("r", encoding="utf-8")

        missing = [role for role in JOB_ROLES if role_pack(role, root=root) is None]
        assert missing == ["reviewer"]

    def test_every_role_prompt_has_substance_not_a_stub(self) -> None:
        """A one-line or empty role file must not satisfy prompt existence.

        Loadability alone is too weak: assert shape, minimum length, rules,
        output prose, and no placeholder markers for every JOB_ROLES entry.
        """
        import re

        roles_dir = REPO_ROOT / "vault" / "prompts" / "roles"
        placeholder_markers = ("TODO", "TBD", "FIXME", "coming soon", "lorem")
        failures: list[str] = []

        for role in JOB_ROLES:
            path = roles_dir / f"{role}.md"
            role_failures: list[str] = []

            if not path.is_file():
                failures.append(f"{role}: missing file {path}")
                continue

            text = path.read_text(encoding="utf-8")
            stripped = text.strip()

            if len(stripped.encode("utf-8")) < 600:
                role_failures.append(
                    f"stripped text is {len(stripped.encode('utf-8'))} bytes, need >= 600"
                )

            first_line = text.splitlines()[0] if text.splitlines() else ""
            if not re.fullmatch(r"# Role: \S.*", first_line):
                role_failures.append(f"first line must be '# Role: <name>', got {first_line!r}")

            if "## Rules" not in text:
                role_failures.append("missing '## Rules' heading")
            if "## Output" not in text:
                role_failures.append("missing '## Output' heading")

            rules_match = re.search(r"## Rules\s*\n(.*?)(?=\n## |\Z)", text, flags=re.DOTALL)
            rules_body = rules_match.group(1) if rules_match else ""
            numbered = [line for line in rules_body.splitlines() if re.match(r"^\d+\.\s", line)]
            if len(numbered) < 5:
                role_failures.append(
                    f"only {len(numbered)} numbered rule lines under Rules, need >= 5"
                )

            normalized_rules: list[str] = []
            for line in numbered:
                collapsed = re.sub(r"\s+", " ", line).strip().casefold()
                without_number = re.sub(r"^\d+\.\s*", "", collapsed)
                normalized_rules.append(without_number)
            duplicate_count = len(normalized_rules) - len(set(normalized_rules))
            if duplicate_count:
                role_failures.append(
                    f"{duplicate_count} duplicate rule lines under Rules (rules must be distinct)"
                )

            output_match = re.search(r"## Output\s*\n(.*?)(?=\n## |\Z)", text, flags=re.DOTALL)
            output_body = (output_match.group(1) if output_match else "").strip()
            output_words = [w for w in re.split(r"\s+", output_body) if w]
            if len(output_words) < 15:
                role_failures.append(f"Output section has {len(output_words)} words, need >= 15")

            lower = text.lower()
            for marker in placeholder_markers:
                if marker.lower() in lower:
                    role_failures.append(f"contains placeholder marker {marker!r}")

            if role_failures:
                failures.append(f"{role}: " + "; ".join(role_failures))

        assert not failures, "role prompt substance failures:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# 3. Tools (harness adapters) exist
# ---------------------------------------------------------------------------


class TestToolsExist:
    @pytest.mark.parametrize("harness", REQUIRED_HARNESSES)
    def test_every_required_harness_adapter_resolves(self, harness: str) -> None:
        adapter = resolve_adapter(harness)
        assert adapter is not None
        assert hasattr(adapter, "health"), f"{harness} adapter exposes no health probe"

    def test_every_harness_named_by_the_org_roster_resolves(self) -> None:
        from omniagentos.orgdims.company_org import _AGENT_PLAN

        unresolvable: list[str] = []
        for plan in _AGENT_PLAN:
            harness = str(plan.get("harness") or "")
            try:
                resolve_adapter(harness)
            except KeyError:
                unresolvable.append(f"{plan['name']}:{harness}")
        assert not unresolvable, (
            f"seeded agents reference harnesses with no adapter: {unresolvable}"
        )

    def test_an_unknown_harness_fails_immediately_and_names_what_is_known(self) -> None:
        with pytest.raises(KeyError) as excinfo:
            resolve_adapter("cli-nonexistent")
        message = str(excinfo.value)
        assert "cli-nonexistent" in message
        assert "known adapters" in message
        assert "cli-claude" in message

    def test_declared_tool_names_are_validated_against_the_registry(self) -> None:
        from omniagentos.policy import PolicyError, load_policy, validate_tools

        cfg = load_policy()
        # A known primitive passes; an invented one fails closed and is named.
        validate_tools(["shell"], cfg)
        with pytest.raises(PolicyError) as excinfo:
            validate_tools(["ThisToolDoesNotExist"], cfg)
        message = str(excinfo.value)
        assert "ThisToolDoesNotExist" in message
        assert "known tools" in message


# ---------------------------------------------------------------------------
# 4. API keys — presence and shape ONLY, never a value
# ---------------------------------------------------------------------------


class TestApiKeyPresenceAndShape:
    """No test in this class reads, logs, or asserts on real secret material."""

    def test_an_api_key_account_without_a_secret_is_refused_immediately(
        self, migrated_db_path: str
    ) -> None:
        from omniagentos.accounts.service import add_account

        with pytest.raises(ValueError, match="a token or API key is required"):
            add_account(
                label="acceptance", auth_type="api_key", secret="   ", db_path=migrated_db_path
            )

    def test_an_unknown_auth_type_is_refused_immediately(self, migrated_db_path: str) -> None:
        from omniagentos.accounts.service import add_account

        with pytest.raises(ValueError, match="invalid auth_type"):
            add_account(label="acceptance", auth_type="psychic", db_path=migrated_db_path)

    def test_a_config_dir_account_pointing_nowhere_is_refused_immediately(
        self, migrated_db_path: str, tmp_path: Path
    ) -> None:
        from omniagentos.accounts.service import add_account

        missing = tmp_path / "no-such-config-dir"
        with pytest.raises(ValueError, match="config_dir does not exist"):
            add_account(
                label="acceptance",
                auth_type="config_dir",
                config_dir=str(missing),
                db_path=migrated_db_path,
            )

    def test_the_account_pool_config_rejects_a_duplicate_account_id(self, tmp_path: Path) -> None:
        import yaml

        from omniagentos.routing.config import load_accounts_config

        path = tmp_path / "accounts.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "providers": {
                        "grok": {
                            "accounts": [
                                {"id": "dup", "config_dir": str(tmp_path)},
                                {"id": "dup", "config_dir": str(tmp_path)},
                            ]
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="declared more than once"):
            load_accounts_config(path)

    def test_the_shipped_accounts_config_parses_and_declares_no_secret_material(self) -> None:
        from omniagentos.routing.config import load_accounts_config

        config = load_accounts_config()
        raw = (REPO_ROOT / "configs" / "accounts.yaml").read_text(encoding="utf-8")
        assert config is not None
        # Committed config must reference credentials by LOCATION only.
        for forbidden in ("api_key:", "secret:", "sk-", "token:"):
            assert forbidden not in raw, (
                f"configs/accounts.yaml appears to contain secret material ({forbidden!r})"
            )

    def test_an_env_shaped_key_is_reported_as_present_without_exposing_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The adapter's configuration-only health probe is the presence check.
        # It must report unhealthy when the key is absent and healthy when it is
        # present, and neither branch may echo the value.
        from omniagentos.adapters.openrouter import OpenRouterAdapter

        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        absent = OpenRouterAdapter().health()
        assert absent.healthy is False
        assert "no api key in the environment" in absent.detail.lower()

        sentinel = "sk-or-v1-" + "0" * 32
        monkeypatch.setenv("OPENROUTER_API_KEY", sentinel)
        present = OpenRouterAdapter().health()
        assert present.healthy is True
        assert sentinel not in present.detail, "health detail leaked the API key"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: no API key SHAPE validation exists anywhere in the repo — non-empty is "
            "the only check (accounts.service.add_account). A truncated or "
            "wrong-provider key is accepted and only fails at the first live call, "
            "mid-run. See docs/acceptance/gaps-AT1.md."
        ),
    )
    def test_a_malformed_api_key_shape_is_rejected_before_a_run(
        self, migrated_db_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omniagentos.accounts import service

        secrets_dir = tmp_path / "secrets"
        monkeypatch.setattr(service, "_secrets_dir", lambda: secrets_dir)
        with pytest.raises(ValueError):
            service.add_account(
                label="acceptance",
                auth_type="api_key",
                secret="obviously-not-a-key",
                db_path=migrated_db_path,
            )


# ---------------------------------------------------------------------------
# 5. Worktrees can be created
# ---------------------------------------------------------------------------


class TestWorktreesCanBeCreated:
    def _worktrees(self, tmp_path: Path) -> SubprocessWorktrees:
        return SubprocessWorktrees(namespace="acceptance", var_root=tmp_path / "var")

    def test_a_real_git_workspace_supports_worktrees_and_one_can_be_created(
        self, git_workspace: Path, tmp_path: Path
    ) -> None:
        worktrees = self._worktrees(tmp_path)
        assert worktrees.supported(str(git_workspace)) is True
        assert worktrees.git_common_dir(str(git_workspace)) is not None

        info = worktrees.create(
            str(git_workspace), owner_id="run1", unit_key="task1", base_ref="HEAD"
        )
        assert Path(info.path).is_dir()
        assert (Path(info.path) / "README.md").is_file()
        assert info.branch == "acceptance/run1/task1"
        assert info.base_sha
        assert worktrees.worktree_git_dir(info.path) is not None

    def test_a_non_git_directory_is_reported_unsupported_not_crashed_into(
        self, tmp_path: Path
    ) -> None:
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        worktrees = self._worktrees(tmp_path)
        assert worktrees.supported(str(plain)) is False
        assert worktrees.git_common_dir(str(plain)) is None

    def test_creating_a_worktree_in_a_non_git_directory_fails_loudly(self, tmp_path: Path) -> None:
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        worktrees = self._worktrees(tmp_path)
        with pytest.raises((subprocess.CalledProcessError, OSError)):
            worktrees.create(str(plain), owner_id="run1", unit_key="task1", base_ref="HEAD")

    @pytest.mark.parametrize("unsafe", ["../escape", "a/b", ""])
    def test_an_unsafe_namespace_or_key_is_refused_before_touching_the_filesystem(
        self, tmp_path: Path, unsafe: str
    ) -> None:
        with pytest.raises(ValueError):
            SubprocessWorktrees(namespace=unsafe, var_root=tmp_path / "var")

    def test_worktree_mode_is_enabled_in_the_shipped_config(self) -> None:
        from omniagentos.swarm.worktrees import config_dep_link_dirs, worktrees_config

        config = worktrees_config()
        assert config, "configs/swarm.yaml has no worktrees: block"
        assert config.get("enabled") is True
        assert config_dep_link_dirs()


# ---------------------------------------------------------------------------
# 6. MCP servers connected
# ---------------------------------------------------------------------------


class TestMcpServers:
    def test_the_declared_mcp_server_manifest_is_present_and_parseable(self) -> None:
        """The DEFAULT roster may legitimately be empty; the profiles may not.

        This used to assert ``servers`` was non-empty. That assertion encoded an
        assumption that stopped being true on 2026-08-13, when the default roster
        was emptied: the telemetry that justified keeping fetch and memory was
        found to be misattributed (``@modelcontextprotocol/server-memory`` has no
        tool called ``memory_search`` -- that is this repo's own bridge tool at
        ``toolplane/mcp_server.py:106``), and a roster that loads in EVERY session
        should hold nothing that cannot justify that cost on every launch.

        Deleting the assertion outright would have left the test vacuous, so the
        non-empty invariant MOVES to where the servers actually live now:
        ``configs/toolbroker/mcp-profiles/``. The well-formedness check applies to
        both. Net coverage is higher than before -- five profile files are checked
        where one roster was.
        """
        import json

        manifest = REPO_ROOT / "tools" / "mcp-servers.json"
        assert manifest.is_file(), f"missing MCP server manifest: {manifest}"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        servers = data.get("mcpServers") or data.get("servers") or {}
        for name, spec in servers.items():
            assert spec.get("command") or spec.get("url"), (
                f"MCP server {name!r} declares neither a command nor a url"
            )

        profile_dir = REPO_ROOT / "configs" / "toolbroker" / "mcp-profiles"
        assert profile_dir.is_dir(), (
            f"missing profile directory: {profile_dir}. The default roster is empty "
            "by design, so if the profiles are gone too, no server is reachable at all."
        )
        profiles = sorted(profile_dir.glob("*.json"))
        assert profiles, f"{profile_dir} declares no MCP profiles"

        total = 0
        for profile in profiles:
            pdata = json.loads(profile.read_text(encoding="utf-8"))
            pservers = pdata.get("mcpServers") or {}
            assert pservers, f"{profile.name} declares no MCP servers"
            for name, spec in pservers.items():
                assert spec.get("command") or spec.get("url"), (
                    f"{profile.name}: MCP server {name!r} declares neither a command nor a url"
                )
                total += 1
        assert total, "no MCP server is declared in any profile"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "GAP: no Python code checks MCP server registration or connectivity. The "
            "only doctor is the bash tools/install-tools.sh, invoked by nothing in "
            "omniagentos/, and tools/README.md points at a tools/mcp_tool.py that does "
            "not exist. A required MCP server being down is discovered by the first "
            "agent that needs it, mid-run. See docs/acceptance/gaps-AT1.md."
        ),
    )
    def test_required_mcp_servers_report_connected_before_a_run(self) -> None:
        import importlib

        module = importlib.import_module("omniagentos.toolplane.mcp_preflight")
        assert hasattr(module, "check_servers")


# ---------------------------------------------------------------------------
# 7. Limits configured
# ---------------------------------------------------------------------------


class TestLimitsConfigured:
    REQUIRED_KEYS = ("max_concurrent_swarms", "max_sessions_global", "reserved_small_task_slots")

    def test_the_authoritative_fleet_limits_are_present_and_coherent(self) -> None:
        config = load_swarm_config()
        missing = [key for key in self.REQUIRED_KEYS if key not in config]
        assert not missing, f"{swarm_config_path()} is missing fleet limits: {missing}"

        total = int(config["max_sessions_global"])
        reserved = int(config["reserved_small_task_slots"])
        assert total > 0 and reserved >= 0
        assert reserved < total, (
            f"reserved_small_task_slots={reserved} >= max_sessions_global={total}: "
            "no swarm session could ever be admitted"
        )
        assert int(config["max_concurrent_swarms"]) > 0

    def test_every_tier_has_a_positive_timeout(self) -> None:
        from omniagentos.swarm.router import tier_timeout_minutes

        timeouts = tier_timeout_minutes()
        for tier in ("simple", "standard", "complex"):
            assert tier in timeouts, f"no attempt timeout configured for tier {tier!r}"
            assert timeouts[tier] > 0

    def test_per_account_and_reservation_limits_are_positive(self) -> None:
        assert max_inflight_per_account("claude") > 0
        assert reservation_ttl_seconds() > 0

    def test_the_two_concurrency_config_files_do_not_contradict_each_other(self) -> None:
        problems = fleet_preflight.config_disagreements()
        assert problems == [], "concurrency config drift: " + "; ".join(problems)

    def test_preflight_names_the_binding_constraint_and_its_source(
        self, migrated_db_path: str
    ) -> None:
        report = fleet_preflight.preflight(db_path=migrated_db_path)
        assert report.ceilings
        binding = report.binding
        assert binding is not None
        assert binding.source, "the binding ceiling does not say which config set it"
        names = {ceiling.name for ceiling in report.ceilings}
        assert {"fleet.sessions_global", "fleet.concurrent_swarms", "os.file_descriptors"} <= names

    def test_preflight_reports_an_unreadable_database_instead_of_pretending(
        self, tmp_path: Path
    ) -> None:
        broken = tmp_path / "corrupt.db"
        broken.write_bytes(b"this is not a sqlite database")
        report = fleet_preflight.preflight(db_path=str(broken))
        assert any("database unreadable" in warning for warning in report.warnings), (
            f"a corrupt control-plane DB produced no warning: {report.warnings}"
        )


# ---------------------------------------------------------------------------
# 8. Benchmark inputs exist
# ---------------------------------------------------------------------------


class TestBenchmarkInputsExist:
    def test_the_devtasks_directory_holds_well_formed_task_files(self) -> None:
        # The individual task files themselves are sound; the directory as a
        # whole is not loadable (see the strict xfail below).
        from omniagentos.harnesses.bench.runner import _load_task_file

        task_files = sorted((REPO_ROOT / "devtasks").glob("task_*.yaml"))
        assert task_files, "make bench would run against an empty task set"
        for path in task_files:
            data = _load_task_file(path)
            for key in ("id", "prompt", "discipline"):
                assert data.get(key), f"{path.name} is missing required field {key!r}"

    # ``devtasks/`` also contains coordination metadata and plans. ``load_tasks``
    # deliberately selects only filename-scoped ``task_*`` files, while malformed
    # files in that namespace still fail closed below.
    def test_the_devtasks_benchmark_corpus_loads_as_make_bench_would_load_it(self) -> None:
        from omniagentos.harnesses.bench.runner import load_tasks

        tasks = load_tasks(REPO_ROOT / "devtasks")
        assert tasks

    def test_a_missing_benchmark_directory_fails_immediately_and_names_it(
        self, tmp_path: Path
    ) -> None:
        from omniagentos.harnesses.bench.runner import load_tasks

        missing = tmp_path / "no-such-tasks"
        with pytest.raises(FileNotFoundError) as excinfo:
            load_tasks(missing)
        assert str(missing) in str(excinfo.value)

    def test_a_malformed_benchmark_task_names_the_file_and_the_field(self, tmp_path: Path) -> None:
        from omniagentos.harnesses.bench.runner import load_tasks

        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        (tasks_dir / "task_broken.yaml").write_text("id: t1\ndiscipline: code\n", encoding="utf-8")
        with pytest.raises(ValueError) as excinfo:
            load_tasks(tasks_dir)
        message = str(excinfo.value)
        assert "task_broken.yaml" in message
        assert "prompt" in message

    def test_the_frozen_fixture_corpus_loads_and_is_complete(self) -> None:
        from scripts.benchmarks.fixtures import FIXTURES_DIR, load_fixtures

        fixtures = load_fixtures(FIXTURES_DIR)
        assert len(fixtures) >= 6, f"fixture corpus is too small to baseline: {len(fixtures)}"
        for fixture in fixtures:
            assert (Path(fixture.root) / "seed").is_dir()
            assert (Path(fixture.root) / "accept").is_dir()

    def test_a_missing_fixture_directory_fails_immediately_and_names_it(
        self, tmp_path: Path
    ) -> None:
        from scripts.benchmarks.fixtures import load_fixtures

        missing = tmp_path / "no-such-fixtures"
        with pytest.raises(FileNotFoundError) as excinfo:
            load_fixtures(missing)
        assert str(missing) in str(excinfo.value)


# ---------------------------------------------------------------------------
# The aggregate gate
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP (headline): there is no aggregate preflight. routing.fleet_preflight is the "
        "only readiness check in the repo and its own docstring says 'Wired nowhere on "
        "purpose'; it covers limits/FDs only and never raises. Nothing verifies models + "
        "prompts + tools + keys + worktrees + MCP + limits + benchmark inputs as one "
        "gate before a run, so a missing prerequisite is discovered by the worker that "
        "needs it. See docs/acceptance/gaps-AT1.md."
    ),
)
def test_a_single_preflight_gate_blocks_a_run_with_a_missing_prerequisite() -> None:
    import importlib

    module = importlib.import_module("omniagentos.swarm.preflight")
    assert hasattr(module, "assert_ready")
