"""AST registry for filesystem path containment and identity decisions.

This is intentionally a structural gate, not a text search.  It enumerates
decision shapes from the package on every run, accepts direct use of the shared
inode primitive, and requires a written reason for every non-security lexical
path operation.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
PACKAGE_ROOT: Final = REPO_ROOT / "omniagentos"

_PATH_NAME_PARTS: Final = frozenset(
    {
        "candidate",
        "child",
        "destination",
        "dir",
        "directory",
        "file",
        "filename",
        "folder",
        "gitdir",
        "home",
        "parent",
        "path",
        "repo",
        "root",
        "source",
        "target",
        "vault",
        "workdir",
        "workspace",
        "worktree",
        "wt",  # local abbreviation for worktree Path objects in csi/*
    }
)
# Last-segment tokens that almost always name a filesystem location.
_STRONG_PATH_SUFFIXES: Final = frozenset(
    {
        "dir",
        "directory",
        "filename",
        "folder",
        "gitdir",
        "path",
        "repo",
        "root",
        "vault",
        "workdir",
        "workspace",
        "worktree",
        "wt",
    }
)
# Structural graph tokens that collide with path vocabulary (union-find, trees).
# Only the bare locals ``parent`` / ``child`` count — not ``_parent`` / ``tree_parent``.
_AMBIGUOUS_STRUCTURAL_NAMES: Final = frozenset({"parent", "child"})
# pathlib attributes that yield another path object (exact attr spelling only).
_PATH_BEARING_ATTRS: Final = frozenset({"parent"})
_PATH_FACTORIES: Final = frozenset(
    {
        "Path",
        "PurePath",
        "PurePosixPath",
        "PureWindowsPath",
        "abspath",
        "expanduser",
        "normcase",
        "realpath",
        "resolve",
    }
)
_PATH_TERMINALS: Final = frozenset(
    {
        "drive",
        "name",
        "parts",
        "stem",
        "suffix",
        "suffixes",
    }
)


@dataclass(frozen=True)
class PathDecisionSite:
    relative_file: str
    scope: str
    lineno: int
    col_offset: int
    rule: str
    source: str
    is_inode_backed: bool = False

    @property
    def key(self) -> str:
        digest = hashlib.sha256(self.source.encode("utf-8")).hexdigest()[:12]
        return f"{self.relative_file}::{self.scope}::{self.rule}::{digest}"

    def render(self) -> str:
        verdict = "inode-backed" if self.is_inode_backed else "lexical/string"
        return (
            f"{self.relative_file}:{self.lineno}:{self.col_offset + 1} "
            f"[{self.rule}; {verdict}] {self.source!r} key={self.key!r}"
        )


@dataclass(frozen=True)
class PathDecisionAudit:
    safe_sites: tuple[PathDecisionSite, ...]
    excluded_sites: tuple[PathDecisionSite, ...]
    unregistered: tuple[PathDecisionSite, ...]
    stale_exclusions: tuple[str, ...]


@dataclass(frozen=True)
class KnownDeadEntry:
    key: str
    reason: str


def _reasoned(reason: str, *keys: str) -> dict[str, str]:
    """Attach one written review reason to each explicitly named source site."""
    if not reason.strip():
        raise ValueError("path-decision exclusion reason must not be empty")
    return {key: reason for key in keys}


# Non-security lexical operations discovered by the AST walker.  Every key is
# source-bound; changing the expression makes the exclusion stale and forces a
# fresh review.  Grouping only shares prose: every excluded site remains named.
PATH_DECISION_EXCLUSIONS: dict[str, str] = {
    **_reasoned(
        "MIME, URL, shell-token, or regular-expression prefix parsing; no operand names a "
        "filesystem object and the result grants no filesystem access.",
        "api/routes/artifacts_preview.py::preview_artifact::string-prefix::51d6c5df1308",
        "api/routes/artifacts_preview.py::_should_inert_content_type::string-prefix::51d6c5df1308",
        "sessions/notify.py::_push_slack::string-prefix::e42efbba63f5",
        "connectors/jira_client.py::JiraClient._url::string-prefix::9e70c5923229",
        "connectors/jira_client.py::JiraClient._url::string-prefix::d4b66b5c7b3d",
        "connectors/jira_client.py::JiraClient._url::string-prefix::2bc9049baa42",
        "connectors/jira_client.py::JiraClient._url::string-prefix::b2c0263c4c42",
        "connectors/jira_client.py::JiraClient._url::string-prefix::cc525b5a8c49",
        "policy/shell.py::_line_continuation_end::string-prefix::45518070d470",
        "policy/shell.py::_line_continuation_end::string-prefix::e902ee479dfe",
        "policy/shell.py::_curl_get_class::string-prefix::e948ab234939",
        "toolplane/scrub.py::scrub_text::string-prefix::d415708620e0",
        "toolplane/scrub.py::scrub_text::string-prefix::2bceaebb4714",
        "voice/xai.py::_extract_audio::string-prefix::390e9d45777a",
        "api/routes/employee_transcripts.py::_upload_payload::string-prefix::6512e60c732b",
    ),
    **_reasoned(
        "Display-label classification: maps a session's project_dir prefix to a company "
        "label for dashboard grouping. No operand grants or denies filesystem access; the "
        "os.sep boundary exists to avoid sibling-name label collisions (PersonalFinance "
        "vs Personal), not to enforce containment.",
        "sessions/company_map.py::resolve_company::string-prefix::a52fe5ffe238",
    ),
    **_reasoned(
        "Raw relative-path grammar rejection only (absolute marker, dot segment, or "
        "normalization syntax); the resolved target is subsequently checked by an inode "
        "containment primitive before any filesystem access.",
        "api/routes/board_files.py::_resolve_contained_path::string-prefix::724ef2ecdc93",
        "api/routes/mounts.py::_safe_relative_path::string-prefix::724ef2ecdc93",
        "api/routes/projects.py::_reject_traversal::string-prefix::40604c052a99",
        "api/routes/projects.py::_safe_file_path::string-prefix::724ef2ecdc93",
        "context/completeness.py::_is_logical_relative::string-prefix::d2d8ec167846",
        "csi/frozen.py::repo_relative::string-prefix::d2d8ec167846",
        "csi/frozen.py::repo_relative::lexical-path-identity::03de29b4130d",
        "execution/policy.py::_norm_path::string-prefix::0dc3922c6d16",
        "execution/verify.py::_norm::string-prefix::81590bea8404",
        "policy/protected_paths.py::_norm_registry_entry::string-prefix::cd1c82af4862",
        "policy/protected_paths.py::_norm_registry_entry::string-prefix::ca86f405479a",
        "provision/service.py::_normalize_dir::string-prefix::40604c052a99",
        "reliability/governance.py::_norm::string-prefix::5bd5383594b5",
        "reliability/pipeline.py::_norm_rel::string-prefix::5bd5383594b5",
        "scope/paths.py::normalize_rel::string-prefix::24bd2aaedc6f",
        "swarm/planner.py::normalize_owned_path::string-prefix::328be64457aa",
        "swarm/scheduler.py::SwarmScheduler._norm_rel_path::string-prefix::06482dba5284",
    ),
    **_reasoned(
        "Verifier-token lexical grammar rejects absolute markers before accepting argv. "
        "Every filesystem target is subsequently required to exist beneath the workspace "
        "by validate_verifier_targets using the shared inode containment primitive at both "
        "plan admission and the final execution boundary.",
        "swarm/plan_safety.py::_validate_verify_target::string-prefix::1d228126e468",
        "swarm/plan_safety.py::parse_verifier_command::string-prefix::1d228126e468",
    ),
    **_reasoned(
        "Home-shell spelling detection is refusal-only: a match classifies the target as "
        "production and can never grant automatic deletion. The only local-temp admission "
        "later in _path_scope requires a strict descendant returned by inode_relative_parts.",
        "orchestrator/approvals.py::_path_scope::string-prefix::892802324549",
    ),
    **_reasoned(
        "Refusal-only, deny-widening lexical detection inside the secret classifier: a "
        "match can only classify the target as a secret reference (hard-stop); the "
        "string-only form exists precisely because inode containment cannot see "
        "case-variant or not-yet-existing spellings on a case-sensitive filesystem "
        "(the #426 design).",
        "secret_registry.py::_collapse_leading_double_slash::string-prefix::ec771957166a",
        "secret_registry.py::_collapse_leading_double_slash::string-prefix::b3e0b9f415f8",
        "secret_registry.py::_relative_to_home::string-prefix::b2c0263c4c42",
        "secret_registry.py::_relative_to_home::lexical-path-identity::71344bcde8d4",
        "secret_registry.py::_relative_to_home::string-prefix::88b38f74f7cf",
        "secret_registry.py::_case_variant_home_secret_dir::string-prefix::bd8b683c7241",
    ),
    **_reasoned(
        "Comparison of canonical repository-relative identifiers or declared policy "
        "components, not operating-system paths. It shapes metadata/risk/ownership only; "
        "the eventual filesystem target is independently inode-confined.",
        "agentless/prompts.py::_windowed_content::lexical-path-identity::6af39f66ecf4",
        "audit/trace.py::_path_covered::prefix-slice-equality::5e43bbd895c0",
        "csi/conflict.py::ConflictForecastService.forecast::string-prefix::38d9feec98f7",
        "csi/conflict.py::ConflictForecastService.forecast::string-prefix::5247b88efd9d",
        "csi/frozen.py::is_frozen_path::string-prefix::1363fa4cb8eb",
        "execution/verify.py::_under::lexical-path-identity::011331b6bdda",
        "execution/verify.py::_under::string-prefix::dd80f882b6cd",
        "lab/surfaces/__init__.py::load_surface_content::string-prefix::0452fdee4bff",
        "lab/surfaces/__init__.py::load_surface_content::string-prefix::4c5ad99a877c",
        "policy/protected_paths.py::ProtectedPathsRegistry.path_tier::string-prefix::2c2d98099855",
        "policy/protected_paths.py::_norm_runtime_path::string-prefix::5bd5383594b5",
        "policy/protected_paths.py::_under_any::string-prefix::2f117641531b",
        "policy/protected_paths.py::_reject_cross_tier_overlap::string-prefix::63c2d577a107",
        "policy/protected_paths.py::_reject_cross_tier_overlap::string-prefix::0defe45f45ea",
        "policy/protected_paths.py::_reject_cross_tier_overlap::string-prefix::901ae9b1d19b",
        "policy/protected_paths.py::_reject_cross_tier_overlap::string-prefix::37b7ca26f64f",
        "reliability/analyzer.py::_risk::string-prefix::1e4489e8db3b",
        "reliability/pipeline.py::ImprovementPipeline._apply_config_edit::string-prefix::d23c4796479d",
        "reliability/sandbox.py::PipelineSandboxRunner._apply_config_edit::string-prefix::c9e9b2e59665",
        "runner/sandbox.py::adapter_write_roots::string-prefix::59e54cf5ef84",
        "runner/sandbox.py::adapter_write_roots::string-prefix::d5ea708b189c",
        "scope/paths.py::under::prefix-slice-equality::748c2d326958",
        "scope/paths.py::_private_realm::prefix-slice-equality::45ae926b3776",
        "swarm/planner.py::paths_overlap::string-prefix::901ae9b1d19b",
        "swarm/planner.py::paths_overlap::string-prefix::37b7ca26f64f",
        "swarm/scheduler.py::SwarmScheduler._contained_in::prefix-slice-equality::f37b28d09e65",
        "swarm/scheduler.py::SwarmScheduler._path_owned::string-prefix::daa573dc6104",
        "testpolicy/deadcode.py::classify_stub::string-prefix::68308db0bc52",
        "workmodes/manifest.py::ArtifactManifest.entry::lexical-path-identity::e4c31118a693",
    ),
    **_reasoned(
        "Relative-name rendering after the caller has already inode-confined the path, or "
        "during a non-following tree walk; it only produces response/evidence text and "
        "cannot admit a filesystem object.",
        "api/routes/board_files.py::list_board_files._add::lexical-relative_to::a81912019a5b",
        "api/routes/board_files.py::download_board_archive::lexical-relative_to::a81912019a5b",
        "api/routes/board_files.py::reveal_board_file::lexical-relative_to::c9ad92bba826",
        "api/routes/projects.py::_file_row::lexical-relative_to::4e72a836ab8e",
        "api/routes/projects.py::list_project_files::lexical-relative_to::d22a8239fcba",
        "api/routes/projects.py::list_project_files::lexical-relative_to::64c372201117",
        "archdocs/context.py::_split_sections::lexical-relative_to::1c9f1095c68c",
        # dev-upload session_id rendering: parse_file already received the path
        # from the confined walk; this only names the row.
        "fleetcap/extract.py::_extract_dev_uploads::lexical-relative_to::bf2fc8ff7fb8",
        "csi/evidence.py::_skills_snapshot::lexical-relative_to::331cea16bf03",
        "csi/evidence.py::_dashboard_ux_signals::lexical-relative_to::331cea16bf03",
        "csi/evidence.py::compile_packet::lexical-relative_to::331cea16bf03",
        "execution/verify.py::_resolved_paths::lexical-relative_to::e4d8f1835c4e",
        "lab/api/routes/lab.py::_note_summary::lexical-relative_to::4e72a836ab8e",
        "lab/api/routes/lab.py::_vault_tree::lexical-relative_to::4e72a836ab8e",
        "lab/curator/rollup.py::scan_vault_context::lexical-relative_to::4e72a836ab8e",
        "reflection/adapters.py::ClaudeSourceAdapter.extract::lexical-relative_to::80554e491e08",
        "reflection/adapters.py::ClaudeSourceAdapter.extract::lexical-is_relative_to::92a3541996ee",
        "reflection/harvest.py::read_ledger_files::lexical-relative_to::e529a53e603c",
        "reflection/harvest.py::read_historical_learnings::lexical-relative_to::e529a53e603c",
        "reflection/harvest.py::read_historical_learnings::lexical-is_relative_to::753aadbbc1cd",
        "reliability/governance.py::_resolve_entry_targets::lexical-relative_to::e4d8f1835c4e",
        "swarm/planner.py::resolve_owned_path::lexical-relative_to::b8d57070ab24",
        "testpolicy/deadcode.py::classify_tree_deadcode::lexical-relative_to::9ad8d391ea9c",
        "testpolicy/handlers.py::classify_tree_handlers::lexical-relative_to::9ad8d391ea9c",
        "tracelab/scan.py::_walk_data_files::lexical-relative_to::4e72a836ab8e",
        "vaultgraph/parser.py::_iter_note_paths::lexical-relative_to::44ba62f64200",
        "vaultgraph/parser.py::walk_vault::lexical-relative_to::4e72a836ab8e",
        "vaultgraph/search.py::global_search::lexical-relative_to::807d761cd147",
    ),
    **_reasoned(
        "Canonical-spelling refusal only: a mismatch can only reject an input. The "
        "following admission/containment check is inode-backed, so an alias cannot gain "
        "access through this lexical guard.",
        "api/routes/board_files.py::_enforce_workspace_floor::lexical-path-identity::084e4fd8852d",
        "csi/frozen.py::assert_canonical_destination::lexical-path-identity::664fdc6b7c22",
        "lab/runtime.py::_prompt_file::lexical-path-identity::aa6c87db950d",
    ),
    **_reasoned(
        "Exact canonical-request spelling guard only: a mismatch can only refuse promotion. "
        "The enforce path derives the accepted root and lock from the validated repository; "
        "_StableLock then opens and rechecks every canonical directory and lock by descriptor "
        "and inode. This lexical equality is deliberately not presented as that filesystem "
        "safety proof.",
        "integration/promote.py::_StableLock.acquire::lexical-path-identity::7bc83037754a",
        "integration/promote.py::_StableLock.acquire::lexical-path-identity::95eeed660c3e",
        "integration/promote.py::PromotionFinalizer.run::lexical-path-identity::7bc83037754a",
        "integration/promote.py::PromotionFinalizer.run::lexical-path-identity::95eeed660c3e",
    ),
    **_reasoned(
        "Canonical architecture-document spelling guard only. A mismatch returns stale "
        "(fail closed); it cannot authorize or open a filesystem object. Inode containment "
        "would weaken this stricter policy by treating a differently named hard link as the "
        "canonical ARCHI.md.",
        "archdocs/staleness.py::is_stamp_stale::lexical-path-identity::fb203b18e391",
    ),
    **_reasoned(
        "Registry-location spelling only decides whether to require the following explicit "
        "inode containment proof. The checked-in default and every spelling below the "
        "repository are inode-confined before opening; custom out-of-repo registries are "
        "explicit caller-supplied inputs and the leaf is separately opened with O_NOFOLLOW.",
        "policy/protected_paths.py::_assert_registry_location_safe::lexical-relative_to::024fa4143529",
        "policy/protected_paths.py::_assert_registry_location_safe::lexical-path-identity::09cb84022ce0",
    ),
    **_reasoned(
        "Reflection label rendering only; it cannot admit or open a filesystem object. "
        "Whole-file sampling is separately authorized by _is_run_scoped_path through "
        "explicit inode containment and identity checks.",
        "reflection/perrun.py::_is_relative::lexical-relative_to::4e72a836ab8e",
        "reflection/perrun.py::_path_label::lexical-relative_to::4e72a836ab8e",
    ),
    **_reasoned(
        "UI grouping, de-duplication, cache selection, or prompt shaping only; the "
        "comparison neither authorizes nor performs a filesystem operation.",
        "api/routes/board_files.py::reveal_board_file::lexical-path-identity::c700d2354552",
        "api/routes/projects.py::list_project_files::lexical-path-identity::d3c84a0ad04f",
        "api/routes/projects.py::list_project_files::lexical-path-identity::9645477a531e",
        "api/routes/projects.py::list_project_files::lexical-path-identity::94e7973822f5",
        "filesearch/catalog.py::root_label::string-prefix::4f81618be0b0",
        "filesearch/catalog.py::root_label::string-prefix::d7a8c4162599",
        "knowledge/recall.py::_get_audit_store::lexical-path-identity::ce42a67f77eb",
        "workmodes/artificer.py::worker_shape::lexical-path-identity::6064d6414be6",
        "workmodes/artificer.py::artificer_prompt::lexical-path-identity::6064d6414be6",
    ),
    **_reasoned(
        "Append-only comparison of the escalation cursor's visited tuple. The values are "
        "route identifiers in in-memory state, not filesystem paths, and this check grants "
        "no filesystem access.",
        "routing/escalation_store.py::InMemoryEscalationCursorStore.cas_advance::prefix-slice-equality::c0c714012d7e",
    ),
    **_reasoned(
        "Root-climb loop termination, not a containment or identity verdict; the loop's "
        "security result is produced later by os.path.samestat through the shared primitive.",
        "path_containment.py::_nearest_existing::lexical-path-identity::6186ad00538b",
        "path_containment.py::_inode_relative_parts_decision::lexical-path-identity::6186ad00538b",
        "scope/paths.py::_nearest_existing_dir::lexical-path-identity::562b47f19b9b",
        "sessions/token.py::_nearest_existing::lexical-path-identity::6186ad00538b",
    ),
    **_reasoned(
        "Additive refusal of filesystem root/home aliases. The same branch separately proves "
        "home containment by inode; these comparisons cannot admit a campaign root.",
        "simgate.py::resolve_sim_context::lexical-path-identity::67e7c1a67250",
        "simgate.py::resolve_sim_context::lexical-path-identity::d316bc24e122",
    ),
    **_reasoned(
        "The module-level credential-path comparisons distinguish explicit in-process override "
        "assignments from untouched sentinels; production-store identity is checked later by "
        "the inode-anchored absent-leaf routine.",
        "sessions/token.py::token_path::lexical-path-identity::74eed1c80b98",
        "sessions/hook_token.py::_hook_tokens_root::lexical-path-identity::05c77865cc22",
        "sessions/ssh_keys.py::_ssh_keys_root::lexical-path-identity::e7ce13b3563b",
        "sessions/ssh_keys.py::_server_inventory_path::lexical-path-identity::fd8a49596879",
    ),
    **_reasoned(
        "os.walk reports this directory lexically below its non-followed root; the comparison "
        "only prunes the generated moc folder, and each file is inode-confined before reading.",
        "vaultgraph/parser.py::_iter_note_paths::lexical-path-identity::87a617b8cc41",
    ),
    **_reasoned(
        "JSON metadata field equality inside implement_json finalization: both sides are raw "
        "stored path *strings* from a dict, not resolved filesystem objects.  The live "
        "worktree identity gates in csi/implement.py use inode_paths_equal separately.",
        "csi/store.py::_is_typed_implementation_finalization::lexical-path-identity::56b6a49300c2",
    ),
    **_reasoned(
        "Sentinel comparison against the unwritable /dev/null placeholder _get_ledger_path "
        "returns when no var root is configured. It decides only whether to mkdir a parent "
        "directory; the ledger append that follows is unconditional and the real path came "
        "from runtime_paths.resolve_var_root, so this equality neither selects nor admits a "
        "filesystem target. The middleware is observe-only and never rejects a request.",
        "api/middleware/chokepoint.py::_record_observation::lexical-path-identity::08885952fc1a",
    ),
    **_reasoned(
        "HTTP ROUTE identity, not a filesystem decision: request.url.path is the URL path of "
        "the incoming request and _BREAKER_RESET_PATH is the constant reset route. Neither "
        "operand names a filesystem object. A match only declines to synthesize the 503 here "
        "so the request reaches routing, where the real reset route runs behind the same "
        "require_session_token gate as every other mutating route; the middleware itself "
        "authenticates, authorizes, and clears nothing.",
        "api/middleware/chokepoint.py::ChokePointMiddleware.dispatch::lexical-path-identity::3f3206742c1d",
    ),
    **_reasoned(
        "Git ref grammar rejection on a projected BRANCH NAME. _valid_branch only shapes the "
        "read-only 'context' projection the engine surface returns (_context); the value is "
        "never joined to a directory, opened, or passed to a process, and a rejection can "
        "only null the projected field. Neither operand is an operating-system path, so there "
        "is no filesystem target for an inode primitive to anchor against.",
        "api/routes/engine.py::_valid_branch::string-prefix::40604c052a99",
    ),
    **_reasoned(
        "Grammar check on a REMOTE ssh path this host cannot stat: the operand is the source "
        "half of an rsync 'user@host:/path/' argument naming a directory on another machine, "
        "so no local inode exists for the shared primitive to prove containment against. The "
        "check is refusal-only (an absolute spelling is required and _safe_root separately "
        "whitelists the root), and the one LOCAL path this function builds — the rsync "
        "destination — is constructed beneath the caller's ingest root, not from this value.",
        "fleetcap/pull.py::commands::string-prefix::e3d996b0beb8",
    ),
    **_reasoned(
        "Device-LABEL rendering for attribution only: both operands are resolved, the file was "
        "already reached and read through the profile's own glob, and the result is the first "
        "path component used as a device name in the session row ('unknown' on failure). It "
        "admits, opens, and selects no filesystem object.",
        "fleetcap/extract.py::_device_for::lexical-relative_to::300ca363538a",
    ),
    **_reasoned(
        "Codex profile-root de-duplication over base.glob('.codex*'), whose results are direct "
        "children of base by construction: the '!=' drops the duplicate '.codex' the glob also "
        "matches, and the '==' keeps the canonical root listed even when it does not exist yet "
        "so enrollment can create it. Neither comparison can introduce a root the glob did not "
        "already produce; every other root's membership is decided by is_dir(). The later "
        "transcript globs (fleetcap/extract.py:364 and :552) walk whatever roots the glob and "
        "is_dir() admitted, identically for all four CLI families, and this equality neither "
        "widens nor narrows that set.",
        "fleetcap/profiles.py::enumerate_profiles::lexical-path-identity::402aeafae0b9",
        "fleetcap/profiles.py::enumerate_profiles::lexical-path-identity::1e2b09ef1f95",
    ),
    **_reasoned(
        "Spelling NORMALIZATION inside the deny-widening portable chain, not a verdict: "
        "_collapse_leading_double_slash feeds _normalized_abs, which is reachable only from "
        "_home_spellings / _base_spellings / _home_relative_rels, whose sole consumer is "
        "_case_variant_home_secret_dir — a predicate that can only return True and therefore "
        "only ADD a hard-stop. The two prefixes separate the one spelling POSIX leaves "
        "implementation-defined (exactly two leading slashes, which os.path.normpath "
        "deliberately preserves) from three or more, which normpath has already collapsed; "
        "both operands are string literals and the function stats nothing. The shared inode "
        "primitive cannot stand in: this chain exists precisely to decide references to "
        "case-variant directories that need not EXIST on disk, so there is no inode to anchor "
        "against, and rule 4's inode containment has already run above it.",
        "secret_registry.py::_collapse_leading_double_slash::string-prefix::ec771957166a",
        "secret_registry.py::_collapse_leading_double_slash::string-prefix::b3e0b9f415f8",
    ),
    **_reasoned(
        "PURE-STRING HOME-relativization, filesystem-free on purpose (#426): the class it "
        "closes — '~/.SSH/authorized_keys' on a case-SENSITIVE filesystem — names a directory "
        "that does not exist, so inode_relative_parts_anchored has nothing to stat and rule 4 "
        "has already declined by the time this runs. The '/' test handles only a root HOME, "
        "where every absolute path genuinely IS home-rooted; the '== home' and "
        "'startswith(home + \"/\")' pair is SEGMENT-anchored so '/homerun/x' can never read as "
        "being under '/home'. The one consumer, _case_variant_home_secret_dir, acts only on "
        "True, so this helper can only widen the deny set: returning None where a real home "
        "relation exists loses a hard-stop, it never grants access.",
        "secret_registry.py::_relative_to_home::string-prefix::b2c0263c4c42",
        "secret_registry.py::_relative_to_home::lexical-path-identity::71344bcde8d4",
        "secret_registry.py::_relative_to_home::string-prefix::88b38f74f7cf",
    ),
    **_reasoned(
        "The case-FOLDED, SEGMENT-ANCHORED secret-dir match itself — deliberately lexical "
        "because the shared inode primitive structurally cannot make this decision: inode "
        "aliasing across case exists only on a case-INSENSITIVE filesystem and only for a "
        "directory that already exists, which is exactly why '~/.SSH/authorized_keys' "
        "hard-stopped on APFS and auto-ran on Linux before #426. The 'dir_rel_cf + \"/\"' "
        "suffix is what keeps the match anchored on a whole segment, so prefix look-alikes "
        "('~/.sshfoo/bar', '~/.config/gcloudx/y') stay allowed. Both call sites — "
        "references_secret rule 5 and write_target_references_secret — consult it only after "
        "the inode checks have declined and act only on True, so it is a pure widening of "
        "DENY and can admit no filesystem object.",
        "secret_registry.py::_case_variant_home_secret_dir::string-prefix::bd8b683c7241",
    ),
    **_reasoned(
        "Additive fast-path refusal beside an inode proof in the SAME condition: the serving "
        "checkout is refused by inode_paths_equal(...) is True on the following operand, and a "
        "root that is a checkout under any other spelling is refused independently by the "
        "version-control marker probe two lines below. This equality can only add a refusal; "
        "removing it changes no verdict.",
        "api/routes/board_files.py::_is_code_checkout_root::lexical-path-identity::b98a34ae5b11",
    ),
    **_reasoned(
        "Bounded vault walk: containment is decided one line earlier by "
        "inode_relative_parts_anchored, and these relative renderings only impose a depth cap "
        "and a stable sort order on already-admitted notes. Both uses can drop a candidate; "
        "neither can admit one.",
        "lab/api/routes/lab.py::_vault_notes::lexical-relative_to::64c372201117",
    ),
    **_reasoned(
        "Hard-stop refusal grammar, evaluated twice on purpose. is_hard_stop only ever RETURNS "
        "TRUE to refuse, and authorise_write runs it on the declared spelling (step b) and "
        "again on the canonical spelling that inode_relative_parts_anchored resolved (step d). "
        "A lexical miss therefore cannot authorise a write: the inode containment proof between "
        "the two calls is what admits, and the second call re-refuses whatever the first missed. "
        "The read-only auditors that call it directly compare committed repo-relative strings "
        "and open nothing.",
        "reflection/guard.py::is_hard_stop::string-prefix::f54602f50859",
        "reflection/guard.py::is_hard_stop::string-prefix::92dd2a2c97c3",
        "reflection/guard.py::is_hard_stop::path-text-membership::92c3084eeccb",
        "reflection/guard.py::is_hard_stop::path-text-membership::cafe107057f3",
    ),
    **_reasoned(
        "Gate-evidence enumeration inside a non-following os.walk whose every symlinked "
        "directory and symlinked config already raises GateEvidenceRefusal. The identity "
        "compare only skips the root level (hashed separately, absence included) and the two "
        "relative renderings only produce the refusal message and the digest's PATH half. None "
        "of them opens, admits, or selects a file the walk had not already reached.",
        "scheduler/gate_ecosystems.py::NpmVitestExecutor._nested_config_paths::lexical-relative_to::663895f88947",
        "scheduler/gate_ecosystems.py::NpmVitestExecutor._nested_config_paths::lexical-path-identity::a658da1d4bfb",
        "scheduler/gate_ecosystems.py::NpmVitestExecutor._nested_config_paths::lexical-relative_to::4b7b96bc299a",
    ),
    **_reasoned(
        "Go tooling grammar, not filesystem access: build_argv prepends the './' package-spec "
        "marker go test requires on the command line (the directory itself was already required "
        "to exist by preflight), and count compares go SUBTEST names ('Parent/Child') to drop "
        "summary parents from the leaf count. Neither operand is a filesystem path.",
        "scheduler/gate_ecosystems.py::GoTestExecutor.build_argv::string-prefix::1a986da7fc08",
        "scheduler/gate_ecosystems.py::GoTestExecutor.count::string-prefix::dcc59a9a8153",
    ),
    **_reasoned(
        "Refusal-only classification of a loop's declared gate TARGET against the constant "
        "policy components ('tests', 'scheduler'). Both directions return a refusal label; "
        "neither reads, writes, nor admits a filesystem object, and the loop machinery re-derives "
        "any real path elsewhere.",
        "scheduler/routines.py::loop_gate_target_verdict::prefix-slice-equality::fcadeadde9c9",
        "scheduler/routines.py::loop_gate_target_verdict::prefix-slice-equality::8ca6919d0249",
    ),
    **_reasoned(
        "Duplicate-entry check on the in-process sys.path list, not a filesystem decision. The "
        "inserted root is derived from this module's own installed location and is already "
        "required to be a directory (ImportError otherwise); the membership test only avoids "
        "appending it twice and cannot change which tree is imported.",
        "scheduler/routines.py::w3_health_monitor_routine::path-text-membership::afd733c7c60f",
    ),
    **_reasoned(
        "Unified-diff line grammar: '\\' introduces the '\\ No newline at end of file' marker, "
        "which is diff metadata rather than content. The operand is a diff line, not a path, and "
        "the comparison selects no filesystem object.",
        "skills/__init__.py::_apply_unified_diff::string-prefix::b6dbc91cc111",
    ),
    **_reasoned(
        "Evidence attribution over git-reported repository-relative path STRINGS: './' stripping "
        "normalises the stored spelling and the equality/prefix match decides only which board "
        "card a commit is credited to. Nothing in this module touches the filesystem.",
        "team/attribution.py::_normalized_path::string-prefix::81590bea8404",
        "team/attribution.py::_owns_any::lexical-path-identity::744e99dc1784",
    ),
}

# Keep this explicit even when empty.  A genuinely known-dead security entry
# belongs here temporarily and is rendered as xfail(strict=True) by
# known_dead_params(); it must never be hidden in PATH_DECISION_EXCLUSIONS.
KNOWN_DEAD_SECURITY_SITES: tuple[KnownDeadEntry, ...] = ()


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _name_looks_pathlike(name: str) -> bool:
    """Return whether *name* is a conventional filesystem-path identifier.

    Discrimination is operand-oriented, not a raw substring match:

    * Strong suffixes (``path``, ``root``, ``worktree``, …) always count —
      including private forms like ``_path`` / ``repo_root``.
    * Single-token path vocabulary (``home``, ``vault``, …) counts.
    * Ambiguous structural tokens ``parent`` / ``child`` count **only** as the
      bare locals used in path-climb code.  Private/structural forms such as
      ``_parent`` (union-find parent pointers) do **not** count — those are
      integers, not filesystem paths.  pathlib's ``.parent`` attribute is
      handled separately via :data:`_PATH_BEARING_ATTRS`.
    """
    raw = name.lower()
    core = raw.strip("_")
    if not core:
        return False
    parts = tuple(part for part in core.split("_") if part)
    if not parts:
        return False
    if parts[-1] in _STRONG_PATH_SUFFIXES:
        return True
    if len(parts) == 1 and parts[0] in _PATH_NAME_PARTS - _AMBIGUOUS_STRUCTURAL_NAMES - {
        "source",
        "target",
        "file",
    }:
        return True
    # Bare path-climb locals only (not _parent, active_parent, parent_ptr, …).
    return raw in _AMBIGUOUS_STRUCTURAL_NAMES


def _is_ambiguous_path_name(node: ast.AST) -> bool:
    """Names too broad for global taint, but path-like beside a proven path."""
    return isinstance(node, ast.Name) and node.id.lower().strip("_") in {
        "file",
        "source",
        "target",
    }


def _annotation_looks_pathlike(node: ast.AST | None) -> bool:
    if node is None:
        return False
    text = ast.unparse(node)
    return "Path" in text or "PathLike" in text


def _is_explicit_string_path(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node.func)
    return name in {"os.fspath", "fspath"} or name.rsplit(".", 1)[-1] in _PATH_FACTORIES


def _is_str_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _call_name(node.func) == "str"


def _contains_os_sep(node: ast.AST) -> bool:
    return any(
        isinstance(part, ast.Attribute)
        and part.attr in {"sep", "altsep"}
        and _call_name(part.value) == "os"
        for part in ast.walk(node)
    )


def _contains_path_separator_literal(node: ast.AST) -> bool:
    return any(
        isinstance(part, ast.Constant)
        and isinstance(part.value, str)
        and ("/" in part.value or "\\" in part.value)
        for part in ast.walk(node)
    )


def _is_prefix_slice(node: ast.AST, other: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
        return False
    upper = node.slice.upper
    if not (
        node.slice.lower is None
        and node.slice.step is None
        and isinstance(upper, ast.Call)
        and _call_name(upper.func) == "len"
        and len(upper.args) == 1
    ):
        return False
    return ast.dump(upper.args[0], include_attributes=False) == ast.dump(
        other,
        include_attributes=False,
    )


class _ScopeCollector(ast.NodeVisitor):
    """Collect path-tainted names and structural decision sites in one scope."""

    def __init__(self, relative_file: str, scope: str, node: ast.AST) -> None:
        self.relative_file = relative_file
        self.scope = scope
        self.node = node
        self.tainted: set[str] = set()
        self.sites: list[PathDecisionSite] = []
        self.nodes = tuple(self._walk_scope(node))
        node_set = set(self.nodes)
        self.parents = {
            child: parent
            for parent in self.nodes
            for child in ast.iter_child_nodes(parent)
            if child in node_set
        }
        self.inode_anchor_calls = sum(
            isinstance(item, ast.Call)
            and _call_name(item.func).rsplit(".", 1)[-1] == "inode_relative_parts"
            for item in self.nodes
        )

    @classmethod
    def _walk_scope(cls, node: ast.AST):
        yield node
        for child in ast.iter_child_nodes(node):
            if child is not node and isinstance(
                child,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
            ):
                continue
            yield from cls._walk_scope(child)

    def collect(self) -> list[PathDecisionSite]:
        args = getattr(self.node, "args", None)
        if isinstance(args, ast.arguments):
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                if _name_looks_pathlike(arg.arg) or _annotation_looks_pathlike(arg.annotation):
                    self.tainted.add(arg.arg)
            if args.vararg and (
                _name_looks_pathlike(args.vararg.arg)
                or _annotation_looks_pathlike(args.vararg.annotation)
            ):
                self.tainted.add(args.vararg.arg)
            if args.kwarg and (
                _name_looks_pathlike(args.kwarg.arg)
                or _annotation_looks_pathlike(args.kwarg.annotation)
            ):
                self.tainted.add(args.kwarg.arg)

        # Fixed point: assignments may be ordered through aliases.
        assignments = [
            item
            for item in self.nodes
            if isinstance(item, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
        ]
        for_loops = [item for item in self.nodes if isinstance(item, (ast.For, ast.AsyncFor))]
        changed = True
        while changed:
            changed = False
            for item in assignments:
                value = item.value
                if not self._is_path_expr(value):
                    continue
                for name in self._assigned_names(item):
                    if name not in self.tainted:
                        self.tainted.add(name)
                        changed = True
            # Path collections iterated in for-loops taint the loop target
            # (e.g. ``for blocked in never_set`` where never holds path roots).
            for loop in for_loops:
                if not self._is_path_expr(loop.iter) and not self._iter_looks_path_collection(
                    loop.iter
                ):
                    continue
                for name in self._target_names(loop.target):
                    if name not in self.tainted:
                        self.tainted.add(name)
                        changed = True

        for item in self.nodes:
            if isinstance(item, ast.Call):
                self._visit_call(item)
            elif isinstance(item, ast.Compare):
                self._visit_compare(item)
        return self.sites

    @staticmethod
    def _assigned_names(
        node: ast.Assign | ast.AnnAssign | ast.NamedExpr,
    ) -> tuple[str, ...]:
        if isinstance(node, ast.Assign):
            targets = node.targets
        else:
            targets = [node.target]
        return tuple(
            item.id
            for target in targets
            for item in ast.walk(target)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
        )

    @staticmethod
    def _target_names(target: ast.AST) -> tuple[str, ...]:
        return tuple(
            item.id
            for item in ast.walk(target)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
        )

    def _iter_looks_path_collection(self, node: ast.AST) -> bool:
        """True for names like never_set / path_roots that hold path members."""
        if isinstance(node, ast.Name):
            lowered = node.id.lower().strip("_")
            if lowered in self.tainted or _name_looks_pathlike(node.id):
                return True
            parts = tuple(part for part in lowered.split("_") if part)
            if not parts:
                return False
            # never_set, path_list, grant_roots, blocked_paths, ...
            return (
                parts[0] in _PATH_NAME_PARTS
                or parts[-1]
                in {
                    "paths",
                    "roots",
                    "dirs",
                    "directories",
                    "set",
                    "list",
                }
                and any(
                    part in _PATH_NAME_PARTS or part in {"never", "blocked", "grant", "standing"}
                    for part in parts
                )
            )
        if isinstance(node, ast.Call):
            # set(never_roots()), list(...), tuple(...) of path sources
            name = _call_name(node.func).rsplit(".", 1)[-1]
            if name in {"set", "list", "tuple", "frozenset"}:
                return any(
                    self._is_path_expr(arg) or self._iter_looks_path_collection(arg)
                    for arg in node.args
                )
            return _name_looks_pathlike(name) or name.endswith(
                ("_roots", "_paths", "_dirs", "_directories")
            )
        return False

    def _is_path_expr(self, node: ast.AST) -> bool:
        """True when *node* is a plausible filesystem-path operand.

        Evidence, in order of strength:

        1. Path factories (``Path(...)``, ``os.path.dirname``, …) and ``str()``
           of a path expression.
        2. Names tainted by assignment from (1) or parameters annotated
           ``Path`` / ``PathLike`` / conventional path identifiers.
        3. Known path-bearing attributes (exact ``.parent`` on pathlib; attrs
           whose names are strong path identifiers such as ``.worktree``).
        4. Binary path composition (``/`` or ``+`` with a path operand).

        Integer parent-pointer tables (``self._parent[i]``) produce none of
        this evidence and must not taint their indices.
        """
        if isinstance(node, ast.Name):
            return node.id in self.tainted or _name_looks_pathlike(node.id)
        if isinstance(node, ast.Attribute):
            if node.attr in _PATH_TERMINALS or node.attr.startswith("st_"):
                return False
            # pathlib.Path.parent (exact spelling) — not private ``_parent``.
            if node.attr in _PATH_BEARING_ATTRS:
                return True
            return _name_looks_pathlike(node.attr) or self._is_path_expr(node.value)
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            base = name.rsplit(".", 1)[-1]
            if base in _PATH_FACTORIES or name in {"os.fspath", "fspath"}:
                return True
            if name == "str":
                return bool(node.args) and self._is_path_expr(node.args[0])
            return _name_looks_pathlike(base)
        if isinstance(node, ast.BinOp):
            return self._is_path_expr(node.left) or self._is_path_expr(node.right)
        if isinstance(node, ast.BoolOp):
            return any(self._is_path_expr(value) for value in node.values)
        if isinstance(node, ast.IfExp):
            return self._is_path_expr(node.body) or self._is_path_expr(node.orelse)
        if isinstance(node, ast.Subscript):
            # Indexing a pathlike *collection of paths* is pathlike; indexing a
            # list of ints named ``_parent`` is not (name is not path evidence).
            return self._is_path_expr(node.value)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return any(self._is_path_expr(elt) for elt in node.elts)
        return False

    def _add(self, node: ast.AST, rule: str, *, inode: bool = False) -> None:
        self.sites.append(
            PathDecisionSite(
                relative_file=self.relative_file,
                scope=self.scope,
                lineno=getattr(node, "lineno", 0),
                col_offset=getattr(node, "col_offset", 0),
                rule=rule,
                source=ast.unparse(node),
                is_inode_backed=inode,
            )
        )

    def _visit_call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        base = name.rsplit(".", 1)[-1]
        # Three-valued boolean primitives (True/False/None).  Bare truthiness
        # collapses None into the false branch and must never render as safe.
        if base in {"inode_path_is_within_anchored", "inode_paths_equal"}:
            explicit = _is_explicit_tri_state_check(node, self.parents.get(node))
            self._add(
                node,
                base.replace("_", "-") if explicit else "tri-state-bare-truthiness",
                inode=explicit,
            )
            return
        if base in {"inode_relative_parts", "inode_relative_parts_anchored"}:
            self._add(node, base.replace("_", "-"), inode=True)
            return
        if base == "samestat":
            self._add(
                node,
                "shared-samestat"
                if self.relative_file == "path_containment.py"
                else "direct-samestat",
                inode=self.relative_file == "path_containment.py",
            )
            return
        if base in {"relative_to", "is_relative_to"}:
            self._add(node, f"lexical-{base}")
            return
        if base in {"commonpath", "commonprefix"}:
            # A surrounding comparison gets the more precise rule below.
            parent = self.parents.get(node)
            while parent is not None and not isinstance(parent, ast.stmt):
                if isinstance(parent, ast.Compare):
                    return
                parent = self.parents.get(parent)
            if parent is None or not isinstance(parent, ast.Compare):
                self._add(node, f"{base}-decision")
            return
        if base != "startswith" or not isinstance(node.func, ast.Attribute):
            return
        receiver = node.func.value
        if (
            _is_str_call(receiver)
            and any(_is_str_call(arg) for arg in node.args)
            or _is_explicit_string_path(receiver)
            or any(_is_explicit_string_path(arg) for arg in node.args)
            or any(_contains_path_separator_literal(arg) for arg in node.args)
            or _contains_os_sep(node)
            and any(self._is_path_expr(arg) for arg in node.args)
            or self._is_path_expr(receiver)
            and (any(self._is_path_expr(arg) for arg in node.args) or _contains_os_sep(node))
        ):
            self._add(node, "string-prefix")

    def _visit_compare(self, node: ast.Compare) -> None:
        if (
            self.relative_file == "sessions/token.py"
            and self.scope == "_is_legacy_token_path"
            and self.inode_anchor_calls >= 1
            and "legacy_parts" in ast.unparse(node)
        ):
            # Exact remaining-component identity after inode_relative_parts is
            # the contract.  Case-fold / normcase / lower on those components
            # is contract-forbidden and must NOT be reported as inode-backed.
            source = ast.unparse(node)
            folded = any(
                marker in source for marker in ("_fold", "normcase", ".lower(", "casefold")
            )
            self._add(
                node,
                (
                    "case-folded-component-identity"
                    if folded
                    else "inode-anchored-component-identity"
                ),
                inode=not folded,
            )
            return
        if (
            self.relative_file == "path_containment.py"
            and self.scope
            in {
                "inode_paths_equal",
                "_inode_relative_parts_anchored_decision",
            }
            and any(
                isinstance(part, ast.Name) and part.id.endswith("_parts") for part in ast.walk(node)
            )
        ):
            # Exact remaining-component compare inside the shared primitive.
            self._add(node, "inode-anchored-component-identity", inode=True)
            return
        operands = [node.left, *node.comparators]
        pairs = list(zip(operands[:-1], operands[1:], strict=True))
        for operator, (left, right) in zip(node.ops, pairs, strict=True):
            if isinstance(operator, (ast.Eq, ast.NotEq)):
                if _is_prefix_slice(left, right) or _is_prefix_slice(right, left):
                    inode_anchored = (
                        self.relative_file == "path_containment.py" or self.inode_anchor_calls >= 2
                    ) and all(
                        isinstance(item, ast.Name) and item.id.endswith("_parts")
                        for item in (left, right)
                        if not isinstance(item, ast.Subscript)
                    )
                    # Prefix of rooted parts after an inode anchor is the shared
                    # primitive's exact-component containment rule.
                    if not inode_anchored and self.relative_file == "path_containment.py":
                        inode_anchored = True
                    self._add(
                        node,
                        (
                            "inode-anchored-component-prefix"
                            if inode_anchored
                            else "prefix-slice-equality"
                        ),
                        inode=inode_anchored,
                    )
                    return
                if any(
                    isinstance(part, ast.Call)
                    and _call_name(part.func).rsplit(".", 1)[-1] in {"commonpath", "commonprefix"}
                    for part in ast.walk(node)
                ):
                    self._add(node, "commonpath-comparison")
                    return
                if (
                    _is_explicit_string_path(left)
                    and _is_explicit_string_path(right)
                    or self._is_path_expr(left)
                    and self._is_path_expr(right)
                    or self._is_path_expr(left)
                    and _is_ambiguous_path_name(right)
                    or _is_ambiguous_path_name(left)
                    and self._is_path_expr(right)
                ):
                    self._add(node, "lexical-path-identity")
                    return
            if isinstance(operator, (ast.In, ast.NotIn)) and (
                _is_explicit_string_path(left)
                or _is_explicit_string_path(right)
                or self._is_path_expr(left)
                and self._is_path_expr(right)
            ):
                self._add(node, "path-text-membership")
                return


def _iter_scopes(tree: ast.Module) -> tuple[tuple[str, ast.AST], ...]:
    scopes: list[tuple[str, ast.AST]] = [("<module>", tree)]

    def descend(body: list[ast.stmt], prefix: str = "") -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = f"{prefix}.{node.name}" if prefix else node.name
                if not isinstance(node, ast.ClassDef):
                    scopes.append((qualname, node))
                descend(node.body, qualname)

    descend(tree.body)
    return tuple(scopes)


def _is_explicit_tri_state_check(call: ast.Call, parent: ast.AST | None) -> bool:
    """True when *call* is compared with ``is`` / ``is not`` against True/False.

    Three-valued path primitives return True, False, or None (unknown).  Bare
    truthiness treats None as false, so unknown silently takes one branch.  Only
    an explicit identity comparison against a boolean is a sound call shape.
    """
    if not isinstance(parent, ast.Compare) or len(parent.ops) != 1:
        return False
    if not isinstance(parent.ops[0], (ast.Is, ast.IsNot)):
        return False
    if parent.left is call:
        other = parent.comparators[0]
    elif parent.comparators[0] is call:
        other = parent.left
    else:
        return False
    return isinstance(other, ast.Constant) and other.value in (True, False)


def enumerate_path_decisions(root: Path = PACKAGE_ROOT) -> tuple[PathDecisionSite, ...]:
    """AST-enumerate path-decision shapes below ``root`` (or in one file).

    Unparseable production modules are recorded as ``unparseable-module`` sites
    (not skipped).  Unknown/unparseable must never render as CLEAN.
    """
    root = Path(root)
    paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
    sites: list[PathDecisionSite] = []
    for path in paths:
        relative_file = path.name if root.is_file() else path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeError):
            # Never silently omit: an unaudited module is not "good".
            sites.append(
                PathDecisionSite(
                    relative_file=relative_file,
                    scope="<module>",
                    lineno=0,
                    col_offset=0,
                    rule="unparseable-module",
                    source="<unparseable>",
                    is_inode_backed=False,
                )
            )
            continue
        for scope, scope_node in _iter_scopes(tree):
            sites.extend(_ScopeCollector(relative_file, scope, scope_node).collect())

    # AST traversal can encounter the same commonpath comparison as both a call
    # and comparison in exotic nesting.  Key-based de-duplication is stable.
    by_key = {site.key: site for site in sites}
    return tuple(
        sorted(
            by_key.values(),
            key=lambda site: (
                site.relative_file,
                site.lineno,
                site.col_offset,
                site.rule,
            ),
        )
    )


def audit_path_decisions(
    sites: tuple[PathDecisionSite, ...],
    *,
    exclusions: dict[str, str] | None = None,
) -> PathDecisionAudit:
    """Classify enumerated sites and completeness-check reasoned exclusions."""
    registry = PATH_DECISION_EXCLUSIONS if exclusions is None else exclusions

    def _reason_ok(key: str) -> bool:
        reason = registry.get(key)
        return isinstance(reason, str) and bool(reason.strip())

    safe = tuple(site for site in sites if site.is_inode_backed)
    lexical = tuple(site for site in sites if not site.is_inode_backed)
    excluded = tuple(site for site in lexical if _reason_ok(site.key))
    # Blank / non-string reasons do not exclude: the site remains unregistered.
    unregistered = tuple(site for site in lexical if not _reason_ok(site.key))
    discovered_keys = {site.key for site in lexical}
    stale = tuple(
        sorted(
            key
            for key, reason in registry.items()
            if key not in discovered_keys or not isinstance(reason, str) or not reason.strip()
        )
    )
    return PathDecisionAudit(
        safe_sites=safe,
        excluded_sites=excluded,
        unregistered=unregistered,
        stale_exclusions=stale,
    )


def counterfeit_grep_for_startswith(root: Path) -> tuple[str, ...]:
    """Deliberately weak text checker used only by the anti-counterfeit test."""
    hits: list[str] = []
    root = Path(root)
    paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        hits.extend(
            f"{path}:{lineno}" for lineno, line in enumerate(lines, 1) if ".startswith(" in line
        )
    return tuple(hits)


def known_dead_params() -> list[object]:
    """Return strict-xfail pytest params without making pytest an engine dependency."""
    import pytest

    return [
        pytest.param(
            entry,
            id=entry.key,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    f"SECURITY GAP: {entry.reason}. If this XPASS, remove the "
                    "known-dead entry; the site now resolves through inode_relative_parts."
                ),
            ),
        )
        for entry in KNOWN_DEAD_SECURITY_SITES
    ]


__all__ = [
    "KNOWN_DEAD_SECURITY_SITES",
    "PACKAGE_ROOT",
    "PATH_DECISION_EXCLUSIONS",
    "KnownDeadEntry",
    "PathDecisionAudit",
    "PathDecisionSite",
    "audit_path_decisions",
    "counterfeit_grep_for_startswith",
    "enumerate_path_decisions",
    "known_dead_params",
]
