"""Stage B — Analysis and proposals for the self-learning loop."""

from __future__ import annotations

import heapq
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.improvement_chain import run_kimi_json_result

_LOG = logging.getLogger(__name__)

# Cap evidence contribution to ~512KB to avoid exceeding transport limits (E2BIG fix 2026-07-31)
_EVIDENCE_BUDGET_BYTES = 512 * 1024


class ImprovementProposal(BaseModel):
    id: str
    kind: Literal[
        "model_config",
        "formation",
        "router_weight",
        "effort_override",
        "category_pin",
        "brief_template",
        "lesson",
        "skill",
        "rollback",
    ]
    target: Any  # e.g., {file: "configs/swarm.yaml", key: "lane_floors"} or "docs/lessons/..."
    current: Any
    proposed: Any
    rationale: str
    evidence_refs: list[str] = Field(default_factory=list)
    predicted_impact: str | None = None
    risk_class: Literal["low", "model", "major", "irreversible"]


def is_hard_stop(kind: str, target: Any) -> bool:
    """A NON-AUTHORITATIVE pre-filter over freshly generated proposals.

    THIS IS NOT THE SECURITY BOUNDARY, and nothing may be built on it as if it
    were. The boundary is :func:`omniagentos.reflection.guard.authorise_write`,
    which every writer obtains its path from and which no caller — including
    ``POST /reflection/{id}/approve`` — can go around.

    What this does is cheaper and weaker on purpose: a substring scan of
    ``json.dumps(target).lower()``, with no path normalisation, no symlink
    resolution, no :data:`~omniagentos.reflection.guard.PROTECTED_PREFIXES` and
    no ``AGENTS.md``/``CLAUDE.md``. It runs at proposal-GENERATION time, on
    model output, before any target is known to exist, to keep obviously-doomed
    suggestions out of the queue and out of the reviewer's inbox. Its false
    negatives cost a wasted row; they cannot cost a write.

    Two rule sets exist here deliberately, and the reason they are allowed to
    differ is that only one of them is load-bearing. That was NOT true before
    2026-08-07: until the hard-stop was wired into the writers, a gap in either
    filter was a gap in enforcement. Do not restore that situation by treating
    a pass here as permission, and do not converge this into the guard either —
    a generation-time filter that resolves symlinks against a repo root is
    answering a question it does not yet have the information to ask.

    The rules it screens for:
    - risk pins (external/deploy/destructive -> Claude)
    - configs/governance.yaml
    - omniagentos/policy/**
    - budget caps
    - credentials
    """
    target_str = ""
    if isinstance(target, dict):
        target_str = json.dumps(target).lower()
    elif isinstance(target, str):
        target_str = target.lower()

    # Rule checks
    if "governance.yaml" in target_str:
        return True
    if "policy/" in target_str or "policy\\" in target_str:
        return True
    if "budget" in target_str and ("cap" in target_str or "limit" in target_str):
        return True
    if (
        "credential" in target_str
        or "key" in target_str
        or "secret" in target_str
        or "token" in target_str
    ):
        if "model" not in target_str and "router" not in target_str:
            return True

    # Check kind and target for risk pin
    if kind == "risk_pin":
        return True

    return False


class EvidenceStarved(RuntimeError):
    """The proposer could not be shown real evidence, so it refuses to ask.

    Replaces the summary-only fallback (R0-2 item 2).  That fallback discarded
    ALL evidence and carried on, so 31 recorded nights asked a model to improve
    the system while showing it a three-field note about its own truncation.  A
    judgement made with the evidence deleted is worse than no judgement, and it
    is indistinguishable from a good one downstream -- textbook favourable
    absence.  Failing loudly at ``propose`` is the whole point.
    """


def _encoded_size(payload: Any) -> int:
    return len(json.dumps(payload, indent=2).encode("utf-8"))


# Fields that are NOT current-night evidence, and so cannot make a starved
# night look substantive. `compiled_learnings` is the HISTORICAL lessons corpus
# (the harvester attaches it every night regardless of what happened), and
# `caps_hit` is the harvester's own metadata about its truncation. A night whose
# every record list is empty but which carries one old lesson and a cap note
# would otherwise pass the gate and be handed to the proposer as "evidence" --
# a subtler shape of the same favourable absence.
_NON_RECORD_KEYS = frozenset(
    {
        "compiled_learnings",
        "caps_hit",
        "date",
        "started_at",
        "finished_at",
        "bytes_read",
        "sources_read",
    }
)


def _record_count(evidence_data: dict[str, Any]) -> int:
    """Count actual harvested RECORDS, ignoring the scaffolding that holds them.

    Counting non-empty top-level containers is not the same question, and the
    difference is load-bearing: ``harvest_evidence`` assigns
    ``harness_transcripts[name] = digests`` for every active adapter
    (harvest.py:591-610), so a night that harvested NOTHING still ships a
    six-key dict of empty lists. A presence check sees ``len(...) == 6``, calls
    that evidence, and the starved packet reaches the proposer anyway -- the
    exact failure the gate exists to stop, wearing the gate's own uniform.

    So containers contribute their CONTENTS, one level down: a dict of six
    empty lists counts zero. A night that genuinely harvested nothing therefore
    raises, which is the intended outcome -- there is nothing to learn from it,
    and asking anyway is what produced 31 evidence-free judgements.
    """
    total = 0
    for key, value in evidence_data.items():
        if key in _NON_RECORD_KEYS:
            continue
        if isinstance(value, list):
            total += len(value)
        elif isinstance(value, dict):
            for sub in value.values():
                total += len(sub) if isinstance(sub, list | dict) else 1
    return total


def _trim_container(value: list[Any] | dict[str, Any], keep: int) -> list[Any] | dict[str, Any]:
    """Trim a container to *keep* entries.

    LISTS keep the LAST *keep* entries: evidence lists are time-ordered and
    recency is what a nightly improvement judgement is about.

    DICTS keep the *keep* LARGEST entries by encoded size, restored to their
    original key order -- explicitly NOT the last *keep* by insertion order.
    A dict's key order is not chronology, and assuming it was produced exactly
    the failure this whole lane exists to stop: on the real evidence,
    ``harness_transcripts`` is ordered ``claude, gemini, kimi, codex, grok,
    swarm_verdicts`` where ``claude`` holds all 170 KB of actual transcript and
    the other five are empty lists. Keeping the "last 3" therefore dropped every
    real transcript and kept three empty ones -- a payload that still looks
    populated with all of its content gone. Size-ranking makes the empties the
    first thing sacrificed, by construction.
    """
    if keep <= 0:
        return [] if isinstance(value, list) else {}
    if isinstance(value, list):
        return value[-keep:]
    ranked = sorted(value, key=lambda key: (-_encoded_size(value[key]), key))[:keep]
    survivors = set(ranked)
    return {key: value[key] for key in value if key in survivors}


def _cap_evidence(
    evidence_data: dict[str, Any], budget_bytes: int = _EVIDENCE_BUDGET_BYTES
) -> dict[str, Any]:
    """Truncate evidence to fit a byte budget by SIZE, largest container first.

    Large evidence contributions cause transport failures (E2BIG, ARG_MAX), so
    the budget itself stays: it is not the defect.  The defect was WHICH key got
    trimmed.  The previous implementation matched four hardcoded names
    ("runs", "sessions", "executions", "records") and ``break``ed on the first
    match, so against the real evidence shape it trimmed ``runs`` -- 2 entries,
    20 KB on 2026-08-05 -- declared itself finished, and never reached
    ``runs_ledger_attempts``, the 1.26 MB key that actually blew the budget.
    Control then fell through to the summary-only fallback, which threw the
    evidence away entirely.

    So: rank every top-level container by encoded size and trim the largest
    until the payload fits.  Every trim is DISCLOSED to the model as a
    ``_truncated_<key>`` note, because silently showing a model a subset and
    letting it believe it saw everything is the same defect in a smaller coat.

    Raises:
        EvidenceStarved: when no amount of container trimming fits the budget
            (i.e. the non-trimmable remainder alone exceeds it).  The message
            names the keys responsible, so the refusal carries its own remedy.
    """
    # ZERO-EVIDENCE GATE (checked BEFORE the budget, because a starved payload
    # is small and would sail straight through a size check).
    #
    # ``run_propose`` manufactures ``{"note": "No evidence file found..."}`` when
    # the harvester produced nothing and ``{"error": "<JSONDecodeError>"}`` when
    # evidence.json will not parse. Both are UNDER budget, so a size-only guard
    # lets the exact failure this lane exists to stop walk through the front
    # door: a model asked to improve the system while shown a one-line note
    # about why there is nothing to show it.
    if not isinstance(evidence_data, dict) or _record_count(evidence_data) == 0:
        keys = ", ".join(sorted(map(str, evidence_data))) if isinstance(evidence_data, dict) else ""
        raise EvidenceStarved(
            "Evidence contains no harvested records "
            f"(top-level keys: {keys or '<none>'}). Refusing to ask the proposer "
            "for a judgement with nothing to judge -- check that the harvest "
            "stage ran and that var/reflection/<date>/evidence.json parsed."
        )

    evidence_bytes = _encoded_size(evidence_data)
    if evidence_bytes <= budget_bytes:
        return evidence_data

    _LOG.info(
        "Evidence size %d bytes exceeds budget %d; truncating largest-first...",
        evidence_bytes,
        budget_bytes,
    )

    # Trimmable units are PATHS, at most one level deep, not just top-level keys.
    #
    # A container-of-containers is trimmed INSIDE its entries rather than by
    # dropping entries: `harness_transcripts` is a dict of six per-provider
    # lists, and dropping providers is both lossy in the wrong dimension and
    # insufficient -- on a day when one provider's transcripts alone exceed the
    # budget, entry-dropping bottoms out with that one entry still over, and the
    # whole run would refuse. Trimming within each provider's list keeps every
    # provider key present AND can actually reach the budget.
    units: list[tuple[str, ...]] = []
    for key, value in evidence_data.items():
        if not isinstance(value, list | dict) or len(value) == 0:
            continue
        if isinstance(value, dict) and any(
            isinstance(sub, list | dict) and len(sub) > 0 for sub in value.values()
        ):
            units.extend(
                (key, sub_key)
                for sub_key, sub in value.items()
                if isinstance(sub, list | dict) and len(sub) > 0
            )
        else:
            units.append((key,))

    def _at(path: tuple[str, ...]) -> Any:
        value = evidence_data[path[0]]
        return value if len(path) == 1 else value[path[1]]

    original_counts = {path: len(_at(path)) for path in units}
    counts = dict(original_counts)
    # Sizes are tracked incrementally: re-encoding the whole 2-7 MB payload to
    # evaluate every candidate is O(units x payload) and costs seconds a night.
    sizes = {path: _encoded_size(_at(path)) for path in units}

    def _build() -> dict[str, Any]:
        out = dict(evidence_data)
        for path, count in counts.items():
            if count >= original_counts[path]:
                continue
            container = _at(path)
            trimmed = _trim_container(container, count)
            if len(path) == 1:
                out[path[0]] = trimmed
            else:
                parent = out[path[0]]
                if parent is evidence_data[path[0]]:
                    parent = dict(evidence_data[path[0]])
                    out[path[0]] = parent
                parent[path[1]] = trimmed
            kept = "last" if isinstance(container, list) else "largest"
            label = ".".join(path)
            out[f"_truncated_{label}"] = f"kept {kept} {count} of {original_counts[path]}"
        return out

    # HALVE THE LARGEST UNIT, repeatedly — never "empty the largest, then move
    # on". Largest-first-to-completion is itself a starvation: on the 7.3 MB
    # 2026-08-01 shape it trimmed `runs` to 0 of 89, so the proposer would have
    # been shown a full sessions list and NO runs at all. Halving spreads the
    # loss and holds every unit at >= 1 entry, so the model still sees the SHAPE
    # of everything the harvester collected.
    #
    # Only PRODUCTIVE trims are kept. Each disclosed `_truncated_<path>` note
    # costs ~36 bytes, so trimming a tiny container can leave the payload BIGGER
    # than before (measured: a 640 KB payload of 20,000 two-item lists GROWS
    # ~36 bytes per trim). A unit whose trim does not shrink the payload is
    # retired, not retried.
    #
    # Accounting is INCREMENTAL: only the victim unit is re-encoded per step and
    # the running total is adjusted by its delta plus the one-off note cost.
    # Re-encoding the whole payload per candidate is O(units x payload) — on
    # that same 20,000-unit input, 20,000 full encodes of 640 KB.
    trimmed_keys: set[tuple[str, ...]] = set()
    unproductive: set[tuple[str, ...]] = set()
    estimated = evidence_bytes
    guard = 0
    truncated = _build()
    final_bytes = _encoded_size(truncated)

    # `remaining` tracks every still-trimmable unit's CURRENT size, and `heap`
    # is a max-size-first priority queue over it (lazily invalidated: a popped
    # entry is only real if `remaining[path]` still equals the size it was
    # pushed with). This replaces rebuilding a dict comprehension over EVERY
    # unit on EVERY iteration -- on a 20,000-unit payload that rebuild alone is
    # O(units) per pick, and retiring all 20,000 units (each pick is O(1)
    # productive work) turned into O(units^2), spinning for minutes on an input
    # this lane is supposed to retire in one guarded pass. Ties still break by
    # key name, matching the heap's own tuple ordering, so an identical input
    # trims identically.
    remaining: dict[tuple[str, ...], int] = {
        path: sizes[path] for path in units if counts[path] > 1
    }
    heap: list[tuple[int, tuple[str, ...]]] = [(-size, path) for path, size in remaining.items()]
    heapq.heapify(heap)

    def _pop_victim() -> tuple[str, ...] | None:
        while heap:
            neg_size, path = heapq.heappop(heap)
            if remaining.get(path) == -neg_size:
                return path
        return None

    # The estimate is deliberately allowed to be slightly LOW (a value encoded
    # standalone renders smaller than the same value nested and indented), so
    # the exact size is re-measured after each pass and fed back in. Without
    # that correction an optimistic estimate could stop early and refuse a
    # payload that more trimming would have fitted -- a false starvation, which
    # is exactly as harmful as the silent truncation this replaces.
    for _pass in range(16):
        if final_bytes <= budget_bytes:
            break
        estimated = max(estimated, final_bytes)
        while estimated > budget_bytes and guard < 100_000:
            guard += 1
            if not remaining:
                break
            is_small = len(remaining) <= 64
            victim = _pop_victim()
            if victim is None:
                if remaining:
                    _LOG.warning(
                        "trim heap exhausted with %d units still trimmable", len(remaining)
                    )
                break
            new_count = max(1, counts[victim] // 2)
            new_size = _encoded_size(_trim_container(_at(victim), new_count))
            delta = new_size - sizes[victim]
            if victim not in trimmed_keys:
                # The first trim of a unit also adds its `_truncated_<path>` note.
                delta += len(
                    f'"_truncated_{".".join(victim)}": "kept largest {new_count} '
                    f'of {original_counts[victim]}",'
                )
            # STRICT SHRINK, verified against the ACTUAL payload -- on BOTH the
            # cost-positive AND the cost-negative estimate. The `delta` above is
            # only a hint: a value renders smaller nested-and-indented than it
            # does standalone, and a `_truncated_<path>` note's real cost differs
            # from its literal length. Accepting a trim purely because its
            # ESTIMATED delta is negative discards a whole record on a promise the
            # payload might not keep -- a 213 -> 210 "shrink" that is actually
            # size-neutral (the note eats every byte the dropped record saved)
            # loses evidence for no budget gain. So every accepted trim is
            # re-measured against the real current payload and kept ONLY if it
            # genuinely shrank it. This also keeps two units that only fit in
            # COMBINATION: the re-measure compares against the CURRENT payload,
            # which already carries the trims accepted earlier this pass, so a
            # step that finishes the reduction jointly still reads as a shrink.
            #
            # The exact re-measure is skipped only on pathologically wide payloads
            # (>64 trimmable units), where two full encodes per candidate would
            # reintroduce the O(units x payload) cost this incremental accounting
            # exists to avoid and where no single unit can be decisive; there the
            # estimate governs.
            if is_small:
                current = _encoded_size(_build())
                saved = counts[victim]
                counts[victim] = new_count
                exact = _encoded_size(_build())
                counts[victim] = saved
                if exact < current:
                    counts[victim] = new_count
                    sizes[victim] = new_size
                    trimmed_keys.add(victim)
                    final_bytes = exact
                    estimated = exact
                    if new_count > 1:
                        remaining[victim] = new_size
                        heapq.heappush(heap, (-new_size, victim))
                    else:
                        del remaining[victim]
                    continue
                unproductive.add(victim)
                del remaining[victim]
                continue

            # Wide-payload fallback: no exact re-measure, so trust the estimate.
            # A trim whose estimated disclosure cost meets or exceeds its saving
            # is retired; otherwise it is accepted and the running total adjusted.
            if delta >= 0:
                unproductive.add(victim)
                del remaining[victim]
                continue
            counts[victim] = new_count
            sizes[victim] = new_size
            trimmed_keys.add(victim)
            estimated += delta
            if new_count > 1:
                remaining[victim] = new_size
                heapq.heappush(heap, (-new_size, victim))
            else:
                del remaining[victim]

        truncated = _build()
        final_bytes = _encoded_size(truncated)
        if not any(counts[path] > 1 and path not in unproductive for path in counts):
            break

    if final_bytes <= budget_bytes:
        _LOG.info(
            "Evidence truncated to %d bytes (within %d budget); trimmed %s",
            final_bytes,
            budget_bytes,
            ", ".join(
                f"{'.'.join(path)} {original_counts[path]}->{counts[path]}"
                for path in counts
                if counts[path] < original_counts[path]
            )
            or "nothing",
        )
        return truncated

    # Nothing left to trim: the non-container remainder alone busts the budget.
    # Name the offenders rather than emitting a content-free note.
    residual = sorted(
        ((key, _encoded_size(value)) for key, value in truncated.items()),
        key=lambda item: -item[1],
    )[:5]
    offenders = ", ".join(f"{key} ({size} bytes)" for key, size in residual)
    raise EvidenceStarved(
        f"Evidence cannot be reduced below the {budget_bytes}-byte budget "
        f"(still {final_bytes} bytes after trimming every top-level container; "
        f"original {evidence_bytes} bytes). Largest remaining: {offenders}. "
        "Refusing to ask the proposer for a judgement with the evidence "
        "discarded -- shrink the harvester's output or raise the budget "
        "deliberately."
    )


def build_analyst_prompt(
    digest_text: str,
    evidence_data: dict[str, Any],
    compiled_learnings: str,
) -> str:
    """Build the prompt for the analyst model."""
    # Cap evidence to avoid transport failures (E2BIG fix 2026-07-31)
    capped_evidence = _cap_evidence(evidence_data)
    evidence_str = json.dumps(capped_evidence, indent=2)
    prompt = f"""You are the senior architect and analyst for OmniAgentOS.
Your goal is to analyze the day's execution transcripts, outcomes, metrics, and compiled historical learnings, and propose typed improvements to model configuration and orchestration patterns.

Here is the Digest of the day's executions:
--- START OF DIGEST ---
{digest_text}
--- END OF DIGEST ---

Here is the parsed Evidence and Metrics from the executions:
--- START OF EVIDENCE ---
{evidence_str}
--- END OF EVIDENCE ---

Here are the compiled historical learnings (last 2-4 weeks):
--- START OF COMPILED LEARNINGS ---
{compiled_learnings}
--- END OF COMPILED LEARNINGS ---

We also have a strict HARD-STOP list. DO NOT propose changes that modify:
- risk pins (external/deploy/destructive -> Claude)
- configs/governance.yaml
- omniagentos/policy/** (any file under policy directory)
- budget caps or limits
- credentials, API keys, or secrets

Based on this data, identify opportunities to improve. You must generate 1 to 5 highly specific, actionable Improvement Proposals.
Each proposal MUST conform EXACTLY to the following JSON schema:
{{
  "id": "rfl_prop_<unique_identifier>",  // Must start with 'rfl_prop_' followed by a unique string
  "kind": "model_config|formation|router_weight|effort_override|category_pin|brief_template|lesson|skill|rollback",
  "target": <string or dict>,            // The specific file, path, or key being targetted (e.g. {{"file": "configs/swarm.yaml", "key": "lane_floors"}} or a file path "docs/lessons/...")
  "current": <string, number, or dict>,  // The current value or representation
  "proposed": <string, number, or dict>, // The proposed new value or representation
  "rationale": "<reasoning explaining why this change is necessary and how it fixes observed failures>",
  "evidence_refs": ["<list of references, e.g. session IDs, run IDs, or lessons matching the evidence>"],
  "predicted_impact": "<predicted impact of this change on success rate or metrics>",
  "risk_class": "low|model|major|irreversible"
}}

Respond with ONLY a valid JSON array of these proposals inside a single markdown JSON codeblock, like so:
```json
[
  ...
]
```
Do not include any conversational filler, introductory text, or concluding text outside the JSON codeblock. Only output the valid JSON array in the codeblock.
"""
    return prompt


def call_llm(prompt: str) -> str:
    """Call the configured Kimi loop model for structured proposals.

    Surfaces spawn and transport errors distinctly from model parse failures
    (E2BIG fix 2026-07-31).
    """
    proposal_item = {
        "type": "object",
        "required": [
            "id",
            "kind",
            "target",
            "current",
            "proposed",
            "rationale",
            "evidence_refs",
            "risk_class",
        ],
        "properties": {
            "id": {"type": "string"},
            "kind": {"type": "string"},
            "target": {},
            "current": {},
            "proposed": {},
            "rationale": {"type": "string"},
            "evidence_refs": {"type": "array", "items": {"type": "string"}},
            "predicted_impact": {"type": ["string", "null"]},
            "risk_class": {"type": "string"},
        },
    }
    schema = {
        "type": "object",
        "required": ["proposals"],
        "properties": {"proposals": {"type": "array", "items": proposal_item}},
    }
    _LOG.info("Invoking configured loop proposer for proposals...")
    try:
        result = run_kimi_json_result(
            prompt + "\n\nTransport note: return one JSON object with a `proposals` array; "
            "the array items use the proposal schema above.",
            schema,
            wall_ms=180_000,
            # This is the seam that carries 11 of 12 genuine failures, and its
            # primary is a single vendor whose credential is paused. Opt into
            # the configured second lineage (R0-2 item 4).
            allow_fallback=True,
        )
    except OSError as exc:
        # Spawn/transport failure — surface clearly (E2BIG fix 2026-07-31)
        raise RuntimeError(f"Kimi spawn failed: {exc}") from exc

    output = result.output
    if output is None:
        # Record the CLASSIFIED cause. The old message asserted a mechanical
        # spawn failure for every class alike -- park, auth, quota, sandbox
        # refusal, adapter-resolution error -- so reflection_runs.error has
        # been misattributing every one of them. An instrument error reported
        # as a candidate defect sends the next agent to debug the wrong thing.
        raise RuntimeError(
            f"Proposal stage produced no output [{result.outcome or 'unclassified'}]: "
            f"{result.describe()}"
        )
    if not isinstance(output.get("proposals"), list):
        raise RuntimeError(
            "Proposal stage returned invalid structure: "
            f"proposals={type(output.get('proposals')).__name__}"
        )
    return json.dumps(output["proposals"], ensure_ascii=False)


def extract_json_array(text: str) -> list[dict[str, Any]]:
    """Extract and parse the JSON array from LLM response."""
    # Find JSON block
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        json_str = match.group(1).strip()
    else:
        # Fallback to searching for the outer [ ]
        match_fallback = re.search(r"(\[.*\])", text, re.DOTALL)
        if match_fallback:
            json_str = match_fallback.group(1).strip()
        else:
            json_str = text.strip()

    try:
        data = json.loads(json_str)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            # If a single dictionary was returned, wrap it in a list
            return [data]
        raise ValueError("Parsed JSON is not a list or dict")
    except Exception as exc:
        _LOG.error("Failed to parse JSON: %s\nRaw text: %s", exc, text)
        raise ValueError(f"JSON parsing failed: {exc}") from exc


def run_propose(
    date_str: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Execute Stage B: read digest, propose improvements, filter hard-stops, persist."""
    if date_str is None:
        date_str = utc_now_iso()[:10]

    # Paths
    root_path = Path(__file__).resolve().parents[2]
    var_path = root_path / "var" / "reflection" / date_str
    digest_path = var_path / "digest.md"
    evidence_path = var_path / "evidence.json"

    digest_text = ""
    evidence_data: dict[str, Any] = {}
    compiled_learnings = ""

    # Load digest and evidence if present
    if digest_path.is_file():
        digest_text = digest_path.read_text(encoding="utf-8")
    else:
        digest_text = f"No digest file found for date {date_str}."

    if evidence_path.is_file():
        try:
            evidence_data = json.loads(evidence_path.read_text(encoding="utf-8"))
        except Exception as exc:
            _LOG.warning("Failed to load evidence.json: %s", exc)
            evidence_data = {"error": str(exc)}
    else:
        evidence_data = {"note": f"No evidence file found for date {date_str}."}

    # Extract compiled_learnings from evidence or load from docs/lessons
    if "compiled_learnings" in evidence_data:
        compiled_learnings = str(evidence_data["compiled_learnings"])
    else:
        # Fallback: search docs/lessons/ for recent md files
        lessons_dir = root_path / "docs" / "lessons"
        if lessons_dir.is_dir():
            parts = []
            for path in sorted(lessons_dir.glob("*.md"), reverse=True)[:3]:
                parts.append(f"--- File: {path.name} ---\n{path.read_text(encoding='utf-8')}")
            compiled_learnings = "\n\n".join(parts)
        else:
            compiled_learnings = "No compiled learnings or historic lessons found."

    prompt = build_analyst_prompt(digest_text, evidence_data, compiled_learnings)

    # Call LLM and parse
    raw_output = ""
    proposals_raw: list[dict[str, Any]] = []
    try:
        raw_output = call_llm(prompt)
        proposals_raw = extract_json_array(raw_output)
    except Exception as exc:
        _LOG.warning(
            "First proposal call failed or failed to parse. Retrying once... error: %s", exc
        )
        try:
            raw_output = call_llm(prompt)
            proposals_raw = extract_json_array(raw_output)
        except Exception as exc_retry:
            _LOG.error("Retry also failed: %s", exc_retry)
            # If we're in offline/mock environment, or both failed, let's fall back to a dummy list in tests
            raise exc_retry

    valid_proposals: list[ImprovementProposal] = []
    dropped_proposals: list[dict[str, Any]] = []

    for item in proposals_raw:
        # Validate schema
        try:
            p = ImprovementProposal.model_validate(item)
            # Check hard-stops
            if is_hard_stop(p.kind, p.target):
                _LOG.warning(
                    "Proposal %s target %s is inside the hard-stop set. Dropping.", p.id, p.target
                )
                dropped_proposals.append(
                    {
                        "proposal": p.model_dump(),
                        "reason": "Hard-stop constraint hit: target or kind modifies forbidden configs/policy/credentials/budget.",
                    }
                )
            else:
                valid_proposals.append(p)
        except Exception as exc:
            _LOG.warning("Failed to validate proposal item: %s. Item was: %s", exc, item)
            # Try to build a fallback or skip
            continue

    # Persist valid proposals to reflection_proposals
    db = db_path or default_db_path()
    store = SqliteStore(db)

    now = utc_now_iso()
    with store._lock:
        for p in valid_proposals:
            target_str = (
                json.dumps(p.target) if isinstance(p.target, (dict, list)) else str(p.target)
            )
            current_str = (
                json.dumps(p.current) if isinstance(p.current, (dict, list)) else str(p.current)
            )
            proposed_str = (
                json.dumps(p.proposed) if isinstance(p.proposed, (dict, list)) else str(p.proposed)
            )
            evidence_refs_str = json.dumps(p.evidence_refs)

            # Check if already exists to avoid duplication
            exists = store._connection.execute(
                "SELECT 1 FROM reflection_proposals WHERE id = ?", (p.id,)
            ).fetchone()

            if not exists:
                store._connection.execute(
                    """
                    INSERT INTO reflection_proposals (
                        id, kind, target, current, proposed, rationale, evidence_refs_json, predicted_impact, risk_class, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        p.id,
                        p.kind,
                        target_str,
                        current_str,
                        proposed_str,
                        p.rationale,
                        evidence_refs_str,
                        p.predicted_impact,
                        p.risk_class,
                        "pending",
                        now,
                        now,
                    ),
                )
                _LOG.info("Persisted proposal %s with kind %s", p.id, p.kind)
            else:
                # Update existing if pending
                store._connection.execute(
                    """
                    UPDATE reflection_proposals SET
                        kind = ?, target = ?, current = ?, proposed = ?, rationale = ?, evidence_refs_json = ?, predicted_impact = ?, risk_class = ?, updated_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (
                        p.kind,
                        target_str,
                        current_str,
                        proposed_str,
                        p.rationale,
                        evidence_refs_str,
                        p.predicted_impact,
                        p.risk_class,
                        now,
                        p.id,
                    ),
                )

    return {
        "date": date_str,
        "proposals": [p.model_dump() for p in valid_proposals],
        "dropped_proposals": dropped_proposals,
    }


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD target date")
    args = parser.parse_args()
    try:
        run_propose(date_str=args.date)
    except Exception as exc:
        # EXIT NONZERO. This entrypoint used to log "Proceeding gracefully" and
        # fall off the end at status 0, which turned every propose failure --
        # including the EvidenceStarved refusal this lane just added -- back
        # into a success for anything that shells out to it. A stage that
        # refuses must not be reported as a stage that ran.
        _LOG.error("Propose runner failed: %s", exc)
        sys.exit(1)
