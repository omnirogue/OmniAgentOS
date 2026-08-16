"""Provisioning service: scoped project creation + auto-grant-vs-escalate.

The LLM and the AC-policy ``is_hard_stop`` contract are both stubbed here — the
tests exercise the decision logic, not the model or the policy package.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# Import the api package first so its app is fully initialized before intake:
# intake -> api.services -> api.__init__ -> api.main -> routes.intake -> intake
# is a pre-existing import cycle that only bites when intake is imported first
# (e.g. running this module in isolation). The full suite loads api earlier.
import omniagentos.api.main  # noqa: F401,E402  (import-order guard)
from omniagentos.contracts import ActionClass  # noqa: E402
from omniagentos.db.store import SqliteStore
from omniagentos.intake.contracts import RefinedSpec
from omniagentos.projects import ProjectStore
from omniagentos.provision import (
    ProvisionServiceError,
    build_scope_view,
    provision_project,
    request_scope,
)
from omniagentos.simgate import SimContext, SimGateError


@pytest.fixture
def store() -> SqliteStore:
    return SqliteStore(":memory:")


def _spec() -> RefinedSpec:
    return RefinedSpec(
        title="Reconcile Stripe payouts",
        description="Pull recent payouts and reconcile them against the ledger.",
        acceptance_criteria=["payouts reconciled"],
    )


def _llm(connectors: list[str], existing: str | None = None) -> Any:
    def run(_prompt: str, _schema: dict[str, Any]) -> dict[str, Any]:
        return {"existing_project": existing, "connectors": connectors, "reason": "stub"}

    return run


def _stub_hard_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the AC-policy is_hard_stop contract: consequential is a hard stop."""
    import omniagentos.policy as policy_mod

    def stub(action_class: Any) -> bool:
        return ActionClass(action_class) == ActionClass.CONSEQUENTIAL

    monkeypatch.setattr(policy_mod, "is_hard_stop", stub, raising=False)


# --- provisioning determines dirs + APIs -----------------------------------


def test_provision_creates_scoped_project_with_dirs_and_apis(
    store: SqliteStore,
    provision_var_dir: Path,
) -> None:
    result = provision_project(store, _spec(), llm=_llm(["stripe_acmeuni.read"]))

    # A fresh scoped workspace under ~/var/projects/<project_id>/ — never a home
    # or process-cwd dir.
    assert result.created is True
    expected = provision_var_dir / "projects" / result.project_id
    assert Path(result.working_dir) == expected
    assert expected.is_dir()
    assert result.root_dirs == [str(expected)]

    # The determined connector API is the project's allowed scope.
    assert result.allowed_connectors == ["stripe_acmeuni.read"]
    row = ProjectStore(store).get_project(result.project_id)
    assert row is not None
    assert "stripe_acmeuni.read" in row["allowed_tools"]
    assert row["root_dirs"] == [str(expected)]


def test_provision_heuristic_grants_no_connectors_when_llm_unavailable(
    store: SqliteStore,
    provision_var_dir: Path,
) -> None:
    # LLM returns None -> deny-by-default: fresh workspace, no external APIs.
    result = provision_project(store, _spec(), llm=lambda _p, _s: None)

    assert result.created is True
    assert result.allowed_connectors == []
    assert Path(result.working_dir).is_dir()


def test_provision_drops_hallucinated_connectors(
    store: SqliteStore,
    provision_var_dir: Path,
) -> None:
    result = provision_project(
        store, _spec(), llm=_llm(["stripe_acmeuni.read", "not_a_real.connector"])
    )
    # Only the real registry capability survives.
    assert result.allowed_connectors == ["stripe_acmeuni.read"]


def test_provision_attaches_to_existing_project(store: SqliteStore, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectStore(store).create_project({"name": "Ledger repo", "root_dirs": [str(repo)]})

    result = provision_project(
        store,
        _spec(),
        project_id=str(project["id"]),
        llm=_llm(["stripe_acmeuni.read"]),
    )
    # Attaches (does not create), works in the project's own root, and widens its
    # connector scope with the determined API.
    assert result.created is False
    assert result.working_dir == str(repo)
    assert result.project_id == str(project["id"])
    assert "stripe_acmeuni.read" in result.allowed_connectors


# --- request more scope: auto-grant vs escalate ----------------------------


def test_request_normal_dir_auto_grants(store: SqliteStore, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    project = ProjectStore(store).create_project({"name": "p", "root_dirs": [str(home)]})

    result = request_scope(
        store,
        str(project["id"]),
        dir=str(home / "work"),
        allow_roots=[str(home)],
    )
    # A dir inside an approved grant root is auto-granted and appended to the project.
    assert result.decision == "auto_granted"
    assert result.granted is True
    assert result.status == "granted"
    row = ProjectStore(store).get_project(str(project["id"]))
    assert row is not None
    assert str(home / "work") in row["root_dirs"]


def test_request_dir_outside_home_escalates(store: SqliteStore, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    project = ProjectStore(store).create_project({"name": "p", "root_dirs": [str(home)]})

    result = request_scope(
        store,
        str(project["id"]),
        dir=str(outside),
        allow_roots=[str(home)],
    )
    # A dir outside the approved roots is a hard stop -> escalation, never granted.
    assert result.decision == "escalated"
    assert result.granted is False
    assert result.status == "pending"
    row = ProjectStore(store).get_project(str(project["id"]))
    assert row is not None
    assert str(outside) not in row["root_dirs"]


def test_request_sim_dir_inside_campaign_is_granted_and_production_checkout_refused(
    store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign_root = tmp_path / "campaign"
    allowed = campaign_root / "allowed"
    allowed.mkdir(parents=True)
    context = SimContext(sim_mode=True, campaign="campaign", campaign_root=campaign_root)
    # environ arg: runtime_paths calls resolve_sim_context(environ); board_files
    # and dir_grants call it bare. The stub must accept both.
    monkeypatch.setattr(
        "omniagentos.simgate.resolve_sim_context", lambda environ=None: context
    )
    project = ProjectStore(store).create_project({"name": "p"})

    granted = request_scope(
        store,
        str(project["id"]),
        dir=str(allowed),
        allow_roots=[str(Path("/"))],
    )
    assert granted.granted is True
    assert granted.status == "granted"

    production_checkout = tmp_path / "production-checkout"
    production_checkout.mkdir()
    refused = request_scope(
        store,
        str(project["id"]),
        dir=str(production_checkout),
        allow_roots=[str(production_checkout)],
    )
    assert refused.granted is False
    assert refused.decision == "escalated"
    assert refused.status == "pending"


def test_request_scope_simgate_error_is_refused_without_a_phantom_request(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    error = SimGateError("invalid simulation context")
    monkeypatch.setattr(
        "omniagentos.simgate.resolve_sim_context",
        lambda environ=None: (_ for _ in ()).throw(error),
    )

    project = ProjectStore(store).create_project({"name": "p"})
    with pytest.raises(SimGateError, match="invalid simulation context"):
        request_scope(store, str(project["id"]), dir="/etc")

    assert build_scope_view(store, str(project["id"]))["scope_requests"] == []
    assert "provision/request_scope: sim gate refused: invalid simulation context" in caplog.text


def test_request_secret_dir_is_hard_rejected(store: SqliteStore, tmp_path: Path) -> None:
    """AC-policy fix7: a request for a secret-registry dir (~/.ssh) is HARD-REJECTED.

    The old ``_dir_within_home`` rule auto-granted any dir under $HOME, including
    ~/.ssh. It must now be refused outright — never granted, never even escalated
    to a human approval queue."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    project = ProjectStore(store).create_project({"name": "p", "root_dirs": [str(home)]})

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(home))
        with pytest.raises(ProvisionServiceError):
            request_scope(
                store,
                str(project["id"]),
                dir=str(home / ".ssh"),
                allow_roots=[str(home)],
            )
    row = ProjectStore(store).get_project(str(project["id"]))
    assert row is not None
    assert str(home / ".ssh") not in (row["root_dirs"] or [])


def test_request_read_connector_auto_grants(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_hard_stop(monkeypatch)
    project = ProjectStore(store).create_project({"name": "p"})

    result = request_scope(store, str(project["id"]), connector="stripe_acmeuni.read")
    # A read/auto-tier connector is not a hard stop -> auto-granted.
    assert result.decision == "auto_granted"
    assert result.granted is True
    assert result.action_class == "read_only"
    row = ProjectStore(store).get_project(str(project["id"]))
    assert row is not None
    assert "stripe_acmeuni.read" in row["allowed_tools"]


def test_request_money_connector_escalates(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_hard_stop(monkeypatch)
    project = ProjectStore(store).create_project({"name": "p"})

    # A money-move (consequential) connector is a hard stop -> escalation.
    result = request_scope(store, str(project["id"]), connector="stripe_acmeuni.charge")
    assert result.decision == "escalated"
    assert result.granted is False
    assert result.status == "pending"
    assert result.action_class == "consequential"
    # NOT granted: the project's allowed scope is unchanged.
    row = ProjectStore(store).get_project(str(project["id"]))
    assert row is not None
    assert "stripe_acmeuni.charge" not in row["allowed_tools"]


def test_request_uses_is_hard_stop_contract(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escalate/grant decision defers to the AC-policy is_hard_stop stub."""
    import omniagentos.policy as policy_mod

    calls: list[Any] = []

    def stub(action_class: Any) -> bool:
        calls.append(action_class)
        return True  # everything is a hard stop under this stub

    monkeypatch.setattr(policy_mod, "is_hard_stop", stub, raising=False)
    project = ProjectStore(store).create_project({"name": "p"})

    result = request_scope(store, str(project["id"]), connector="stripe_acmeuni.read")
    # Even a read connector escalates when the contract says hard-stop.
    assert calls == [ActionClass.READ_ONLY]
    assert result.decision == "escalated"
    assert result.granted is False


def test_request_rejects_both_or_neither(store: SqliteStore) -> None:
    project = ProjectStore(store).create_project({"name": "p"})
    with pytest.raises(ProvisionServiceError):
        request_scope(store, str(project["id"]))
    with pytest.raises(ProvisionServiceError):
        request_scope(store, str(project["id"]), dir="/tmp/x", connector="stripe_acmeuni.read")


def test_request_unknown_connector_is_rejected(store: SqliteStore) -> None:
    project = ProjectStore(store).create_project({"name": "p"})
    with pytest.raises(ProvisionServiceError):
        request_scope(store, str(project["id"]), connector="nope.read")


# --- scope view -------------------------------------------------------------


def test_build_scope_view_reports_dirs_apis_and_requests(
    store: SqliteStore,
    provision_var_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_hard_stop(monkeypatch)
    result = provision_project(store, _spec(), llm=_llm(["stripe_acmeuni.read"]))

    # One escalation on the record.
    request_scope(store, result.project_id, connector="stripe_acmeuni.charge")

    view = build_scope_view(store, result.project_id)
    assert view is not None
    assert view["root_dirs"] == result.root_dirs
    connectors = {c["id"]: c for c in view["connectors"]}
    assert "stripe_acmeuni.read" in connectors
    assert connectors["stripe_acmeuni.read"]["action_class"] == "read_only"
    assert connectors["stripe_acmeuni.read"]["hard_stop"] is False
    # The escalation is on the decision log.
    reqs = view["scope_requests"]
    assert any(
        r["scope_value"] == "stripe_acmeuni.charge" and r["decision"] == "escalated" for r in reqs
    )


def test_build_scope_view_unknown_project_is_none(store: SqliteStore) -> None:
    assert build_scope_view(store, "proj_missing") is None


def test_scope_request_persisted_shape(
    store: SqliteStore,
    tmp_path: Path,
    provision_var_dir: Path,
) -> None:
    result = provision_project(store, _spec(), llm=_llm([]))
    home = tmp_path / "home"
    (home / "d").mkdir(parents=True)
    req = request_scope(store, result.project_id, dir=str(home / "d"), allow_roots=[str(home)])
    assert req.request_id.startswith("psr")
    # Round-trips through the store.
    rows = json.loads(json.dumps(build_scope_view(store, result.project_id)["scope_requests"]))
    assert rows[0]["id"] == req.request_id
