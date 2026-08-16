"""TN.3 — artifact protocols: the non-code equivalent of a declared-output contract.

A code task can be checked against its diff. A report, an ad, an image or a video
has no diff, so "the agent said it did the work" is the only evidence unless
something else supplies it. This module supplies it, in four layers:

1. **Per-mode protocol** (:data:`PROTOCOLS`) — provider, model, default and
   allowed formats, whether a generation prompt is mandatory, how many outputs
   are permitted, a minimum byte size, and the files the manifest must always
   carry. ``image`` requires a prompt and at least ~1 KiB per file (a 200-byte
   PNG is an error page, not a deliverable); ``video`` additionally has its
   duration probed with ``ffprobe`` when ``ffprobe`` exists and degrades to "not
   checked" when it does not.

2. **Executable-format warning** — a deliverable that arrives as ``.sh``,
   ``.exe`` or ``.command`` is not refused (a legitimate task can produce one)
   but is always surfaced. Silence there is how a "content" task ships a payload.

3. **Per-platform ad-copy profiles** (:data:`PLATFORM_PROFILES`) — field
   character limits (Meta headline 40 / primary text 125, Google RSA headline 30
   / description 90), variant-slot counts, required disclaimers and a
   banned-claims lexicon. The lexicon is NEGATION-AWARE, reusing
   :func:`omniagentos.workmodes.modes.is_negated`: "results are not guaranteed"
   is the standard disclaimer, and a checker that flags it is a checker that gets
   switched off within a week.

4. **Rubrics and acceptance criteria** — a per-mode rubric plus per-task criteria
   that get recorded in the manifest, so the artifact-mode assessor grades
   against stated criteria instead of improvising a standard per run.

Nothing here reads the network or calls a model. It is all data plus pure
validation, so the assessor's judgement is applied to a set of facts that were
established without it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from omniagentos.contracts import TaskMode
from omniagentos.workmodes.modes import WorkModeError, coerce_task_mode, is_negated

__all__ = [
    "AD_DISCLAIMER_PRESETS",
    "BANNED_CLAIMS",
    "EXECUTABLE_SUFFIXES",
    "MANIFEST_FILENAME",
    "MODE_RUBRICS",
    "PLATFORM_PROFILES",
    "PROMPT_FILENAME",
    "PROTOCOLS",
    "AcceptanceCriteria",
    "AdCopyFinding",
    "AdCopyReport",
    "ArtifactProtocol",
    "FileCheck",
    "PlatformProfile",
    "ProtocolReport",
    "RubricCriterion",
    "VariantSlot",
    "acceptance_to_json",
    "build_acceptance",
    "check_files",
    "platform_profile",
    "probe_duration_s",
    "protocol_for",
    "validate_ad_copy",
]

#: The manifest file itself. Named here rather than in ``manifest.py`` so a
#: protocol can require it without importing the module that writes it.
MANIFEST_FILENAME = "manifest.json"

#: Where a generation prompt is recorded for the modes that require one. An
#: image or video whose prompt was not kept cannot be revised, re-run or
#: audited — the prompt IS the source for those modes.
PROMPT_FILENAME = "prompt.txt"


@dataclass(frozen=True)
class ArtifactProtocol:
    """The declared-output contract for one non-code mode.

    ``provider``/``model`` are ``None`` for the modes this repo has no wiring for
    yet (image, video). ``None`` is the honest answer and it is load-bearing:
    :attr:`requires_wiring` turns it into a named, testable precondition instead
    of a plausible-looking model id that no adapter would accept.
    """

    mode: TaskMode
    provider: str | None
    model: str | None
    default_format: str
    allowed_formats: tuple[str, ...]
    require_prompt: bool
    max_outputs: int
    min_bytes: int
    manifest_files: tuple[str, ...]
    warn_on_executable_format: bool = True
    min_duration_s: float | None = None
    max_duration_s: float | None = None

    @property
    def requires_wiring(self) -> bool:
        """True when no provider is configured for this mode in this repo yet."""
        return self.provider is None

    def env_provider(self) -> str | None:
        """Provider override from ``OMNIAGENTOS_WORKMODE_<MODE>_PROVIDER``."""
        return (
            os.environ.get(f"OMNIAGENTOS_WORKMODE_{self.mode.value.upper()}_PROVIDER")
            or self.provider
        )

    def env_model(self) -> str | None:
        """Model override from ``OMNIAGENTOS_WORKMODE_<MODE>_MODEL``."""
        return os.environ.get(f"OMNIAGENTOS_WORKMODE_{self.mode.value.upper()}_MODEL") or self.model


#: Per-mode protocols. ``code`` deliberately has NO entry: a code task is graded
#: against its diff by the TN.0 declared-vs-observed path, and giving it an
#: artifact protocol would create a second, weaker way to call code work done.
#:
#: Text modes route to ``cli-claude`` — the harness name this repo's adapter
#: registry actually resolves (adapters/registry.py) — at ``opus``, because these
#: are the deliverables a human reads and quality is the priority that decides
#: model choice. Image and video have no provider wired here; that is a fact
#: about this repo, not a placeholder to be quietly filled in.
PROTOCOLS: dict[TaskMode, ArtifactProtocol] = {
    TaskMode.REPORT: ArtifactProtocol(
        mode=TaskMode.REPORT,
        provider="cli-claude",
        model="opus",
        default_format="md",
        allowed_formats=("md", "pdf", "docx", "html", "txt"),
        require_prompt=False,
        max_outputs=8,
        min_bytes=512,
        manifest_files=(MANIFEST_FILENAME,),
    ),
    TaskMode.CONTENT: ArtifactProtocol(
        mode=TaskMode.CONTENT,
        provider="cli-claude",
        model="opus",
        default_format="md",
        allowed_formats=("md", "txt", "json", "csv", "html", "docx"),
        require_prompt=False,
        max_outputs=32,
        min_bytes=16,
        manifest_files=(MANIFEST_FILENAME,),
    ),
    TaskMode.IMAGE: ArtifactProtocol(
        mode=TaskMode.IMAGE,
        provider=None,
        model=None,
        default_format="png",
        allowed_formats=("png", "jpg", "jpeg", "webp"),
        require_prompt=True,
        max_outputs=12,
        min_bytes=1024,
        manifest_files=(MANIFEST_FILENAME, PROMPT_FILENAME),
    ),
    TaskMode.VIDEO: ArtifactProtocol(
        mode=TaskMode.VIDEO,
        provider=None,
        model=None,
        default_format="mp4",
        allowed_formats=("mp4", "webm", "mov"),
        require_prompt=True,
        max_outputs=4,
        min_bytes=16384,
        manifest_files=(MANIFEST_FILENAME, PROMPT_FILENAME),
        min_duration_s=1.0,
        max_duration_s=900.0,
    ),
    TaskMode.INTAKE_PROCESSING: ArtifactProtocol(
        mode=TaskMode.INTAKE_PROCESSING,
        provider="cli-claude",
        model="opus",
        default_format="md",
        allowed_formats=("md", "txt", "json", "csv", "html", "pdf", "xlsx"),
        require_prompt=False,
        max_outputs=64,
        min_bytes=1,
        manifest_files=(MANIFEST_FILENAME,),
    ),
}


def protocol_for(mode: TaskMode | str) -> ArtifactProtocol | None:
    """The protocol for ``mode``, or ``None`` for ``code`` / an unknown mode.

    ``None`` means "this mode is not graded from a manifest", which is exactly
    true for ``code`` and is why the return type is optional rather than a raise.
    """
    resolved = coerce_task_mode(mode)
    if resolved is None:
        return None
    return PROTOCOLS.get(resolved)


#: Suffixes that make a deliverable executable somewhere. Warned, never refused:
#: a legitimate report can ship an ``install.sh``, and a checker that blocks it
#: teaches people to rename the file.
EXECUTABLE_SUFFIXES: frozenset[str] = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "command",
        "exe",
        "bat",
        "cmd",
        "com",
        "scr",
        "ps1",
        "psm1",
        "vbs",
        "js",
        "jar",
        "msi",
        "app",
        "dmg",
        "pkg",
        "deb",
        "rpm",
        "apk",
        "so",
        "dylib",
        "dll",
        "py",
        "rb",
        "pl",
    }
)


def _suffix(path: str) -> str:
    return os.path.splitext(path)[1].lstrip(".").lower()


def probe_duration_s(path: str, *, timeout_s: float = 10.0) -> float | None:
    """Media duration in seconds via ``ffprobe``, or ``None`` when unknown.

    DEGRADES GRACEFULLY on purpose, and the distinction matters: ``None`` means
    "not checked" (no ffprobe on this host, an unreadable container, a timeout),
    never "zero seconds". A validator that treated a missing probe as a
    zero-length video would fail every deliverable on a machine without ffmpeg
    installed — i.e. would make the feature depend on an undeclared system
    package.
    """
    binary = shutil.which("ffprobe")
    if not binary or not os.path.isfile(path):
        return None
    try:
        completed = subprocess.run(  # noqa: S603 -- fixed argv, never shell
            (
                binary,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        value = float(completed.stdout.strip())
    except (TypeError, ValueError):
        return None
    if value != value or value < 0:  # NaN / negative -> unusable
        return None
    return value


@dataclass(frozen=True)
class FileCheck:
    """One deliverable file, checked against its mode's protocol."""

    path: str
    rel_path: str
    fmt: str
    byte_size: int
    duration_s: float | None = None
    violations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class ProtocolReport:
    """The protocol verdict over a whole artifact set."""

    mode: TaskMode
    files: tuple[FileCheck, ...]
    violations: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.violations


def check_files(
    mode: TaskMode | str,
    files: Sequence[tuple[str, str, int]],
    *,
    prompt: str | None = None,
    probe: bool = True,
) -> ProtocolReport:
    """Validate ``(abs_path, rel_path, byte_size)`` triples against the mode's protocol.

    Takes sizes from the caller rather than re-``stat``-ing so the manifest and
    this check describe the SAME bytes: a file that changed between the hash and
    the check would otherwise produce a report about a file that no longer exists
    in that form.
    """
    resolved = coerce_task_mode(mode)
    if resolved is None:
        raise WorkModeError(f"unknown task mode: {mode!r}")
    protocol = PROTOCOLS.get(resolved)
    if protocol is None:
        return ProtocolReport(
            mode=resolved,
            files=(),
            violations=(),
            warnings=("code mode has no artifact protocol; grade the diff instead",),
        )

    checks: list[FileCheck] = []
    violations: list[str] = []
    warnings: list[str] = []

    if protocol.require_prompt and not (prompt or "").strip():
        violations.append(
            f"{protocol.mode.value} requires a generation prompt and none was recorded"
        )

    deliverables = [
        (abs_path, rel_path, size)
        for abs_path, rel_path, size in files
        if os.path.basename(rel_path) not in {MANIFEST_FILENAME, PROMPT_FILENAME}
    ]
    if not deliverables:
        violations.append("no deliverable files were produced")
    if len(deliverables) > protocol.max_outputs:
        violations.append(
            f"{len(deliverables)} outputs exceeds max_outputs={protocol.max_outputs} for "
            f"{protocol.mode.value}"
        )

    for abs_path, rel_path, size in deliverables:
        fmt = _suffix(rel_path)
        file_violations: list[str] = []
        file_warnings: list[str] = []
        if fmt not in protocol.allowed_formats:
            file_violations.append(
                f"format '{fmt or '(none)'}' is not allowed for {protocol.mode.value} "
                f"(allowed: {', '.join(protocol.allowed_formats)})"
            )
        if size < protocol.min_bytes:
            file_violations.append(
                f"{size} bytes is below the {protocol.min_bytes}-byte floor for "
                f"{protocol.mode.value}"
            )
        if protocol.warn_on_executable_format and fmt in EXECUTABLE_SUFFIXES:
            file_warnings.append(
                f"'{fmt}' is an executable format for a {protocol.mode.value} deliverable"
            )

        duration: float | None = None
        if probe and (protocol.min_duration_s is not None or protocol.max_duration_s is not None):
            duration = probe_duration_s(abs_path)
            if duration is None:
                file_warnings.append("duration not checked (ffprobe unavailable or unreadable)")
            else:
                if protocol.min_duration_s is not None and duration < protocol.min_duration_s:
                    file_violations.append(
                        f"duration {duration:.2f}s is under the {protocol.min_duration_s}s minimum"
                    )
                if protocol.max_duration_s is not None and duration > protocol.max_duration_s:
                    file_violations.append(
                        f"duration {duration:.2f}s is over the {protocol.max_duration_s}s maximum"
                    )

        checks.append(
            FileCheck(
                path=abs_path,
                rel_path=rel_path,
                fmt=fmt,
                byte_size=size,
                duration_s=duration,
                violations=tuple(file_violations),
                warnings=tuple(file_warnings),
            )
        )
        violations.extend(f"{rel_path}: {text}" for text in file_violations)
        warnings.extend(f"{rel_path}: {text}" for text in file_warnings)

    if protocol.requires_wiring:
        warnings.append(
            f"no provider is wired for {protocol.mode.value} in this repo "
            f"(set OMNIAGENTOS_WORKMODE_{protocol.mode.value.upper()}_PROVIDER)"
        )

    return ProtocolReport(
        mode=protocol.mode,
        files=tuple(checks),
        violations=tuple(violations),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Ad-copy platform profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VariantSlot:
    """One named slot in an ad's variant schema.

    Character limits and slot COUNTS live in the same object because they fail
    together in practice: a Google RSA rejected for "3 headlines required" and
    one rejected for "headline is 34 characters" are the same class of incident
    (the ad never runs), and splitting them across two validators means one of
    them gets skipped.
    """

    name: str
    max_chars: int
    min_count: int = 1
    max_count: int = 1
    required: bool = True


@dataclass(frozen=True)
class PlatformProfile:
    """Field limits, slot schema, disclaimers and banned claims for one platform."""

    platform: str
    slots: tuple[VariantSlot, ...]
    required_disclaimers: tuple[str, ...] = ()
    banned_claims: tuple[str, ...] = ()
    notes: str = ""

    def slot(self, name: str) -> VariantSlot | None:
        for slot in self.slots:
            if slot.name == name:
                return slot
        return None


#: Character limits are the platforms' published display limits at time of
#: writing: Meta headline 40 / primary text 125 / link description 30; Google
#: responsive search ads headline 30 / description 90, 3-15 headlines and 2-4
#: descriptions. They are DATA so that a limit change is a one-line edit rather
#: than a code change, and so a caller can register a house profile of its own.
PLATFORM_PROFILES: dict[str, PlatformProfile] = {
    "meta": PlatformProfile(
        platform="meta",
        slots=(
            VariantSlot("headline", max_chars=40, min_count=1, max_count=5),
            VariantSlot("primary_text", max_chars=125, min_count=1, max_count=5),
            VariantSlot("description", max_chars=30, min_count=0, max_count=5, required=False),
        ),
        notes="Meta feed placement display limits; longer text is truncated, not rejected.",
    ),
    "google_rsa": PlatformProfile(
        platform="google_rsa",
        slots=(
            VariantSlot("headline", max_chars=30, min_count=3, max_count=15),
            VariantSlot("description", max_chars=90, min_count=2, max_count=4),
        ),
        notes="Google responsive search ad; headlines/descriptions are hard limits.",
    ),
}

#: Disclaimer presets a caller opts into per campaign. Deliberately NOT attached
#: to a platform profile: whether income language needs a disclaimer is a fact
#: about the OFFER, not about Meta.
AD_DISCLAIMER_PRESETS: dict[str, tuple[str, ...]] = {
    "income": ("results not typical", "individual results vary"),
    "health": ("not medical advice",),
    "crypto": ("not financial advice",),
}

#: Banned-claim patterns, applied to every slot of every platform. Matched
#: case-insensitively and skipped when negated, so "no risk" is flagged and
#: "results are not guaranteed" is not.
BANNED_CLAIMS: tuple[str, ...] = (
    r"\bguarantee\w*\s+(income|results?|returns?|profits?|approval)\b",
    r"\b100%\s*(guaranteed|safe|effective|risk[- ]free)\b",
    r"\brisk[- ]free\b",
    r"\bno risk\b",
    r"\bget rich quick\b",
    r"\bmake \$?\d[\d,]*k?\s*(a|per)\s*(day|week|month)\b",
    r"\bdouble your (money|investment)\b",
    r"\bovernight (success|results)\b",
    r"\bmiracle (cure|drug|treatment)\b",
    r"\bcures?\s+(cancer|diabetes|covid)\b",
    r"\bfda[- ]approved\b",
    r"\binstant (results|weight loss)\b",
    r"\bguaranteed\b",
)

_COMPILED_BANNED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (pattern, re.compile(pattern, re.IGNORECASE)) for pattern in BANNED_CLAIMS
)


def platform_profile(platform: str) -> PlatformProfile | None:
    """The profile for ``platform`` (case/format-insensitive), or ``None``."""
    key = (platform or "").strip().lower().replace("-", "_").replace(" ", "_")
    return PLATFORM_PROFILES.get(key)


@dataclass(frozen=True)
class AdCopyFinding:
    """One compliance/limit finding, addressed to a slot and index where possible."""

    kind: str
    slot: str
    index: int | None
    detail: str
    severity: str = "violation"


@dataclass(frozen=True)
class AdCopyReport:
    platform: str
    findings: tuple[AdCopyFinding, ...]

    @property
    def violations(self) -> tuple[AdCopyFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "violation")

    @property
    def warnings(self) -> tuple[AdCopyFinding, ...]:
        return tuple(f for f in self.findings if f.severity != "violation")

    @property
    def ok(self) -> bool:
        return not self.violations


def validate_ad_copy(
    platform: str,
    variants: Mapping[str, Sequence[str]],
    *,
    disclaimers: Iterable[str] = (),
    banned: Iterable[str] = (),
) -> AdCopyReport:
    """Validate an ad-copy variant set against a platform profile.

    ``variants`` is ``{slot_name: [text, ...]}`` — the shape a copywriting task
    actually returns. Findings cover: unknown slot, missing required slot, too
    few / too many variants, empty text, over the character limit, a duplicate
    variant within a slot, a banned claim, and a missing required disclaimer.

    Character counting is on the raw string. Not a graphemes-vs-code-points
    subtlety worth solving here: the platforms count UTF-16 units and the
    difference only shows up in emoji-heavy copy, where being one character
    conservative is the safe direction.
    """
    profile = platform_profile(platform)
    if profile is None:
        return AdCopyReport(
            platform=platform,
            findings=(
                AdCopyFinding(
                    "unknown_platform", "", None, f"no profile for platform {platform!r}"
                ),
            ),
        )

    findings: list[AdCopyFinding] = []
    known = {slot.name for slot in profile.slots}
    for name in variants:
        if name not in known:
            findings.append(
                AdCopyFinding(
                    "unknown_slot",
                    name,
                    None,
                    f"{profile.platform} has no slot named {name!r} "
                    f"(known: {', '.join(sorted(known))})",
                )
            )

    all_text: list[str] = []
    for slot in profile.slots:
        values = [str(v) for v in variants.get(slot.name, ())]
        all_text.extend(values)
        if not values:
            if slot.required and slot.min_count > 0:
                findings.append(
                    AdCopyFinding(
                        "missing_slot", slot.name, None, f"slot {slot.name!r} is required"
                    )
                )
            continue
        if len(values) < slot.min_count:
            findings.append(
                AdCopyFinding(
                    "too_few",
                    slot.name,
                    None,
                    f"{len(values)} variant(s); {profile.platform} needs at least {slot.min_count}",
                )
            )
        if len(values) > slot.max_count:
            findings.append(
                AdCopyFinding(
                    "too_many",
                    slot.name,
                    None,
                    f"{len(values)} variant(s); {profile.platform} accepts at most {slot.max_count}",
                )
            )
        seen: dict[str, int] = {}
        for index, text in enumerate(values):
            stripped = text.strip()
            if not stripped:
                findings.append(AdCopyFinding("empty", slot.name, index, "variant is empty"))
                continue
            if len(text) > slot.max_chars:
                findings.append(
                    AdCopyFinding(
                        "over_limit",
                        slot.name,
                        index,
                        f"{len(text)} chars exceeds the {slot.max_chars}-char limit for "
                        f"{profile.platform} {slot.name}",
                    )
                )
            key = stripped.lower()
            if key in seen:
                findings.append(
                    AdCopyFinding(
                        "duplicate",
                        slot.name,
                        index,
                        f"identical to variant {seen[key]}",
                        severity="warning",
                    )
                )
            else:
                seen[key] = index
            findings.extend(_banned_findings(text, slot.name, index, profile, banned))

    joined = "\n".join(all_text).lower()
    required = [*profile.required_disclaimers, *(str(d) for d in disclaimers)]
    for phrase in required:
        needle = phrase.strip().lower()
        if needle and needle not in joined:
            findings.append(
                AdCopyFinding(
                    "missing_disclaimer", "", None, f"required disclaimer absent: {phrase!r}"
                )
            )

    return AdCopyReport(platform=profile.platform, findings=tuple(findings))


def _banned_findings(
    text: str,
    slot: str,
    index: int,
    profile: PlatformProfile,
    extra: Iterable[str],
) -> list[AdCopyFinding]:
    """Banned-claim hits in one variant, skipping negated occurrences.

    The negation check is the difference between a lexicon people keep and a
    lexicon people disable. "Results are not guaranteed" contains the banned word
    ``guaranteed`` and is the exact phrase compliance ASKS for; flagging it once
    is a false positive, flagging it on every ad is a decision to stop reading
    the report.
    """
    patterns: list[tuple[str, re.Pattern[str]]] = list(_COMPILED_BANNED)
    patterns.extend((raw, re.compile(raw, re.IGNORECASE)) for raw in profile.banned_claims)
    patterns.extend((raw, re.compile(raw, re.IGNORECASE)) for raw in extra)

    out: list[AdCopyFinding] = []
    # Reported spans, so overlapping rules ("guaranteed income" and the broader
    # "guaranteed") report the phrase ONCE. The specific rule is listed first in
    # BANNED_CLAIMS and therefore wins, which is the more useful message.
    claimed: list[tuple[int, int]] = []
    for raw, pattern in patterns:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < c_end and c_start < end for c_start, c_end in claimed):
                continue
            if is_negated(text, start):
                continue
            claimed.append((start, end))
            out.append(
                AdCopyFinding(
                    "banned_claim", slot, index, f"banned claim {match.group(0)!r} (rule {raw})"
                )
            )
    return out


# ---------------------------------------------------------------------------
# Rubrics + per-task acceptance criteria
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RubricCriterion:
    """One thing the assessor is asked to judge, phrased as a checkable claim."""

    key: str
    text: str
    weight: int = 1


#: Per-mode rubrics. These exist so the artifact-mode assessor grades against a
#: STATED standard instead of improvising one per run — two runs of the same
#: report task currently get two different bars, and the difference is invisible
#: because neither bar was written down.
MODE_RUBRICS: dict[TaskMode, tuple[RubricCriterion, ...]] = {
    TaskMode.REPORT: (
        RubricCriterion("answers_question", "Answers the question that was actually asked.", 3),
        RubricCriterion("evidence", "Every non-obvious claim names its source or its data.", 3),
        RubricCriterion("structure", "Has a lead, a body a reader can skim, and a conclusion.", 2),
        RubricCriterion(
            "uncertainty", "States what is uncertain rather than smoothing it over.", 2
        ),
        RubricCriterion("length", "Length matches the ask; no padding.", 1),
    ),
    TaskMode.CONTENT: (
        RubricCriterion("brief_fit", "Matches the brief's audience, offer and channel.", 3),
        RubricCriterion("specific", "Concrete and specific; no interchangeable filler.", 2),
        RubricCriterion("compliance", "No banned claims; required disclaimers present.", 3),
        RubricCriterion("limits", "Every field is within its platform character limit.", 2),
        RubricCriterion("variants", "Variants are genuinely different angles, not rewordings.", 2),
    ),
    TaskMode.IMAGE: (
        RubricCriterion("prompt_fit", "Depicts what the prompt asked for.", 3),
        RubricCriterion("usable", "Correct format and resolution for its stated placement.", 2),
        RubricCriterion(
            "text_legible", "Any text in the image is spelled correctly and legible.", 2
        ),
        RubricCriterion("artifacts", "No obvious generation artifacts in the focal area.", 2),
    ),
    TaskMode.VIDEO: (
        RubricCriterion("prompt_fit", "Shows what the prompt asked for.", 3),
        RubricCriterion("duration", "Duration is within the requested range.", 2),
        RubricCriterion(
            "playable", "Plays end to end in a standard player; audio present if asked.", 3
        ),
        RubricCriterion("continuity", "No mid-clip discontinuity or frozen frames.", 2),
    ),
    TaskMode.INTAKE_PROCESSING: (
        RubricCriterion("coverage", "Every input file is accounted for in the output.", 3),
        RubricCriterion("fidelity", "Extracted values match the source; nothing invented.", 3),
        RubricCriterion("inputs_intact", "The input files are unmodified.", 3),
        RubricCriterion("schema", "Output matches the requested schema/format.", 2),
    ),
}


@dataclass(frozen=True)
class AcceptanceCriteria:
    """The per-task bar, recorded in the manifest and handed to the assessor.

    ``expected_files`` is the part that makes a FAST-PASS possible (TN.4): if the
    manifest shows those files present and the protocol found no violation, the
    assessor has nothing left to disagree with about existence, and can either
    skip or confine itself to judgement.
    """

    mode: TaskMode
    rubric: tuple[RubricCriterion, ...] = ()
    expected_files: tuple[str, ...] = ()
    must_include: tuple[str, ...] = ()
    must_not_include: tuple[str, ...] = ()
    platform: str | None = None
    notes: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)


def build_acceptance(
    mode: TaskMode | str,
    *,
    expected_files: Iterable[str] = (),
    must_include: Iterable[str] = (),
    must_not_include: Iterable[str] = (),
    platform: str | None = None,
    notes: str = "",
    extra_criteria: Iterable[RubricCriterion] = (),
    extra: Mapping[str, Any] | None = None,
) -> AcceptanceCriteria:
    """Assemble the per-task acceptance criteria from the mode rubric plus extras."""
    resolved = coerce_task_mode(mode)
    if resolved is None:
        raise WorkModeError(f"unknown task mode: {mode!r}")
    rubric = (*MODE_RUBRICS.get(resolved, ()), *extra_criteria)
    return AcceptanceCriteria(
        mode=resolved,
        rubric=rubric,
        expected_files=tuple(str(f) for f in expected_files),
        must_include=tuple(str(t) for t in must_include),
        must_not_include=tuple(str(t) for t in must_not_include),
        platform=platform,
        notes=notes,
        extra=dict(extra or {}),
    )


def acceptance_to_json(acceptance: AcceptanceCriteria | None) -> dict[str, Any]:
    """JSON-safe form for the manifest. ``None`` -> ``{}`` (no criteria stated)."""
    if acceptance is None:
        return {}
    return {
        "mode": acceptance.mode.value,
        "rubric": [{"key": c.key, "text": c.text, "weight": c.weight} for c in acceptance.rubric],
        "expected_files": list(acceptance.expected_files),
        "must_include": list(acceptance.must_include),
        "must_not_include": list(acceptance.must_not_include),
        "platform": acceptance.platform,
        "notes": acceptance.notes,
        "extra": dict(acceptance.extra),
    }
