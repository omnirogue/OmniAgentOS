"""Staleness detection for living architecture docs (§8).

A stamp — `git HEAD, max migration number, route count` — is embedded as an HTML
comment at the top of `ARCHI.md` each time it's (re)generated. `is_stale()` recomputes
the live values and compares; a mismatch means the doc has drifted from the code and
should surface as a `doc_stale` finding (taxonomy.FailureClass.DOC_STALE) feeding an
L1 docs-refresh improvement (§8, auto-appliable once the operator enables auto for L1 — this
module only detects, it never writes).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

from omniagentos.archdocs.generate import (
    git_head,
    scan_migrations,
    scan_routes,
)
from omniagentos.contracts import utc_now_iso

_STAMP_RE = re.compile(
    r"<!-- archdocs:stamp git_head=(?P<git_head>\S+) "
    r"max_migration=(?P<max_migration>\d+|unknown) "
    r"route_count=(?P<route_count>\d+|unknown) "
    r"generated_at=(?P<generated_at>\S+) -->"
)

_OWNED_REFRESH_PATHS = frozenset(
    {
        "ARCHI.md",
        "ARCHI.json",
        "docs/architecture/system-map.md",
        "docs/architecture/system-map.mmd",
    }
)


class Stamp(NamedTuple):
    git_head: str
    max_migration: int | None
    route_count: int | None
    generated_at: str


def _stamp_int_field(raw: str) -> int | None:
    """Parse a stamp numeric field; ``unknown`` means unmeasurable (not zero)."""
    if raw == "unknown":
        return None
    return int(raw)


def _format_stamp_int(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def compute_stamp(repo_root: str | Path) -> Stamp:
    """Compute the CURRENT live values (git HEAD, max migration number, route count).

    When a scan source is absent (``None``), the corresponding numeric field is
    ``None`` (unmeasurable) — never the favourable measured ``0``. Bare
    truthiness (``if migrations else 0`` / ``routes or []``) is prohibited: it
    collapses absent and measured-empty into the same stamp value that
    ``emit_archi_json`` publishes.
    """
    repo_root = Path(repo_root)
    migrations = scan_migrations(repo_root)
    # Explicit is None — bare truthiness collapses absent None and measured
    # empty [] both to max_migration 0 (non-result as measured zero).
    if migrations is None:
        max_migration: int | None = None
    else:
        max_migration = migrations[-1][0] if migrations else 0
    routes = scan_routes(repo_root)
    # Explicit is None — bare ``routes or []`` collapses absent to count 0.
    # UNPARSEABLE sentinels are inventory signals, not measured routes.
    # UNPARSEABLE-only inventory must stamp None (unmeasurable), never the
    # favourable measured 0 that a genuinely empty routes directory gets.
    if routes is None:
        route_count: int | None = None
    else:
        measurable = sum(1 for method, _, _ in routes if method != "UNPARSEABLE")
        has_unparseable = any(method == "UNPARSEABLE" for method, _, _ in routes)
        if has_unparseable and measurable == 0:
            route_count = None
        else:
            route_count = measurable
    return Stamp(
        git_head=git_head(repo_root),
        max_migration=max_migration,
        route_count=route_count,
        generated_at=utc_now_iso(),
    )


def render_stamp_comment(stamp: Stamp) -> str:
    """Render the stamp as a single HTML comment line."""
    return (
        f"<!-- archdocs:stamp git_head={stamp.git_head} "
        f"max_migration={_format_stamp_int(stamp.max_migration)} "
        f"route_count={_format_stamp_int(stamp.route_count)} "
        f"generated_at={stamp.generated_at} -->"
    )


def parse_stamp_comment(content: str) -> Stamp | None:
    """Extract the embedded stamp from doc content, or None if absent/malformed."""
    match = _STAMP_RE.search(content)
    if not match:
        return None
    return Stamp(
        git_head=match.group("git_head"),
        max_migration=_stamp_int_field(match.group("max_migration")),
        route_count=_stamp_int_field(match.group("route_count")),
        generated_at=match.group("generated_at"),
    )


def splice_stamp_comment(content: str, stamp: Stamp) -> str:
    """Return ``content`` with its stamp comment replaced, or prepended if absent.

    Single shared implementation of the substitution grammar previously
    duplicated between `stamp_archi()` and `stamp_archi_and_json()`. Also used
    by `generate.py`'s in-memory stamp refresh (`render_archi()`/
    `emit_archi_json()`) so every writer of the embedded ARCHI.md stamp
    comment goes through exactly one splice implementation.
    """
    comment = render_stamp_comment(stamp)
    if _STAMP_RE.search(content):
        return _STAMP_RE.sub(comment, content, count=1)
    return f"{comment}\n{content}" if content else f"{comment}\n"


def stamp_archi(repo_root: str | Path, archi_path: str | Path | None = None) -> str:
    """Compute a fresh stamp and (re)write it as the first line of `ARCHI.md`,
    replacing any prior stamp comment. Returns the new file content."""
    repo_root = Path(repo_root)
    archi_path = Path(archi_path) if archi_path is not None else repo_root / "ARCHI.md"
    stamp = compute_stamp(repo_root)

    existing = archi_path.read_text(encoding="utf-8") if archi_path.exists() else ""
    new_content = splice_stamp_comment(existing, stamp)

    archi_path.write_text(new_content, encoding="utf-8")
    return new_content


def stamp_archi_and_json(
    repo_root: str | Path,
    archi_path: str | Path | None = None,
    archi_json_path: str | Path | None = None,
) -> Stamp:
    """Compute a fresh stamp and atomically update BOTH ARCHI.md and ARCHI.json.

    Updates the stamp comment as the first line of ARCHI.md and the "stamp" object
    in ARCHI.json, keeping ARCHI.json's existing key order and formatting
    (json.dumps with indent=2, sort_keys=True, trailing newline).

    This ensures the two oracles stay synchronized within a single commit.
    Returns the computed Stamp.
    """
    repo_root = Path(repo_root)
    archi_path = Path(archi_path) if archi_path is not None else repo_root / "ARCHI.md"
    archi_json_path = (
        Path(archi_json_path) if archi_json_path is not None else repo_root / "ARCHI.json"
    )

    stamp = compute_stamp(repo_root)

    # Update ARCHI.md
    existing = archi_path.read_text(encoding="utf-8") if archi_path.exists() else ""
    new_md_content = splice_stamp_comment(existing, stamp)
    archi_path.write_text(new_md_content, encoding="utf-8")

    # Update ARCHI.json
    if archi_json_path.exists():
        archi_json_data = json.loads(archi_json_path.read_text(encoding="utf-8"))
        # Update the stamp object with the new values
        archi_json_data["stamp"] = {
            "generated_at": stamp.generated_at,
            "git_head": stamp.git_head,
            "max_migration": stamp.max_migration,
            "route_count": stamp.route_count,
        }
        # Write with the exact formatting: indent=2, sort_keys=True, trailing newline
        json_content = json.dumps(archi_json_data, indent=2, sort_keys=True)
        archi_json_path.write_text(f"{json_content}\n", encoding="utf-8")

    return stamp


#: Repo surfaces the generated map DESCRIBES. A successor commit touching any
#: of these can change the route table, the migrations block, the diagram
#: node/edge tables, or the architecture prose — so it must invalidate the
#: stamp. Everything else (tests, dashboard, loops, non-route product code) is
#: map-neutral: the stamped quantities are still compared LIVE above this
#: check, so numeric drift is caught regardless of path class.
_ARCH_SURFACE_PREFIXES = (
    "omniagentos/api/routes/",
    "omniagentos/db/migrations/",
    "omniagentos/archdocs/",
    "docs/architecture/",
    # generate.py parses the launcher for default ports and env flags and
    # renders them into the map (launcher_default_ports, the env-flag block) —
    # a launcher-only commit is NOT map-neutral (grok second-lens finding).
    "scripts/launch-omniagentos.sh",
)

#: A new or deleted top-level package changes the packages inventory block.
_PACKAGE_INIT_RE = re.compile(r"^omniagentos/[^/]+/__init__\.py$")

#: Fail-closed bound on the successor walk: a chain longer than this is not
#: examined commit-by-commit — it reads stale.
_CHAIN_WALK_CAP = 200


def _chain_is_map_neutral(repo_root: Path, stamped_head: str, head_ref: str = "HEAD") -> bool:
    """Return whether every commit in ``stamped_head..head_ref`` leaves the map true.

    The one-direct-successor rule below made the stamp stale the moment ANY
    second commit landed — with continuous landings that reddened main's stamp
    gate after nearly every merge (3× on 2026-08-15 alone), while the daily
    ``archi-morning`` refresh could never catch up. The honest question is not
    "how many commits passed" but "did any of them change what the map
    describes". This walk accepts an arbitrary chain when:

    * the FIRST commit may be the generator's own oracle refresh (single
      parent == the stamped source, changed paths a non-empty subset of the
      oracle set including ``ARCHI.md``) — the same shape
      ``_is_owned_refresh_successor`` proves;
    * every other commit has exactly one parent and touches NO oracle path,
      NO architecture surface (``_ARCH_SURFACE_PREFIXES``), and neither adds
      nor deletes a top-level package ``__init__.py``;
    * the checked-out oracles are byte-identical to ``head_ref``'s (a
      worktree tamper never inherits freshness).

    Fail closed: merge commits and root commits in the chain, a chain longer
    than ``_CHAIN_WALK_CAP``, renames (disabled, so they surface as add+delete
    pairs), and every git/decoding failure all return ``False``. A later
    oracle-touching commit — including a hand edit of ``ARCHI.md`` — returns
    ``False`` exactly as the one-successor rule did.
    """

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    try:
        resolved_stamp = _git("rev-parse", "--verify", f"{stamped_head}^{{commit}}")
        if resolved_stamp.returncode != 0:
            return False
        stamp_commit = resolved_stamp.stdout.strip()

        chain = _git("rev-list", "--reverse", f"{stamp_commit}..{head_ref}")
        if chain.returncode != 0:
            return False
        commits = chain.stdout.split()
        if not commits or len(commits) > _CHAIN_WALK_CAP:
            return False

        # Explicit linearity invariant (gemini review hardening): every chain
        # commit's sole parent must be the previous chain member, anchored at
        # the stamped source — auditable directly rather than implied by
        # rev-list algebra plus the merge/root rejections below.
        expected_parent = stamp_commit
        for index, commit in enumerate(commits):
            ancestry = _git("rev-list", "--parents", "-n", "1", commit)
            if ancestry.returncode != 0:
                return False
            fields = ancestry.stdout.strip().split()
            if len(fields) != 2:            # merge or root commit: fail closed
                return False
            parent = fields[1]
            if parent != expected_parent:
                return False
            expected_parent = commit

            changed = _git(
                "diff-tree",
                "--no-commit-id",
                "--name-status",
                "--no-renames",
                "-r",
                commit,
            )
            if changed.returncode != 0:
                return False
            entries: list[tuple[str, str]] = []
            for line in changed.stdout.splitlines():
                if not line.strip():
                    continue
                status, _, path = line.partition("\t")
                if not status or not path:
                    return False
                entries.append((status, path))

            paths = {path for _, path in entries}
            touches_oracle = bool(paths & _OWNED_REFRESH_PATHS)
            if touches_oracle:
                # Only the chain's first commit may touch the oracles, and only
                # in the generator's own shape: direct successor of the stamped
                # source, oracle-only, ARCHI.md included.
                if (
                    index != 0
                    or parent != stamp_commit
                    or "ARCHI.md" not in paths
                    or not paths.issubset(_OWNED_REFRESH_PATHS)
                ):
                    return False
            else:
                if any(
                    path.startswith(_ARCH_SURFACE_PREFIXES) for path in paths
                ):
                    return False
                if any(
                    status[:1] in ("A", "D") and _PACKAGE_INIT_RE.match(path)
                    for status, path in entries
                ):
                    return False

        clean_owned = _git("diff", "--quiet", head_ref, "--", *_OWNED_REFRESH_PATHS)
        return clean_owned.returncode == 0
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError):
        return False


def _is_owned_refresh_successor(repo_root: Path, stamped_head: str, head_ref: str = "HEAD") -> bool:
    """Return whether ``head_ref`` is one direct, oracle-only successor of ``stamped_head``.

    ``archi-morning`` necessarily stamps the source commit and then commits the
    generated oracles.  That one commit is fresh when it has exactly one parent,
    the stamp resolves unambiguously to that parent, and its complete changed
    path set is a non-empty subset of the generator-owned oracle set.

    Every git/provenance failure returns ``False``.  In particular, merge
    commits, a second commit, mixed code/doc commits, and dirty owned files do
    not receive the exception.
    """

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    def _git_bytes(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            timeout=5,
            check=False,
        )

    try:
        ancestry = _git("rev-list", "--parents", "-n", "1", head_ref)
        if ancestry.returncode != 0:
            return False
        fields = ancestry.stdout.strip().split()
        if len(fields) != 2:
            return False
        _, parent_head = fields

        resolved_stamp = _git("rev-parse", "--verify", f"{stamped_head}^{{commit}}")
        if resolved_stamp.returncode != 0 or resolved_stamp.stdout.strip() != parent_head:
            return False

        changed = _git_bytes(
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            parent_head,
            head_ref,
        )
        if changed.returncode != 0:
            return False
        paths = {raw.decode("utf-8") for raw in changed.stdout.split(b"\0") if raw}
        if "ARCHI.md" not in paths or not paths.issubset(_OWNED_REFRESH_PATHS):
            return False

        # A post-commit edit to an owned oracle invalidates the proof even when
        # the embedded stamp has been tampered back to the accepted parent.
        clean_owned = _git("diff", "--quiet", head_ref, "--", *_OWNED_REFRESH_PATHS)
        return clean_owned.returncode == 0
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError):
        return False


def _is_trial_merge_of_fresh_mainline(repo_root: Path, stamped_head: str) -> bool:
    """Return whether HEAD is CI's two-parent trial merge of a fresh mainline.

    CI grades every candidate from a trial-merge checkout (``refs/pull/N/merge``),
    whose HEAD is a synthetic merge commit no stamp can ever name — the head
    equality above is structurally unsatisfiable there, while the merge-gate's
    oracle-path rule already refuses any candidate that touches the oracles.
    The provenance question that remains answerable is therefore: did the
    oracles in this tree come UNCHANGED from a mainline commit the stamp
    accepts?  Fresh when HEAD has exactly two parents, the tree's complete
    oracle set is byte-identical to the first (mainline) parent's, and that
    parent is either the stamped source itself or its one owned refresh
    (evaluated by the same ``_is_owned_refresh_successor`` proof).

    **CI-context only** (review F3): parent ORDER is the only thing separating
    CI's trial merge (parent1 = mainline) from an operator's local
    ``pull.rebase=false`` merge (parent1 = the feature branch) — git alone
    cannot tell them apart, and in the local shape this form would launder a
    branch-stamped, code-drifted tree as fresh.  The form therefore applies
    only when ``GITHUB_ACTIONS`` is ``true`` (set by the runner for every job;
    absent in local shells and launchd daemons).  Outside CI every merge
    commit keeps the strict pre-existing behavior: stale.

    Scope honestly stated (review F1/F2): a branch that regenerates the
    oracles ITSELF reads stale here — intended, because the merge-gate's
    oracle-path rule refuses such candidates and this estate regenerates on
    main only; and freshness in CI context attests oracle PROVENANCE plus the
    numeric drift gates (max migration, route count, unparseable routes — all
    run BEFORE any head comparison, against the MERGED tree), not full
    inventory drift (launchd/package blocks), whose detection remains owned by
    main's own runs and the daily refresh.  Every git failure returns
    ``False``; single-parent commits and octopus merges never qualify.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return False

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    try:
        ancestry = _git("rev-list", "--parents", "-n", "1", "HEAD")
        if ancestry.returncode != 0:
            return False
        fields = ancestry.stdout.strip().split()
        if len(fields) != 3:
            return False
        _, mainline_parent, _branch_parent = fields

        # The merge result's oracles must be exactly the mainline parent's —
        # a branch-side oracle edit (which the merge-gate refuses anyway)
        # never inherits mainline freshness.
        oracle_delta = _git(
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            mainline_parent,
            "HEAD",
            "--",
            *_OWNED_REFRESH_PATHS,
        )
        if oracle_delta.returncode != 0 or oracle_delta.stdout.strip():
            return False

        # Worktree tamper check: the checked-out oracles must match the tree.
        clean_owned = _git("diff", "--quiet", "HEAD", "--", *_OWNED_REFRESH_PATHS)
        if clean_owned.returncode != 0:
            return False

        resolved_stamp = _git("rev-parse", "--verify", f"{stamped_head}^{{commit}}")
        if resolved_stamp.returncode != 0:
            return False
        if resolved_stamp.stdout.strip() == mainline_parent:
            return True
        return _is_owned_refresh_successor(
            repo_root, stamped_head, head_ref=mainline_parent
        ) or _chain_is_map_neutral(repo_root, stamped_head, head_ref=mainline_parent)
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError):
        return False


def is_stamp_stale(
    repo_root: str | Path,
    stored: Stamp | None,
    archi_path: str | Path | None = None,
) -> bool:
    """Core staleness comparison, operating on an already-resolved stamp.

    This is the single implementation `is_stale()` uses for its disk-read
    stamp; it is also exported so a caller holding a stamp that has not (yet)
    been flushed to disk — `generate.py`'s in-memory ARCHI.md/ARCHI.json
    regeneration, which recomputes a stale stamp before writing rather than
    carrying it forward unchanged — can ask the exact same question without
    re-reading `archi_path` (which, mid-regeneration, may still hold the
    pre-regeneration bytes) and without reimplementing the comparison.

    Stale (True) when: `stored` is ``None``; git HEAD differs except for one
    direct, generator-owned archdocs refresh commit; the max migration number
    differs; the route count differs; or git HEAD could not be measured
    (sentinel ``unknown`` on either side — unmeasurable is never "fresh").
    Never raises — an absent stamp counts as stale (nothing to trust).
    """
    repo_root = Path(repo_root)
    archi_path = Path(archi_path) if archi_path is not None else repo_root / "ARCHI.md"

    if stored is None:
        return True

    live = compute_stamp(repo_root)
    # Fail closed: "unknown" means the measurement failed. Matching two
    # failures is not evidence the doc is current (same class as empty-denom
    # rates scored as healthy).
    if stored.git_head == "unknown" or live.git_head == "unknown":
        return True
    # Fail closed: unmeasurable stamp numerics (absent/unreadable inventory →
    # None, or stamp field ``unknown``) are not a measured zero. Matching two
    # unknowns (``None == None``) must never report fresh — that is the
    # unmeasurable-as-healthy counterfeit. This single guard also covers live
    # scan returning None (compute_stamp already maps that to None numerics);
    # a second rescan of scan_* is redundant and would mask a disabled guard.
    if (
        stored.max_migration is None
        or live.max_migration is None
        or stored.route_count is None
        or live.route_count is None
    ):
        return True
    # Fail closed: UNPARSEABLE route modules mean inventory is incomplete.
    # UNPARSEABLE-only stamps route_count=None (already fail-closed above).
    # Mixed inventory still has a numeric count of measurable routes, so this
    # gate is required: without it, a partial count can match and look fresh.
    routes = scan_routes(repo_root)
    if routes is not None and any(method == "UNPARSEABLE" for method, _, _ in routes):
        return True
    if stored.max_migration != live.max_migration or stored.route_count != live.route_count:
        return True
    if stored.git_head == live.git_head:
        return False
    try:
        if archi_path.resolve() != (repo_root / "ARCHI.md").resolve():
            return True
    except OSError:
        return True
    return not (
        _is_owned_refresh_successor(repo_root, stored.git_head)
        or _chain_is_map_neutral(repo_root, stored.git_head)
        or _is_trial_merge_of_fresh_mainline(repo_root, stored.git_head)
    )


def is_stale(repo_root: str | Path, archi_path: str | Path | None = None) -> bool:
    """Compare `ARCHI.md`'s embedded stamp against live-computed values.

    Stale (True) when: no stamp is present; git HEAD differs except for one
    direct, generator-owned archdocs refresh commit; the max migration number
    differs; the route count differs; or git HEAD could not be measured
    (sentinel ``unknown`` on either side — unmeasurable is never "fresh").
    Never raises — a missing/unreadable doc counts as stale (nothing to trust).
    """
    repo_root = Path(repo_root)
    archi_path = Path(archi_path) if archi_path is not None else repo_root / "ARCHI.md"

    if not archi_path.exists():
        return True

    content = archi_path.read_text(encoding="utf-8")
    stored = parse_stamp_comment(content)
    return is_stamp_stale(repo_root, stored, archi_path)


def check_staleness(
    repo_root: str | Path | None = None,
    archi_path: str | Path | None = None,
) -> dict[str, bool]:
    """Production entry for reliability audit (``audit._staleness_check``).

    Zero-arg callable: when ``repo_root`` is omitted, resolve it the same way
    the generate CLI does. Returns ``{"stale": bool}`` so a missing import or
    silent skip cannot present "no measurement" as a clean audit stage.

    Without this symbol the audit path imports fail and report
    ``{"skipped": "no_staleness_fn"}`` — built/tested ``is_stale`` with no
    production call path.
    """
    if repo_root is None:
        from omniagentos.archdocs.generate import _repo_root_default

        repo_root = _repo_root_default()
    return {"stale": is_stale(repo_root, archi_path=archi_path)}
