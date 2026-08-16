"""Contract-lens WARNINGS for proposal quality — warn-only, by ruling.

Reviewing all 18 queued plans on 2026-08-08 (anchors re-verified at main
bc94156d; full deltas in ~/.omniagentos/ops/Research/loops/proposal-quality-review-
2026-08-08.md) found the SAME six defect classes recurring across authors and
days. Each one costs a foreign executor — a model with no conversation history
inheriting the plan cold — a clarification round or a wrong guess:

  1. tests without invocations            (13 of 18 plans)
  2. unresolvable dependency tokens        (9)
  3. no per-step done-criteria             (8)
  4. evidence not re-runnable              (7)
  5. missing considered_and_rejected       (7)
  6. conversation-context references       (6)

This module detects them. It NEVER refuses: every finding is a warning, the
exit code is untouched, and that is a ruling, not an oversight — CONTRACT.md's
base schema deliberately never requires house doctrine (its opening lines), so
refusing on these classes needs a contract version bump or an explicit operator
adoption of the lens as admission doctrine. Until then the lens teaches at the
one moment the author is still holding the artifact: filing time.

Everything already admitted or parked is grandfathered by construction — the
lens runs only inside the writers (file_proposal.py / file_inquiry.py), never
against the queue at rest.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Conversation shorthand that means nothing to a model with no history. Kept
# deliberately narrow: false positives here train authors to ignore the lens.
_CONVERSATION_REFS = re.compile(
    r"\b(this batch|as discussed|per (?:our|the) (?:conversation|chat|call)|"
    r"[A-Z][a-z]+-named)\b")

# A tests_required entry with none of these tokens names a behaviour, not a
# test — the foreign implementer must invent the harness.
_INVOCATION = re.compile(
    r"(pytest|python3?\s|npx\s|npm\s|vitest|bash\s|sh\s+-c|make\s|uv\s+run|"
    r"\.venv/bin/|curl\s|sqlite3\s|git\s)")

_SHA_REF = re.compile(r"sha256[:_][0-9a-f]{8,}")
_LOCATOR = re.compile(r"(/|\.py\b|\.md\b|\.sh\b|\.sql\b|\.json\b|https?://)")
_PLAN_DOC = re.compile(r"[~/][^\"'\s]*/Plans/[^\"'\s]+\.md")
_NUMBERED_STEPS = re.compile(r"(?:^|\n|\. )\s*[2-9]\.\s")
_DONE_MARK = re.compile(r"DONE when|done_when", re.IGNORECASE)


def _evidence_entries(art: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """(dict evidence entries, whether prose-string evidence bullets exist).

    Payload evidence is often a list of prose strings on this queue; only dict
    entries can carry verified_by/command, so only those are graded — but the
    presence of prose bullets changes the WORDING of the warning (the author
    tried; the upgrade path is one structured entry, not evidence from zero).
    """
    payload = art.get("payload")
    out: list[dict[str, Any]] = []
    prose = False
    for source in (art.get("evidence"),
                   payload.get("evidence") if isinstance(payload, dict) else None):
        if isinstance(source, list):
            out.extend(e for e in source if isinstance(e, dict))
            prose = prose or any(isinstance(e, str) and e.strip() for e in source)
    return out, prose


def _blank(value: Any) -> bool:
    """Present-but-empty reads as absent — a field check is not satisfied by an
    empty string, an all-whitespace string, an empty list, or a list whose
    every entry is itself blank (real proposals use list-shaped values for
    considered_and_rejected, so ``["  ", "\\t"]`` must not pass)."""
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return all(_blank(v) for v in value)   # empty list -> all() -> True
    if isinstance(value, dict):
        return all(_blank(v) for v in value.values())
    return value is None


def lens_warnings(art: dict[str, Any]) -> list[str]:
    """The six classes, at most one warning each — so on any artifact,
    len(warnings) == len({prefixes}) <= 6. Class 2 (lens.deps) covers all
    unresolvable/unpinned references: opaque dependency tokens, a missing
    observed_at_main_sha, and an unpinned external plan authority.

    Never raises: a lens crash would block a filing, and the lens is advisory
    by ruling. Non-dict payloads grade as empty.
    """
    warnings: list[str] = []
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}

    # -- class 4: evidence not re-runnable ---------------------------------
    entries, has_prose = _evidence_entries(art)
    executed = [e for e in entries
                if e.get("verified_by") == "execution"
                and not _blank(e.get("command"))]
    if not executed:
        if has_prose:
            warnings.append(
                "lens.evidence: evidence exists only as prose bullets — add ONE "
                "structured entry {claim, verified_by:'execution', command, "
                "exit_code} so an inheriting implementer can re-run it")
        else:
            warnings.append(
                "lens.evidence: no verified_by:'execution' evidence entry with a "
                "command — nothing in this artifact is re-runnable by the "
                "implementer that inherits it")
    else:
        relative = [
            str(e.get("command", ""))[:60]
            for e in executed
            if str(e.get("command", "")).strip()
            and not str(e.get("command")).lstrip().startswith(("/", "~", "cd /", "cd ~", "env "))
            and " -C " not in str(e.get("command"))
        ]
        if relative:
            warnings.append(
                f"lens.evidence: {len(relative)} execution command(s) are cwd-"
                f"relative (first: {relative[0]!r}) — a foreign executor runs "
                "from an unknown cwd; anchor with an absolute path or `git -C`")

    # -- class 1: tests without invocations --------------------------------
    tests = payload.get("tests_required")
    if isinstance(tests, list) and tests:
        bare = [str(t)[:50] for t in tests if not _INVOCATION.search(str(t))]
        if bare:
            warnings.append(
                f"lens.tests: {len(bare)} of {len(tests)} tests_required entries "
                f"carry no runnable invocation (first: {bare[0]!r}) — name the "
                "command, the test file, and the MUST-FAIL base")

    # -- class 5: missing considered_and_rejected --------------------------
    # _blank now handles list- and dict-shaped values, so a list of blank
    # strings counts as absent — no special-casing needed.
    if _blank(payload.get("considered_and_rejected")):
        warnings.append(
            "lens.dead_ends: no considered_and_rejected — a successor may "
            "'improve' the fix into a dead end nobody recorded")

    # -- class 2: unresolvable or unpinned references — ONE warning --------
    # Opaque dependency tokens, a missing observed_at_main_sha, and an
    # unpinned external plan authority are the same defect at three sites: a
    # reference a cold model cannot resolve or re-verify.
    blob = json.dumps(payload, ensure_ascii=False)
    ref_problems = []
    deps = payload.get("dependencies")
    if isinstance(deps, list):
        opaque = [
            str(d)[:60] for d in deps
            if not _SHA_REF.search(str(d)) and not _LOCATOR.search(str(d))
        ]
        if opaque:
            ref_problems.append(
                f"{len(opaque)} dependenc(ies) resolve to no artifact id, path, "
                f"or URL (first: {opaque[0]!r})")
    if _blank(payload.get("observed_at_main_sha")):
        ref_problems.append("no observed_at_main_sha, so line anchors cannot be "
                            "re-verified once main moves")
    if _PLAN_DOC.search(blob) and _blank(payload.get("plan_authority_sha256")):
        ref_problems.append("delegates to an external plan document without "
                            "plan_authority_sha256, so the authority can drift "
                            "under the implementer mid-build")
    if ref_problems:
        warnings.append("lens.deps: " + "; ".join(ref_problems))

    # -- class 6: conversation-context references --------------------------
    hit = _CONVERSATION_REFS.search(blob)
    if hit:
        warnings.append(
            f"lens.context: conversation-context reference {hit.group(0)!r} — "
            "meaningless to a foreign model with no history; inline the fact "
            "instead")

    # -- class 3: no per-step done-criteria --------------------------------
    plan = payload.get("implementation_plan")
    if isinstance(plan, str) and _NUMBERED_STEPS.search(plan) and not _DONE_MARK.search(plan):
        warnings.append(
            "lens.checkpoints: multi-step implementation_plan carries no "
            "per-step 'DONE when' — a successor finding a half-built lane "
            "cannot tell which step completed")

    return warnings
