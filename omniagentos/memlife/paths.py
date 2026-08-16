"""Single path resolver for memlife store and lessons.

Lane 0 shared prerequisite. Two inconsistent resolvers used to exist:

- ``routes/memlife.memlife_store_root`` — ``OMNIAGENTOS_MEMLIFE_STORE``, default
  ``<repo>/var/memories/memlife``
- ``render.default_memories_root`` — ``OMNIAGENTOS_MEMORIES_DIR``, default
  ``<repo>/var/memories``

They only agreed by coincidence under the empty-env default. Setting
``OMNIAGENTOS_MEMORIES_DIR`` alone made Lane C's write end and read end address
different trees. Every memlife path resolution goes through this module.

Anchored to the package parent (repo root), never ``os.getcwd()`` — same
discipline as ``contracts.default_db_path``.
"""

from __future__ import annotations

import os
from pathlib import Path

MEMLIFE_PROJECT = "memlife"
ENV_STORE = "OMNIAGENTOS_MEMLIFE_STORE"  # mirrors routes/memlife.py (historical)
ENV_MEMORIES = "OMNIAGENTOS_MEMORIES_DIR"  # mirrors render.py ENV_MEMORIES_DIR


def _repo_root() -> Path:
    # omniagentos/memlife/paths.py → omniagentos/memlife → omniagentos → repo
    return Path(__file__).resolve().parents[2]


def memories_root() -> Path:
    """``OMNIAGENTOS_MEMORIES_DIR`` or ``<repo>/var/memories``."""
    override = os.environ.get(ENV_MEMORIES)
    if override:
        return Path(override)
    return _repo_root() / "var" / "memories"


def memlife_store_root() -> Path:
    """``OMNIAGENTOS_MEMLIFE_STORE`` or ``memories_root() / "memlife"``."""
    override = os.environ.get(ENV_STORE)
    if override:
        return Path(override)
    return memories_root() / MEMLIFE_PROJECT
