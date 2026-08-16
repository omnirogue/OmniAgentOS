"""TN.1 — task modes, artifact roots, deterministic mode inference, task splitting.

``contracts.TaskMode`` says what KIND of output a task produces. This module says
what that KIND is *allowed to touch*, and it is the half that makes the system
useful for webinars, ad copy, images and reports rather than only code:

===================  =========================================================
mode                 provisioned roots
===================  =========================================================
``code``             declared file scope ONLY — no artifact roots at all
``report``           ``var/artifacts/<scope>/`` writable
``content``          ``var/artifacts/<scope>/`` writable
``image``            ``var/artifacts/<scope>/`` writable
``video``            ``var/artifacts/<scope>/`` writable
``intake_processing````var/inputs/<scope>/`` READ-ONLY, ``var/workspace/<scope>/``
                     mutable, ``var/artifacts/<scope>/`` writable
===================  =========================================================

Three things live here that are easy to get wrong, so they are written once:

1. **Root resolution** reuses :mod:`omniagentos.scope.paths` for every
   containment question and :func:`omniagentos.intake.fastlane.parse_target_location`
   for "where did the human say to put it". No new path algebra is invented —
   ``safe_component`` builds the scope directory name, ``resolve_into_realm``
   answers containment, and ``normalize_rel`` validates every relative path.

2. **Mode inference is deterministic and negation-aware.** A planner label is
   never trusted alone (that is how a copywriting task ends up with a repo-scoped
   worker holding ``--permission-mode acceptEdits``), so a regex classifier runs
   over the request text and :func:`reconcile_task_mode` joins the two. The
   classifier discards a signal that sits inside a negation window: *"not a
   report, a video"* must not classify as ``report``. A keyword scan without
   negation handling is worse than no scan, because it is confidently wrong.

3. **A task that declares BOTH repo files and artifact outputs is split**, not
   coerced. One unit cannot be simultaneously repo-scoped and forbidden the repo,
   and picking either interpretation silently loses half the work.
   :func:`split_units` produces two units with rewired dependencies instead.

SHIP DARK: nothing here has a side effect unless you call
:func:`provision_roots` (the only function that touches the filesystem), and the
enforcement gate :func:`work_modes_mode` defaults to ``off``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from omniagentos.contracts import TaskMode
from omniagentos.scope.paths import ScopePathError, normalize_rel, safe_component

__all__ = [
    "ARTIFACTS_ROOT_NAME",
    "DEFAULT_ARTIFACT_MODE",
    "INPUTS_ROOT_NAME",
    "SPLIT_ARTIFACT_SUFFIX",
    "SPLIT_CODE_SUFFIX",
    "WORKSPACE_ROOT_NAME",
    "WORK_MODES_ENV",
    "ModeDecision",
    "ModeInference",
    "ModePolicy",
    "ModeRoots",
    "SplitResult",
    "WorkModeError",
    "WorkModeGate",
    "WorkUnit",
    "artifact_modes",
    "coerce_task_mode",
    "infer_task_mode",
    "is_negated",
    "mode_scope_slug",
    "policy_for",
    "provision_roots",
    "reconcile_task_mode",
    "resolve_roots",
    "split_mixed_unit",
    "split_units",
    "target_location_for",
    "var_root",
    "work_modes_enabled",
    "work_modes_enforcing",
    "work_modes_mode",
]


class WorkModeError(ValueError):
    """A work-mode declaration cannot be honored as written.

    Subclasses ``ValueError`` so the intake/planner call sites that already catch
    ``ValueError`` around scope handling keep behaving as they do today.
    """


# ---------------------------------------------------------------------------
# The rollout gate. Off by default -- the whole package is inert until a lane
# both calls it AND the operator turns enforcement on.
# ---------------------------------------------------------------------------

WorkModeGate = Literal["off", "observe", "enforce"]

#: The three gate values, in increasing order of consequence. Same vocabulary as
#: ``scope.config.SCOPE_MODES`` on purpose: an operator should not have to learn
#: a second set of words for the same off/measure/act ramp.
WORK_MODE_GATES: tuple[WorkModeGate, ...] = ("off", "observe", "enforce")

WORK_MODES_ENV = "OMNIAGENTOS_WORK_MODES"

#: Off by default. A guard that can refuse a worker's writes ships dark and is
#: turned on deliberately, per host, after an observe soak.
DEFAULT_WORK_MODE_GATE: WorkModeGate = "off"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _coerce_gate(value: Any) -> WorkModeGate | None:
    """Parse one gate spelling; ``None`` when the value says nothing.

    Mirrors ``scope.config._coerce_mode`` exactly, including the two traps it
    documents: a bare YAML ``mode: off`` parses as the BOOLEAN ``False`` and must
    mean the OFF GATE, and an unparseable value (a typo like ``enfroce``) is
    ignored rather than read as "on" — a typo must never start refusing writes.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "enforce" if value else "off"
    text = str(value).strip().lower()
    if not text:
        return None
    if text in WORK_MODE_GATES:
        return text  # type: ignore[return-value]
    if text in _TRUTHY:
        return "enforce"
    if text in _FALSY:
        return "off"
    return None


def work_modes_mode() -> WorkModeGate:
    """The effective gate: env override, then ``configs/parallelism.yaml``, then ``off``.

    Reads the EXISTING parallelism config file through the public
    :func:`omniagentos.scope.config.parallelism_config` rather than adding a
    second config surface; the section is ``work_modes:``. A missing file, a
    missing section and a broken file all resolve to ``off``, i.e. fail closed
    with respect to refusing anybody's writes.
    """
    from_env = _coerce_gate(os.environ.get(WORK_MODES_ENV))
    if from_env is not None:
        return from_env
    from omniagentos.scope.config import parallelism_config

    section = parallelism_config().get("work_modes")
    if isinstance(section, dict):
        from_config = _coerce_gate(section.get("mode"))
        if from_config is not None:
            return from_config
    return DEFAULT_WORK_MODE_GATE


def work_modes_enabled() -> bool:
    """True when the artificer guard evaluates and records (``observe``/``enforce``)."""
    return work_modes_mode() != "off"


def work_modes_enforcing() -> bool:
    """True only in ``enforce`` — i.e. when a guard violation actually refuses."""
    return work_modes_mode() == "enforce"


# ---------------------------------------------------------------------------
# Root layout
# ---------------------------------------------------------------------------

ARTIFACTS_ROOT_NAME = "artifacts"
WORKSPACE_ROOT_NAME = "workspace"
INPUTS_ROOT_NAME = "inputs"

#: Root kinds in provisioning order (inputs first: an intake job reads before it
#: writes, and a stable order keeps a provisioning log diffable).
ROOT_KINDS: tuple[str, ...] = (INPUTS_ROOT_NAME, WORKSPACE_ROOT_NAME, ARTIFACTS_ROOT_NAME)


def _repo_root() -> str:
    """Repo root = parent of the ``omniagentos`` package (cwd-independent).

    Same derivation as ``contracts._repo_root`` / ``scope.paths._repo_root``: the
    launch cwd must never decide which ``var/`` tree a task's artifacts land in.
    """
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def var_root() -> str:
    """``<repo>/var``, honoring ``OMNIAGENTOS_VAR_DIR``.

    The same one-line idiom seven other modules already use (adapters/common.py,
    intake/service.py, runner/core.py, provision/service.py, projects/activity.py,
    api/routes/board_files.py, scope/paths.py). Duplicated rather than imported
    because the only existing spellings are module-private, and a public wrapper
    here would be an eighth spelling either way.
    """
    return os.environ.get("OMNIAGENTOS_VAR_DIR") or os.path.join(_repo_root(), "var")


@dataclass(frozen=True)
class ModePolicy:
    """Which roots a :class:`~omniagentos.contracts.TaskMode` gets, and how.

    ``repo_scope`` is the load-bearing field: it is True for ``code`` alone.
    Every other mode is forbidden the repo entirely, which is what
    :mod:`omniagentos.workmodes.artificer` enforces. ``inputs_readonly`` exists
    because ``intake_processing`` is the one mode with a root it may READ but not
    write: the human's uploads. Mutating them destroys the only copy of the input.
    """

    mode: TaskMode
    repo_scope: bool
    artifacts: bool
    workspace: bool
    inputs: bool
    inputs_readonly: bool

    @property
    def writable_kinds(self) -> tuple[str, ...]:
        """Root kinds this mode may WRITE, in :data:`ROOT_KINDS` order."""
        kinds: list[str] = []
        if self.inputs and not self.inputs_readonly:
            kinds.append(INPUTS_ROOT_NAME)
        if self.workspace:
            kinds.append(WORKSPACE_ROOT_NAME)
        if self.artifacts:
            kinds.append(ARTIFACTS_ROOT_NAME)
        return tuple(kinds)

    @property
    def readonly_kinds(self) -> tuple[str, ...]:
        """Root kinds this mode may READ but never write."""
        return (INPUTS_ROOT_NAME,) if (self.inputs and self.inputs_readonly) else ()


#: The whole TN.1 table, as data. Adding a TaskMode without adding a row here is
#: a hard error (:func:`policy_for` raises) rather than a silent default, because
#: the silent default would be "no roots", i.e. a task that cannot write anywhere
#: and fails in a confusing place.
MODE_POLICIES: dict[TaskMode, ModePolicy] = {
    TaskMode.CODE: ModePolicy(
        mode=TaskMode.CODE,
        repo_scope=True,
        artifacts=False,
        workspace=False,
        inputs=False,
        inputs_readonly=False,
    ),
    TaskMode.REPORT: ModePolicy(
        mode=TaskMode.REPORT,
        repo_scope=False,
        artifacts=True,
        workspace=False,
        inputs=False,
        inputs_readonly=False,
    ),
    TaskMode.CONTENT: ModePolicy(
        mode=TaskMode.CONTENT,
        repo_scope=False,
        artifacts=True,
        workspace=False,
        inputs=False,
        inputs_readonly=False,
    ),
    TaskMode.IMAGE: ModePolicy(
        mode=TaskMode.IMAGE,
        repo_scope=False,
        artifacts=True,
        workspace=False,
        inputs=False,
        inputs_readonly=False,
    ),
    TaskMode.VIDEO: ModePolicy(
        mode=TaskMode.VIDEO,
        repo_scope=False,
        artifacts=True,
        workspace=False,
        inputs=False,
        inputs_readonly=False,
    ),
    TaskMode.INTAKE_PROCESSING: ModePolicy(
        mode=TaskMode.INTAKE_PROCESSING,
        repo_scope=False,
        artifacts=True,
        workspace=True,
        inputs=True,
        inputs_readonly=True,
    ),
}


def policy_for(mode: TaskMode | str) -> ModePolicy:
    """The :class:`ModePolicy` for ``mode``; raises for an unknown mode."""
    resolved = coerce_task_mode(mode)
    if resolved is None:
        raise WorkModeError(f"unknown task mode: {mode!r}")
    policy = MODE_POLICIES.get(resolved)
    if policy is None:  # pragma: no cover -- guarded by test_every_mode_has_a_policy
        raise WorkModeError(f"no root policy declared for task mode: {resolved}")
    return policy


def artifact_modes() -> tuple[TaskMode, ...]:
    """Every mode that writes artifact roots instead of the repo, in enum order."""
    return tuple(m for m in TaskMode if not MODE_POLICIES[m].repo_scope)


def coerce_task_mode(value: TaskMode | str | None) -> TaskMode | None:
    """A :class:`TaskMode` from either spelling; ``None`` for absent/unrecognized.

    Never raises: a planner-authored string is untrusted input, and ``None`` (the
    "no declared mode" answer) is what :func:`reconcile_task_mode` already
    handles. Callers that need a hard failure use :func:`policy_for`.
    """
    if value is None:
        return None
    if isinstance(value, TaskMode):
        return value
    text = str(value).strip().lower().replace("-", "_")
    if not text:
        return None
    try:
        return TaskMode(text)
    except ValueError:
        return None


def mode_scope_slug(scope: str) -> str:
    """One filesystem-safe component for a scope id (task id, run id, campaign id).

    Delegates to :func:`omniagentos.scope.paths.safe_component`, which rejects
    every separator and traversal form rather than rewriting it into a
    plausible-looking name — the difference between refusing ``../../etc`` and
    silently creating ``.._.._etc``.
    """
    try:
        return safe_component(scope)
    except ValueError as exc:
        raise WorkModeError(f"unusable artifact scope: {scope!r}") from exc


@dataclass(frozen=True)
class ModeRoots:
    """The resolved (not necessarily existing) roots for one mode+scope.

    A root this mode does not get is ``None``, not a path that happens to be
    unused: a ``None`` cannot be accidentally handed to a worker as a write root,
    whereas an unused-but-present path can.
    """

    mode: TaskMode
    scope: str
    base: str
    artifacts: str | None = None
    workspace: str | None = None
    inputs: str | None = None
    inputs_readonly: bool = True

    @property
    def policy(self) -> ModePolicy:
        return policy_for(self.mode)

    @property
    def write_roots(self) -> tuple[str, ...]:
        """Every root this mode may write, in :data:`ROOT_KINDS` order."""
        by_kind = {
            INPUTS_ROOT_NAME: self.inputs,
            WORKSPACE_ROOT_NAME: self.workspace,
            ARTIFACTS_ROOT_NAME: self.artifacts,
        }
        return tuple(
            path for kind in self.policy.writable_kinds if (path := by_kind.get(kind)) is not None
        )

    @property
    def read_only_roots(self) -> tuple[str, ...]:
        """Every root this mode may read but never write (``inputs`` for intake)."""
        if self.inputs is not None and self.inputs_readonly:
            return (self.inputs,)
        return ()

    @property
    def all_roots(self) -> tuple[str, ...]:
        return tuple(
            path for path in (self.inputs, self.workspace, self.artifacts) if path is not None
        )


def resolve_roots(
    mode: TaskMode | str,
    scope: str,
    *,
    base: str | None = None,
) -> ModeRoots:
    """Resolve ``var/{artifacts,workspace,inputs}/<scope>`` for ``mode``. PURE.

    Creates nothing — resolution and provisioning are separate so that planning,
    prompting and grading can all reason about the layout without a side effect
    on disk. ``base`` defaults to :func:`var_root`.
    """
    policy = policy_for(mode)
    slug = mode_scope_slug(scope)
    root = os.path.abspath(base or var_root())
    return ModeRoots(
        mode=policy.mode,
        scope=slug,
        base=root,
        artifacts=os.path.join(root, ARTIFACTS_ROOT_NAME, slug) if policy.artifacts else None,
        workspace=os.path.join(root, WORKSPACE_ROOT_NAME, slug) if policy.workspace else None,
        inputs=os.path.join(root, INPUTS_ROOT_NAME, slug) if policy.inputs else None,
        inputs_readonly=policy.inputs_readonly,
    )


def provision_roots(roots: ModeRoots) -> tuple[str, ...]:
    """``mkdir -p`` every root in ``roots``; returns the paths that now exist.

    The ONLY filesystem side effect in this module, and it must be called
    explicitly — importing :mod:`omniagentos.workmodes` never creates a
    directory. Idempotent (``exist_ok=True``), so a retried task re-uses the
    workspace it already had rather than failing or starting over.

    ``inputs`` is created too even though it is read-only for
    ``intake_processing``: the uploader needs a place to put the files, and a
    missing directory is indistinguishable from an empty one to a worker that is
    told to read it.
    """
    created: list[str] = []
    for path in roots.all_roots:
        os.makedirs(path, exist_ok=True)
        created.append(path)
    return tuple(created)


def target_location_for(goal: str | None) -> str | None:
    """Where the human explicitly said to write, or ``None``.

    Thin delegation to :func:`omniagentos.intake.fastlane.parse_target_location`,
    which already refuses everything outside ``$HOME`` and every secret/system
    dir, so "on my desktop" resolves and "in ~/.ssh" does not. Imported lazily:
    ``omniagentos.intake`` pulls in the whole intake service on package import,
    and this package must stay cheap enough to import from a path helper.

    NOTE: a parsed location is a CANDIDATE, not a grant. It still has to survive
    :func:`omniagentos.workmodes.artificer.guard_writes`, which rejects a
    destination inside the repo for every non-code mode — fastlane's own filter
    permits repo paths, because the repo lives under ``$HOME``.
    """
    if not goal:
        return None
    from omniagentos.intake.fastlane import parse_target_location

    return parse_target_location(goal)


# ---------------------------------------------------------------------------
# Deterministic mode inference (regex + negation)
# ---------------------------------------------------------------------------

#: Words/phrases that KILL a following signal. A signal inside this window is
#: recorded as suppressed rather than scored.
_NEGATORS: frozenset[str] = frozenset(
    {
        "not",
        "no",
        "never",
        "without",
        "isn't",
        "isnt",
        "aren't",
        "arent",
        "don't",
        "dont",
        "doesn't",
        "doesnt",
        "avoid",
        "skip",
        "exclude",
        "excluding",
        "omit",
        "except",
    }
)

#: Two-word negators, checked as adjacent pairs ("instead of a report").
_NEGATOR_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("instead", "of"),
        ("rather", "than"),
        ("other", "than"),
        ("as", "opposed"),
        ("do", "not"),
        ("does", "not"),
        ("no", "need"),
        ("not", "just"),
    }
)

#: How many preceding words can carry a negation. Four covers "instead of a long
#: report" and "not just a quick summary" without reaching across a clause.
_NEGATION_WINDOW = 4

#: Word tokenizer for the negation window. Matched case-INSENSITIVELY over the
#: original text rather than over ``text.lower()``: lowercasing is not
#: length-preserving for every Unicode character (``İ`` lowercases to two code
#: points), and a shifted offset would silently move the negation window onto the
#: wrong words.
_WORD_RE = re.compile(r"[A-Za-z0-9'’$%_-]+")

#: Weight 2 = a signal that is unambiguous on its own; weight 1 = a signal that
#: only means something in company. A mode needs total weight >= 2 to be
#: CONFIDENT, so one weak hit never overrides a planner's label.
_STRONG = 2
_WEAK = 1

_SIGNALS: tuple[tuple[TaskMode, str, int], ...] = (
    # --- image -------------------------------------------------------------
    (TaskMode.IMAGE, r"\bimages?\b", _STRONG),
    (TaskMode.IMAGE, r"\blogos?\b", _STRONG),
    (TaskMode.IMAGE, r"\bthumbnails?\b", _STRONG),
    (TaskMode.IMAGE, r"\bposters?\b", _STRONG),
    (TaskMode.IMAGE, r"\billustrations?\b", _STRONG),
    (TaskMode.IMAGE, r"\bmock-?ups?\b", _STRONG),
    (TaskMode.IMAGE, r"\bfavicons?\b", _STRONG),
    (TaskMode.IMAGE, r"\bhero (image|shot)\b", _STRONG),
    (TaskMode.IMAGE, r"\bad creative\b", _WEAK),
    (TaskMode.IMAGE, r"\bbanners?\b", _WEAK),
    (TaskMode.IMAGE, r"\bgraphics?\b", _WEAK),
    (TaskMode.IMAGE, r"\bartwork\b", _WEAK),
    (TaskMode.IMAGE, r"\bphotos?\b", _WEAK),
    (TaskMode.IMAGE, r"\bpictures?\b", _WEAK),
    (TaskMode.IMAGE, r"\.png\b", _WEAK),
    (TaskMode.IMAGE, r"\.jpe?g\b", _WEAK),
    # --- video -------------------------------------------------------------
    (TaskMode.VIDEO, r"\bvideos?\b", _STRONG),
    (TaskMode.VIDEO, r"\breels?\b", _STRONG),
    (TaskMode.VIDEO, r"\banimations?\b", _STRONG),
    (TaskMode.VIDEO, r"\bscreencasts?\b", _STRONG),
    (TaskMode.VIDEO, r"\btrailers?\b", _STRONG),
    (TaskMode.VIDEO, r"\bb-roll\b", _STRONG),
    (TaskMode.VIDEO, r"\.mp4\b", _STRONG),
    (TaskMode.VIDEO, r"\byoutube shorts?\b", _STRONG),
    (TaskMode.VIDEO, r"\bfootage\b", _WEAK),
    (TaskMode.VIDEO, r"\bstoryboards?\b", _WEAK),
    (TaskMode.VIDEO, r"\bvoice-?overs?\b", _WEAK),
    # --- report ------------------------------------------------------------
    (TaskMode.REPORT, r"\breports?\b", _STRONG),
    (TaskMode.REPORT, r"\bwhite ?papers?\b", _STRONG),
    (TaskMode.REPORT, r"\b(market|competitive|swot) analysis\b", _STRONG),
    (TaskMode.REPORT, r"\bmemos?\b", _STRONG),
    (TaskMode.REPORT, r"\bbriefing\b", _WEAK),
    (TaskMode.REPORT, r"\banalysis\b", _WEAK),
    (TaskMode.REPORT, r"\bfindings\b", _WEAK),
    (TaskMode.REPORT, r"\bslide deck\b", _WEAK),
    (TaskMode.REPORT, r"\bexecutive summary\b", _STRONG),
    # --- content -----------------------------------------------------------
    (TaskMode.CONTENT, r"\bad cop(y|ies)\b", _STRONG),
    (TaskMode.CONTENT, r"\bcopy-?writing\b", _STRONG),
    (TaskMode.CONTENT, r"\b(marketing|website|landing page|sales) copy\b", _STRONG),
    (TaskMode.CONTENT, r"\bblog post\b", _STRONG),
    (TaskMode.CONTENT, r"\bnewsletters?\b", _STRONG),
    (TaskMode.CONTENT, r"\bemail (sequence|sequences|blast)\b", _STRONG),
    (TaskMode.CONTENT, r"\blanding pages?\b", _STRONG),
    (TaskMode.CONTENT, r"\bsales pages?\b", _STRONG),
    (TaskMode.CONTENT, r"\b(webinar|video|ad|sales) scripts?\b", _STRONG),
    (TaskMode.CONTENT, r"\bheadlines?\b", _STRONG),
    (TaskMode.CONTENT, r"\bsocial (media )?posts?\b", _STRONG),
    (TaskMode.CONTENT, r"\bcaptions?\b", _WEAK),
    (TaskMode.CONTENT, r"\btweets?\b", _WEAK),
    (TaskMode.CONTENT, r"\barticles?\b", _WEAK),
    (TaskMode.CONTENT, r"\bsubject lines?\b", _WEAK),
    (TaskMode.CONTENT, r"\bseo\b", _WEAK),
    # --- code --------------------------------------------------------------
    (TaskMode.CODE, r"\brefactor(ing)?\b", _STRONG),
    (TaskMode.CODE, r"\bunit tests?\b", _STRONG),
    (TaskMode.CODE, r"\bbug ?fix(es)?\b", _STRONG),
    (TaskMode.CODE, r"\bpull requests?\b", _STRONG),
    (TaskMode.CODE, r"\bmigrations?\b", _STRONG),
    (TaskMode.CODE, r"\bcodebase\b", _STRONG),
    (TaskMode.CODE, r"\brepos(itory|itories)?\b", _STRONG),
    (TaskMode.CODE, r"\bendpoints?\b", _STRONG),
    (TaskMode.CODE, r"\bstack ?traces?\b", _STRONG),
    (TaskMode.CODE, r"\btest suite\b", _STRONG),
    (TaskMode.CODE, r"\bfunctions?\b", _WEAK),
    (TaskMode.CODE, r"\bclass(es)?\b", _WEAK),
    (TaskMode.CODE, r"\bmodules?\b", _WEAK),
    (TaskMode.CODE, r"\bapi\b", _WEAK),
    (TaskMode.CODE, r"\bpatch\b", _WEAK),
    (TaskMode.CODE, r"\bimplement\b", _WEAK),
    (TaskMode.CODE, r"\bcompile\b", _WEAK),
    (TaskMode.CODE, r"\b(python|typescript|javascript|rust|golang)\b", _WEAK),
    # --- intake_processing --------------------------------------------------
    (TaskMode.INTAKE_PROCESSING, r"\battach(ed|ment|ments)\b", _STRONG),
    (TaskMode.INTAKE_PROCESSING, r"\bupload(s|ed)?\b", _STRONG),
    (TaskMode.INTAKE_PROCESSING, r"\bthese files?\b", _STRONG),
    (TaskMode.INTAKE_PROCESSING, r"\bthis (pdf|csv|spreadsheet|document|file)\b", _STRONG),
    (TaskMode.INTAKE_PROCESSING, r"\btranscribe\b", _STRONG),
    (TaskMode.INTAKE_PROCESSING, r"\bocr\b", _STRONG),
    (TaskMode.INTAKE_PROCESSING, r"\binvoices?\b", _STRONG),
    (TaskMode.INTAKE_PROCESSING, r"\breceipts?\b", _STRONG),
    (TaskMode.INTAKE_PROCESSING, r"\bspreadsheets?\b", _WEAK),
    (TaskMode.INTAKE_PROCESSING, r"\bcsvs?\b", _WEAK),
    (TaskMode.INTAKE_PROCESSING, r"\bpdfs?\b", _WEAK),
    (TaskMode.INTAKE_PROCESSING, r"\bextract\b", _WEAK),
)

_COMPILED_SIGNALS: tuple[tuple[TaskMode, re.Pattern[str], int], ...] = tuple(
    (mode, re.compile(pattern, re.IGNORECASE), weight) for mode, pattern, weight in _SIGNALS
)

#: Deterministic tie-break, most-constrained first. ``intake_processing`` leads
#: because its root set is a SUPERSET of an artifact mode's (inputs read-only +
#: workspace + artifacts): when the text says both "the attached PDF" and
#: "a report", provisioning the intake shape loses nothing, while provisioning
#: the report shape loses the inputs the task needs. ``code`` is last because it
#: is the only mode that grants repo writes, and a tie must never be resolved in
#: favor of more access.
_TIE_ORDER: tuple[TaskMode, ...] = (
    TaskMode.INTAKE_PROCESSING,
    TaskMode.VIDEO,
    TaskMode.IMAGE,
    TaskMode.REPORT,
    TaskMode.CONTENT,
    TaskMode.CODE,
)

#: Weight at which an inference is allowed to overrule a planner's ``code`` label.
CONFIDENT_SCORE = 2


def _word_spans(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, lowercased_word)`` for every word, at ORIGINAL offsets."""
    return [(m.start(), m.end(), m.group(0).lower()) for m in _WORD_RE.finditer(text)]


def is_negated(
    text: str, start: int, *, words: Sequence[tuple[int, int, str]] | None = None
) -> bool:
    """True when the token at character offset ``start`` sits under a negation.

    Looks at the up-to-:data:`_NEGATION_WINDOW` words immediately BEFORE
    ``start`` for a single-word negator or an adjacent negator pair. Pre-window
    only, deliberately: trailing negation ("a report is not needed") is rarer
    than a following clause that would be swallowed by a lookahead ("write a
    report; do not touch the repo" must keep ``report`` scored).

    Exported because :mod:`omniagentos.workmodes.protocols` needs exactly this
    for banned-claim detection — "results are not guaranteed" is a DISCLAIMER,
    and flagging it as a banned claim is the false positive that gets a
    compliance lexicon switched off.
    """
    spans = list(words) if words is not None else _word_spans(text)
    before = [w for (_s, e, w) in spans if e <= start][-_NEGATION_WINDOW:]
    if any(word in _NEGATORS for word in before):
        return True
    return any(pair in _NEGATOR_PAIRS for pair in zip(before, before[1:], strict=False))


@dataclass(frozen=True)
class ModeInference:
    """What the regex classifier saw. Never a decision on its own.

    ``mode`` is ``None`` when nothing matched, which is a real and common answer
    ("finish the thing we discussed") and must not be papered over with a
    default. ``suppressed`` records the signals a negation killed, so a surprising
    classification is explainable after the fact.
    """

    mode: TaskMode | None
    score: int
    scores: dict[TaskMode, int]
    matched: tuple[str, ...]
    suppressed: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def confident(self) -> bool:
        """True when the winning mode carries enough weight to overrule a label."""
        return self.mode is not None and self.score >= CONFIDENT_SCORE


def infer_task_mode(text: str | None) -> ModeInference:
    """Classify a request's task mode from its text. Deterministic; never raises.

    Each distinct signal scores ONCE regardless of how often it repeats, so a
    request that says "image" eight times does not outvote one that says
    "video" once plus "reel" once. Ties break through :data:`_TIE_ORDER`.
    """
    raw = (text or "").strip()
    if not raw:
        return ModeInference(None, 0, {}, (), (), ("empty text",))
    spans = _word_spans(raw)
    scores: dict[TaskMode, int] = {}
    matched: list[str] = []
    suppressed: list[str] = []
    for mode, pattern, weight in _COMPILED_SIGNALS:
        hit_positions = [m.start() for m in pattern.finditer(raw)]
        if not hit_positions:
            continue
        live = [pos for pos in hit_positions if not is_negated(raw, pos, words=spans)]
        label = f"{mode.value}:{pattern.pattern}"
        if not live:
            suppressed.append(label)
            continue
        scores[mode] = scores.get(mode, 0) + weight
        matched.append(label)
    if not scores:
        reason = "no mode signals" if not suppressed else "every mode signal was negated"
        return ModeInference(None, 0, {}, (), tuple(suppressed), (reason,))
    best = max(scores.values())
    winners = [mode for mode in _TIE_ORDER if scores.get(mode) == best]
    winner = winners[0]
    reasons = [f"{winner.value} scored {best}"]
    if len(winners) > 1:
        reasons.append("tie broken by precedence over " + ", ".join(m.value for m in winners[1:]))
    if suppressed:
        reasons.append(f"{len(suppressed)} signal(s) suppressed by negation")
    return ModeInference(
        mode=winner,
        score=best,
        scores=dict(sorted(scores.items(), key=lambda kv: kv[0].value)),
        matched=tuple(matched),
        suppressed=tuple(suppressed),
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class ModeDecision:
    """The joined answer: what mode to run, where it came from, and whether the
    planner's label and the text agreed.

    ``conflict=True`` is not an error — it is the signal worth logging, because a
    planner that labels copywriting as ``code`` is the exact failure TN.2 exists
    to stop, and it will keep happening until someone looks at these rows.
    """

    mode: TaskMode
    source: Literal["declared", "inferred", "fallback"]
    declared: TaskMode | None
    inference: ModeInference
    conflict: bool
    reasons: tuple[str, ...]


def reconcile_task_mode(declared: TaskMode | str | None, text: str | None) -> ModeDecision:
    """Join a planner's label with the text classifier. A label is never trusted alone.

    The resolution rule, in one sentence: **the more restrictive of the two
    wins, and among equally restrictive modes the declared label wins.**

    * declared missing/garbage -> the inference; nothing inferred either ->
      ``code``, which is the status quo (a repo-scoped worker under a declared
      file scope) and therefore the choice that changes no behaviour.
    * declared ``code`` + a CONFIDENT non-code inference -> the inference. This
      is the important direction: it takes repo write access AWAY from a task
      whose text is plainly about ad copy or a thumbnail. An unconfident
      inference (a single weak signal, e.g. one stray "api") never does this.
    * declared non-code + a ``code`` inference -> the declared non-code mode.
      Refusing to widen scope on the strength of a keyword scan is the whole
      point; "migrate the copy to the new landing page" says ``migration`` and
      means ad copy.
    * two different non-code modes -> declared, since both are sandboxed
      identically and the planner is the one that knows the deliverable format.
    """
    label = coerce_task_mode(declared)
    inference = infer_task_mode(text)
    reasons: list[str] = list(inference.reasons)

    if label is None:
        if inference.mode is not None:
            reasons.append("no declared mode; using inference")
            return ModeDecision(inference.mode, "inferred", None, inference, False, tuple(reasons))
        reasons.append("no declared mode and no signals; falling back to code")
        return ModeDecision(TaskMode.CODE, "fallback", None, inference, False, tuple(reasons))

    inferred = inference.mode
    if inferred is None or inferred == label:
        return ModeDecision(label, "declared", label, inference, False, tuple(reasons))

    declared_is_code = policy_for(label).repo_scope
    inferred_is_code = policy_for(inferred).repo_scope

    if declared_is_code and not inferred_is_code:
        if inference.confident:
            reasons.append(
                f"declared {label.value} but the text confidently reads as "
                f"{inferred.value}; taking the narrower mode"
            )
            return ModeDecision(inferred, "inferred", label, inference, True, tuple(reasons))
        reasons.append(
            f"declared {label.value}; {inferred.value} signals present but not confident"
        )
        return ModeDecision(label, "declared", label, inference, True, tuple(reasons))

    if not declared_is_code and inferred_is_code:
        reasons.append(
            f"declared {label.value} beats a {inferred.value} inference: a keyword scan "
            "never widens scope to the repo"
        )
        return ModeDecision(label, "declared", label, inference, True, tuple(reasons))

    reasons.append(
        f"declared {label.value} and inferred {inferred.value} are equally sandboxed; "
        "keeping the declared deliverable type"
    )
    return ModeDecision(label, "declared", label, inference, True, tuple(reasons))


# ---------------------------------------------------------------------------
# Task splitting
# ---------------------------------------------------------------------------

SPLIT_CODE_SUFFIX = "::code"
SPLIT_ARTIFACT_SUFFIX = "::artifacts"

#: Which artifact mode a mixed unit's non-code half gets when neither the unit's
#: own mode nor its text says. ``report`` and not ``content``: a mixed unit is
#: almost always "change X and write up what changed".
DEFAULT_ARTIFACT_MODE = TaskMode.REPORT


@dataclass(frozen=True)
class WorkUnit:
    """One schedulable unit, as far as mode/scope splitting is concerned.

    Deliberately NOT a lane's task model. Every lane has its own (board_tasks,
    swarm plan tasks, longhaul units) and this has to work for all of them, so
    ``payload`` carries whatever the caller needs to round-trip and this module
    touches only the four fields it understands.
    """

    key: str
    mode: TaskMode = TaskMode.CODE
    repo_paths: tuple[str, ...] = ()
    artifact_outputs: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    title: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_mixed(self) -> bool:
        """True when this unit declares repo files AND artifact outputs.

        The one condition that CANNOT be honored by a single unit: a repo-scoped
        worker is forbidden the artifact roots and an artificer is forbidden the
        repo, so one process cannot satisfy both halves.
        """
        return bool(self.repo_paths) and bool(self.artifact_outputs)


@dataclass(frozen=True)
class SplitResult:
    """The rewired unit list plus the mapping from each original key to its halves.

    ``replaced`` is ordered ``(code_key, artifact_key)`` and is what a caller
    needs to fix up anything OUTSIDE the unit list that referenced the old key
    (a board card, a lock claim, an approval row).
    """

    units: tuple[WorkUnit, ...]
    replaced: dict[str, tuple[str, str]]

    @property
    def changed(self) -> bool:
        return bool(self.replaced)


def split_mixed_unit(
    unit: WorkUnit, *, artifact_mode: TaskMode | None = None
) -> tuple[WorkUnit, WorkUnit]:
    """Split one mixed unit into ``(code_half, artifact_half)``.

    The artifact half depends on the code half, never the reverse: the write-up
    describes the change, so it has to run after it. The code half inherits the
    original's dependencies verbatim.

    The artifact half's mode is, in order: the explicit ``artifact_mode``
    argument, the unit's own mode when that is already non-code, whatever the
    title infers, then :data:`DEFAULT_ARTIFACT_MODE`.
    """
    if not unit.is_mixed:
        raise WorkModeError(f"unit {unit.key!r} is not mixed; nothing to split")
    chosen = coerce_task_mode(artifact_mode)
    if chosen is None and not policy_for(unit.mode).repo_scope:
        chosen = unit.mode
    if chosen is None:
        inferred = infer_task_mode(unit.title).mode
        if inferred is not None and not policy_for(inferred).repo_scope:
            chosen = inferred
    if chosen is None:
        chosen = DEFAULT_ARTIFACT_MODE
    if policy_for(chosen).repo_scope:
        raise WorkModeError(f"artifact half of {unit.key!r} cannot be mode {chosen.value}")

    code_key = f"{unit.key}{SPLIT_CODE_SUFFIX}"
    artifact_key = f"{unit.key}{SPLIT_ARTIFACT_SUFFIX}"
    code_half = WorkUnit(
        key=code_key,
        mode=TaskMode.CODE,
        repo_paths=unit.repo_paths,
        artifact_outputs=(),
        depends_on=unit.depends_on,
        title=unit.title,
        payload=dict(unit.payload),
    )
    artifact_half = WorkUnit(
        key=artifact_key,
        mode=chosen,
        repo_paths=(),
        artifact_outputs=unit.artifact_outputs,
        depends_on=(code_key,),
        title=unit.title,
        payload=dict(unit.payload),
    )
    return code_half, artifact_half


def split_units(units: Iterable[WorkUnit]) -> SplitResult:
    """Split every mixed unit and rewire the whole dependency graph.

    IDENTITY when nothing is mixed — the same units, in the same order, with the
    same dependency tuples — which is what makes this safe to run over an
    existing code-only plan.

    Rewiring rule for a dependent: a unit that depended on a split key ``K``
    now depends on BOTH halves, not just the terminal one. Depending on the
    artifact half alone would be equivalent only if the scheduler computed the
    transitive closure, and "correct only if someone else is transitive" is the
    kind of assumption that fails silently the day a scheduler is rewritten.
    """
    original = list(units)
    keys = [unit.key for unit in original]
    if len(set(keys)) != len(keys):
        dupes = sorted({key for key in keys if keys.count(key) > 1})
        raise WorkModeError(f"duplicate unit keys: {dupes}")

    mixed = [unit for unit in original if unit.is_mixed]
    if not mixed:
        return SplitResult(tuple(original), {})

    existing = set(keys)
    replaced: dict[str, tuple[str, str]] = {}
    for unit in mixed:
        halves = (f"{unit.key}{SPLIT_CODE_SUFFIX}", f"{unit.key}{SPLIT_ARTIFACT_SUFFIX}")
        clash = [half for half in halves if half in existing]
        if clash:
            raise WorkModeError(f"split of {unit.key!r} would collide with existing keys: {clash}")
        replaced[unit.key] = halves

    def _remap(deps: Sequence[str], *, owner: str) -> tuple[str, ...]:
        out: list[str] = []
        for dep in deps:
            for candidate in replaced.get(dep, (dep,)):
                if candidate != owner and candidate not in out:
                    out.append(candidate)
        return tuple(out)

    rebuilt: list[WorkUnit] = []
    for unit in original:
        if unit.key in replaced:
            code_half, artifact_half = split_mixed_unit(unit)
            code_deps = _remap(unit.depends_on, owner=code_half.key)
            rebuilt.append(
                WorkUnit(
                    key=code_half.key,
                    mode=code_half.mode,
                    repo_paths=code_half.repo_paths,
                    artifact_outputs=(),
                    depends_on=code_deps,
                    title=code_half.title,
                    payload=code_half.payload,
                )
            )
            # Remapped a SECOND time with the artifact half as the owner: a unit
            # that (degenerately) depended on itself would otherwise have the
            # artifact half depend on the artifact half, since the first remap
            # only filtered out the code half's own key.
            artifact_deps = tuple(
                dep
                for dep in _remap(unit.depends_on, owner=artifact_half.key)
                if dep != code_half.key
            )
            rebuilt.append(
                WorkUnit(
                    key=artifact_half.key,
                    mode=artifact_half.mode,
                    repo_paths=(),
                    artifact_outputs=artifact_half.artifact_outputs,
                    depends_on=(code_half.key, *artifact_deps),
                    title=artifact_half.title,
                    payload=artifact_half.payload,
                )
            )
            continue
        rebuilt.append(
            WorkUnit(
                key=unit.key,
                mode=unit.mode,
                repo_paths=unit.repo_paths,
                artifact_outputs=unit.artifact_outputs,
                depends_on=_remap(unit.depends_on, owner=unit.key),
                title=unit.title,
                payload=unit.payload,
            )
        )
    return SplitResult(tuple(rebuilt), replaced)


def validate_artifact_outputs(outputs: Iterable[str]) -> tuple[str, ...]:
    """Normalize declared artifact outputs to root-relative posix text.

    Every path goes through :func:`omniagentos.scope.paths.normalize_rel`, so an
    absolute path, a ``~`` path, a drive letter, an embedded NUL and a ``..``
    escape are all rejected HERE — at declaration time, where the error names the
    plan — rather than at copy time, where it names a file nobody chose.
    """
    normalized: list[str] = []
    for raw in outputs:
        try:
            parts = normalize_rel(raw)
        except ScopePathError as exc:
            raise WorkModeError(f"artifact output is not root-relative: {raw!r}") from exc
        if not parts:
            raise WorkModeError(f"artifact output cannot be the whole root: {raw!r}")
        text = "/".join(parts)
        if text not in normalized:
            normalized.append(text)
    return tuple(normalized)
