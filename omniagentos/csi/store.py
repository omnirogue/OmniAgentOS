"""Persist CSI runs + model plans."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.csi.models import PlannerPlan, SynthesisResult
from omniagentos.db.migrate import migrate
from omniagentos.path_containment import inode_paths_equal

_RESERVED_CLAIM_STATES = frozenset({"IMPLEMENTING", "CLEANING"})
_IMPLEMENTATION_CLAIM_SOURCES = frozenset({"AWAITING_HUMAN", "DEFERRED"})
_ANALYSIS_FINISH_STATES = frozenset(
    {"AWAITING_HUMAN", "CANCELLED", "DEFERRED", "INCIDENT", "NO_CHANGE"}
)
_CLEANUP_TERMINAL_STATES = frozenset({"CANCELLED", "MERGED", "QUARANTINED", "REJECTED"})
_PUBLIC_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "ANALYZING": frozenset({"AWAITING_HUMAN", "DEFERRED", "CANCELLED", "INCIDENT"}),
    "AWAITING_HUMAN": frozenset({"DEFERRED", "REJECTED", "CANCELLED", "INCIDENT"}),
    "DEFERRED": frozenset({"AWAITING_HUMAN", "REJECTED", "CANCELLED", "INCIDENT"}),
    "AWAITING_MERGE": frozenset({"MERGED", "REJECTED", "CANCELLED", "QUARANTINED", "INCIDENT"}),
    "INCIDENT": frozenset({"REJECTED", "CANCELLED", "QUARANTINED"}),
    "REJECTED": frozenset({"CANCELLED", "QUARANTINED"}),
    "CANCELLED": frozenset({"QUARANTINED"}),
    "MERGED": frozenset({"INCIDENT", "QUARANTINED"}),
    "QUARANTINED": frozenset(),
}
_IMMUTABLE_IMPLEMENTATION_KEYS = frozenset(
    {
        "approval_binding",
        "approved_by",
        "base_sha",
        "branch",
        "committed_payload_sha256",
        "implementation_commit",
        "implementation_parent",
        "implementation_tree",
        "staged_index_sha256",
        "worktree",
        "written_paths",
    }
)
_INCIDENT_DIAGNOSTIC_KEYS = frozenset({"recovery_required"})
_IMPLEMENTATION_FINALIZATION_KEYS = frozenset(
    {
        "approved_by",
        "base_sha",
        "branch",
        "committed_payload_sha256",
        "implementation_commit",
        "implementation_parent",
        "implementation_tree",
        "staged_index_sha256",
        "worktree",
        "written_paths",
    }
)
_CLEANUP_RESULT_KEYS = frozenset({"cleanup_action", "cleanup_retained_reason"})
_CLEANUP_METADATA_KEYS = frozenset({"cleanup_claim", "cleanup_quarantine", *_CLEANUP_RESULT_KEYS})
_RESERVED_METADATA_KEYS = (
    _IMMUTABLE_IMPLEMENTATION_KEYS | _INCIDENT_DIAGNOSTIC_KEYS | _CLEANUP_METADATA_KEYS
)
_CLEANUP_ACTION_PARTS = frozenset(
    {
        "merged_branch_deleted",
        "nothing_to_clean",
        "worktree_quarantined",
        "worktree_registration_retired",
    }
)
_CLEANUP_RETAINED_REASONS = frozenset(
    {
        "",
        "safe_branch_delete_failed",
        "unmerged_branch_retained_for_recovery",
        "worktree_quarantine_retained_for_safe_unlink",
    }
)
_CLEANUP_JOURNAL_STATES = (
    "rename_intent_bound",
    "quarantined_bound",
    "bound_retained_at_persist",
)
_SOURCE_BINDING_FIELDS = (
    "approval_status",
    "approved_by",
    "approved_at",
    "codebase_sha",
    "evidence_json",
    "synthesis_json",
    "conflict_json",
)


def _parse_json_object(raw: object) -> dict[str, Any] | None:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _matches_full_metadata(
    observed_raw: object,
    replacement: dict[str, Any],
) -> bool:
    """Compare the complete JSON object while retaining its stored bytes."""

    observed = _parse_json_object(observed_raw)
    if observed is None:
        return False
    try:
        return json.dumps(
            replacement,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ) == json.dumps(
            observed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return False


def _is_typed_implementation_finalization(
    observed_row_or_raw: object,
    replacement: dict[str, Any],
    *,
    repo_root: str | Path | None = None,
) -> bool:
    """Allow the implementation owner to append only its complete provenance."""

    if isinstance(observed_row_or_raw, dict) and "codebase_sha" in observed_row_or_raw:
        observed_row = observed_row_or_raw
        observed_raw = observed_row.get("implement_json")
    else:
        observed_row = None
        observed_raw = observed_row_or_raw

    observed = _parse_json_object(observed_raw)
    if observed is None or any(
        key in observed
        for key in (
            "implementation_commit",
            "base_sha",
            "implementation_parent",
            "implementation_tree",
        )
    ):
        return False
    if any(replacement.get(key) != value for key, value in observed.items()):
        return False
    added_keys = set(replacement).difference(observed)
    expected_added = _IMPLEMENTATION_FINALIZATION_KEYS.difference(observed)
    if added_keys != expected_added:
        return False

    approved_by = replacement.get("approved_by")
    base_sha = replacement.get("base_sha")
    branch = replacement.get("branch")
    worktree = replacement.get("worktree")
    parent = replacement.get("implementation_parent")
    commit = replacement.get("implementation_commit")
    tree = replacement.get("implementation_tree")
    staged_index = replacement.get("staged_index_sha256")
    committed_payload = replacement.get("committed_payload_sha256")
    written_paths = replacement.get("written_paths")

    if observed_row is not None:
        expected_approved_by = str(observed_row.get("approved_by") or "")
        expected_base_sha = str(observed_row.get("codebase_sha") or "")
        if approved_by != expected_approved_by or base_sha != expected_base_sha:
            return False

    expected_branch = str(observed.get("branch") or "")
    expected_worktree = str(observed.get("worktree") or "")
    if (expected_branch and branch != expected_branch) or (
        expected_worktree and worktree != expected_worktree
    ):
        return False

    if not isinstance(base_sha, str) or re.fullmatch(r"[0-9a-f]{40}", base_sha) is None:
        return False
    if parent != base_sha:
        return False

    if (
        not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
        or commit == base_sha
        or not isinstance(tree, str)
        or re.fullmatch(r"[0-9a-f]{40}", tree) is None
        or not isinstance(branch, str)
        or not branch
        or not isinstance(worktree, str)
        or not worktree
    ):
        return False

    if (
        not isinstance(staged_index, str)
        or re.fullmatch(r"[0-9a-f]{64}", staged_index) is None
        or not isinstance(committed_payload, dict)
        or not isinstance(written_paths, list)
    ):
        return False

    for k, v in committed_payload.items():
        if (
            not isinstance(k, str)
            or not isinstance(v, str)
            or re.fullmatch(r"[0-9a-f]{64}", v) is None
        ):
            return False
    if set(written_paths) != set(committed_payload.keys()):
        return False

    wt_path = Path(worktree).resolve()
    repo_path = Path(repo_root or worktree or Path.cwd()).resolve()

    if not wt_path.is_dir() or not repo_path.is_dir():
        return False

    wt_gitfile = wt_path / ".git"
    if not wt_gitfile.is_file():
        return False
    gitfile_text = wt_gitfile.read_text("utf-8").strip()
    if not gitfile_text.startswith("gitdir: "):
        return False
    admin_path = Path(gitfile_text[len("gitdir: ") :].strip()).resolve()
    if not admin_path.is_dir():
        return False

    admin_gitfile = admin_path / "gitdir"
    if not admin_gitfile.is_file():
        return False
    if (
        # Safety (`is not True`): require a positively equal worktree backlink.
        inode_paths_equal(
            Path(admin_gitfile.read_text("utf-8").strip()).resolve(),
            wt_gitfile.resolve(),
        )
        is not True
    ):
        return False

    admin_head = admin_path / "HEAD"
    if not admin_head.is_file():
        return False
    if admin_head.read_text("utf-8").strip() != f"ref: refs/heads/{branch}":
        return False

    from omniagentos.csi.implement import (  # noqa: PLC0415
        _git_bytes,
        _git_common_directory,
        _git_text,
    )

    try:
        common_dir = _git_common_directory(repo_path)
        admin_commondir = admin_path / "commondir"
        if admin_commondir.is_file():
            cd_text = admin_commondir.read_text("utf-8").strip()
            resolved_cd = (admin_path / cd_text).resolve()
            # Safety (`is not True`): reject an unverified Git common directory.
            if inode_paths_equal(resolved_cd, common_dir) is not True:
                return False

        porcelain = _git_text(repo_path, "worktree", "list", "--porcelain")
        found_wt = False
        current_wt: str | None = None
        current_head: str | None = None
        current_branch: str | None = None

        for line in porcelain.splitlines():
            if line.startswith("worktree "):
                current_wt = line[len("worktree ") :].strip()
                current_head = None
                current_branch = None
            elif line.startswith("HEAD "):
                current_head = line[len("HEAD ") :].strip()
            elif line.startswith("branch "):
                current_branch = line[len("branch ") :].strip()
            if current_wt and current_head and current_branch:
                if (
                    # Safety (`is True`): accept only a positively equal listed worktree.
                    inode_paths_equal(Path(current_wt).resolve(), wt_path) is True
                    and current_head == commit
                    and current_branch == f"refs/heads/{branch}"
                ):
                    found_wt = True
                    break

        if not found_wt:
            return False

        commit_type = _git_text(repo_path, "cat-file", "-t", commit)
        if commit_type != "commit":
            return False
        commit_parent = _git_text(repo_path, "rev-parse", f"{commit}^")
        if commit_parent != base_sha:
            return False
        commit_tree = _git_text(repo_path, "rev-parse", f"{commit}^{{tree}}")
        if commit_tree != tree:
            return False
        branch_ref = _git_text(repo_path, "rev-parse", f"refs/heads/{branch}")
        if branch_ref != commit:
            return False

        diff_output = _git_text(
            repo_path, "diff-tree", "--no-commit-id", "--name-only", "-r", base_sha, commit
        )
        changed_in_commit = set(filter(None, diff_output.splitlines()))
        if changed_in_commit != set(written_paths):
            return False

        for rel in written_paths:
            blob_data = _git_bytes(repo_path, "cat-file", "blob", f"{commit}:{rel}")
            computed_payload_hash = hashlib.sha256(blob_data).hexdigest()
            if committed_payload.get(rel) != computed_payload_hash:
                return False

        wt_index_file = admin_path / "index"
        if not wt_index_file.is_file():
            return False
        index_data = wt_index_file.read_bytes()
        computed_index_hash = hashlib.sha256(index_data).hexdigest()
        if staged_index != computed_index_hash:
            return False

    except Exception:
        return False

    return True


def _matches_implementation_provenance(
    observed_raw: object,
    replacement: dict[str, Any],
) -> bool:
    """Require the reserved provenance projection to remain exactly unchanged."""

    observed = _parse_json_object(observed_raw)
    if observed is None:
        return False
    observed_provenance = {
        key: observed[key] for key in _IMMUTABLE_IMPLEMENTATION_KEYS if key in observed
    }
    replacement_provenance = {
        key: replacement[key] for key in _IMMUTABLE_IMPLEMENTATION_KEYS if key in replacement
    }
    return replacement_provenance == observed_provenance


def _is_unclaimed_approval_metadata(metadata: dict[str, Any]) -> bool:
    """Reject legacy provenance that could not predate implementation."""

    return not any(
        key in metadata for key in _IMMUTABLE_IMPLEMENTATION_KEYS if key != "approval_binding"
    )


def _is_append_only_incident_update(
    observed_raw: object,
    replacement: dict[str, Any],
) -> bool:
    """Allow only a typed diagnostic append while preserving authority exactly."""

    observed = _parse_json_object(observed_raw)
    if observed is None or not _matches_implementation_provenance(
        observed_raw,
        replacement,
    ):
        return False
    if any(replacement.get(key) != value for key, value in observed.items()):
        return False
    added = set(replacement).difference(observed)
    if not added.issubset(_INCIDENT_DIAGNOSTIC_KEYS):
        return False
    return "recovery_required" not in added or isinstance(replacement["recovery_required"], bool)


def _source_binding(row: dict[str, Any]) -> dict[str, Any]:
    """Capture the exact approval/source columns backing a state claim."""

    return {field: row.get(field) for field in _SOURCE_BINDING_FIELDS}


def _cleanup_claim_matches(
    metadata: dict[str, Any],
    *,
    restore_status: str,
    row: dict[str, Any],
) -> bool:
    return metadata.get("cleanup_claim") == {
        "restore_status": restore_status,
        "source_binding": _source_binding(row),
    }


def _valid_cleanup_registration(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "active_path",
        "retired_path",
        "device",
        "inode",
        "worktree",
        "branch",
        "state",
    }:
        return False
    return (
        all(
            isinstance(value.get(key), str) and bool(value.get(key))
            for key in (
                "active_path",
                "retired_path",
                "worktree",
                "branch",
            )
        )
        and all(
            isinstance(value.get(key), int) and not isinstance(value.get(key), bool)
            for key in ("device", "inode")
        )
        and value.get("state") in {"active_bound", "retired_bound"}
    )


def _valid_cleanup_journal(
    value: object,
    *,
    restore_status: str | None,
) -> bool:
    if not isinstance(value, dict):
        return False
    state = value.get("state")
    legacy = state == "bound_empty_at_persist" and "source_path" not in value
    expected_keys = {
        "path",
        "device",
        "inode",
        "state",
        "path_verification",
        "unlink_pending",
    }
    if not legacy or "restore_status" in value:
        expected_keys.add("restore_status")
    if not legacy:
        expected_keys.add("source_path")
    if "registration" in value:
        expected_keys.add("registration")
    if set(value) != expected_keys:
        return False
    if (
        state not in {*_CLEANUP_JOURNAL_STATES, "bound_empty_at_persist"}
        or value.get("unlink_pending") is not True
        or (restore_status is not None and value.get("restore_status") != restore_status)
        or (
            "restore_status" in value
            and value.get("restore_status") not in _CLEANUP_TERMINAL_STATES
        )
        or not isinstance(value.get("path"), str)
        or not value.get("path")
        or (
            not legacy
            and (not isinstance(value.get("source_path"), str) or not value.get("source_path"))
        )
        or not isinstance(value.get("path_verification"), str)
        or not value.get("path_verification")
        or len(str(value.get("path_verification"))) > 1_024
        or not isinstance(value.get("device"), int)
        or isinstance(value.get("device"), bool)
        or not isinstance(value.get("inode"), int)
        or isinstance(value.get("inode"), bool)
    ):
        return False
    registration = value.get("registration")
    if registration is not None and not _valid_cleanup_registration(registration):
        return False
    if legacy:
        return registration is None
    if registration is None:
        return False
    return not (
        state == "bound_retained_at_persist" and registration.get("state") != "retired_bound"
    )


def _cleanup_journal_transition_is_typed(
    previous: object,
    replacement: dict[str, Any],
    *,
    restore_status: str,
    run_row: dict[str, Any] | None = None,
    run_parsed: dict[str, Any] | None = None,
) -> bool:
    """Allow only monotonic, identity-preserving cleanup journal phases."""

    if not _valid_cleanup_journal(replacement, restore_status=restore_status):
        if previous is None:
            return False
    if previous is None:
        if run_parsed is None or run_row is None:
            return False
        if (
            replacement.get("state") != "rename_intent_bound"
            or replacement.get("path_verification") != "source_bound_before_rename"
        ):
            return False
        registration = replacement.get("registration")
        if not isinstance(registration, dict) or registration.get("state") != "active_bound":
            return False

        expected_worktree = str(run_parsed.get("worktree") or "")
        expected_branch = str(run_parsed.get("branch") or "")
        if not expected_worktree or not expected_branch:
            return False

        if (
            replacement.get("source_path") != expected_worktree
            or registration.get("worktree") != expected_worktree
            or registration.get("branch") != expected_branch
        ):
            return False

        try:
            source_path = Path(expected_worktree)
            if not source_path.is_dir():
                return False
            st = source_path.stat()
            if st.st_dev != replacement.get("device") or st.st_ino != replacement.get("inode"):
                return False

            source_gitfile = source_path / ".git"
            if not source_gitfile.is_file():
                return False
            gitfile_text = source_gitfile.read_text("utf-8").strip()
            if not gitfile_text.startswith("gitdir: "):
                return False
            admin_path = Path(gitfile_text[len("gitdir: ") :]).resolve()
            active_path = Path(registration.get("active_path") or "").resolve()
            # Safety (`is not True`): reject unless active registration is exact.
            if inode_paths_equal(active_path, admin_path) is not True:
                return False
            if not active_path.is_dir():
                return False
            admin_st = active_path.stat()
            if admin_st.st_dev != registration.get("device") or admin_st.st_ino != registration.get(
                "inode"
            ):
                return False

            gitdir_content = (active_path / "gitdir").read_text("utf-8").strip()
            head_content = (active_path / "HEAD").read_text("utf-8").strip()
            if (
                # Safety (`is not True`): require a positively equal source backlink.
                inode_paths_equal(
                    Path(gitdir_content).resolve(),
                    (source_path / ".git").resolve(),
                )
                is not True
            ):
                return False
            if head_content != f"ref: refs/heads/{expected_branch}":
                return False

            quarantine_path = Path(replacement.get("path") or "")
            # Safety (`is not True`): reject unless quarantine retains the source parent.
            if inode_paths_equal(quarantine_path.parent, source_path.parent) is not True:
                return False
            match_q = re.fullmatch(
                r"\.csi-cleanup-([0-9a-f]{32})\.quarantine", quarantine_path.name
            )
            if match_q is None:
                return False
            q_token = match_q.group(1)

            retired_path = Path(registration.get("retired_path") or "")
            if retired_path.name != f"{admin_path.name}-{q_token}.retired":
                return False
        except (OSError, PermissionError, ValueError):
            return False

        return True
    if not _valid_cleanup_journal(previous, restore_status=None):
        return False
    if not _valid_cleanup_journal(replacement, restore_status=None):
        return False
    assert isinstance(previous, dict)
    for key, value in previous.items():
        if key not in {"state", "path_verification", "registration"}:
            if replacement.get(key) != value:
                return False
    for key, value in replacement.items():
        if key not in {"state", "path_verification", "registration"}:
            if previous.get(key) != value:
                return False

    previous_state = str(previous.get("state") or "")
    replacement_state = str(replacement.get("state") or "")
    if previous_state == "bound_empty_at_persist":
        if replacement_state != previous_state:
            return False
    else:
        previous_index = _CLEANUP_JOURNAL_STATES.index(previous_state)
        replacement_index = _CLEANUP_JOURNAL_STATES.index(replacement_state)
        if replacement_index not in {previous_index, previous_index + 1}:
            return False

    previous_registration = previous.get("registration")
    replacement_registration = replacement.get("registration")
    if previous_registration is None:
        return replacement_registration is None
    if not isinstance(
        previous_registration,
        dict,
    ) or not isinstance(replacement_registration, dict):
        return False
    if any(
        replacement_registration.get(key) != value
        for key, value in previous_registration.items()
        if key != "state"
    ):
        return False
    registration_transition = (
        previous_registration.get("state"),
        replacement_registration.get("state"),
    )
    return registration_transition in {
        ("active_bound", "active_bound"),
        ("active_bound", "retired_bound"),
        ("retired_bound", "retired_bound"),
    }


def _typed_cleanup_finalization(
    observed: dict[str, Any],
    replacement: dict[str, Any],
) -> bool:
    """Remove only the claim and append one narrowly typed cleanup result."""

    expected = dict(observed)
    expected.pop("cleanup_claim", None)
    for key in _CLEANUP_RESULT_KEYS:
        expected.pop(key, None)
    replacement_base = {
        key: value for key, value in replacement.items() if key not in _CLEANUP_RESULT_KEYS
    }
    if replacement_base != expected:
        return False
    if not _CLEANUP_RESULT_KEYS.issubset(replacement):
        return False
    action = replacement.get("cleanup_action")
    retained_reason = replacement.get("cleanup_retained_reason")
    if not isinstance(action, str) or not isinstance(retained_reason, str):
        return False
    action_parts = action.split(",")
    if (
        not action_parts
        or any(part not in _CLEANUP_ACTION_PARTS for part in action_parts)
        or len(action_parts) != len(set(action_parts))
        or ("nothing_to_clean" in action_parts and len(action_parts) != 1)
        or retained_reason not in _CLEANUP_RETAINED_REASONS
    ):
        return False
    journal = replacement.get("cleanup_quarantine")
    if journal is not None:
        claim = observed.get("cleanup_claim")
        restore_status = str(claim.get("restore_status") or "") if isinstance(claim, dict) else ""
        if not _valid_cleanup_journal(journal, restore_status=restore_status):
            return False
    return True


def _json_digest(raw: object, *, field: str) -> str:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid_{field}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid_{field}")
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def approval_binding(row: dict[str, Any]) -> dict[str, str]:
    """Hash the exact evidence/decision bundle a human approved."""

    return {
        "codebase_sha": str(row.get("codebase_sha") or ""),
        "evidence_sha256": _json_digest(row.get("evidence_json"), field="evidence_json"),
        "synthesis_sha256": _json_digest(row.get("synthesis_json"), field="synthesis_json"),
        "conflict_sha256": _json_digest(row.get("conflict_json"), field="conflict_json"),
    }


class CsiStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        mig_dir = Path(__file__).resolve().parents[1] / "db" / "migrations"
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            migrate(self.db_path)
            self._conn = sqlite3.connect(self.db_path)
        else:
            self._conn = sqlite3.connect(":memory:")
            for name in ("066_csi_runs.sql", "067_csi_approval.sql"):
                sql = mig_dir / name
                if sql.is_file():
                    self._conn.executescript(sql.read_text(encoding="utf-8"))
        self._conn.row_factory = sqlite3.Row
        # Ensure approval columns exist even if 066 applied without 067
        try:
            self._conn.execute(
                "ALTER TABLE csi_runs ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'none'"
            )
        except sqlite3.OperationalError:
            pass
        for col, decl in (
            ("approved_by", "TEXT"),
            ("approved_at", "TEXT"),
            ("implement_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            try:
                self._conn.execute(f"ALTER TABLE csi_runs ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def rollback(self) -> None:
        """Roll back a failed local transaction before an invariant fallback."""

        self._conn.rollback()

    def _approved_merge_ready_observation(
        self,
        run_id: str,
        *,
        observed_implement_json: str,
    ) -> dict[str, Any] | None:
        """Return the row only when its whole approval/provenance bundle matches."""

        row = self.get_run(run_id)
        if (
            not row
            or str(row.get("status") or "") != "AWAITING_MERGE"
            or str(row.get("approval_status") or "") != "approved"
            or str(row.get("implement_json") or "{}") != observed_implement_json
        ):
            return None
        try:
            metadata = json.loads(observed_implement_json)
            if not isinstance(metadata, dict) or metadata.get(
                "approval_binding"
            ) != approval_binding(row):
                return None
        except (TypeError, ValueError):
            return None
        return row

    def _cas_approved_merge_ready_status(
        self,
        row: dict[str, Any],
        *,
        status: str,
        error: str | None,
    ) -> bool:
        """Transition one exact approval and implementation observation."""

        cur = self._conn.execute(
            "UPDATE csi_runs SET status=?, error=?, finished_at=? "
            "WHERE id=? AND status='AWAITING_MERGE' "
            "AND approval_status='approved' "
            "AND approved_by IS ? AND approved_at IS ? AND codebase_sha IS ? "
            "AND evidence_json=? AND synthesis_json=? AND conflict_json=? "
            "AND implement_json=?",
            (
                status,
                error,
                utc_now_iso(),
                row.get("id"),
                row.get("approved_by"),
                row.get("approved_at"),
                row.get("codebase_sha"),
                row.get("evidence_json"),
                row.get("synthesis_json"),
                row.get("conflict_json"),
                row.get("implement_json"),
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def create_run(
        self,
        *,
        routine_id: str,
        window_days: int,
        codebase_sha: str,
        evidence: dict[str, Any],
    ) -> str:
        run_id = new_id("csi")
        self._conn.execute(
            "INSERT INTO csi_runs ("
            "id, routine_id, status, window_days, codebase_sha, evidence_json, "
            "created_at"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                routine_id,
                "ANALYZING",
                window_days,
                codebase_sha,
                json.dumps(evidence),
                utc_now_iso(),
            ),
        )
        self._conn.commit()
        return run_id

    def save_plan(
        self,
        run_id: str,
        plan: PlannerPlan,
        *,
        status: str = "ok",
        latency_s: float | None = None,
        error: str | None = None,
    ) -> str:
        pid = new_id("cplan")
        self._conn.execute(
            "INSERT INTO csi_model_plans ("
            "id, run_id, planner, lineage, status, plan_json, latency_s, error, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?)",
            (
                pid,
                run_id,
                plan.planner,
                plan.lineage,
                status,
                plan.model_dump_json(),
                latency_s,
                error,
                utc_now_iso(),
            ),
        )
        self._conn.commit()
        return pid

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        verdict: str,
        no_change_reason: str = "",
        synthesis: SynthesisResult | None = None,
        conflict: dict[str, Any] | None = None,
        improvement_id: str | None = None,
        wall_clock_s: float = 0.0,
        error: str | None = None,
        approval_status: str | None = None,
    ) -> None:
        if status not in _ANALYSIS_FINISH_STATES:
            return
        # approval_status optional for older DBs mid-migration
        cols = (
            "status=?, verdict=?, no_change_reason=?, synthesis_json=?, conflict_json=?, "
            "improvement_id=?, wall_clock_s=?, error=?, finished_at=?"
        )
        vals: list[Any] = [
            status,
            verdict,
            no_change_reason,
            json.dumps(synthesis.model_dump() if synthesis else {}),
            json.dumps(conflict or {}),
            improvement_id,
            wall_clock_s,
            error,
            utc_now_iso(),
        ]
        if approval_status is not None:
            cols += ", approval_status=?"
            vals.append(approval_status)
        vals.append(run_id)
        self._conn.execute(
            f"UPDATE csi_runs SET {cols} WHERE id=? AND status='ANALYZING'",
            vals,
        )
        self._conn.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM csi_runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def set_approval(
        self,
        run_id: str,
        *,
        status: str,
        approved_by: str = "operator",
    ) -> bool:
        """CSI-native approve/reject — does not touch reliability improvements."""
        if status not in {"approved", "rejected", "proposed", "none"}:
            return False
        now = utc_now_iso() if status == "approved" else None
        try:
            row = self.get_run(run_id)
            if not row:
                return False
            implement_meta: dict[str, Any]
            try:
                parsed = json.loads(str(row.get("implement_json") or "{}"))
                implement_meta = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                implement_meta = {}
            if status == "approved":
                if str(row.get("status") or "") not in {
                    "AWAITING_HUMAN",
                    "DEFERRED",
                } or not _is_unclaimed_approval_metadata(implement_meta):
                    return False
                implement_meta["approval_binding"] = approval_binding(row)
            else:
                # Once an implementation record carries approval or Git
                # provenance, changing the approval decision would erase the
                # reviewer identity/binding that authorized those exact
                # objects. Terminal and recovery APIs must retain that record
                # byte-for-byte; a later disposition belongs in ``status``.
                # Pre-implementation revocation remains legal even when an
                # older release admitted suspicious provenance-shaped keys.
                # Preserve those keys for diagnosis, but never let the row
                # re-enter an implementation claim.
                if str(row.get("status") or "") not in {
                    "ANALYZING",
                    "AWAITING_HUMAN",
                    "DEFERRED",
                }:
                    return False
                implement_meta.pop("approval_binding", None)
            if status == "approved":
                cur = self._conn.execute(
                    "UPDATE csi_runs SET approval_status=?, approved_by=?, approved_at=?, "
                    "implement_json=? WHERE id=? AND status=? "
                    "AND codebase_sha IS ? "
                    "AND evidence_json=? AND synthesis_json=? AND conflict_json=? "
                    "AND implement_json=?",
                    (
                        status,
                        approved_by,
                        now,
                        json.dumps(implement_meta, sort_keys=True),
                        run_id,
                        row.get("status"),
                        row.get("codebase_sha"),
                        row.get("evidence_json"),
                        row.get("synthesis_json"),
                        row.get("conflict_json"),
                        row.get("implement_json"),
                    ),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE csi_runs SET approval_status=?, approved_by=?, approved_at=?, "
                    "implement_json=? WHERE id=? AND status=? "
                    "AND codebase_sha IS ? "
                    "AND evidence_json=? AND synthesis_json=? AND conflict_json=? "
                    "AND implement_json=? "
                    "AND status NOT IN ('IMPLEMENTING','CLEANING','AWAITING_MERGE')",
                    (
                        status,
                        None,
                        now,
                        json.dumps(implement_meta, sort_keys=True),
                        run_id,
                        row.get("status"),
                        row.get("codebase_sha"),
                        row.get("evidence_json"),
                        row.get("synthesis_json"),
                        row.get("conflict_json"),
                        row.get("implement_json"),
                    ),
                )
            self._conn.commit()
            return cur.rowcount > 0
        except (sqlite3.Error, ValueError):
            # Column may be missing pre-067
            return False

    def count_applies_today(self) -> int:
        """Fail closed: raise on DB error so implement can refuse."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM csi_runs "
            "WHERE status IN ('AWAITING_MERGE','MERGED') "
            "AND date(COALESCE(finished_at, created_at)) = date('now')"
        ).fetchone()
        return int(row[0] if row else 0)

    def set_status(
        self,
        run_id: str,
        status: str,
        *,
        implement_json: dict[str, Any] | None = None,
        error: str | None = None,
        repo_root: str | Path | None = None,
    ) -> bool:
        """CAS a public status transition without weakening merge provenance.

        Every transition preserves the stored implementation record byte-for-
        byte. If a caller supplies that record, its complete canonical JSON
        object must match the current observation; it is never used as an update
        payload. ``AWAITING_MERGE`` additionally requires the exact approved
        record and a live Git proof before leaving merge readiness.
        """

        if status == "AWAITING_MERGE":
            raise ValueError("AWAITING_MERGE is reserved for fenced finalization")
        if status in _RESERVED_CLAIM_STATES:
            return False
        row = self.get_run(run_id)
        if not row:
            return False
        observed_status = str(row.get("status") or "")
        if observed_status in _RESERVED_CLAIM_STATES:
            return False
        if status not in _PUBLIC_STATUS_TRANSITIONS.get(observed_status, frozenset()):
            return False
        observed_json = str(row.get("implement_json") or "{}")
        if implement_json is not None and not _matches_full_metadata(
            observed_json,
            implement_json,
        ):
            return False
        if observed_status == "AWAITING_MERGE":
            if implement_json is None:
                return False
            approved_row = self._approved_merge_ready_observation(
                run_id,
                observed_implement_json=observed_json,
            )
            if approved_row is None:
                return False
            if repo_root is None:
                return False
            # Local import avoids a module-load cycle: implement imports
            # CsiStore, while this transition needs its Git ref guard.
            from omniagentos.csi.implement import (  # noqa: PLC0415
                _transition_validated_merge_ready_status,
            )

            return _transition_validated_merge_ready_status(
                store=self,
                run_id=run_id,
                root=Path(repo_root).resolve(),
                observed_implement_json=observed_json,
                status=status,
                error=error,
            )
        cur = self._conn.execute(
            "UPDATE csi_runs SET status=?, error=?, finished_at=? "
            "WHERE id=? AND status=? AND implement_json=?",
            (
                status,
                error,
                utc_now_iso(),
                run_id,
                observed_status,
                observed_json,
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def claim_implementation(
        self,
        run_id: str,
        *,
        expected_status: str,
        observed: dict[str, Any],
    ) -> bool:
        """Atomically fence plan/apply state immediately before git mutation."""

        if expected_status not in _IMPLEMENTATION_CLAIM_SOURCES:
            return False
        metadata = _parse_json_object(observed.get("implement_json"))
        if metadata is None or not _is_unclaimed_approval_metadata(metadata):
            return False
        try:
            if metadata.get("approval_binding") != approval_binding(observed):
                return False
        except ValueError:
            return False
        cur = self._conn.execute(
            "UPDATE csi_runs SET status='IMPLEMENTING', error=NULL WHERE id=? "
            "AND status=? AND approval_status='approved' "
            "AND approved_by IS ? AND approved_at IS ? AND codebase_sha IS ? "
            "AND evidence_json=? AND synthesis_json=? AND conflict_json=? "
            "AND implement_json=?",
            (
                run_id,
                expected_status,
                observed.get("approved_by"),
                observed.get("approved_at"),
                observed.get("codebase_sha"),
                observed.get("evidence_json"),
                observed.get("synthesis_json"),
                observed.get("conflict_json"),
                observed.get("implement_json"),
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def release_implementation_claim(
        self,
        run_id: str,
        *,
        restore_status: str,
        error: str,
        observed: dict[str, Any] | None = None,
    ) -> bool:
        if restore_status not in _IMPLEMENTATION_CLAIM_SOURCES or observed is None:
            return False
        cur = self._conn.execute(
            "UPDATE csi_runs SET status=?, error=? WHERE id=? AND status='IMPLEMENTING' "
            "AND approval_status='approved' "
            "AND approved_by IS ? AND approved_at IS ? AND codebase_sha IS ? "
            "AND evidence_json=? AND synthesis_json=? AND conflict_json=? "
            "AND implement_json=?",
            (
                restore_status,
                error,
                run_id,
                observed.get("approved_by"),
                observed.get("approved_at"),
                observed.get("codebase_sha"),
                observed.get("evidence_json"),
                observed.get("synthesis_json"),
                observed.get("conflict_json"),
                observed.get("implement_json"),
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def finalize_implementation_claim(
        self,
        run_id: str,
        *,
        observed: dict[str, Any],
        implement_json: dict[str, Any],
        repo_root: str | Path | None = None,
    ) -> bool:
        """Finalize only while the exact approved claim is still current."""

        if not _is_typed_implementation_finalization(
            observed,
            implement_json,
            repo_root=repo_root,
        ):
            return False
        cur = self._conn.execute(
            "UPDATE csi_runs SET status='AWAITING_MERGE', implement_json=?, "
            "error=NULL, finished_at=? WHERE id=? AND status='IMPLEMENTING' "
            "AND approval_status='approved' "
            "AND approved_by IS ? AND approved_at IS ? AND codebase_sha IS ? "
            "AND evidence_json=? AND synthesis_json=? AND conflict_json=? "
            "AND implement_json=?",
            (
                json.dumps(implement_json, sort_keys=True),
                utc_now_iso(),
                run_id,
                observed.get("approved_by"),
                observed.get("approved_at"),
                observed.get("codebase_sha"),
                observed.get("evidence_json"),
                observed.get("synthesis_json"),
                observed.get("conflict_json"),
                observed.get("implement_json"),
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def invalidate_finalized_implementation(
        self,
        run_id: str,
        *,
        observed_implement_json: dict[str, Any] | str,
        error: str,
    ) -> bool:
        """CAS merge readiness to incident while preserving exact provenance."""

        observed_json = (
            observed_implement_json
            if isinstance(observed_implement_json, str)
            else json.dumps(observed_implement_json, sort_keys=True)
        )
        approved_row = self._approved_merge_ready_observation(
            run_id,
            observed_implement_json=observed_json,
        )
        if approved_row is None:
            return False
        return self._cas_approved_merge_ready_status(
            approved_row,
            status="INCIDENT",
            error=error,
        )

    def merge_readiness_is_current(
        self,
        run_id: str,
        *,
        observed_implement_json: str,
    ) -> bool:
        """Validate the exact immutable metadata backing merge readiness."""

        row = self._conn.execute(
            "SELECT 1 FROM csi_runs WHERE id=? AND status='AWAITING_MERGE' "
            "AND approval_status='approved' AND implement_json=?",
            (run_id, observed_implement_json),
        ).fetchone()
        return row is not None

    def force_implementation_incident(
        self,
        run_id: str,
        *,
        observed_implement_json: dict[str, Any] | str,
        error: str,
    ) -> bool:
        """Retry an exact-provenance incident CAS after a local DB failure."""

        observed_json = (
            observed_implement_json
            if isinstance(observed_implement_json, str)
            else json.dumps(observed_implement_json, sort_keys=True)
        )
        approved_row = self._approved_merge_ready_observation(
            run_id,
            observed_implement_json=observed_json,
        )
        if approved_row is None:
            return False
        return self._cas_approved_merge_ready_status(
            approved_row,
            status="INCIDENT",
            error=error,
        )

    def mark_implementation_incident(
        self,
        run_id: str,
        *,
        observed: dict[str, Any] | None = None,
        implement_json: dict[str, Any] | None = None,
        error: str,
    ) -> bool:
        """Append one diagnostic without manufacturing implementation authority."""

        row = self.get_run(run_id)
        observed_json = (
            str(observed.get("implement_json"))
            if observed is not None and observed.get("implement_json") is not None
            else "{}"
        )
        serialized_update = observed_json
        if implement_json is not None:
            if not _is_append_only_incident_update(observed_json, implement_json):
                return False
            serialized_update = json.dumps(implement_json, sort_keys=True)
        if (
            row is None
            or observed is None
            or str(row.get("status") or "") != "IMPLEMENTING"
            or row.get("approval_status") != observed.get("approval_status")
            or row.get("approved_by") != observed.get("approved_by")
            or row.get("approved_at") != observed.get("approved_at")
            or row.get("codebase_sha") != observed.get("codebase_sha")
            or row.get("evidence_json") != observed.get("evidence_json")
            or row.get("synthesis_json") != observed.get("synthesis_json")
            or row.get("conflict_json") != observed.get("conflict_json")
            or row.get("implement_json") != observed.get("implement_json")
        ):
            return False
        cur = self._conn.execute(
            "UPDATE csi_runs SET status='INCIDENT', implement_json=?, error=?, "
            "finished_at=? WHERE id=? AND status='IMPLEMENTING' "
            "AND approval_status=? "
            "AND approved_by IS ? AND approved_at IS ? AND codebase_sha IS ? "
            "AND evidence_json=? AND synthesis_json=? AND conflict_json=? "
            "AND implement_json=?",
            (
                serialized_update,
                error,
                utc_now_iso(),
                run_id,
                observed.get("approval_status"),
                observed.get("approved_by"),
                observed.get("approved_at"),
                observed.get("codebase_sha"),
                observed.get("evidence_json"),
                observed.get("synthesis_json"),
                observed.get("conflict_json"),
                observed.get("implement_json"),
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def patch_implement_json(
        self,
        run_id: str,
        patch: dict[str, Any],
    ) -> bool:
        row = self.get_run(run_id)
        if (
            not row
            or str(row.get("status") or "") not in {"ANALYZING", "AWAITING_HUMAN", "DEFERRED"}
            or set(patch).intersection(_RESERVED_METADATA_KEYS)
        ):
            return False
        current = _parse_json_object(row.get("implement_json"))
        if current is None:
            return False
        current.update(patch)
        observed_json = row.get("implement_json")
        cur = self._conn.execute(
            "UPDATE csi_runs SET implement_json=? WHERE id=? AND implement_json=? "
            "AND status IN ('ANALYZING','AWAITING_HUMAN','DEFERRED')",
            (
                json.dumps(current, sort_keys=True),
                run_id,
                observed_json,
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def claim_cleanup(
        self,
        run_id: str,
        *,
        expected_status: str,
        observed_implement_json: str,
    ) -> str | None:
        if expected_status not in _CLEANUP_TERMINAL_STATES:
            return None
        row = self.get_run(run_id)
        metadata = _parse_json_object(observed_implement_json)
        try:
            if (
                row is None
                or str(row.get("status") or "") != expected_status
                or str(row.get("approval_status") or "") != "approved"
                or str(row.get("implement_json") or "") != observed_implement_json
                or metadata is None
                or "cleanup_claim" in metadata
                or metadata.get("approval_binding") != approval_binding(row)
            ):
                return None
        except ValueError:
            return None
        claimed_metadata = dict(metadata)
        claimed_metadata["cleanup_claim"] = {
            "restore_status": expected_status,
            "source_binding": _source_binding(row),
        }
        claimed_json = json.dumps(claimed_metadata, sort_keys=True)
        cur = self._conn.execute(
            "UPDATE csi_runs SET status='CLEANING', implement_json=?, error=NULL WHERE id=? "
            "AND status=? AND approval_status='approved' "
            "AND approved_by IS ? AND approved_at IS ? AND codebase_sha IS ? "
            "AND evidence_json=? AND synthesis_json=? AND conflict_json=? "
            "AND implement_json=?",
            (
                claimed_json,
                run_id,
                expected_status,
                row.get("approved_by"),
                row.get("approved_at"),
                row.get("codebase_sha"),
                row.get("evidence_json"),
                row.get("synthesis_json"),
                row.get("conflict_json"),
                observed_implement_json,
            ),
        )
        self._conn.commit()
        return claimed_json if cur.rowcount == 1 else None

    def cleanup_claim_is_current(
        self,
        run_id: str,
        *,
        observed_implement_json: str,
    ) -> bool:
        row = self.get_run(run_id)
        metadata = _parse_json_object(observed_implement_json)
        cleanup_claim = metadata.get("cleanup_claim") if isinstance(metadata, dict) else None
        restore_status = (
            str(cleanup_claim.get("restore_status") or "")
            if isinstance(cleanup_claim, dict)
            else ""
        )
        try:
            return bool(
                row is not None
                and str(row.get("status") or "") == "CLEANING"
                and str(row.get("approval_status") or "") == "approved"
                and str(row.get("implement_json") or "") == observed_implement_json
                and metadata is not None
                and metadata.get("approval_binding") == approval_binding(row)
                and _cleanup_claim_matches(
                    metadata,
                    restore_status=restore_status,
                    row=row,
                )
            )
        except ValueError:
            return False

    def journal_cleanup_quarantine(
        self,
        run_id: str,
        *,
        observed_implement_json: str,
        cleanup_quarantine: dict[str, Any],
    ) -> str | None:
        """Persist an inode-bound cleanup pointer without releasing the claim."""

        try:
            parsed = json.loads(observed_implement_json)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        row = self.get_run(run_id)
        cleanup_claim = parsed.get("cleanup_claim")
        restore_status = (
            str(cleanup_claim.get("restore_status") or "")
            if isinstance(cleanup_claim, dict)
            else ""
        )
        try:
            if (
                row is None
                or str(row.get("status") or "") != "CLEANING"
                or str(row.get("approval_status") or "") != "approved"
                or str(row.get("implement_json") or "") != observed_implement_json
                or parsed.get("approval_binding") != approval_binding(row)
                or not _cleanup_claim_matches(
                    parsed,
                    restore_status=restore_status,
                    row=row,
                )
            ):
                return None
        except ValueError:
            return None
        if not _cleanup_journal_transition_is_typed(
            parsed.get("cleanup_quarantine"),
            cleanup_quarantine,
            restore_status=restore_status,
            run_row=row,
            run_parsed=parsed,
        ):
            return None
        updated = dict(parsed)
        updated["cleanup_quarantine"] = cleanup_quarantine
        serialized = json.dumps(updated, sort_keys=True)
        cur = self._conn.execute(
            "UPDATE csi_runs SET implement_json=? WHERE id=? AND status='CLEANING' "
            "AND approval_status='approved' "
            "AND approved_by IS ? AND approved_at IS ? AND codebase_sha IS ? "
            "AND evidence_json=? AND synthesis_json=? AND conflict_json=? "
            "AND implement_json=?",
            (
                serialized,
                run_id,
                row.get("approved_by"),
                row.get("approved_at"),
                row.get("codebase_sha"),
                row.get("evidence_json"),
                row.get("synthesis_json"),
                row.get("conflict_json"),
                observed_implement_json,
            ),
        )
        self._conn.commit()
        return serialized if cur.rowcount == 1 else None

    def release_cleanup_claim(
        self,
        run_id: str,
        *,
        restore_status: str,
        observed_implement_json: str,
        error: str | None = None,
    ) -> bool:
        if restore_status not in _CLEANUP_TERMINAL_STATES:
            return False
        row = self.get_run(run_id)
        metadata = _parse_json_object(observed_implement_json)
        if (
            row is None
            or str(row.get("status") or "") != "CLEANING"
            or str(row.get("approval_status") or "") != "approved"
            or str(row.get("implement_json") or "") != observed_implement_json
            or metadata is None
            or not _cleanup_claim_matches(
                metadata,
                restore_status=restore_status,
                row=row,
            )
        ):
            return False
        try:
            if metadata.get("approval_binding") != approval_binding(row):
                return False
        except ValueError:
            return False
        released_metadata = dict(metadata)
        released_metadata.pop("cleanup_claim", None)
        cur = self._conn.execute(
            "UPDATE csi_runs SET status=?, implement_json=?, error=? "
            "WHERE id=? AND status='CLEANING' "
            "AND approval_status='approved' "
            "AND approved_by IS ? AND approved_at IS ? AND codebase_sha IS ? "
            "AND evidence_json=? AND synthesis_json=? AND conflict_json=? "
            "AND implement_json=?",
            (
                restore_status,
                json.dumps(released_metadata, sort_keys=True),
                error,
                run_id,
                row.get("approved_by"),
                row.get("approved_at"),
                row.get("codebase_sha"),
                row.get("evidence_json"),
                row.get("synthesis_json"),
                row.get("conflict_json"),
                observed_implement_json,
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def finalize_cleanup_claim(
        self,
        run_id: str,
        *,
        restore_status: str,
        observed_implement_json: str,
        implement_json: dict[str, Any],
        repo_root: str | Path | None = None,
    ) -> bool:
        if restore_status not in _CLEANUP_TERMINAL_STATES or repo_root is None:
            return False
        row = self.get_run(run_id)
        observed_metadata = _parse_json_object(observed_implement_json)
        if (
            row is None
            or str(row.get("status") or "") != "CLEANING"
            or str(row.get("approval_status") or "") != "approved"
            or str(row.get("implement_json") or "") != observed_implement_json
            or observed_metadata is None
            or not _cleanup_claim_matches(
                observed_metadata,
                restore_status=restore_status,
                row=row,
            )
        ):
            return False
        try:
            if observed_metadata.get("approval_binding") != approval_binding(row):
                return False
        except ValueError:
            return False
        if not _typed_cleanup_finalization(observed_metadata, implement_json):
            return False
        # The database can validate metadata authority but cannot prove that a
        # registered worktree was actually retired. Require the cleanup owner
        # to re-observe the exact repository state before the terminal CAS.
        from omniagentos.csi.implement import (  # noqa: PLC0415
            _cleanup_finalization_matches_repository,
        )

        if not _cleanup_finalization_matches_repository(
            root=Path(repo_root).resolve(),
            run_id=run_id,
            metadata=implement_json,
        ):
            return False
        cur = self._conn.execute(
            "UPDATE csi_runs SET status=?, implement_json=?, error=NULL, finished_at=? "
            "WHERE id=? AND status='CLEANING' AND approval_status='approved' "
            "AND approved_by IS ? AND approved_at IS ? AND codebase_sha IS ? "
            "AND evidence_json=? AND synthesis_json=? AND conflict_json=? "
            "AND implement_json=?",
            (
                restore_status,
                json.dumps(implement_json, sort_keys=True),
                utc_now_iso(),
                run_id,
                row.get("approved_by"),
                row.get("approved_at"),
                row.get("codebase_sha"),
                row.get("evidence_json"),
                row.get("synthesis_json"),
                row.get("conflict_json"),
                observed_implement_json,
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def mark_cleanup_incident(
        self,
        run_id: str,
        *,
        observed_implement_json: str,
        error: str,
    ) -> bool:
        """Make an interrupted cleanup durably non-active without touching files."""

        row = self.get_run(run_id)
        if (
            row is None
            or str(row.get("status") or "") != "CLEANING"
            or str(row.get("implement_json") or "") != observed_implement_json
        ):
            return False
        cur = self._conn.execute(
            "UPDATE csi_runs SET status='INCIDENT', error=?, finished_at=? "
            "WHERE id=? AND status='CLEANING' AND approval_status=? "
            "AND approved_by IS ? AND approved_at IS ? AND codebase_sha IS ? "
            "AND evidence_json=? AND synthesis_json=? AND conflict_json=? "
            "AND implement_json=?",
            (
                error,
                utc_now_iso(),
                run_id,
                row.get("approval_status"),
                row.get("approved_by"),
                row.get("approved_at"),
                row.get("codebase_sha"),
                row.get("evidence_json"),
                row.get("synthesis_json"),
                row.get("conflict_json"),
                observed_implement_json,
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1


__all__ = ["CsiStore", "approval_binding"]
