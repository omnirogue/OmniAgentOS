"""The unified tool catalog: one record shape over two very different sources.

The repo has two disjoint tool inventories and no single view of them:

* the toolplane built-ins (``toolplane/tools.py::CAPABILITY_INVENTORY``), which
  carry three fields — category, risk, requires_scope; and
* every connector capability (``configs/connectors.yaml`` via
  :mod:`omniagentos.connectors`), which carries an ActionClass and an HTTP allowlist.

Exposure filtering (``toolplane/exposure.py``), deferred search (``toolplane/search.py``)
and the resource-aware scheduler (``toolplane/scheduler.py``) all need the SAME facts
about a tool regardless of which inventory it came from. :class:`CatalogEntry` is that
shape, and this module is the only place the two sources are reconciled.

**This module adds no new classifier.** The read/write and side-effect facts for a
connector capability are DERIVED from its ActionClass via the ``resolved_*`` properties
on :class:`~omniagentos.connectors.Capability`; the facts for a built-in are derived from
its existing ``category``. The repo's classifier invariant (``policy/shell.py``,
``sessions/policy_map.py``, ``runner/core.py``) is untouched — this metadata feeds those
gates, it never second-guesses them.

**Unknown means serial, never fast.** :func:`catalog_entry` returns ``None`` for a tool
that is in neither inventory. Callers must treat that as unclassified and fall back to
the safe path (serial execution, no deferral), never as "no constraints".
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from omniagentos.connectors import (
    ConnectorError,
    ConnectorRegistry,
    ResultSizeClass,
    SideEffectClass,
)
from omniagentos.contracts import ActionClass
from omniagentos.toolplane.tools import CAPABILITY_INVENTORY

__all__ = [
    "CatalogEntry",
    "CatalogSource",
    "RiskLevel",
    "build_catalog",
    "catalog_entry",
    "default_catalog",
]

CatalogSource = Literal["builtin", "connector"]
RiskLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """A record representing unified metadata of a tool in the catalog.

    The 'classified' attribute is load-bearing for the scheduler: it is True when
    this entry's read/write and resource classification came from the catalog at all.
    Every entry built here is classified=True; a tool the caller names that is
    absent from the catalog is unknown, and the scheduler must then serialise.
    """

    id: str
    namespace: str
    label: str
    compact_hint: str
    description: str
    source: CatalogSource
    action_class: ActionClass
    read_only: bool
    side_effect_class: SideEffectClass
    resource_keys: tuple[str, ...]
    idempotent: bool
    parallel_safe: bool
    cancellation_group: str
    credential_scope: str
    result_size_class: ResultSizeClass
    risk: RiskLevel
    requires_scope: bool
    input_examples: tuple[str, ...]
    parameter_names: tuple[str, ...]
    callable_now: bool
    classified: bool


CATEGORY_CLASSIFICATION: dict[str, tuple[bool, SideEffectClass]] = {
    "fs_read": (True, SideEffectClass.NONE),
    "fs_write": (False, SideEffectClass.INTERNAL_WRITE),
    "subprocess": (False, SideEffectClass.INTERNAL_WRITE),
    "validation": (True, SideEffectClass.NONE),
    "network": (False, SideEffectClass.EXTERNAL_WRITE),
    "media": (False, SideEffectClass.SANDBOX_WRITE),
}

BUILTIN_ACTION_CLASSES: dict[str, ActionClass] = {
    "fs_read": ActionClass.READ_ONLY,
    "validation": ActionClass.READ_ONLY,
    "fs_write": ActionClass.INTERNAL_REVERSIBLE,
    "subprocess": ActionClass.INTERNAL_REVERSIBLE,
    "network": ActionClass.EXTERNAL_REVERSIBLE,
    "media": ActionClass.EXTERNAL_REVERSIBLE,
}

ACTION_CLASS_RISK: dict[ActionClass, RiskLevel] = {
    ActionClass.READ_ONLY: "low",
    ActionClass.SANDBOXED_CREATION: "medium",
    ActionClass.INTERNAL_REVERSIBLE: "medium",
    ActionClass.EXTERNAL_REVERSIBLE: "high",
    ActionClass.CONSEQUENTIAL: "high",
    ActionClass.IRREVERSIBLE: "high",
}

BUILTIN_METADATA: dict[str, dict[str, Any]] = {
    "read_file": {
        "label": "Read File",
        "compact_hint": "Read a file from disk.",
        "description": "Reads the complete or partial content of a file within the scoped path limit.",
        "resource_keys": ("fs:read",),
        "idempotent": True,
        "parallel_safe": True,
        "cancellation_group": "toolplane-fs",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.LARGE,
        "parameter_names": ("path", "offset", "limit"),
        "input_examples": (),
    },
    "list_files": {
        "label": "List Files",
        "compact_hint": "List directory contents.",
        "description": "Lists files and subdirectories located within the specified target path.",
        "resource_keys": ("fs:read",),
        "idempotent": True,
        "parallel_safe": True,
        "cancellation_group": "toolplane-fs",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.LARGE,
        "parameter_names": ("path", "recursive"),
        "input_examples": (),
    },
    "search_files": {
        "label": "Search Files",
        "compact_hint": "Search file contents.",
        "description": "Searches for occurrences of a given pattern inside files in the target folder.",
        "resource_keys": ("fs:read",),
        "idempotent": True,
        "parallel_safe": True,
        "cancellation_group": "toolplane-fs",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.LARGE,
        "parameter_names": ("path", "pattern"),
        "input_examples": (),
    },
    "hash_file": {
        "label": "Hash File",
        "compact_hint": "Compute file checksum.",
        "description": "Computes the SHA-256 cryptographic checksum of a file.",
        "resource_keys": ("fs:read",),
        "idempotent": True,
        "parallel_safe": True,
        "cancellation_group": "toolplane-fs",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.SMALL,
        "parameter_names": ("path",),
        "input_examples": (),
    },
    "write_file": {
        "label": "Write File",
        "compact_hint": "Write content to file.",
        "description": "Writes the given content into a target file path, creating parent dirs if missing.",
        "resource_keys": ("fs:write",),
        "idempotent": False,
        "parallel_safe": False,
        "cancellation_group": "toolplane-fs",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.SMALL,
        "parameter_names": ("path", "content"),
        "input_examples": (),
    },
    "edit_file": {
        "label": "Edit File",
        "compact_hint": "Perform search and replace.",
        "description": "Replaces exactly one occurrence of a target string within a file.",
        "resource_keys": ("fs:write",),
        "idempotent": False,
        "parallel_safe": False,
        "cancellation_group": "toolplane-fs",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.SMALL,
        "parameter_names": ("path", "old_str", "new_str"),
        "input_examples": (),
    },
    "make_dir": {
        "label": "Make Directory",
        "compact_hint": "Create directory path.",
        "description": "Creates a directory tree recursively at the target path.",
        "resource_keys": ("fs:write",),
        "idempotent": True,
        "parallel_safe": False,
        "cancellation_group": "toolplane-fs",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.SMALL,
        "parameter_names": ("path",),
        "input_examples": (),
    },
    "run_test": {
        "label": "Run Test",
        "compact_hint": "Run subprocess command.",
        "description": "Runs a subprocess test command with custom arguments under a timed sandbox.",
        "resource_keys": ("proc:subprocess",),
        "idempotent": False,
        "parallel_safe": False,
        "cancellation_group": "toolplane-proc",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.LARGE,
        "parameter_names": ("argv", "cwd"),
        "input_examples": (),
    },
    "image_info": {
        "label": "Image Info",
        "compact_hint": "Inspect image metadata.",
        "description": "Retrieves dimensions, format, and mode metadata of an image file.",
        "resource_keys": ("fs:read",),
        "idempotent": True,
        "parallel_safe": True,
        "cancellation_group": "toolplane-fs",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.SMALL,
        "parameter_names": ("path",),
        "input_examples": (),
    },
    "validate_artifact_output": {
        "label": "Validate Artifact Output",
        "compact_hint": "Validate artifact format.",
        "description": "Validates structured output formats and returns matched artifact targets.",
        "resource_keys": ("fs:read",),
        "idempotent": True,
        "parallel_safe": True,
        "cancellation_group": "toolplane-fs",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.SMALL,
        "parameter_names": ("outputs",),
        "input_examples": (),
    },
    "connector_invoke": {
        "label": "Connector Invoke",
        "compact_hint": "Invoke connector API.",
        "description": "Calls external third-party connector capabilities within validated grant list.",
        "resource_keys": ("net:connector",),
        "idempotent": False,
        "parallel_safe": False,
        "cancellation_group": "toolplane-net",
        "credential_scope": "connector-broker",
        "result_size_class": ResultSizeClass.SMALL,
        "parameter_names": ("cap_id", "granted"),
        "input_examples": (),
    },
    "globex_generate_image": {
        "label": "Generate Image",
        "compact_hint": "Generate image artifact.",
        "description": "Generates an image via the Globex Studio API and saves the file locally.",
        "resource_keys": ("net:globex",),
        "idempotent": False,
        "parallel_safe": False,
        "cancellation_group": "toolplane-net",
        "credential_scope": "globex-studio",
        "result_size_class": ResultSizeClass.SMALL,
        "parameter_names": ("payload",),
        "input_examples": (),
    },
    "globex_generate_video": {
        "label": "Generate Video",
        "compact_hint": "Generate video artifact.",
        "description": "Generates a video via the Globex Studio API and saves the file locally.",
        "resource_keys": ("net:globex",),
        "idempotent": False,
        "parallel_safe": False,
        "cancellation_group": "toolplane-net",
        "credential_scope": "globex-studio",
        "result_size_class": ResultSizeClass.SMALL,
        "parameter_names": ("payload",),
        "input_examples": (),
    },
    "voice_tts": {
        "label": "Synthesize Voice",
        "compact_hint": "Generate a scoped audio artifact.",
        "description": "Synthesizes speech and saves a verified audio file within a granted root.",
        "resource_keys": ("net:voice",),
        "idempotent": False,
        "parallel_safe": False,
        "cancellation_group": "toolplane-net",
        "credential_scope": "voice-provider",
        "result_size_class": ResultSizeClass.SMALL,
        "parameter_names": ("text", "voice_id", "provider", "output_path"),
        "input_examples": (),
    },
    "repomap": {
        "label": "Query Repo Map",
        "compact_hint": "Get repository structure and symbols.",
        "description": "Queries the repository code map to understand structure and callable symbols.",
        "resource_keys": ("fs:read",),
        "idempotent": True,
        "parallel_safe": True,
        "cancellation_group": "toolplane-fs",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.LARGE,
        "parameter_names": ("repo_dir", "focus_files", "focus_terms", "max_tokens"),
        "input_examples": ("repo_dir=/Users/me/project",),
    },
    "semantic_search": {
        "label": "Semantic File Search",
        "compact_hint": "Search files by meaning.",
        "description": "Semantic file search via pgvector embeddings. Returns chunks ranked by cosine similarity.",
        "resource_keys": ("fs:read", "db:knowledge"),
        "idempotent": True,
        "parallel_safe": True,
        "cancellation_group": "toolplane-fs",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.LARGE,
        "parameter_names": ("query", "root", "category", "limit"),
        "input_examples": ("query=security vulnerability", "root=/codebase", "limit=10"),
    },
    "knowledge_recall": {
        "label": "Recall Knowledge",
        "compact_hint": "Recall learned facts.",
        "description": "Hybrid fact recall from the knowledge store using embeddings and full-text search.",
        "resource_keys": ("db:knowledge",),
        "idempotent": True,
        "parallel_safe": True,
        "cancellation_group": "toolplane-db",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.LARGE,
        "parameter_names": ("query", "scope", "top_k", "sources"),
        "input_examples": ("query=how do I deploy?", "top_k=5"),
    },
    "memory_search": {
        "label": "Search Memory",
        "compact_hint": "Search metacog lessons and procedures.",
        "description": "Searches metacognitive memory records (lessons, procedures, warnings).",
        "resource_keys": ("db:metacog",),
        "idempotent": True,
        "parallel_safe": True,
        "cancellation_group": "toolplane-db",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.LARGE,
        "parameter_names": ("query", "limit"),
        "input_examples": ("query=learned lessons", "limit=10"),
    },
    "vault_search": {
        "label": "Search Vault",
        "compact_hint": "Search vault notes and MOCs.",
        "description": "Keyword search over vault Map-of-Content summaries for broad 'what does the vault know' queries.",
        "resource_keys": ("fs:read",),
        "idempotent": True,
        "parallel_safe": True,
        "cancellation_group": "toolplane-fs",
        "credential_scope": "",
        "result_size_class": ResultSizeClass.LARGE,
        "parameter_names": ("query", "vault_dir", "limit"),
        "input_examples": ("query=design patterns", "vault_dir=/Users/me/vault", "limit=5"),
    },
}


def build_catalog(registry: ConnectorRegistry | None = None) -> dict[str, CatalogEntry]:
    """Every known tool, keyed by id: built-ins plus every connector capability.

    Fails safe if the registry is unreadable by catching ConnectorError and
    falling back to returning built-ins alone.
    """
    out: dict[str, CatalogEntry] = {}

    # 1. Build the built-ins
    for tool_id, inv in CAPABILITY_INVENTORY.items():
        meta = BUILTIN_METADATA[tool_id]
        category = inv["category"]
        read_only, side_effect = CATEGORY_CLASSIFICATION[category]
        action_class = BUILTIN_ACTION_CLASSES[category]

        entry = CatalogEntry(
            id=tool_id,
            namespace="toolplane",
            label=meta["label"],
            compact_hint=meta["compact_hint"],
            description=meta["description"],
            source="builtin",
            action_class=action_class,
            read_only=read_only,
            side_effect_class=side_effect,
            resource_keys=meta["resource_keys"],
            idempotent=meta["idempotent"],
            parallel_safe=meta["parallel_safe"],
            cancellation_group=meta["cancellation_group"],
            credential_scope=meta["credential_scope"],
            result_size_class=meta["result_size_class"],
            risk=inv["risk"],
            requires_scope=inv["requires_scope"],
            input_examples=meta["input_examples"],
            parameter_names=meta["parameter_names"],
            callable_now=True,
            classified=True,
        )
        out[tool_id] = entry

    # 2. Add the connector capabilities
    if registry is None:
        try:
            from omniagentos.connectors import load_registry

            registry = load_registry()
        except ConnectorError:
            # Fall back to built-ins alone; a broken registry must not crash the catalog.
            registry = None

    if registry is not None:
        for cap_id, cap in registry.capabilities.items():
            # Built-in ids (bare words) and connector ids (<connector>.<action>)
            # cannot collide under normal conditions, but if they ever do,
            # the connector entry wins deterministically.
            connector_label = registry.connectors[cap.connector].label
            risk = ACTION_CLASS_RISK.get(cap.action_class, "high")

            entry = CatalogEntry(
                id=cap_id,
                namespace=cap.resolved_namespace,
                label=cap.label,
                compact_hint=cap.resolved_compact_hint,
                description=f"{cap.label} ({connector_label}).",
                source="connector",
                action_class=cap.action_class,
                read_only=cap.resolved_read_only,
                side_effect_class=cap.resolved_side_effect_class,
                resource_keys=cap.resolved_resource_keys,
                idempotent=cap.resolved_idempotent,
                parallel_safe=cap.resolved_parallel_safe,
                cancellation_group=cap.resolved_cancellation_group,
                credential_scope=cap.resolved_credential_scope,
                result_size_class=cap.resolved_result_size_class,
                risk=risk,
                requires_scope=True,
                input_examples=cap.input_examples,
                parameter_names=("method", "path", "query", "body"),
                callable_now=cap.callable_now,
                classified=True,
            )
            out[cap_id] = entry

    return out


@functools.lru_cache(maxsize=1)
def default_catalog() -> Mapping[str, CatalogEntry]:
    """Process-wide cached catalog over the default registry.

    Returns a READ-ONLY view. The cached object is shared by every caller in the
    process, so handing back the mutable dict would let one caller's edit silently
    redefine another caller's exposure decision. Callers that need to modify a
    catalog build their own with :func:`build_catalog`.
    """
    return MappingProxyType(build_catalog())


def catalog_entry(
    tool_id: str, catalog: Mapping[str, CatalogEntry] | None = None
) -> CatalogEntry | None:
    """Look up one entry. Returns None for an unknown tool — callers must fail SAFE."""
    cat = catalog if catalog is not None else default_catalog()
    return cat.get(tool_id)
