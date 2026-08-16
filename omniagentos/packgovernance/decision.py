"""Read an operator decision into a closed, machine-checkable authority record.

An operator decision is the ONLY source of permission in this subsystem, so the
reader is deliberately literal:

* every directive sentence is matched against a closed pattern set;
* a sentence that matches nothing is recorded in ``unclassified_directives``
  rather than dropped — unread authority later costs an item its fast tier;
* two incompatible stances on one item produce an :class:`AuthorityConflict`,
  and the most restrictive stance wins for anything that still proceeds;
* prohibitions are translated into :class:`SideEffect` values at read time, so
  the authorizer never has to re-interpret prose.

"Approve only X, Y, Z" additionally sets ``approval_is_exclusive``: silence
about an item is a refusal, not an omission.
"""

from __future__ import annotations

import re
from collections import defaultdict

from omniagentos.packgovernance.checksum import matching_view
from omniagentos.packgovernance.contracts import (
    ArtifactKind,
    AuthorityConflict,
    BoundArtifact,
    ConstraintKind,
    DecisionConstraint,
    Disposition,
    DispositionKind,
    OperatorDecision,
    SideEffect,
    UntrustedText,
)
from omniagentos.packgovernance.errors import AuthorityError

__all__ = [
    "DIRECTIVE_SECTIONS",
    "PROHIBITION_SIDE_EFFECTS",
    "parse_operator_decision",
]

MAX_DECISION_LINES = 5_000

# Only sentences inside these sections may be "unclassified"; everything else in
# the document is context, and treating context as unread authority would make
# the signal meaningless.
DIRECTIVE_SECTIONS: tuple[str, ...] = ("decision:", "required engine behavior:")

_ITEM_ID = re.compile(r"(?<![A-Za-z0-9])(?P<id>[A-B]\d{1,3})(?![A-Za-z0-9])")
_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+(?P<body>.*\S)[ \t]*$")
_NUMBERED = re.compile(r"^[ \t]*(?P<number>\d{1,2})[.)][ \t]+(?P<body>.*\S)[ \t]*$")
_TARGET_REPOSITORY = re.compile(
    r"target repository[^:]*:\s*`(?P<repository>[^`]+)`", re.IGNORECASE
)
_PROTECTED_SCOPE = re.compile(
    r"do not (?:modify|edit|touch|operate)\s*`(?P<scope>[^`]+)`", re.IGNORECASE
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])[\"'”’]?\s+(?=[A-Z“\"(])")
# Clause boundaries inside one directive sentence. Classifying a whole sentence
# first-verb-wins let "Approve A1, but reject A2." approve BOTH items and let
# "Approve A1, but A1 must not push." drop the prohibition — a fail-open.
# Contrast connectives ("but", "however") and semicolons always split;
# "and"/"or" split only when a new disposition verb follows, so a plain
# "Approve A1 and A2" stays one clause.
_CLAUSE_SPLIT = re.compile(
    r";\s+|,?\s+(?:but|however),?\s+|,?\s+(?:and|or)\s+(?=approve|reject|defer)",
    re.IGNORECASE,
)
_PARTIAL_SCOPE = re.compile(
    r"(half|portion|part) of\s*$|only the\s*$|smoke-only (?:half|part) of\s*$", re.IGNORECASE
)

# Prohibition verbs mapped onto the side effects they forbid. Matching is done
# on the normalised sentence, so a homoglyph "ｐush" cannot slip through.
PROHIBITION_SIDE_EFFECTS: tuple[tuple[str, SideEffect], ...] = (
    ("push", SideEffect.REPO_BRANCH_PUSH),
    ("merge", SideEffect.REPO_PR_MERGE),
    ("deploy", SideEffect.DEPLOYMENT),
    ("deployment", SideEffect.DEPLOYMENT),
    ("external issue", SideEffect.REPO_ISSUE_WRITE),
    ("external issues", SideEffect.REPO_ISSUE_WRITE),
    ("issues/prs", SideEffect.REPO_ISSUE_WRITE),
    ("external pr", SideEffect.REPO_PR_OPEN),
    ("prs", SideEffect.REPO_PR_OPEN),
    ("pull request", SideEffect.REPO_PR_OPEN),
    ("repository settings", SideEffect.REPO_SETTINGS_CHANGE),
    ("settings", SideEffect.REPO_SETTINGS_CHANGE),
    ("credential", SideEffect.CREDENTIAL_USE),
    ("credentials", SideEffect.CREDENTIAL_USE),
    ("mutate product code", SideEffect.PRODUCT_CODE_MUTATION),
    ("product code", SideEffect.PRODUCT_CODE_MUTATION),
    ("scheduled automation", SideEffect.SCHEDULE_ACTIVATION),
    ("schedule activation", SideEffect.SCHEDULE_ACTIVATION),
    ("live schedule", SideEffect.SCHEDULE_ACTIVATION),
    ("send email", SideEffect.CUSTOMER_COMMUNICATION),
    ("refund", SideEffect.MONEY_MOVEMENT),
)


def _sentences(text: str) -> list[str]:
    flattened = " ".join(text.split())
    if not flattened:
        return []
    return [part.strip() for part in _SENTENCE_SPLIT.split(flattened) if part.strip()]


def _ids_with_context(sentence: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in _ITEM_ID.finditer(sentence):
        preceding = sentence[max(0, match.start() - 48) : match.start()]
        found.append((match.group("id"), preceding))
    return found


def _forbidden_effects(sentence: str) -> frozenset[SideEffect]:
    haystack = matching_view(sentence).casefold()
    effects = {effect for token, effect in PROHIBITION_SIDE_EFFECTS if token in haystack}
    return frozenset(effects)


def _clauses(sentence: str) -> list[str]:
    normalized = matching_view(sentence)
    return [part.strip() for part in _CLAUSE_SPLIT.split(normalized) if part.strip()]


def _classify(
    sentence: str, line: int
) -> tuple[list[Disposition], list[DecisionConstraint], list[str]]:
    """Classify one directive sentence, clause by clause.

    A sentence may carry several stances on several items. Every clause is
    classified independently and ALL dispositions and constraints accumulate —
    never first-verb-wins — so a rejection or prohibition phrased inside an
    approval sentence is retained, and ``disposition_for`` later resolves
    most-restrictive-wins. Clauses that match nothing are returned so unread
    authority stays visible.
    """
    clauses = _clauses(sentence)
    if not clauses:
        # Nothing survives normalisation (e.g. all-invisible text): still report
        # the sentence as unread authority rather than dropping it.
        return [], [], [sentence]
    dispositions: list[Disposition] = []
    constraints: list[DecisionConstraint] = []
    unrecognized: list[str] = []
    for clause in clauses:
        clause_dispositions, clause_constraints, recognised = _classify_clause(
            clause, sentence, line
        )
        dispositions.extend(clause_dispositions)
        constraints.extend(clause_constraints)
        if not recognised:
            unrecognized.append(clause)
    return dispositions, constraints, unrecognized


def _classify_clause(
    clause: str, sentence: str, line: int
) -> tuple[list[Disposition], list[DecisionConstraint], bool]:
    """Classify one clause. ``sentence`` is kept whole as provenance/evidence;
    matching runs on the clause alone so one clause's verbs cannot leak into
    another's forbidden-side-effect or disposition set."""
    normalized = matching_view(clause)
    lowered = normalized.casefold()
    ids = _ids_with_context(normalized)
    evidence = UntrustedText(raw=sentence, field_name="operator_decision", line=line)

    def _dispositions(
        kind: DispositionKind,
        *,
        scope_note: str | None = None,
        conditions: tuple[str, ...] = (),
        blockers: tuple[str, ...] = (),
    ) -> list[Disposition]:
        return [
            Disposition(
                item_id=item_id,
                kind=kind,
                evidence=evidence,
                scope_note=scope_note,
                conditions=conditions,
                blockers=blockers,
                partial_scope=bool(_PARTIAL_SCOPE.search(preceding)),
            )
            for item_id, preceding in ids
        ]

    # --- dispositions -----------------------------------------------------
    if lowered.startswith("reject") and ids:
        return _dispositions(DispositionKind.REJECTED), [], True

    if lowered.startswith("defer") and ids:
        return _dispositions(DispositionKind.DEFERRED), [], True

    blocked = re.search(r"\b(?:are|is) blocked on (?P<blockers>.+?)\.?$", lowered)
    if blocked is not None and ids:
        raw_blockers = tuple(
            token.strip()
            for token in re.split(r",| and ", blocked.group("blockers"))
            if token.strip()
        )
        return _dispositions(DispositionKind.BLOCKED, blockers=raw_blockers), [], True

    candidate = re.search(r"\bcandidates? only after (?P<condition>.+?)\.?$", lowered)
    if candidate is not None and ids:
        return (
            _dispositions(
                DispositionKind.CANDIDATE, conditions=(candidate.group("condition").strip(),)
            ),
            [],
            True,
        )

    if lowered.startswith("approve") and ids:
        scope = re.search(r"\bfor (?P<scope>an? .+?)\.?$", lowered)
        # Exclusivity is carried on the decision record itself (see the caller),
        # not as a constraint: it is a property of the whole approval set.
        return (
            _dispositions(
                DispositionKind.APPROVED,
                scope_note=scope.group("scope").strip() if scope else None,
            ),
            [],
            True,
        )

    # --- constraints ------------------------------------------------------
    if lowered.startswith("exclude"):
        return (
            [],
            [
                DecisionConstraint(
                    kind=ConstraintKind.SCOPE_EXCLUSION,
                    text=sentence,
                    line=line,
                    item_ids=tuple(item_id for item_id, _ in ids),
                )
            ],
            True,
        )

    if " must not " in f" {lowered} " and ids:
        return (
            [],
            [
                DecisionConstraint(
                    kind=ConstraintKind.ITEM_PROHIBITION,
                    text=sentence,
                    line=line,
                    item_ids=tuple(item_id for item_id, _ in ids),
                    forbidden_side_effects=_forbidden_effects(clause),
                )
            ],
            True,
        )

    if lowered.startswith("never ") or lowered.startswith("do not "):
        return (
            [],
            [
                DecisionConstraint(
                    kind=ConstraintKind.GLOBAL_PROHIBITION,
                    text=sentence,
                    line=line,
                    scopes=tuple(
                        match.group("scope") for match in _PROTECTED_SCOPE.finditer(normalized)
                    ),
                    forbidden_side_effects=_forbidden_effects(clause),
                )
            ],
            True,
        )

    if "derived and gated" in lowered or "never inferred from prose" in lowered:
        return (
            [],
            [DecisionConstraint(ConstraintKind.DERIVATION_RULE, sentence, line)],
            True,
        )

    if "unproven until exercised" in lowered or "independently reproduced" in lowered:
        return (
            [],
            [DecisionConstraint(ConstraintKind.EVIDENCE_TRUST_RULE, sentence, line)],
            True,
        )

    if lowered.startswith("surface") and "contradiction" in lowered:
        return (
            [],
            [DecisionConstraint(ConstraintKind.CONTRADICTION_SURFACING, sentence, line)],
            True,
        )

    if "fast governance tier" in lowered or "full adversarial pipeline" in lowered:
        return (
            [],
            [DecisionConstraint(ConstraintKind.FAST_TIER_RULE, sentence, line)],
            True,
        )

    return [], [], False


def parse_operator_decision(artifact: BoundArtifact) -> OperatorDecision:
    """Turn a bound operator-decision artifact into an authority record."""
    if artifact.kind is not ArtifactKind.OPERATOR_AUTHORITY:
        raise AuthorityError(
            f"{artifact.path}: parse_operator_decision requires an operator-authority "
            f"artifact, got {artifact.kind.value}"
        )

    lines = artifact.text.split("\n")
    if len(lines) > MAX_DECISION_LINES:
        raise AuthorityError(
            f"{artifact.path}: {len(lines)} lines exceeds the {MAX_DECISION_LINES} ceiling"
        )

    target_repository: str | None = None
    dispositions: list[Disposition] = []
    constraints: list[DecisionConstraint] = []
    unclassified: list[str] = []
    approval_is_exclusive = False

    in_directive_section = False
    pending: list[str] = []
    pending_line = 0

    def _drain() -> None:
        nonlocal approval_is_exclusive
        if not pending:
            return
        body = " ".join(pending)
        for sentence in _sentences(body):
            found_dispositions, found_constraints, unrecognized = _classify(sentence, pending_line)
            if matching_view(sentence).casefold().startswith("approve only"):
                approval_is_exclusive = True
            dispositions.extend(found_dispositions)
            constraints.extend(found_constraints)
            if found_dispositions or found_constraints:
                # Partially recognised: surface only the clauses that were not.
                unclassified.extend(unrecognized)
            elif unrecognized:
                unclassified.append(sentence)
        pending.clear()

    for index, raw_line in enumerate(lines):
        line_no = index + 1
        line = raw_line.rstrip()
        stripped = line.strip()

        if target_repository is None:
            repository_match = _TARGET_REPOSITORY.search(line)
            if repository_match is not None:
                target_repository = repository_match.group("repository").strip()

        for scope_match in _PROTECTED_SCOPE.finditer(line):
            constraints.append(
                DecisionConstraint(
                    kind=ConstraintKind.PROTECTED_SCOPE,
                    text=stripped,
                    line=line_no,
                    scopes=(scope_match.group("scope").strip(),),
                )
            )

        if stripped.casefold().rstrip("*_ ") in DIRECTIVE_SECTIONS:
            _drain()
            in_directive_section = True
            continue
        if stripped.startswith("#"):
            _drain()
            in_directive_section = False
            continue
        if not stripped:
            _drain()
            continue

        bullet = _BULLET.match(line)
        numbered = _NUMBERED.match(line)
        if bullet is not None:
            _drain()
            pending.append(bullet.group("body"))
            pending_line = line_no
            continue
        if numbered is not None and in_directive_section:
            _drain()
            constraints.append(
                DecisionConstraint(
                    kind=ConstraintKind.ENGINE_REQUIREMENT,
                    text=numbered.group("body"),
                    line=line_no,
                )
            )
            continue
        if pending:
            pending.append(stripped)
            continue
        if in_directive_section:
            pending.append(stripped)
            pending_line = line_no

    _drain()

    if target_repository is None:
        raise AuthorityError(
            f"{artifact.path}: operator decision does not name a target repository; "
            "refusing to bind work to a repository the operator never stated"
        )
    if not dispositions:
        raise AuthorityError(
            f"{artifact.path}: operator decision carries no recognisable disposition"
        )

    conflicts = _detect_conflicts(dispositions)
    return OperatorDecision(
        artifact=artifact,
        target_repository=target_repository,
        dispositions=tuple(dispositions),
        constraints=tuple(constraints),
        conflicts=conflicts,
        approval_is_exclusive=approval_is_exclusive,
        unclassified_directives=tuple(unclassified),
    )


def _detect_conflicts(dispositions: list[Disposition]) -> tuple[AuthorityConflict, ...]:
    """An item may carry several stances; incompatible ones are a hard conflict."""
    by_item: dict[str, list[Disposition]] = defaultdict(list)
    for entry in dispositions:
        by_item[entry.item_id].append(entry)

    conflicts: list[AuthorityConflict] = []
    for item_id, entries in sorted(by_item.items()):
        kinds = {entry.kind for entry in entries}
        if len(kinds) <= 1:
            continue
        # APPROVED alongside anything else is a genuine contradiction: the other
        # stances all withhold or delay permission the approval grants.
        if DispositionKind.APPROVED in kinds or DispositionKind.REJECTED in kinds:
            conflicts.append(
                AuthorityConflict(
                    item_id=item_id,
                    kinds=tuple(sorted(kinds, key=lambda kind: kind.value)),
                    evidence=tuple(entry.evidence.excerpt() for entry in entries),
                )
            )
    return tuple(conflicts)
