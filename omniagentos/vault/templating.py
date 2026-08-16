"""Jinja2 loader for the note-body templates under vault/templates/ (owned by
p05 per contracts/vault-frontmatter.md layout: "templates/ — p05-owned Jinja/py
templates for note bodies").

Templates render ONLY the body (frontmatter is handled separately by
omniagentos.vault.frontmatter so the exact 8-field YAML contract stays in one
place, independent of body formatting).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

# omniagentos/vault/templating.py -> omniagentos/vault -> omniagentos -> <repo root>
# vault/templates/ sits alongside omniagentos/ at the repo root (ADR-003: the
# vault lives inside this repository, not a separate one, so this relative
# layout is a structural invariant, not a guess).
_REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = _REPO_ROOT / "vault" / "templates"


@lru_cache(maxsize=1)
def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
        autoescape=False,  # plain markdown output, not HTML
    )


def render_template(name: str, **context: object) -> str:
    """Render `name` (a filename under vault/templates/) with `context`."""
    template = _environment().get_template(name)
    return template.render(**context)
