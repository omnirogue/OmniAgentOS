"""AT-04 — Tool assignment: correct tools, correct permissions, no unauthorized tools.

Every assertion here is grounded in the real toolplane kernel:

* ``omniagentos.toolplane.tools`` — ``TOOLS`` registry, ``CAPABILITY_INVENTORY``
  and the ``dispatch`` authorization gate (the only way a capability executes).
* ``omniagentos.toolplane.manifest`` — ``CapabilityManifest``, the per-invocation
  capability ceiling, and its fail-closed parser.
* ``omniagentos.toolplane.exposure`` — ``compute_exposure``, which decides what an
  identity is allowed to even SEE before a prompt is serialized.
* ``omniagentos.policy`` — ``validate_tools``, the fail-closed allowlist check.

The load-bearing idea being tested is MINIMUM TOOLS: a role receives exactly the
capabilities it was granted and nothing else, the grant is enforced per capability
(not per category), and an ungranted capability produces no side effect at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.connectors import ResultSizeClass, SideEffectClass
from omniagentos.contracts import ActionClass
from omniagentos.policy import PolicyError, load_policy, validate_tools
from omniagentos.toolplane.catalog import CatalogEntry, default_catalog
from omniagentos.toolplane.exposure import (
    ExposureContext,
    compute_exposure,
    enforce_argv_patch,
)
from omniagentos.toolplane.manifest import (
    CapabilityManifest,
    ManifestValidationError,
    load_manifest,
)
from omniagentos.toolplane.tools import (
    CAPABILITY_INVENTORY,
    TOOLS,
    dispatch,
    get_capability_inventory,
)

# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

#: Capabilities that reach a network/provider seam. An acceptance test must never
#: dispatch these, so the per-capability sweep asserts denial only (never a call).
_EGRESS_CAPABILITIES = frozenset(
    {
        "connector_invoke",
        "globex_generate_image",
        "globex_generate_video",
        "voice_tts",
    }
)


def manifest_for(tmp_path: Path, **overrides: Any) -> CapabilityManifest:
    """A minimal, well-formed manifest rooted entirely inside ``tmp_path``."""
    (tmp_path / "read").mkdir(exist_ok=True)
    (tmp_path / "write").mkdir(exist_ok=True)
    data: dict[str, Any] = {
        "run_id": "run-at04",
        "session_id": "session-at04",
        "holder_generation": 7,
        "read_roots": [str(tmp_path / "read")],
        "write_roots": [str(tmp_path / "write")],
        "allowed_ops": ["read_file"],
    }
    data.update(overrides)
    return load_manifest(data)


def catalog_entry(
    cap_id: str,
    *,
    source: str,
    risk: str,
    action_class: ActionClass = ActionClass.READ_ONLY,
) -> CatalogEntry:
    """A fully specified synthetic catalog entry.

    Built explicitly rather than sliced out of ``default_catalog()`` so an
    exposure assertion cannot pass or fail for a reason that lives in
    ``configs/connectors.yaml``.
    """
    return CatalogEntry(
        id=cap_id,
        namespace=cap_id.split(".", 1)[0],
        label=cap_id,
        compact_hint=f"hint for {cap_id}",
        description=f"description for {cap_id}",
        source=source,  # type: ignore[arg-type]
        action_class=action_class,
        read_only=action_class is ActionClass.READ_ONLY,
        side_effect_class=SideEffectClass.NONE,
        resource_keys=(),
        idempotent=True,
        parallel_safe=True,
        cancellation_group=cap_id,
        credential_scope="",
        result_size_class=ResultSizeClass.SMALL,
        risk=risk,  # type: ignore[arg-type]
        requires_scope=True,
        input_examples=(),
        parameter_names=(),
        callable_now=True,
        classified=True,
    )


# --------------------------------------------------------------------------
# The registry is closed: no tool exists without a declared classification
# --------------------------------------------------------------------------


class TestRegistryIsClosed:
    def test_every_dispatchable_tool_is_classified(self) -> None:
        """A capability that ``dispatch`` can run but the inventory does not
        classify is an unaudited tool — exactly the hole M-08 exists to close."""
        assert TOOLS, "the tool registry is empty; the sweep below would be vacuous"
        assert set(TOOLS) == set(CAPABILITY_INVENTORY), (
            "TOOLS and CAPABILITY_INVENTORY diverged: "
            f"dispatchable-but-unclassified={sorted(set(TOOLS) - set(CAPABILITY_INVENTORY))}, "
            f"classified-but-undispatchable={sorted(set(CAPABILITY_INVENTORY) - set(TOOLS))}"
        )

    def test_every_classification_declares_category_and_risk(self) -> None:
        for cap_id, meta in CAPABILITY_INVENTORY.items():
            assert meta.get("category"), f"{cap_id} has no category"
            assert meta["risk"] in {"low", "medium", "high"}, f"{cap_id} has an unknown risk"
            assert isinstance(meta.get("requires_scope"), bool), f"{cap_id} has no scope flag"

    def test_inventory_accessor_returns_a_copy(self) -> None:
        """The audit surface must not hand out the live dict — a caller that
        mutated it would silently widen every future authorization decision."""
        snapshot = get_capability_inventory()
        snapshot["read_file"] = {"category": "tampered", "risk": "low", "requires_scope": False}
        assert CAPABILITY_INVENTORY["read_file"]["category"] == "fs_read"


# --------------------------------------------------------------------------
# Minimum tools: every capability is gated INDIVIDUALLY
# --------------------------------------------------------------------------


class TestMinimumTools:
    @pytest.mark.parametrize("target", sorted(TOOLS))
    def test_capability_denied_when_it_alone_is_withheld(self, tmp_path: Path, target: str) -> None:
        """Grant every capability EXCEPT ``target`` and prove ``target`` is denied.

        This is the minimum-tools proof: the gate is per capability, so a role
        cannot inherit a neighbouring capability from the same category. It also
        makes the sweep robust — deleting the ``_allow`` check in ``dispatch``
        fails this for all fourteen capabilities at once.
        """
        allowed = sorted(set(TOOLS) - {target})
        manifest = manifest_for(tmp_path, allowed_ops=allowed)

        result = dispatch(target, manifest, {"path": str(tmp_path / "read")})

        assert result["ok"] is False, f"{target} executed while withheld from allowed_ops"
        assert result["error"] == "not_allowed"
        assert target in result["detail"]

    @pytest.mark.parametrize("target", sorted(set(TOOLS) - _EGRESS_CAPABILITIES))
    def test_capability_permitted_when_it_alone_is_granted(
        self, tmp_path: Path, target: str
    ) -> None:
        """The mirror of the sweep above: with ONLY ``target`` granted, the gate
        lets it through.

        Without this the deny sweep would pass just as well against a manifest
        that denies everything unconditionally, which proves nothing about the
        grant actually being honoured. Args are deliberately invalid, so the call
        stops at argument validation — the assertion is only that authorization
        was not the thing that rejected it.
        """
        manifest = manifest_for(tmp_path, allowed_ops=[target])

        result = dispatch(target, manifest, {})

        assert result.get("error") != "not_allowed", f"{target} was denied while granted"

    def test_empty_allowlist_grants_nothing(self, tmp_path: Path) -> None:
        manifest = manifest_for(tmp_path, allowed_ops=[])
        for cap_id in sorted(TOOLS):
            result = dispatch(cap_id, manifest, {})
            assert result["ok"] is False, f"{cap_id} ran under an empty allowlist"
            assert result["error"] == "not_allowed"


# --------------------------------------------------------------------------
# No unauthorized tools: denial has NO side effect
# --------------------------------------------------------------------------


class TestUnauthorizedToolsHaveNoEffect:
    def test_ungranted_write_creates_no_file(self, tmp_path: Path) -> None:
        """A denial that still wrote the file would be a green test over a real
        breach, so the observable effect is asserted, not just the return code."""
        manifest = manifest_for(tmp_path, allowed_ops=["read_file"])
        target = tmp_path / "write" / "should-not-exist.txt"

        result = dispatch("write_file", manifest, {"path": str(target), "content": "x"})

        assert result["ok"] is False
        assert result["error"] == "not_allowed"
        assert not target.exists(), "a denied write still created the file"

    def test_ungranted_mkdir_creates_no_directory(self, tmp_path: Path) -> None:
        manifest = manifest_for(tmp_path, allowed_ops=["read_file"])
        target = tmp_path / "write" / "nope"

        result = dispatch("make_dir", manifest, {"path": str(target)})

        assert result["ok"] is False
        assert not target.exists(), "a denied make_dir still created the directory"

    def test_unknown_capability_is_denied_by_default(self, tmp_path: Path) -> None:
        """Deny-by-default: an unregistered name is rejected as unknown even when
        the manifest names it, so a typo or a forged grant cannot open a hole."""
        manifest = manifest_for(tmp_path, allowed_ops=["totally_made_up_tool"])

        result = dispatch("totally_made_up_tool", manifest, {})

        assert result["ok"] is False
        assert result["error"] == "unknown_capability"

    def test_identityless_manifest_gets_no_tools(self, tmp_path: Path) -> None:
        """``holder_generation`` is the identity binding; without it nothing runs.

        The manifest is built directly (bypassing ``load_manifest``, which would
        reject it earlier) precisely to prove ``dispatch`` re-checks rather than
        trusting its caller.
        """
        naked = CapabilityManifest(
            run_id="run-at04",
            session_id="session-at04",
            holder_generation=None,  # type: ignore[arg-type]
            read_roots=[str(tmp_path)],
            write_roots=[str(tmp_path)],
            allowed_ops=sorted(TOOLS),
        )

        result = dispatch("read_file", naked, {"path": str(tmp_path)})

        assert result["ok"] is False
        assert result["error"] == "missing_holder_generation"

    def test_manifest_parser_refuses_missing_identity(self) -> None:
        with pytest.raises(ManifestValidationError) as excinfo:
            load_manifest({"run_id": "r", "session_id": "s"})
        assert excinfo.value.error == "missing_holder_generation"


# --------------------------------------------------------------------------
# Correct permissions: read grants never imply write grants
# --------------------------------------------------------------------------


class TestPermissionScoping:
    def test_granted_capability_still_bounded_by_roots(self, tmp_path: Path) -> None:
        """Holding ``write_file`` is not permission to write ANYWHERE — the
        capability grant and the path scope are independent gates."""
        manifest = manifest_for(tmp_path, allowed_ops=["write_file"])
        outside = tmp_path / "outside.txt"

        result = dispatch("write_file", manifest, {"path": str(outside), "content": "x"})

        assert result["ok"] is False
        assert result["error"] == "out_of_scope"
        assert not outside.exists()

    def test_read_only_role_cannot_write_into_its_read_root(self, tmp_path: Path) -> None:
        """The classic least-privilege shape: a reviewer role gets a read root and
        NO write roots. Its read root must not become writable by omission."""
        manifest = manifest_for(
            tmp_path,
            allowed_ops=["read_file", "write_file"],
            write_roots=[],
        )
        target = tmp_path / "read" / "inject.txt"

        result = dispatch("write_file", manifest, {"path": str(target), "content": "x"})

        assert result["ok"] is False
        assert result["error"] == "out_of_scope"
        assert not target.exists(), "a role with no write roots wrote a file"

    def test_granted_read_inside_root_succeeds(self, tmp_path: Path) -> None:
        """The positive control for the two denials above."""
        manifest = manifest_for(tmp_path, allowed_ops=["read_file"])
        source = tmp_path / "read" / "ok.txt"
        source.write_text("hello acceptance\n", encoding="utf-8")

        result = dispatch("read_file", manifest, {"path": str(source)})

        assert result["ok"] is True
        assert "hello acceptance" in result["result"]["content"]

    def test_secret_material_is_never_readable(self, tmp_path: Path) -> None:
        """Even a correctly scoped, correctly granted read is refused for
        credential material. Asserts the DENIAL, never the value."""
        manifest = manifest_for(tmp_path, allowed_ops=["read_file"])
        secret = tmp_path / "read" / ".env"
        secret.write_text("API_KEY=placeholder\n", encoding="utf-8")

        result = dispatch("read_file", manifest, {"path": str(secret)})

        assert result["ok"] is False
        assert result["error"] == "secret_path"


# --------------------------------------------------------------------------
# Exposure: an unauthorized tool is never serialized into a prompt
# --------------------------------------------------------------------------


class TestExposureNeverLeaksUngrantedTools:
    def test_ungranted_connector_capability_is_hidden(self) -> None:
        catalog = {
            "read_file": catalog_entry("read_file", source="builtin", risk="low"),
            "acme.read": catalog_entry("acme.read", source="connector", risk="low"),
            "acme.write": catalog_entry("acme.write", source="connector", risk="low"),
        }

        decision = compute_exposure(
            ExposureContext(run_id="r"), grants=["acme.read"], catalog=catalog
        )

        assert decision.fallback is False, "the fallback path would mask the policy filter"
        assert "acme.write" in decision.hidden
        assert "acme.write" not in decision.visible
        assert "acme.read" in decision.visible

    def test_hidden_and_visible_never_overlap(self) -> None:
        catalog = {
            "read_file": catalog_entry("read_file", source="builtin", risk="low"),
            "acme.read": catalog_entry("acme.read", source="connector", risk="low"),
            "acme.danger": catalog_entry("acme.danger", source="connector", risk="high"),
        }

        decision = compute_exposure(ExposureContext(), grants=["acme.read"], catalog=catalog)

        assert set(decision.hidden) & set(decision.visible) == set()
        assert set(decision.allowed) <= set(decision.visible)

    def test_risk_ceiling_hides_over_risk_tools_even_when_granted(self) -> None:
        """A grant does not defeat the risk ceiling: a low-risk identity never
        sees a high-risk capability, however it was granted."""
        catalog = {
            "acme.read": catalog_entry("acme.read", source="connector", risk="low"),
            "acme.danger": catalog_entry("acme.danger", source="connector", risk="high"),
        }

        decision = compute_exposure(
            ExposureContext(), grants=["acme.read", "acme.danger"], risk="low", catalog=catalog
        )

        assert "acme.danger" in decision.hidden
        assert "acme.danger" not in decision.visible

    def test_unknown_risk_level_fails_closed(self) -> None:
        catalog = {"acme.weird": catalog_entry("acme.weird", source="connector", risk="nonsense")}

        decision = compute_exposure(ExposureContext(), grants=["acme.weird"], catalog=catalog)

        assert "acme.weird" in decision.hidden, "an unclassifiable risk level failed OPEN"

    def test_real_catalog_hides_every_ungranted_connector_capability(self) -> None:
        """The synthetic-catalog proofs above, restated against the shipped
        catalog so a real connector entry cannot slip past with no grants."""
        catalog = default_catalog()
        connector_ids = {cap for cap, entry in catalog.items() if entry.source == "connector"}
        assert connector_ids, "no connector entries in the shipped catalog; test is vacuous"

        decision = compute_exposure(ExposureContext(), grants=[], catalog=catalog)

        assert decision.fallback is False
        assert connector_ids <= set(decision.hidden)
        assert not (connector_ids & set(decision.visible))

    def test_argv_patch_only_ever_narrows(self) -> None:
        """``enforce_argv_patch`` must union onto an existing --disallowedTools
        value; dropping a previously disallowed native tool would widen access."""
        catalog = {
            cap: catalog_entry(cap, source="builtin", risk="high")
            for cap in ("read_file", "write_file", "edit_file")
        }
        decision = compute_exposure(ExposureContext(), grants=[], risk="low", catalog=catalog)
        assert decision.hidden, "nothing hidden; the patch assertion would be vacuous"

        argv = ["claude", "--disallowedTools", "PreexistingTool"]
        patched = enforce_argv_patch(argv, decision)

        assert "--disallowedTools" in patched
        disallowed = set(patched[patched.index("--disallowedTools") + 1].split(","))
        assert "PreexistingTool" in disallowed, "the patch dropped a pre-existing denial"
        assert len(disallowed) > 1, "the patch added nothing for the hidden capabilities"


# --------------------------------------------------------------------------
# Policy kernel allowlist
# --------------------------------------------------------------------------


class TestPolicyAllowlistFailsClosed:
    def test_unknown_bare_tool_is_rejected(self) -> None:
        cfg = load_policy()
        with pytest.raises(PolicyError, match="Unknown tool"):
            validate_tools(["definitely_not_a_real_primitive"], cfg)

    def test_unknown_namespaced_capability_is_rejected(self) -> None:
        cfg = load_policy()
        with pytest.raises(PolicyError, match="Unknown capability"):
            validate_tools(["no_such_connector.no_such_action"], cfg)

    def test_known_primitives_are_accepted(self) -> None:
        """Positive control — otherwise the two rejections above would also pass
        against a ``validate_tools`` that rejected everything."""
        cfg = load_policy()
        assert cfg.tools.known, "policy declares no known tools; the test is vacuous"
        validate_tools(sorted(cfg.tools.known), cfg)
