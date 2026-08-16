"""Fast-lane classification for the quick front door.

A **fast** task is a single, bounded, mechanical action a live Claude Code session
can just DO immediately -- "make a folder on my desktop called tiger", "create an
html file which is a bird app" -- with no need for the Orchestrator's heavy Fable
planning pass first. Anything that looks multi-step, batch, research-y, or
system-scale falls to the **planned** (orchestrate) lane, which plans before it
spawns.

The classifier is a pure heuristic: zero LLM, zero latency. The whole point of the
fast lane is that a trivial ask spawns a session in milliseconds instead of paying
for a ~minute planning step (and dollars) it never needed. When genuinely unsure it
returns ``"planned"`` -- a false "planned" only costs some planning time, while a
false "fast" could under-scope a real multi-step ask.

:func:`parse_target_location` lets a fast task actually land where the user asked --
"on my desktop" -> ``~/Desktop`` -- which the caller grants as a sandbox write root.
It is deliberately conservative: only benign, user-named locations UNDER ``$HOME``
are ever returned, and secret/system/CLI-state dirs are refused outright, so a
parse can never widen scope onto ``~/.ssh`` or a system path. (Deletion and money
moves still escalate regardless of write scope -- widening *where* a session may
create files never widens *what* it may do unattended.)
"""

from __future__ import annotations

import glob
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Literal

from omniagentos.path_containment import inode_relative_parts

Speed = Literal["fast", "planned"]
Lane = Literal["fast", "planned", "longhaul"]
ComplexityBand = Literal["simple", "standard", "complex"]
ContentDispatchMode = Literal["off", "shadow", "enforce"]

LOG = logging.getLogger(__name__)

# Phase 1 (A2): content-aware dispatch. Tri-state, DEFAULT off -> classify_task_speed
# is byte-identical to the pre-A2 heuristic (including batch-routing on any digit
# >= 4). "shadow" computes the content-aware decision and LOGs it without applying
# it. "enforce" applies: durations/dimensions no longer batch-route, and multi-phase
# / substantial creative deliverables are never banded solo_fast/simple.
CONTENT_DISPATCH_ENV = "OMNIAGENTOS_CONTENT_DISPATCH_MODE"
_CONTENT_DISPATCH_MODES: frozenset[str] = frozenset({"off", "shadow", "enforce"})

# Multi-step / batch / system-scale signals -> always planned. Spaced where a bare
# word would over-match (" plan " must not fire on "plane"/"planet").
_COMPLEX_SIGNALS: tuple[str, ...] = (
    " and then ",
    " then ",
    "after that",
    "followed by",
    "step by step",
    "step-by-step",
    "multi-step",
    "multiple steps",
    "one by one",
    " a plan",
    "plan to",
    "plan for",
    "plan the",
    "planning",
    "build a system",
    "build an app",
    "build the app",
    "build a platform",
    "build a service",
    "build a pipeline",
    "pipeline",
    "end to end",
    "end-to-end",
    "research",
    "analyze",
    "analyse",
    "investigate",
    "audit",
    "review the",
    "migrate",
    "refactor",
    "integrate",
    "deploy",
    "provision",
    "benchmark",
    "architecture",
    "design a",
    "design the",
    "strategy",
    "campaign",
    "for each",
    " across ",
    "several",
    "multiple files",
    "whole codebase",
    "entire",
    "set up the",
    "set up a",
    "wire up",
    "orchestrate",
    "scaffold",
)

# Simple single-action verbs that, in a short goal, signal the fast lane.
_SIMPLE_VERBS: tuple[str, ...] = (
    "make",
    "create",
    "write",
    "add",
    "rename",
    "move",
    "copy",
    "duplicate",
    "open",
    "save",
    "generate",
    "list",
    "show",
    "print",
    "put",
    "place",
    "touch",
    "append",
    "edit",
    "change",
    "name",
    "draft",
    "scratch",
)

_MAX_FAST_WORDS = 45

# Content-creation shape signals (creative deliverables). Spaced / phrase form
# where a bare word would over-match ordinary coding asks.
_CONTENT_CREATION_SIGNALS: tuple[str, ...] = (
    "webinar",
    "creative",
    "creatives",
    "ad copy",
    "ad creative",
    "video ad",
    "video script",
    "storyboard",
    "thumbnail",
    "voiceover",
    "voice-over",
    "b-roll",
    "broll",
    "reel ",
    " reels",
    "landing page hero",
    "funnel video",
    "promo video",
    "training video",
    "explainer video",
    "script for",
    "write a script",
    "draft a script",
    "campaign video",
    "youtube video",
    "tiktok",
    "short-form video",
    "short form video",
)

# Multi-phase / multi-stage creative work — never solo_fast/simple under enforce.
_CONTENT_MULTI_PHASE_SIGNALS: tuple[str, ...] = (
    "multi-phase",
    "multi phase",
    "multiphase",
    "multi-step",
    "multi step",
    "phase 1",
    "phase 2",
    "phase one",
    "phase two",
    "storyboard then",
    "script then",
    "then edit",
    "then produce",
    "then render",
    "hook, body",
    "hook and offer",
    "and then ",
)

# Duration unit attached to a number ("30 second", "30-minute", "5min").
_DURATION_UNIT_RE = re.compile(
    r"\b(\d{1,4})\s*[- ]?"
    r"(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)\b",
    re.IGNORECASE,
)

# Media dimensions: 1080x1080, 1080p, or bare common frame sizes (not batch counts).
_DIMENSION_PAIR_RE = re.compile(
    r"\b(\d{3,5})\s*[x×]\s*(\d{3,5})\b",
    re.IGNORECASE,
)
_DIMENSION_P_RE = re.compile(r"\b(\d{3,4})p\b", re.IGNORECASE)
_BARE_DIMENSIONS: frozenset[str] = frozenset(
    {"720", "1080", "1280", "1440", "1920", "2160", "3840"}
)

# ~30-minute (or longer) creative runtime markers.
_LONG_CREATIVE_DURATION_RE = re.compile(
    r"\b(\d{1,4})\s*[- ]?(?:min|mins|minute|minutes|h|hr|hrs|hour|hours)\b",
    re.IGNORECASE,
)


def content_dispatch_mode() -> ContentDispatchMode:
    """The tri-state ``OMNIAGENTOS_CONTENT_DISPATCH_MODE`` gate (off default)."""
    mode = os.environ.get(CONTENT_DISPATCH_ENV, "off").strip().lower()
    if mode in _CONTENT_DISPATCH_MODES:
        return mode  # type: ignore[return-value]
    return "off"


def _is_duration_or_dimension_number(text: str, match: re.Match[str]) -> bool:
    """True when a digit match is a duration or media dimension, not a batch count.

    Intent (configs/dispatch.yaml content_creation notes): "30 second", "30-minute",
    "1080", "1080x1080", "1080p" describe the deliverable shape — they must not trip
    the batch heuristic that exists for "make 10 creatives" / "create 5 files".
    """
    start, end = match.start(1), match.end(1)
    token = match.group(1)

    # Trailing unit: "30 second", "30-second", "30s", "5 min".
    after = text[end : end + 16]
    if re.match(
        r"\s*[- ]?(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
        r"h|hr|hrs|hour|hours)\b",
        after,
        re.IGNORECASE,
    ):
        return True

    # Dimension pair: the left or right side of "1080x1080" / "1080×1920".
    # Word-boundary ``\b(\d+)\b`` does not fire inside "1080x1080" (x is a word
    # char), but the left/right halves can still appear as bare tokens nearby.
    window = text[max(0, start - 8) : min(len(text), end + 8)]
    if _DIMENSION_PAIR_RE.search(window):
        return True

    # "1080p" / "720p".
    if re.match(r"p\b", text[end : end + 2], re.IGNORECASE):
        return True
    if _DIMENSION_P_RE.search(text[max(0, start - 1) : end + 2]):
        return True

    # Bare common frame sizes when not clearly a count ("1080 creatives" still
    # has "creatives" as the noun — treat bare 1080/1920/… as dimension).
    if token in _BARE_DIMENSIONS:
        return True

    return False


def _looks_like_content_creation(text: str) -> bool:
    """True when the goal is content/creative-shaped (webinar, video, ad creative…)."""
    padded = f" {text} "
    if any(sig in padded or sig in text for sig in _CONTENT_CREATION_SIGNALS):
        return True
    # Bare "video" / "script" as whole words (avoid "videography" false-negatives
    # being required; avoid "javascript" via word boundary).
    if re.search(r"\b(videos?|scripts?|ads?|webinars?)\b", text):
        return True
    return False


def _content_is_multi_phase_or_long_creative(text: str) -> bool:
    """Multi-phase language or ~30-minute (or longer) creative runtime."""
    padded = f" {text} "
    if any(sig in padded or sig in text for sig in _CONTENT_MULTI_PHASE_SIGNALS):
        return True
    for match in _LONG_CREATIVE_DURATION_RE.finditer(text):
        try:
            amount = int(match.group(1))
        except (TypeError, ValueError):
            continue
        unit = match.group(0).lower()
        # Hours always count as long creative; minutes >= 20 ≈ half-hour band.
        if "h" in unit and "min" not in unit:
            if amount >= 1:
                return True
        elif amount >= 20:
            return True
    # Any explicit duration on a content deliverable (e.g. "30 second webinar")
    # is a shaped creative asset, not a tiny code edit.
    if _DURATION_UNIT_RE.search(text):
        return True
    return False


def content_blocks_solo_fast(goal: str) -> bool:
    """True when the content band rule forbids solo_fast / simple banding.

    Multi-phase creative work, ~30-minute (or longer) deliverables, and other
    duration-shaped content assets (webinar, video ad, …) must not land on the
    solo_fast/simple session band — they need planned effort.
    """
    text = (goal or "").strip().lower()
    if not text or not _looks_like_content_creation(text):
        return False
    if _content_is_multi_phase_or_long_creative(text):
        return True
    # Named substantial creative deliverables even without an explicit duration.
    if re.search(
        r"\b(webinar|storyboard|training video|promo video|explainer video|"
        r"campaign video|funnel video|video ad|ad creative|ad creatives)\b",
        text,
    ):
        return True
    return False


def content_complexity_band(goal: str) -> ComplexityBand:
    """Content-aware complexity band (standard floor when solo_fast is blocked)."""
    if content_blocks_solo_fast(goal):
        text = (goal or "").strip().lower()
        # Multi-phase or hour-scale work floors to complex; otherwise standard.
        padded = f" {text} "
        if any(sig in padded or sig in text for sig in _CONTENT_MULTI_PHASE_SIGNALS):
            return "complex"
        for match in _LONG_CREATIVE_DURATION_RE.finditer(text):
            try:
                amount = int(match.group(1))
            except (TypeError, ValueError):
                continue
            unit = match.group(0).lower()
            if ("h" in unit and "min" not in unit and amount >= 1) or amount >= 30:
                return "complex"
        return "standard"
    return "simple"


@dataclass(frozen=True)
class ContentDispatchAssessment:
    """The content-aware decision (computed even when mode is off/shadow)."""

    speed: Speed
    band: ComplexityBand
    blocks_solo_fast: bool
    batch_routed: bool
    is_content_creation: bool
    mode: ContentDispatchMode


def _batch_routed_by_numeric(text: str, *, content_aware: bool) -> bool:
    """True if a 4+ digit would force the planned (batch) lane."""
    for match in re.finditer(r"\b(\d{1,4})\b", text):
        if int(match.group(1)) < 4:
            continue
        if content_aware and _is_duration_or_dimension_number(text, match):
            continue
        return True
    return False


def _classify_task_speed_impl(goal: str, *, content_aware: bool) -> Speed:
    """Core speed classifier. ``content_aware`` toggles A2 numeric + band rules."""
    text = (goal or "").strip().lower()
    if not text:
        return "planned"
    padded = f" {text} "

    if any(sig in padded for sig in _COMPLEX_SIGNALS):
        return "planned"

    # A count of 4+ ("make 10 creatives", "create 5 files") reads as a batch, which
    # the planned lane fans out properly. Bare word-boundary digits only, so a token
    # like "html5" or "s3" never trips it. Under content-aware mode, durations and
    # media dimensions ("30 second", "1080") are NOT batch counts.
    if _batch_routed_by_numeric(text, content_aware=content_aware):
        return "planned"

    # Content band rule: multi-phase / substantial creative work is never fast.
    if content_aware and content_blocks_solo_fast(text):
        return "planned"

    words = text.split()
    if len(words) > _MAX_FAST_WORDS:
        return "planned"

    # More than two clause-like segments usually means several distinct deliverables.
    # Split on sentence/semicolon/"..., and|then" boundaries -- a period is only a
    # boundary when followed by whitespace, so a filename ("report.txt to final.txt")
    # is never mistaken for multiple clauses.
    clauses = [c for c in re.split(r"\.\s|[;\n]|,\s*(?:and|then)\b", text) if c.strip()]
    if len(clauses) > 2:
        return "planned"

    has_simple_verb = any(
        f" {verb} " in padded or padded.lstrip().startswith(verb + " ") for verb in _SIMPLE_VERBS
    )
    if has_simple_verb:
        return "fast"

    # Short and not obviously complex -> bias to speed for tiny asks.
    return "fast" if len(words) <= 12 else "planned"


def assess_content_dispatch(goal: str) -> ContentDispatchAssessment:
    """Compute the content-aware assessment (pure; does not apply mode)."""
    text = (goal or "").strip().lower()
    is_content = _looks_like_content_creation(text) if text else False
    blocks = content_blocks_solo_fast(goal)
    batch = _batch_routed_by_numeric(text, content_aware=True) if text else False
    speed = _classify_task_speed_impl(goal, content_aware=True)
    band = content_complexity_band(goal)
    return ContentDispatchAssessment(
        speed=speed,
        band=band,
        blocks_solo_fast=blocks,
        batch_routed=batch,
        is_content_creation=is_content,
        mode=content_dispatch_mode(),
    )


def classify_task_speed(goal: str) -> Speed:
    """``"fast"`` for a single bounded action, else ``"planned"``. Never raises.

    When ``OMNIAGENTOS_CONTENT_DISPATCH_MODE`` is ``off`` (default), behavior is
    identical to the pre-A2 heuristic. ``shadow`` logs the content-aware decision
    but returns the legacy result. ``enforce`` applies content-aware rules.
    """
    mode = content_dispatch_mode()
    legacy = _classify_task_speed_impl(goal, content_aware=False)
    if mode == "off":
        return legacy

    aware = _classify_task_speed_impl(goal, content_aware=True)
    if mode == "shadow":
        assessment = assess_content_dispatch(goal)
        if aware != legacy or assessment.blocks_solo_fast:
            LOG.info(
                "content_dispatch shadow: goal=%r legacy_speed=%s "
                "content_speed=%s band=%s blocks_solo_fast=%s batch_routed=%s",
                (goal or "")[:200],
                legacy,
                aware,
                assessment.band,
                assessment.blocks_solo_fast,
                assessment.batch_routed,
            )
        return legacy

    # enforce
    return aware


def classify_task_lane(
    goal: str,
    *,
    requested: Literal["auto", "fast", "longhaul"] = "auto",
    discipline: str | None = None,
    cfg: dict | None = None,
) -> Lane:
    """Resolve an explicit lane or route planned coding work to longhaul."""

    if requested in {"fast", "longhaul"}:
        return requested
    if requested != "auto":
        raise ValueError("requested lane must be auto, fast, or longhaul")
    speed = classify_task_speed(goal)
    if speed == "fast":
        return "fast"
    settings = cfg
    if settings is None:
        from omniagentos.longhaul.config import load_config

        settings = load_config()
    coding_text = f"{discipline or ''} {goal}".lower()
    is_coding = any(
        token in coding_text
        for token in (
            "coding",
            "codebase",
            "software",
            "developer",
            "engineering",
            "implement",
            "repository",
            "repo ",
        )
    )
    if is_coding and bool(settings.get("default_for_planned_coding", True)):
        return "longhaul"
    return "planned"


# --- target-location parsing -------------------------------------------------

_HOME = os.path.realpath(os.path.expanduser("~"))

# Cloud-drive roots under ~/Library that the machine-roots registry treats as
# grantable (see configs/mounts.yaml + omniagentos.mounts). ~/Library stays
# forbidden as a whole, but these two subtrees are explicit carve-outs so an
# agent can be pointed at iCloud Drive / Google Drive / Dropbox.
_ICLOUD_ROOT = os.path.join(_HOME, "Library", "Mobile Documents", "com~apple~CloudDocs")
_CLOUDSTORAGE = os.path.join(_HOME, "Library", "CloudStorage")

# The first signed-in Google Drive "My Drive" mount (globbed the same way as
# omniagentos.drive, never hardcoding an account), falling back to the canonical
# account path from configs/mounts.yaml when no mount is present on disk yet.
_GDRIVE_MYDRIVE = next(
    iter(sorted(glob.glob(os.path.join(_CLOUDSTORAGE, "GoogleDrive-*", "My Drive")))),
    os.path.join(
        _CLOUDSTORAGE,
        "GoogleDrive-owner@acmeuni.example",
        "My Drive",
    ),
)
_DROPBOX_ROOT = os.path.join(_CLOUDSTORAGE, "Dropbox")

# Benign, user-named locations a goal commonly targets, mapped to real dirs.
# More-specific keys come FIRST so a "\bword\b" search resolves them before a
# broader alias ("icloud desktop" must win over both "icloud" and "desktop").
_NAMED_LOCATIONS: dict[str, str] = {
    "icloud desktop": os.path.join(_ICLOUD_ROOT, "Desktop"),
    "icloud drive": _ICLOUD_ROOT,
    "icloud": _ICLOUD_ROOT,
    "google drive": _GDRIVE_MYDRIVE,
    "gdrive": _GDRIVE_MYDRIVE,
    "coding projects": os.path.join(_HOME, "Coding Projects"),
    "dropbox": _DROPBOX_ROOT,
    "work": os.path.join(_HOME, "Work"),
    "desktop": os.path.join(_HOME, "Desktop"),
    "documents": os.path.join(_HOME, "Documents"),
    "downloads": os.path.join(_HOME, "Downloads"),
    "pictures": os.path.join(_HOME, "Pictures"),
    "movies": os.path.join(_HOME, "Movies"),
    "music": os.path.join(_HOME, "Music"),
}

# ~/Library stays forbidden EXCEPT these cloud-drive carve-outs, which are the
# canonical on-disk homes of iCloud Drive, Google Drive, and Dropbox.
_ALLOWED_LIBRARY_SUBTREES: tuple[str, ...] = (
    _ICLOUD_ROOT,
    _CLOUDSTORAGE,
)

# Never grant write here even if a goal names/derives it: secrets, credential
# stores, CLI state. A parse that resolves into any of these, or anywhere outside
# $HOME, is refused — so a fast task can only ever be handed a benign personal
# directory or a whitelisted cloud-drive subtree.
#
# System roots (/etc, /usr, /System, …) are NOT listed here. Containment is
# inode-true, and on macOS the firmlink spelling of $HOME is
# ``/System/Volumes/Data$HOME``; listing ``/System`` would refuse every safe
# personal path under that spelling. Outside-$HOME is already refused by the
# under-home check in :func:`_is_forbidden`.
_FORBIDDEN: tuple[str, ...] = (
    os.path.join(_HOME, ".ssh"),
    os.path.join(_HOME, ".aws"),
    os.path.join(_HOME, ".config"),
    os.path.join(_HOME, ".gnupg"),
    os.path.join(_HOME, ".claude"),
    os.path.join(_HOME, ".claude-account-2"),
    os.path.join(_HOME, "Library"),
)


def _is_under(candidate: str, root: str) -> bool:
    """True when ``candidate`` is at or under ``root`` by inode ancestry.

    Uses :func:`omniagentos.path_containment.inode_relative_parts` — never
    string ``startswith`` — so case-folded spellings on APFS and macOS firmlink
    dual-spellings of $HOME share one security verdict with the canonical path.
    Relative inputs and unprovable containment fail closed (return False).
    """
    try:
        cand = os.path.expanduser(os.fspath(candidate))
        base = os.path.expanduser(os.fspath(root))
    except (OSError, TypeError, ValueError):
        return False
    # Do not abspath() relatives — that would silently widen them into cwd and
    # contradict fail-closed. path_containment also refuses non-absolute inputs.
    if not os.path.isabs(cand) or not os.path.isabs(base):
        return False
    return inode_relative_parts(cand, base) is not None


def _parts_under(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    """True when ``parts`` is exactly ``prefix`` or starts with those components.

    On macOS (case-insensitive APFS) component comparison is case-folded so a
    missing ``~/.ssh`` still matches a candidate spelled ``~/.SSH/...``.
    """
    if not prefix or len(parts) < len(prefix):
        return False
    head = parts[: len(prefix)]
    if sys.platform == "darwin":
        return tuple(p.casefold() for p in head) == tuple(p.casefold() for p in prefix)
    return head == prefix


def _is_forbidden(path: str) -> bool:
    """True unless ``path`` is a safe personal directory under ``$HOME``.

    ~/Library is blanket-forbidden, but the iCloud Drive
    (``~/Library/Mobile Documents/com~apple~CloudDocs``) and CloudStorage
    (``~/Library/CloudStorage`` -- Google Drive, Dropbox) subtrees are explicit
    carve-outs: a path inside one of them is permitted even though it lives under
    ~/Library.

    Containment is inode-true via :func:`_is_under` / path_containment. A string
    ``startswith`` gate previously admitted ``~/.SSH`` (same inode as ``~/.ssh``
    on case-insensitive APFS) as a favourable "allowed" result. System paths
    outside $HOME fail the under-home check; personal secrets under $HOME fail
    the ``_FORBIDDEN`` check.

    Forbidden roots are matched by **home-relative parts**, not by statting the
    secret directory itself. ``inode_relative_parts(path, bad_root)`` returns
    None when ``bad_root`` does not exist yet, which would fail *open* (treat as
    allowed) on a fresh machine. Anchoring at $HOME keeps absent ``~/.ssh`` etc.
    refuseable: the candidate's missing suffix is still returned under home.
    """
    for allowed in _ALLOWED_LIBRARY_SUBTREES:
        if _is_under(path, allowed):
            # A whitelisted cloud-drive subtree, allowed despite ~/Library below.
            return False
    try:
        cand = os.path.expanduser(os.fspath(path))
    except (OSError, TypeError, ValueError):
        return True
    # Relative / unprovable: fail closed (forbidden), never favourable "allowed".
    if not os.path.isabs(cand):
        return True
    # Home-relative parts: works for firmlink spellings of $HOME and for paths
    # whose secret-dir components do not exist on disk yet.
    home_parts = inode_relative_parts(cand, _HOME)
    if home_parts is None:
        return True
    for bad in _FORBIDDEN:
        rel = os.path.relpath(bad, _HOME)
        if rel in (os.curdir, ""):
            continue
        bad_parts = tuple(p for p in rel.split(os.sep) if p and p != os.curdir)
        if _parts_under(home_parts, bad_parts):
            return True
    return False


def parse_target_location(goal: str) -> str | None:
    """The absolute directory a fast task should write into, or ``None``.

    Recognizes an explicit ``~/...`` / ``/Users/...`` path or a named location
    ("on my desktop"). Returns a *directory* (a file-looking path yields its parent)
    that has passed :func:`_is_forbidden`. ``None`` means "no explicit target" -- the
    caller then keeps the default scoped workspace, exactly as before.
    """
    text = (goal or "").strip()
    if not text:
        return None

    # 1) An explicit path token in the goal.
    for match in re.finditer(r"(~/[^\s'\"]+|/Users/[^\s'\"]+)", text):
        raw = match.group(1).rstrip(".,;:!?)")
        candidate = os.path.expanduser(raw)
        base = os.path.basename(candidate)
        # A path that looks like a file (has an extension) grants its parent dir.
        directory = (
            candidate
            if (os.path.isdir(candidate) or "." not in base)
            else os.path.dirname(candidate)
        )
        if directory and not _is_forbidden(directory):
            return os.path.realpath(directory)

    # 2) A named location keyword.
    low = text.lower()
    for name, path in _NAMED_LOCATIONS.items():
        if re.search(rf"\b{name}\b", low) and not _is_forbidden(path):
            return os.path.realpath(path)
    return None


def target_and_todo_prompt(
    goal: str, target_path: str | None, granted_roots: list[str] | None = None
) -> str:
    """The prompt for a fast-lane session: the raw goal + where to write + a nudge to
    use TodoWrite so the board shows live progress.

    ``granted_roots`` (P3) names the project's other in-scope directories so the
    agent knows the full set of paths it may touch. Machine-level mounts (iCloud,
    Google Drive, Dropbox, ~/Work, ...) are registered in ``configs/mounts.yaml``
    and become writable only when a project explicitly grants a directory under one.
    """
    parts = [goal.strip()]
    if target_path:
        example = os.path.join(target_path, "example.txt")
        parts.append(
            "IMPORTANT — write every deliverable under this exact directory, using "
            f"absolute paths: {target_path}\n"
            f"(e.g. a file 'example.txt' must be created at {example}, NOT in the "
            "current working directory)."
        )
    if granted_roots:
        named = "\n".join(f"- {root}" for root in granted_roots)
        parts.append(
            "You may also read and write within these granted project directories "
            "(absolute paths):\n"
            f"{named}\n"
            "Other machine locations (iCloud, Google Drive, Dropbox, ~/Work, ...) are "
            "registered in the mounts registry but are OUT of scope unless listed above."
        )
    parts.append(
        "Work entirely within THIS session — do NOT use the Task tool and do NOT spawn "
        "subagents; just do the work yourself. Before you start, call the TodoWrite "
        "tool with a short checklist of the concrete steps, and mark each todo "
        "in_progress then completed as you go, so progress stays visible on the board. "
        "Do exactly what was asked — nothing more, and stop when it is done."
    )
    parts.append(
        "Deliverables: write final output files into the `outputs/` folder inside your working "
        "directory (it already exists). Only write elsewhere if the user explicitly names a "
        "destination. Files a user gave you are in `uploads/`."
    )
    # OmniAgentOS: prefer the governed toolplane for file/connector ops when
    # available (CLI shim). Native tools still work under policy + Seatbelt.
    parts.append(
        "Tool access (OmniAgentOS): when you need scoped file ops, media checks, or "
        "connector calls, prefer the `omniagentos-tool` CLI "
        "(`omniagentos-tool <tool> --manifest <json>`) so capability, scope, and secret "
        "scrubbing are enforced in-process. Do not invent credentials; never print secrets."
    )
    return "\n\n".join(parts)


__all__ = [
    "Speed",
    "Lane",
    "ComplexityBand",
    "ContentDispatchMode",
    "ContentDispatchAssessment",
    "CONTENT_DISPATCH_ENV",
    "content_dispatch_mode",
    "content_blocks_solo_fast",
    "content_complexity_band",
    "assess_content_dispatch",
    "classify_task_lane",
    "classify_task_speed",
    "parse_target_location",
    "target_and_todo_prompt",
]
