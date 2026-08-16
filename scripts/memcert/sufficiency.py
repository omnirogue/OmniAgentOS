#!/usr/bin/env python3
"""memcert deterministic retrieval-sufficiency certification (DESIGN-v2.md §4).

Grades the CONTEXT an arm builds — not a model's answer — against the
generator's private answer state, with zero LLM calls and zero network. For
each (item, arm) pair the arm's ``context_block`` is checked for:

- ``evidence_present`` — every gold evidence statement recorded by ``gen.py``
  (``audit["evidence"]``: the minimal statements sufficient to answer) appears
  in the context under normalized word-boundary containment;
- ``answer_present`` — the gold value itself appears (informational: axis H
  values can be DERIVED from evidence, e.g. ``{prefix}-{service}`` job names,
  so evidence_present is the certified metric, answer_present is not);
- ``stale_only`` — a superseded value appears while the current one does not
  (guaranteed-wrong-or-stale retrieval, the axis D/F hazard).

This is the AutoRAG idea applied to the estate's memory stack: component-level
evaluation of the retrieval pipeline against a gold set, cheap enough to run in
the DEFAULT pytest lane on every merge (tests/memcert/test_sufficiency.py).
It is necessary-not-sufficient for end-to-end correctness — a model can still
fumble present evidence — which is exactly what makes it a clean, deterministic
certification of the retrieval component in isolation.

CLI::

    python scripts/memcert/sufficiency.py --seeds 42,43 \
        --arms system_legacy,system,rag [--scale S] [--budget 12000] \
        [--bars configs/memcert/sufficiency-bars.yaml] [--out PATH]

Exit codes: 0 ok; 1 = a bar floor was breached (``--bars``); 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

try:  # imported as a package member: `scripts.memcert.sufficiency`
    from . import core
    from . import gen as gen_mod
except ImportError:  # pragma: no cover - bare-script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import core  # type: ignore[no-redef]
    import gen as gen_mod  # type: ignore[no-redef]

#: Axes with evidence-bearing items. E (abstention) has none by construction:
#: its correct behaviour is answering from ABSENCE, so context sufficiency is
#: undefined there and E items are skipped.
EVIDENCE_AXES: tuple[str, ...] = ("A", "B", "C", "D", "F", "G", "H")


@dataclass(frozen=True)
class ItemSufficiency:
    """Deterministic context grades for one (item, arm) pair."""

    item_id: str
    axis: str
    level: int
    arm: str
    evidence_present: bool
    answer_present: bool
    stale_present: bool
    stale_only: bool
    context_tokens: int


_SENTENCE_DOT_RE = re.compile(r"\.(?=\s|$)")


def _norm(text: str) -> str:
    """Containment normalizer: ``core.normalize_answer`` plus SENTENCE-dot
    removal. normalize_answer PRESERVES mid-string dots (needed for grading
    decimals/paths), which makes "…on bidatemu." never contain-match the
    needle "…on bidatemu". Only dots followed by whitespace/end are stripped —
    a blanket strip equated "1.2" with "1 2" (codex-critic CR-005)."""
    return " ".join(_SENTENCE_DOT_RE.sub(" ", core.normalize_answer(text)).split())


def _contains(context_norm: str, needle: str) -> bool:
    needle_norm = _norm(needle)
    if not needle_norm:
        return False
    return f" {needle_norm} " in f" {context_norm} "


#: A rendered line's stamp is its LEADING bracket token and must LOOK like a
#: timestamp: ``[YYYY-MM-DD]`` or ``[YYYY-MM-DDT…]``. Anything else — a role
#: bracket, a body bracket like ``[body-note 2027-03-03]``, or a line whose
#: head was truncated away — carries NO stamp.
_STAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})(?:T[^\]]*)?\]")


def _dates_bound(context_block: str, evidence: list[str], dates: list[str]) -> bool:
    """Each evidence statement's line must open with a TIMESTAMP-SHAPED stamp
    whose date equals the evidence turn's date.

    Five review rounds shaped this (gemini-critic F1 R2-R5; codex-critic
    CR-005 R2/R3): dates-anywhere, proximity windows, whole-leading-segment
    matching, any-first-bracket parsing, and substring date matching were each
    spoofable in turn (adjacent stamps, coalesced fragments, ``_tail_fit``
    re-rooting a truncated line at a body bracket). The surviving contract:
    the line must START with ``[YYYY-MM-DD…]`` and that captured date must
    EQUAL the expected one — renderer-earned by construction, since content
    rendering collapses whitespace and only renderers emit line-leading
    timestamp brackets. ``evidence`` and ``dates`` pair 1:1 (caller-enforced).
    """
    prepared: list[tuple[str, str]] = []
    for raw in context_block.splitlines():
        match = _STAMP_RE.match(raw.lstrip())
        prepared.append((f" {_norm(raw)} ", match.group(1) if match else ""))
    for ev, date in zip(evidence, dates, strict=True):
        marker = f" {_norm(ev)} "
        if not any(stamp == date and marker in padded for padded, stamp in prepared):
            return False
    return True


def _value_strings(spec: core.AnswerSpec) -> list[str]:
    """The gold value's leaf strings (any-of for exact via aliases; all-of else)."""
    if spec.kind == "exact":
        return [str(spec.value)]
    if spec.kind in ("set", "ordered"):
        return [str(v) for v in spec.value]
    if spec.kind == "params":
        value = dict(spec.value)
        args = value.get("args", {})
        leaves = [str(v) for v in (args.values() if isinstance(args, dict) else [])]
        return leaves or [str(v) for k, v in value.items() if k != "tool"]
    return []


def evaluate_item(
    item: core.Item,
    evidence: list[str],
    context_block: str,
    arm: str,
    evidence_dates: list[str] | None = None,
) -> ItemSufficiency | None:
    """Grade one item's context. Returns None for evidence-free axes (E).

    ``evidence_dates`` (as-of items, gen.py C-L2): ISO dates of the evidence
    turns that must ALSO be visible in the context — the statements alone
    cannot answer an as-of question, and without this requirement an unstamped
    context falsely certifies temporal ordering (gemini-critic F1). Dates are
    matched as substrings (they appear inside `[stamp]` prefixes and ISO
    timestamps, not as space-delimited words).
    """
    if item.axis not in EVIDENCE_AXES:
        return None
    context_norm = _norm(context_block)
    evidence_present = bool(evidence) and all(_contains(context_norm, ev) for ev in evidence)
    # Date binding fails CLOSED (codex-critic CR-005-R2): an as-of item (C-L2)
    # whose audit lost its dates, or a dates list that does not pair 1:1 with
    # the evidence, must grade insufficient — never silently skip the check.
    if item.axis == "C" and item.level == 2 and not evidence_dates:
        evidence_present = False
    elif evidence_present and evidence_dates:
        if len(evidence_dates) != len(evidence):
            evidence_present = False
        else:
            evidence_present = _dates_bound(context_block, evidence, evidence_dates)

    spec = item.answer_spec
    values = _value_strings(spec)
    if spec.kind == "exact":
        accepted = [str(spec.value), *spec.aliases]
        answer_present = any(_contains(context_norm, v) for v in accepted)
    else:
        answer_present = bool(values) and all(_contains(context_norm, v) for v in values)
    stale_present = any(_contains(context_norm, str(s)) for s in spec.stale_values)
    return ItemSufficiency(
        item_id=item.item_id,
        axis=item.axis,
        level=item.level,
        arm=arm,
        evidence_present=evidence_present,
        answer_present=answer_present,
        stale_present=stale_present,
        stale_only=stale_present and not answer_present,
        context_tokens=len(context_block) // 4,
    )


def evaluate_arm(
    world: Any,
    world_dir: Path,
    arm: str,
    budget_tokens: int,
    *,
    seed: int,
) -> list[ItemSufficiency]:
    """Build ``arm``'s context for every evidence-bearing item and grade it."""
    try:
        from . import arms as arms_mod
    except ImportError:  # pragma: no cover - bare-script execution path
        import arms as arms_mod  # type: ignore[no-redef]

    rows: list[ItemSufficiency] = []
    rng = core.rng_for(seed, f"sufficiency.{arm}")
    for item in world.items():
        audit = world.audit.get(item.item_id, {})
        evidence = [str(e) for e in audit.get("evidence", [])]
        dates = [str(d) for d in audit.get("evidence_dates", [])]
        if item.axis not in EVIDENCE_AXES:
            continue
        # The arm under grading must NEVER see the answer key (run_bench's
        # MC-001 rule, mirrored here — codex-critic CR-006): context builders
        # get a REDACTED item; only this grader keeps the real spec.
        redacted = replace(
            item, answer_spec=core.AnswerSpec(kind=item.answer_spec.kind, value="")
        )
        ctx = arms_mod.build_context(arm, world_dir, redacted, budget_tokens, rng)
        row = evaluate_item(item, evidence, ctx.context_block, arm, evidence_dates=dates)
        if row is not None:
            rows.append(row)
    return rows


def summarize(rows: list[ItemSufficiency]) -> dict[str, dict[str, Any]]:
    """Per-axis sufficiency (mean evidence_present) + stale_only rate + n."""
    by_axis: dict[str, list[ItemSufficiency]] = {}
    for row in rows:
        by_axis.setdefault(row.axis, []).append(row)
    out: dict[str, dict[str, Any]] = {}
    for axis in sorted(by_axis):
        axis_rows = by_axis[axis]
        n = len(axis_rows)
        stale_candidates = [r for r in axis_rows if r.stale_present or r.stale_only]
        out[axis] = {
            "sufficiency": round(sum(r.evidence_present for r in axis_rows) / n, 4),
            "answer_present": round(sum(r.answer_present for r in axis_rows) / n, 4),
            "stale_only": round(
                sum(r.stale_only for r in axis_rows) / n, 4
            ),
            "n": n,
            "n_stale_present": len(stale_candidates),
        }
    return out


def run_sufficiency(
    seeds: list[int],
    arms: list[str],
    *,
    scale: str = "S",
    split: str = "dev",
    budget_tokens: int = 12000,
    fixtures_root: Path | None = None,
) -> dict[str, Any]:
    """Generate the worlds, evaluate every arm, and pool per-axis summaries."""
    pooled_rows: dict[str, list[ItemSufficiency]] = {arm: [] for arm in arms}
    with tempfile.TemporaryDirectory(prefix="memcert-sufficiency-") as tmp:
        root = fixtures_root or Path(tmp)
        for seed in seeds:
            world = gen_mod.generate_world(seed, scale=scale, split=split)
            world_dir = root / f"w{seed}-{scale}"
            if not world_dir.exists():
                world.write_fixtures(world_dir, run_uuid=f"sufficiency-{seed}")
            for arm in arms:
                pooled_rows[arm].extend(
                    evaluate_arm(world, world_dir, arm, budget_tokens, seed=seed)
                )
    return {
        "seeds": seeds,
        "scale": scale,
        "split": split,
        "budget_tokens": budget_tokens,
        "arms": {
            arm: {
                "summary": summarize(rows),
                "rows": [asdict(r) for r in rows],
            }
            for arm, rows in pooled_rows.items()
        },
    }


def check_bars(result: dict[str, Any], bars: dict[str, Any]) -> list[str]:
    """Return human-readable floor breaches (empty = certified).

    Bars shape (configs/memcert/sufficiency-bars.yaml)::

        arms:
          system:
            floors: {A: 0.9, B: 0.9, ...}     # min sufficiency per axis
            stale_only_max: 0.25              # max stale_only rate per axis
        dominance:
          - {arm: system, over: system_legacy}  # per-axis sufficiency >= baseline
    """
    breaches: list[str] = []
    arm_results = result.get("arms", {})
    for arm, spec in (bars.get("arms") or {}).items():
        summary = (arm_results.get(arm) or {}).get("summary") or {}
        for axis, floor in (spec.get("floors") or {}).items():
            got = (summary.get(axis) or {}).get("sufficiency")
            if got is None:
                breaches.append(f"{arm}/{axis}: no sufficiency measured (floor {floor})")
            elif got < float(floor):
                breaches.append(f"{arm}/{axis}: sufficiency {got} < floor {floor}")
        stale_max = spec.get("stale_only_max")
        if stale_max is not None:
            for axis, cell in summary.items():
                if cell.get("n_stale_present", 0) > 0 and cell["stale_only"] > float(stale_max):
                    breaches.append(
                        f"{arm}/{axis}: stale_only {cell['stale_only']} > max {stale_max}"
                    )
    for rule in bars.get("dominance") or []:
        arm, over = str(rule.get("arm")), str(rule.get("over"))
        s_arm = (arm_results.get(arm) or {}).get("summary") or {}
        s_over = (arm_results.get(over) or {}).get("summary") or {}
        for axis in sorted(set(s_arm) & set(s_over)):
            if s_arm[axis]["sufficiency"] < s_over[axis]["sufficiency"]:
                breaches.append(
                    f"dominance: {arm}/{axis} {s_arm[axis]['sufficiency']} < "
                    f"{over}/{axis} {s_over[axis]['sufficiency']}"
                )
    return breaches


def _load_bars(path: Path) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="memcert deterministic retrieval-sufficiency")
    p.add_argument("--seeds", default="42,43", help="comma-separated world seeds")
    p.add_argument("--arms", default="system_legacy,system,rag", help="comma-separated arms")
    p.add_argument("--scale", default="S", choices=sorted(gen_mod.SCALES))
    p.add_argument("--split", default="dev", choices=("dev", "cert"))
    p.add_argument("--budget", type=int, default=12000, help="context budget tokens")
    p.add_argument("--bars", type=Path, default=None, help="sufficiency-bars.yaml to enforce")
    p.add_argument("--out", type=Path, default=None, help="write full JSON result here")
    args = p.parse_args(argv)

    seeds = [int(s) for s in str(args.seeds).split(",") if s.strip()]
    arms = [a.strip() for a in str(args.arms).split(",") if a.strip()]
    if not seeds or not arms:
        print("refused: need at least one seed and one arm", file=sys.stderr)
        return 2

    result = run_sufficiency(
        seeds, arms, scale=args.scale, split=args.split, budget_tokens=args.budget
    )
    for arm in arms:
        summary = result["arms"][arm]["summary"]
        cells = " ".join(
            f"{axis}={summary[axis]['sufficiency']:.2f}" for axis in sorted(summary)
        )
        print(f"sufficiency {arm}: {cells}")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    if args.bars is not None:
        breaches = check_bars(result, _load_bars(args.bars))
        if breaches:
            for b in breaches:
                print(f"BAR BREACH: {b}", file=sys.stderr)
            return 1
        print("all sufficiency bars met")
    return 0


__all__ = [
    "EVIDENCE_AXES",
    "ItemSufficiency",
    "check_bars",
    "evaluate_arm",
    "evaluate_item",
    "run_sufficiency",
    "summarize",
]

if __name__ == "__main__":
    raise SystemExit(main())
