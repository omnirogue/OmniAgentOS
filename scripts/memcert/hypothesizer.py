#!/usr/bin/env python3
"""memcert hypothesizer: the daily hypothesize -> test -> propose loop (DESIGN §8).

Reads the latest dev-split run (summary.json + results.jsonl), picks the weakest
axis cell among arms of interest, and generates ONE hypothesis from a fixed,
ordered, deterministic playbook (no LLM). The hypothesis is PRE-REGISTERED as a
state file (state machine mirrors improve-lane ``configtest_hypotheses``:
proposed -> testing -> confirmed | disproved) BEFORE any A/B test runs, with the
expected delta sign and an ``mde_hint`` from ``grade.mde_hint``.

Testing: the exact ``run_bench.py`` commands for the paired A/B (same seeds,
same trials, split=dev) are emitted (or executed with ``--exec``). Verdicts come
from ``grade.paired_delta`` on the two run dirs, filtered to the target axis:
confirmed requires a significant delta in the registered direction AND no other
axis regressing significantly. The two run dirs are injectable
(``--control-run`` / ``--candidate-run`` or ``run(control_run=..., ...)``) so
tests never need a subprocess.

Filing (proposals only on measured improvement, DESIGN §7/§8):
- DEFAULT: the would-be envelope JSON is written to ``<state-dir>/outbox/<id>.json``.
- LIVE: ``pipeline/bridge/file_proposal.py`` is invoked as a subprocess ONLY
  under TWO keys: the ``--live`` flag AND env ``MEMCERT_LIVE=1``. Never with one.
- Backpressure: filing is skipped (``skipped_backpressure``) when
  ``var/loopqueue/proposals/*.json`` count exceeds ``--max-pending``.
- Dedup: filing is skipped when the hypothesis id already exists in the outbox
  or in ``var/loopqueue/{proposals,rejected,parked}``.

Suite-improvement path: an axis SATURATED (mean >= 0.98 across all arms) emits a
``kind=suite`` hypothesis ("axis X saturated: rotate/harden fixtures") into the
same state machine/outbox — the test improvements that ride the proposal flow.

Exit codes:
    0   ok (including nothing-to-do)
    2   unchanged input (this latest-run was already processed by this state dir)
    70  instrument failure (VOID: error rows > 20%, or zero rows for the target
        axis — never hypothesize on instrument failure)
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shlex
import statistics
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:  # imported as a package member: `scripts.memcert.hypothesizer`
    from . import grade as grade_mod
except ImportError:
    # Run as a SCRIPT: sys.path[0] is this directory and there is no parent
    # package for the relative import to resolve against (grade.py idiom).
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import grade as grade_mod  # type: ignore[no-redef]

EXIT_OK = 0
EXIT_UNCHANGED = 2
EXIT_INSTRUMENT_FAILURE = 70

ERROR_ROW_RATIO_MAX = 0.20
SATURATION_MEAN = 0.98
DEFAULT_MAX_PENDING = 15
MIN_TARGET_PAIRS = 20


class EvidenceVoidError(RuntimeError):
    """A/B evidence too degraded to support ANY verdict (Sol review MC-005)."""

# Floor/control arms are never the arm a hypothesis tries to improve.
CONTROL_ARMS = frozenset({"none", "placebo", "fullhistory", "lessons_shuffled"})

STATE_PROPOSED = "proposed"
STATE_TESTING = "testing"
STATE_CONFIRMED = "confirmed"
STATE_DISPROVED = "disproved"

_LAST_PROCESSED = "last_processed.json"

# The id is a function of WHAT is hypothesized, never WHEN — same idea on a
# later run must dedup to the same id.
_ID_FIELDS = (
    "kind",
    "axis",
    "model",
    "arm_control",
    "arm_candidate",
    "param_change",
    "expected_direction",
)

ARMS_CONFIG_PATH = "configs/memcert/arms.yaml"
SUITE_PATHS = ("scripts/memcert/gen.py", "configs/memcert/bars.yaml")
FALSIFIER = "revert change, delta disappears"

__all__ = [
    "EXIT_INSTRUMENT_FAILURE",
    "EXIT_OK",
    "EXIT_UNCHANGED",
    "HypothesizerResult",
    "build_ab_commands",
    "build_proposal_envelope",
    "build_suite_envelope",
    "evaluate_hypothesis",
    "hypothesis_id",
    "main",
    "pick_weakest_cell",
    "playbook_candidates",
    "run",
    "saturated_axes",
]


# ---------------------------------------------------------------------------
# paths / small helpers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _var_root() -> Path:
    base = os.environ.get("OMNIAGENTOS_VAR_DIR")
    return Path(base) if base else _repo_root() / "var"


def default_state_dir() -> Path:
    return _var_root() / "memcert" / "hypotheses"


def default_loopqueue_root() -> Path:
    return _var_root() / "loopqueue"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    print(f"memcert-hypothesizer: {msg}", file=sys.stderr)


def _write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _content_id(payload: dict[str, Any]) -> str:
    """Envelope id: RFC 8785 hash via pipeline/bridge/canonical.py when present.

    Falls back to a sorted-keys sha256 (prefix-compatible) when the bridge module
    is unavailable — the fallback is only ever used for OUTBOX drafts; a live
    filing goes through file_proposal.py, which recomputes and enforces the id.
    """
    mod = sys.modules.get("memcert_envelope_canonical")
    if mod is None:
        path = _repo_root() / "pipeline" / "bridge" / "canonical.py"
        try:
            spec = importlib.util.spec_from_file_location("memcert_envelope_canonical", path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules["memcert_envelope_canonical"] = mod
                spec.loader.exec_module(mod)
        except Exception:  # noqa: BLE001 - fallback below is deliberate
            mod = None
    if mod is not None and hasattr(mod, "content_id"):
        try:
            return str(mod.content_id(payload))
        except Exception:  # noqa: BLE001 - non-canonicalisable payloads fall back
            pass
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# run loading + instrument health
# ---------------------------------------------------------------------------


def load_run(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load a run_bench out dir: (summary.json dict, results.jsonl rows)."""
    run_dir = Path(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    return summary, _load_rows(run_dir)


def _load_rows(run_dir: Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / "results.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _run_identity(summary: dict[str, Any], run_dir: Path) -> str:
    manifest = summary.get("manifest") or {}
    return str(manifest.get("run_uuid") or summary.get("run_id") or Path(run_dir).name)


def instrument_failure_reason(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str | None:
    """Non-None when the latest run must not be hypothesized on (exit 70)."""
    if not rows:
        return "no result rows in latest run"
    n_err = sum(1 for r in rows if r.get("error"))
    ratio = n_err / len(rows)
    if ratio > ERROR_ROW_RATIO_MAX:
        return f"error rows {n_err}/{len(rows)} ({ratio:.0%}) exceed {ERROR_ROW_RATIO_MAX:.0%}"
    cells = summary.get("axes") or {}
    if not cells:
        return "summary has no axis cells"
    axes_seen = {str(e.get("axis")) for e in cells.values()}
    manifest = summary.get("manifest") or {}
    for axis in manifest.get("axes") or []:
        if str(axis) not in axes_seen:
            return f"zero graded rows for axis {axis}"
    return None


# ---------------------------------------------------------------------------
# hypothesis generation (deterministic playbook, no LLM)
# ---------------------------------------------------------------------------


def saturated_axes(cells: dict[str, dict[str, Any]]) -> list[str]:
    """Axes whose mean is >= SATURATION_MEAN across ALL arms (and models)."""
    by_axis: dict[str, list[float]] = {}
    for entry in cells.values():
        if entry.get("mean") is None:
            continue
        by_axis.setdefault(str(entry["axis"]), []).append(float(entry["mean"]))
    return sorted(a for a, means in by_axis.items() if means and min(means) >= SATURATION_MEAN)


def pick_weakest_cell(
    cells: dict[str, dict[str, Any]], exclude_axes: tuple[str, ...] | list[str] = ()
) -> dict[str, Any] | None:
    """Lowest-mean (axis, arm, model) cell among arms of interest, deterministic.

    Control/floor arms (CONTROL_ARMS) are excluded unless nothing else exists.
    Ties break on the sorted (axis, arm, model) key so the pick never depends on
    dict ordering.
    """
    excluded = {str(a) for a in exclude_axes}
    pool = [
        e
        for e in cells.values()
        if e.get("mean") is not None and str(e.get("axis")) not in excluded
    ]
    interest = [e for e in pool if str(e.get("arm")) not in CONTROL_ARMS]
    pool = interest or pool
    if not pool:
        return None
    return min(
        pool,
        key=lambda e: (float(e["mean"]), str(e["axis"]), str(e["arm"]), str(e["model"])),
    )


def hypothesis_id(hyp: dict[str, Any]) -> str:
    """sha16 of the hypothesis payload (the WHAT, never the WHEN)."""
    basis = {k: hyp.get(k) for k in _ID_FIELDS}
    blob = json.dumps(basis, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def playbook_candidates(cell: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Fixed ORDERED playbook for the weakest cell; first un-registered wins.

    1. raise budget_tokens for the arm (config-level, cheapest to test);
    2. switch arm transcript<->rag (or ->rag) for the axis;
    3. enable the composed ``system`` arm variant.
    """
    arm = str(cell["arm"])
    axis = str(cell["axis"])
    model = str(cell["model"])
    budget = int(manifest.get("budget_tokens") or 24000)
    base = {
        "kind": "perf",
        "axis": axis,
        "model": model,
        "arm_control": arm,
        "expected_direction": "+",
    }
    out: list[dict[str, Any]] = [
        {**base, "arm_candidate": None, "param_change": {"budget_tokens": budget * 2}}
    ]
    swap = {"transcript": "rag", "rag": "transcript"}.get(arm, "rag")
    if swap != arm:
        out.append({**base, "arm_candidate": swap, "param_change": None})
    if arm != "system":
        out.append({**base, "arm_candidate": "system", "param_change": None})
    for hyp in out:
        hyp["id"] = hypothesis_id(hyp)
    return out


def _cell_mde_hint(rows: list[dict[str, Any]], cell: dict[str, Any]) -> float | None:
    scores = [
        float(r["score"])
        for r in rows
        if r.get("error") is None
        and r.get("score") is not None
        and str(r.get("axis")) == str(cell["axis"])
        and str(r.get("arm")) == str(cell["arm"])
        and str(r.get("model")) == str(cell["model"])
    ]
    sd = statistics.pstdev(scores) if len(scores) > 1 else 0.0
    n_items = int(cell.get("n_items") or 0) or len(scores)
    return grade_mod.mde_hint(n_items, sd)


# ---------------------------------------------------------------------------
# A/B commands + evaluation
# ---------------------------------------------------------------------------


def build_ab_commands(
    hyp: dict[str, Any], manifest: dict[str, Any], out_base: Path, runner_args: str = ""
) -> dict[str, list[str]]:
    """The exact run_bench commands for the paired A/B (same seeds/trials/dev).

    ALL manifest axes are run, not just the target — the no-other-axis-regresses
    check needs paired rows on every axis.
    """
    axes = [str(a) for a in (manifest.get("axes") or [])]
    seeds = [str(s) for s in (manifest.get("seeds") or [])]
    trials = int(manifest.get("trials") or 3)
    adapter = str(manifest.get("adapter") or "mock")
    budget = int(manifest.get("budget_tokens") or 24000)
    script = str(_repo_root() / "scripts" / "memcert" / "run_bench.py")
    extra = shlex.split(runner_args) if runner_args else []

    def cmd(arm: str, name: str, budget_tokens: int) -> list[str]:
        return [
            sys.executable,
            script,
            "--models",
            str(hyp["model"]),
            "--arms",
            arm,
            "--axes",
            ",".join(axes),
            "--trials",
            str(trials),
            "--seeds",
            ",".join(seeds),
            "--split",
            "dev",
            "--adapter",
            adapter,
            "--budget-tokens",
            str(budget_tokens),
            "--out",
            str(Path(out_base) / name),
            *extra,
        ]

    cand_arm = str(hyp.get("arm_candidate") or hyp["arm_control"])
    cand_budget = budget
    param_change = hyp.get("param_change") or {}
    if "budget_tokens" in param_change:
        cand_budget = int(param_change["budget_tokens"])
    return {
        "control": cmd(str(hyp["arm_control"]), "control", budget),
        "candidate": cmd(cand_arm, "candidate", cand_budget),
    }


def evaluate_hypothesis(
    hyp: dict[str, Any],
    control_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    boot_seed: int = 1,
) -> dict[str, Any]:
    """grade.paired_delta verdict for a pre-registered hypothesis.

    confirmed = target-axis delta significant AND in the registered direction
    AND no other axis regresses significantly. Error/unscored rows are never
    averaged in (instrument errors are not candidate defects).
    """
    model = str(hyp["model"])
    cand_arm = str(hyp.get("arm_candidate") or hyp["arm_control"])

    def usable(rows: list[dict[str, Any]], arm: str) -> list[dict[str, Any]]:
        return [
            r
            for r in rows
            if r.get("error") is None
            and r.get("score") is not None
            and str(r.get("model")) == model
            and str(r.get("arm")) == arm
        ]

    def by_axis(rows: list[dict[str, Any]], axis: str) -> list[dict[str, Any]]:
        return [r for r in rows if str(r.get("axis")) == axis]

    # Sol review MC-005: evidence-quality gates BEFORE any verdict. An A/B
    # where either side lost >20% of its calls to errors, or that yields
    # fewer than 20 matched pairs on the target axis, is VOID evidence — it
    # can neither confirm nor disprove (raise, caller maps to exit 70).
    def _error_rate(rows: list[dict[str, Any]], arm: str) -> float:
        scoped = [r for r in rows if str(r.get("model")) == model and str(r.get("arm")) == arm]
        if not scoped:
            return 1.0
        return sum(1 for r in scoped if r.get("error") is not None) / len(scoped)

    ctrl_err = _error_rate(control_rows, str(hyp["arm_control"]))
    cand_err = _error_rate(candidate_rows, cand_arm)
    if ctrl_err > 0.20 or cand_err > 0.20:
        raise EvidenceVoidError(
            f"A/B evidence void: error rates control={ctrl_err:.0%} candidate={cand_err:.0%} "
            "(>20% on either side)"
        )

    ctrl = usable(control_rows, str(hyp["arm_control"]))
    cand = usable(candidate_rows, cand_arm)
    axis = str(hyp["axis"])
    target = grade_mod.paired_delta(by_axis(ctrl, axis), by_axis(cand, axis), boot_seed=boot_seed)
    expected = str(hyp.get("expected_direction") or "+")
    direction_ok = target["delta"] is not None and (
        target["delta"] > 0 if expected == "+" else target["delta"] < 0
    )

    regressions: list[dict[str, Any]] = []
    shared_axes = {str(r.get("axis")) for r in ctrl} & {str(r.get("axis")) for r in cand}
    for other in sorted(shared_axes - {axis}):
        pd = grade_mod.paired_delta(by_axis(ctrl, other), by_axis(cand, other), boot_seed=boot_seed)
        if pd["significant"] and (pd["delta"] or 0.0) < 0:
            regressions.append({"axis": other, **pd})

    def mean(rows: list[dict[str, Any]]) -> float | None:
        return round(sum(float(r["score"]) for r in rows) / len(rows), 4) if rows else None

    target_ctrl = by_axis(ctrl, axis)
    target_cand = by_axis(cand, axis)
    # Grok review SHOULD-FIX-7: a daily loop testing at CI-only significance
    # accumulates ~5% false positives on pure noise. Confirmation requires the
    # bootstrap CI to exclude zero AND the exact McNemar p <= 0.01 — a joint
    # rule sized for one hypothesis per day over months of operation.
    mcnemar_p = target.get("mcnemar_p")
    mcnemar_ok = mcnemar_p is not None and float(mcnemar_p) <= 0.01
    return {
        "target": target,
        "direction_ok": bool(direction_ok),
        "regressions": regressions,
        "mcnemar_ok": mcnemar_ok,
        "confirmed": bool(
            target["significant"] and mcnemar_ok and direction_ok and not regressions
        ),
        "before_mean": mean(target_ctrl),
        "after_mean": mean(target_cand),
        "n_control_rows": len(target_ctrl),
        "n_candidate_rows": len(target_cand),
    }


# ---------------------------------------------------------------------------
# envelopes + filing
# ---------------------------------------------------------------------------


def _describe_change(hyp: dict[str, Any]) -> str:
    if hyp.get("arm_candidate"):
        return f"switch arm {hyp['arm_control']} -> {hyp['arm_candidate']}"
    return f"set {json.dumps(hyp.get('param_change') or {}, sort_keys=True)} for arm {hyp['arm_control']}"


def build_proposal_envelope(
    hyp: dict[str, Any],
    evaluation: dict[str, Any],
    receipts: list[str],
    run_ident: str,
) -> dict[str, Any]:
    """Envelope for a CONFIRMED perf hypothesis (pipeline/schema/envelope.schema.json)."""
    change = _describe_change(hyp)
    target = evaluation["target"]
    payload: dict[str, Any] = {
        "hypothesis_id": hyp["id"],
        "axis": hyp["axis"],
        "model": hyp["model"],
        "problem": (
            f"memcert dev-split axis {hyp['axis']} is the weakest cell for arm "
            f"{hyp['arm_control']} on {hyp['model']} (registered at run {run_ident})."
        ),
        "implementation_plan": (
            f"Apply the measured winner in {ARMS_CONFIG_PATH}: {change} for axis "
            f"{hyp['axis']}. Verified by paired A/B on the dev split (same seeds/trials)."
        ),
        "measured": {
            "before_mean": evaluation["before_mean"],
            "after_mean": evaluation["after_mean"],
            "delta": target["delta"],
            "ci": [target["ci_lo"], target["ci_hi"]],
            "p": target["mcnemar_p"],
            "n_pairs": target["n_pairs"],
        },
        "receipts": [str(p) for p in receipts],
        "falsifier": FALSIFIER,
        "registered_at_run": run_ident,
    }
    return {
        "contract": "v1.1",
        "id": _content_id(payload),
        "kind": "proposal",
        "title": f"memcert: {change} improves axis {hyp['axis']} (measured)"[:200],
        "created_at": _now_iso(),
        "producer": {"role": "external", "actor": "memcert-hypothesizer"},
        "paths": [ARMS_CONFIG_PATH],
        "payload": payload,
    }


def build_suite_envelope(axis: str, run_ident: str, hyp_id: str) -> dict[str, Any]:
    """Envelope for a suite-improvement hypothesis (saturated axis)."""
    payload: dict[str, Any] = {
        "hypothesis_id": hyp_id,
        "axis": axis,
        "problem": (
            f"memcert axis {axis} saturated: mean >= {SATURATION_MEAN} across all arms — "
            "the fixtures no longer separate arms, so 100% is not meaningful here."
        ),
        "implementation_plan": (
            f"Rotate/harden axis {axis} fixtures in scripts/memcert/gen.py (harder "
            "L-levels, fresh distractors) and revisit the axis bar in "
            "configs/memcert/bars.yaml (ratchet-only)."
        ),
        "falsifier": "regenerated fixtures restore sub-saturation spread between arms",
        "registered_at_run": run_ident,
    }
    return {
        "contract": "v1.1",
        "id": _content_id(payload),
        "kind": "proposal",
        "title": f"memcert suite: axis {axis} saturated - rotate/harden fixtures"[:200],
        "created_at": _now_iso(),
        "producer": {"role": "external", "actor": "memcert-hypothesizer"},
        "paths": list(SUITE_PATHS),
        "payload": payload,
    }


def pending_proposal_count(loopqueue_root: Path) -> int:
    return len(list((Path(loopqueue_root) / "proposals").glob("*.json")))


def dedup_hit(hyp_id: str, outbox_dir: Path, loopqueue_root: Path) -> bool:
    """True when this hypothesis id is already in the outbox or the loopqueue."""
    if (Path(outbox_dir) / f"{hyp_id}.json").exists():
        return True
    for area in ("proposals", "rejected", "parked"):
        d = Path(loopqueue_root) / area
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            if hyp_id in p.name:
                return True
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and (data.get("payload") or {}).get("hypothesis_id") == hyp_id:
                return True
    return False


def file_envelope(
    envelope: dict[str, Any],
    hyp_id: str,
    *,
    live: bool,
    env: Mapping[str, str],
    state_dir: Path,
    outbox_dir: Path,
    loopqueue_root: Path,
    max_pending: int,
) -> str:
    """File a proposal envelope; returns the action taken.

    TWO-KEY ARMING: file_proposal.py is invoked ONLY when ``live`` is set AND
    ``env['MEMCERT_LIVE'] == '1'`` — never on one key alone. The default is the
    outbox write (dry). Backpressure and dedup are checked before either path.
    """
    if dedup_hit(hyp_id, outbox_dir, loopqueue_root):
        _log(f"skipped_dedup: hypothesis {hyp_id} already outboxed/filed")
        return "skipped_dedup"
    pending = pending_proposal_count(loopqueue_root)
    if pending > max_pending:
        _log(f"skipped_backpressure: {pending} pending proposals > max {max_pending}")
        return "skipped_backpressure"
    armed = bool(live) and env.get("MEMCERT_LIVE") == "1"
    if armed:
        draft = Path(state_dir) / "drafts" / f"{hyp_id}.draft.json"
        _write_json(draft, envelope)
        tool = _repo_root() / "pipeline" / "bridge" / "file_proposal.py"
        proc = subprocess.run(  # noqa: S603 - fixed local tool, two-key armed
            [sys.executable, str(tool), str(draft)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            _log(f"filed_live: hypothesis {hyp_id} via file_proposal.py")
            return "filed_live"
        _log(
            f"file_proposal refused rc={proc.returncode} for {hyp_id}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
        )
        return f"file_proposal_refused_rc{proc.returncode}"
    outbox = Path(outbox_dir) / f"{hyp_id}.json"
    _write_json(outbox, envelope)
    _log(f"outbox: wrote would-be envelope {outbox}")
    return "outbox"


# ---------------------------------------------------------------------------
# state machine
# ---------------------------------------------------------------------------


def _state_path(state_dir: Path, hyp_id: str) -> Path:
    return Path(state_dir) / f"{hyp_id}.json"


def _update_state(path: Path, **fields: Any) -> dict[str, Any]:
    record = json.loads(path.read_text(encoding="utf-8"))
    if "state" in fields and fields["state"] != record.get("state"):
        record.setdefault("transitions", []).append(
            {"from": record.get("state"), "to": fields["state"], "at": _now_iso()}
        )
    record.update(fields)
    _write_json(path, record)
    return record


def _find_pending(state_dir: Path, run_ident: str) -> tuple[dict[str, Any], Path] | None:
    state_dir = Path(state_dir)
    if not state_dir.is_dir():
        return None
    for path in sorted(state_dir.glob("*.json")):
        if path.name == _LAST_PROCESSED:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            record.get("kind") == "perf"
            and record.get("registered_at_run") == run_ident
            and record.get("state") in (STATE_PROPOSED, STATE_TESTING)
        ):
            return record, path
    return None


def _already_processed(state_dir: Path, run_ident: str) -> bool:
    path = Path(state_dir) / _LAST_PROCESSED
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("run") == run_ident


def _mark_processed(state_dir: Path, run_ident: str) -> None:
    _write_json(Path(state_dir) / _LAST_PROCESSED, {"run": run_ident, "at": _now_iso()})


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


@dataclass
class HypothesizerResult:
    exit_code: int
    action: str
    hypothesis_id: str | None = None
    state_path: Path | None = None
    details: dict[str, Any] = field(default_factory=dict)


def run(
    *,
    latest_run: Path,
    baseline_run: Path | None = None,
    state_dir: Path | None = None,
    live: bool = False,
    max_pending: int = DEFAULT_MAX_PENDING,
    runner_args: str = "",
    dry_run_cmd: bool = False,
    exec_ab: bool = False,
    control_run: Path | None = None,
    candidate_run: Path | None = None,
    loopqueue_root: Path | None = None,
    boot_seed: int = 1,
    min_pairs: int = MIN_TARGET_PAIRS,
    env: Mapping[str, str] | None = None,
) -> HypothesizerResult:
    env_map: Mapping[str, str] = os.environ if env is None else env
    state_dir = Path(state_dir) if state_dir else default_state_dir()
    loopqueue_root = Path(loopqueue_root) if loopqueue_root else default_loopqueue_root()
    outbox_dir = state_dir / "outbox"

    latest_run = Path(latest_run)
    summary, rows = load_run(latest_run)
    manifest = summary.get("manifest") or {}
    run_ident = _run_identity(summary, latest_run)

    # 1. Instrument health guard — never hypothesize on instrument failure.
    reason = instrument_failure_reason(summary, rows)
    if reason:
        _log(f"VOID: instrument failure - {reason}")
        return HypothesizerResult(
            EXIT_INSTRUMENT_FAILURE, "instrument_failure", details={"reason": reason}
        )

    # 2. Unchanged-input refusal. Injected A/B run dirs ARE new input (they are
    #    the test results for the pending hypothesis), and --exec is a changed
    #    ACTION (run the pending A/B, not re-hypothesize) — both bypass this.
    has_ab_runs = control_run is not None and candidate_run is not None
    if _already_processed(state_dir, run_ident) and not has_ab_runs and not exec_ab:
        _log(f"unchanged input: run {run_ident} already processed by {state_dir}")
        return HypothesizerResult(EXIT_UNCHANGED, "unchanged_input", details={"run": run_ident})

    state_dir.mkdir(parents=True, exist_ok=True)
    cells: dict[str, dict[str, Any]] = summary.get("axes") or {}
    actions: list[str] = []

    # 3. Suite-improvement path: saturated axes emit kind=suite hypotheses into
    #    the same state machine/outbox (test improvements ride the proposal flow).
    sat = saturated_axes(cells)
    for axis in sat:
        s_hyp: dict[str, Any] = {
            "kind": "suite",
            "axis": axis,
            "model": None,
            "arm_control": None,
            "arm_candidate": None,
            "param_change": None,
            "expected_direction": None,
        }
        s_hyp["id"] = hypothesis_id(s_hyp)
        spath = _state_path(state_dir, s_hyp["id"])
        if not spath.exists():
            _write_json(
                spath,
                {
                    **s_hyp,
                    "state": STATE_PROPOSED,
                    "registered_at_run": run_ident,
                    "created_at": _now_iso(),
                    "note": f"axis {axis} saturated: rotate/harden fixtures",
                },
            )
        envelope = build_suite_envelope(axis, run_ident, s_hyp["id"])
        filing = file_envelope(
            envelope,
            s_hyp["id"],
            live=live,
            env=env_map,
            state_dir=state_dir,
            outbox_dir=outbox_dir,
            loopqueue_root=loopqueue_root,
            max_pending=max_pending,
        )
        _update_state(spath, filing=filing)
        actions.append(f"suite:{axis}:{filing}")

    # 4. ONE perf hypothesis: resume the pending one for this run, or generate
    #    the first un-registered playbook candidate for the weakest cell.
    record: dict[str, Any] | None = None
    state_path: Path | None = None
    pending_hyp = _find_pending(state_dir, run_ident)
    if pending_hyp is not None:
        record, state_path = pending_hyp
    else:
        cell = pick_weakest_cell(cells, exclude_axes=tuple(sat))
        if cell is not None:
            for cand in playbook_candidates(cell, manifest):
                spath = _state_path(state_dir, cand["id"])
                if spath.exists():
                    continue  # dedup: same hypothesis already registered
                commands = build_ab_commands(
                    cand, manifest, state_dir / "ab" / cand["id"], runner_args
                )
                record = {
                    **cand,
                    "state": STATE_PROPOSED,
                    "registered_at_run": run_ident,
                    "created_at": _now_iso(),
                    "mde_hint": _cell_mde_hint(rows, cell),
                    "commands": commands,
                    "weakest_cell": {
                        k: cell.get(k) for k in ("axis", "arm", "model", "mean", "n_items")
                    },
                }
                if baseline_run is not None:
                    record["baseline_delta"] = _baseline_delta(
                        rows, baseline_run, cell, boot_seed
                    )
                _write_json(spath, record)  # PRE-REGISTERED before any test run
                state_path = spath
                actions.append(f"registered:{cand['id']}")
                break

    if record is None or state_path is None:
        _mark_processed(state_dir, run_ident)
        _log("nothing to do: no un-registered hypothesis for the weakest cell")
        return HypothesizerResult(EXIT_OK, "nothing_to_do", details={"actions": actions})

    if dry_run_cmd:
        for name in ("control", "candidate"):
            print(shlex.join(record["commands"][name]))
        _mark_processed(state_dir, run_ident)
        return HypothesizerResult(
            EXIT_OK, "dry_run_cmd", record["id"], state_path, {"actions": actions}
        )

    # 5. Test mode: run the A/B (only with --exec) or use injected run dirs.
    if exec_ab and not has_ab_runs:
        ab_base = state_dir / "ab" / record["id"]
        rcs: dict[str, int] = {}
        for name in ("control", "candidate"):
            proc = subprocess.run(record["commands"][name], check=False)  # noqa: S603
            rcs[name] = proc.returncode
        _update_state(state_path, ab_exit_codes=rcs)
        control_run = ab_base / "control"
        candidate_run = ab_base / "candidate"
        has_ab_runs = True

    if has_ab_runs:
        assert control_run is not None and candidate_run is not None
        record = _update_state(state_path, state=STATE_TESTING)
        try:
            evaluation = evaluate_hypothesis(
                record, _load_rows(control_run), _load_rows(candidate_run), boot_seed=boot_seed
            )
        except EvidenceVoidError as exc:
            _log(f"VOID: {exc}")
            return HypothesizerResult(
                EXIT_INSTRUMENT_FAILURE,
                "ab_instrument_failure",
                record["id"],
                state_path,
                {"error": str(exc)},
            )
        # Sol review MC-005: below min_pairs matched pairs the paired test is
        # underpowered noise — VOID, never a verdict either way. Production
        # default is MIN_TARGET_PAIRS (20); injectable for smaller test doubles.
        if (evaluation["target"]["n_pairs"] or 0) < min_pairs:
            _log(
                f"VOID: only {evaluation['target']['n_pairs']} paired rows for the target "
                f"axis (minimum {min_pairs})"
            )
            return HypothesizerResult(
                EXIT_INSTRUMENT_FAILURE,
                "ab_instrument_failure",
                record["id"],
                state_path,
                {"evaluation": evaluation},
            )
        new_state = STATE_CONFIRMED if evaluation["confirmed"] else STATE_DISPROVED
        record = _update_state(state_path, state=new_state, evaluation=evaluation)
        actions.append(new_state)
        if evaluation["confirmed"]:
            receipts = [
                str(Path(control_run) / "summary.json"),
                str(Path(candidate_run) / "summary.json"),
                str(Path(control_run) / "results.jsonl"),
                str(Path(candidate_run) / "results.jsonl"),
            ]
            envelope = build_proposal_envelope(record, evaluation, receipts, run_ident)
            filing = file_envelope(
                envelope,
                record["id"],
                live=live,
                env=env_map,
                state_dir=state_dir,
                outbox_dir=outbox_dir,
                loopqueue_root=loopqueue_root,
                max_pending=max_pending,
            )
            _update_state(state_path, filing=filing)
            actions.append(f"filing:{filing}")
        _mark_processed(state_dir, run_ident)
        return HypothesizerResult(
            EXIT_OK, new_state, record["id"], state_path,
            {"actions": actions, "evaluation": evaluation},
        )

    # 6. Registered only: emit the exact A/B commands for the cadence to run.
    for name in ("control", "candidate"):
        print(shlex.join(record["commands"][name]))
    _mark_processed(state_dir, run_ident)
    return HypothesizerResult(EXIT_OK, "registered", record["id"], state_path, {"actions": actions})


def _baseline_delta(
    latest_rows: list[dict[str, Any]],
    baseline_run: Path,
    cell: dict[str, Any],
    boot_seed: int,
) -> dict[str, Any]:
    """Paired delta latest-vs-baseline for the weakest cell (context, not a verdict)."""
    base_rows = _load_rows(Path(baseline_run))

    def filt(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            r
            for r in rows
            if r.get("error") is None
            and r.get("score") is not None
            and str(r.get("axis")) == str(cell["axis"])
            and str(r.get("arm")) == str(cell["arm"])
            and str(r.get("model")) == str(cell["model"])
        ]

    return grade_mod.paired_delta(filt(base_rows), filt(latest_rows), boot_seed=boot_seed)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="memcert-hypothesizer",
        description="Daily memcert hypothesize -> test -> propose loop (DESIGN §8).",
    )
    ap.add_argument("--latest-run", required=True, type=Path, help="latest dev-split run dir")
    ap.add_argument("--baseline-run", type=Path, default=None, help="optional prior run dir")
    ap.add_argument(
        "--state-dir", type=Path, default=None, help="default: var/memcert/hypotheses"
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help="one of TWO keys for live filing (the other is env MEMCERT_LIVE=1)",
    )
    ap.add_argument("--max-pending", type=int, default=DEFAULT_MAX_PENDING)
    ap.add_argument("--runner-args", default="", help="extra args appended to run_bench commands")
    ap.add_argument(
        "--dry-run-cmd", action="store_true", help="print the A/B commands and stop"
    )
    ap.add_argument(
        "--exec", dest="exec_ab", action="store_true", help="run the A/B commands via subprocess"
    )
    ap.add_argument("--control-run", type=Path, default=None, help="completed control run dir")
    ap.add_argument("--candidate-run", type=Path, default=None, help="completed candidate run dir")
    ap.add_argument("--loopqueue-root", type=Path, default=None, help="default: var/loopqueue")
    ap.add_argument("--boot-seed", type=int, default=1)
    ap.add_argument(
        "--min-pairs",
        type=int,
        default=MIN_TARGET_PAIRS,
        help="minimum matched pairs on the target axis before a verdict (default 20)",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    result = run(
        latest_run=args.latest_run,
        baseline_run=args.baseline_run,
        state_dir=args.state_dir,
        live=args.live,
        max_pending=args.max_pending,
        runner_args=args.runner_args,
        dry_run_cmd=args.dry_run_cmd,
        exec_ab=args.exec_ab,
        control_run=args.control_run,
        candidate_run=args.candidate_run,
        loopqueue_root=args.loopqueue_root,
        boot_seed=args.boot_seed,
        min_pairs=args.min_pairs,
    )
    suffix = f" hypothesis={result.hypothesis_id}" if result.hypothesis_id else ""
    _log(f"action={result.action} exit={result.exit_code}{suffix}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
