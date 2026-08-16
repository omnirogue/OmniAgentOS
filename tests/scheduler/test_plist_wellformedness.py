"""Every git-tracked ``.plist`` / ``.plist.template`` file must parse cleanly.

Regression for sha256:e63c660be4da172c0c3cd52522d8310620393dd — a bare ``--``
inside an XML comment is illegal XML. Every strict XML parser (expat, and
therefore both ``plistlib`` and ``xml.etree.ElementTree``, which back this
repo's own :func:`parse_plist`) rejects it, while ``plutil -lint`` and
launchd's own CFPropertyList reader silently accept it — so the malformation
was invisible to the only two checks the repo ran, and it propagated (3
carriers found across 2 days, on 2 separate `.plist`/`.plist.template`
families) before this test existed.

This enumerates every tracked plist/template MECHANICALLY via `git ls-files`
rather than hand-listing the files known to be broken at write time — a
fourth carrier landing next month must fail here, not silently drop out of
`scan_installed_plists`/`list_system_jobs` months later.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omniagentos.scheduler.system_jobs import parse_plist

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tracked_plist_paths() -> list[Path]:
    """Every git-tracked *.plist / *.plist.template file, repo-root-relative.

    Falls back to an empty list (never raises) if this tree is somehow not a
    git checkout — pytest will then just collect zero parametrized cases
    rather than erroring the whole module out at collection time.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(
        REPO_ROOT / line
        for line in result.stdout.splitlines()
        if line.endswith(".plist") or line.endswith(".plist.template")
    )


TRACKED_PLISTS = _tracked_plist_paths()


def test_at_least_the_known_plist_families_are_enumerated() -> None:
    """Sanity check on the enumeration itself: if `git ls-files` returns
    nothing (e.g. run outside a git checkout) the parametrized test below
    would silently collect zero cases and this whole file would pass by
    doing nothing — the exact favourable-absence shape this file exists to
    prevent. Assert a floor instead of trusting an empty pass.

    That guard is itself moot in a checkout that is not a git repository at
    all (no ``.git`` at the root, as ships in this public-release export):
    `git ls-files` cannot enumerate anything here regardless of how many
    plists are actually present, so an empty result names a real absence of
    version control, not a broken or favourably-quiet enumeration. Skip with
    that explicit reason rather than fail on a floor no git command run here
    could ever clear.
    """
    if not (REPO_ROOT / ".git").exists():
        pytest.skip(
            f"{REPO_ROOT} is not a git checkout (no .git) -- `git ls-files` "
            "cannot enumerate tracked plists here"
        )
    assert len(TRACKED_PLISTS) >= 50, (
        f"expected >=50 tracked .plist/.plist.template files, found "
        f"{len(TRACKED_PLISTS)} — the git ls-files enumeration may be broken"
    )


@pytest.mark.parametrize(
    "path", TRACKED_PLISTS, ids=[str(p.relative_to(REPO_ROOT)) for p in TRACKED_PLISTS]
)
def test_tracked_plist_parses_cleanly(path: Path) -> None:
    """Every tracked plist/template must parse via the repo's own
    `parse_plist` — the same function `scan_installed_plists` and
    `list_system_jobs` rely on for live status. A file that fails here is
    exactly the failure mode that made a loaded job read as not installed."""
    parse_plist(path)  # raises ValueError on failure — that IS the assertion
