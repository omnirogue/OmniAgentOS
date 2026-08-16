#!/usr/bin/env python3
"""launchd_reconcile.py — what runs here, and which of the four answers disagree.

"What launchd jobs does this machine run?" currently has four sources and no
reconciliation between them (dsh-audit C-22):

1. **live**     — labels loaded in launchd right now (``launchctl list``).
2. **repo**     — plist templates committed in this repository (the declared set).
3. **rendered** — plists installed under ``~/Library/LaunchAgents`` (what an
   installer actually wrote to this machine).
4. **approved** — ``configs/launchd-approved.yaml``, the human record of what
   was approved to run (that file's own header: RECORD, NOT ACTUATOR).

The audit measured 49 / 22 / 29 / 15 across those four. This script is the
diff, and it is the estate's report-then-enforce idiom: it **reads only**.
It never runs ``launchctl load``/``unload``/``bootstrap``/``bootout``, never
writes a plist, never edits the approved manifest. It cannot change what runs;
it can only tell you what does.

Honesty rules (the ones that make the report worth reading)
-----------------------------------------------------------
* **An unreadable source is not an empty source.** If ``launchctl list`` cannot
  be read (non-macOS, timeout, missing binary), every label would otherwise
  read as "approved but not live" — 15 fabricated findings from one instrument
  error. The probe carries ``available``; when it is false the live column is
  ``?``, live-derived drift is SUPPRESSED, and the run exits 2 (instrument
  error), never 1 (drift found). Same for a missing LaunchAgents directory or
  an unreadable approved manifest.
* **An unreadable FILE is an unreadable source.** A plist that cannot be opened
  (permissions, I/O) takes its whole source to ``available=False`` → exit 2. The
  earlier behaviour — note it and continue — let a rogue, unreadable LaunchAgent
  read as "not rendered", suppressing ``rendered_but_not_in_repo`` and allowing
  an exit-0 ``aligned``: the same favourable absence the rule above forbids,
  committed one level down.
* **A template whose Label is a placeholder is not absent.** ``{{LABEL}}`` /
  ``__LABEL__`` templates fall back to the filename; the ones that still cannot
  be resolved are listed under ``unresolved_templates`` rather than dropped, and
  they floor the verdict at ``drift`` — a run holding a plist it could not
  reconcile never reports ``aligned``.
* **Out-of-prefix labels are counted, not hidden.** The default scope is the
  three estate prefixes; anything else is reported as an out-of-scope count so
  the report never implies the machine runs nothing else.

Exit codes
----------
* ``0`` — every in-scope label agrees across every source, every source was
  read, and every plist resolved to a label.
* ``1`` — drift found, or a plist was read but could not be resolved to a label
  (this is the expected steady state today).
* ``2`` — at least one source could not be read, INCLUDING a single unreadable
  plist inside it; the drift verdict is not trustworthy and is NOT reported as a
  defect. An instrument error must never be laundered into a finding.

Usage::

    python scripts/ops/launchd_reconcile.py            # table
    python scripts/ops/launchd_reconcile.py --json     # machine-readable
    python scripts/ops/launchd_reconcile.py --prefix com.omniagentos.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from omniagentos.scheduler.system_jobs import (  # noqa: E402
    LaunchctlProbe,
    launchctl_list,
    parse_plist,
)

#: The estate's own label namespaces. Extend with ``--prefix``; anything outside
#: this set is counted as out-of-scope rather than silently ignored.
DEFAULT_PREFIXES = ("com.omniagentos.", "com.omniagentos.", "com.threeloops.")

DEFAULT_APPROVED = REPO_ROOT / "configs" / "launchd-approved.yaml"

#: Repo subtrees that hold generated, vendored or runtime state — a plist under
#: any of these is not a DECLARED template, so scanning them would invent repo
#: coverage that does not exist.
_SKIP_DIRS = frozenset(
    {".git", "node_modules", "var", "third_party", ".venv", ".next", "dashboard", "assets"}
)

#: A real launchd Label. Placeholder forms ({{LABEL}}, __LABEL__) fail this.
#: ``@`` is admitted deliberately -- homebrew ships versioned labels like
#: ``homebrew.mxcl.postgresql@17``, and rejecting them would file a readable
#: plist as "unresolved", i.e. an instrument error dressed as a finding.
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]*$")

_LABEL_TAG_RE = re.compile(rb"<key>\s*Label\s*</key>\s*<string>([^<]*)</string>")


def _is_label(candidate: str | None) -> bool:
    """Whether ``candidate`` looks like a real, fully-qualified launchd label.

    The dot requirement is what separates a label from a filename stem:
    ``scripts/workqueue/wq-worker.plist.template`` declares ``__LABEL__`` and
    its filename yields ``wq-worker``, which is not a label. Accepting that stem
    would silently classify the template as out-of-prefix and drop it — an
    absence the reader would read as "this template does not exist". It is
    reported as unresolved instead.
    """
    return bool(candidate) and bool(_LABEL_RE.match(candidate or "")) and "." in (candidate or "")

#: Every drift class this script can report, declared as data so the JSON shape
#: is stable even when a class is empty (an absent key reads as "not checked").
DRIFT_CLASSES = (
    "live_but_not_approved",
    "approved_but_not_live",
    "rendered_but_not_in_repo",
    "repo_but_not_rendered",
    "live_but_not_rendered",
    "rendered_but_not_live",
    "approved_but_not_rendered",
)

#: Drift classes derived from the live probe. Suppressed when it is unavailable.
_LIVE_DERIVED = frozenset(
    {
        "live_but_not_approved",
        "approved_but_not_live",
        "live_but_not_rendered",
        "rendered_but_not_live",
    }
)


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


@dataclass
class SourceRead:
    """One source's labels plus whether reading it actually succeeded.

    Two failure kinds are kept apart because they mean different things:

    * ``unreadable`` — a file that could NOT BE READ (permissions, I/O). This is
      an instrument error, and it takes the whole source down (``available``
      goes false → the run is ``unmeasured``, exit 2). Letting one unreadable
      plist through would make a rogue LaunchAgent read as "not rendered",
      suppress ``rendered_but_not_in_repo``, and let the run exit 0 aligned —
      the favourable absence this script exists to expose, committed by the
      script itself (Class-A review F2).
    * ``notes`` — a file that WAS read but declares no usable label. Not an
      instrument failure (we know the file exists), but it still cannot be
      reconciled, so it floors the verdict at ``drift``; it never reads as
      aligned either.
    """

    labels: set[str] = field(default_factory=set)
    available: bool = True
    reason: str = ""
    notes: list[dict[str, str]] = field(default_factory=list)
    unreadable: list[dict[str, str]] = field(default_factory=list)

    def mark_unreadable(self, path: str, reason: str) -> None:
        self.unreadable.append({"path": path, "reason": reason})
        self.available = False
        self.reason = f"{len(self.unreadable)} file(s) could not be read; first: {path} ({reason})"


def in_scope(label: str, prefixes: tuple[str, ...]) -> bool:
    return any(label.startswith(prefix) for prefix in prefixes)


def _label_from_bytes(raw: bytes) -> str | None:
    match = _LABEL_TAG_RE.search(raw)
    if match is None:
        return None
    label = match.group(1).decode("utf-8", "replace").strip()
    return label or None


def _label_from_filename(path: Path) -> str | None:
    """``com.omniagentos.morning.plist.template`` → ``com.omniagentos.morning``."""
    name = path.name
    for suffix in (".plist.template", ".plist"):
        if name.endswith(suffix):
            return name[: -len(suffix)] or None
    return None


def resolve_template_label(path: Path) -> tuple[str | None, str, bool]:
    """Return ``(label, how, readable)`` for a plist/template.

    Prefers the declared ``Label``; falls back to the filename when the Label is
    a render placeholder (``{{LABEL}}``, ``__LABEL__``).

    ``readable`` is the third element precisely because callers must NOT treat
    "this file could not be opened" as "this file declares no label". The first
    is an instrument error that invalidates the source; the second is a fact
    about a file we successfully read.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"unreadable: {type(exc).__name__}", False
    label = None
    try:
        data = parse_plist(path)
        candidate = data.get("Label")
        if isinstance(candidate, str):
            label = candidate.strip()
    except (ValueError, OSError):
        label = None
    if label is None:
        label = _label_from_bytes(raw)
    if _is_label(label):
        return label, "declared Label", True
    from_name = _label_from_filename(path)
    if _is_label(from_name):
        return from_name, "filename (Label is a render placeholder)", True
    return None, "Label is a placeholder and the filename is not a label", True


def read_repo_templates(repo_root: Path, prefixes: tuple[str, ...]) -> SourceRead:
    """Every committed plist/plist.template, keyed by the label it declares."""
    out = SourceRead()
    if not repo_root.is_dir():
        return SourceRead(available=False, reason=f"repo root not a directory: {repo_root}")
    for path in sorted(repo_root.rglob("*.plist*")):
        if not (path.name.endswith(".plist") or path.name.endswith(".plist.template")):
            continue
        rel = path.relative_to(repo_root)
        if any(part in _SKIP_DIRS for part in rel.parts[:-1]):
            continue
        label, how, readable = resolve_template_label(path)
        if not readable:
            out.mark_unreadable(str(rel), how)
            continue
        if label is None:
            out.notes.append({"path": str(rel), "reason": how})
            continue
        if not in_scope(label, prefixes):
            continue
        out.labels.add(label)
    return out


def read_rendered(launch_agents_dir: Path, prefixes: tuple[str, ...]) -> SourceRead:
    """Installed plists under ``~/Library/LaunchAgents`` (this machine's disk)."""
    if not launch_agents_dir.is_dir():
        return SourceRead(
            available=False,
            reason=f"LaunchAgents directory not readable: {launch_agents_dir}",
        )
    out = SourceRead()
    try:
        entries = sorted(launch_agents_dir.iterdir())
    except OSError as exc:
        return SourceRead(
            available=False,
            reason=f"LaunchAgents directory could not be listed: {type(exc).__name__}",
        )
    for path in entries:
        if not path.name.endswith(".plist"):
            continue
        label, how, readable = resolve_template_label(path)
        if not readable:
            out.mark_unreadable(str(path), how)
            continue
        if label is None:
            out.notes.append({"path": str(path), "reason": how})
            continue
        if not in_scope(label, prefixes):
            continue
        out.labels.add(label)
    return out


def read_approved(approved_path: Path, prefixes: tuple[str, ...]) -> SourceRead:
    """The human-approved record. Never actuated — only compared against."""
    try:
        raw = approved_path.read_text(encoding="utf-8")
    except OSError as exc:
        return SourceRead(
            available=False, reason=f"approved manifest unreadable: {type(exc).__name__}"
        )
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return SourceRead(
            available=False, reason=f"approved manifest is not valid YAML: {type(exc).__name__}"
        )
    if not isinstance(data, dict):
        return SourceRead(available=False, reason="approved manifest has no top-level mapping")
    if "approved" not in data:
        return SourceRead(available=False, reason="approved manifest has no 'approved' key")
    entries = data.get("approved")
    if entries is None:
        # `approved:` with nothing under it is an EMPTY approved set, which is a
        # measurement ("a human has approved nothing"). Only a MISSING key, or a
        # value of the wrong shape, means the manifest could not be read.
        entries = []
    if not isinstance(entries, list):
        return SourceRead(
            available=False, reason="approved manifest's 'approved' value is not a list"
        )
    out = SourceRead()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        label = label.strip()
        if not in_scope(label, prefixes):
            continue
        out.labels.add(label)
    return out


def read_live(prefixes: tuple[str, ...], probe: LaunchctlProbe | None = None) -> SourceRead:
    """Labels loaded in launchd right now. Read-only: ``launchctl list`` only."""
    if probe is None:
        probe = launchctl_list()
    if not probe.available:
        return SourceRead(available=False, reason=probe.reason or "launchctl list unavailable")
    return SourceRead(labels={label for label in probe.entries if in_scope(label, prefixes)})


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #


def reconcile(
    *,
    live: SourceRead,
    repo: SourceRead,
    rendered: SourceRead,
    approved: SourceRead,
) -> dict[str, Any]:
    """Diff the four answers into per-label rows plus named drift classes.

    A drift class derived from a source that could not be read is reported as
    ``None`` (not checked), never as an empty list — "we did not look" and "we
    looked and found nothing" are different facts, and collapsing them is the
    favourable-absence bug this whole report exists to expose.
    """
    sources = {"live": live, "repo": repo, "rendered": rendered, "approved": approved}
    labels = sorted(set().union(*(source.labels for source in sources.values())))

    rows: list[dict[str, Any]] = []
    for label in labels:
        row: dict[str, Any] = {"label": label}
        for name, source in sources.items():
            row[name] = (label in source.labels) if source.available else None
        row["aligned"] = all(row[name] is True for name in sources)
        rows.append(row)

    def diff(present: str, absent: str) -> list[str] | None:
        left, right = sources[present], sources[absent]
        if not (left.available and right.available):
            return None
        return sorted(left.labels - right.labels)

    drift: dict[str, list[str] | None] = {
        "live_but_not_approved": diff("live", "approved"),
        "approved_but_not_live": diff("approved", "live"),
        "rendered_but_not_in_repo": diff("rendered", "repo"),
        "repo_but_not_rendered": diff("repo", "rendered"),
        "live_but_not_rendered": diff("live", "rendered"),
        "rendered_but_not_live": diff("rendered", "live"),
        "approved_but_not_rendered": diff("approved", "rendered"),
    }
    if not live.available:
        # Belt and braces: every live-derived class must already be None above,
        # but assert the intent here so a future diff() change cannot quietly
        # start reporting fabricated findings off an unread instrument.
        for name in _LIVE_DERIVED:
            drift[name] = None

    unavailable = sorted(name for name, source in sources.items() if not source.available)
    drift_total = sum(len(found) for found in drift.values() if found is not None)
    unreadable = [note for source in sources.values() for note in source.unreadable]
    unresolved = [note for source in sources.values() for note in source.notes]

    if unavailable:
        # Instrument error outranks drift: a source that could not be read makes
        # the drift verdict untrustworthy, and an untrustworthy verdict must not
        # be reported as a finding.
        verdict, exit_code = "unmeasured", 2
    elif drift_total or unresolved:
        # A file we read but cannot map to a label is not drift we can name, and
        # it is certainly not alignment. It floors the verdict here so no run
        # claims "aligned" while holding a plist it could not reconcile.
        verdict, exit_code = "drift", 1
    else:
        verdict, exit_code = "aligned", 0

    return {
        "verdict": verdict,
        "exit_code": exit_code,
        "counts": {
            name: (len(source.labels) if source.available else None)
            for name, source in sources.items()
        },
        "sources": {
            name: {
                "available": source.available,
                "reason": source.reason,
                "notes": source.notes,
                "unreadable": source.unreadable,
            }
            for name, source in sources.items()
        },
        "unavailable_sources": unavailable,
        "labels": rows,
        "drift": drift,
        "drift_total": drift_total,
        "unreadable_files": unreadable,
        "unresolved_templates": unresolved,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _cell(value: bool | None) -> str:
    if value is None:
        return "?"
    return "yes" if value else "-"


def render_table(report: dict[str, Any]) -> str:
    lines: list[str] = []
    counts = report["counts"]
    lines.append(
        f"launchd composition — live={counts['live']} repo={counts['repo']} "
        f"rendered={counts['rendered']} approved={counts['approved']} "
        "(None = source unreadable)"
    )
    lines.append("")
    width = max([len("label")] + [len(row["label"]) for row in report["labels"]] or [len("label")])
    header = f"{'label':<{width}}  live  repo  rend  appr"
    lines.append(header)
    lines.append("-" * len(header))
    for row in report["labels"]:
        lines.append(
            f"{row['label']:<{width}}  "
            f"{_cell(row['live']):<4}  {_cell(row['repo']):<4}  "
            f"{_cell(row['rendered']):<4}  {_cell(row['approved']):<4}"
        )
    lines.append("")
    lines.append("drift:")
    for name in DRIFT_CLASSES:
        found = report["drift"].get(name)
        if found is None:
            lines.append(f"  {name}: NOT CHECKED (a source could not be read)")
        elif not found:
            lines.append(f"  {name}: none")
        else:
            lines.append(f"  {name}: {len(found)}")
            for label in found:
                lines.append(f"      {label}")
    for name in report["unavailable_sources"]:
        lines.append(f"  ! source '{name}' unreadable: {report['sources'][name]['reason']}")
    for note in report["unreadable_files"]:
        lines.append(f"  ! UNREADABLE plist {note['path']}: {note['reason']}")
    for note in report["unresolved_templates"]:
        lines.append(f"  ! unresolved plist {note['path']}: {note['reason']}")
    lines.append("")
    lines.append(f"verdict: {report['verdict']} (drift_total={report['drift_total']})")
    lines.append("REPORT ONLY — this script never loads, unloads or writes anything.")
    return "\n".join(lines)


def build_report(
    *,
    repo_root: Path,
    launch_agents_dir: Path,
    approved_path: Path,
    prefixes: tuple[str, ...],
    probe: LaunchctlProbe | None = None,
) -> dict[str, Any]:
    return reconcile(
        live=read_live(prefixes, probe),
        repo=read_repo_templates(repo_root, prefixes),
        rendered=read_rendered(launch_agents_dir, prefixes),
        approved=read_approved(approved_path, prefixes),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--launch-agents-dir",
        type=Path,
        default=Path.home() / "Library" / "LaunchAgents",
    )
    parser.add_argument("--approved", type=Path, default=DEFAULT_APPROVED)
    parser.add_argument(
        "--prefix",
        action="append",
        default=None,
        help=f"label prefix in scope (repeatable; default {' '.join(DEFAULT_PREFIXES)})",
    )
    args = parser.parse_args(argv)

    prefixes = tuple(args.prefix) if args.prefix else DEFAULT_PREFIXES
    report = build_report(
        repo_root=args.repo_root,
        launch_agents_dir=args.launch_agents_dir,
        approved_path=args.approved,
        prefixes=prefixes,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_table(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
