"""TN.5 — promote: copy APPROVED artifacts to their final destination. Non-agentic.

Everything upstream of this module involves a model. This one does not, and that
is the design, not an omission: the step where an approved deliverable becomes a
live file in a human's directory is the step where a model improvising would be
most expensive and least visible. So promote is a pure, deterministic copy with
four properties:

* **Approved-version targeting.** Sources come from ``work_artifacts`` rows whose
  ``approved_at`` is set, taking the highest approved version per lineage —
  never "the newest file on disk", which is how an unreviewed v3 ships because
  an agent happened to write it last.
* **Explicit mappings.** ``pattern -> dest``. A wildcard pattern MUST target a
  directory; a wildcard whose destination is a single file is a mapping that can
  only be correct by accident, so it is refused rather than resolved.
* **No path escapes.** Every pattern and destination goes through
  :func:`omniagentos.scope.paths.normalize_rel`, and every resolved destination
  through :func:`~omniagentos.scope.paths.resolve_into_realm`. There is no path
  algebra in this file — traversal, absolute paths, ``~``, drive letters and
  symlink escapes are all rejected by the one implementation that already
  handles them.
* **Verified copies.** Every source is re-hashed against the sha256 the manifest
  recorded BEFORE anything is copied, and every destination is hashed after. A
  file that changed between approval and promotion stops the promotion instead
  of shipping.

Plan and execute are separate calls (:func:`plan_promotion`,
:func:`promote`) so a human or a UI can see the exact file list before a byte
moves, and so a plan with any error copies NOTHING.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.scope.paths import ScopePathError, normalize_rel, rel_text, resolve_into_realm
from omniagentos.workmodes.manifest import ArtifactManifest, ManifestEntry, file_sha256
from omniagentos.workmodes.modes import WorkModeError

__all__ = [
    "GLOB_CHARS",
    "CopyOutcome",
    "PlannedCopy",
    "PromoteMapping",
    "PromotionError",
    "PromotionPlan",
    "PromotionResult",
    "entries_from_rows",
    "plan_promotion",
    "promote",
    "promote_approved",
]

#: Characters that make a pattern a wildcard. ``[`` counts: ``report[12].md`` is
#: a character class, not a literal filename, and treating it as literal would
#: silently match nothing.
GLOB_CHARS = "*?["


class PromoteError(WorkModeError):
    """A promotion cannot proceed as planned."""


@dataclass(frozen=True)
class PromoteMapping:
    """One ``pattern -> dest`` rule.

    ``dest`` keeps its RAW text because the trailing slash is meaningful: it is
    how a caller declares "this destination is a directory" for a destination
    that does not exist yet. Without that declaration a wildcard mapping to a
    not-yet-existing path is ambiguous, and this module refuses ambiguity rather
    than guessing from whether the name happens to contain a dot.
    """

    pattern: str
    dest: str

    @property
    def is_wildcard(self) -> bool:
        return any(ch in self.pattern for ch in GLOB_CHARS)

    @property
    def dest_declared_dir(self) -> bool:
        return self.dest.rstrip().endswith(("/", os.sep))


@dataclass(frozen=True)
class PlannedCopy:
    """One verified source -> destination pair."""

    source_rel: str
    dest_rel: str
    sha256: str
    byte_size: int
    pattern: str
    artifact_id: str | None = None
    lineage_id: str | None = None


@dataclass(frozen=True)
class PromotionError:
    """One reason a promotion cannot proceed. ``kind`` is machine-readable."""

    kind: str
    detail: str
    pattern: str = ""
    path: str = ""


@dataclass(frozen=True)
class PromotionPlan:
    """The full copy list, or the reasons there isn't one."""

    source_root: str
    dest_root: str
    copies: tuple[PlannedCopy, ...] = ()
    errors: tuple[PromotionError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def dest_paths(self) -> tuple[str, ...]:
        return tuple(os.path.join(self.dest_root, c.dest_rel) for c in self.copies)


@dataclass(frozen=True)
class CopyOutcome:
    source: str
    dest: str
    sha256: str
    verified: bool
    error: str = ""


@dataclass(frozen=True)
class PromotionResult:
    copied: tuple[CopyOutcome, ...] = ()
    failed: tuple[CopyOutcome, ...] = ()
    errors: tuple[PromotionError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed and not self.errors


def _split_pattern(pattern: str) -> tuple[str, ...]:
    """Validate and split a pattern into components.

    ``normalize_rel`` does the validating: absolute, ``~``, drive-letter, NUL and
    ``..``-escaping patterns all raise here, at declaration time, rather than
    matching something unexpected at copy time. Glob metacharacters survive
    normalization untouched because they are ordinary characters to it.
    """
    try:
        parts = normalize_rel(pattern)
    except ScopePathError as exc:
        raise PromoteError(f"promote pattern is not root-relative: {pattern!r}") from exc
    if not parts:
        raise PromoteError(f"promote pattern cannot be the whole root: {pattern!r}")
    return parts


def _match(entry_parts: Sequence[str], pattern_parts: Sequence[str]) -> bool:
    """Component-wise glob match. ``**`` spans any number of components.

    Component-wise and not ``fnmatch`` over the joined string, for the same
    reason ``scope.paths.under`` compares components: ``*`` in a path pattern
    must not cross a ``/``. ``docs/*.md`` matching ``docs/a/b.md`` would promote
    files the human never listed.
    """
    if not pattern_parts:
        return not entry_parts
    head, *rest = pattern_parts
    if head == "**":
        if not rest:
            return True
        for index in range(len(entry_parts) + 1):
            if _match(entry_parts[index:], rest):
                return True
        return False
    if not entry_parts:
        return False
    if not fnmatch.fnmatchcase(entry_parts[0], head):
        return False
    return _match(entry_parts[1:], rest)


def _literal_prefix(pattern_parts: Sequence[str]) -> int:
    """How many leading pattern components contain no glob character.

    Used to preserve directory structure under a wildcard: with
    ``reports/**/*.md -> final/``, the entry ``reports/q1/summary.md`` lands at
    ``final/q1/summary.md``. Flattening to the basename instead would silently
    merge two same-named files from different subdirectories, and the second
    would overwrite the first.
    """
    count = 0
    for part in pattern_parts:
        if any(ch in part for ch in GLOB_CHARS):
            break
        count += 1
    return count


def entries_from_rows(
    rows: Iterable[Mapping[str, Any]], source_root: str
) -> tuple[ManifestEntry, ...]:
    """``work_artifacts`` rows -> manifest entries, keeping ids and lineages.

    A row whose ``file_path`` does not resolve INSIDE ``source_root`` is dropped:
    the row may have been written on another host or against another root, and a
    promotion that followed it would copy from a directory nobody granted.
    """
    out: list[ManifestEntry] = []
    for row in rows:
        raw = str(row.get("file_path") or "")
        if not raw:
            continue
        parts = resolve_into_realm(raw, source_root)
        if parts is None or not parts:
            continue
        out.append(
            ManifestEntry(
                rel_path=rel_text(parts),
                sha256=str(row.get("sha256") or ""),
                byte_size=int(row.get("byte_size") or 0),
                artifact_type=str(row.get("artifact_type") or "deliverable"),
                version=int(row.get("version") or 1),
                lineage_id=(str(row["lineage_id"]) if row.get("lineage_id") else None),
                artifact_id=(str(row["id"]) if row.get("id") else None),
            )
        )
    return tuple(out)


def plan_promotion(
    entries: Sequence[ManifestEntry] | ArtifactManifest,
    mappings: Sequence[PromoteMapping | tuple[str, str] | Mapping[str, str]],
    *,
    source_root: str,
    dest_root: str,
) -> PromotionPlan:
    """Resolve ``pattern -> dest`` mappings into a concrete, verified copy list.

    Errors are COLLECTED rather than raised one at a time, so a caller sees every
    problem with a mapping set at once instead of fixing them one round-trip
    apiece. A plan with any error copies nothing (:func:`promote` refuses it).

    Rules enforced here:

    * a wildcard pattern must target a directory (declared with a trailing
      ``/`` or already existing as one);
    * a non-wildcard pattern must match EXACTLY one entry — zero matches is an
      error, because a promotion that silently copies nothing is a lie told to
      whoever approved it;
    * every destination must land inside ``dest_root`` after ``..``/symlink
      resolution;
    * two sources may not land on the same destination.
    """
    manifest = entries if isinstance(entries, ArtifactManifest) else None
    items: Sequence[ManifestEntry] = manifest.entries if manifest is not None else entries  # type: ignore[assignment]
    source = os.path.abspath(source_root)
    dest = os.path.abspath(dest_root)

    copies: list[PlannedCopy] = []
    errors: list[PromotionError] = []
    claimed: dict[str, str] = {}

    for raw_mapping in mappings:
        mapping = _coerce_mapping(raw_mapping)
        try:
            pattern_parts = _split_pattern(mapping.pattern)
        except PromoteError as exc:
            errors.append(PromotionError("bad_pattern", str(exc), mapping.pattern))
            continue

        dest_text = mapping.dest.strip()
        if not dest_text or dest_text.rstrip("/") == "":
            dest_parts: tuple[str, ...] = ()
        else:
            try:
                dest_parts = normalize_rel(dest_text)
            except ScopePathError as exc:
                errors.append(PromotionError("bad_dest", str(exc), mapping.pattern, dest_text))
                continue

        dest_abs = os.path.join(dest, *dest_parts) if dest_parts else dest
        dest_is_dir = mapping.dest_declared_dir or not dest_parts or os.path.isdir(dest_abs)
        if mapping.is_wildcard and not dest_is_dir:
            errors.append(
                PromotionError(
                    "wildcard_dest_not_dir",
                    "a wildcard pattern must target a directory; declare it with a trailing "
                    f"'/' (pattern {mapping.pattern!r} -> {mapping.dest!r})",
                    mapping.pattern,
                    mapping.dest,
                )
            )
            continue

        matched = [entry for entry in items if _match(entry.rel_path.split("/"), pattern_parts)]
        if not matched:
            errors.append(
                PromotionError("no_match", "pattern matched no artifact", mapping.pattern)
            )
            continue
        if not mapping.is_wildcard and len(matched) > 1:
            errors.append(
                PromotionError(
                    "ambiguous",
                    f"literal pattern matched {len(matched)} artifacts",
                    mapping.pattern,
                )
            )
            continue

        prefix = _literal_prefix(pattern_parts)
        for entry in matched:
            entry_parts = entry.rel_path.split("/")
            if dest_is_dir:
                tail = entry_parts[prefix:] or [entry_parts[-1]]
                target_parts = (*dest_parts, *tail)
            else:
                target_parts = dest_parts
            target_abs = os.path.join(dest, *target_parts)
            if _escapes(target_abs, dest):
                errors.append(
                    PromotionError(
                        "dest_escape",
                        "destination resolves outside the destination root",
                        mapping.pattern,
                        "/".join(target_parts),
                    )
                )
                continue
            target_rel = "/".join(target_parts)
            if target_rel in claimed and claimed[target_rel] != entry.rel_path:
                errors.append(
                    PromotionError(
                        "dest_collision",
                        f"{entry.rel_path!r} and {claimed[target_rel]!r} both map to {target_rel!r}",
                        mapping.pattern,
                        target_rel,
                    )
                )
                continue
            claimed[target_rel] = entry.rel_path
            copies.append(
                PlannedCopy(
                    source_rel=entry.rel_path,
                    dest_rel=target_rel,
                    sha256=entry.sha256,
                    byte_size=entry.byte_size,
                    pattern=mapping.pattern,
                    artifact_id=entry.artifact_id,
                    lineage_id=entry.lineage_id,
                )
            )

    return PromotionPlan(
        source_root=source,
        dest_root=dest,
        copies=tuple(copies),
        errors=tuple(errors),
    )


def _escapes(candidate: str, root: str) -> bool:
    """True when ``candidate`` does not provably live inside ``root``.

    Uses :func:`inode_relative_parts` from the nearest EXISTING root ancestor
    plus exact remaining components: the destination file and even its root may
    not exist yet. Checking the existing ancestor is what catches
    ``dest/link -> /etc`` before the copy, not after.
    """
    # os.path.LEXISTS, not os.path.exists. exists() FOLLOWS symlinks and is False
    # for a DANGLING one, so `dest/report.md -> /elsewhere/pwned.md` (target absent)
    # was treated as a not-yet-created tail component and sailed through -- the
    # copy then wrote straight through the link, outside the root. lexists() sees
    # the link itself, so the loop stops there and resolve_into_realm judges the
    # link's own resolved location.
    return inode_relative_parts_anchored(candidate, root) is None


def _coerce_mapping(raw: PromoteMapping | tuple[str, str] | Mapping[str, str]) -> PromoteMapping:
    if isinstance(raw, PromoteMapping):
        return raw
    if isinstance(raw, Mapping):
        return PromoteMapping(str(raw.get("pattern", "")), str(raw.get("dest", "")))
    pattern, dest = raw
    return PromoteMapping(str(pattern), str(dest))


def promote(
    plan: PromotionPlan,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
) -> PromotionResult:
    """Execute a plan. Verifies every source BEFORE copying anything.

    Two phases, deliberately:

    1. **Verify.** Every source must exist and hash to the sha256 the manifest
       recorded (when one was recorded), and, unless ``overwrite``, no
       destination may already exist. Any failure here aborts the whole
       promotion with ZERO files copied.
    2. **Copy and re-verify.** Each file is copied with ``shutil.copy2`` (mode
       and mtime preserved) and the destination is hashed and compared.

    A filesystem copy is not transactional, so a failure DURING phase 2 leaves
    the files already copied in place; the result names exactly which ones, and
    the phase-1 verification is what makes that outcome rare rather than routine.
    """
    if not plan.ok:
        raise PromoteError(
            "refusing to promote a plan with errors: "
            + "; ".join(f"{e.kind}: {e.detail}" for e in plan.errors)
        )

    problems: list[PromotionError] = []
    for copy in plan.copies:
        src = os.path.join(plan.source_root, *copy.source_rel.split("/"))
        dst = os.path.join(plan.dest_root, *copy.dest_rel.split("/"))
        if not os.path.isfile(src):
            problems.append(
                PromotionError(
                    "source_missing", f"source file is gone: {src}", copy.pattern, copy.source_rel
                )
            )
            continue
        if copy.sha256:
            actual = file_sha256(src)
            if actual != copy.sha256:
                problems.append(
                    PromotionError(
                        "source_drift",
                        f"{copy.source_rel} hashes {actual[:12]}… but was approved as "
                        f"{copy.sha256[:12]}…",
                        copy.pattern,
                        copy.source_rel,
                    )
                )
                continue
        if os.path.exists(dst) and not overwrite:
            problems.append(
                PromotionError(
                    "dest_exists",
                    f"destination already exists (pass overwrite=True to replace): {dst}",
                    copy.pattern,
                    copy.dest_rel,
                )
            )

    if problems:
        return PromotionResult(errors=tuple(problems))
    if dry_run:
        return PromotionResult()

    copied: list[CopyOutcome] = []
    failed: list[CopyOutcome] = []
    for copy in plan.copies:
        src = os.path.join(plan.source_root, *copy.source_rel.split("/"))
        dst = os.path.join(plan.dest_root, *copy.dest_rel.split("/"))
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            written = file_sha256(dst)
        except OSError as exc:
            failed.append(CopyOutcome(src, dst, copy.sha256, False, str(exc)))
            continue
        expected = copy.sha256 or file_sha256(src)
        if written != expected:
            failed.append(
                CopyOutcome(src, dst, written, False, "destination hash does not match the source")
            )
            continue
        copied.append(CopyOutcome(src, dst, written, True))

    return PromotionResult(copied=tuple(copied), failed=tuple(failed))


@dataclass(frozen=True)
class ApprovedSelection:
    """The approved artifact rows a promotion will draw from."""

    entries: tuple[ManifestEntry, ...] = ()
    skipped: tuple[str, ...] = ()
    lineages: Mapping[str, str] = field(default_factory=dict)


def promote_approved(
    artifact_store: Any,
    mappings: Sequence[PromoteMapping | tuple[str, str] | Mapping[str, str]],
    *,
    work_ref_type: str,
    work_ref_id: str,
    source_root: str,
    dest_root: str,
    overwrite: bool = False,
    dry_run: bool = False,
) -> tuple[PromotionPlan, PromotionResult]:
    """Plan and execute a promotion over the APPROVED artifacts for a work ref.

    ``artifact_store`` is a
    :class:`~omniagentos.workmodes.manifest.WorkArtifactStore` (duck-typed so a
    caller can pass a narrower façade). Rows without ``approved_at`` never
    appear: this is the function that makes "approved" mean something, and it is
    why the revision loop can produce a v2 without any risk that v2 ships before
    a human has looked at it.

    Returns ``(plan, result)``; when the plan has errors the result is empty and
    carries them, so a caller has one place to look for either failure.
    """
    rows = artifact_store.approved_for_ref(work_ref_type, work_ref_id)
    entries = entries_from_rows(rows, source_root)
    if not entries:
        # Named separately from "no_match" because the fix is different: nothing
        # is APPROVED, which is a decision that has not been made yet, not a
        # mapping that is wrong.
        empty = PromotionPlan(
            source_root=os.path.abspath(source_root),
            dest_root=os.path.abspath(dest_root),
            errors=(
                PromotionError("no_approved_artifacts", "nothing is approved for this work ref"),
            ),
        )
        return empty, PromotionResult(errors=empty.errors)
    plan = plan_promotion(entries, mappings, source_root=source_root, dest_root=dest_root)
    if not plan.ok:
        return plan, PromotionResult(errors=plan.errors)
    return plan, promote(plan, overwrite=overwrite, dry_run=dry_run)
