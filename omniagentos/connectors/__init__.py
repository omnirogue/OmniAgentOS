"""Connector registry: the catalogue of external systems agents may reach.

Loads and validates ``configs/connectors.yaml``. This module is pure -- it never
reads a credential and never performs I/O against a connector. It answers three
questions:

    * what capabilities exist, and what is each one's ActionClass?
    * which environment variables back a given connector?
    * is a capability id one that an agent may hold at all?

Credential resolution and call execution live in ``broker.py``, which is the only
module permitted to read secret material.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from omniagentos.contracts import ActionClass

LOG = logging.getLogger(__name__)

# ActionClasses that can never run unattended, regardless of policy.yaml. The
# broker enforces this in code (see broker.HARD_HUMAN_CLASSES) so that a config
# edit alone cannot promote a refund or an ad launch into the auto tier.
AUTO_CLASSES: frozenset[ActionClass] = frozenset(
    {
        ActionClass.READ_ONLY,
        ActionClass.SANDBOXED_CREATION,
        ActionClass.INTERNAL_REVERSIBLE,
    }
)


class ConnectorError(ValueError):
    """Raised when the registry is malformed or a capability is unknown."""


class HttpSpec(BaseModel):
    """Outbound HTTP allowlist for one capability.

    A capability with no HttpSpec is *declared but not callable*: the broker
    refuses it. This is deliberate -- a connector appears in the catalogue and in
    the UI before its call path has been reviewed, and the safe default for an
    unreviewed path is 'no'.

    ``methods`` + ``path_prefixes`` together ARE the read/write boundary. The
    credential behind a read capability is usually fully privileged (a Stripe
    secret key reads *and* refunds), so 'read-only' is only true insofar as this
    allowlist makes it true. Widening either field widens what the agent can do.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str
    methods: list[str] = Field(default_factory=lambda: ["GET"])
    # Prefix match. Sufficient for read capabilities, where the risk is reading the
    # wrong collection. NOT sufficient for writes with a variable id in the middle
    # of the path -- '/contacts/' would admit POST /contacts/{id}/anything. Use
    # path_regex for those.
    path_prefixes: list[str] = Field(default_factory=list)
    # Full-match regex, checked in addition to path_prefixes when present. This is
    # what pins a write to one exact subresource:
    #   ^/contacts/[A-Za-z0-9_-]+/notes/?$
    path_regex: str = ""
    # Auth scheme, resolved by the broker at call time against the process env:
    #   "bearer:ENV_NAME"      -> Authorization: Bearer <value>
    #   "basic:USER_ENV:PW_ENV"-> Authorization: Basic <b64>
    #   "query:param:ENV_NAME" -> appended as a query parameter
    #   "form:param:ENV_NAME"  -> added to a form-encoded request body
    auth: str = ""
    headers: dict[str, str] = Field(default_factory=dict)  # static, non-secret headers

    @field_validator("path_regex")
    @classmethod
    def _compilable(cls, value: str) -> str:
        if value:
            re.compile(value)  # fail loudly at registry load, not at call time
        return value


class SideEffectClass(StrEnum):
    """What a capability changes in the world, independent of who may call it."""

    NONE = "none"  # pure read
    SANDBOX_WRITE = "sandbox_write"  # writes only into the run workspace
    INTERNAL_WRITE = "internal_write"  # mutates our own systems, trivially undone
    EXTERNAL_WRITE = "external_write"  # a third party / customer sees it
    IRREVERSIBLE = "irreversible"  # moves money, destroys data


class ResultSizeClass(StrEnum):
    """Rough size of what a call returns; drives programmatic-call routing."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    STREAMING = "streaming"


#: ActionClass -> the side effect it implies when a capability does not declare one.
ACTION_CLASS_SIDE_EFFECT: dict[ActionClass, SideEffectClass] = {
    ActionClass.READ_ONLY: SideEffectClass.NONE,
    ActionClass.SANDBOXED_CREATION: SideEffectClass.SANDBOX_WRITE,
    ActionClass.INTERNAL_REVERSIBLE: SideEffectClass.INTERNAL_WRITE,
    ActionClass.EXTERNAL_REVERSIBLE: SideEffectClass.EXTERNAL_WRITE,
    ActionClass.CONSEQUENTIAL: SideEffectClass.IRREVERSIBLE,
    ActionClass.IRREVERSIBLE: SideEffectClass.IRREVERSIBLE,
}


class Capability(BaseModel):
    """One grantable action against one connector."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    connector: str = ""
    group: str = ""
    label: str
    action_class: ActionClass
    http: HttpSpec | None = None

    namespace: str = ""
    compact_hint: str = ""
    read_only: bool | None = None
    side_effect_class: SideEffectClass | None = None
    resource_keys: tuple[str, ...] = ()
    idempotent: bool | None = None
    parallel_safe: bool | None = None
    cancellation_group: str = ""
    credential_scope: str = ""
    result_size_class: ResultSizeClass | None = None
    input_examples: tuple[str, ...] = ()

    @property
    def is_auto(self) -> bool:
        """True when this capability may run without a human in the loop."""
        return self.action_class in AUTO_CLASSES

    @property
    def callable_now(self) -> bool:
        """True when the broker has a reviewed call path for this capability."""
        return self.http is not None

    # -- resolved execution metadata -----------------------------------------
    #
    # Every field these read is OPTIONAL and defaults to unset, so a capability
    # written before this metadata existed still answers all of them. An unset field
    # is DERIVED from ``action_class`` -- the value a human already had to defend
    # when the capability was declared -- rather than from a fresh judgement made
    # here. That is what keeps this from becoming a second opinion about what a
    # capability does: the ActionClass stays the single authored fact and these are
    # projections of it.

    @property
    def resolved_namespace(self) -> str:
        """Search/grouping namespace; the connector id unless overridden."""
        return self.namespace or self.connector

    @property
    def resolved_compact_hint(self) -> str:
        """One-line hint used by deferred search, falling back to the label."""
        return self.compact_hint or self.label

    @property
    def resolved_side_effect_class(self) -> SideEffectClass:
        """What this capability changes in the world; derived from ActionClass."""
        if self.side_effect_class is not None:
            return self.side_effect_class
        return ACTION_CLASS_SIDE_EFFECT[self.action_class]

    @property
    def resolved_read_only(self) -> bool:
        """True only for a pure read. Anything undeclared and non-``none`` is a write."""
        if self.read_only is not None:
            return self.read_only
        return self.resolved_side_effect_class is SideEffectClass.NONE

    @property
    def resolved_idempotent(self) -> bool:
        """Safe to repeat after an ambiguous failure.

        Undeclared writes answer False: replaying an unknown mutation is exactly the
        outcome a retry is supposed to avoid, so the fail-safe answer is "do not".
        """
        if self.idempotent is not None:
            return self.idempotent
        return self.resolved_read_only

    @property
    def resolved_parallel_safe(self) -> bool:
        """Safe to run concurrently with another call. Undeclared writes answer False."""
        if self.parallel_safe is not None:
            return self.parallel_safe
        return self.resolved_read_only

    @property
    def resolved_resource_keys(self) -> tuple[str, ...]:
        """What this call contends over, for the scheduler's disjointness test.

        The default is deliberately COARSE -- the whole connector -- so two undeclared
        writes against the same connector collide and serialise. Narrowing it is an
        opt-in act of declaring what you actually touch. The default must never be
        "contends with nothing", which would license a bogus concurrent wave.
        """
        return self.resource_keys if self.resource_keys else (f"connector:{self.connector}",)

    @property
    def resolved_cancellation_group(self) -> str:
        """Group cancelled together when one call in it is cancelled."""
        return self.cancellation_group or self.connector

    @property
    def resolved_credential_scope(self) -> str:
        """NAME of the credential domain this reaches. Never a credential value."""
        return self.credential_scope or self.connector

    @property
    def resolved_result_size_class(self) -> ResultSizeClass:
        """Rough response size, used to route large intermediates programmatically.

        Undeclared reads answer ``medium`` (they return data); undeclared writes
        answer ``small`` (they return a receipt).
        """
        if self.result_size_class is not None:
            return self.result_size_class
        return ResultSizeClass.SMALL if not self.resolved_read_only else ResultSizeClass.MEDIUM


class Connector(BaseModel):
    """One external system, its credentials (by name), and its capabilities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    label: str
    group: str
    env: list[str] = Field(default_factory=list)
    capabilities: dict[str, Capability]


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    danger: bool = False


class ConnectorRegistry(BaseModel):
    """The validated catalogue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    groups: dict[str, Group]
    connectors: dict[str, Connector]

    @functools.cached_property
    def capabilities(self) -> dict[str, Capability]:
        """Every capability in the registry, keyed by its fully-qualified id."""
        out: dict[str, Capability] = {}
        for connector in self.connectors.values():
            for cap in connector.capabilities.values():
                out[cap.id] = cap
        return out

    def capability(self, cap_id: str) -> Capability:
        """Look up one capability, failing closed on anything unrecognised."""
        cap = self.capabilities.get(cap_id)
        if cap is None:
            raise ConnectorError(f"Unknown capability {cap_id!r}; refusing.")
        return cap

    def env_for(self, cap_ids: list[str]) -> set[str]:
        """Environment variable names backing a set of capabilities.

        Used only for reporting ('what does this agent's grant reach?'). It is NOT
        an injection list -- see the module docstring on broker.py for why agents
        are never handed these variables directly.
        """
        names: set[str] = set()
        for cap_id in cap_ids:
            cap = self.capability(cap_id)
            names.update(self.connectors[cap.connector].env)
        return names


def _default_registry_path() -> Path:
    """Resolve the registry under the configured runtime root when present.

    ``OMNIAGENTOS_VAR_DIR`` is the process-wide isolation boundary used by the
    swarm and the other runtime stores.  A nonblank override therefore selects
    its own connector catalogue rather than silently reaching back into the
    checkout's production configuration.  As with
    :func:`omniagentos.swarm.spawn.default_swarm_var_root`, the environment is
    read at call time and ``~`` is expanded.
    """
    configured = os.environ.get("OMNIAGENTOS_VAR_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser() / "connectors.yaml"
    return Path(__file__).resolve().parents[2] / "configs" / "connectors.yaml"


def compute_registry_hash(path: Path) -> str:
    """Return the SHA-256 digest of a registry's exact on-disk bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@functools.lru_cache(maxsize=4)
def load_registry(path: str | None = None) -> ConnectorRegistry:
    """Load, validate, and cache the connector registry.

    Capability and connector ids are denormalised onto their children so a
    Capability is self-describing once it leaves the registry.
    """
    target = Path(path) if path else _default_registry_path()
    try:
        raw: Any = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConnectorError(f"Unable to read connector registry {str(target)!r}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConnectorError("Connector registry must be a mapping.")

    # Denormalise ids down the tree before validation so Capability.id is populated.
    for connector_id, connector in (raw.get("connectors") or {}).items():
        if not isinstance(connector, dict):
            raise ConnectorError(f"Connector {connector_id!r} must be a mapping.")
        connector["id"] = connector_id
        group = connector.get("group", "")
        for cap_id, cap in (connector.get("capabilities") or {}).items():
            if not isinstance(cap, dict):
                raise ConnectorError(f"Capability {cap_id!r} must be a mapping.")
            cap["id"] = cap_id
            cap["connector"] = connector_id
            cap["group"] = group
            cap.setdefault("namespace", connector_id)

    try:
        registry = ConnectorRegistry.model_validate(raw)
    except ValidationError as exc:
        raise ConnectorError(f"Invalid connector registry: {exc}") from exc

    # A capability id must be namespaced by its connector. This is what makes the
    # grant list readable and prevents 'read' from meaning Stripe in one agent and
    # Postgres in another.
    for cap_id, cap in registry.capabilities.items():
        if not cap_id.startswith(f"{cap.connector}."):
            raise ConnectorError(
                f"Capability {cap_id!r} must be namespaced as '{cap.connector}.<action>'."
            )
    for group in {c.group for c in registry.connectors.values()}:
        if group not in registry.groups:
            raise ConnectorError(f"Connector declares unknown group {group!r}.")

    try:
        registry_hash = compute_registry_hash(target)
    except OSError as exc:
        raise ConnectorError(
            f"Unable to hash connector registry {str(target)!r}: {exc}"
        ) from exc
    LOG.info("Loaded connector registry path=%s sha256=%s", target, registry_hash)

    return registry


__all__ = [
    "ACTION_CLASS_SIDE_EFFECT",
    "AUTO_CLASSES",
    "Capability",
    "Connector",
    "ConnectorError",
    "ConnectorRegistry",
    "Group",
    "HttpSpec",
    "ResultSizeClass",
    "SideEffectClass",
    "compute_registry_hash",
    "load_registry",
]
