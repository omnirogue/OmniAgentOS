"""`docs` is a ceiling, not a match — one production file revokes it.

Written after a 66-file merge containing production Python was classified `docs` because a
single `devtasks/` path matched first. That surface runs `forbidden-paths` only, so the
reachability gate never ran and five unwired public symbols reached main.

A weakening surface must be earned by the WHOLE diff. A strengthening one needs one file.
"""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "gate_runner", Path(__file__).resolve().parents[2] / "scripts" / "gate-runner.py"
)
gate_runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate_runner)

REG = {
    "surface_paths": {
        "security": ["omniagentos/policy/"],
        "verification": ["omniagentos/audit/"],
        "data": ["omniagentos/banking/"],
        "docs": ["devtasks/", "docs/", "HANDOFF/"],
    }
}


def test_one_production_file_revokes_docs():
    """The regression. Forty docs plus one production file is NOT a docs change."""
    files = [f"devtasks/f{i}.md" for i in range(40)] + ["omniagentos/integration/config.py"]
    assert gate_runner._surface(files, REG) == "default"


def test_pure_docs_change_is_docs():
    assert gate_runner._surface(["devtasks/a.md", "docs/b.md", "HANDOFF/c.md"], REG) == "docs"


def test_one_security_file_among_many_docs_is_security():
    """A strengthening surface needs only a single file — a large benign diff must not
    hide a small dangerous one."""
    files = [f"devtasks/f{i}.md" for i in range(40)] + ["omniagentos/policy/shell.py"]
    assert gate_runner._surface(files, REG) == "security"


def test_unmatched_production_paths_get_default_not_docs():
    assert gate_runner._surface(["omniagentos/integration/verdicts.py"], REG) == "default"


def test_empty_diff_is_default_not_docs():
    """No files means nothing was witnessed. That is not a docs change."""
    assert gate_runner._surface([], REG) == "default"
