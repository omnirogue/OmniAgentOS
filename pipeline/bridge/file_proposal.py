#!/usr/bin/env python3
"""The sanctioned writer for ``var/loopqueue/proposals/``.

    python3 bridge/file_proposal.py draft.json                 # validate, then write
    python3 bridge/file_proposal.py --check draft.json          # validate only, write nothing
    python3 bridge/file_proposal.py --derive draft.json         # fill `paths` from ground truth first

    `python3` above is a stand-in for "the interpreter on your PATH" — it is
    NOT guaranteed to have `jsonschema` installed. If it exits 2 with
    "jsonschema is not importable" (could not run — Ruling #4), the stderr
    message names a conforming
    interpreter (discovered from $VIRTUAL_ENV / $LOOP_WORKDIR) to re-run
    under; there is no system-wide guarantee, so do not assume `python3`
    alone works on a fresh host.

WHY THIS EXISTS
---------------
Until this file, nothing between the Planner and ``proposals/`` looked at
``paths``. The envelope schema listed ``paths`` as an OPTIONAL top-level field
described as "candidates: EVERY file touched"; ``CONTRACT.md`` required it for
candidates only; ``validate_envelope.py`` therefore passed a proposal with no
``paths`` at all. The single place that asked a proposal for ``paths`` was one
prose bullet in ``PROMPT-planning-loop.md``, addressed to a model writing JSON
by hand with a file-write tool.

So the absent field read as *"not required"* to every machine that looked at it,
and as *"required"* only to the Implementer, which refused on it — correctly.
That is the favourable-absence class: an abnormal condition (no conflict-graph
input) presented to every reader as a favourable value (a well-formed artifact).
Measured cost at the time this was written: 13 of 36 queued proposals carried an
empty ``paths``, and 10 of 14 ``replan`` refusals in ``rejected/`` were for
exactly that, plus one more for understating it.

The fix is not a politer request. It is this tool plus a schema that makes the
empty case invalid, so the unpopulated proposal cannot reach ``proposals/``:

* Nothing is written unless every check passes. There is no degraded write.
* Refusals are printed with the check name, what was wrong, and the remedy.
* Where ground truth exists (a branch, a PR), ``paths`` is DERIVED and the
  declared list is checked against it rather than trusted.

WHY ``paths`` IS LOAD-BEARING
-----------------------------
The Implementer builds its parallel-landing conflict graph from ``paths``
(``bridge/integration.py``, ``conflict_groups``). File-disjoint lanes land in
parallel; overlapping ones serialise. An understated ``paths`` therefore
produces a WRONG schedule — two lanes that really do collide get scheduled
together — not merely a slow one. That is why an empty ``paths`` is a refusal
and not a warning.

EXIT CODES (the process code is the carrier; every printed REFUSE line repeats
the same number, so the two never disagree)
-------------------------------------------
    0   written (or, with --check, would be written)
    1   REFUSED, fixable — correct the named gap and run again
    2   COULD NOT RUN — the instrument could not evaluate this input at all
        (missing dependency, unreadable draft, a write/ledger failure). Never
        to be read as "valid"; fix the mechanics, then the SAME input can be
        re-run. Distinct from a candidate defect (1) and from do-not-retry (3).
    3   REFUSED, do-not-retry-this-input — this exact id is already filed, or
        carries a live rejection/park. Change the payload or drop the idea;
        re-running with the same input buys the same answer twice.

Ratified 2026-08-09 (operator Ruling #4): exit 2 = COULD NOT RUN, estate-wide,
distinct from a candidate defect and from do-not-retry. This tool now AGREES
with ``validate_envelope.py`` (2 = could not run) instead of diverging from it;
the earlier scheme put could-not-run at 3 and do-not-retry at 2. The producer
convention adds one code the pure gate convention (0 pass / 1 candidate-defect /
2 could-not-run) does not need: `3` do-not-retry, for the writer's genuine
dead-end (the id is already filed), which a test/lint/build gate has no analogue
for.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from ledger_read import parse_events  # noqa: E402
from ledger_write import LedgerAppendError, append_event  # noqa: E402

EXIT_WRITTEN = 0
EXIT_REFUSED_FIXABLE = 1
EXIT_COULD_NOT_RUN = 2
EXIT_REFUSED_DO_NOT_RETRY = 3

# A conflict-graph node is one FILE. These shapes are not files, and admitting
# them silently weakens the graph rather than failing it.
_ABSOLUTE_OR_HOME = re.compile(r"^[~/]")
_PARENT_ESCAPE = re.compile(r"(^|/)\.\.(/|$)")
_GLOB = re.compile(r"[*?\[\]]")


@dataclass
class Refusal:
    code: int
    check: str
    message: str
    remedy: str = ""

    def render(self) -> str:
        tail = f"\n         REMEDY: {self.remedy}" if self.remedy else ""
        return f"REFUSE[{self.code}] {self.check}: {self.message}{tail}"


@dataclass
class Report:
    refusals: list[Refusal] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    resolved: dict[str, str] = field(default_factory=dict)

    def refuse(self, code: int, check: str, message: str, remedy: str = "") -> None:
        self.refusals.append(Refusal(code, check, message, remedy))

    @property
    def exit_code(self) -> int:
        return max((r.code for r in self.refusals), default=EXIT_WRITTEN)


# --------------------------------------------------------------------------
# repo roots
# --------------------------------------------------------------------------

def _workdir() -> Path:
    """The one repo ``LOOP_WORKDIR`` names (or ``~/OmniAgentOS`` by
    default). Factored out so ``default_roots`` and ``landable_root_names``
    can never drift on what "the repo the queue lives in" means — a second
    copy of this resolution is exactly this codebase's #1 recurring defect
    pattern (one bug becomes 2-13 across clone families)."""
    return Path(os.environ.get("LOOP_WORKDIR") or (Path.home() / "OmniAgentOS")).expanduser()


def default_roots() -> dict[str, Path]:
    """The surfaces a proposal's paths may name — KNOWN, not necessarily
    LANDABLE (see ``landable_root_names`` below for that distinction).

    ``LOOP_WORKDIR`` is the one repo the loops work on (``run-loop.sh`` passes it
    to tmux as ``-c``).  Since convergence, the queue package lives at
    ``pipeline/`` inside that repo.  It is deliberately *not* installed as a
    second nested root: doing so would canonicalise ``pipeline/bridge/x.py``
    back to the retired ``bridge/x.py`` spelling and split the conflict graph.

    ``~/Ops`` and ``~/Library/LaunchAgents`` are NOT git repos, and that is
    exactly why they are here. Ten of the thirteen empty-``paths`` proposals were
    host-ops work — the disk reaper is ``~/.omniagentos/ops/bin/disk-reaper.sh``, the
    gate is ``~/.omniagentos/ops/AccurateGate/accurate-gate.py``, the five dead daemons
    are plists in ``~/Library/LaunchAgents`` — and every one of those is a real
    file that two plans can collide on. With no root covering them a planner had
    only two options, an empty ``paths`` or a lie, and it picked the empty one.
    A conflict graph should span the surface people actually edit, not the
    subset that happens to be under version control.

    Naming them here so `paths.reality` does not refuse them is NOT the same
    as saying this Implementer can land a change into them — see
    ``landable_root_names``.
    """
    workdir = _workdir()
    roots: dict[str, Path] = {}
    for path in (workdir, Path.home() / "Work" / "Ops",
                 Path.home() / "Library" / "LaunchAgents"):
        path = Path(path).expanduser()
        if path.is_dir():
            roots.setdefault(path.name, path.resolve())
    return roots


def landable_root_names(roots: dict[str, Path], extra: Iterable[str] = ()) -> set[str]:
    """Which of ``roots`` this Implementer can actually LAND a change into.

    Every root ``default_roots`` returns is KNOWN — real enough that a plan can
    name a file there without lying about ``paths`` — but only ONE of them is
    landable by default: the repo the queue lives in (``LOOP_WORKDIR``, or
    ``~/OmniAgentOS``). ``~/Ops`` and ``~/Library/LaunchAgents`` are
    real surfaces people edit, yet nothing this Implementer merges ever reaches
    them — a proposal whose paths resolve ONLY under one of those would file
    cleanly, get admitted, and charge ``wip_cap`` forever with no way to drain
    it. That is the off-repo half of the undrainable-WIP class (operator
    directive sha256:8f5317c7192f098e4199b552ce1a9829ca25a579e55021202eaae125f3150313
    item (4)).

    ``--root NAME=PATH`` is how a caller declares an EXTRA checkout landable
    explicitly, and every name it supplies is trusted here by design — the
    alternative (a hardcoded deny-list of non-landable roots) is exactly what
    was rejected in favour of this allow-list, because a deny-list drifts out
    of date and an allow-list keyed on "landed here" cannot.
    """
    workdir = _workdir()
    if workdir.is_dir():
        workdir = workdir.resolve()
    names = {name for name, path in roots.items() if path == workdir}
    names.update(extra)
    return names


def parse_root(spec: str) -> tuple[str, Path]:
    if "=" in spec:
        name, _, raw = spec.partition("=")
    else:
        name, raw = "", spec
    path = Path(raw).expanduser().resolve()
    return (name or path.name), path


# --------------------------------------------------------------------------
# path checks — the core of this tool
# --------------------------------------------------------------------------

def _shape_problem(path: str) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return "empty or non-string entry"
    if path != path.strip():
        return "leading/trailing whitespace"
    if _ABSOLUTE_OR_HOME.match(path):
        return ("absolute or ~-rooted; the conflict graph keys on repo-relative "
                "paths, so an absolute entry can never match a lane that spells "
                "the same file relatively")
    if _PARENT_ESCAPE.search(path):
        return ".. escapes the repo root; two spellings of one file do not collide"
    if path.endswith("/"):
        return ("a directory, not a file; the conflict graph keys on files, so a "
                "directory entry never collides with the files inside it")
    if _GLOB.search(path):
        return "a glob; expand it — an unexpanded pattern is not a graph node"
    if os.path.normpath(path) in (".", ""):
        return ("the repo root, not a file; the conflict graph keys on files, "
                "so the root can never be a node — name the files under it")
    return None


def check_paths(art: dict[str, Any], roots: dict[str, Path], landable: set[str],
                rep: Report) -> list[str]:
    """Every rule that makes ``paths`` usable as a conflict graph.

    Returns the normalised top-level path list (possibly empty when refused).
    """
    raw = art.get("paths")
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    host_config = payload.get("landing_surface") == "host-config"

    if host_config:
        # Some real work touches no repo file. Proposal sha256:e413dda1 is
        # Spotlight privacy entries and Backblaze exclusions and says so in its
        # own payload: "no code, no merge train". Making it invent paths would
        # put phantom nodes in the conflict graph, which is worse than none.
        # The exemption is therefore DECLARED and ENUMERATED — never inferred
        # from an absent field, because "could not list it" and "did not" must
        # not look the same.
        touches = payload.get("touches")
        if not isinstance(touches, list) or not touches or not all(
                isinstance(t, str) and t.strip() for t in touches):
            rep.refuse(
                EXIT_REFUSED_FIXABLE, "host_config.touches",
                "landing_surface is 'host-config' but payload.touches is absent "
                "or empty",
                "enumerate what it touches anyway — a plist path, a Settings "
                "pane, an env var. The exemption is from repo-relative paths, "
                "not from saying what you are changing.",
            )
        if not str(payload.get("landing_surface_rationale") or "").strip():
            rep.refuse(
                EXIT_REFUSED_FIXABLE, "host_config.rationale",
                "landing_surface is 'host-config' but no "
                "payload.landing_surface_rationale is given",
                "state why this work touches no repo file. Stating it is the "
                "cost of the exemption, and it is what stops the hatch becoming "
                "the default escape from enumerating paths.",
            )
        rep.warnings.append(
            "host-config surface: this proposal has no conflict-graph nodes, so "
            "the Implementer must SERIALISE it rather than schedule it in "
            "parallel — it cannot be shown disjoint from anything")

    if not isinstance(raw, list) or not raw:
        if host_config:
            return []
        rep.refuse(
            EXIT_REFUSED_FIXABLE, "paths.present",
            "top-level `paths` is absent or empty. The Implementer builds its "
            "parallel-landing conflict graph from this field, so an empty one "
            "produces a WRONG schedule (colliding lanes scheduled together), "
            "not a slow one",
            "list EVERY file the work will touch, including files it will "
            "CREATE (declare those in payload.new_paths). If the plan already "
            "has a branch or a PR, run with --derive and this tool will take "
            "the list from the diff instead of asking you for it. If the work "
            "genuinely touches no repo file — a launchd plist, a Settings pane "
            "— set payload.landing_surface to 'host-config' and enumerate "
            "payload.touches plus a landing_surface_rationale.",
        )
        return []

    normalised: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        problem = _shape_problem(entry)
        if problem:
            rep.refuse(EXIT_REFUSED_FIXABLE, "paths.shape",
                       f"{entry!r} is {problem}",
                       "give a repo-relative file path")
            continue
        norm = os.path.normpath(entry)
        if norm in seen:
            rep.warnings.append(f"paths lists {norm!r} more than once — de-duplicated")
            continue
        seen.add(norm)
        normalised.append(norm)

    # --- reality: a path must exist, or be declared as one the plan creates ---
    declared_new = payload.get("new_paths") or []
    if not isinstance(declared_new, list):
        rep.refuse(EXIT_REFUSED_FIXABLE, "paths.new_paths",
                   "payload.new_paths must be a list of strings",
                   "list only the paths this plan CREATES")
        declared_new = []
    new_set = {os.path.normpath(str(p)) for p in declared_new}

    stray = sorted(new_set - set(normalised))
    if stray:
        rep.refuse(EXIT_REFUSED_FIXABLE, "paths.new_paths",
                   f"payload.new_paths names {len(stray)} path(s) absent from "
                   f"top-level paths: {', '.join(stray[:6])}",
                   "every new file is still a file the work touches — it belongs "
                   "in paths too")

    # Deepest root first. Host-ops roots can still nest, but pipeline/ is not a
    # root of its own: every repository file has exactly one Grok-relative graph
    # key (for example pipeline/bridge/governor.py).
    by_depth = sorted(roots.items(), key=lambda kv: len(str(kv[1])), reverse=True)

    def canonical(abs_path: Path) -> tuple[str, str] | None:
        for name, root in by_depth:
            try:
                return name, str(abs_path.relative_to(root))
            except ValueError:
                continue
        return None

    phantom: list[str] = []
    off_repo: list[tuple[str, list[str]]] = []
    for path in normalised:
        hits = [(name, root) for name, root in by_depth if (root / path).exists()]
        if hits:
            if not any(name in landable for name, _ in hits):
                # Exists, but ONLY under a KNOWN root this Implementer cannot
                # LAND a change into (see landable_root_names) — the off-repo
                # half of the undrainable-WIP class. Filing this would pass
                # `paths.reality` clean, get admitted, and charge wip_cap
                # forever with nothing this Implementer merges ever reaching
                # it, so it is refused here rather than left to strand later.
                off_repo.append((path, sorted({name for name, _ in hits})))
                continue
            name, root = hits[0]
            best = canonical((root / path).resolve())
            if best and best[1] != path:
                rep.refuse(
                    EXIT_REFUSED_FIXABLE, "paths.spelling",
                    f"{path!r} resolves under root {name!r}, but the same file is "
                    f"{best[1]!r} under the more specific root {best[0]!r}",
                    f"use {best[1]!r}. Roots nest here, and the conflict graph keys "
                    "on the string — two spellings of one file are two nodes, so "
                    "they never collide and both lanes land together.",
                )
                continue
            rep.resolved[path] = name
            if len({n for n, _ in hits}) > 1:
                rep.warnings.append(
                    f"{path!r} exists in more than one root "
                    f"({', '.join(sorted(n for n, _ in hits))}) — the graph treats "
                    "them as one node, which over-serialises rather than "
                    "under-serialises, so this is safe but imprecise")
            if path in new_set:
                rep.warnings.append(
                    f"{path!r} is declared in new_paths but already exists — it is "
                    "an edit, not a creation")
        elif path in new_set:
            rep.resolved[path] = "new"
        else:
            phantom.append(path)

    if phantom:
        rep.refuse(
            EXIT_REFUSED_FIXABLE, "paths.reality",
            f"{len(phantom)} path(s) exist in no known root and are not declared "
            f"as new: {', '.join(phantom[:8])}"
            f"{' …' if len(phantom) > 8 else ''} (roots searched: "
            f"{', '.join(f'{k}={v}' for k, v in sorted(roots.items()))})",
            "a path that names nothing is the pre-rename-name trap that produced "
            "rejection sha256:d12b0fdc — it declared PROMPT-repair-loop.md and "
            "PROMPT-integration-loop.md, which commit 0000000 had already renamed. "
            "Fix the spelling, add --root for a checkout this tool does not know, "
            "or add the path to payload.new_paths if the plan creates it.",
        )

    if off_repo:
        landable_list = ", ".join(sorted(landable)) or "none configured"
        offending = "; ".join(
            f"{path!r} (only under {'/'.join(names)})" for path, names in off_repo[:8]
        )
        rep.refuse(
            EXIT_REFUSED_FIXABLE, "paths.off_repo",
            f"{len(off_repo)} path(s) resolve, but only under a root this "
            f"Implementer cannot land a change into — landable root(s): "
            f"{landable_list}; offending: {offending}"
            f"{' …' if len(off_repo) > 8 else ''}",
            "nothing this Implementer merges ever reaches a non-landable root, "
            "so filing this here would charge wip_cap forever with no way to "
            "drain it — file it in that root's own landing lane instead. If "
            "this checkout really IS landable from here, pass --root "
            "NAME=PATH to declare it explicitly.",
        )

    check_lanes(payload, normalised, rep)
    return normalised


def check_lanes(payload: dict[str, Any], top: list[str], rep: Report) -> None:
    """Lanes are the graph's partition. An empty lane has no partition to give."""
    lanes = payload.get("lanes")
    if lanes is None:
        return
    if not isinstance(lanes, list) or not lanes:
        rep.refuse(EXIT_REFUSED_FIXABLE, "lanes.shape",
                   "payload.lanes is present but not a non-empty list",
                   "omit `lanes` for single-lane work, or give it real lanes")
        return

    union: set[str] = set()
    for index, lane in enumerate(lanes):
        label = (lane.get("lane") if isinstance(lane, dict) else None) or f"#{index}"
        if not isinstance(lane, dict):
            rep.refuse(EXIT_REFUSED_FIXABLE, "lanes.shape",
                       f"lane {label} is not an object")
            continue
        lane_paths = lane.get("paths")
        if not isinstance(lane_paths, list) or not lane_paths:
            rep.refuse(
                EXIT_REFUSED_FIXABLE, "lanes.paths",
                f"lane {label!r} has an empty `paths`. Lanes ARE the parallelism: "
                "a lane with no files cannot be shown disjoint from its siblings, "
                "so the file-disjointness asserted in known_conflicts is unverifiable",
                "give every lane the files it owns; the union must equal top-level paths",
            )
            continue
        union.update(os.path.normpath(str(p)) for p in lane_paths)

    if not top:
        return
    top_set = set(top)
    missing_from_top = sorted(union - top_set)
    if missing_from_top:
        rep.refuse(
            EXIT_REFUSED_FIXABLE, "lanes.coverage",
            f"{len(missing_from_top)} lane path(s) are absent from top-level paths: "
            f"{', '.join(missing_from_top[:6])}",
            "top-level paths must be the union of the lanes — the Implementer "
            "schedules against the top-level list, so a file only a lane knows "
            "about is invisible to the graph",
        )
    unowned = sorted(top_set - union)
    if unowned:
        rep.refuse(
            EXIT_REFUSED_FIXABLE, "lanes.coverage",
            f"{len(unowned)} path(s) belong to no lane: {', '.join(unowned[:6])}",
            "assign every file to a lane; an unowned file is a file two lanes "
            "may both edit without either declaring it",
        )


# --------------------------------------------------------------------------
# derive rather than ask
# --------------------------------------------------------------------------

_PR_KEYS = ("pr", "pr_number", "pull_request", "pr_url")
_PR_NUM = re.compile(r"(?:^|/pull/|#)(\d+)\s*$")


def _pr_reference(art: dict[str, Any]) -> str | None:
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    for key in _PR_KEYS:
        for source in (art, payload):
            value = source.get(key)
            if value is None:
                continue
            match = _PR_NUM.search(str(value).strip())
            if match:
                return match.group(1)
    return None


def _run(cmd: list[str]) -> tuple[set[str] | None, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{cmd[0]} could not run: {exc}"
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout).strip()[:400]
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}, ""


def derive_paths(art: dict[str, Any], roots: dict[str, Path]) -> tuple[set[str] | None, str]:
    """Ground truth for ``paths``, when the artifact points at something real.

    A branch gives it via a three-dot diff (which resolves the fork point itself,
    so this never has to shell out to the plumbing command whose name the local
    git guard refuses). A PR gives it via ``gh pr diff --name-only``.
    """
    branch = art.get("branch")
    base = art.get("base_sha")
    if branch and base:
        for name, root in sorted(roots.items()):
            found, err = _run(["git", "-C", str(root), "diff", "--name-only",
                               f"{base}...{branch}"])
            if found is not None:
                return found, f"git diff {base[:12]}...{branch} in {name}"
        return None, f"branch {branch!r} is not resolvable in any known root"

    pr = _pr_reference(art)
    if pr:
        found, err = _run(["gh", "pr", "diff", pr, "--name-only"])
        if found is not None:
            return found, f"gh pr diff {pr} --name-only"
        return None, f"gh pr diff {pr}: {err}"

    return None, ("nothing to derive from — the artifact names no branch "
                  "(branch + base_sha) and no PR")


def check_against_truth(declared: Iterable[str], truth: set[str], how: str,
                        rep: Report) -> None:
    missing = sorted(truth - set(declared))
    if missing:
        rep.refuse(
            EXIT_REFUSED_FIXABLE, "paths.understated",
            f"{how} shows {len(missing)} file(s) that `paths` does not declare: "
            f"{', '.join(missing[:8])}{' …' if len(missing) > 8 else ''}",
            "declare them. Understating paths schedules a conflicting candidate "
            "to land in parallel — this is the check the Implementer would have "
            "run later, moved to write time",
        )


# --------------------------------------------------------------------------
# identity, duplicates, live rejections
# --------------------------------------------------------------------------

def _stem(ident: str) -> str:
    return ident.replace(":", "_")


def _rollback(target: Path) -> bool:
    """Undo a publish. Returns True iff the artifact is really gone — a swallowed
    unlink error is how "rolled back" becomes a lie."""
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


# Three-valued readback: the difference between "absent" and "unknown" is what
# keeps a rollback from creating a phantom (§5 forbids deleting the ledger line
# to repair one). "unknown" is treated like "present" by the callers — keep the
# artifact — because an orphan artifact is visible and recoverable, while a
# phantom event with no artifact is neither.
LEDGER_EVENT_PRESENT = "present"
LEDGER_EVENT_ABSENT = "absent"
LEDGER_EVENT_UNKNOWN = "unknown"


def _ledger_event_state(queue: Path, ident: str, event: str) -> str:
    """Did the event actually land? PRESENT / ABSENT / UNKNOWN.

    The authority is the ledger on disk, not the exception type — a naive append
    can raise OSError AFTER its bytes are durable, and rolling back then orphans
    a real ledger line. Tolerates a torn tail (skip unparseable lines, never
    abort — CONTRACT §5). **An unreadable ledger is UNKNOWN, never ABSENT:**
    returning "absent" on a read error is what let a rollback delete an artifact
    whose event had in fact landed (R3-FI-01)."""
    path = queue / "ledger.jsonl"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return LEDGER_EVENT_UNKNOWN
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        # Non-object lines have no .get; this read-back decides whether a
        # rollback is safe, so a crash here aborts the filing mid-write.
        #
        # Reading EVERY event on the line matters most in exactly this
        # function: our own append can be the one a newline-less writer left
        # object 2 behind on. Reporting ABSENT for an event that is durably on
        # disk is what rolls back a real artifact (R3-FI-01). `reversed` inside
        # the line too, to keep the scan strictly newest-first.
        events, _st = parse_events(line)
        for ev in reversed(events):
            if ev.get("id") == ident and ev.get("event") == event:
                return LEDGER_EVENT_PRESENT
    return LEDGER_EVENT_ABSENT


def _ledger_fail_message(exc: Exception, target: Path, queue: Path,
                         rolled_back: bool) -> str:
    if rolled_back:
        return (f"REFUSE[{EXIT_COULD_NOT_RUN}] ledger: the event write failed "
                f"({exc}); the artifact was rolled back.\n"
                f"         REMEDY: fix the ledger (disk/permissions on "
                f"{queue / 'ledger.jsonl'}) and re-run — the filing is atomic.")
    return (f"REFUSE[{EXIT_COULD_NOT_RUN}] ledger: the event write failed ({exc}) "
            f"AND the rollback unlink failed — {target} is ORPHANED (on disk with "
            f"no ledger event).\n         REMEDY: remove {target} by hand, fix the "
            f"ledger, and re-file.")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _expired(stamp: str | None) -> bool:
    if not stamp:
        return False  # no expiry recorded == never expires; treat as live
    try:
        parsed = time.strptime(stamp.replace("Z", "").split(".")[0], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return calendar.timegm(parsed) < calendar.timegm(time.gmtime())


def check_identity(art: dict[str, Any], queue: Path, rep: Report) -> None:
    from canonical import content_id

    payload = art.get("payload")
    if not isinstance(payload, dict):
        rep.refuse(EXIT_REFUSED_FIXABLE, "id.payload", "payload is missing or not an object")
        return

    expected = content_id(payload)
    if art.get("id") != expected:
        rep.refuse(
            EXIT_REFUSED_FIXABLE, "id.hash",
            f"id does not match the canonical hash of payload "
            f"(declared {str(art.get('id'))[:19]}…, computed {expected[:19]}…)",
            f"set id to {expected}. A hand-copied id from an earlier draft is how "
            "a corrected plan gets refused as a duplicate of the plan it corrects.",
        )
        art["id"] = expected  # so the remaining checks look at the real identity

    ident = art["id"]
    if (queue / "proposals" / f"{_stem(ident)}.json").exists():
        rep.refuse(
            EXIT_REFUSED_DO_NOT_RETRY, "id.duplicate",
            f"{ident[:19]}… is already filed in proposals/",
            "the payload is byte-identical to a pending proposal. Change the plan "
            "or leave it alone — re-filing an unchanged payload is the "
            "unchanged-input retry that this estate measured 28 of in one night.",
        )

    for area in ("rejected", "parked"):
        marker = queue / area / f"{_stem(ident)}.json"
        if not marker.exists():
            continue
        try:
            record = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            # Fail CLOSED: an unreadable tombstone must block, not vanish — a
            # marker we cannot read might be a live ban, and filing past it
            # defeats the §4/§7 trap in exactly the invisible way.
            rep.refuse(
                EXIT_COULD_NOT_RUN, f"id.unreadable_{area}",
                f"{area}/ marker for {ident[:19]}… exists but cannot be read ({exc})",
                f"fix or remove {marker} first — an unreadable marker is an "
                "instrument error, not an absence.",
            )
            continue
        if not isinstance(record, dict):
            # Valid JSON but not an object (e.g. `[]`): every later `.get()`
            # would AttributeError. A marker of the wrong shape is unreadable,
            # not absent — fail closed, same as a JSON parse error.
            rep.refuse(
                EXIT_COULD_NOT_RUN, f"id.malformed_{area}",
                f"{area}/ marker for {ident[:19]}… is JSON but not an object "
                f"({type(record).__name__})",
                f"fix {marker} to a JSON object first.",
            )
            continue
        expires = record.get("expires_at")
        if area == "parked":
            # Parking NEVER decays by TTL: only an authenticated human
            # `unparked` event ends it (CONTRACT §9). An expires_at field on a
            # park marker is ignored, not honoured.
            rep.refuse(
                EXIT_REFUSED_DO_NOT_RETRY, "id.live_parked",
                f"{ident[:19]}… is parked awaiting a human decision: "
                f"{str(record.get('reason', ''))[:200]}",
                "parking ends only with an authenticated unparked event — no "
                "loop (and no TTL) may release it. Raise an inquiry if the "
                "park looks stale.",
            )
            continue
        if _expired(expires):
            rep.warnings.append(
                f"{area}/ carries an EXPIRED marker for this id (expired {expires}) — "
                "re-filing is allowed")
            continue
        rep.refuse(
            EXIT_REFUSED_DO_NOT_RETRY, f"id.live_{area}",
            f"{ident[:19]}… carries a live {area} marker until {expires} "
            f"(remedy={record.get('remedy')!r}, class={record.get('class')!r}): "
            f"{str(record.get('reason', ''))[:200]}",
            "a live rejection bounds resubmission of an UNCHANGED payload. If the "
            "remedy is 'replan', change the payload (fixing lanes[].paths changes "
            "the hash) and set `supersedes` to this id — that is a NEW artifact, "
            "not a retry. If the remedy is 'drop', do not re-file at all.",
        )


# --------------------------------------------------------------------------
# structured probe -- the security boundary
# --------------------------------------------------------------------------
# The CLOSED set of probe types. Each maps to a hardcoded, read-only argv the
# gate builds itself; the proposal supplies only the typed data fields. Adding a
# type here is a Class-A security change (it widens the only steerable surface).
_PROBE_TYPES = ("grep_present", "path_absent", "diff_from_ref")

# Field caps -- a probe field is data, never a program. Cap length so a probe
# cannot smuggle a megabyte of argv, and reject the shapes that would let a field
# act as a flag or escape the repo.
_PROBE_PATTERN_MAX = 512
_PROBE_PATH_MAX = 512
_PROBE_REF_MAX = 200

# Defence-in-depth on typed fields. shell=False already neutralises these, and
# every field is passed as a literal argv element AFTER a `--` or an `-e` (so it
# can never become a flag), but a clear refusal beats a silently-inert byte.
_PROBE_FIELD_METACHARS = frozenset(";|&<>\n\r\t\0`$(){}[]!*?\\")
# A git ref is a name, not a path: no separators, no `..` range/escape, no
# leading dash (option injection), a tight charset. This deliberately rejects
# `origin/main` (has a `/`) -- pin to a sha or a slash-free branch/tag name.
_PROBE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Reproduce verdicts from a probe's exit code.
_REPRO = "reproduces"   # the problem is present on HEAD -> admit
_GONE = "gone"          # the problem is already solved -> refuse necessity.reproduce
_PROBE_ERR = "error"    # the probe ran but returned an uninterpretable code -> WARN skip

# A proposal id is a content hash; a git object is loose hex. Kept distinct so a
# dependency that names a proposal is checked against the ledger and one that
# names a commit is checked against the object store.
_SHA256_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
# >=12 hex, so an English word can never masquerade as a base_sha and draw a
# false dependency refusal (the anti-false-refusal polarity above).
_GIT_SHA_RE = re.compile(r"\b[0-9a-f]{12,40}\b")

# Self-governing surfaces (Step 4c): a change to one of these is a DIRECTION
# call, not a reproducible bug, and the filer does not decide direction.
#
# Round-3 (Grok Gc + Sweep B): the old substring regex r"(gate|schema|prompt|
# approv)" over-matched -- 'aggregate_stats.py', 'propagate.py' and
# 'gateway_client.py' each carry one of those letter-runs and were false-routed to
# necessity.undecided. Match WHOLE path TOKENS instead: split every path on '/'
# and then on any non-alphanumeric run, lowercase, and compare each token against
# this closed set. 'aggregate'/'propagate'/'gateway' are single tokens that are
# not in the set, so they no longer match; 'gates.d', 'PROMPT-x.md',
# 'approvals.py' tokenise to a member and still do.
_SELF_GOVERNING_TOKENS = frozenset({
    "gate", "gates", "schema", "schemas", "prompt", "prompts",
    "approve", "approval", "approvals", "approver",
})
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# A declared change class that REQUIRES a reproduce-first probe vs one EXEMPT from
# it (the over-refusal fix -- do not false-starve net-new work). Round-3 Sweep B:
# anchor each stem at a word START (\b) so a substring like 'fix' inside 'prefix'
# or 'bug' inside 'humbug' no longer forces an unrelated class into the bug lane.
# The stems stay PREFIXES on purpose (regress->regression, enhanc->enhancement,
# hotfix stays whole, bugfix still matches via \bbug), only the left edge is
# tightened to a token boundary.
_BUG_CLASS_RE = re.compile(r"\b(bug|fix|defect|regress|hotfix|repro)", re.I)
_EXEMPT_CLASS_RE = re.compile(
    r"\b(feat|feature|doc|refactor|chore|enhanc|style|test|perf|build)|\bci\b", re.I)

# C-ii MISLABEL-SMELL (observability only -- NEVER a refusal). When a proposal's
# change class is ABSENT or UNRECOGNIZED (it matched neither `_BUG_CLASS_RE` nor
# `_EXEMPT_CLASS_RE`) AND it carries no probe/falsifier -- so it is EXEMPT and
# checks 1-3 are skipped -- yet its problem/title text talks like a defect, it MAY
# be an honestly-undeclared or mislabeled bug dodging the probe by omission. We
# ADMIT it and emit ONE nudge-warning so the omission is OBSERVABLE and an honest
# planner is prompted to declare it. This is a plain substring match on purpose:
# it is a cheap smell receipt, not a gate, so a loose hit ('fix' inside 'prefix',
# a bare 'error') is an acceptable false-flag -- a WARN costs nothing and never
# drops work. It deliberately does NOT force a probe: requiring one from every
# proposal is the STARVATION blocker (a behavioural bug often cannot be expressed
# as a static grep/path/diff probe), so this stays a receipt. A DECLARED exempt
# class (feature/doc/refactor/chore) whose text has defect words is left QUIET --
# flagging declared net-new is noise; that mislabel is the accepted residual (see
# DESIGN §2iii). Checks 4-6 still run for exempt proposals, so a mislabeled
# duplicate is still caught, and the exec surface is closed by construction.
_MISLABEL_SMELL_RE = re.compile(
    r"(bug|fix|regression|broken|fails|error|crash|defect|incorrect)", re.I)

# Events that RETIRE a proposal id from the live/in-flight state. `parked` is
# included: a parked id awaits a human and is not "in flight" competing for a
# builder, so a new draft is not duplicating live work by re-raising it.
_NECESSITY_RETIRED_EVENTS = frozenset({
    "merged", "rejected", "released", "completed", "closed", "dropped", "parked",
})
# Events that ABANDON a dependency -- building on one of these is building on
# dead work.
_NECESSITY_ABANDONED_EVENTS = frozenset({"rejected", "dropped"})


# --------------------------------------------------------------------------
# probe selection + typed-field validation
# --------------------------------------------------------------------------

def _art_paths(art: dict[str, Any]) -> list[str]:
    """Every declared file path on an artifact, defensively typed.

    Round-3 (Grok Gb): several sites read ``art.get("paths") or []`` and iterate
    it. If ``paths`` is any non-list (an int, a str, a dict -- a malformed or
    hostile artifact), ``for p in <that>`` raises TypeError and crashes the whole
    filer. This is the single coercion point: a non-list is treated as "no paths",
    and non-str elements of a list are dropped. Read paths ONLY through here so no
    art-supplied type can propagate an exception to the filer (polarity: a
    malformed field is an instrument fault, never a crash and never a refusal)."""
    value = art.get("paths")
    if not isinstance(value, list):
        return []
    return [p for p in value if isinstance(p, str)]


def _necessity_probe_specs(art: dict[str, Any]) -> list[Any]:
    """Every declared necessity probe, from the top-level `necessity_probe`
    field (canonical) or a `payload.necessity_probe` fallback. A probe is a
    single typed object; a list is tolerated (each object counted) so the caller
    can tell "none" from "more than one" -- both are `necessity.unproven`,
    because the filer must know which single proof to run."""
    found: list[Any] = []
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    for source in (art, payload):
        value = source.get("necessity_probe")
        if value is None:
            continue
        if isinstance(value, list):
            found.extend(value)
        else:
            found.append(value)
    return found


def _proposal_class(art: dict[str, Any]) -> str | None:
    """The declared change class, if any. Read from the PAYLOAD (and an explicit
    top-level `risk_class`) -- NOT from `art['kind']`, which is the envelope kind
    (`"proposal"`), not the change class."""
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    for key in ("risk_class", "change_kind", "kind", "category", "type", "class"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    top = art.get("risk_class")
    if isinstance(top, str) and top.strip():
        return top.strip()
    return None


def _problem_text(art: dict[str, Any]) -> str:
    """The proposal's CLAIMED-PROBLEM text, lowercased and joined: the payload
    `problem`/`symptom` plus any `title`/`summary` on the payload or the
    envelope. Read ONLY for the C-ii mislabel-smell WARN in the orchestrator (a
    receipt on an exempt proposal that nonetheless talks like a defect). It does
    NOT read `implementation_plan` -- the smell is about the CLAIM, not the plan."""
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    parts: list[str] = []
    for source, key in ((payload, "problem"), (payload, "symptom"),
                        (payload, "title"), (payload, "summary"),
                        (art, "title"), (art, "summary")):
        value = source.get(key)
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts).lower()


def _needs_probe(art: dict[str, Any]) -> bool:
    """Is this the reproducible-problem / bug-fix class that must PROVE its
    problem still reproduces?

    A declared class wins: bug-ish -> yes, feature/docs/refactor/chore -> no. With
    no (or an unrecognised) class, a present probe or a declared falsifier is the
    reproducible-problem signal; otherwise the proposal is EXEMPT (the over-refusal
    fix: a plain net-new feature is not made to invent a probe)."""
    cls = _proposal_class(art)
    if cls:
        if _BUG_CLASS_RE.search(cls):
            return True
        if _EXEMPT_CLASS_RE.search(cls):
            return False
    if _necessity_probe_specs(art):
        return True
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    return bool(payload.get("falsifier") or payload.get("reproduce")
                or payload.get("repro") or payload.get("reproduces"))


def _field_has_metachar(value: str) -> str | None:
    for ch in value:
        if ch in _PROBE_FIELD_METACHARS:
            return f"contains the shell metacharacter {ch!r}"
    return None


def _valid_pattern(pattern: Any) -> tuple[str | None, str]:
    """A grep pattern is arbitrary literal text (it is the operand of `-e` under
    `-F`, never a flag) -- only its type, non-emptiness, length, and freedom from
    embedded newlines/NULs are constrained. `pattern="--output=/tmp/x"` is a
    perfectly valid, HARMLESS search string here."""
    if not isinstance(pattern, str) or not pattern:
        return None, "probe.pattern is missing or not a non-empty string"
    if len(pattern) > _PROBE_PATTERN_MAX:
        return None, f"probe.pattern exceeds {_PROBE_PATTERN_MAX} chars"
    if any(ch in "\n\r\0" for ch in pattern):
        return None, "probe.pattern contains a newline or NUL"
    return pattern, ""


def _confine_path(path: Any) -> tuple[str | None, str]:
    """Validate a repo-relative file path and return it normalised, or refuse.

    Repo confinement is structural: the path must be RELATIVE (no absolute, no
    `~`), carry no `..` component, no leading `-` (so it can never be read as an
    option), and no shell metacharacter. Combined with a cwd pinned to the repo
    and a leading `-- ` separator in every argv, a validated path can only ever
    name a file inside the repo the gate runs in -- there is no `-C`, and no
    absolute operand, for a probe to reach outside with."""
    if not isinstance(path, str) or not path.strip():
        return None, "probe.path is missing or not a non-empty string"
    candidate = path.strip()
    if len(candidate) > _PROBE_PATH_MAX:
        return None, f"probe.path exceeds {_PROBE_PATH_MAX} chars"
    if candidate.startswith("-"):
        return None, ("probe.path starts with '-' (would be read as an option, "
                      "not a path)")
    if candidate.startswith("/") or candidate.startswith("~"):
        return None, ("probe.path is absolute or ~-rooted; a probe path must be "
                      "repo-relative so it cannot reach outside the repo")
    meta = _field_has_metachar(candidate)
    if meta:
        return None, f"probe.path {meta}"
    parts = [p for p in candidate.replace("\\", "/").split("/") if p]
    if ".." in parts:
        return None, "probe.path escapes the repo root with '..'"
    normalised = os.path.normpath(candidate)
    # Belt-and-braces confinement invariant on the NORMALISED path. The raw
    # leading-'-' check above runs before normpath, and normpath('./-foo') ==
    # '-foo' -- a normalised leading dash that the raw check misses. Re-reject a
    # leading dash (option injection), a TRAVERSAL '..' component (escape), or an
    # absolute path here so the check holds even if a future probe type ever
    # builds an argv without the '--' separator.
    #
    # Round-3 (Grok G3 + Sweep B): the escape test must be traversal-SPECIFIC, not
    # a raw ``startswith("..")``. normpath leaves a legit filename like '..foo' or
    # '..env' unchanged, and startswith('..') false-refused those. A real traversal
    # normalises to exactly '..' or to a value beginning '..' + os.sep ('../x');
    # test for that instead so a dotfile-ish leading-'..' NAME is accepted while an
    # actual parent escape is still refused. (The component-level ``".." in parts``
    # guard above already rejects '../x' before we get here; this is defence in
    # depth for the no-separator future.)
    is_traversal = (normalised == ".."
                    or normalised.startswith(".." + os.sep))
    if (normalised.startswith("-") or is_traversal
            or os.path.isabs(normalised)):
        return None, "probe.path normalises to an option or an escape"
    return normalised, ""


def _confine_ref(ref: Any) -> tuple[str | None, str]:
    """Validate a git ref (a name, not a path): no separators, no `..`, no
    leading dash, a tight charset, capped length."""
    if not isinstance(ref, str) or not ref.strip():
        return None, "probe.ref is missing or not a non-empty string"
    candidate = ref.strip()
    if len(candidate) > _PROBE_REF_MAX:
        return None, f"probe.ref exceeds {_PROBE_REF_MAX} chars"
    if candidate.startswith("-"):
        return None, "probe.ref starts with '-' (would be read as an option)"
    if "/" in candidate:
        return None, ("probe.ref contains a path separator; pin to a sha or a "
                      "slash-free branch/tag name")
    if ".." in candidate:
        return None, "probe.ref contains '..' (a range or escape)"
    if not _PROBE_REF_RE.match(candidate):
        return None, "probe.ref contains characters outside [A-Za-z0-9._-]"
    return candidate, ""


def _interpret_grep_present(code: int) -> str:
    if code == 0:
        return _REPRO   # match found -> the string is still present
    if code == 1:
        return _GONE    # no match -> the string is gone
    return _PROBE_ERR   # >=2 -> git error


def _interpret_path_absent(code: int) -> str:
    if code == 0:
        return _GONE    # ls-files matched -> the path is tracked / present
    if code == 1:
        return _REPRO   # --error-unmatch -> the path is absent / renamed
    return _PROBE_ERR


def _interpret_diff_from_ref(code: int) -> str:
    if code == 0:
        return _GONE    # no diff -> already reconciled to <ref>
    if code == 1:
        return _REPRO   # differs from <ref>
    return _PROBE_ERR


def _build_probe(spec: Any) -> tuple[list[str] | None, str, Any]:
    """Turn a typed probe spec into a fixed read-only argv the GATE owns, or
    refuse it. Returns (argv, "", interpret_fn) when safe, else (None, reason,
    None). No field of `spec` ever becomes a binary or a flag: the binary is the
    literal ``"git"``, every flag is written here, and the data fields are
    validated and placed only as operands after ``-e`` / ``--``."""
    if not isinstance(spec, dict):
        return None, "necessity_probe is not a typed object with a `type`", None
    ptype = spec.get("type")
    if ptype not in _PROBE_TYPES:
        return None, (
            f"unknown probe type {ptype!r}; allowed: {', '.join(_PROBE_TYPES)} "
            "(adding a type is a Class-A security change)"), None

    if ptype == "grep_present":
        pattern, why = _valid_pattern(spec.get("pattern"))
        if pattern is None:
            return None, why, None
        relpath, why = _confine_path(spec.get("path"))
        if relpath is None:
            return None, why, None
        return (["git", "--no-pager", "grep", "-q", "-F", "-e", pattern, "--", relpath],
                "", _interpret_grep_present)

    if ptype == "path_absent":
        relpath, why = _confine_path(spec.get("path"))
        if relpath is None:
            return None, why, None
        return (["git", "--no-pager", "ls-files", "--error-unmatch", "--", relpath],
                "", _interpret_path_absent)

    # diff_from_ref
    ref, why = _confine_ref(spec.get("ref"))
    if ref is None:
        return None, why, None
    relpath, why = _confine_path(spec.get("path"))
    if relpath is None:
        return None, why, None
    return (["git", "--no-pager", "diff", "--quiet", "--no-ext-diff", ref, "--", relpath],
            "", _interpret_diff_from_ref)


# --------------------------------------------------------------------------
# probe execution -- gate-built argv only, minimal env, fixed cwd, hard timeout
# --------------------------------------------------------------------------

def _probe_env() -> dict[str, str]:
    """A minimal, curated env. Nothing that lets git run an external program or
    reach a remote is inherited; the system gitconfig is disabled. Combined with
    the read-only argv templates and ``--no-ext-diff`` this closes the config /
    attribute driver vectors even against the repo's own config."""
    src = os.environ
    return {
        "PATH": src.get("PATH", "/usr/bin:/bin"),
        "HOME": src.get("HOME", ""),
        "LANG": src.get("LANG", "C"),
        "LC_ALL": src.get("LC_ALL", "C"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ALLOW_PROTOCOL": "none",
    }


def _probe_cwd(roots: dict[str, Path]) -> str | None:
    """The fixed cwd a probe runs in: the repo LOOP_WORKDIR names (the same
    resolution _workdir() uses), else any known root. This is the repo every
    probe path is confined to; there is no filer `-C` to override it."""
    workdir = _workdir()
    if workdir.is_dir():
        return str(workdir.resolve())
    for path in roots.values():
        if Path(path).is_dir():
            return str(path)
    return None


def _run_probe(argv: list[str], cwd: str | None,
               timeout: int = 120) -> tuple[int | None, str]:
    """Run a GATE-BUILT argv and return (returncode, combined_output).

    Deliberately NOT the module's `_run` helper: `_run` collapses every non-zero
    exit to None because it only cares about a command that SUCCEEDS. A probe's
    returncode IS the datum being compared, so it needs the real code even when
    the probe exits non-zero. Returns (None, message) only on a genuine
    could-not-run (OSError / timeout / ValueError), which the caller routes to
    WARN. ValueError is caught too: subprocess.run raises it when an argv element
    the gate built carries an embedded NUL byte (e.g. a proposal-supplied
    `branch` smuggled into a rev-parse argv) -- a malformed gate-built argv is an
    instrument error, never a crash of the whole filer. Always shell=False, a
    curated minimal env, a fixed cwd, and a hard timeout."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, cwd=cwd,
                              shell=False, timeout=timeout, env=_probe_env())
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return None, f"{argv[0]} could not run: {exc}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()[:400]


def _head_sha(cwd: str | None) -> str | None:
    if cwd is None:
        return None
    code, out = _run_probe(["git", "-C", cwd, "rev-parse", "HEAD"], cwd)
    return out.strip() if code == 0 and out.strip() else None


def _normalise_problem(text: Any) -> str:
    """Lowercased, whitespace-collapsed problem text for equality/containment.
    Not a hash: two Planners restating one problem must compare equal without
    matching byte-for-byte."""
    if not isinstance(text, str):
        return ""
    return " ".join(text.lower().split())


def _problem_contained(shorter: str, longer: str) -> bool:
    """True when the SHORTER normalised problem appears as a CONTIGUOUS run of
    whole tokens inside the LONGER one -- a word-boundary containment, NOT a raw
    substring.

    A raw substring test (`shorter in longer`) false-refuses two ways: a terse
    problem ('fix bug') matches any longer live problem that merely contains those
    letters, and a mid-word hit ('cat' inside 'category') reads as containment. So
    require the shorter problem to be SUBSTANTIAL (>= 4 whitespace tokens) and to
    match on token boundaries. Polarity: this test gates a do-not-retry refusal, so
    it must be hard to satisfy -- a false positive silently drops distinct work."""
    short_tokens = shorter.split()
    long_tokens = longer.split()
    if len(short_tokens) < 4 or len(short_tokens) > len(long_tokens):
        return False
    span = len(short_tokens)
    for start in range(len(long_tokens) - span + 1):
        if long_tokens[start:start + span] == short_tokens:
            return True
    return False


def _ids_with_events(queue: Path, kinds: frozenset) -> set[str] | None:
    """Ids carrying ANY of `kinds` in the ledger. Returns None when the ledger
    cannot be read -- an instrument error the caller must route to WARN, never
    to a refusal (an unreadable ledger cannot PROVE an id is retired)."""
    path = queue / "ledger.jsonl"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found: set[str] = set()
    for line in raw.splitlines():
        events, _status = parse_events(line)  # shared decoder; never a private one
        for event in events:
            ident = event.get("id")
            kind = event.get("event")
            if isinstance(ident, str) and kind in kinds:
                found.add(ident)
    return found


# --------------------------------------------------------------------------
# check 4 -- dedup-against-landed  (reuses land_detect.LandDetector verbatim)
# --------------------------------------------------------------------------

# git rev-syntax metacharacters. A real branch name (per `git check-ref-format`)
# NEVER contains any of these: ':' (the <rev>:<path> blob selector), '~'/'^' (the
# ancestry navigators), '?'/'*'/'[' (refspec globs), '\\', or ASCII whitespace.
# The '@{' sequence (reflog selector) is checked separately as a two-char run.
# Left in, ':foo' would reach git as the blob-reading ref ':foo^{commit}'.
_BRANCH_FORBIDDEN_CHARS = frozenset(":~^?*[\\ \t\n\r\f\v")


def _valid_branch(branch: Any) -> tuple[str | None, str]:
    """Validate a proposal-supplied `branch` before it is interpolated into a git
    ref argv (``<branch>^{commit}``).

    Defence-in-depth companion to the _run_probe ValueError catch: a `branch` is
    read straight off the artifact and placed into an argv, so an embedded NUL (or
    a newline, or a leading '-') is a malformed ref name that must be rejected as
    an instrument error, not run. Unlike a probe REF, a branch name MAY legitimately
    contain '/' (git branches like ``lane/foo-0810``), so '/' is allowed. Rejects a
    non-str/empty value, any control character (NUL/newline/CR/other <0x20 or DEL),
    a leading '-' (option injection), a name over 200 chars, or -- round-3 (Grok
    Ga) -- any git rev-syntax metacharacter (`:~^?*[\\`, whitespace, or the `@{`
    reflog sequence) that a legal branch name never carries but that git would
    interpret as ancestry/blob/glob selectors on the `<branch>^{commit}` argv."""
    if not isinstance(branch, str) or not branch:
        return None, "not a non-empty string"
    if len(branch) > 200:
        return None, "exceeds 200 chars"
    if branch.startswith("-"):
        return None, "starts with '-' (would be read as an option)"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in branch):
        return None, "contains a control character (NUL / newline / other)"
    bad = _BRANCH_FORBIDDEN_CHARS & set(branch)
    if bad:
        ch = sorted(bad)[0]
        return None, f"contains the git rev-syntax metacharacter {ch!r}"
    if "@{" in branch:
        return None, "contains the git reflog sequence '@{'"
    return branch, ""


def _check_landed(art: dict[str, Any], roots: dict[str, Path], rep: Report) -> None:
    """necessity.landed (exit 3, do-not-retry). Fires only when the artifact
    names a `branch` + `base_sha` (the same trigger derive_paths uses) and the
    already-proven land_detect engine says the branch is CLOSABLE-landed.

    land_detect is imported LOCALLY (like content_id in check_identity) so a
    missing or drifted engine degrades to a warn-and-skip -- never a hard crash
    of the filer, and never a false refusal.
    """
    branch = art.get("branch")
    base = art.get("base_sha")
    if not (branch and base):
        return  # no ground truth to check against -- silence, not a refusal
    valid_branch, _why = _valid_branch(branch)
    if valid_branch is None:
        rep.warnings.append(
            f"necessity.landed: branch {branch!r} is not a usable ref name -- "
            "skipped (instrument error, not a finding)")
        return
    branch = valid_branch
    try:
        from land_detect import InstrumentError, LandDetector
    except ImportError as exc:  # pragma: no cover - environment failure
        rep.warnings.append(
            f"necessity.landed: land_detect unavailable ({exc}) -- "
            "dedup-against-landed skipped (instrument error, not a defect)")
        return

    root = None
    for _name, candidate in sorted(roots.items()):
        code, _out = _run_probe(
            ["git", "-C", str(candidate), "rev-parse", "--verify",
             f"{branch}^{{commit}}"], None)
        if code == 0:
            root = candidate
            break
    if root is None:
        rep.warnings.append(
            f"necessity.landed: branch {branch!r} resolves in no known root -- "
            "dedup-against-landed skipped (cannot run, not a finding)")
        return

    try:
        detector = LandDetector(root)
        detector.self_test()
        verdict = detector.classify(head=branch, branch=str(branch),
                                    number=_pr_number(art))
    except InstrumentError as exc:
        rep.warnings.append(
            f"necessity.landed: land_detect self-test/classify could not run "
            f"({exc}) -- skipped; an instrument fault is never a finding")
        return
    except Exception as exc:  # noqa: BLE001 - the engine must never crash the filer
        rep.warnings.append(
            f"necessity.landed: land_detect raised ({exc}) -- skipped")
        return

    # Only CLOSABLE landings refuse: land_detect is built to be bad at saying
    # yes, and `closable` is the only field its own docstring says an actor may
    # act on (it additionally requires a NAMED landing sha). A `landed` that is
    # not closable is exactly the case we must NOT drop work over.
    if verdict.status == "landed" and verdict.closable and verdict.landing_shas:
        sha = verdict.landing_shas[0]
        rep.refuse(
            EXIT_REFUSED_DO_NOT_RETRY, "necessity.landed",
            f"this work is already on main -- land_detect finds every touched "
            f"file contained at landing sha {sha[:12]} ({verdict.landing_kind}); "
            f"{verdict.reason}",
            f"already on main at {sha} -- drop it. If a DIFFERENT change on the "
            "same files is still needed, say what land_detect could not see and "
            "file that, which is a new payload and a new id.",
        )


def _pr_number(art: dict[str, Any]) -> int | None:
    ref = _pr_reference(art)  # the module's existing PR detector
    try:
        return int(ref) if ref is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# check 5 -- dedup-against-in-flight
# --------------------------------------------------------------------------

def _check_in_flight(art: dict[str, Any], queue: Path, rep: Report) -> None:
    """necessity.in_flight (exit 3 on mechanical certainty, else WARN).

    Walks proposals/ for still-LIVE proposals (present on disk, not retired by
    the ledger) and refuses do-not-retry ONLY on certainty: an identical
    normalised `problem`, a paths-superset whose problem statement contains (or
    is contained by) a live one, or a `supersedes` that resolves to a still-live
    proposal. Pure path overlap with a DIFFERENT problem is WARN only -- distinct
    fixes legitimately share files, and a do-not-retry there would drop real
    work.
    """
    proposals_dir = queue / "proposals"
    if not proposals_dir.is_dir():
        return
    retired = _ids_with_events(queue, _NECESSITY_RETIRED_EVENTS)
    if retired is None:
        rep.warnings.append(
            "necessity.in_flight: ledger unreadable -- cannot prove any proposal "
            "is live, so dedup-against-in-flight is skipped (not a refusal)")
        return

    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    my_id = art.get("id")
    my_problem = _normalise_problem(payload.get("problem"))
    my_paths = {os.path.normpath(p) for p in _art_paths(art)}
    my_supersedes = art.get("supersedes") or payload.get("supersedes")

    for path in sorted(proposals_dir.glob("*.json")):
        try:
            # errors="replace" matches the safe sibling _ids_with_events: a
            # neighbour .json with invalid UTF-8 must WARN-skip, not crash the
            # filer. Round-3 (Gemini G1): a bare read_text(encoding="utf-8") raises
            # UnicodeDecodeError (a ValueError, NOT an OSError/JSONDecodeError) on a
            # corrupt neighbour, which propagated out and killed every proposal
            # queued behind it. json.loads may also raise ValueError on some
            # malformed inputs, so the except catches ValueError too.
            #
            # Round-4 (Gemini F1): a pathologically deep-nested neighbour (e.g.
            # '{"a":'*2000 + '1' + '}'*2000) makes json.loads raise RecursionError,
            # which is a subclass of RuntimeError -- NOT of ValueError -- so it
            # escaped this handler, propagated out of _check_in_flight, and only the
            # top-level backstop absorbed it, silently skipping checks 4-7 for the
            # artifact. Catch RecursionError HERE so a single corrupt/pathological
            # neighbour is skipped and the loop CONTINUES (the correct polarity: one
            # bad neighbour must never abort dedup for everything queued behind it).
            # Kept narrow -- NOT bare Exception -- so a bug in our own comparison
            # logic below still surfaces to the backstop rather than being masked.
            other = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError, RecursionError):
            continue  # unreadable neighbour -- cannot compare, do not refuse on it
        if not isinstance(other, dict):
            continue
        other_id = other.get("id")
        if not isinstance(other_id, str) or other_id == my_id:
            continue  # byte-identical id is check_identity's job, not this one
        if other_id in retired:
            continue  # not live -- no competition
        if my_supersedes and other_id == my_supersedes:
            rep.refuse(
                EXIT_REFUSED_DO_NOT_RETRY, "necessity.in_flight",
                f"this draft supersedes {other_id[:19]}…, which is STILL LIVE in "
                "proposals/ -- superseding a live proposal duplicates in-flight "
                "work rather than replacing retired work",
                "drop this draft, or wait until "
                f"{other_id[:19]}… is retired (rejected/merged/completed) before "
                "re-filing its successor.",
            )
            continue
        other_payload = other.get("payload") if isinstance(other.get("payload"), dict) else {}
        other_problem = _normalise_problem(other_payload.get("problem"))
        other_paths = {os.path.normpath(p) for p in _art_paths(other)}

        if other_problem and other_problem == my_problem:
            # Path guard (polarity): an identical problem STRING is only certain
            # duplicate work when the files also overlap -- two distinct fixes on
            # DIFFERENT files can share a terse problem restatement, and freezing
            # them do-not-retry drops real work. Refuse only when the paths overlap
            # or one side declares no paths; if BOTH declare non-empty, disjoint
            # paths, downgrade to a warning and admit as distinct work.
            both_have_paths = bool(my_paths) and bool(other_paths)
            paths_overlap = bool(my_paths & other_paths)
            if both_have_paths and not paths_overlap:
                rep.warnings.append(
                    f"necessity.in_flight: live proposal {other_id[:19]}… states an "
                    "identical problem restatement but disjoint files -- admitted "
                    "as distinct work")
                continue
            rep.refuse(
                EXIT_REFUSED_DO_NOT_RETRY, "necessity.in_flight",
                f"a LIVE proposal {other_id[:19]}… states an identical problem on "
                "overlapping files -- this is duplicate in-flight work",
                f"drop it, or set supersedes: {other_id} and change the payload so "
                "this is a REPLACEMENT (a new id), not a second copy.",
            )
            continue
        superset = bool(other_paths) and other_paths <= my_paths
        contained = bool(other_problem) and bool(my_problem) and (
            _problem_contained(other_problem, my_problem)
            or _problem_contained(my_problem, other_problem))
        if superset and contained:
            rep.refuse(
                EXIT_REFUSED_DO_NOT_RETRY, "necessity.in_flight",
                f"LIVE proposal {other_id[:19]}… covers a subset of these paths and "
                "restates the same problem -- this draft is a superset duplicate of "
                "in-flight work",
                f"drop it, or set supersedes: {other_id} and change the payload.",
            )
            continue
        if my_paths and other_paths and (my_paths & other_paths) and other_problem != my_problem:
            shared = ", ".join(sorted(my_paths & other_paths)[:4])
            rep.warnings.append(
                f"necessity.in_flight: live proposal {other_id[:19]}… shares files "
                f"({shared}) but states a DIFFERENT problem -- admitted (distinct "
                "fixes may legitimately share files), but check they do not collide.")


# --------------------------------------------------------------------------
# check 6 -- dependency-satisfied
# --------------------------------------------------------------------------

def _check_dependency(art: dict[str, Any], roots: dict[str, Path],
                      queue: Path, rep: Report) -> None:
    """necessity.dependency (exit 1, fixable). For each declared dependency:
    a proposal id carrying a terminal rejected/dropped event -> refuse (building
    on abandoned work); a named base_sha no root can resolve -> refuse ONLY when
    every root's cat-file actually RAN and definitively found nothing everywhere
    (if any root query could not run -> WARN, not refuse: an instrument error is
    never a phantom finding); a dependency sha equal to current HEAD -> WARN (a
    moving HEAD sha mints a new id every filing -- the re-mint-duplicate class).
    Prose dependencies that name neither are left alone."""
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    deps = payload.get("dependencies")
    if not isinstance(deps, list) or not deps:
        return
    text = "\n".join(d for d in deps if isinstance(d, str))
    if not text.strip():
        return

    proposal_ids = set(_SHA256_ID_RE.findall(text))
    if proposal_ids:
        abandoned = _ids_with_events(queue, _NECESSITY_ABANDONED_EVENTS)
        if abandoned is None:
            rep.warnings.append(
                "necessity.dependency: ledger unreadable -- cannot check whether a "
                "dependency proposal was abandoned (skipped, not refused)")
        else:
            dead = sorted(proposal_ids & abandoned)
            if dead:
                rep.refuse(
                    EXIT_REFUSED_FIXABLE, "necessity.dependency",
                    f"{len(dead)} dependency proposal(s) were rejected/dropped: "
                    f"{', '.join(d[:19] + '…' for d in dead[:4])}",
                    "you are building on abandoned work. Pin the dependency to a "
                    "MERGED proposal id, or drop the dependency if it is no longer "
                    "needed.",
                )

    # git shas: mask out the sha256: ids first so their hex tail is not
    # re-read as a loose object name.
    masked = _SHA256_ID_RE.sub(" ", text)
    head_by_root: dict[Path, str | None] = {}
    for token in set(_GIT_SHA_RE.findall(masked)):
        # Collect every root's cat-file returncode. A phantom refusal is only
        # CERTAIN when at least one query ran AND every query returned a real
        # (non-None) code that definitively found nothing everywhere. A None code
        # means cat-file COULD NOT RUN (git missing / timeout) -- indistinguishable
        # from a genuine miss, so it must WARN, never hard-refuse on an instrument
        # error (polarity: a false dependency refusal drops real work).
        codes: list[int | None] = []
        is_head = False
        resolved = False
        for _name, root in roots.items():
            code, _out = _run_probe(
                ["git", "-C", str(root), "cat-file", "-e", f"{token}^{{commit}}"], None)
            codes.append(code)
            if code == 0:
                resolved = True
                if root not in head_by_root:
                    head_by_root[root] = _head_sha(str(root))
                head = head_by_root[root]
                if head and head.startswith(token):
                    is_head = True
                break
        if resolved and is_head:
            rep.warnings.append(
                f"necessity.dependency: dependency sha {token[:12]} is the CURRENT "
                "HEAD -- a moving HEAD sha mints a new proposal id every filing (the "
                "re-mint-duplicate class). Pin it to an immutable merged sha.")
        elif resolved:
            continue  # resolved somewhere -> the dependency exists, admit
        elif roots and codes and all(c is not None for c in codes):
            # every root query ran and definitively found nothing everywhere
            rep.refuse(
                EXIT_REFUSED_FIXABLE, "necessity.dependency",
                f"dependency names base_sha {token[:12]}, which no known root can "
                "resolve",
                "pin the dependency to a sha that exists on this estate (a merged "
                "commit) or to a proposal id -- a dependency on a phantom sha can "
                "never be satisfied.",
            )
        else:
            # at least one root query could not run (or there is no root to query)
            rep.warnings.append(
                "necessity.dependency: cat-file could not run in one or more roots "
                f"-- cannot prove sha {token[:12]} is phantom, skipped (instrument "
                "error)")


# --------------------------------------------------------------------------
# check 7 -- decision-park  (routes to a human, never parks on their behalf)
# --------------------------------------------------------------------------

def _path_tokens(path: str) -> list[str]:
    """Lowercased whole-word tokens of a path: split on '/', then split each
    segment on any non-alphanumeric run. 'configs/gates.d/x.yaml' ->
    [configs, gates, d, x, yaml]; 'omniagentos/aggregate_stats.py' ->
    [omniagentos, aggregate, stats, py]. Used for WHOLE-TOKEN self-governing
    matching so a letter-run inside an ordinary name ('aggregate', 'propagate',
    'gateway') is not mistaken for a governing token."""
    tokens: list[str] = []
    for segment in path.split("/"):
        tokens.extend(t for t in _TOKEN_SPLIT_RE.split(segment.lower()) if t)
    return tokens


def _is_self_governing(art: dict[str, Any]) -> bool:
    for path in _art_paths(art):
        if any(tok in _SELF_GOVERNING_TOKENS for tok in _path_tokens(path)):
            return True
    return False


def _has_decision_signal(art: dict[str, Any]) -> bool:
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    return bool(
        payload.get("decision") or art.get("decision")
        or payload.get("answers_inquiry")
    )


# --------------------------------------------------------------------------
# _GONE ambiguity guard -- shared "confirm presence before trusting _GONE"
# --------------------------------------------------------------------------

def _gone_is_ambiguous(spec: Any, argv: list[str], cwd: str | None,
                       rep: Report) -> bool:
    """Return True (and append a WARN) when a _GONE verdict cannot be trusted
    because the probed path is not present on the side the probe compares
    against -- an instrument-ambiguity (wrong root / nonexistent path) that must
    NOT be turned into a necessity.reproduce refusal.

    Shared by grep_present and diff_from_ref; path_absent's _GONE (exit 0 =
    ls-files matched) is unambiguous and never calls here. The presence checks
    run gate-built, read-only argvs whose only data operands are the ALREADY
    VALIDATED probe fields the gate placed into `argv` (ref via _confine_ref,
    relpath via _confine_path) -- no new unvalidated field is introduced."""
    ptype = spec.get("type") if isinstance(spec, dict) else None
    if ptype == "grep_present":
        relpath = argv[-1]
        ls_code, _ls = _run_probe(
            ["git", "--no-pager", "ls-files", "--error-unmatch", "--", relpath],
            cwd)
        if ls_code != 0:
            rep.warnings.append(
                "necessity.reproduce: probe path not present in the probed repo "
                "-- cannot distinguish \"gone\" from \"wrong root\", skipped")
            return True
        return False
    if ptype == "diff_from_ref":
        # argv == [git, --no-pager, diff, --quiet, --no-ext-diff, ref, --, relpath]
        ref, relpath = argv[-3], argv[-1]
        # Present at <ref>? cat-file -e <ref>:<relpath>. ref/relpath are the
        # already-validated fields the gate wrote into argv (ref has no ':' or
        # metachars, relpath is repo-confined), passed only as fixed operands.
        at_ref, _r = _run_probe(
            ["git", "--no-pager", "cat-file", "-e", f"{ref}:{relpath}"], cwd)
        # Tracked in the worktree?
        in_tree, _t = _run_probe(
            ["git", "--no-pager", "ls-files", "--error-unmatch", "--", relpath],
            cwd)
        if at_ref != 0 and in_tree != 0:
            rep.warnings.append(
                f"necessity.reproduce: the diff_from_ref path is absent from both "
                f"{ref} and the working tree -- cannot distinguish 'already "
                "reconciled' from 'wrong root/nonexistent path', skipped "
                "(instrument ambiguity, not a finding).")
            return True
        return False
    return False


# --------------------------------------------------------------------------
# the orchestrator
# --------------------------------------------------------------------------

def _necessity_check_body(art: dict[str, Any], queue: Path,
                          roots: dict[str, Path], rep: Report,
                          before: int) -> None:
    """The seven checks, run in order. Extracted so `check_necessity` can wrap
    the whole sequence in one narrow instrument-fault guard (Sweep A backstop)
    without indenting the body. Any exception raised here is caught by the caller
    and turned into a WARNING, never a crash and never a refusal."""
    needs = _needs_probe(art)

    # --- C-ii mislabel-smell receipt (observability only, NEVER a refusal) -----
    # When the proposal is EXEMPT from the reproduce-first probe AND its change
    # class is ABSENT/UNRECOGNIZED (matched neither the bug nor the exempt regex)
    # AND its problem/title talks like a defect, nudge the planner to declare it.
    # A DECLARED exempt class stays quiet (the accepted residual). This admits in
    # every case -- it forces nothing and drops nothing.
    if not needs:
        cls = _proposal_class(art)
        class_unrecognized = not (cls and (_BUG_CLASS_RE.search(cls)
                                           or _EXEMPT_CLASS_RE.search(cls)))
        if class_unrecognized and _MISLABEL_SMELL_RE.search(_problem_text(art)):
            rep.warnings.append(
                "necessity: this reads like a bug (defect-language in the problem "
                "statement) but declares no bug class and carries no "
                "necessity_probe -- admitted; if it IS a defect, declare "
                "risk_class=bug and add a necessity_probe so the gate can verify "
                "it is not already solved.")

    # --- checks 1-3: the reproduce-first probe (bug/reproducible class only) ---
    if needs:
        specs = _necessity_probe_specs(art)
        if len(specs) != 1:
            if not specs:
                message = ("no structured `necessity_probe` is present -- nothing "
                           "here PROVES the problem is present on HEAD")
            else:
                message = (f"{len(specs)} `necessity_probe` specs are present; "
                           "exactly one is required so the filer knows which "
                           "single proof to run")
            rep.refuse(
                EXIT_REFUSED_FIXABLE, "necessity.unproven", message,
                "add exactly one structured `necessity_probe` -- a typed object "
                f"{{type: one of {', '.join(_PROBE_TYPES)}, ...}} whose exit code "
                "means the problem is PRESENT. The filer builds the read-only argv "
                "itself and RE-RUNS it against HEAD; there is no command string to "
                "write. A feature/docs/refactor proposal that declares its "
                "risk_class/kind is exempt from this check.",
            )
        else:
            argv, why, interpret = _build_probe(specs[0])
            if argv is None:
                rep.refuse(
                    EXIT_REFUSED_FIXABLE, "necessity.probe_unsafe",
                    f"the necessity probe is invalid: {why}",
                    "a probe is a TYPED spec, not a command: pick a `type` from "
                    f"{', '.join(_PROBE_TYPES)} and give repo-relative, "
                    "shell-metacharacter-free fields. The gate builds the argv, so "
                    "no binary, flag, or shell trick can be supplied here.",
                )
            else:
                cwd = _probe_cwd(roots)
                if cwd is None:
                    rep.warnings.append(
                        "necessity.reproduce: no repo root to run the probe in -- "
                        "skipped, not refused (an instrument error is never a "
                        "finding).")
                else:
                    code, output = _run_probe(argv, cwd)
                    if code is None:
                        rep.warnings.append(
                            f"necessity.reproduce: the probe could not run ({output}) "
                            "-- skipped, not refused (an instrument error is never a "
                            "finding).")
                    else:
                        verdict = interpret(code)
                        if verdict == _PROBE_ERR:
                            rep.warnings.append(
                                f"necessity.reproduce: the probe returned an "
                                f"uninterpretable exit {code} ({output}) -- skipped, "
                                "not refused (an instrument error is never a finding).")
                        elif verdict == _GONE:
                            # A _GONE verdict certifies "already solved" ONLY when
                            # the probed path actually exists on the side the probe
                            # compares against. If it is present in NEITHER the
                            # worktree NOR (for diff_from_ref) the <ref>, the exit
                            # code is AMBIGUOUS with a wrong-root / nonexistent-path
                            # run, so WARN-and-skip rather than refuse (polarity:
                            # instrument ambiguity is never a finding, a false
                            # necessity.reproduce silently DROPS real work).
                            #
                            #   * grep_present exit 1: literal genuinely gone, OR the
                            #     path is not in this cwd (wrong root) -- both exit 1.
                            #     Confirm the path is TRACKED here before refusing.
                            #   * diff_from_ref exit 0 (Round-4, Gemini F2): identical
                            #     to <ref>, OR the path exists in NEITHER the worktree
                            #     NOR <ref> (git diffs nothing-vs-nothing -> exit 0)
                            #     -- both exit 0. Confirm the path is present on at
                            #     least ONE side (tracked in the worktree, or present
                            #     at <ref>) before refusing.
                            #   * path_absent exit 0 is unambiguous (ls-files matched
                            #     -> the path is definitely tracked), so it is NOT
                            #     guarded here.
                            skip_gone = _gone_is_ambiguous(specs[0], argv, cwd, rep)
                            if skip_gone:
                                pass  # a warning was already appended by the guard
                            else:
                                head = _head_sha(cwd) or "unknown"
                                rep.refuse(
                                    EXIT_REFUSED_FIXABLE, "necessity.reproduce",
                                    f"the problem does NOT reproduce on current HEAD "
                                    f"{head[:12]}: the {specs[0].get('type')} probe "
                                    f"now exits {code}, which means the problem is "
                                    "already solved.",
                                    "re-verify against this HEAD. If the problem is "
                                    "genuinely gone it is already solved -- drop the "
                                    "plan. If the probe is WRONG, fix it; that changes "
                                    "nothing hashed but the plan is moot without a "
                                    "reproducing probe.",
                                )
                        # else verdict == _REPRO -> the problem is present, admit.

    # --- checks 4-6: independent of the probe --------------------------------
    _check_landed(art, roots, rep)
    _check_in_flight(art, queue, rep)
    _check_dependency(art, roots, queue, rep)

    # --- check 7: only if nothing above fired --------------------------------
    # Round-3 (Gemini G2): the guard is `not needs`, NOT the old
    # `not probe_reproduced`. A defect-ASSERTING proposal (bug class, or one that
    # declares a probe / falsifier -- exactly what `_needs_probe` reports) is never
    # "undecided": it makes a reproducibility CLAIM, so if its probe merely
    # INSTRUMENT-ERRORED (code=None -> warn-skip, leaving the old flag False) it
    # must still be ADMITTED, not false-routed to necessity.undecided. Only a
    # proposal that needs no probe at all AND touches a self-governing surface AND
    # carries no decision signal is a genuine direction call for a human.
    if (len(rep.refusals) == before and not needs
            and _is_self_governing(art) and not _has_decision_signal(art)):
        rep.refuse(
            EXIT_REFUSED_FIXABLE, "necessity.undecided",
            "this change touches a self-governing surface (gate / schema / prompt "
            "/ approval), its probe checks passed, but nothing here establishes a "
            "reproducible DEFECT -- a change to how the system governs itself is a "
            "direction call, and the filer does not decide direction",
            "file this as an `inquiry` for an operator ruling (the cheap reverse "
            "edge), or attach a payload.decision block per Step 4c stating what is "
            "being committed to, what it costs, and what you would do if refused.",
        )


def check_necessity(art: dict[str, Any], queue: Path, roots: dict[str, Path],
                    rep: Report) -> None:
    """Pre-file necessity gate -- proves a well-shaped proposal is still NEEDED.

    Runs after check_identity on the SAME rep accumulator (exit_code = max());
    it never early-returns out of main() and never reorders the existing checks.
    Order is cheapest-and-most-certain first:

        1 necessity.unproven      (exit 1) exactly one STRUCTURED necessity_probe
                                           (bug/reproducible class only)
        2 necessity.probe_unsafe  (exit 1) probe type/fields fail validation
        3 necessity.reproduce     (exit 1) build the probe's fixed read-only argv
                                           and run it; refuse if the problem no
                                           longer reproduces on HEAD
        4 necessity.landed        (exit 3) land_detect: already on main
        5 necessity.in_flight     (exit 3) duplicate of a live proposal
        6 necessity.dependency    (exit 1) built on abandoned/phantom work
        7 necessity.undecided     (exit 1) self-governing surface, no defect --
                                           route to a human

    Checks 1-3 apply ONLY to the bug/reproducible class (see _needs_probe); a
    declared feature/docs/refactor proposal is exempt from them but still gets
    4-7. Checks 2-3 depend on a valid probe from check 1. Checks 4-6 are
    independent. Check 7 fires only if checks 1-6 added no refusal. See the
    module-top POLARITY note: every skip here is a WARN, never a fail-closed
    refusal, and there is NO filer-supplied command anywhere -- the gate builds
    every probe argv itself, so the whole command-injection surface is gone by
    construction.
    """
    before = len(rep.refusals)
    # Round-3 Sweep A backstop: the whole seven-check body runs under one narrow
    # instrument-fault guard. Every field read is already defensively typed
    # (`_art_paths`, the payload `isinstance` guards, `_valid_branch`, the probe
    # validators) and every on-disk / subprocess site routes its own errors to a
    # WARN. This guard is the last line: if ANY art field of ANY type still
    # manages to raise, the necessity gate WARNS and skips rather than propagating
    # the exception to the filer. It never silently swallows -- a warning is always
    # emitted -- and, by POLARITY, an instrument fault is never turned into a
    # refusal. Deterministic refusals recorded on `rep` before a raise stay put:
    # they are certain findings, not the fault. (MemoryError / KeyboardInterrupt
    # are BaseException, deliberately NOT caught -- those are facts about the host,
    # not about the artifact, and must reach the filer unmasked.)
    try:
        _necessity_check_body(art, queue, roots, rep, before)
    except Exception as exc:  # noqa: BLE001 - the gate must never crash the filer
        rep.warnings.append(
            f"necessity: an unexpected internal error ({type(exc).__name__}: "
            f"{exc}) -- the necessity gate is skipped for this artifact (an "
            "instrument fault is never a finding, never a refusal).")


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

# Interpreter discovery lives in ONE place (bridge/validate_envelope.py) and
# is imported here rather than duplicated — three copies of this probe is
# exactly this codebase's #1 recurring defect pattern (one bug becomes
# 2-13 across clone families), and it already cost a real bug once: an
# earlier duplicated copy caught only OSError, so a hanging candidate
# interpreter escaped as an uncaught subprocess.TimeoutExpired identically
# in all three copies. `sys.path` already carries this file's own directory
# (see HERE, above) so `from validate_envelope import ...` resolves the same
# way `check_schema` below already resolves `_build_validator`.
def check_schema(art: dict[str, Any], rep: Report) -> None:
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - environment failure
        from validate_envelope import _no_conforming_interpreter_hint
        print(f"jsonschema is not importable — cannot validate ({exc}). "
              f"{_no_conforming_interpreter_hint(Path(__file__).resolve(), sys.argv[1:])}",
              file=sys.stderr)
        raise SystemExit(EXIT_COULD_NOT_RUN) from exc
    from validate_envelope import _build_validator

    validate, _note = _build_validator()
    try:
        validate(art)
    except jsonschema.ValidationError as exc:
        loc = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        rep.refuse(EXIT_REFUSED_FIXABLE, "schema", f"{loc}: {exc.message}",
                   "see schema/envelope.schema.json")


# --------------------------------------------------------------------------
# the atomic write
# --------------------------------------------------------------------------

def atomic_write(path: Path, obj: dict[str, Any]) -> None:
    """UNIQUE tmp in the SAME directory -> fsync -> EXCLUSIVE link -> fsync dir.

    The loops are long-running and read this directory while it is written, and
    more than one producer can race to publish the same id. ``os.link`` refuses
    an existing target with EEXIST where ``rename`` would silently overwrite —
    so exactly ONE publisher wins, which is what "artifacts are immutable after
    creation" (CONTRACT §2) requires. The loser gets ``FileExistsError`` and
    must report a duplicate, not a success.

    The temp name is minted by ``mkstemp`` (kernel-unique), NOT ``os.getpid()``:
    two writers in ONE process share a pid, so a pid-named temp lets the second
    clobber the first's bytes before the link — a same-process re-run of the
    cross-process race this function exists to stop.

    Atomicity to the caller: if the post-link directory fsync fails, the target
    is unlinked before re-raising, so the caller never sees a half-durable
    artifact it then fails to record.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)          # EEXIST if someone else already published
    finally:
        os.unlink(tmp)
    try:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        # The link is done but not durably indexed; undo it so the filing is
        # all-or-nothing from the caller's point of view.
        try:
            os.unlink(path)
        finally:
            raise


class LedgerNotDurable(RuntimeError):
    """os.write of the event LANDED, but the fsync that would make it durable
    failed. The event is very likely in the file already, so rolling the
    artifact back would risk a ledger line pointing at nothing (a phantom).
    The caller keeps the artifact and warns about durability instead."""


def _append_ledger_line(queue: Path, line: str) -> None:
    """Append a prebuilt event through the shared ledger transport.

    The distinction the caller's rollback depends on: a plain ``OSError`` means
    the bytes NEVER landed (safe to roll the artifact back), while
    ``LedgerNotDurable`` means the bytes DID land but durability is unconfirmed
    (rolling back would orphan a ledger line pointing at nothing). So once
    ``os.write`` returns, EVERY later failure — a failed ``fsync`` OR a failed
    ``close`` (which can surface a deferred write error) — is LedgerNotDurable,
    never a rollback-triggering OSError."""
    try:
        append_event(queue, json.loads(line))
    except LedgerAppendError as exc:
        if exc.bytes_written:
            raise LedgerNotDurable(str(exc)) from exc
        raise


def append_ledger(queue: Path, art: dict[str, Any]) -> None:
    line = json.dumps({
        "ts": _now(),
        "role": (art.get("producer") or {}).get("role", "planner"),
        "event": "proposed",
        "id": art["id"],
        "actor": (art.get("producer") or {}).get("actor", "file_proposal.py"),
        "detail": {"title": art.get("title", ""), "paths": len(art.get("paths") or [])},
    }, separators=(",", ":")) + "\n"
    _append_ledger_line(queue, line)


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Validate and atomically file a proposal. Refuses rather than "
                    "writing a degraded artifact.")
    ap.add_argument("draft", help="path to the draft proposal JSON")
    ap.add_argument("--queue", default=os.environ.get("LOOPQUEUE") or
                    str(Path.home() / "OmniAgentOS" / "var" / "loopqueue"))
    ap.add_argument("--root", action="append", default=[],
                    help="extra checkout as PATH or NAME=PATH (repeatable)")
    ap.add_argument("--check", action="store_true", help="validate only; write nothing")
    ap.add_argument("--derive", action="store_true",
                    help="take `paths` from the branch/PR diff when the artifact "
                         "names one; refuses if it cannot derive")
    ap.add_argument("--no-ledger", action="store_true",
                    help="refused: a filing without its 'proposed' event is "
                         "invisible to every status reader (use --check to "
                         "validate only)")
    args = ap.parse_args(argv)

    if args.no_ledger:
        # Consistent with file_inquiry.py: status is ledger-derived (§8), so an
        # artifact without its event is a contract-invalid invisible filing.
        print(f"REFUSE[{EXIT_COULD_NOT_RUN}] no-ledger: an artifact without its "
              "'proposed' event is invisible to every status reader. Use --check "
              "to validate without writing.", file=sys.stderr)
        return EXIT_COULD_NOT_RUN

    try:
        art = json.loads(Path(args.draft).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"REFUSE[{EXIT_COULD_NOT_RUN}] draft: cannot read {args.draft}: {exc}",
              file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    if not isinstance(art, dict):
        print(f"REFUSE[{EXIT_COULD_NOT_RUN}] draft: not a JSON object", file=sys.stderr)
        return EXIT_COULD_NOT_RUN

    queue = Path(args.queue).expanduser()
    roots = default_roots()
    extra_root_names: list[str] = []
    for spec in args.root:
        name, path = parse_root(spec)
        roots[name] = path
        extra_root_names.append(name)
    landable = landable_root_names(roots, extra_root_names)

    rep = Report()

    if art.get("kind") != "proposal":
        print(f"REFUSE[{EXIT_COULD_NOT_RUN}] kind: this tool files proposals; got "
              f"{art.get('kind')!r}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN

    truth, how = derive_paths(art, roots)
    if args.derive:
        if truth is None:
            print(f"REFUSE[{EXIT_REFUSED_FIXABLE}] derive: {how}\n"
                  f"         REMEDY: --derive is for plans bound to real work. "
                  f"Without a branch or PR there is nothing to derive FROM, and "
                  f"writing an empty paths anyway is the degraded write this tool "
                  f"exists to prevent — enumerate the files instead.",
                  file=sys.stderr)
            return EXIT_REFUSED_FIXABLE
        art["paths"] = sorted(truth)
        art.setdefault("paths_derived_from", how)

    check_schema(art, rep)
    normalised = check_paths(art, roots, landable, rep)
    if truth is not None and normalised:
        check_against_truth(normalised, truth, how, rep)
    check_identity(art, queue, rep)
    check_necessity(art, queue, roots, rep)

    # Contract-lens quality checks — WARN-ONLY by ruling (see contract_lens.py:
    # refusing on house doctrine needs a contract version bump or an operator
    # adoption; until then the lens teaches at filing time and never blocks).
    # A lens crash must not block a filing either — warn and continue.
    try:
        from contract_lens import lens_warnings
        rep.warnings.extend(lens_warnings(art))
    except Exception as exc:  # noqa: BLE001 — the lens is advisory by ruling
        rep.warnings.append(f"lens.error: contract lens crashed ({exc}) — "
                            "warnings unavailable for this filing")

    for warning in rep.warnings:
        print(f"warn: {warning}", file=sys.stderr)

    if rep.refusals:
        for refusal in rep.refusals:
            print(refusal.render(), file=sys.stderr)
        print(f"\nNOT WRITTEN. {len(rep.refusals)} refusal(s); exit {rep.exit_code}.",
              file=sys.stderr)
        return rep.exit_code

    art["paths"] = normalised
    art["paths_resolved"] = rep.resolved
    art.setdefault("contract", "v1.1")

    if args.check:
        print(f"OK would write {art['id'][:19]}… with {len(normalised)} path(s)")
        return EXIT_WRITTEN

    target = queue / "proposals" / f"{_stem(art['id'])}.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, art)
    except FileExistsError:
        print(f"REFUSE[{EXIT_REFUSED_DO_NOT_RETRY}] id.duplicate: another producer "
              f"published {art['id'][:19]}… between the duplicate check and the "
              f"write — the artifact on disk wins; this filing is a duplicate.",
              file=sys.stderr)
        return EXIT_REFUSED_DO_NOT_RETRY
    except OSError as exc:
        print(f"REFUSE[{EXIT_COULD_NOT_RUN}] write: could not publish to {target}: "
              f"{exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    try:
        append_ledger(queue, art)
    except (LedgerNotDurable, OSError) as exc:
        # Decide rollback on OBSERVED ledger state, never on the exception type:
        # a naive append can raise OSError after its bytes are durable, and
        # rolling back then orphans a real ledger line (a phantom). Roll back
        # ONLY on a confirmed ABSENT — an UNKNOWN (unreadable ledger) is kept,
        # because a rollback we cannot justify is how the phantom is created.
        state = _ledger_event_state(queue, art["id"], "proposed")
        if state == LEDGER_EVENT_ABSENT:
            rolled_back = _rollback(target)
            print(_ledger_fail_message(exc, target, queue, rolled_back), file=sys.stderr)
            return EXIT_COULD_NOT_RUN
        detail = ("is on disk" if state == LEDGER_EVENT_PRESENT
                  else "could not be confirmed (ledger unreadable)")
        print(f"warn: ledger: the 'proposed' event {detail} but the append "
              f"reported an error ({exc}); the artifact is KEPT (a rollback that "
              f"cannot confirm absence would risk a phantom). Verify the ledger.",
              file=sys.stderr)
    print(f"OK wrote {target}  ({len(normalised)} paths)")
    return EXIT_WRITTEN


if __name__ == "__main__":
    sys.exit(main())
