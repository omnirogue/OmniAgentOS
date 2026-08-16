"""Pure assembly of named prompt layers with per-part content hashes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256

PromptLayers = Mapping[str, str] | Sequence[str]


def _parts(prefix: str, layers: PromptLayers) -> list[tuple[str, str]]:
    items = layers.items() if isinstance(layers, Mapping) else enumerate(layers)
    return [(f"{prefix}:{name}", str(value)) for name, value in items]


def assemble(
    system_layers: PromptLayers, context_layers: PromptLayers
) -> tuple[str, dict[str, str]]:
    """Return rendered prompt text and SHA-256 digests for every named layer."""
    parts = [*_parts("system", system_layers), *_parts("context", context_layers)]
    hashes = {name: sha256(value.encode("utf-8")).hexdigest() for name, value in parts}
    return "\n\n".join(value for _, value in parts if value), hashes
