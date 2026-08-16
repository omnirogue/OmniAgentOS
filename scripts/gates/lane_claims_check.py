#!/usr/bin/env python3
"""Refuse a candidate that edits a path another IN-FLIGHT lane holds.

Called by ``scripts/gates/lane-claims-check.sh``; see that script's header for
the operator contract, the deliberate non-wiring into git hooks, and the
intended merge-gate follow-up.

The ledger is ``devtasks/lane-claims.yaml``. It is a machine-readable subset of
``devtasks/SESSION-CLAIMS.md``, which is prose that nothing reads and therefore
nothing enforces — a lane crossed a live ``dashboard/**`` claim on 2026-07-31
and only a human noticed, after the fact.

Answers exactly one question: does this candidate touch a path a DIFFERENT lane
declared in flight? It does not judge the change. A ``yes`` is a refusal the
author either fixes or converts into a recorded decision with
``LANE_CLAIMS_OVERRIDE=1`` — the same "make the violation visible rather than
silent" idiom as ``devtasks/REACHABILITY-EXEMPT.txt``.

Exit codes: 0 no violations (or override) · 1 violation · 2 could not run.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = REPO_ROOT / "devtasks" / "lane-claims.yaml"
IN_FLIGHT = "in_flight"
RELEASED = "released"
VALID_STATUSES = frozenset({IN_FLIGHT, RELEASED})
OVERRIDE_ENV = "LANE_CLAIMS_OVERRIDE"
# The ONLY value that overrides. See _override_requested().
OVERRIDE_VALUE = "1"


class LedgerError(Exception):
    """The ledger cannot be trusted — a refusal, never a pass."""


@dataclass(frozen=True)
class LaneClaim:
    lane: str
    owner: str
    status: str
    claimed_at: str
    paths: tuple[str, ...]
    note: str = ""
    # Alternate ids that resolve to this lane — typically the branch name, so a
    # caller that only knows `lane/foo` still resolves to lane id `foo`. Needed
    # because an unknown LANE_ID is now a refusal, not a silent pass.
    aliases: tuple[str, ...] = ()

    def answers_to(self, lane_id: str) -> bool:
        return lane_id == self.lane or lane_id in self.aliases


@dataclass(frozen=True)
class Violation:
    path: str
    lane: str
    owner: str
    glob: str
    claimed_at: str
    # "changed" for an ordinary edit; "renamed from"/"renamed to" when the path
    # was one side of a rename. A rename OUT of a claimed tree is still a
    # violation, and naming which side matched is what makes it fixable.
    how: str = "changed"


def validate_glob(glob: str, *, where: str) -> str:
    """A claim glob must be a repo-relative path pattern. Anything else refuses.

    A blank, absolute or ``..``-bearing glob silently protects NOTHING — it can
    never match a ``git diff`` path, so the lane believes it is covered and is
    not. That is the fail-open shape this whole file exists to remove, so it is
    a parse-time hard error rather than a runtime non-match.
    """
    candidate = glob.strip()
    if not candidate:
        raise LedgerError(f"{where}: blank path glob")
    if candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":"):
        raise LedgerError(
            f"{where}: path glob {glob!r} is absolute; claims are repo-relative "
            "and an absolute glob can never match a git diff path"
        )
    if candidate.startswith("\\") or "\\" in candidate:
        raise LedgerError(f"{where}: path glob {glob!r} uses backslashes; use '/'")
    segments = candidate.split("/")
    if ".." in segments:
        raise LedgerError(
            f"{where}: path glob {glob!r} contains a '..' segment; claims cannot "
            "reach outside the repo and a traversal glob matches nothing"
        )
    if "." in segments or candidate.startswith("./"):
        raise LedgerError(f"{where}: path glob {glob!r} contains a '.' segment; write it plainly")
    return candidate


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Glob semantics, pinned deliberately (tests/gates_scripts pins each one):

    * The whole path must match — the pattern is anchored at both ends. So a
      claim on ``a/b.py`` does NOT match ``a/bc.py``.
    * ``**`` matches any characters INCLUDING ``/``. ``dashboard/**`` therefore
      matches ``dashboard/src/x.ts`` at any depth, but not ``dashboard`` itself
      (a directory is not a changed file) and not ``dashboard-other/x.ts``.
    * ``*`` and ``?`` match within one segment only: they never cross ``/``.
      ``omniagentos/*/models.py`` matches ``omniagentos/swarm/models.py`` but not
      ``omniagentos/a/b/models.py``.
    * There is NO implicit prefix matching. A directory claim must be written
      ``dir/**``; making a bare ``dir`` mean its whole subtree would silently
      widen every claim in the ledger.
    """
    out: list[str] = []
    index = 0
    while index < len(glob):
        char = glob[index]
        if char == "*":
            if glob.startswith("**", index):
                out.append(".*")
                index += 2
            else:
                out.append("[^/]*")
                index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(char))
            index += 1
    return re.compile("^" + "".join(out) + "$")


def _require_str(row: Any, key: str, *, where: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{where}: {key!r} must be a non-empty string")
    return value.strip()


def parse_ledger(data: Any, *, where: str = "lane-claims.yaml") -> list[LaneClaim]:
    """Validate and structure the ledger. Anything malformed is a hard error."""
    if not isinstance(data, dict):
        raise LedgerError(f"{where}: top level must be a mapping")
    rows = data.get("lanes")
    if not isinstance(rows, list) or not rows:
        raise LedgerError(f"{where}: 'lanes' must be a non-empty list")

    claims: list[LaneClaim] = []
    seen: dict[str, int] = {}
    # alias -> (lane index, lane id). An alias that resolves to two lanes would
    # silently enforce only whichever `answers_to()` matched first, so the other
    # lane's claims are not checked against that caller — a bypass by ledger typo.
    alias_owner: dict[str, tuple[int, str]] = {}
    for index, row in enumerate(rows):
        at = f"{where}: lanes[{index}]"
        if not isinstance(row, dict):
            raise LedgerError(f"{at} is not a mapping")
        lane = _require_str(row, "lane", where=at)
        if lane in seen:
            raise LedgerError(
                f"{at}: duplicate lane id {lane!r} (also lanes[{seen[lane]}]) — "
                "two entries for one lane means one of them is silently ignored"
            )
        seen[lane] = index
        status = _require_str(row, "status", where=at)
        if status not in VALID_STATUSES:
            raise LedgerError(f"{at}: status {status!r} not in {sorted(VALID_STATUSES)}")
        paths = row.get("paths")
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            raise LedgerError(f"{at}: 'paths' must be a non-empty list of glob strings")
        validated = tuple(validate_glob(p, where=f"{at}.paths") for p in paths)

        raw_aliases = row.get("aliases", [])
        if not isinstance(raw_aliases, list) or not all(isinstance(a, str) for a in raw_aliases):
            raise LedgerError(f"{at}: 'aliases' must be a list of strings")
        aliases = tuple(a.strip() for a in raw_aliases if a.strip())
        for alias in aliases:
            if alias in seen:
                raise LedgerError(f"{at}: alias {alias!r} collides with a lane id")
            owner_of_alias = alias_owner.get(alias)
            if owner_of_alias is not None:
                previous_index, previous_lane = owner_of_alias
                if previous_index == index:
                    raise LedgerError(f"{at}: alias {alias!r} is listed twice for {lane!r}")
                raise LedgerError(
                    f"{at}: alias {alias!r} is already an alias of lane "
                    f"{previous_lane!r} (lanes[{previous_index}]) — an alias that "
                    f"resolves to two lanes enforces only one of them against a "
                    f"caller using it, so the other lane's claims are silently skipped"
                )
            alias_owner[alias] = (index, lane)

        claims.append(
            LaneClaim(
                lane=lane,
                owner=_require_str(row, "owner", where=at),
                status=status,
                claimed_at=str(row.get("claimed_at", "")).strip(),
                paths=validated,
                note=str(row.get("note", "")).strip(),
                aliases=aliases,
            )
        )

    # The in-loop `alias in seen` check only sees lanes declared SO FAR, so an
    # alias naming a lane defined LATER slipped through and resolved to the wrong
    # lane. Same fail-open as a shared alias; it just needed the other ordering.
    for alias, (alias_index, _alias_lane) in alias_owner.items():
        lane_index = seen.get(alias)
        if lane_index is not None and lane_index != alias_index:
            raise LedgerError(
                f"{where}: lanes[{alias_index}]: alias {alias!r} collides with a lane id "
                f"(lanes[{lane_index}])"
            )
    return claims


def load_ledger(path: Path) -> list[LaneClaim]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LedgerError(f"cannot read ledger {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise LedgerError(f"{path}: malformed YAML: {exc}") from exc
    return parse_ledger(data, where=str(path))


def find_violations(
    claims: list[LaneClaim],
    *,
    lane_id: str,
    changed_paths: list[tuple[str, str]],
) -> list[Violation]:
    """Touched paths held by a DIFFERENT lane that is currently in flight.

    ``changed_paths`` is ``(path, how)`` pairs, because a rename touches TWO
    paths and only checking the destination lets a lane move a file OUT of
    another lane's claimed tree unnoticed.

    A lane is never blocked by its own claims, and a `released` claim never
    blocks anyone — otherwise the ledger would rot into something people route
    around instead of update.
    """
    violations: list[Violation] = []
    for claim in claims:
        if claim.status != IN_FLIGHT or claim.answers_to(lane_id):
            continue
        for glob in claim.paths:
            pattern = _glob_to_regex(glob)
            for path, how in changed_paths:
                if pattern.match(path):
                    violations.append(
                        Violation(
                            path=path,
                            lane=claim.lane,
                            owner=claim.owner,
                            glob=glob,
                            claimed_at=claim.claimed_at,
                            how=how,
                        )
                    )
    return violations


def parse_name_status(output: str) -> list[tuple[str, str]]:
    """``git diff --name-status -M`` -> ``(path, how)`` pairs, renames expanded.

    ``--name-only`` reports ONLY the destination of a rename, so moving a file
    out of a claimed tree looked like touching an unclaimed path. Both sides of
    an R/C record are returned.
    """
    touched: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(path: str, how: str) -> None:
        entry = (path.strip(), how)
        if entry[0] and entry not in seen:
            seen.add(entry)
            touched.append(entry)

    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        status = fields[0].strip()
        if status[:1] in {"R", "C"} and len(fields) >= 3:
            add(fields[1], "renamed from" if status[:1] == "R" else "copied from")
            add(fields[2], "renamed to" if status[:1] == "R" else "copied to")
        elif len(fields) >= 2:
            add(fields[1], "changed")
    return touched


def changed_paths(repo: Path, base: str, candidate: str) -> list[tuple[str, str]]:
    """Paths the candidate touches relative to the merge base with ``base``.

    ``-M`` keeps rename detection on and ``--name-status`` exposes BOTH sides,
    so a rename across a claim boundary is visible from either end.
    """
    proc = subprocess.run(
        ["git", "diff", "--name-status", "-M", f"{base}...{candidate}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # A probe that cannot run is not a pass.
        raise LedgerError(f"cannot diff {base}...{candidate} in {repo}: {proc.stderr.strip()}")
    return parse_name_status(proc.stdout)


def _report(violations: list[Violation], *, lane_id: str) -> None:
    print(f"LANE CLAIMS: {len(violations)} path(s) held by another in-flight lane", file=sys.stderr)
    for violation in violations:
        print(
            f"  {violation.path}  [{violation.how}]\n"
            f"      claimed by lane {violation.lane!r} (owner {violation.owner},"
            f" since {violation.claimed_at or 'unrecorded'}) via glob {violation.glob!r}",
            file=sys.stderr,
        )
    print(
        f"  acting lane: {lane_id!r}\n"
        "  Resolve by: coordinating with the owning lane, waiting for its release,\n"
        "  or recording the decision with "
        f"{OVERRIDE_ENV}=1 (printed loudly, never silent).",
        file=sys.stderr,
    )


def _override_requested() -> bool:
    """``LANE_CLAIMS_OVERRIDE`` is exactly ``1`` or it is not an override.

    It previously enabled on anything outside a small deny-list, so
    ``LANE_CLAIMS_OVERRIDE=False`` bypassed all violations — a bypass nobody
    intended, spelled the way people actually spell booleans. Unset/empty means
    enforce; any other value is a hard error rather than a silent enable OR a
    silent ignore, because whoever set it meant something and both silent
    readings are wrong.
    """
    raw = os.environ.get(OVERRIDE_ENV)
    if raw is None or not raw.strip():
        return False
    value = raw.strip()
    if value == OVERRIDE_VALUE:
        return True
    if value == "0":
        # The one unambiguous "off". Everything else is a guess.
        return False
    raise LedgerError(
        f"{OVERRIDE_ENV}={raw!r} is not understood; the only value that overrides is "
        f"{OVERRIDE_VALUE!r} (use '0' or unset it to enforce). Notably 'False'/'true'/"
        "'yes' are NOT accepted — a bypass must be unambiguous"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lane-claims-check",
        description="Refuse a candidate that edits another in-flight lane's claimed paths",
    )
    parser.add_argument("base", help="base ref (e.g. main)")
    parser.add_argument("candidate", help="candidate ref (e.g. HEAD or a lane branch)")
    parser.add_argument("--lane", default=None, help="acting lane id (or LANE_ID env)")
    parser.add_argument("--ledger", type=Path, default=None, help="ledger path")
    parser.add_argument("--repo", type=Path, default=None, help="repo to diff in")
    args = parser.parse_args(argv)

    lane_id = (args.lane or os.environ.get("LANE_ID") or "").strip()
    if not lane_id:
        # Without the acting lane we cannot tell "own claim" from "someone
        # else's", and guessing would either block every lane or none.
        print(
            "LANE CLAIMS REFUSED: no acting lane — pass --lane <id> or set LANE_ID",
            file=sys.stderr,
        )
        return 2

    repo = (args.repo or REPO_ROOT).resolve()
    ledger_path = (args.ledger or DEFAULT_LEDGER).resolve()

    try:
        claims = load_ledger(ledger_path)
        override = _override_requested()
    except LedgerError as exc:
        print(f"LANE CLAIMS REFUSED: {exc}", file=sys.stderr)
        return 2

    # Fail closed BEFORE looking at the diff. An unregistered or misspelled lane
    # used to print a note and exit 0 with "0 changed path(s)", so a typo in the
    # lane name silently disabled the entire check.
    if not any(claim.answers_to(lane_id) for claim in claims):
        print(
            f"LANE CLAIMS REFUSED: lane {lane_id!r} has no entry in {ledger_path}.\n"
            "  A lane the ledger does not know cannot be distinguished from a typo, and\n"
            "  a typo must never disable the check. Add an entry (status: in_flight, its\n"
            "  owned globs, and an `aliases:` line if your branch name differs from the\n"
            f"  lane id). Known lanes: {', '.join(sorted(c.lane for c in claims))}",
            file=sys.stderr,
        )
        return 2

    try:
        touched = changed_paths(repo, args.base, args.candidate)
    except LedgerError as exc:
        print(f"LANE CLAIMS REFUSED: {exc}", file=sys.stderr)
        return 2

    violations = find_violations(claims, lane_id=lane_id, changed_paths=touched)
    if not violations:
        print(
            f"lane-claims: OK — {len(touched)} touched path(s), none held by another "
            f"in-flight lane (acting lane {lane_id!r})"
        )
        return 0

    _report(violations, lane_id=lane_id)

    if override:
        banner = "=" * 72
        print(banner, file=sys.stderr)
        print(
            f"WARNING: {OVERRIDE_ENV}=1 — {len(violations)} lane-claim "
            f"violation(s) DELIBERATELY OVERRIDDEN by lane {lane_id!r}.\n"
            "This is a recorded decision, not a clean run. Tell the owning lane(s) "
            "before you land, and put the reason in your lane evidence.",
            file=sys.stderr,
        )
        print(banner, file=sys.stderr)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
