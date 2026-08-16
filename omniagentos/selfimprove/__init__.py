"""omniagentos.selfimprove — skill capture + CONSTRAINTS growth (Wave 4).

Per the self-improving-loop method: when a run/workflow passes its
verification gate, capture the workflow as a reusable skill and grow a
per-project constraints file from what the fix taught.

Public API:
    capture_skill(metadata, gate, vault_dir, *, skills_dir=None,
        autocommit=None) -> SkillCaptureResult
    capture_skill_from_run_dir(run_dir, metadata, vault_dir, *,
        skills_dir=None, autocommit=None) -> SkillCaptureResult
    append_constraint(project, rule, gate, *, constraints_dir=None,
        source_run_id=None) -> Path
    append_constraint_from_run_dir(run_dir, project, rule, *,
        constraints_dir=None) -> Path
    gate_from_status(data) / gate_from_status_json(run_dir) -> VerificationGate
    curate(*, ledger_dir=None, vault_dir=None, skills_dir=None, limit=200,
        autocommit=None) -> CurateResult

HARD RULE (self-improving-loop method): both `capture_skill` and
`append_constraint` refuse (`UnverifiedCaptureError`) unless the supplied
`VerificationGate.status` is `GateStatus.PASSED`. Never capture unverified
output — it poisons the skill/constraint library every future run is meant
to trust. This is enforced in code (see `skills.py`, `constraints.py`), not
just documented here.

Storage:
    skill notes -> vault/playbook/skill-<id>.md (via omniagentos.vault.write_note)
    optional SKILL.md mirror -> <skills_dir>/<id>/SKILL.md
    constraints -> <constraints_dir>/<project>/CONSTRAINTS.md
        (default constraints_dir: omniagentos.selfimprove.paths.default_constraints_dir())

Wiring (post-Wave-4): `python -m omniagentos.selfimprove.curator` scans the
run ledger (`omniagentos.ledger.read_manifests`) for recently-COMPLETED runs
and calls `capture_skill` for each verified one not yet captured — see
`curator.py` (`curate()` / `CurateResult`) for why this is a standalone
consumer rather than a hook inside
`omniagentos.runner.core.Runner._finalize_body`. A `cli.py` entrypoint is
also provided for manual/scripted use against a single run's `status.json`.

NOTE: `curator.py` is deliberately NOT re-exported from this `__init__.py`
(unlike the rest of the public API below) — it is the module Python re-runs
as `__main__` for `python -m omniagentos.selfimprove.curator`, and importing
it here too would make that invocation import it twice under two different
module identities (`sys` warns "found in sys.modules ... prior to execution
... may result in unpredictable behaviour"). Import it directly:
`from omniagentos.selfimprove.curator import curate, CurateResult`.
"""

from __future__ import annotations

from omniagentos.selfimprove.constraints import append_constraint, append_constraint_from_run_dir
from omniagentos.selfimprove.errors import SelfImproveError, UnverifiedCaptureError
from omniagentos.selfimprove.gate import gate_from_status, gate_from_status_json
from omniagentos.selfimprove.models import (
    ConstraintEntry,
    GateStatus,
    SkillCaptureResult,
    SkillMetadata,
    VerificationGate,
)
from omniagentos.selfimprove.skills import capture_skill, capture_skill_from_run_dir

__all__ = [
    "capture_skill",
    "capture_skill_from_run_dir",
    "append_constraint",
    "append_constraint_from_run_dir",
    "gate_from_status",
    "gate_from_status_json",
    "GateStatus",
    "VerificationGate",
    "SkillMetadata",
    "SkillCaptureResult",
    "ConstraintEntry",
    "SelfImproveError",
    "UnverifiedCaptureError",
]
