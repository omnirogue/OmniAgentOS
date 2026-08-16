"""Contract tests: whole-API OpenAPI anti-drift (S19A / H-34, M-21).

Fails closed when contracts/openapi.json is missing (CI must never auto-seed
the artifact). Diffs live schema generation against the committed contract.
Production interactive docs remain disabled.
"""

from __future__ import annotations

import json
from pathlib import Path

from omniagentos.api.main import app
from scripts.generate_openapi import build_openapi_schema

CONTRACT_PATH = Path("contracts/openapi.json")


def _live_schema() -> dict:
    # The generator is the single schema-building authority: a hand-copied
    # get_openapi(...) call here would silently drift from what the artifact
    # is actually built with (supplementary payload-shape models included).
    return build_openapi_schema()


def test_production_docs_remain_disabled() -> None:
    """Interactive OpenAPI/Swagger/ReDoc endpoints stay off in production app."""
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_openapi_contract_artifact_present() -> None:
    """Fail closed: missing committed artifact is a hard CI failure."""
    assert CONTRACT_PATH.is_file(), (
        f"Missing OpenAPI contract artifact {CONTRACT_PATH}. "
        "Generate with `uv run python scripts/generate_openapi.py` "
        "(or `make openapi`) and commit contracts/openapi.json."
    )
    # Non-empty schema document with required OpenAPI keys.
    existing = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(existing, dict)
    assert existing.get("openapi"), "contract missing openapi version"
    assert existing.get("paths"), "contract missing paths"
    assert existing.get("info"), "contract missing info"


def test_openapi_contract_diff() -> None:
    """Generated whole-API schema must match the committed contract."""
    assert CONTRACT_PATH.is_file(), (
        f"Missing OpenAPI contract artifact {CONTRACT_PATH}. "
        "Generate with `uv run python scripts/generate_openapi.py` and commit it."
    )
    schema = _live_schema()
    existing = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    # Normalize both sides with the same dump settings used by the generator.
    normalized_live = json.loads(json.dumps(schema, indent=2, sort_keys=True))
    normalized_existing = json.loads(json.dumps(existing, indent=2, sort_keys=True))

    if normalized_live != normalized_existing:
        new_path = CONTRACT_PATH.with_suffix(".json.new")
        new_path.write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise AssertionError(
            f"OpenAPI contract drifted! Generated schema does not match {CONTRACT_PATH}. "
            f"Diff against {new_path}, then regenerate with "
            "`uv run python scripts/generate_openapi.py` and commit."
        )
