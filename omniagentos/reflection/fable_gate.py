"""Fable approval gate for low-risk reflection proposals.

The nightly reflection loop generates ``reflection_proposals`` rows that
historically wait for a human decision in the dashboard. This module lets the
configured ``final_reviewer`` stage (``claude-fable-5`` in
``configs/loop_models.yaml``) decide the LOW-RISK subset automatically, with
three modes (``OMNIAGENTOS_FABLE_GATE_MODE``):

* ``off``    — do nothing.
* ``shadow`` — record Fable's verdicts as artifacts under
  ``var/loop-review/fable-gate/`` plus an improvement-log entry; change no
  proposal row. DEFAULT.
* ``on``     — an ``approve`` verdict applies the proposal through the same
  :func:`omniagentos.reflection.apply.apply_proposal` path the human dashboard
  approval uses; ``reject`` transitions the row to ``rejected``;
  ``needs_human`` rows stay ``pending`` for the dashboard.

Eligibility is deliberately narrow and fail-closed. A proposal is submitted to
the reviewer only when ALL hold:

* ``status == 'pending'`` and ``risk_class == 'low'``;
* ``kind`` is in :data:`GATE_KIND_ALLOWLIST` (single-key config values and
  additive documents — the trivially revertible classes);
* a YAML target names a ``configs/`` file whose stem is not in
  :data:`PROTECTED_CONFIG_STEMS` (the gate must never be able to relax the
  policy/governance floor or edit its own reviewer roster) and whose key does
  not touch a frozen knob (:data:`PROTECTED_KEY_MARKERS` — concurrency is
  frozen by the loop plan's C-1 ruling);
* a document target does not name a governance/house-rules document
  (:data:`PROTECTED_DOC_NAMES`).

Every other row — and any error talking to the reviewer, an unparseable
verdict, or a verdict for an id that was not submitted — degrades to
``needs_human`` and is left untouched. The gate never raises out of
:func:`run_gate`: a broken gate must never break the nightly pipeline.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.improvement_chain import (
    DEFAULT_CONFIG_PATH,
    JsonRunner,
    load_config,
    run_stage_json,
)
from omniagentos.reflection.apply import apply_proposal
from omniagentos.reflection.kinds import resolve_target_path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT_ROOT = ROOT / "var" / "loop-review" / "fable-gate"
PLAN_PATH = ROOT / "devtasks" / "LOOP-IMPROVEMENT-PLAN.md"

GATE_MODE_ENV = "OMNIAGENTOS_FABLE_GATE_MODE"
GateMode = Literal["off", "shadow", "on"]
GATE_MODES: tuple[GateMode, ...] = ("off", "shadow", "on")
DEFAULT_GATE_MODE: GateMode = "shadow"

#: Kinds the gate may decide: single-key config values and additive documents.
#: ``formation``/``router_weight``/``category_pin`` stay human — they change
#: routing topology; ``rollback`` is remediation and always a human call;
#: ``skill`` writes executable guidance and stays human for now.
GATE_KIND_ALLOWLIST: frozenset[str] = frozenset(
    {"model_config", "effort_override", "lesson", "brief_template"}
)

#: configs/<stem>.yaml files the gate must never approve edits to, mirroring
#: the governance Tier-P floor plus this gate's own model roster.
PROTECTED_CONFIG_STEMS: frozenset[str] = frozenset(
    {"policy", "governance", "reliability", "roots", "self_improvement", "loop_models"}
)

#: Key substrings that are frozen by standing rulings (loop plan C-1/C3 froze
#: every concurrency knob); matched case-insensitively on the target key.
PROTECTED_KEY_MARKERS: tuple[str, ...] = (
    "concurrency",
    "target_n",
    "max_sessions",
    "parallel",
)

#: House-rules / governance documents a document-kind proposal may not touch.
PROTECTED_DOC_NAMES: frozenset[str] = frozenset(
    {"AGENTS.md", "CLAUDE.md", "ARCHI.md", "ARCHI.json", "TESTING.md", "DECISIONS.md"}
)

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "verdict", "reason"],
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["approve", "reject", "needs_human"],
                    },
                    "reason": {"type": "string"},
                },
            },
        }
    },
}

VALID_VERDICTS = frozenset({"approve", "reject", "needs_human"})


@dataclass(frozen=True)
class GateResult:
    run_id: str
    mode: str
    considered: int
    eligible: list[str]
    ineligible: dict[str, str]
    verdicts: dict[str, dict[str, str]]
    applied: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    needs_human: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    artifact_dir: str = ""


def resolve_mode(explicit: str | None = None) -> GateMode:
    """Explicit argument > environment > default; anything invalid is ``off``.

    Fail closed: a typo in the mode env must disable the gate, not silently
    run it in some other mode.
    """
    raw = (explicit or os.environ.get(GATE_MODE_ENV) or DEFAULT_GATE_MODE).strip().lower()
    if raw in GATE_MODES:
        return raw  # type: ignore[return-value]
    return "off"


def _eligibility(row: Mapping[str, Any]) -> str | None:
    """None when the row may be submitted to the reviewer, else the reason."""
    if str(row["status"]) != "pending":
        return f"status={row['status']}"
    if str(row["risk_class"]).lower() != "low":
        return f"risk_class={row['risk_class']}"
    kind = str(row["kind"])
    if kind not in GATE_KIND_ALLOWLIST:
        return f"kind={kind} not in gate allowlist"

    try:
        target = json.loads(row["target"])
    except Exception:
        target = row["target"]

    if kind in {"model_config", "effort_override"}:
        if not isinstance(target, Mapping):
            return "yaml target is not a mapping"
        file_raw = str(target.get("file") or "")
        rel = Path(file_raw)
        if rel.is_absolute() or ".." in rel.parts:
            return f"target file escapes the repo: {file_raw}"
        if not rel.parts or rel.parts[0] != "configs":
            return f"target file not under configs/: {file_raw}"
        if rel.stem in PROTECTED_CONFIG_STEMS:
            return f"protected config: {file_raw}"
        key = str(target.get("key") or "").lower()
        for marker in PROTECTED_KEY_MARKERS:
            if marker in key:
                return f"protected key marker {marker!r} in {key!r}"
    else:  # lesson / brief_template
        # The shared resolver, not a local re-read of target["doc"]: this gate
        # decides eligibility for the same write apply.py performs, so it must
        # resolve the path the same way, fallbacks included. A kind that
        # resolves to nothing is unroutable, and unroutable is ineligible —
        # never "no doc named, so nothing to check".
        doc_raw = resolve_target_path(kind, target)
        if not doc_raw:
            return f"{kind} names no document and has no default (unroutable)"
        rel = Path(doc_raw)
        if rel.is_absolute() or ".." in rel.parts:
            return f"target doc escapes the repo: {doc_raw}"
        if rel.name in PROTECTED_DOC_NAMES:
            return f"protected document: {doc_raw}"
    return None


def _plan_guardrails(max_chars: int = 4000) -> str:
    """The loop plan's "Explicitly not doing" section, for the review packet."""
    try:
        text = PLAN_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    marker = "## 9. Explicitly not doing this cycle"
    start = text.find(marker)
    if start < 0:
        return ""
    end = text.find("\n## ", start + len(marker))
    section = text[start:end] if end > 0 else text[start:]
    return section[:max_chars]


def _review_prompt(proposals: list[dict[str, Any]]) -> str:
    guardrails = _plan_guardrails()
    packet = json.dumps(proposals, indent=1, ensure_ascii=False)
    parts = [
        "You are the Fable approval gate for OmniAgentOS's reflection loop. "
        "Each proposal below is a pre-screened LOW-RISK change: a single config "
        "key or an additive document, with a trivial revert. For EACH proposal "
        "return exactly one verdict:",
        "- approve: the rationale is supported by its evidence refs, the change "
        "matches its stated target, and applying it cannot plausibly degrade "
        "safety or routing correctness.",
        "- reject: the proposal is wrong, contradicted by evidence, or its "
        "predicted impact does not follow. Give the concrete reason.",
        "- needs_human: anything uncertain, novel, or with wider blast radius "
        "than its risk_class claims. When in doubt, choose needs_human.",
        "Never approve a change that contradicts a standing owner ruling in the "
        "guardrails section below. You are read-only: return only the "
        "structured verdicts.",
    ]
    if guardrails:
        parts.append("## STANDING GUARDRAILS (from the current loop plan)\n\n" + guardrails)
    parts.append("## PROPOSALS\n\n" + packet)
    return "\n\n".join(parts)


def _load_pending(db_path: str) -> list[dict[str, Any]]:
    store = SqliteStore(db_path)
    with store._lock:
        rows = store._connection.execute(
            "SELECT * FROM reflection_proposals WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
        return [dict(row) for row in rows]


def _reject_pending(db_path: str, proposal_id: str) -> bool:
    """pending -> rejected; False when the row moved under us (left alone)."""
    store = SqliteStore(db_path)
    now = utc_now_iso()
    with store._lock:
        cursor = store._connection.execute(
            "UPDATE reflection_proposals SET status = 'rejected', updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (now, proposal_id),
        )
        return cursor.rowcount == 1


def run_gate(
    db_path: str | None = None,
    *,
    mode: str | None = None,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    artifact_root: str | Path = DEFAULT_ARTIFACT_ROOT,
    json_runner: JsonRunner = run_stage_json,
) -> GateResult:
    """Review pending low-risk proposals; apply/reject only in ``on`` mode."""
    resolved_mode = resolve_mode(mode)
    started = datetime.now(UTC)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    if resolved_mode == "off":
        return GateResult(
            run_id=run_id,
            mode="off",
            considered=0,
            eligible=[],
            ineligible={},
            verdicts={},
        )

    db = db_path or default_db_path()
    errors: list[str] = []
    try:
        pending = _load_pending(db)
    except Exception as exc:  # noqa: BLE001 - a broken gate must not break the nightly
        return GateResult(
            run_id=run_id,
            mode=resolved_mode,
            considered=0,
            eligible=[],
            ineligible={},
            verdicts={},
            errors=[f"load_pending failed: {exc}"],
        )

    eligible_rows: list[dict[str, Any]] = []
    ineligible: dict[str, str] = {}
    for row in pending:
        reason = _eligibility(row)
        if reason is None:
            eligible_rows.append(row)
        else:
            ineligible[str(row["id"])] = reason
    eligible_ids = [str(row["id"]) for row in eligible_rows]

    artifact_dir = Path(artifact_root) / run_id
    verdicts: dict[str, dict[str, str]] = {}
    if eligible_rows:
        packet = [
            {
                "id": row["id"],
                "kind": row["kind"],
                "target": row["target"],
                "current": row["current"],
                "proposed": row["proposed"],
                "rationale": row["rationale"],
                "evidence_refs": row.get("evidence_refs_json", "[]"),
                "predicted_impact": row.get("predicted_impact"),
                "risk_class": row["risk_class"],
            }
            for row in eligible_rows
        ]
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            config = load_config(config_path)
            raw = json_runner(
                config.final_reviewer,
                _review_prompt(packet),
                VERDICT_SCHEMA,
                artifact_dir,
                config.reviewer_wall_ms,
            )
            for item in raw.get("verdicts") or []:
                if not isinstance(item, Mapping):
                    continue
                pid = str(item.get("id") or "")
                verdict = str(item.get("verdict") or "").strip().lower()
                if pid not in eligible_ids or verdict not in VALID_VERDICTS:
                    continue
                verdicts[pid] = {
                    "verdict": verdict,
                    "reason": str(item.get("reason") or "").strip(),
                }
        except Exception as exc:  # noqa: BLE001 - reviewer trouble degrades, never raises
            errors.append(f"reviewer stage failed: {exc}")
    # Fail closed: anything eligible that did not get a valid verdict is
    # needs_human, including the whole batch when the reviewer errored.
    for pid in eligible_ids:
        verdicts.setdefault(pid, {"verdict": "needs_human", "reason": "no valid verdict returned"})

    applied: list[str] = []
    rejected: list[str] = []
    needs_human: list[str] = []
    for pid in eligible_ids:
        verdict = verdicts[pid]["verdict"]
        if verdict == "needs_human" or resolved_mode == "shadow":
            needs_human.append(pid)
            continue
        if verdict == "approve":
            try:
                outcome = apply_proposal(pid, db_path=db)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"apply {pid} raised: {exc}")
                needs_human.append(pid)
                continue
            if outcome and outcome.get("status") == "success":
                applied.append(pid)
            else:
                detail = (outcome or {}).get("error", "apply returned no outcome")
                errors.append(f"apply {pid} failed: {detail}")
                needs_human.append(pid)
        elif verdict == "reject":
            if _reject_pending(db, pid):
                rejected.append(pid)
            else:
                errors.append(f"reject {pid}: row no longer pending; left alone")
                needs_human.append(pid)

    result = GateResult(
        run_id=run_id,
        mode=resolved_mode,
        considered=len(pending),
        eligible=eligible_ids,
        ineligible=ineligible,
        verdicts=verdicts,
        applied=applied,
        rejected=rejected,
        needs_human=needs_human,
        errors=errors,
        artifact_dir=str(artifact_dir) if eligible_rows else "",
    )
    _write_artifacts(result, artifact_dir if eligible_rows else None, started)
    return result


def _write_artifacts(result: GateResult, artifact_dir: Path | None, started: datetime) -> None:
    """Verdict artifact + improvement-log line; best-effort, never raises."""
    try:
        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "verdicts.json").write_text(
                json.dumps(asdict(result), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        # Same root override apply_proposal honours, so tests stay hermetic.
        log_root = Path(os.environ.get("OMNIAGENTOS_HOME") or ROOT)
        log_path = log_root / "var" / "improvement-log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(
                json.dumps(
                    {
                        "ts": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "improver": "fable-gate",
                        "mode": result.mode,
                        "considered": result.considered,
                        "eligible": len(result.eligible),
                        "applied": result.applied,
                        "rejected": result.rejected,
                        "needs_human": result.needs_human,
                        "errors": result.errors,
                        "notes": f"artifact_dir={result.artifact_dir}",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:  # noqa: BLE001 - audit trouble must not fail the gate
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fable approval gate over pending low-risk reflection proposals"
    )
    parser.add_argument("--db", default=None, help="state database path (default: runtime db)")
    parser.add_argument("--mode", default=None, choices=list(GATE_MODES))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    args = parser.parse_args(argv)

    artifact_root = Path(args.artifact_root)
    artifact_root.mkdir(parents=True, exist_ok=True)
    lock_path = artifact_root / ".lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print(json.dumps({"status": "skipped", "reason": "fable gate already running"}))
            return 0
        result = run_gate(
            args.db,
            mode=args.mode,
            config_path=args.config,
            artifact_root=artifact_root,
        )
    print(json.dumps(asdict(result), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_GATE_MODE",
    "GATE_KIND_ALLOWLIST",
    "GATE_MODES",
    "GateMode",
    "GateResult",
    "PROTECTED_CONFIG_STEMS",
    "PROTECTED_DOC_NAMES",
    "PROTECTED_KEY_MARKERS",
    "main",
    "resolve_mode",
    "run_gate",
]
