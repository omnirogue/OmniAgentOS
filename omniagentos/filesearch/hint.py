"""Agent-facing capability hint for mid-run file search.

The runner prepends :func:`brief_hint` to each brief when
``OMNIAGENTOS_FILESEARCH_HINT=1`` (set by ``scripts/launch-env.sh`` for the
production launch path; default off so tests and ad-hoc invocations are
unaffected). The hint is the bridge between the federated file catalog
(:mod:`omniagentos.filesearch`) and CLI agents working in arbitrary
directories: without it, an agent has no way to know the operator's files
outside its working tree are searchable at all.

The command uses the running interpreter (``sys.executable`` — the product
venv, where ``omniagentos`` is importable) so it works from any working
directory, including non-repo project checkouts.
"""

from __future__ import annotations

import os
import sys

ENV_HINT = "OMNIAGENTOS_FILESEARCH_HINT"  # "1" → runner prepends brief_hint()


def hint_enabled() -> bool:
    """Whether the runner should prepend the file-search hint to briefs."""
    return os.getenv(ENV_HINT, "0") == "1"


def brief_hint() -> str:
    """A short block telling an agent HOW to search the operator's files."""
    return (
        "<file-search>\n"
        "To find the operator's files and docs beyond this working directory "
        "(local disk, iCloud, Google Drive, Dropbox — cloud-only files are never "
        "downloaded), run:\n"
        f'  {sys.executable} -m omniagentos.filesearch "<query>" '
        "--mode hybrid --scope local,icloud,gdrive,dropbox --limit 10\n"
        "Results are ranked paths with excerpts. Read a returned file before "
        "relying on its excerpt.\n"
        "</file-search>"
    )
