"""Keep the fixture corpus out of the repo's own pytest collection.

Every file under ``tests/benchmarks/fixtures/`` is *input data* for the A/B
benchmark: seed workspaces contain deliberately buggy modules and deliberately
FAILING checks (that is the point of a "bug fix with a failing test" fixture).
None of it may ever be collected by the repo suite.

Two guards, deliberately redundant:
  * this ``collect_ignore_glob`` — pytest skips the whole subtree;
  * the ``check_*.py`` naming convention — outside pytest's default
    ``python_files`` patterns, so even a stray ``--ignore`` change cannot
    resurrect them. Acceptance runs pass those files to pytest *explicitly*
    (an explicit path argument is collected regardless of the filename
    pattern), which is the only way they ever execute.
"""

from __future__ import annotations

collect_ignore_glob = ["*"]
