#!/usr/bin/env python3
"""Make the parallel-builder directive EXECUTABLE, not prose.

PROMPT-implementer-loop.md's "Build in PARALLEL" section (2026-08-08) tells the
coordinating loop to spawn one builder subagent per claim, up to `wip_cap`,
each in its own isolated worktree. Measured result: ZERO builder subagent
spawns across 18 sessions / 1,330 tool calls — a directive stated in prose
that nothing ever executed. This module is the one command that does the
fan-out mechanically, so "spawn a builder" is a subprocess call the loop
makes, not a paragraph it is supposed to remember.

What it does, per invocation:

  1. Reads ``<loops-root>/proposals/*.json`` directly and selects
     admitted-unclaimed items: well-formed proposal artifacts with no live
     claim marker under ``claims/`` and no entry under ``parked/`` or a
     ``parked`` ledger event with no later ``unparked``.

     **Round-1 review trap (2026-08-10, do not repeat): this module used to
     read ``state/advice.json``'s ``would_admit`` list.** That list is built
     by ``bridge/integration.py`` (see ``cdir = root / "candidates"`` in
     ``_gather_candidates``) from ``candidates/*.json`` — ALREADY-BUILT
     candidate envelopes queued for the merge gate, not proposals awaiting a
     builder. ``integration.py`` says so itself in its own summary
     (``proposals_note``: "this adapter does NOT admit proposals"). Reading
     it here handed builder subagents already-complete candidates dressed
     up as build assignments, and its ``paths`` field is an INT (a count,
     ``len(c.paths)``), not a list — the second defect that produced. Never
     read ``would_admit`` for this purpose again; select from
     ``proposals/`` directly, as this file now does.

  2. Acquires a claim for each selected item by IMPORTING ``bridge/claim.py``
     — **NON-NEGOTIABLE (Kimi ruling, 2026-08-08): this module never writes
     ``claims/*.claim`` itself.** ``claim.py`` is the only sanctioned writer;
     a second writer, even a careful one, is the exact defect CONTRACT.md §6
     closed (7 live hand-written markers, one that outlived its own
     ``released`` ledger event).
  3. Provisions one disposable git worktree per claim, following the same
     ``git worktree add`` convention ``bridge/gate_loop.py`` already uses for
     its own scratch/mint worktrees.
  4. Writes one exact per-builder brief into that worktree: objective, owned
     paths (from the item), the mechanical test command, and the git
     discipline rules (branch, never touch main, never claim, never append
     the ledger).
  5. On a later ``--harvest`` pass, reads the marker each provisioned
     worktree carries, and — for one whose branch has commits beyond its
     base — writes a schema-shaped (``schema/envelope.schema.json``,
     ``kind: candidate``) envelope into ``var/loopqueue/candidates/``, reusing
     the same "build an envelope dict, hash the payload with
     ``bridge/canonical.content_id``" shape ``bridge/github_bridge.py``
     already uses, rather than inventing a second envelope constructor.
  6. Self-reports: if this iteration acquired more than one claim but ended
     up provisioning zero builder worktrees, that IS the directive going
     inert again — it appends a ``directive_inert`` ledger event using the
     same append-only writer ``bridge/claim.py`` uses for its own events
     (matching the ``{ts, role, event, id, actor, detail}`` shape
     ``bridge/integration.py`` writes throughout).

FAIL CLOSED throughout: an unreadable/malformed proposal, a claim that
cannot be acquired, or a worktree that cannot be provisioned skips that one
item with an alert/ledger event — never a crash, never a claim left held
with nothing provisioned for it. A failure AFTER the claim and worktree
exist (writing the brief/marker) releases the claim and best-effort removes
the worktree it just opened, rather than aborting the whole iteration and
silently skipping every later item (round-1 review, F3).

Usage:
    spawn_builders.py --loops-root var/loopqueue --repo . --dry-run
    spawn_builders.py --loops-root var/loopqueue --repo .
    spawn_builders.py --loops-root var/loopqueue --repo . --harvest
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT))

# The sanctioned claim writer. NON-NEGOTIABLE: this module never open()s or
# unlink()s anything under `claims/` itself -- every acquire/release goes
# through these two calls.
from bridge import canonical as _canonical  # noqa: E402
from bridge import claim as _claim  # noqa: E402
from bridge.integration import LedgerView as _LedgerView  # noqa: E402

try:  # optional -- a brief without traps is still a valid brief
    from bridge.known_traps import load_rejections as _load_rejections
    from bridge.known_traps import traps_block as _traps_block
except Exception:  # pragma: no cover -- defensive, matches claim.py's own posture on notify
    _traps_block = None
    _load_rejections = None

try:  # optional -- lessons are advisory and must never block brief minting
    from omniagentos.knowledge.brief_recall import recall_lessons as _recall_lessons
    from omniagentos.knowledge.config import knowledge_enabled as _knowledge_enabled
except Exception:  # pragma: no cover -- dependency/config failure is handled at call time
    _recall_lessons = None
    _knowledge_enabled = None

_LOG = logging.getLogger(__name__)

ROLE = "implementer"
ACTOR = "spawn_builders"
#: The mechanical pass-list is ALWAYS run through the repo's OWN venv
#: interpreter, NEVER the ambient ``python3`` -- which on this estate has no
#: pytest installed, so ``python3 -m pytest`` exits non-zero for every candidate
#: and the harvester silently drops real, passing builds as ``harvest_red``.
#: That was the measured DROP-TRAP (2026-08-11). There is therefore deliberately
#: no module-level ``python3 -m pytest`` default any more: the effective command
#: is resolved per repo/candidate at harvest time by preferring the builder's
#: OWN recorded command (``.spawn-item.json`` marker), then the repo venv
#: interpreter, and if NEITHER can be resolved the harvest ERRORS loudly
#: (``alert_once``) rather than run the wrong interpreter and drop the build.
_PYTEST_TAIL = "-m pytest -q"
DEFAULT_PRIORITY = 2  # CONTRACT.md's own default for an absent `priority` field
MARKER_NAME = ".spawn-item.json"
BRIEF_NAME = "BUILDER_BRIEF.md"
ACK_NAME = ".builder-started.json"
ACK_GRACE_SECONDS = 120

#: The content-address shape of a PROPOSAL id. It is a real integrity check
#: only where the id must equal `canonical.content_id(payload)` (a short or
#: descriptive id can never satisfy that), so it belongs on proposals and on
#: the markers this module writes from them -- and NOT on `parked/`, which is
#: id-keyed rather than content-addressed and whose live artifacts legitimately
#: use short/descriptive/namespaced ids. See `_queue_state`.
_SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class _SelectionRefused(RuntimeError):
    """Queue state cannot be proved safe enough to select from."""


class _RegistryUnreadable(RuntimeError):
    """Git worktree registry could not prove a debris path unregistered."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(ident: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in ident)[-40:].strip("-") or "item"


def _venv_python(repo: Path) -> Path | None:
    """The repo's OWN venv interpreter (``<repo>/.venv/bin/python``), or
    ``None`` if the repo has no venv. This -- not the ambient ``python3`` --
    is the interpreter the mechanical pass-list must run under, because the
    ambient interpreter on this estate has no pytest and would fail-CLOSED
    every candidate's verify."""
    candidate = repo / ".venv" / "bin" / "python"
    return candidate if candidate.exists() else None


def _venv_test_cmd(repo: Path) -> str | None:
    """The venv-default mechanical pass-list for ``repo``, or ``None`` when the
    repo has no venv to run it under. Quoted so a venv path with spaces still
    ``shlex.split``s back to a single interpreter argument."""
    py = _venv_python(repo)
    return f"{shlex.quote(str(py))} {_PYTEST_TAIL}" if py is not None else None


# --------------------------------------------------------------------------
# coordinator-authored, builder-IMMUTABLE test-command record (F1, 2026-08-11
# cross-lineage rework). The per-builder `.spawn-item.json` marker lives INSIDE
# the builder-writable worktree, so a builder could overwrite the coordinator's
# intended (possibly failing) command with a no-op and mint a false-green
# candidate. The verification command the harvester runs must therefore come
# from a source the builder cannot reach: this record, written by the
# coordinator at provision time under `<loops-root>/state/`, which no builder
# worktree ever writes to. The marker's own `test_cmd` is kept for the brief and
# for humans, but is NEVER read back as the authoritative verification command.
# --------------------------------------------------------------------------

_TESTCMD_DIRNAME = "builder-testcmd"


def _testcmd_record_path(loops_root: Path, ident: str) -> Path:
    return loops_root / "state" / _TESTCMD_DIRNAME / f"{ident.replace(':', '_', 1)}.json"


def _write_testcmd_record(loops_root: Path, ident: str, test_cmd: str | None) -> None:
    """Persist the coordinator's intended pass-list for ``ident`` where no
    builder can tamper with it. ``None`` (no explicit command and no repo venv)
    is recorded honestly so the harvester fails closed rather than guess."""
    path = _testcmd_record_path(loops_root, ident)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"id": ident, "test_cmd": test_cmd, "at": _iso(_now())}, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _trusted_test_cmd(loops_root: Path, ident: str) -> str | None:
    """The coordinator-recorded verification command for ``ident``, or ``None``
    when there is no trusted STRING command to run (record absent, unreadable,
    or recorded as ``None``). An unreadable record is treated as absent, not as
    a favourable default -- the caller then falls to the repo venv command and,
    failing that, fails closed. NEVER reads the builder-writable marker."""
    path = _testcmd_record_path(loops_root, ident)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    recorded = body.get("test_cmd")
    return recorded if isinstance(recorded, str) else None


# --------------------------------------------------------------------------
# proposal selection -- reads proposals/*.json directly, never advice.json's
# would_admit (F1 above). Fail closed on anything malformed: skip + alert,
# never a crash, never a favourable "treat unreadable as absent".
# --------------------------------------------------------------------------


def _valid_paths(item: dict) -> list[str] | None:
    """Non-empty list of non-empty strings, or ``None``. Used both to admit
    a proposal in the first place and, defensively, again right before a
    provisioned worktree is used -- so a shape defect can never reach a bare
    ``for p in paths`` and raise ``TypeError`` (F2, round-1 review: a prior
    version fed ``write_brief``/``_envelope`` an INT here and crashed)."""
    paths = item.get("paths")
    if not isinstance(paths, list) or not paths:
        return None
    if not all(isinstance(p, str) and p for p in paths):
        return None
    return paths


def _park_ident_from_name(name: str) -> str:
    """The id a park sidecar's FILENAME names.

    Exactly the derivation ``bridge/janitor.py`` (``_parked_ids``) and
    ``bridge/integration.py`` already use for this same directory: the stem,
    with the first ``_`` read back as ``:``. No format check, because neither
    of those readers applies one either.

    Kept byte-identical to the sibling readers ON PURPOSE, so this module can
    be compared against them. It is NOT the exclusion key on its own --
    ``_park_idents_from_name`` is, and the difference is a BLOCKER, not a
    nicety: see there."""
    stem = name[: -len(".json")] if name.endswith(".json") else name
    return stem.replace("_", ":", 1)


#: The park-reason sidecar's infix. ``sha256_<id>.parkinfo.json`` is a
#: SECOND carrier for the same park, not a park of a different id.
_PARKINFO_SUFFIX = ".parkinfo"


def _park_idents_from_name(name: str) -> set[str]:
    """EVERY id a park sidecar's filename legitimately names.

    Why this exists as a set, and why it is the exclusion key (2026-08-11,
    closing a cross-lineage BLOCKER on the first cut of this fix):
    ``_park_ident_from_name`` strips only ``.json``, so the park-reason
    carrier ``sha256_<id>.parkinfo.json`` derives ``sha256:<id>.parkinfo`` --
    an id no proposal ever has. If that sidecar's BODY id is also stale or
    wrong (which the relaxed body check above now tolerates by design), then
    neither the name key nor the body key matches the genuinely parked
    proposal, and a builder is handed work a human parked. That is the ONE
    outcome this module must never produce, and over-exclusion is free.

    So a ``.parkinfo`` name contributes BOTH the literal derivation and the
    bare id beneath it. Everything else contributes just itself."""
    ident = _park_ident_from_name(name)
    idents = {ident}
    if ident.endswith(_PARKINFO_SUFFIX):
        idents.add(ident[: -len(_PARKINFO_SUFFIX)])
    return {i for i in idents if i}


def _park_filenames(ident: str) -> set[str]:
    """Filenames that legitimately carry ``ident``, per the shapes the live
    ``parked/`` directory actually uses (measured 2026-08-11, 64 sidecars):

      * ``sha256_<id>.json`` — the canonical stem, first ``:`` as ``_``;
      * ``<slug>.json`` for a namespaced id like
        ``parked/billing-harm-c36bb80d53-0810`` (the ``parked/`` prefix cannot
        appear in a filename, so the basename is what lands on disk);
      * either of the above with a ``.parkinfo`` infix — the park-reason
        sidecar written alongside a park.

    Used ONLY to decide whether a name/body pair is worth alerting about. It
    never gates exclusion: a name outside this set still parks both ids."""
    stems = {ident.replace(":", "_", 1)}
    if "/" in ident:
        stems.add(ident.rsplit("/", 1)[-1])
    return ({f"{stem}.json" for stem in stems}
            | {f"{stem}.parkinfo.json" for stem in stems})


def _queue_state(loops_root: Path, *, persist_alerts: bool = False,
                 alerts: list[str] | None = None) -> tuple[_LedgerView, set[str]]:
    """Use Integration's exact terminal/park replay and fail closed on holes.

    **Park sidecars EXCLUDE conservatively; they never abort the selection
    (2026-08-11).** This function used to require every file in ``parked/``
    to carry a full ``sha256:<64 hex>`` id agreeing exactly with its filename,
    and to raise ``_SelectionRefused`` — which aborts the WHOLE fan-out — for
    any that did not. Measured against the live queue: 24 of 64 sidecars use
    the estate's other id conventions (short hex ``sha256:043b77fe3ac43``,
    descriptive ``sha256:loop-accounts-weekly-exhausted-20260810``, and
    ``parked/``-namespaced slugs), all written by the planner and by earlier
    implementer iterations, and all accepted by ``bridge/claim.py``,
    ``bridge/janitor.py`` and ``bridge/integration.py``. So ONE ordinary
    artifact took every builder to zero: 44 otherwise-selectable proposals
    built nothing. Refusing all selection protects NOTHING here — the point of
    reading ``parked/`` is to keep a parked item away from a builder, and a
    readable sidecar tells you exactly what it names.

    The safe direction is therefore to over-exclude, not to abort: both the
    body id and the filename-derived id go into the parked set, so the item is
    excluded whichever key the proposal uses. Over-excluding costs one skipped
    build; under-excluding could hand a builder something a human parked.

    What STILL refuses, and why — a sidecar you cannot INTERPRET is not the
    same as one whose id you do not like:

      * unreadable / corrupt JSON, and a body that is not a JSON object: there
        is no id to read, so the set of things this file parks is unknown and
        selection cannot be proved safe;
      * a readable object with no usable string ``id``: same hole. The
        filename alone is not enough — a sidecar with a body that declines to
        name its subject may well be parking something other than its own
        stem, and guessing in the favourable direction is the exact failure
        mode this queue is built to refuse.

    A filename/body DISAGREEMENT is not in that list: both ids are known, so
    both are parked and the oddity is alerted once (``alert_once``, matching
    ``_open_proposals``' posture on a permanently-odd file)."""
    try:
        view = _LedgerView.build(loops_root)
    except (OSError, UnicodeError, ValueError) as exc:
        raise _SelectionRefused(
            f"ledger state unreadable ({type(exc).__name__}: {exc})") from exc
    if view.torn_tail or view.discarded:
        raise _SelectionRefused(
            f"ledger state incomplete (torn_tail={view.torn_tail}, "
            f"discarded={view.discarded})")

    parked = set(view.parked)
    parked_dir = loops_root / "parked"
    if parked_dir.exists():
        if not parked_dir.is_dir():
            raise _SelectionRefused("parked/ exists but is not a directory")
        try:
            entries = sorted(parked_dir.iterdir())
        except OSError as exc:
            raise _SelectionRefused(
                f"parked/ unreadable ({type(exc).__name__}: {exc})") from exc
        for path in entries:
            if not path.is_file() or path.suffix != ".json":
                continue
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise _SelectionRefused(
                    f"park sidecar {path.name} unreadable/corrupt "
                    f"({type(exc).__name__}: {exc})") from exc
            if not isinstance(body, dict):
                raise _SelectionRefused(
                    f"park sidecar {path.name} is not a JSON object, so what it "
                    "parks cannot be read")
            ident = body.get("id")
            if not isinstance(ident, str) or not ident.strip():
                raise _SelectionRefused(
                    f"park sidecar {path.name} names no id in its JSON body, so "
                    "what it parks cannot be read")
            from_name = _park_idents_from_name(path.name)
            if path.name not in _park_filenames(ident.strip()):
                msg = (f"park sidecar {path.name} disagrees with body id "
                       f"{ident.strip()} -- BOTH ids are treated as parked")
                if alerts is not None:
                    alerts.append(msg)
                if persist_alerts:
                    # RECORDING the oddity must never decide whether builders
                    # run. `alert_once` touches ALERTS.md and an alert-state
                    # file, and `_run_iteration` catches only
                    # `_SelectionRefused`, so ANY escape here aborts the whole
                    # fan-out for a sidecar this function just successfully
                    # INTERPRETED -- the exact defect this lane exists to
                    # remove, re-entering through the telemetry path.
                    #
                    # `Exception`, deliberately, and this file otherwise refuses
                    # blanket catches. Narrow was tried and was wrong: a first
                    # cut caught OSError, and cross-lineage review reproduced
                    # two ordinary non-OSError escapes -- a `state/alerted.json`
                    # containing JSON `null` raises TypeError inside
                    # `claim.alert_once`, and a lone-surrogate body id raises
                    # UnicodeEncodeError on the ALERTS.md write. Enumerating
                    # exception types here means re-deriving the transitive
                    # failure surface of another module every time it changes,
                    # and being wrong is total starvation. The asymmetry
                    # settles it: an unrecorded alert costs one missing line,
                    # an escaped alert costs every builder.
                    #
                    # BaseException is NOT caught -- KeyboardInterrupt and
                    # SystemExit must still stop the process.
                    try:
                        _claim.alert_once(loops_root,
                                          f"spawn_builders:park_name:{path.name}",
                                          msg, source="spawn_builders")
                    except Exception as exc:  # noqa: BLE001 - see above
                        if alerts is not None:
                            alerts.append(
                                f"could not persist park-name alert for "
                                f"{path.name} ({type(exc).__name__}: {exc}); "
                                "exclusion is unaffected")
            # Any non-empty id, in any format: `parked/` is id-keyed, not
            # content-addressed. EVERY key goes in -- see the docstrings.
            parked.update(key for key in (ident, ident.strip()) if key)
            parked.update(from_name)
    return view, parked


#: Bounded fan-out reading candidates/*.json to prove a proposal already has a
#: live (non-terminal) resolver candidate -- mirrors gate_loop.py's own
#: `_resolve_bindings` bound (32 refs) on `payload.resolves`, which may be a
#: scalar full id or a list of them (38 of 168 live candidates use the list
#: shape; this is a read-compatibility concern only, never a new authoring
#: contract -- the harvester in this file still emits a scalar).
_RESOLVES_LIST_LIMIT = 32


def _live_resolver_targets(loops_root: Path, ledger: _LedgerView, *,
                           persist_alerts: bool, alerts: list[str]) -> set[str]:
    """Proposal ids PROVED to already have a live (non-terminal) resolver
    candidate, from `candidates/*.json`'s own `payload.resolves` edge --
    the mechanism the measured duplicate-dispatch defect exists because this
    module never read (2026-08-11): a builder releases or loses a proposal's
    claim after publishing a non-terminal candidate, and the proposal is
    offered to a second builder while the first build still exists.

    Deliberately narrow, matching the proposal's own risk_notes: only a
    well-formed, full `sha256:<64 hex>` id from a scalar `resolves` or the
    retained list carrier proves an edge. Branch names, titles, shared paths,
    missing values, and malformed ids are NEVER inferred as a relationship --
    an unproven candidate contributes no suppression.

    Fails OPEN, never closed: an unreadable/corrupt candidate file or a
    non-object body contributes no edge and alerts once (keyed by filename,
    matching `_open_proposals`' malformed-file posture) -- it must never
    abort the whole worklist, because the worst fail-open consequence here is
    exactly the duplicate work this function exists to reduce, while
    fail-closed recreates the measured total-starvation class `_queue_state`
    already documents for `parked/`."""
    targets: set[str] = set()
    cdir = loops_root / "candidates"
    if not cdir.is_dir():
        return targets
    try:
        entries = sorted(cdir.glob("*.json"))
    except OSError as exc:
        msg = f"candidates/ could not be listed ({type(exc).__name__}: {exc}) -- no live-resolver edges proved"
        alerts.append(msg)
        if persist_alerts:
            _claim.alert_once(loops_root, "spawn_builders:candidates_unreadable", msg,
                              source="spawn_builders")
        return targets
    for p in entries:
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            msg = f"{p.name}: unreadable/corrupt candidate ({type(exc).__name__}: {exc}) -- contributes no live-resolver edge"
            alerts.append(msg)
            if persist_alerts:
                _claim.alert_once(loops_root, f"spawn_builders:candidate_malformed:{p.name}",
                                  msg, source="spawn_builders")
            continue
        if not isinstance(obj, dict):
            msg = f"{p.name}: not a candidate JSON object -- contributes no live-resolver edge"
            alerts.append(msg)
            if persist_alerts:
                _claim.alert_once(loops_root, f"spawn_builders:candidate_malformed:{p.name}",
                                  msg, source="spawn_builders")
            continue
        if obj.get("kind") != "candidate":
            continue  # tolerate a stray non-candidate file without alerting -- not this dir's job

        cand_id = obj.get("id")
        if isinstance(cand_id, str) and cand_id in ledger.terminal:
            # Terminal (merged/completed/rejected/closed): this candidate no
            # longer proves a LIVE build. A rejected resolver must re-enable
            # its proposal for a legitimate recut -- this is the one place
            # that happens: the id simply contributes no edge here.
            continue

        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        raw = payload.get("resolves")
        if isinstance(raw, str):
            refs = [raw]
        elif isinstance(raw, list):
            refs = raw[:_RESOLVES_LIST_LIMIT]
        else:
            continue  # absent/null/other-typed resolves proves no proposal edge
        for ref in refs:
            if isinstance(ref, str) and _SHA256_ID.fullmatch(ref):
                targets.add(ref)
            # a malformed member (not a full sha256 id) is never inferred --
            # it simply contributes no edge; not alerted individually to avoid
            # per-member noise on an already-alerted-once malformed file class.
    return targets


def _open_proposals(loops_root: Path, *, persist_alerts: bool,
                    alerts: list[str]) -> list[dict]:
    """Every well-formed artifact in ``proposals/`` -- ``kind == "proposal"``,
    a non-empty ``id``, and a non-empty list-of-strings ``paths``. A file
    that fails any of these is SKIPPED with an alert (``alert_once``, so a
    permanently-broken file alerts exactly once, not every iteration), never
    silently dropped and never a crash that would take the whole selection
    down with it."""
    d = loops_root / "proposals"
    if not d.is_dir():
        return []
    try:
        entries = sorted(d.glob("*.json"))
    except OSError as exc:
        raise _SelectionRefused(f"proposals/ could not be listed ({exc})") from exc

    out: list[dict] = []
    for p in entries:
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            msg = (f"{p.name}: unreadable/corrupt proposal "
                   f"({type(exc).__name__}: {exc}) -- skipped")
            alerts.append(msg)
            if persist_alerts:
                _claim.alert_once(loops_root, f"spawn_builders:malformed:{p.name}",
                                  msg, source="spawn_builders")
            continue
        if not isinstance(obj, dict):
            msg = f"{p.name}: not a proposal JSON object -- skipped"
            alerts.append(msg)
            if persist_alerts:
                _claim.alert_once(loops_root, f"spawn_builders:malformed:{p.name}",
                                  msg, source="spawn_builders")
            continue
        if obj.get("kind") != "proposal":
            continue  # tolerate a stray non-proposal file without alerting -- not this dir's job

        ident, payload = obj.get("id"), obj.get("payload")
        reason = None
        if not isinstance(ident, str) or not _SHA256_ID.fullmatch(ident):
            reason = "id must match sha256:<64 lowercase hex>"
        elif p.name != f"{ident.replace(':', '_', 1)}.json":
            reason = "filename disagrees with content id"
        elif not isinstance(payload, dict):
            reason = "payload is not a JSON object"
        else:
            try:
                derived = _canonical.content_id(payload)
            except (TypeError, ValueError) as exc:
                reason = f"payload is not canonicalizable ({exc})"
            else:
                if derived != ident:
                    reason = f"payload content id is {derived}, not {ident}"
        if reason is None and _valid_paths(obj) is None:
            reason = "paths missing, empty, or not a list of strings"
        if reason is not None:
            msg = f"{p.name}: {reason} -- skipped before claim.acquire"
            alerts.append(msg)
            if persist_alerts:
                _claim.alert_once(loops_root, f"spawn_builders:malformed:{p.name}",
                                  msg, source="spawn_builders")
            continue

        out.append(obj)
    return out


def _has_live_claim(loops_root: Path, ident: str) -> bool:
    """Read-only existence check against the marker ``claim.py`` itself
    would create -- this NEVER creates, edits, or removes anything; it only
    asks the question ``claim.acquire`` would answer authoritatively a
    moment later. Selection must respect an existing claim (do not offer an
    already-claimed item to a second builder), but the actual arbitration of
    ownership still happens inside ``claim.acquire``'s ``O_EXCL`` create."""
    return _claim._marker_path(loops_root, ident).exists()


def _admitted_unclaimed(loops_root: Path, *, persist_alerts: bool,
                        alerts: list[str]) -> list[dict]:
    """Well-formed proposals with no live claim and not parked, ordered
    priority-then-id (CONTRACT.md's own ordering convention; absent
    ``priority`` reads as ``DEFAULT_PRIORITY`` -- the same default
    ``bridge/integration.py`` uses, never a favourable/unfavourable guess).

    Also excludes any proposal a non-terminal candidate PROVES it already has
    a live resolver for (``_live_resolver_targets``, 2026-08-11): the claim
    marker mutex is not the only protection a proposal has once a builder has
    published a candidate -- a released/expired claim must not re-expose a
    proposal whose build already exists."""
    ledger, parked = _queue_state(loops_root, persist_alerts=persist_alerts,
                                  alerts=alerts)
    live_resolved = _live_resolver_targets(loops_root, ledger,
                                           persist_alerts=persist_alerts,
                                           alerts=alerts)
    items = [
        p for p in _open_proposals(loops_root, persist_alerts=persist_alerts,
                                   alerts=alerts)
        if p["id"] not in ledger.terminal and p["id"] not in parked
        and not _has_live_claim(loops_root, p["id"])
        and p["id"] not in live_resolved
    ]
    items.sort(key=lambda p: (
        p["priority"] if isinstance(p.get("priority"), int) else DEFAULT_PRIORITY,
        p["id"],
    ))
    return items


# --------------------------------------------------------------------------
# worktree provisioning -- same `git worktree add` shape as gate_loop.py
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _builders_root(repo: Path) -> Path:
    return repo.parent / f"{repo.name}-builders"


def _registered_worktrees(repo: Path) -> set[Path]:
    rc, out, err = _git(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        raise _RegistryUnreadable(
            f"git worktree registry unreadable: {(err or out).strip() or f'rc={rc}'}")
    registered: set[Path] = set()
    for line in out.splitlines():
        if line.startswith("worktree "):
            raw = line[len("worktree "):]
            registered.add(Path(raw).resolve())
    return registered


def _provision_worktree(loops_root: Path, repo: Path, item: dict,
                        *, base: str = "main") -> tuple[Path, str]:
    """Open one disposable worktree, on its own branch off ``base``, for one
    claim. Returns ``(path, branch)`` or ``None`` on any git failure -- the
    caller is responsible for releasing the claim it just acquired when this
    returns ``None`` (fail closed: never a held claim with nothing to build
    it in)."""
    ident = item.get("id", "item")
    slug = ident.removeprefix("sha256:")
    branch = f"lane/spawn-{slug}"
    path = _builders_root(repo) / f"spawn-{slug}"
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() or path.is_symlink():
        removed = subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(path)],
            capture_output=True, text=True, check=False)
        # `git worktree remove --force` silently no-ops on plain debris -- a
        # directory git never registered as a worktree (e.g. left over from
        # a killed process before `worktree add` finished) -- and then
        # `worktree add` fails at this path FOREVER (F4, 2026-08-10 review).
        # Only rmtree it after confirming it is NOT a live registered
        # worktree; a genuinely registered-but-locked worktree must never be
        # deleted by hand.
        # C4: even if the path disappeared, a failed remove is accepted only
        # after a SUCCESSFUL registry read proves it unregistered.
        registered = (_registered_worktrees(repo)
                      if removed.returncode != 0 or path.exists() or path.is_symlink()
                      else set())
        if path.resolve() in registered:
            _claim.alert_once(
                loops_root, f"spawn_builders:worktree_locked:{slug}",
                f"{path} is a REGISTERED git worktree that `worktree remove --force` could not "
                "clear -- refusing to touch a live worktree by hand.",
                source="spawn_builders",
            )
            raise RuntimeError(f"{path} remains registered; refusing manual removal")
        try:
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise OSError("unregistered debris is not a real directory; refusing rmtree")
            if path.exists():
                shutil.rmtree(path)
            _claim.alert_once(
                loops_root, f"spawn_builders:worktree_debris:{slug}",
                f"{path} was UNREGISTERED debris `git worktree remove` left behind -- removed by "
                "hand so provisioning could proceed.",
                source="spawn_builders",
            )
        except OSError as exc:
            _claim.alert_once(
                loops_root, f"spawn_builders:worktree_debris_stuck:{slug}",
                f"{path} is unregistered debris that could not be removed ({type(exc).__name__}: "
                f"{exc}) -- refusing to provision on top of it.",
                source="spawn_builders",
            )
            raise RuntimeError(f"unregistered debris at {path} could not be removed") from exc

    rc, _out, _err = _git(repo, "worktree", "add", "-b", branch, str(path), base)
    if rc != 0:
        # branch may already exist from a prior crashed attempt for this same
        # id -- fall back to attaching a fresh worktree to the existing branch
        # rather than refusing outright.
        rc2, _out2, _err2 = _git(repo, "worktree", "add", str(path), branch)
        if rc2 != 0:
            raise RuntimeError(
                f"git worktree add failed: {(_err2 or _err).strip()}")
    return path, branch


def _close_worktree(repo: Path, path: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(path)],
        capture_output=True, text=True, check=False)
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()


# --------------------------------------------------------------------------
# per-builder brief
# --------------------------------------------------------------------------


def _known_traps_block(loops_root: Path) -> str:
    if _traps_block is None or _load_rejections is None:
        return ""
    try:
        rejections = _load_rejections(loops_root, 0.0)
        return _traps_block(rejections, 8)
    except Exception:  # fail-soft: a brief without traps beats a crash here
        return ""


def _lessons_fact_count(block: str) -> int:
    """Best-effort fact count for the structured log line -- recall_lessons()
    only returns the already-rendered string, not the RecallResult it came
    from, so this counts non-wrapper lines rather than re-parsing facts."""
    if not block:
        return 0
    return len(
        [
            line
            for line in block.splitlines()
            if line.strip() and not line.startswith(("<recalled-knowledge", "</recalled-knowledge"))
        ]
    )


def _lessons_block(item: dict) -> str:
    if _recall_lessons is None:
        _LOG.info("lessons_block state=unavailable facts=0 ms=0.0")
        return ""
    if _knowledge_enabled is not None and not _knowledge_enabled():
        _LOG.info("lessons_block state=disabled facts=0 ms=0.0")
        return ""
    started = time.monotonic()
    try:
        paths = _valid_paths(item) or ()
        block = (
            _recall_lessons(
                item.get("title") or "(no title on the admitted item)",
                paths=paths,
                role=ROLE,
                budget_tokens=300,
            )
            or ""
        )
    except Exception:  # recall must never block task minting; KeyboardInterrupt/
        # SystemExit are NOT caught here on purpose -- an operator's Ctrl-C during
        # a live DB round-trip must still stop the loop.
        elapsed_ms = (time.monotonic() - started) * 1000
        _LOG.info("lessons_block state=unavailable facts=0 ms=%.1f", elapsed_ms)
        return ""
    elapsed_ms = (time.monotonic() - started) * 1000
    state = "injected" if block else "empty"
    _LOG.info(
        "lessons_block state=%s facts=%d ms=%.1f",
        state,
        _lessons_fact_count(block),
        elapsed_ms,
    )
    return block


def _write_brief(worktree: Path, item: dict, *, branch: str, test_cmd: str,
                 known_traps: str = "") -> Path:
    """Write the ONE exact brief a builder subagent reads: objective, owned
    paths (named by the item, not chosen by the builder), the mechanical
    pass-list test command, and the git discipline PROMPT-implementer-loop.md
    requires every builder brief to carry.

    Raises ``ValueError`` (never ``TypeError``) on a malformed ``paths`` --
    defensive, belt-and-suspenders: ``admitted_unclaimed`` already filters
    these out, but this function must never trust a caller blindly (F2,
    round-1 review: iterating an int `paths` crashed here with a bare
    TypeError before this check existed)."""
    paths = _valid_paths(item)
    if paths is None:
        raise ValueError(f"{item.get('id', '<no id>')}: paths is not a non-empty list of "
                          f"strings ({item.get('paths')!r})")

    lines = [
        f"# Builder brief -- {item.get('id', '(unknown id)')}",
        "",
        "## Objective",
        item.get("title") or "(no title on the admitted item)",
        "",
        "## Owned paths (touch ONLY these; if the work needs more, stop and escalate)",
        *(f"- {p}" for p in paths),
        "",
        "## Mechanical pass-list (run before writing the candidate envelope)",
        f"    {test_cmd}",
        "The envelope is not written until every item on this list passes BY EXECUTION",
        "in this worktree.",
        "",
        "## Start acknowledgement (write this BEFORE touching code)",
        f"Create `{ACK_NAME}` with JSON containing this exact id:",
        f"    {{\"id\": \"{item.get('id')}\", \"actor\": \"<builder>\", "
        "\"started_at\": \"<UTC-Z>\"}",
        "The coordinator measures consumed briefs by this marker, not by worktree existence.",
        "",
        "## Git discipline",
        f"- Build on branch `{branch}` in THIS worktree only.",
        "- Never touch the serving checkout or `main`; never push/pull/merge/rebase.",
        "- Never append the ledger, never write anything under `claims/` -- the",
        "  coordinator holds the claim and releases it.",
        "- Commit your work; the coordinator harvests the candidate envelope from",
        "  this branch once you are done.",
        f"- Do not commit `{MARKER_NAME}`, `{ACK_NAME}`, or `{BRIEF_NAME}`.",
    ]
    if known_traps:
        lines += ["", "## Known traps", known_traps]
    lessons = _lessons_block(item)
    if lessons:
        lines += ["", "## Lessons from previous runs", lessons]
    brief = worktree / BRIEF_NAME
    brief.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return brief


def _write_marker(worktree: Path, item: dict, *, branch: str, base_sha: str,
                  actor: str, test_cmd: str | None) -> None:
    """Record everything the harvester needs, INCLUDING the exact mechanical
    pass-list this builder was briefed to run (``test_cmd``). The harvester
    prefers this recorded command over any generic default so a candidate whose
    real test passed is never dropped because the harvest ran a different (and
    on this estate, pytest-less) interpreter."""
    path = worktree / MARKER_NAME
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"id": item["id"], "item": item, "branch": branch,
                   "base_sha": base_sha, "claim_actor": actor,
                   "test_cmd": test_cmd,
                   "provisioned_at": _iso(_now())}, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# ledger -- reuse claim.py's own append-only writer, same event shape
# integration.py uses throughout ({ts, role, event, id, actor, detail})
# --------------------------------------------------------------------------


def _worklist_entries(repo: Path) -> list[dict]:
    root = _builders_root(repo)
    if not root.is_dir():
        return []
    try:
        worktrees = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError:
        return []
    entries: list[dict] = []
    for worktree in worktrees:
        try:
            meta = json.loads((worktree / MARKER_NAME).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ident = meta.get("id") if isinstance(meta, dict) else None
        if not isinstance(ident, str) or not _SHA256_ID.fullmatch(ident):
            continue
        entries.append({"id": ident, "worktree": str(worktree),
                        "branch": meta.get("branch"),
                        "brief": str(worktree / BRIEF_NAME),
                        "ack": str(worktree / ACK_NAME),
                        "provisioned_at": meta.get("provisioned_at")})
    return entries


def _append_directive_inert(loops_root: Path, *, claim_ids: list[str], actor: str) -> bool:
    """Schema-valid self-report, deduplicated for the same held-claim set."""
    fingerprint = ",".join(sorted(claim_ids))
    try:
        view = _LedgerView.build(loops_root)
    except Exception:
        return False
    for event in view.events:
        detail = event.get("detail") if isinstance(event, dict) else None
        if (event.get("event") == "instrument_error" and event.get("actor") == actor
                and isinstance(detail, dict)
                and detail.get("condition") == "directive_inert"
                and detail.get("claim_fingerprint") == fingerprint):
            return False
    # instrument_error is the ledger schema's exact vocabulary for an
    # execution instrument that did not act. It may omit id by schema.
    _claim._append_ledger(loops_root, {
        "ts": _iso(_now()), "role": ROLE, "event": "instrument_error",
        "actor": actor,
        "detail": {
            "condition": "directive_inert", "class": "instrument-error",
            "reason": ("multiple stale held proposal claims have builder briefs, "
                       "but zero brief acknowledgement markers were consumed"),
            "held_claims": len(claim_ids), "briefs_consumed": 0,
            "claim_ids": sorted(claim_ids), "claim_fingerprint": fingerprint,
        },
    })
    return True


def _detect_directive_inert(loops_root: Path, repo: Path, *, actor: str,
                            persist: bool) -> dict:
    now = _now()
    stale_held: list[str] = []
    consumed = 0
    for entry in _worklist_entries(repo):
        ident = entry["id"]
        if not _has_live_claim(loops_root, ident):
            continue
        try:
            provisioned = datetime.fromisoformat(
                str(entry.get("provisioned_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if provisioned.tzinfo is None or (now - provisioned).total_seconds() < ACK_GRACE_SECONDS:
            continue
        ack = Path(entry["ack"])
        if ack.exists():
            try:
                body = json.loads(ack.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                body = None
            if isinstance(body, dict) and body.get("id") == ident:
                consumed += 1
                continue
        stale_held.append(ident)
    fired = (persist and len(stale_held) > 1 and consumed == 0
             and _append_directive_inert(loops_root, claim_ids=stale_held, actor=actor))
    return {"held_unacknowledged": stale_held, "briefs_consumed": consumed,
            "directive_inert": bool(fired)}


# --------------------------------------------------------------------------
# candidate envelope harvest -- reuses github_bridge's envelope-dict shape
# and canonical.content_id, rather than a second envelope constructor
# --------------------------------------------------------------------------


def _own_work(repo: Path, branch: str) -> tuple[bool, str | None] | None:
    """Does ``branch`` carry any commit absent from ``main``, and if so, where
    does that work actually fork off?

    Returns ``(carries_own_work, fork_point_or_None)``, or ``None`` when the
    question could not be answered at all.

    ``None`` is NOT ``(False, None)``. "I could not check" and "the branch is
    empty" are different claims, and collapsing them would let one unreadable
    repo silently stop harvesting every lane. Callers must fail OPEN on ``None``
    (behave exactly as they did before this probe existed) and act only on an
    explicit ``False``.

    Deliberately implemented with ``rev-list --boundary ... --not main`` rather
    than ``merge-base --is-ancestor``, matching ``integration.is_ancestor_of_main``:
    this estate's git guard hook intercepts commands containing merge verbs, and
    a hook-blocked probe returns an rc indistinguishable from a genuine "no".
    """
    rc, out, _err = _git(repo, "rev-list", "--boundary", branch, "--not", "main")
    if rc != 0:
        return None
    own: list[str] = []
    boundary: list[str] = []
    for token in out.split():
        if token.startswith("-"):
            boundary.append(token[1:])
        else:
            own.append(token)
    if not own:
        return (False, None)
    # Exactly one boundary commit is the unambiguous fork point of this branch's
    # own work. Several of them (a branch that merged main back into itself) do
    # not describe one range, so report no fork point rather than guess one.
    fork = boundary[0] if len(boundary) == 1 else ""
    return (True, fork if re.fullmatch(r"[0-9a-f]{40}", fork) else None)


def _envelope(item: dict, *, branch: str, base_sha: str, tip_sha: str, actor: str,
              evidence: dict) -> dict:
    paths = _valid_paths(item)
    if paths is None:
        raise ValueError(f"{item.get('id', '<no id>')}: paths is not a non-empty list of "
                          f"strings ({item.get('paths')!r})")
    payload = {"resolves": item.get("id"), "lane": branch, "head_sha": tip_sha}
    return {
        "contract": "v1.1",
        "id": _canonical.content_id(payload),
        "kind": "candidate",
        "title": (item.get("title") or item.get("id") or "candidate")[:200],
        "created_at": _iso(_now()),
        "producer": {"role": "implementer", "actor": actor},
        "base_sha": base_sha,
        "head_sha": tip_sha,
        "branch": branch,
        "paths": paths,
        "evidence": [evidence],
        "payload": payload,
    }


def _release_claim_after_harvest(loops_root: Path, ident: str, claim_actor: object,
                                 *, out_name: str) -> None:
    """Release ``ident``'s claim, idempotently. ``claim.release`` already
    tolerates an already-absent marker (it logs a 'marker already absent'
    event and returns), so calling this on the reconcile path after a crash --
    where the claim may or may not still exist -- is safe and completes the
    interrupted release rather than stranding the claim."""
    if not isinstance(claim_actor, str):
        return
    try:
        _claim.release(loops_root, ident, claim_actor, role=ROLE)
    except _claim.ClaimError as exc:
        _claim.alert_once(
            loops_root, f"spawn_builders:harvest_release:{ident}",
            f"candidate {out_name} present but claim release REFUSED[{exc.code}]: {exc}",
            source="spawn_builders")


def _harvest_one(loops_root: Path, repo: Path, worktree: Path, *, actor: str = ACTOR,
                 test_cmd: str | None = None, close: bool = True,
                 errors: list | None = None, skipped: list | None = None) -> Path | None:
    """For ONE provisioned worktree: if its branch carries commits of its own
    (commits absent from ``main``), write the candidate envelope into
    ``candidates/``. Returns the written path, or ``None`` if there is nothing
    to harvest yet (no commits of its own) or the worktree is not one this
    module provisioned.

    Emptiness is decided by ancestry (``_own_work``), not by ``tip != base``:
    once main advances past the recorded base, a branch that built nothing still
    has a tip different from that base. ``skipped``, when provided, collects
    those structurally-empty lanes -- deliberately a separate channel from
    ``errors``, which means instrument failure and makes the harvest exit
    non-zero; an empty lane is a healthy skip, not a failure. The same ancestry
    answer re-anchors the envelope's ``base_sha`` onto the branch's real fork
    point, so a stale base can never mint a candidate whose diff is main's own
    landed commits.

    ``test_cmd`` semantics (defect 1 + F1 rework): ``None`` means "no explicit
    override -- resolve the pass-list yourself", in which case this uses the
    COORDINATOR-authored, builder-immutable recorded command
    (``_trusted_test_cmd``), then the repo venv interpreter. It NEVER trusts the
    builder-writable marker to name the verification command, and if neither a
    trusted command nor a venv exists it FAILS CLOSED -- it surfaces the error
    (``alert_once`` and ``errors``) and writes nothing, rather than run the
    ambient pytest-less ``python3`` and drop a passing build. An explicit
    non-``None`` value (including the empty string, which selects the
    deliberately-unverified path) always wins.

    A non-``None`` return ALWAYS corresponds to a candidate envelope this call
    actually WROTE (defect 2). A candidate that already exists on disk means a
    prior harvest of this worktree was interrupted before its cleanup finished;
    this call RECONCILES -- it completes the claim release and (when ``close``)
    the worktree removal idempotently -- then returns ``None`` (a skipped write
    is never reported as a fresh harvest). ``errors``, when provided, collects
    instrument failures (an unresolvable pass-list; a pass-list that could not
    be executed) so the caller can surface them as a distinguishable non-success
    rather than an empty, exit-0 'no work' harvest (F3).

    ``close`` governs only the reconcile path's worktree removal; the normal
    success path leaves the worktree for ``_harvest_all`` to close."""
    marker = worktree / MARKER_NAME
    try:
        meta = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ident = meta.get("id")
    item = meta.get("item") or {}
    branch = meta.get("branch") or ""
    base_sha = meta.get("base_sha") or ""
    if (not isinstance(ident, str) or not _SHA256_ID.fullmatch(ident)
            or item.get("id") != ident or not branch
            or not re.fullmatch(r"[0-9a-f]{40}", str(base_sha))):
        return None

    rc, tip, _err = _git(repo, "rev-parse", branch)
    if rc != 0:
        return None
    tip_sha = tip.strip()
    if tip_sha == base_sha:
        return None  # nothing built here yet

    # `tip != base` stops being an emptiness test the moment main advances past
    # the recorded base: the tip then legitimately differs from a stale base
    # while carrying no commit of its own, and the branch is handed a full
    # pass-list that cannot produce a candidate however it exits -- and, on a
    # green exit, mints one whose base..tip range is main's OWN landed commits
    # attributed to this lane. So ask the real question by ancestry instead.
    # Fail OPEN: only an explicit False skips; an unreadable probe (None) falls
    # through to exactly the behaviour that predates this check.
    own = _own_work(repo, branch)
    if own is not None:
        carries_own_work, fork_point = own
        if not carries_own_work:
            detail = (f"{branch} carries no commit absent from main (tip "
                      f"{tip_sha}); nothing to verify and nothing publishable")
            _LOG.info("spawn_builders: harvest skipped for %s -- %s", ident, detail)
            # NOT an alert (45 skips would flood ALERTS.md) and NOT an `errors`
            # entry: `errors` means instrument failure and drives a non-zero
            # harvest exit. A structurally empty lane is a healthy skip.
            if skipped is not None:
                skipped.append({"id": ident, "worktree": str(worktree),
                                "branch": branch, "tip_sha": tip_sha,
                                "reason": "no_own_work", "detail": detail})
            return None
        if fork_point is not None and fork_point != base_sha:
            # The branch's own work forks off somewhere other than the recorded
            # base (it was re-anchored onto a newer main after provisioning), so
            # base_sha..tip_sha would attribute main's landed commits to this
            # lane. Publish the range that holds exactly this lane's commits.
            _LOG.info("spawn_builders: re-anchored %s base %s -> %s (recorded base is "
                      "not this branch's fork point)", ident, base_sha, fork_point)
            base_sha = fork_point

    # Resolve the effective pass-list: explicit override > COORDINATOR-recorded
    # command (builder-immutable) > repo venv default. The builder-writable
    # marker is NEVER consulted for this (F1). If none resolves, FAIL CLOSED and
    # SURFACE it -- never fall back to the ambient python3 (no pytest) and never
    # let the miss read as a healthy no-op harvest.
    if test_cmd is not None:
        effective: str | None = test_cmd
    else:
        trusted = _trusted_test_cmd(loops_root, ident)
        effective = trusted if trusted is not None else _venv_test_cmd(repo)
    if effective is None:
        msg = (f"harvest for {ident} has no runnable pass-list: no --test-cmd "
               f"was given, no coordinator-recorded command exists, and "
               f"{repo}/.venv/bin/python does not exist -- refusing to run the "
               "ambient python3 (no pytest) and drop a passing build. No "
               "candidate written; provision a venv or pass --test-cmd.")
        _claim.alert_once(
            loops_root, f"spawn_builders:harvest_no_interpreter:{ident}", msg,
            source="spawn_builders")
        if errors is not None:
            errors.append({"id": ident, "worktree": str(worktree),
                           "error": "no_runnable_test_command", "detail": msg})
        return None

    if effective.strip():
        argv = shlex.split(effective)
        if not argv:
            msg = (f"harvest for {ident} has an unrunnable pass-list "
                   f"({effective!r} split to zero arguments); no candidate written")
            _claim.alert_once(
                loops_root, f"spawn_builders:harvest_empty_argv:{ident}", msg,
                source="spawn_builders")
            if errors is not None:
                errors.append({"id": ident, "worktree": str(worktree),
                               "error": "unrunnable_test_command", "detail": msg})
            return None
        try:
            verified = _run_verification_command(argv, str(worktree), timeout=3600)
            if verified is None:
                msg = (f"harvest verification for {ident} timed out after 3600s; "
                       "no candidate written")
                _claim.alert_once(
                    loops_root, f"spawn_builders:harvest_verify_timeout:{ident}",
                    msg, source="spawn_builders")
                if errors is not None:
                    errors.append({"id": ident, "worktree": str(worktree),
                                   "error": "verify_timeout", "detail": msg})
                return None
        except (OSError, subprocess.SubprocessError) as exc:
            msg = (f"could not run harvest verification for {ident} "
                   f"({type(exc).__name__}: {exc}); no candidate written")
            _claim.alert_once(
                loops_root, f"spawn_builders:harvest_verify:{ident}", msg,
                source="spawn_builders")
            if errors is not None:
                errors.append({"id": ident, "worktree": str(worktree),
                               "error": "test_command_unexecutable", "detail": msg})
            return None
        if verified.returncode != 0:
            _claim.alert_once(
                loops_root, f"spawn_builders:harvest_red:{ident}:{tip_sha}",
                f"harvest verification for {ident} exited {verified.returncode}; "
                "no candidate written", source="spawn_builders")
            return None
        evidence = {"claim": "harvester ran the configured mechanical pass-list",
                    "verified_by": "execution", "command": effective,
                    "exit_code": verified.returncode}
    else:
        # C8: no command ran, so no exit_code is fabricated. This remains
        # schema-valid but deliberately unverified for Integration to refuse.
        evidence = {"claim": "committed work exists; mechanical verification was not run",
                    "verified_by": "reading", "result": "unverified"}

    try:
        envelope = _envelope(item, branch=branch, base_sha=base_sha, tip_sha=tip_sha,
                              actor=actor, evidence=evidence)
    except ValueError as exc:
        _claim.alert_once(
            loops_root, f"spawn_builders:harvest_malformed:{worktree.name}",
            f"{worktree}: cannot build a candidate envelope ({exc}) -- left in place for manual "
            "recovery, not harvested.",
            source="spawn_builders",
        )
        return None

    out_dir = loops_root / "candidates"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{envelope['id'].replace(':', '_', 1)}.json"
    data = (json.dumps(envelope, indent=2) + "\n").encode("utf-8")
    claim_actor = meta.get("claim_actor")
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        # Defect 2 + F2 reconcile: a candidate with this exact content id already
        # exists, so THIS call wrote nothing -- returning ``out`` would report a
        # fresh harvest for a write that did not happen. A reported success MUST
        # correspond to a real write this call made, so return None. But a
        # pre-existing candidate means a PRIOR harvest of this worktree was
        # interrupted (crash between the candidate fsync and the claim
        # release/worktree close), which the earlier version left stranded. So
        # do not merely skip: RECONCILE idempotently -- finish the claim release
        # and (when close) the worktree removal the prior run did not -- then
        # return None.
        _claim.alert_once(
            loops_root, f"spawn_builders:harvest_exists:{ident}:{tip_sha}",
            f"candidate {out.name} already exists for {ident}@{tip_sha}; skipped "
            "the duplicate write and reconciled the interrupted claim/worktree "
            "cleanup instead of reporting a fresh harvest.",
            source="spawn_builders")
        # Close-then-release: never free the claim while the worktree still
        # exists -- a concurrent provisioner can re-acquire and force-remove it
        # under this process's cwd.
        if close:
            ok, detail = _close_worktree(repo, worktree)
            if not ok:
                _claim.alert_once(
                    loops_root, f"spawn_builders:harvest_reconcile_close:{worktree.name}",
                    f"reconciled duplicate {out.name} but worktree cleanup failed: {detail}",
                    source="spawn_builders")
        _release_claim_after_harvest(loops_root, ident, claim_actor, out_name=out.name)
        return None
    try:
        written = os.write(fd, data)
        if written != len(data):
            raise OSError(f"short candidate write: {written}/{len(data)}")
        os.fsync(fd)
    finally:
        os.close(fd)
    # Close-then-release on the success path too (same race as reconcile).
    # When close=True this call owns worktree removal so _harvest_all does not
    # close again after the claim is already free.
    if close:
        ok, detail = _close_worktree(repo, worktree)
        if not ok:
            _claim.alert_once(
                loops_root, f"spawn_builders:harvest_close:{worktree.name}",
                f"candidate {out.name} written but worktree cleanup failed: {detail}",
                source="spawn_builders")
    _release_claim_after_harvest(loops_root, ident, claim_actor, out_name=out.name)
    return out


def _run_verification_command(argv: list, cwd: str, *, timeout: int = 3600
                              ) -> subprocess.CompletedProcess | None:
    """Run ``argv`` with its own process group so a timeout can be killed as a
    tree (orphan harvesters otherwise leave pytest-xdist workers at ppid=1).

    Returns the ``CompletedProcess`` on a normal exit, or ``None`` on timeout.
    The child is ALWAYS reaped before this returns -- even the innermost
    escalation (pipes still not draining after SIGKILL, e.g. a grandchild
    holding the fd) falls through to an explicit ``proc.kill()`` +
    ``proc.wait()`` so nothing is left as an unwaited zombie.
    """
    proc = subprocess.Popen(
        argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True)
    # Round 4 (BLOCKER): record this child in the currently-held harvest
    # lock's metadata (no-op if none is held, e.g. called directly outside
    # _harvest_all) so a takeover that kills the HOLDER's group can also
    # reach this child -- it runs in its own session and a group-only kill
    # never touches it otherwise.
    _record_verification_child(
        getattr(_harvest_ctx, "lock_fd", None), pid=proc.pid, pgid=proc.pid)
    try:
        try:
            proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        else:
            return subprocess.CompletedProcess(
                args=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

        _kill_process_group(proc.pid)
        try:
            proc.communicate(timeout=5)
            return None
        except subprocess.TimeoutExpired:
            pass

        _kill_process_group(proc.pid, sig=signal.SIGKILL)
        try:
            proc.communicate(timeout=5)
            return None
        except subprocess.TimeoutExpired:
            pass

        # The pipes still will not drain (e.g. a grandchild inherited the fd
        # past the process-group kill) -- reap the process itself so it is
        # never left as an unwaited zombie, even though its output is lost.
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return None
    finally:
        # The child is done (however it ended) -- clear the record so a
        # LATER takeover of a healthy, idle holder never signals a pid this
        # child's slot has since been reused for.
        _record_verification_child(
            getattr(_harvest_ctx, "lock_fd", None), pid=None, pgid=None)


_HARVEST_LOCK_TTL = timedelta(hours=2)
_HARVEST_LOCK_NAME = "harvest.lock"

# Set by _harvest_all for the duration of one harvest pass (cleared in its
# finally) so _run_verification_command -- several call frames down, inside
# _harvest_one, which this module must not otherwise re-signature (see the
# do-not-touch band note on _harvest_one) -- can record/clear the live
# verification child in the SAME held lock without _harvest_one itself ever
# needing to know about locking.
#
# Round 5 (BLOCKER): a bare module global here cross-talks between two
# concurrent _harvest_all calls in one process -- thread B's acquire
# overwrites the value thread A just set, so A's child gets recorded into
# B's lock (or nowhere), and A's own takeover then finds no child to kill.
# threading.local gives each thread its own slot -- no other change to how
# this value is set/read/cleared.
_harvest_ctx = threading.local()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_process_group(pid: int, *, sig: int = signal.SIGTERM) -> None:
    """Signal a process group; fall back to the single pid if getpgid fails
    or the target shares this process's group (never kill our own group)."""
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None
    if pgid is not None and pgid != os.getpgrp():
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _terminate_pid(pid: int, *, wait_s: float = 5.0) -> None:
    """SIGTERM a process group, wait up to wait_s, then SIGKILL if still alive."""
    if not _pid_alive(pid):
        return
    _kill_process_group(pid, sig=signal.SIGTERM)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    if _pid_alive(pid):
        _kill_process_group(pid, sig=signal.SIGKILL)


def _harvest_lock_path(loops_root: Path) -> Path:
    return loops_root / "locks" / _HARVEST_LOCK_NAME


def _read_harvest_lock(path: Path) -> dict | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    return body


def _parse_lock_deadline(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # Accept the Z-suffix form this module writes.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pid_lstart(pid: int) -> str | None:
    """The kernel's start-time stamp for ``pid`` (identity, not just liveness).

    Returns the lstart string when the process is live and readable; ``None``
    when the pid is absent. Raises ``OSError`` when the probe itself fails
    (permission/sandbox/timeout) so the caller can distinguish ``pid-absent``
    from ``unverifiable`` and never mistake "can't tell" for "safe to kill".
    Same identity pattern as gate_loop.py's ``_pid_lstart`` (I8): a stale
    harvest lock's pid can be recycled by an unrelated process over the 2h
    deadline window, and killpg against a recycled pid hits an innocent
    victim.
    """
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, check=False, timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"ps lstart timed out for pid {pid}") from exc
    if proc.returncode != 0:
        return None
    stamp = (proc.stdout or "").strip()
    return stamp or None


def _read_lock_metadata_from_fd(fd: int) -> dict:
    """Read the CURRENTLY-HELD lock's own metadata via its own fd (never via
    the path -- see ``_write_lock_metadata_to_fd``). Never raises; an
    unreadable/corrupt body reads back as ``{}`` so a merge-update degrades
    to "start fresh" rather than crashing the holder's own harvest pass.
    """
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 65536)
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def _write_lock_metadata_to_fd(fd: int, payload: dict) -> bool:
    """Write ``payload`` into an already-flock'd fd, IN PLACE (ftruncate +
    write on the SAME fd -- never ``os.replace`` on the path, which would
    swap the directory entry to a new inode and silently decouple it from
    the flock this fd holds, breaking exclusion for anyone reading the path
    afterward). MUST only ever be called on an fd this process already holds
    ``fcntl.flock(fd, LOCK_EX|LOCK_NB)`` on.

    This JSON body is observability/identity metadata for the NEXT
    acquirer's receipt and reclaim decision, never itself part of how
    exclusion is decided (round 3 ruling: acquisition/updates are
    open-then-write, so any reader who does NOT hold the flock can
    legitimately see a truncated/partial file mid-write -- that reader must
    fail closed, not treat "unreadable" as "dead").
    """
    data = (json.dumps(payload) + "\n").encode("utf-8")
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        if os.write(fd, data) != len(data):
            raise OSError("short harvest lock write")
        os.fsync(fd)
    except OSError as exc:
        _LOG.info("spawn_builders: harvest lock metadata write failed (%s)", exc)
        return False
    return True


def _write_harvest_lock_metadata(fd: int) -> bool:
    """Write this process's fresh identity into a just-acquired fd (no
    prior content to preserve -- this is the initial acquire, not a
    renewal/update; see ``_renew_harvest_lock_lease`` and
    ``_record_verification_child`` for those). MUST only ever be called
    after ``fcntl.flock(fd, LOCK_EX|LOCK_NB)`` has already succeeded on
    ``fd``.
    """
    try:
        my_lstart = _pid_lstart(os.getpid())
    except OSError:
        my_lstart = None
    now = _now()
    payload = {
        "pid": os.getpid(),
        "started_at": _iso(now),
        "deadline": _iso(now + _HARVEST_LOCK_TTL),
        "pid_started": my_lstart,
    }
    return _write_lock_metadata_to_fd(fd, payload)


def _renew_harvest_lock_lease(fd: int) -> bool:
    """Extend the deadline by another ``_HARVEST_LOCK_TTL`` from now.

    Round 4 fix (MAJOR): a flat TTL measured from acquire time kills a
    healthy multi-worktree batch mid-harvest once the batch's total runtime
    (worktree count x up to 3600s each) exceeds the TTL, even though every
    individual worktree is well within its own bound. Called by
    ``_harvest_all`` after EACH worktree's harvest completes (success,
    empty-skip, or per-worktree error -- any of those is "the holder made
    progress"), so takeover only ever fires when a holder made NO
    per-worktree progress for a full TTL window -- a true wedge, since a
    single worktree is capped at 3600s (+ overhead) and can never itself
    exhaust a 2h lease. Preserves pid/pid_started/started_at/child-tracking
    fields as recorded; only the deadline advances.
    """
    payload = _read_lock_metadata_from_fd(fd)
    payload["pid"] = os.getpid()
    payload.setdefault("started_at", _iso(_now()))
    if "pid_started" not in payload:
        try:
            payload["pid_started"] = _pid_lstart(os.getpid())
        except OSError:
            payload["pid_started"] = None
    payload["deadline"] = _iso(_now() + _HARVEST_LOCK_TTL)
    return _write_lock_metadata_to_fd(fd, payload)


def _record_verification_child(fd: int | None, *, pid: int | None,
                               pgid: int | None) -> None:
    """Record (or, when ``pid`` is ``None``, clear) the live verification
    child in the CURRENTLY-HELD lock's metadata.

    Round 4 fix (BLOCKER): the verification child runs in its OWN session
    (``start_new_session=True``, deliberately, so ``_run_verification_command``
    can killpg a timed-out verify without hitting the harvester's own group).
    That means a takeover that only kills the HOLDER's group never reaches
    this child -- the exact orphan class this whole series exists to close.
    Recording it here lets a takeover also identify and terminate it (see
    ``_terminate_recorded_child``). No-ops silently if ``fd`` is ``None``
    (e.g. ``_run_verification_command`` invoked outside a held harvest lock,
    such as directly from a test).
    """
    if fd is None:
        return
    payload = _read_lock_metadata_from_fd(fd)
    if pid is None:
        payload.pop("child_pid", None)
        payload.pop("child_pgid", None)
        payload.pop("child_lstart", None)
    else:
        try:
            child_lstart = _pid_lstart(pid)
        except OSError:
            child_lstart = None
        payload["child_pid"] = pid
        payload["child_pgid"] = pgid
        payload["child_lstart"] = child_lstart
    _write_lock_metadata_to_fd(fd, payload)


def _terminate_recorded_child(existing: dict) -> None:
    """After a takeover kills the (verified) holder's own process group,
    also verify-then-terminate any verification child it recorded --
    otherwise that child (its own session, see ``_record_verification_child``)
    survives the harvester's death as an unbounded-CPU orphan.

    Same identity discipline as the holder check: absent/mismatched lstart
    means "nothing to kill" (pid recycled, or the child already finished and
    the field is merely stale), never a blind signal.
    """
    child_pid = existing.get("child_pid")
    if not isinstance(child_pid, int) or child_pid <= 0:
        return
    recorded_lstart = existing.get("child_lstart")
    try:
        current_lstart = _pid_lstart(child_pid)
    except OSError as exc:
        _LOG.info(
            "spawn_builders: recorded verification child pid=%s identity "
            "unverifiable (%s) -- not signalling", child_pid, exc)
        return
    if current_lstart is None:
        _LOG.info(
            "spawn_builders: recorded verification child pid=%s already gone",
            child_pid)
        return
    if not recorded_lstart or current_lstart != recorded_lstart:
        _LOG.info(
            "spawn_builders: recorded verification child pid=%s identity "
            "mismatch (recorded=%r current=%r) -- not signalling (pid "
            "recycled)", child_pid, recorded_lstart, current_lstart)
        return
    _LOG.info(
        "spawn_builders: reclaiming expired harvest lock -- also terminating "
        "its recorded verification child pid=%s", child_pid)
    _terminate_pid(child_pid)


def _try_flock_harvest_lock(path: Path) -> int | None:
    """Attempt the actual mutual-exclusion primitive: ``flock(LOCK_EX|LOCK_NB)``.

    Returns an open fd (caller now holds the lock and owns releasing it via
    ``_release_harvest_lock``) or ``None`` if another holder has it. The
    kernel releases flock automatically on process death/exit for ANY reason
    (crash, SIGKILL, normal exit) -- a dead holder's lock is simply free,
    with no steal/unlink logic needed to discover that.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    if not _write_harvest_lock_metadata(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return None
    return fd


def _evaluate_harvest_lock_holder(path: Path) -> str:
    """Decide whether it is worth retrying the flock after losing it once.

    Returns "refuse" (do not retry this pass) or "takeover" (safe to keep
    retrying ``_try_flock_harvest_lock`` -- possibly after signalling the
    holder). This function NEVER writes or unlinks anything and never itself
    grants the lock -- it only informs whether the caller's retry loop
    continues. Actual exclusion is decided exclusively by the kernel flock in
    ``_try_flock_harvest_lock``, so two concurrent callers reaching
    "takeover" simultaneously is harmless: they race the SAME kernel
    primitive on the retry and exactly one wins.
    """
    existing = _read_harvest_lock(path)
    if existing is None:
        # Unreadable/partial content. This is NOT proof of a dead or corrupt
        # holder: acquisition is open-then-truncate-then-write while HOLDING
        # the flock, so a reader without the flock can legitimately observe
        # a 0-byte or partial-JSON file mid-write by the real, live holder.
        # Fail closed -- refuse this pass; a later pass re-reads once the
        # write has settled (round 3 ruling).
        _LOG.info(
            "spawn_builders: harvest lock unreadable (still flocked, so "
            "possibly mid-write) -- refusing this pass")
        return "refuse"

    holder = existing.get("pid")
    holder_pid = holder if isinstance(holder, int) else -1
    recorded_lstart = existing.get("pid_started")
    deadline = _parse_lock_deadline(existing.get("deadline"))
    now = _now()
    if deadline is None:
        # Can't parse a deadline out of otherwise-readable metadata -- treat
        # conservatively as still within its window rather than guessing it
        # has expired.
        within = True
    else:
        deadline_utc = (deadline.replace(tzinfo=UTC) if deadline.tzinfo is None
                        else deadline.astimezone(UTC))
        within = now < deadline_utc

    try:
        current_lstart = _pid_lstart(holder_pid)
    except OSError as exc:
        # Identity unverifiable -- fail closed. A busy/expired holder we
        # cannot confirm is still safer treated as live than killed blind.
        _LOG.info(
            "spawn_builders: harvest lock identity unverifiable for pid=%s (%s) "
            "-- refusing (fail closed)", holder_pid, exc)
        return "refuse"

    if current_lstart is None:
        # pid absent -- the kernel would already have freed the real flock
        # when that process died, so in practice ``_try_flock_harvest_lock``
        # would already have succeeded before we ever got here. Handled
        # defensively anyway: nothing alive to signal, safe to keep retrying.
        _LOG.info(
            "spawn_builders: harvest lock pid=%s is dead (no signal needed) "
            "-- retrying flock", holder_pid)
        return "takeover"

    if not recorded_lstart:
        # Legacy/unknown holder identity: we cannot verify who this live pid
        # actually is, so we NEVER signal it. Wait it out (deadline expiry
        # or process exit release the real flock on their own).
        _LOG.info(
            "spawn_builders: harvest lock pid=%s alive with no recorded "
            "identity -- refusing (never signalling an unverified holder)",
            holder_pid)
        return "refuse"

    if current_lstart != recorded_lstart:
        # pid alive but its lstart does not match what was recorded -- the
        # pid was recycled by an unrelated process after the real holder
        # died (which already freed the real flock). Never signal an
        # innocent, unrelated process; just keep retrying the flock.
        _LOG.info(
            "spawn_builders: harvest lock pid=%s reused (recorded lstart=%r, "
            "current lstart=%r) -- retrying flock without signalling",
            holder_pid, recorded_lstart, current_lstart)
        return "takeover"

    if within:
        _LOG.info(
            "spawn_builders: harvest refused -- lock held by live pid=%s until %s",
            holder_pid, existing.get("deadline"))
        return "refuse"

    # Verified-live holder past its deadline: terminate, then keep retrying
    # the flock (which the kernel frees the instant that process exits).
    _LOG.info(
        "spawn_builders: reclaiming expired harvest lock from live pid=%s",
        holder_pid)
    _terminate_pid(holder_pid)
    # Round 4: the holder's own process group does not reach its
    # verification child (own session, by design) -- terminate it too.
    _terminate_recorded_child(existing)
    return "takeover"


def _acquire_harvest_lock(loops_root: Path, *, retry_timeout_s: float = 10.0
                          ) -> int | None:
    """Single-host cooperative harvest singleton. Returns an open fd holding
    an ``flock(LOCK_EX)`` on success (release via ``_release_harvest_lock``),
    or ``None`` if this pass could not acquire it.

    Mutual exclusion is ``fcntl.flock`` end to end -- never a JSON
    check-then-write, never an unlink/recreate dance. The kernel releases
    flock automatically on holder death for any reason, so "the holder is
    dead" requires no detection logic of its own; it simply shows up as the
    next ``LOCK_NB`` attempt succeeding. The JSON body is metadata only
    (identity/observability), written strictly AFTER already holding the
    flock, so there is no window where a partially-written file can be
    mistaken for a dead/corrupt lock and stolen.
    """
    path = _harvest_lock_path(loops_root)
    fd = _try_flock_harvest_lock(path)
    if fd is not None:
        return fd

    decision = _evaluate_harvest_lock_holder(path)
    if decision != "takeover":
        return None

    deadline = time.monotonic() + retry_timeout_s
    while True:
        fd = _try_flock_harvest_lock(path)
        if fd is not None:
            return fd
        if time.monotonic() >= deadline:
            _LOG.info(
                "spawn_builders: harvest lock takeover timed out waiting for "
                "flock to free -- refusing")
            return None
        time.sleep(0.2)


def _release_harvest_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _harvest_all(loops_root: Path, repo: Path, *, actor: str = ACTOR,
                 test_cmd: str | None = None, close: bool = True,
                 errors: list | None = None, skipped: list | None = None) -> list[Path]:
    lock_fd = _acquire_harvest_lock(loops_root)
    if lock_fd is None:
        return []
    _harvest_ctx.lock_fd = lock_fd
    try:
        root = _builders_root(repo)
        if not root.exists():
            return []
        written: list[Path] = []
        for wt in sorted(p for p in root.iterdir() if p.is_dir()):
            try:
                out = _harvest_one(loops_root, repo, wt, actor=actor, test_cmd=test_cmd,
                                   close=close, errors=errors, skipped=skipped)
            except Exception as exc:
                msg = f"harvest failed for {wt} ({type(exc).__name__}: {exc})"
                _claim.alert_once(
                    loops_root, f"spawn_builders:harvest:{wt.name}", msg,
                    source="spawn_builders")
                if errors is not None:
                    errors.append({"worktree": str(wt), "error": "harvest_exception",
                                   "detail": msg})
                continue  # fail-soft per worktree -- one bad worktree must not block the rest
            finally:
                # Round 4 (MAJOR): renew the lease after EVERY worktree this
                # pass touches (success, empty-skip, or per-worktree error --
                # all are "made progress"), not once at acquire time. A
                # healthy multi-worktree batch can legitimately run well past
                # the flat 2h TTL; only a holder that makes NO per-worktree
                # progress for a full TTL window is a wedge.
                _renew_harvest_lock_lease(lock_fd)
            if out is not None:
                written.append(out)
                # Worktree close (when close=True) is owned by _harvest_one so the
                # claim is never released while the tree is still present.
        return written
    finally:
        _harvest_ctx.lock_fd = None
        _release_harvest_lock(lock_fd)


# --------------------------------------------------------------------------
# one iteration: select, claim, provision, brief, self-report
# --------------------------------------------------------------------------


def _run_iteration(loops_root: Path, repo: Path, *, actor: str = ACTOR,
                   dry_run: bool = False, ttl_seconds: int = _claim.DEFAULT_TTL_SECONDS,
                   test_cmd: str | None = None, max_builders: int | None = None,
                   base: str = "main") -> dict:
    result: dict = {"dry_run": dry_run, "claims_acquired": [], "claims_failed": [],
                    "release_failures": [], "worklist": [], "candidates": [],
                    "alerts": []}
    # The command each builder is briefed to run and that its marker records for
    # the harvester: an explicit --test-cmd wins; otherwise the repo venv
    # default (never the ambient python3). ``None`` -- no explicit and no venv
    # -- is recorded honestly so the harvester fails closed rather than guess.
    provision_cmd = test_cmd if test_cmd is not None else _venv_test_cmd(repo)

    try:
        items = _admitted_unclaimed(loops_root, persist_alerts=not dry_run,
                                    alerts=result["alerts"])
    except _SelectionRefused as exc:
        result["selection_refused"] = str(exc)
        return result
    if max_builders is not None:
        items = items[:max_builders]
    result["candidates"] = [i["id"] for i in items]

    if dry_run:
        result["plan"] = items
        result["inertness"] = _detect_directive_inert(
            loops_root, repo, actor=actor, persist=False)
        return result

    known_traps = _known_traps_block(loops_root)

    for item in items:
        ident = item["id"]
        try:
            _claim.acquire(loops_root, ident, actor, ttl_seconds=ttl_seconds, role=ROLE)
        except _claim.ClaimError as exc:
            result["claims_failed"].append({"id": ident, "code": exc.code, "reason": str(exc)})
            continue
        result["claims_acquired"].append(ident)

        worktree: Path | None = None
        # C3: every step after acquire is inside this one boundary, including
        # base resolution and generic exceptions, not only known I/O failures.
        try:
            rc, base_sha, err = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
            base_sha = base_sha.strip()
            if rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", base_sha):
                raise RuntimeError(f"base {base!r} unresolved: {err.strip()}")
            worktree, branch = _provision_worktree(loops_root, repo, item, base=base)
            brief_cmd = provision_cmd or (
                "<no repo .venv and no --test-cmd: set the mechanical "
                "pass-list explicitly before harvesting>")
            brief = _write_brief(worktree, item, branch=branch, test_cmd=brief_cmd,
                                 known_traps=known_traps)
            _write_marker(worktree, item, branch=branch, base_sha=base_sha, actor=actor,
                          test_cmd=provision_cmd)
            # The verification command the harvester will actually run, in a
            # place the builder cannot reach (F1). The marker copy above is
            # informational only.
            _write_testcmd_record(loops_root, ident, provision_cmd)
        except Exception as exc:
            try:
                _claim.release(loops_root, ident, actor, role=ROLE)
                release_detail = "claim released"
            except _claim.ClaimError as release_exc:
                release_detail = (f"claim release REFUSED[{release_exc.code}]: "
                                  f"{release_exc}")
                result["release_failures"].append(
                    {"id": ident, "reason": release_detail})
            except Exception as release_exc:
                release_detail = (f"claim release FAILED ({type(release_exc).__name__}: "
                                  f"{release_exc})")
                result["release_failures"].append(
                    {"id": ident, "reason": release_detail})
            cleanup = ""
            if worktree is not None:
                closed, close_detail = _close_worktree(repo, worktree)
                cleanup = ("; worktree removed" if closed else
                           f"; worktree cleanup failed: {close_detail}")
            message = (f"post-acquire setup failed for {ident} "
                       f"({type(exc).__name__}: {exc}); {release_detail}{cleanup}")
            result["alerts"].append(message)
            _claim.alert_once(
                loops_root, f"spawn_builders:post_acquire:{ident}", message,
                source="spawn_builders")
            continue

        result["worklist"].append({"id": ident, "worktree": str(worktree),
                                   "branch": branch, "brief": str(brief),
                                   "ack": str(worktree / ACK_NAME)})

    result["inertness"] = _detect_directive_inert(
        loops_root, repo, actor=actor, persist=True)
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loops-root", required=True, type=Path)
    ap.add_argument("--repo", type=Path, default=None,
                     help="the repo builders are provisioned from (default: <loops-root>/../..)")
    ap.add_argument("--actor", default=ACTOR)
    ap.add_argument("--ttl-seconds", type=int, default=_claim.DEFAULT_TTL_SECONDS)
    ap.add_argument("--test-cmd", default=None,
                     help="explicit mechanical pass-list; overrides the builder's recorded "
                          "command and the repo venv default. Omit to let the harvester prefer "
                          "the builder's own recorded command, then <repo>/.venv/bin/python.")
    ap.add_argument("--max-builders", type=int, default=None)
    ap.add_argument("--base", default="main")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; claim/provision nothing")
    ap.add_argument("--harvest", action="store_true",
                     help="harvest candidate envelopes from already-provisioned builder worktrees")
    args = ap.parse_args(argv)

    loops_root: Path = args.loops_root.resolve()
    repo: Path = (args.repo or loops_root.parent.parent).resolve()

    if args.harvest:
        errors: list = []
        skipped: list = []
        written = _harvest_all(loops_root, repo, actor=args.actor, test_cmd=args.test_cmd,
                               errors=errors, skipped=skipped)
        # `skipped_no_own_work` is reported but deliberately does NOT affect the
        # exit code: a lane carrying no commit absent from main is a healthy
        # skip, whereas `errors` is reserved for instrument failure (F3).
        print(json.dumps({"harvested": [str(p) for p in written],
                          "skipped_no_own_work": skipped, "errors": errors},
                         indent=2))
        # F3: an instrument failure during harvest (an unresolvable pass-list, a
        # pass-list that could not be executed) must be DISTINGUISHABLE from a
        # healthy no-work harvest -- a non-zero exit AND an explicit `errors`
        # field, never a silent exit-0 empty result.
        return 1 if errors else 0

    result = _run_iteration(loops_root, repo, actor=args.actor, dry_run=args.dry_run,
                            ttl_seconds=args.ttl_seconds, test_cmd=args.test_cmd,
                            max_builders=args.max_builders, base=args.base)
    print(json.dumps(result, indent=2, default=str))
    return 2 if "selection_refused" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
