"""Plugin manifest."""

from __future__ import annotations

# Deliberately stale manifest
MANIFEST: tuple[tuple[str, int], ...] = (
    ("verify", 90),
    ("sync", 85),
    ("ingest", 80),
    ("notify", 70),
    ("digest", 60),
    ("backup", 100),  # Disagree (actual 50)
    ("render", 45),
    ("archive", 40),
    ("audit", 30),
    ("purge", 5),  # Disagree (actual 15)
)


def manifest_ids() -> tuple[str, ...]:
    """Return the list of plugin IDs in the manifest."""
    return tuple(item[0] for item in MANIFEST)
