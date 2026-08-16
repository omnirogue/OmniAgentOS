"""TN.3/TN.4 — the artifact manifest, its registry rows, and manifest GRADING.

A code assessor reads a diff. There is no diff for a report, an ad set, an image
or a video, so this module produces the thing an assessor can read instead: a
manifest of exactly what landed in the artifact root — every file, its sha256,
its size, its format, its protocol violations, the prompt that generated it, and
the acceptance criteria it was supposed to meet.

Two subtleties in the grading path carry most of the value, and both exist
because of how assessors fail in practice rather than in theory:

**FAST-PASS.** When the manifest shows every expected file, with content, and the
protocol found no violation, the existence question is settled by evidence. A
model asked to re-confirm it can only add noise and latency; :func:`grade_manifest`
returns ``fast_pass=True`` so the caller can skip straight to judgement (or skip
entirely for a task whose criteria are purely existential).

**FAIL-OVERRIDE.** When an assessor returns FAIL and *every* reason it gives is
of the form "missing" / "not found" / "cannot be confirmed", that pattern means
the assessor looked in the wrong place — it was handed a repo diff, or a working
directory, and the deliverables were in the artifact root all along. That is not
evidence the work is bad. :func:`apply_fail_override` recognises the pattern and
re-decides against the manifest: PASS when the manifest independently shows a
clean, complete artifact set, NEEDS_REVIEW when it does not. The override is
narrow on purpose — a reason like "the report is missing the required
disclaimer" contains the word "missing" and is a genuine quality defect, so a
quality-defect lexicon disqualifies a reason from ever counting as a lookup
failure.

The registry half writes ``work_artifacts`` (migration 058), never the frozen
Wave-0 ``artifacts`` table. ``version``/``lineage_id`` are what make the later
revision loop honest: v2 shares v1's lineage, and promote targets the APPROVED
version rather than whatever is newest on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from omniagentos.contracts import AssessmentVerdict, TaskMode, new_id, utc_now_iso
from omniagentos.scope.paths import ScopePathError, normalize_rel, rel_text, resolve_into_realm
from omniagentos.workmodes.modes import WorkModeError, coerce_task_mode
from omniagentos.workmodes.protocols import (
    MANIFEST_FILENAME,
    PROMPT_FILENAME,
    AcceptanceCriteria,
    acceptance_to_json,
    check_files,
)

__all__ = [
    "ARTIFACT_TYPES",
    "MANIFEST_SCHEMA_VERSION",
    "STORAGE_KINDS",
    "ArtifactManifest",
    "FailOverride",
    "ManifestEntry",
    "ManifestGrade",
    "WorkArtifactStore",
    "apply_fail_override",
    "build_manifest",
    "file_sha256",
    "grade_manifest",
    "is_lookup_failure_reason",
    "load_manifest",
    "manifest_path",
    "write_manifest",
]

MANIFEST_SCHEMA_VERSION = 1

#: Mirrors the CHECK constraint on ``work_artifacts.artifact_type`` (migration
#: 058). Kept here so a bad type fails with a sentence a human can act on rather
#: than as a sqlite IntegrityError three frames deeper; the DB constraint is
#: still the authority, this is only the early, legible copy.
ARTIFACT_TYPES: frozenset[str] = frozenset(
    {
        "prompt",
        "response",
        "task_spec",
        "review",
        "summary",
        "diff",
        "log",
        "human_answer",
        "report",
        "nudge",
        "plan_primary",
        "plan_redteam",
        "plan_synthesis",
        "web_fetch_cache",
        "deliverable",
        "other",
    }
)

#: Mirrors the CHECK constraint on ``work_artifacts.storage_kind``, same reason.
STORAGE_KINDS: frozenset[str] = frozenset({"inline", "file_path", "url"})

#: sha256 of a file, streamed. 1 MiB chunks: a 400 MB video must not be read into
#: memory to be hashed, and ``contracts.digest`` takes bytes by design.
_HASH_CHUNK = 1024 * 1024

#: Names never treated as deliverables when walking an artifact root.
_SKIPPED_NAMES: frozenset[str] = frozenset({".DS_Store", "Thumbs.db"})


def file_sha256(path: str) -> str:
    """Streamed sha256 hex digest of a file."""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


@dataclass(frozen=True)
class ManifestEntry:
    """One file in the artifact root, as recorded.

    ``sha256`` is what makes the manifest evidence rather than testimony: promote
    (TN.5) re-hashes the source before copying and the copy after, so a file that
    changed between grading and promotion is caught instead of shipped.
    """

    rel_path: str
    sha256: str
    byte_size: int
    fmt: str = ""
    mime_type: str | None = None
    artifact_type: str = "deliverable"
    version: int = 1
    lineage_id: str | None = None
    artifact_id: str | None = None
    duration_s: float | None = None
    violations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "format": self.fmt,
            "mime_type": self.mime_type,
            "artifact_type": self.artifact_type,
            "version": self.version,
            "lineage_id": self.lineage_id,
            "artifact_id": self.artifact_id,
            "duration_s": self.duration_s,
            "violations": list(self.violations),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> ManifestEntry:
        return cls(
            rel_path=str(raw.get("rel_path", "")),
            sha256=str(raw.get("sha256", "")),
            byte_size=int(raw.get("byte_size", 0) or 0),
            fmt=str(raw.get("format", "") or ""),
            mime_type=raw.get("mime_type"),
            artifact_type=str(raw.get("artifact_type", "deliverable")),
            version=int(raw.get("version", 1) or 1),
            lineage_id=raw.get("lineage_id"),
            artifact_id=raw.get("artifact_id"),
            duration_s=raw.get("duration_s"),
            violations=tuple(str(v) for v in raw.get("violations", ())),
            warnings=tuple(str(w) for w in raw.get("warnings", ())),
        )


@dataclass(frozen=True)
class ArtifactManifest:
    """Everything an artifact-mode assessor needs, established without a model."""

    task_mode: TaskMode
    scope: str
    artifact_root: str
    entries: tuple[ManifestEntry, ...] = ()
    expected_files: tuple[str, ...] = ()
    prompt: str | None = None
    acceptance: Mapping[str, Any] = field(default_factory=dict)
    violations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    created_at: str = ""
    schema_version: int = MANIFEST_SCHEMA_VERSION
    work_ref_type: str | None = None
    work_ref_id: str | None = None

    @property
    def ok(self) -> bool:
        """No protocol violations anywhere."""
        return not self.violations

    @property
    def rel_paths(self) -> tuple[str, ...]:
        return tuple(entry.rel_path for entry in self.entries)

    def entry(self, rel_path: str) -> ManifestEntry | None:
        for candidate in self.entries:
            if candidate.rel_path == rel_path:
                return candidate
        return None

    @property
    def missing_expected(self) -> tuple[str, ...]:
        """Expected files with no entry, or an entry of zero bytes.

        Zero bytes counts as missing on purpose: an empty file is the most common
        shape of "the tool ran, the write failed, nobody noticed".
        """
        present = {entry.rel_path: entry for entry in self.entries}
        missing: list[str] = []
        for expected in self.expected_files:
            entry = present.get(expected)
            if entry is None or entry.byte_size <= 0:
                missing.append(expected)
        return tuple(missing)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_mode": self.task_mode.value,
            "scope": self.scope,
            "artifact_root": self.artifact_root,
            "created_at": self.created_at,
            "work_ref_type": self.work_ref_type,
            "work_ref_id": self.work_ref_id,
            "prompt": self.prompt,
            "expected_files": list(self.expected_files),
            "acceptance": dict(self.acceptance),
            "violations": list(self.violations),
            "warnings": list(self.warnings),
            "entries": [entry.to_json() for entry in self.entries],
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> ArtifactManifest:
        mode = coerce_task_mode(raw.get("task_mode"))
        if mode is None:
            raise WorkModeError(f"manifest has an unknown task_mode: {raw.get('task_mode')!r}")
        return cls(
            task_mode=mode,
            scope=str(raw.get("scope", "")),
            artifact_root=str(raw.get("artifact_root", "")),
            entries=tuple(ManifestEntry.from_json(e) for e in raw.get("entries", ())),
            expected_files=tuple(str(f) for f in raw.get("expected_files", ())),
            prompt=raw.get("prompt"),
            acceptance=dict(raw.get("acceptance", {}) or {}),
            violations=tuple(str(v) for v in raw.get("violations", ())),
            warnings=tuple(str(w) for w in raw.get("warnings", ())),
            created_at=str(raw.get("created_at", "")),
            schema_version=int(raw.get("schema_version", MANIFEST_SCHEMA_VERSION) or 1),
            work_ref_type=raw.get("work_ref_type"),
            work_ref_id=raw.get("work_ref_id"),
        )


def manifest_path(artifact_root: str) -> str:
    """Absolute path of the manifest inside ``artifact_root``."""
    return os.path.join(artifact_root, MANIFEST_FILENAME)


def _walk_files(root: str) -> list[tuple[str, str]]:
    """``(abs_path, rel_posix_path)`` for every file under ``root``, sorted.

    Sorted so two runs over the same directory produce byte-identical manifests
    — a manifest that reorders itself cannot be diffed, and a manifest nobody can
    diff stops being read. Symlinks are skipped: a symlinked "deliverable" is a
    pointer at bytes that live somewhere the guard never checked.
    """
    out: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") or name in _SKIPPED_NAMES:
                continue
            abs_path = os.path.join(dirpath, name)
            if os.path.islink(abs_path) or not os.path.isfile(abs_path):
                continue
            rel = os.path.relpath(abs_path, root)
            out.append((abs_path, rel.replace(os.sep, "/")))
    return sorted(out, key=lambda pair: pair[1])


def build_manifest(
    mode: TaskMode | str,
    artifact_root: str,
    *,
    scope: str = "",
    expected_files: Iterable[str] = (),
    prompt: str | None = None,
    acceptance: AcceptanceCriteria | None = None,
    work_ref_type: str | None = None,
    work_ref_id: str | None = None,
    probe: bool = True,
    now: str | None = None,
) -> ArtifactManifest:
    """Walk ``artifact_root``, hash everything, and check it against the protocol.

    Reads the recorded prompt from ``prompt.txt`` when the caller does not pass
    one, because for image/video the prompt file IS the source and a manifest
    that omitted it would fail its own ``require_prompt`` check while the prompt
    sat on disk beside it.
    """
    resolved = coerce_task_mode(mode)
    if resolved is None:
        raise WorkModeError(f"unknown task mode: {mode!r}")
    root = os.path.abspath(artifact_root)
    files = _walk_files(root) if os.path.isdir(root) else []

    effective_prompt = prompt
    if effective_prompt is None:
        prompt_file = os.path.join(root, PROMPT_FILENAME)
        if os.path.isfile(prompt_file):
            try:
                with open(prompt_file, encoding="utf-8", errors="replace") as handle:
                    effective_prompt = handle.read()
            except OSError:
                effective_prompt = None

    sized: list[tuple[str, str, int]] = []
    hashes: dict[str, tuple[str, int]] = {}
    for abs_path, rel in files:
        try:
            size = os.path.getsize(abs_path)
            digest = file_sha256(abs_path)
        except OSError:
            # An unreadable file is a violation, not an omission: leaving it out
            # would let a deliverable nobody can open pass as "not produced yet".
            hashes[rel] = ("", -1)
            sized.append((abs_path, rel, 0))
            continue
        hashes[rel] = (digest, size)
        sized.append((abs_path, rel, size))

    report = check_files(resolved, sized, prompt=effective_prompt, probe=probe)
    per_file = {check.rel_path: check for check in report.files}

    entries: list[ManifestEntry] = []
    unreadable: list[str] = []
    for _abs_path, rel in files:
        digest, size = hashes.get(rel, ("", -1))
        if size < 0:
            unreadable.append(rel)
            size = 0
        check = per_file.get(rel)
        entries.append(
            ManifestEntry(
                rel_path=rel,
                sha256=digest,
                byte_size=size,
                fmt=(check.fmt if check else os.path.splitext(rel)[1].lstrip(".").lower()),
                artifact_type=(
                    "prompt" if os.path.basename(rel) == PROMPT_FILENAME else "deliverable"
                ),
                duration_s=check.duration_s if check else None,
                violations=check.violations if check else (),
                warnings=check.warnings if check else (),
            )
        )

    expected = tuple(str(f) for f in expected_files) or (
        tuple(acceptance.expected_files) if acceptance else ()
    )
    violations = list(report.violations)
    violations.extend(f"{rel}: unreadable" for rel in unreadable)
    manifest = ArtifactManifest(
        task_mode=resolved,
        scope=scope,
        artifact_root=root,
        entries=tuple(entries),
        expected_files=expected,
        prompt=effective_prompt,
        acceptance=acceptance_to_json(acceptance),
        violations=tuple(violations),
        warnings=tuple(report.warnings),
        created_at=now or utc_now_iso(),
        work_ref_type=work_ref_type,
        work_ref_id=work_ref_id,
    )
    missing = manifest.missing_expected
    if missing:
        manifest = replace(
            manifest,
            violations=(
                *manifest.violations,
                *(f"expected file absent: {name}" for name in missing),
            ),
        )
    return manifest


def write_manifest(manifest: ArtifactManifest, *, path: str | None = None) -> str:
    """Write the manifest as JSON next to the artifacts; returns the path.

    Written atomically (temp file + ``os.replace``) because a half-written
    manifest is worse than a missing one: the grader would read it, find fewer
    files than exist, and report exactly the "missing" verdict that
    :func:`apply_fail_override` was built to distrust.
    """
    target = path or manifest_path(manifest.artifact_root)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(manifest.to_json(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, target)
    return target


def load_manifest(path: str) -> ArtifactManifest:
    """Read a manifest written by :func:`write_manifest`."""
    with open(path, encoding="utf-8") as handle:
        return ArtifactManifest.from_json(json.load(handle))


# ---------------------------------------------------------------------------
# TN.4 — grading the manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestGrade:
    """The deterministic half of an artifact-mode assessment."""

    verdict: AssessmentVerdict
    fast_pass: bool
    reasons: tuple[str, ...]
    missing: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        """True when the manifest is complete and violation-free."""
        return not self.missing and not self.violations


def grade_manifest(
    manifest: ArtifactManifest | None, *, expected_files: Iterable[str] | None = None
) -> ManifestGrade:
    """Grade the MANIFEST, not a diff.

    FAST-PASS is the point: when every expected file is present with content and
    the protocol found nothing wrong, existence is settled by evidence and an
    assessor call can only add latency and variance. ``fast_pass`` is returned
    separately from ``verdict`` so a caller can still choose to run a judgement
    pass over content quality — the fast pass answers "is it there and valid",
    never "is it good".

    A manifest that is missing entirely is ``NEEDS_REVIEW``, never ``FAIL``: no
    manifest means nothing was measured, and "unmeasured" is not "bad" (the same
    distinction ``ObservedChange.source == 'unobserved'`` makes).
    """
    if manifest is None:
        return ManifestGrade(
            verdict=AssessmentVerdict.NEEDS_REVIEW,
            fast_pass=False,
            reasons=("no manifest was produced; nothing was measured",),
        )

    expected = (
        tuple(str(f) for f in expected_files)
        if expected_files is not None
        else manifest.expected_files
    )
    present = {entry.rel_path: entry for entry in manifest.entries}
    missing = tuple(
        name for name in expected if name not in present or present[name].byte_size <= 0
    )
    deliverables = [
        entry
        for entry in manifest.entries
        if os.path.basename(entry.rel_path) not in {MANIFEST_FILENAME, PROMPT_FILENAME}
    ]
    violations = tuple(manifest.violations)

    reasons: list[str] = []
    if missing:
        reasons.append(f"expected file(s) missing or empty: {', '.join(missing)}")
    if violations:
        reasons.append(f"{len(violations)} protocol violation(s)")
    if not deliverables:
        reasons.append("the artifact root contains no deliverable files")

    if missing or violations or not deliverables:
        return ManifestGrade(
            verdict=AssessmentVerdict.FAIL,
            fast_pass=False,
            reasons=tuple(reasons),
            missing=missing,
            violations=violations,
        )

    reasons.append(
        f"{len(deliverables)} deliverable(s) present, "
        f"{len(expected)} expected file(s) accounted for, no protocol violations"
    )
    return ManifestGrade(
        verdict=AssessmentVerdict.PASS,
        fast_pass=True,
        reasons=tuple(reasons),
    )


#: Reason shapes that mean "the assessor could not find it", not "it is bad".
_LOOKUP_FAILURE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bnot found\b",
        r"\bno such file\b",
        r"\bdoes not exist\b",
        r"\bnot present\b",
        r"\bcould not (find|locate|be found|be located)\b",
        r"\bcannot (find|locate|be found|be confirmed|be verified|confirm|verify)\b",
        r"\bcan't (find|locate|confirm|verify)\b",
        r"\bunable to (find|locate|confirm|verify|access|read)\b",
        r"\bno evidence\b",
        r"\bnothing (was )?(found|produced|created|written)\b",
        r"\bempty (directory|folder|workspace)\b",
        r"\bno (files?|outputs?|artifacts?|deliverables?|changes?|diff)\b",
        r"\bmissing\b",
        r"\bnot (visible|accessible|available)\b",
        r"\bno .* (were|was) (found|produced|created)\b",
    )
)

#: Reason shapes that DISQUALIFY a reason from ever counting as a lookup failure.
#: This is the guard that keeps the override narrow: "the report is missing the
#: required disclaimer" matches ``\bmissing\b`` and is a real, substantive defect.
#: Without this list the override would launder genuine quality failures into
#: passes, which is a far worse failure than the one it fixes.
_QUALITY_DEFECT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bmissing the\b",
        r"\bmissing a\b",
        r"\bmissing an\b",
        r"\bdisclaimer\b",
        r"\bbanned\b",
        r"\bcharacter limit\b",
        r"\btoo (long|short|generic|vague)\b",
        r"\b(incorrect|inaccurate|wrong|false|misleading|fabricat\w+|hallucinat\w+)\b",
        r"\bdoes not (meet|match|follow|satisfy|address|answer)\b",
        r"\bviolat\w+\b",
        r"\bpoor(ly)?\b",
        r"\blow quality\b",
        r"\bunsupported (format|claim)\b",
        # "no evidence OF ANY ARTIFACTS" is a lookup failure; "no evidence FOR the
        # central claim" is a judgement about the content and must never be one.
        r"\bno (evidence|support|citations?|sources?) (for|behind|backing)\b",
        r"\bno evidence that\b",
        r"\bcontradict\w+\b",
        r"\bplaceholder\b",
        r"\blorem ipsum\b",
        r"\bmissing section\b",
        r"\bmissing (context|detail|evidence|citation|source)s?\b",
    )
)


def is_lookup_failure_reason(reason: str) -> bool:
    """True when ``reason`` says "I could not find it", not "it is wrong".

    Both halves are required: a lookup-shaped phrase AND no quality-defect
    phrase. The asymmetry is deliberate — a false negative here costs one
    unnecessary human review, while a false positive turns a real defect into a
    pass.
    """
    text = (reason or "").strip()
    if not text:
        return False
    if any(pattern.search(text) for pattern in _QUALITY_DEFECT_PATTERNS):
        return False
    return any(pattern.search(text) for pattern in _LOOKUP_FAILURE_PATTERNS)


@dataclass(frozen=True)
class FailOverride:
    """The result of re-deciding a FAIL whose reasons were all lookup failures."""

    verdict: AssessmentVerdict
    overridden: bool
    original: AssessmentVerdict
    reasons: tuple[str, ...]
    note: str = ""


#: Reasons that are unambiguously about the wrong SURFACE rather than about the
#: content -- an artifact-mode task graded against a git diff, which is empty by
#: construction because the deliverables went to the artifact root exactly as
#: intended. These are a small CLOSED allowlist on purpose: every one is a
#: statement about whether files/diffs/outputs exist, and none can be a judgement
#: about what is inside them.
#:
#: Contrast "I could not find a coherent argument in section 3" -- lookup-SHAPED,
#: matches nothing here (the nouns are constrained to output/artifact/file/diff),
#: and correctly routes to NEEDS_REVIEW instead of PASS.
_SURFACE_LOOKUP_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bno (changed|modified|new) files?\b",
        r"\bno files? (were |was )?(found|changed|modified|produced|created)\b",
        r"\b(empty|no) diff\b",
        r"\bnothing was (written|modified|changed|produced|created|committed)\b",
        r"\bcannot confirm\b.{0,60}\b(was|were) (written|produced|created|generated|delivered)\b",
        r"\bcould not (locate|find|see)\b.{0,30}\b(output|artifact|deliverable|file|report|asset)s?\b",
        r"\bno (output|artifact|deliverable|asset)s? (were |was )?(found|produced|present)\b",
        r"\bworking tree is clean\b",
    )
)


def _is_surface_lookup(reason: str) -> bool:
    """True when a reason is about the wrong surface, not about the content."""
    text = (reason or "").strip()
    return bool(text) and any(p.search(text) for p in _SURFACE_LOOKUP_PATTERNS)


def _reasons_named_in_manifest(reasons: Sequence[str], manifest: ArtifactManifest) -> bool:
    """True when EVERY reason names a file the manifest contains.

    The positive half of the override test. Matches on the basename as well as
    the full relative path, because an assessor typically writes "report.md"
    rather than "artifacts/wi-42/report.md". Case-insensitive: assessors are
    prose, not shells.

    An empty manifest yields False -- there is nothing for a reason to name, so
    nothing can be contradicted.
    """
    names: set[str] = set()
    for entry in manifest.entries:
        # ManifestEntry's field is rel_path. Reading a non-existent "path"
        # attribute silently yielded "" and made this whole branch dead code --
        # the tests still passed because the surface-allowlist path covered them.
        rel = str(getattr(entry, "rel_path", "") or "").replace("\\", "/").strip()
        if not rel:
            continue
        names.add(rel.lower())
        names.add(rel.rsplit("/", 1)[-1].lower())
    if not names:
        return False
    for reason in reasons:
        text = (reason or "").lower()
        if not any(name and name in text for name in names):
            return False
    return True


def apply_fail_override(
    verdict: AssessmentVerdict | str,
    reasons: Sequence[str],
    *,
    manifest: ArtifactManifest | None = None,
    grade: ManifestGrade | None = None,
) -> FailOverride:
    """Re-decide a FAIL whose reasons are ALL "missing / not found / cannot confirm".

    That pattern is diagnostic of an assessor that looked in the wrong place —
    the classic case being an artifact-mode task graded against a git diff, which
    is empty by construction because the deliverables were written to the
    artifact root exactly as they were supposed to be.

    Two outcomes, and the split matters:

    * the manifest independently shows a clean, complete artifact set ->
      **PASS**. The evidence the assessor could not find has been produced by
      something that is not a model.
    * no manifest, or a manifest that is itself incomplete or in violation ->
      **NEEDS_REVIEW**. The assessor's reasoning was unusable, but that is not a
      reason to call the work good; a human decides.

    Anything else — a non-FAIL verdict, no reasons at all, or even one
    substantive reason among the lookup failures — is returned untouched.
    """
    original = (
        verdict if isinstance(verdict, AssessmentVerdict) else AssessmentVerdict(str(verdict))
    )
    listed = [str(r) for r in reasons if str(r).strip()]
    if original is not AssessmentVerdict.FAIL or not listed:
        return FailOverride(original, False, original, tuple(listed))
    if not all(is_lookup_failure_reason(reason) for reason in listed):
        return FailOverride(
            original,
            False,
            original,
            tuple(listed),
            note="at least one reason is a substantive defect",
        )

    resolved_grade = grade if grade is not None else grade_manifest(manifest)

    # A lookup-shaped reason is NECESSARY but NOT SUFFICIENT to reach PASS.
    #
    # The lexicon above is a denylist, and a denylist can never be complete: an
    # assessor writing "I could not find a coherent argument in section 3" is
    # lookup-SHAPED, matches no quality-defect pattern, and is a genuine defect.
    # Overriding it would launder bad work into a pass -- strictly worse than the
    # bug the override exists to fix, because a false FAIL costs one human review
    # while a false PASS ships the deliverable.
    #
    # So PASS additionally requires a POSITIVE, checkable condition: every reason
    # must NAME something the manifest proves is present. "I could not find
    # report.md" + a manifest containing report.md is an assessor that looked in
    # the wrong place -- that is the exact situation this override is for.
    # "I could not find a coherent argument" names no artifact, so the manifest
    # cannot contradict it, and it routes to NEEDS_REVIEW instead.
    if manifest is not None and resolved_grade.verdict is AssessmentVerdict.PASS:
        # PASS needs a POSITIVE, checkable justification, one of two kinds:
        #  (a) every reason names an artifact the manifest proves exists -- the
        #      assessor looked in the wrong PLACE; or
        #  (b) every reason is a closed-allowlist SURFACE complaint (no changed
        #      files / empty diff / nothing written) -- the assessor looked at the
        #      wrong SURFACE, the canonical artifact-graded-against-a-git-diff case,
        #      which names no file precisely because nothing was examined.
        # Anything else -- including a lookup-SHAPED judgement about content that
        # the denylist happens not to cover -- cannot reach PASS.
        justified = all(
            _is_surface_lookup(reason) for reason in listed
        ) or _reasons_named_in_manifest(listed, manifest)
        if justified:
            return FailOverride(
                AssessmentVerdict.PASS,
                True,
                original,
                tuple(listed),
                note=(
                    "every fail reason was a lookup failure naming an artifact the "
                    f"manifest proves exists; manifest shows {len(manifest.entries)} "
                    "file(s) with no protocol violations"
                ),
            )
        return FailOverride(
            AssessmentVerdict.NEEDS_REVIEW,
            True,
            original,
            tuple(listed),
            note=(
                "reasons were lookup-shaped but named no artifact in the manifest, "
                "so the manifest cannot contradict them -- a human decides"
            ),
        )
    return FailOverride(
        AssessmentVerdict.NEEDS_REVIEW,
        True,
        original,
        tuple(listed),
        note="every fail reason was a lookup failure but the manifest does not clear the work",
    )


# ---------------------------------------------------------------------------
# work_artifacts registry (migration 058)
# ---------------------------------------------------------------------------


class _WriterStore(Protocol):
    """The private surface of ``SqliteStore`` this DAL needs.

    Structural, exactly like ``scope.locks._WriterStore``: this module must not
    import ``omniagentos.db``, so that the workmodes package stays importable
    from anywhere in the graph and a test can hand it a fake.
    """

    @property
    def _lock(self) -> Any: ...  # noqa: D105

    @property
    def _read_lock(self) -> Any: ...  # noqa: D105

    @property
    def _connection(self) -> sqlite3.Connection: ...  # noqa: D105

    @property
    def _shared_connection(self) -> sqlite3.Connection | None: ...  # noqa: D105

    def _begin(self) -> None: ...  # noqa: D105

    def _commit(self) -> None: ...  # noqa: D105

    def _rollback(self) -> None: ...  # noqa: D105


_ARTIFACT_COLUMNS = (
    "id",
    "created_at",
    "artifact_type",
    "task_mode",
    "work_ref_type",
    "work_ref_id",
    "storage_kind",
    "file_path",
    "url",
    "content_inline",
    "mime_type",
    "sha256",
    "byte_size",
    "version",
    "lineage_id",
    "approved_at",
    "external_id",
    "metadata_json",
)


class WorkArtifactStore:
    """Durable ``work_artifacts`` rows over an existing ``SqliteStore``.

    Constructed around the store the lane already owns and never opening its own
    connection — the same rule ``scope.locks.PathLockStore`` documents: Phase 2
    serialized every writer behind one process-wide lock precisely so two DALs in
    one process cannot open two overlapping transactions, and a private
    connection here would opt straight back out of that guarantee.
    """

    def __init__(self, store: _WriterStore) -> None:
        self._store = store

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        store = self._store
        with store._lock:
            store._begin()
            try:
                yield store._connection
            except BaseException:
                store._rollback()
                raise
            store._commit()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        store = self._store
        shared = getattr(store, "_shared_connection", None)
        read_lock = getattr(store, "_read_lock", None)
        guard = read_lock if (shared is None and read_lock is not None) else store._lock
        with guard:
            yield store._connection

    # --- writes -------------------------------------------------------------

    def record(
        self,
        *,
        artifact_type: str = "deliverable",
        task_mode: TaskMode | str | None = None,
        work_ref_type: str | None = None,
        work_ref_id: str | None = None,
        file_path: str | None = None,
        sha256: str | None = None,
        byte_size: int | None = None,
        mime_type: str | None = None,
        storage_kind: str = "file_path",
        url: str | None = None,
        content_inline: str | None = None,
        lineage_id: str | None = None,
        external_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        approved_at: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Insert one artifact row; returns the stored row.

        Versioning happens INSIDE the transaction: ``version`` is
        ``max(version) + 1`` for the lineage, read and written under the same
        ``BEGIN IMMEDIATE``, so two concurrent revisions cannot both become v2.
        A row with no ``lineage_id`` starts a lineage and adopts its own id as
        the lineage key — the convention migration 058 documents.
        """
        if artifact_type not in ARTIFACT_TYPES:
            raise WorkModeError(
                f"unknown artifact_type {artifact_type!r} (allowed: {sorted(ARTIFACT_TYPES)})"
            )
        if storage_kind not in STORAGE_KINDS:
            raise WorkModeError(
                f"unknown storage_kind {storage_kind!r} (allowed: {sorted(STORAGE_KINDS)})"
            )
        mode = coerce_task_mode(task_mode)
        if task_mode is not None and mode is None:
            raise WorkModeError(f"unknown task mode: {task_mode!r}")
        artifact_id = new_id("wart")
        created = now or utc_now_iso()
        with self._write() as conn:
            if lineage_id:
                row = conn.execute(
                    "SELECT MAX(version) AS v FROM work_artifacts WHERE lineage_id = ?",
                    (lineage_id,),
                ).fetchone()
                current = row["v"] if row is not None and row["v"] is not None else 0
                version = int(current) + 1
                lineage = lineage_id
            else:
                version = 1
                lineage = artifact_id
            values = {
                "id": artifact_id,
                "created_at": created,
                "artifact_type": artifact_type,
                "task_mode": mode.value if mode is not None else None,
                "work_ref_type": work_ref_type,
                "work_ref_id": work_ref_id,
                "storage_kind": storage_kind,
                "file_path": file_path,
                "url": url,
                "content_inline": content_inline,
                "mime_type": mime_type,
                "sha256": sha256,
                "byte_size": byte_size,
                "version": version,
                "lineage_id": lineage,
                "approved_at": approved_at,
                "external_id": external_id,
                "metadata_json": json.dumps(dict(metadata or {}), sort_keys=True),
            }
            conn.execute(
                f"INSERT INTO work_artifacts ({', '.join(_ARTIFACT_COLUMNS)}) "
                f"VALUES ({', '.join(':' + name for name in _ARTIFACT_COLUMNS)})",
                values,
            )
        return dict(values)

    def record_manifest(
        self,
        manifest: ArtifactManifest,
        *,
        work_ref_type: str | None = None,
        work_ref_id: str | None = None,
        lineage_by_path: Mapping[str, str] | None = None,
        include_manifest_file: bool = False,
        now: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Register every deliverable in ``manifest``; returns the stored rows.

        ``lineage_by_path`` carries a previous revision's lineage per relative
        path, which is how a v2 keeps v1's identity: the revision loop hands back
        the lineage ids it got from the first run and each file continues its own
        lineage rather than starting a new one.
        """
        ref_type = work_ref_type or manifest.work_ref_type
        ref_id = work_ref_id or manifest.work_ref_id
        rows: list[dict[str, Any]] = []
        for entry in manifest.entries:
            base = os.path.basename(entry.rel_path)
            if base == MANIFEST_FILENAME and not include_manifest_file:
                continue
            rows.append(
                self.record(
                    artifact_type=entry.artifact_type,
                    task_mode=manifest.task_mode,
                    work_ref_type=ref_type,
                    work_ref_id=ref_id,
                    file_path=os.path.join(manifest.artifact_root, entry.rel_path),
                    sha256=entry.sha256,
                    byte_size=entry.byte_size,
                    mime_type=entry.mime_type,
                    lineage_id=(lineage_by_path or {}).get(entry.rel_path),
                    metadata={"rel_path": entry.rel_path, "scope": manifest.scope},
                    now=now,
                )
            )
        return tuple(rows)

    def approve(self, artifact_id: str, *, now: str | None = None) -> bool:
        """Stamp ``approved_at``; returns False when the row does not exist.

        Idempotent by design — re-approving keeps the FIRST approval timestamp,
        because promote's audit trail should say when the human decided, not when
        someone last clicked.
        """
        stamp = now or utc_now_iso()
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE work_artifacts SET approved_at = ? WHERE id = ? AND approved_at IS NULL",
                (stamp, artifact_id),
            )
            if cursor.rowcount:
                return True
            existing = conn.execute(
                "SELECT approved_at FROM work_artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        return existing is not None

    # --- reads --------------------------------------------------------------

    def get(self, artifact_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_ARTIFACT_COLUMNS)} FROM work_artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def approved_version(self, lineage_id: str) -> dict[str, Any] | None:
        """The APPROVED row for a lineage — the highest approved version.

        This is what promote targets. Never "the newest version" and never "the
        newest file on disk": a v3 that nobody approved must not ship because it
        happens to be the most recent thing an agent wrote.
        """
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_ARTIFACT_COLUMNS)} FROM work_artifacts "
                "WHERE lineage_id = ? AND approved_at IS NOT NULL "
                "ORDER BY version DESC, created_at DESC LIMIT 1",
                (lineage_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_version(self, lineage_id: str) -> dict[str, Any] | None:
        """The newest row in a lineage, approved or not. Informational only."""
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_ARTIFACT_COLUMNS)} FROM work_artifacts "
                "WHERE lineage_id = ? ORDER BY version DESC, created_at DESC LIMIT 1",
                (lineage_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_for_ref(
        self, work_ref_type: str, work_ref_id: str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_ARTIFACT_COLUMNS)} FROM work_artifacts "
                "WHERE work_ref_type = ? AND work_ref_id = ? "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                (work_ref_type, work_ref_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def approved_for_ref(self, work_ref_type: str, work_ref_id: str) -> list[dict[str, Any]]:
        """One approved row per lineage for a work ref, highest approved version each."""
        rows = self.list_for_ref(work_ref_type, work_ref_id)
        best: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not row.get("approved_at"):
                continue
            lineage = str(row.get("lineage_id") or row["id"])
            current = best.get(lineage)
            if current is None or int(row["version"]) > int(current["version"]):
                best[lineage] = row
        return [best[key] for key in sorted(best)]


def manifest_relative(root: str, candidate: str) -> str | None:
    """``candidate`` as a manifest-style relative path inside ``root``, or ``None``.

    Wraps :func:`omniagentos.scope.paths.resolve_into_realm` so that a path from
    a database row (which may have been written on another host, or moved) is
    re-checked for containment before anything is done with it. Absolute escapes,
    ``..`` chains and symlinks out of the root all return ``None``.
    """
    parts = resolve_into_realm(candidate, root)
    if parts is None:
        return None
    return rel_text(parts)


def normalize_expected(paths: Iterable[str]) -> tuple[str, ...]:
    """Normalize expected-file declarations; raises on anything not root-relative."""
    out: list[str] = []
    for raw in paths:
        try:
            parts = normalize_rel(raw)
        except ScopePathError as exc:
            raise WorkModeError(f"expected file is not root-relative: {raw!r}") from exc
        if not parts:
            raise WorkModeError(f"expected file cannot be the root itself: {raw!r}")
        text = "/".join(parts)
        if text not in out:
            out.append(text)
    return tuple(out)
