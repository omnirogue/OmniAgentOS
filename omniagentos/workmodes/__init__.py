"""Non-code work modes: artifact roots, artificer scoping, protocols, promote.

TN.1–TN.5. The half of the system that makes it useful for webinars, ad copy,
images and reports rather than only code. Four layers, bottom-up:

- :mod:`omniagentos.workmodes.modes` (TN.1) — which roots each
  :class:`~omniagentos.contracts.TaskMode` gets, deterministic negation-aware
  mode inference at intake, and splitting a task that declares both repo files
  and artifact outputs into two units with rewired dependencies.
- :mod:`omniagentos.workmodes.artificer` (TN.2) — mode-aware root resolution and
  the guard that fails a non-code task which writes outside its artifact root.
- :mod:`omniagentos.workmodes.protocols` (TN.3) — the per-mode declared-output
  contract (formats, prompt requirement, byte floors, ffprobe duration checks),
  per-platform ad-copy validation profiles, and the rubric + acceptance criteria
  recorded in the manifest.
- :mod:`omniagentos.workmodes.manifest` (TN.3/TN.4) — the manifest itself, its
  ``work_artifacts`` rows, and manifest GRADING with its two subtleties
  (fast-pass, and the fail-override for verdicts whose reasons are all lookup
  failures).
- :mod:`omniagentos.workmodes.promote` (TN.5) — the deterministic, non-agentic
  copy of APPROVED artifacts to their final destinations.

SHIPS DARK. Nothing outside this package imports it (there is a test that
asserts exactly that), the enforcement gate ``OMNIAGENTOS_WORK_MODES`` defaults
to ``off``, and the only functions with a filesystem side effect are the ones a
caller has to name: ``provision_roots``, ``write_manifest`` and ``promote``.

Layering: this package sits ABOVE :mod:`omniagentos.scope` and
:mod:`omniagentos.contracts` and below every lane. It must never import a lane
(``runner``, ``swarm``, ``longhaul``, ``intake`` at module scope) — the one
intake dependency, ``parse_target_location``, is imported lazily inside the
function that needs it.
"""

from omniagentos.workmodes.artificer import (
    ArtifactScopeViolation,
    ArtificerPlan,
    GuardVerdict,
    WorkerShape,
    WriteFinding,
    artificer_prompt,
    enforce_writes,
    guard_writes,
    plan_artificer,
    worker_shape,
)
from omniagentos.workmodes.manifest import (
    ArtifactManifest,
    FailOverride,
    ManifestEntry,
    ManifestGrade,
    WorkArtifactStore,
    apply_fail_override,
    build_manifest,
    file_sha256,
    grade_manifest,
    is_lookup_failure_reason,
    load_manifest,
    write_manifest,
)
from omniagentos.workmodes.modes import (
    MODE_POLICIES,
    ModeDecision,
    ModeInference,
    ModePolicy,
    ModeRoots,
    SplitResult,
    WorkModeError,
    WorkUnit,
    infer_task_mode,
    policy_for,
    provision_roots,
    reconcile_task_mode,
    resolve_roots,
    split_units,
    work_modes_enabled,
    work_modes_enforcing,
    work_modes_mode,
)
from omniagentos.workmodes.promote import (
    PromoteMapping,
    PromotionPlan,
    PromotionResult,
    plan_promotion,
    promote,
    promote_approved,
)
from omniagentos.workmodes.protocols import (
    MODE_RUBRICS,
    PLATFORM_PROFILES,
    PROTOCOLS,
    AcceptanceCriteria,
    AdCopyReport,
    ArtifactProtocol,
    PlatformProfile,
    ProtocolReport,
    RubricCriterion,
    VariantSlot,
    build_acceptance,
    check_files,
    platform_profile,
    protocol_for,
    validate_ad_copy,
)

__all__ = [
    "MODE_POLICIES",
    "MODE_RUBRICS",
    "PLATFORM_PROFILES",
    "PROTOCOLS",
    "AcceptanceCriteria",
    "AdCopyReport",
    "ArtificerPlan",
    "ArtifactManifest",
    "ArtifactProtocol",
    "ArtifactScopeViolation",
    "FailOverride",
    "GuardVerdict",
    "ManifestEntry",
    "ManifestGrade",
    "ModeDecision",
    "ModeInference",
    "ModePolicy",
    "ModeRoots",
    "PlatformProfile",
    "PromoteMapping",
    "PromotionPlan",
    "PromotionResult",
    "ProtocolReport",
    "RubricCriterion",
    "SplitResult",
    "VariantSlot",
    "WorkArtifactStore",
    "WorkModeError",
    "WorkUnit",
    "WorkerShape",
    "WriteFinding",
    "apply_fail_override",
    "artificer_prompt",
    "build_acceptance",
    "build_manifest",
    "check_files",
    "enforce_writes",
    "file_sha256",
    "grade_manifest",
    "guard_writes",
    "infer_task_mode",
    "is_lookup_failure_reason",
    "load_manifest",
    "plan_artificer",
    "plan_promotion",
    "platform_profile",
    "policy_for",
    "promote",
    "promote_approved",
    "protocol_for",
    "provision_roots",
    "reconcile_task_mode",
    "resolve_roots",
    "split_units",
    "validate_ad_copy",
    "work_modes_enabled",
    "work_modes_enforcing",
    "work_modes_mode",
    "worker_shape",
    "write_manifest",
]
