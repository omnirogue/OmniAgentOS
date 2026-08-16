#!/usr/bin/env python3
"""Generate the checked-in whole-API OpenAPI contract artifact.

Production docs remain disabled (docs_url/openapi_url/redoc_url are None).
This script builds the schema offline via the app object's own ``openapi()``
and writes contracts/openapi.json for anti-drift contract tests and the
release gate.

The schema is derived from ``app`` itself rather than a hand-copied
``get_openapi(...)`` argument list, so every mounted router (and any future
FastAPI generation parameter) is included by construction — a router can
never be silently dropped because this script's copy of the arguments went
stale.

Some endpoints intentionally return dynamic ``dict`` payloads that FastAPI
cannot type (e.g. ``GET /api/team/board`` keys its buckets by employee id),
so the pydantic models that define those payload shapes never enter the
schema through a ``response_model``. ``_SUPPLEMENTARY_MODELS`` carries them
into ``components.schemas`` explicitly so the contract still pins their
shape (anti-drift for the queue-card payload, e.g. ``QueueCard.priority``).
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# Ensure THIS checkout wins the import race. A membership check
# (`if str(_ROOT) not in sys.path`) is not enough: an editable install or a
# stray PYTHONPATH can put a DIFFERENT checkout ahead while _ROOT sits at the
# end of sys.path, and the schema then silently regenerates from the wrong
# tree — routes and fields that exist only here just vanish from the artifact.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import omniagentos  # noqa: E402

_RESOLVED = Path(omniagentos.__file__).resolve()
if _ROOT not in _RESOLVED.parents:
    raise SystemExit(
        f"omniagentos resolves to {_RESOLVED}, not under {_ROOT} — refusing to "
        "generate a contract from a different checkout (PYTHONPATH/editable shadow)"
    )

from omniagentos.api.main import app  # noqa: E402
from omniagentos.team.contracts import QueueCard, TeamQueueBuckets  # noqa: E402

# Payload-shape authorities for endpoints whose responses are dynamic dicts
# (see module docstring). Each model lands in components.schemas under its
# class name; nested refs resolve within the same namespace.
_SUPPLEMENTARY_MODELS: tuple[type[BaseModel], ...] = (
    QueueCard,
    TeamQueueBuckets,
)

_REF_TEMPLATE = "#/components/schemas/{model}"


def _merge_supplementary_schemas(schema: dict[str, Any]) -> None:
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    pending: dict[str, dict[str, Any]] = {}
    for model in _SUPPLEMENTARY_MODELS:
        model_schema = model.model_json_schema(ref_template=_REF_TEMPLATE)
        pending.update(model_schema.pop("$defs", {}))
        pending[model.__name__] = model_schema
    for name, body in sorted(pending.items()):
        existing = components.get(name)
        if existing is not None and existing != body:
            raise SystemExit(
                f"supplementary schema {name!r} collides with a different "
                "route-derived component of the same name — rename one of them"
            )
        components[name] = body


def build_openapi_schema() -> dict[str, Any]:
    """The full contract schema: app-derived routes + supplementary models."""
    # app.openapi() caches its result on the app; deep-copy before augmenting
    # so the running app never serves the supplemented variant.
    schema = copy.deepcopy(app.openapi())
    _merge_supplementary_schemas(schema)
    return schema


def generate_openapi_artifact(repo_root: Path | None = None) -> Path:
    root = repo_root or _ROOT
    schema = build_openapi_schema()
    contract_path = root / "contracts" / "openapi.json"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return contract_path


def main() -> int:
    path = generate_openapi_artifact()
    print(f"Successfully generated {path.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
