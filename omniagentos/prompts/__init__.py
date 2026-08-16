"""System-prompt registry: role id in, prompt text out.

The inventory of every agent role lives in ``system-prompts/ROLE-REGISTRY.yaml``;
``system-prompts/README.md`` explains how to add one.  Typical use::

    from omniagentos.prompts import get_prompt

    text = get_prompt("job.implementer")

Every failure raises — see :mod:`omniagentos.prompts.registry`.
"""

from __future__ import annotations

from omniagentos.prompts.registry import (
    KINDS,
    LOCATIONS,
    REGISTRY_RELATIVE_PATH,
    RESOLVABLE_LOCATIONS,
    EmptyPromptError,
    PromptFileMissingError,
    PromptRegistry,
    PromptRegistryError,
    RegistryFileError,
    RoleEntry,
    UnknownRoleError,
    UnresolvablePromptError,
    get_prompt,
    get_role,
    list_roles,
    load_registry,
    repo_root,
)

__all__ = [
    "KINDS",
    "LOCATIONS",
    "REGISTRY_RELATIVE_PATH",
    "RESOLVABLE_LOCATIONS",
    "EmptyPromptError",
    "PromptFileMissingError",
    "PromptRegistry",
    "PromptRegistryError",
    "RegistryFileError",
    "RoleEntry",
    "UnknownRoleError",
    "UnresolvablePromptError",
    "get_prompt",
    "get_role",
    "list_roles",
    "load_registry",
    "repo_root",
]
