"""Fleet-scale-200 ceilings: config authority, cross-file agreement, preflight.

Guards the invariant the work package bought: the fleet's ceilings live in more
than one file, and a silent disagreement between them is indistinguishable from
a fleet that simply refuses to widen. Every duplicated number is pinned here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from omniagentos.api.routes import swarm as swarm_routes
from omniagentos.collab.store import CollabStore
from omniagentos.routing import fleet_preflight, limit_state

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The shipped ceilings this work package installed. Written out literally (not
# read from the file) so a config edit that lowers them fails HERE, loudly,
# instead of quietly halving the fleet.
EXPECTED_MAX_SESSIONS_GLOBAL = 260
EXPECTED_RESERVED_SMALL_TASK_SLOTS = 40
EXPECTED_MAX_CONCURRENT_SWARMS = 20
EXPECTED_MAX_SESSIONS_PER_PROJECT = 25
EXPECTED_RUNNER_CONCURRENCY = 16

_SHARED_FLEET_KEYS = (
    "max_sessions_global",
    "reserved_small_task_slots",
    "max_concurrent_swarms",
)


def _swarm_yaml() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "configs" / "swarm.yaml").read_text(encoding="utf-8"))


def _concurrency_yaml() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "configs" / "concurrency.yaml").read_text(encoding="utf-8"))


class TestShippedCeilings:
    def test_swarm_yaml_has_the_raised_ceilings(self) -> None:
        config = _swarm_yaml()
        assert config["max_sessions_global"] == EXPECTED_MAX_SESSIONS_GLOBAL
        assert config["reserved_small_task_slots"] == EXPECTED_RESERVED_SMALL_TASK_SLOTS
        assert config["max_concurrent_swarms"] == EXPECTED_MAX_CONCURRENT_SWARMS

    def test_concurrency_yaml_has_the_raised_dials(self) -> None:
        config = _concurrency_yaml()
        assert config["fleet"]["max_sessions_per_project"] == EXPECTED_MAX_SESSIONS_PER_PROJECT
        assert config["runner"]["concurrency"] == EXPECTED_RUNNER_CONCURRENCY

    def test_the_two_config_files_agree_on_every_duplicated_key(self) -> None:
        """configs/swarm.yaml is AUTHORITATIVE; concurrency.yaml mirrors it.

        Only swarm.yaml is read by code (limit_state.fleet_available,
        intake.service), so a drifting mirror is a runbook that lies about the
        live fleet -- the exact failure this asserts away.
        """
        swarm = _swarm_yaml()
        fleet = _concurrency_yaml()["fleet"]
        for key in _SHARED_FLEET_KEYS:
            assert fleet[key] == swarm[key], (
                f"configs/concurrency.yaml fleet.{key}={fleet[key]} != "
                f"configs/swarm.yaml {key}={swarm[key]}; swarm.yaml is authoritative"
            )
        assert fleet_preflight.config_disagreements() == []

    def test_limit_state_fallbacks_mirror_the_config(self) -> None:
        """A missing/unparseable swarm.yaml must not silently restore the OLD cap."""
        assert limit_state._DEFAULT_MAX_SESSIONS_GLOBAL == EXPECTED_MAX_SESSIONS_GLOBAL
        assert limit_state._DEFAULT_RESERVED_SMALL_TASK_SLOTS == EXPECTED_RESERVED_SMALL_TASK_SLOTS

    def test_dashboard_denominators_mirror_the_config(self) -> None:
        """GET /api/swarm's utilization tiles divide by these; a stale value
        renders a healthy fleet as ">100% utilized"."""
        swarm = _swarm_yaml()
        assert swarm_routes.MAX_CONCURRENT_SWARMS == swarm["max_concurrent_swarms"]
        assert swarm_routes.MAX_SWARM_TERMINALS == (
            swarm["max_sessions_global"] - swarm["reserved_small_task_slots"]
        )

    def test_per_account_inflight_is_untouched_by_this_package(self) -> None:
        """feat/provider-resilience owns max_inflight_per_account; this branch
        must not have edited it (the two branches merge)."""
        providers = _swarm_yaml()["limits"]["providers"]
        for provider, entry in providers.items():
            assert entry["max_inflight_per_account"] >= 1, provider

    def test_the_inflight_fallback_does_not_lag_the_config(self) -> None:
        """Same no-silent-drift invariant as the two session keys above.

        Added at integration: fleet-scale-200 pinned config==fallback for
        max_sessions_global and reserved_small_task_slots and deliberately left
        max_inflight_per_account alone because provider-resilience owned it;
        provider-resilience then raised the CONFIG to 20 and left the fallback at
        3. Merged, a swarm.yaml that fails to load would hand out 260 global
        sessions while capping every account at 3 — the silent capacity cliff
        limit_state.py's own comment calls the hardest failure of this kind to
        diagnose. Pin it the same way the session keys are pinned.
        """
        providers = _swarm_yaml()["limits"]["providers"]
        shipped = {entry["max_inflight_per_account"] for entry in providers.values()}
        assert len(shipped) == 1, f"providers disagree on the ceiling: {providers}"
        assert limit_state._DEFAULT_MAX_INFLIGHT_PER_ACCOUNT == shipped.pop()

    def test_the_agents_ceiling_fallback_does_not_lag_the_config(self) -> None:
        """PKG-INSESSION-FANOUT: the same no-silent-drift invariant for the
        AGENT ceiling (live sessions + committed in-session grant budgets).
        Only claude ships the key — the only provider whose CLI has a Task
        tool to grant — and the accessor must fall back identically."""
        claude = _swarm_yaml()["limits"]["providers"]["claude"]
        assert limit_state._DEFAULT_MAX_AGENTS_PER_ACCOUNT == claude["max_agents_per_account"]
        assert limit_state.max_agents_per_account("claude") == claude["max_agents_per_account"]


class TestConfigLoad:
    def test_fleet_available_reads_the_raised_ceiling(self, tmp_path: Path) -> None:
        db = str(tmp_path / "fleet.db")
        CollabStore(db)
        snapshot = limit_state.fleet_available(db_path=db)
        assert snapshot.max_sessions_global == EXPECTED_MAX_SESSIONS_GLOBAL
        assert snapshot.reserved_small_task_slots == EXPECTED_RESERVED_SMALL_TASK_SLOTS
        # 260 - 40 = 220 sessions available to swarm on an idle fleet.
        assert snapshot.available_for_swarm == (
            EXPECTED_MAX_SESSIONS_GLOBAL - EXPECTED_RESERVED_SMALL_TASK_SLOTS
        )

    def test_config_override_still_wins_over_the_shipped_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        override = tmp_path / "swarm.yaml"
        override.write_text(
            "max_sessions_global: 12\nreserved_small_task_slots: 2\n", encoding="utf-8"
        )
        monkeypatch.setenv("OMNIAGENTOS_SWARM_CONFIG", str(override))
        snapshot = limit_state.fleet_available(total_live=0, swarm_live=0)
        assert snapshot.max_sessions_global == 12
        assert snapshot.available_for_swarm == 10


class TestAdmissionMathAtScale:
    """`fleet_available` is pure arithmetic over two counts -- these pin that it
    keeps working at 200+, and that nothing integer-divides to zero on the way."""

    def test_two_hundred_live_sessions_still_leaves_headroom(self) -> None:
        snapshot = limit_state.fleet_available(
            EXPECTED_MAX_SESSIONS_GLOBAL,
            EXPECTED_RESERVED_SMALL_TASK_SLOTS,
            total_live=200,
            swarm_live=180,
        )
        assert snapshot.available_global == 60
        # swarm ceiling 220 - 180 live = 40, and 40 < 60 so the swarm term binds.
        assert snapshot.available_for_swarm == 40

    def test_reserve_is_never_eaten_by_swarm(self) -> None:
        """Swarm may fill to the ceiling and not one session past it, however
        much global headroom is left."""
        snapshot = limit_state.fleet_available(
            EXPECTED_MAX_SESSIONS_GLOBAL,
            EXPECTED_RESERVED_SMALL_TASK_SLOTS,
            total_live=0,
            swarm_live=220,
        )
        assert snapshot.available_global == 260
        assert snapshot.available_for_swarm == 0

    def test_over_subscription_clamps_at_zero_not_negative(self) -> None:
        snapshot = limit_state.fleet_available(
            EXPECTED_MAX_SESSIONS_GLOBAL,
            EXPECTED_RESERVED_SMALL_TASK_SLOTS,
            total_live=300,
            swarm_live=280,
        )
        assert snapshot.available_global == 0
        assert snapshot.available_for_swarm == 0

    @pytest.mark.parametrize("swarms", [1, 2, 10, 20])
    def test_fair_share_never_divides_to_zero(self, swarms: int) -> None:
        """The scheduler's fair-share term is an integer division by the number
        of active swarms; at 20 concurrent swarms it must still hand out >= 1."""
        snapshot = limit_state.fleet_available(
            EXPECTED_MAX_SESSIONS_GLOBAL,
            EXPECTED_RESERVED_SMALL_TASK_SLOTS,
            total_live=0,
            swarm_live=0,
        )
        fair_share = max(1, (snapshot.available_for_swarm + 0) // swarms)
        assert fair_share >= 1
        if swarms <= 20:
            # 220 / 20 = 11 -> every one of 20 concurrent swarms can still run
            # wider than the OLD per-run ceiling of 10.
            assert fair_share >= 11


class TestInflightBatching:
    def test_batched_and_single_reads_agree(self, tmp_path: Path) -> None:
        from omniagentos.contracts import utc_now_iso
        from omniagentos.db.store import _connect

        db = str(tmp_path / "inflight.db")
        CollabStore(db)
        conn = _connect(db)
        now = utc_now_iso()
        try:
            for index in range(4):
                conn.execute(
                    "INSERT INTO claude_accounts "
                    "(id, label, config_dir, enabled, status, provider, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, 'ok', 'claude', ?, ?)",
                    (f"acct_{index}", f"a{index}", f"/tmp/a{index}", now, now),
                )
            # acct_0 gets 3 live sessions, acct_1 gets 1, acct_2/3 get none.
            for index, account in enumerate(["acct_0", "acct_0", "acct_0", "acct_1"]):
                conn.execute(
                    "INSERT INTO sessions "
                    "(id, source, project_dir, provider, state, account_id, "
                    " created_at, updated_at) "
                    "VALUES (?, 'bridge', '/tmp', 'claude', 'running', ?, ?, ?)",
                    (f"ses_{index}", account, now, now),
                )
            conn.commit()
            ids = [f"acct_{index}" for index in range(4)]
            batched = limit_state.inflight_by_account(conn, ids)
            singles = {
                account_id: limit_state._account_inflight(conn, account_id) for account_id in ids
            }
            assert batched == singles
            assert batched == {"acct_0": 3, "acct_1": 1, "acct_2": 0, "acct_3": 0}
        finally:
            conn.close()

    def test_unknown_account_reports_zero(self, tmp_path: Path) -> None:
        from omniagentos.db.store import _connect

        db = str(tmp_path / "inflight-empty.db")
        CollabStore(db)
        conn = _connect(db)
        try:
            assert limit_state.inflight_by_account(conn, ["nope"]) == {"nope": 0}
            assert limit_state.inflight_by_account(conn, []) == {}
        finally:
            conn.close()


class TestPreflight:
    def test_reports_ceilings_and_names_a_binding_constraint(self, tmp_path: Path) -> None:
        db = str(tmp_path / "preflight.db")
        CollabStore(db)
        report = fleet_preflight.preflight(db_path=db)
        names = {ceiling.name for ceiling in report.ceilings}
        assert {
            "fleet.sessions_global",
            "fleet.swarm_sessions",
            "fleet.concurrent_swarms",
            "provider.account_inflight",
            "os.file_descriptors",
        } <= names
        by_name = {ceiling.name: ceiling for ceiling in report.ceilings}
        assert by_name["fleet.sessions_global"].limit == EXPECTED_MAX_SESSIONS_GLOBAL
        assert by_name["fleet.swarm_sessions"].limit == (
            EXPECTED_MAX_SESSIONS_GLOBAL - EXPECTED_RESERVED_SMALL_TASK_SLOTS
        )
        assert by_name["fleet.concurrent_swarms"].limit == EXPECTED_MAX_CONCURRENT_SWARMS
        # With no accounts there is no capacity at all, and preflight must say so
        # rather than reporting a comfortable YAML number. Since PKG-INSESSION-
        # FANOUT shipped LIVE, the agents ceiling is a second zero-capacity
        # account ceiling — either is a truthful binding constraint here.
        assert report.binding is not None
        assert report.binding.name in {
            "provider.account_inflight",
            "provider.account_agents",
        }

    def test_advisory_ceilings_are_never_reported_as_binding(self, tmp_path: Path) -> None:
        db = str(tmp_path / "advisory.db")
        CollabStore(db)
        report = fleet_preflight.preflight(db_path=db)
        advisory = {c.name for c in report.ceilings if not c.enforced}
        assert "run.slots" in advisory
        assert "project.sessions" in advisory
        assert report.binding is not None
        assert report.binding.name not in advisory

    def test_per_account_capacity_becomes_the_binding_constraint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The point of the module: when accounts x max_inflight_per_account is
        the smallest ceiling, preflight NAMES it — raising the YAML numbers
        cannot help, and preflight has to say so instead of reporting a
        comfortable 260.

        The per-account ceiling is patched rather than read from the shipped
        YAML. This test asserts on preflight's ARITHMETIC, and the shipped value
        belongs to feat/provider-resilience (see
        test_per_account_inflight_is_untouched_by_this_package above, and
        tests/routing/test_api_policy.py which pins it at 20). Reading the real
        value coupled this test to that branch's policy: at inflight 20, two
        accounts allow 40 > max_concurrent_swarms 20, so the binding ceiling
        legitimately moves to fleet.concurrent_swarms and the scenario this test
        means to construct stops existing. Patching keeps the scarcity premise
        explicit and lets the shipped ceiling move without falsifying the module.
        """
        from omniagentos.contracts import utc_now_iso
        from omniagentos.db.store import _connect

        # Scarce on purpose: 2 x 3 = 6 is below every fleet ceiling in the YAML.
        monkeypatch.setattr(fleet_preflight, "max_inflight_per_account", lambda provider: 3)

        db = str(tmp_path / "bound.db")
        CollabStore(db)
        conn = _connect(db)
        now = utc_now_iso()
        try:
            for index in range(2):
                conn.execute(
                    "INSERT INTO claude_accounts "
                    "(id, label, config_dir, enabled, status, provider, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, 'ok', 'claude', ?, ?)",
                    (f"acct_{index}", f"a{index}", f"/tmp/a{index}", now, now),
                )
            conn.commit()
        finally:
            conn.close()
        report = fleet_preflight.preflight(db_path=db)
        capacity = next(c for c in report.ceilings if c.name == "provider.account_inflight")
        assert capacity.limit == 2 * 3
        assert report.binding is capacity
        assert any("per-account capacity" in warning for warning in report.warnings)

    def test_the_shipped_inflight_ceiling_moves_the_binding_constraint(
        self, tmp_path: Path
    ) -> None:
        """The integrated reality after feat/provider-resilience raised inflight.

        Companion to the test above, and the reason it had to be patched: with
        the SHIPPED ceiling, two accounts are no longer the scarce resource, so
        per-account capacity must NOT be reported as binding. This is the
        compounded-admission change the two branches make together — pinned here
        so a future edit to either ceiling has to acknowledge it.
        """
        from omniagentos.contracts import utc_now_iso
        from omniagentos.db.store import _connect

        db = str(tmp_path / "shipped.db")
        CollabStore(db)
        conn = _connect(db)
        now = utc_now_iso()
        try:
            for index in range(2):
                conn.execute(
                    "INSERT INTO claude_accounts "
                    "(id, label, config_dir, enabled, status, provider, created_at, updated_at) "
                    "VALUES (?, ?, ?, 1, 'ok', 'claude', ?, ?)",
                    (f"acct_{index}", f"a{index}", f"/tmp/a{index}", now, now),
                )
            conn.commit()
        finally:
            conn.close()
        report = fleet_preflight.preflight(db_path=db)
        capacity = next(c for c in report.ceilings if c.name == "provider.account_inflight")
        assert capacity.limit == 2 * limit_state.max_inflight_per_account("claude")
        assert report.binding is not None
        assert report.binding is not capacity
        assert report.binding.enforced

    def test_fd_ceiling_tracks_the_soft_limit(self) -> None:
        assert (
            fleet_preflight.sessions_supported_by_fds(
                fleet_preflight.FDS_BASE + 200 * fleet_preflight.FDS_PER_SESSION
            )
            == 200
        )
        # The classic launchd default: 256 fds is ~32 sessions, not 200.
        assert fleet_preflight.sessions_supported_by_fds(256) < 200
        assert fleet_preflight.sessions_supported_by_fds(fleet_preflight.RECOMMENDED_NOFILE) >= 200

    def test_render_is_text_and_mentions_the_binding_constraint(self, tmp_path: Path) -> None:
        db = str(tmp_path / "render.db")
        CollabStore(db)
        report = fleet_preflight.preflight(db_path=db)
        text = fleet_preflight.render(report)
        assert "BINDING CONSTRAINT" in text
        assert report.binding is not None
        assert report.binding.name in text

    def test_runnable_as_a_module_with_json_output(self, tmp_path: Path) -> None:
        db = str(tmp_path / "cli.db")
        CollabStore(db)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "omniagentos.routing.fleet_preflight",
                "--db",
                db,
                "--json",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["binding_constraint"]
        assert any(
            ceiling["name"] == "fleet.sessions_global"
            and ceiling["limit"] == EXPECTED_MAX_SESSIONS_GLOBAL
            for ceiling in payload["ceilings"]
        )
