"""Conflict forecasting against current branches and reviewer worktrees."""

from __future__ import annotations

import subprocess
from pathlib import Path

from omniagentos.csi.frozen import repo_relative
from omniagentos.csi.models import ConflictForecast, PlannerProposal
from omniagentos.path_containment import inode_paths_equal

_ROOT = Path(__file__).resolve().parents[2]


def _run_git(
    root: Path,
    *args: str,
    timeout: int = 15,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _worktree_paths(root: Path) -> tuple[list[str], str | None]:
    try:
        out = _run_git(root, "worktree", "list", "--porcelain")
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"worktree_observation_failed:{exc}"
    if out.returncode != 0:
        detail = (out.stderr or out.stdout or "unknown")[:200]
        return [], f"worktree_observation_failed:{detail}"
    paths = []
    for line in (out.stdout or "").splitlines():
        if line.startswith("worktree "):
            paths.append(line.split(" ", 1)[1].strip())
    return paths, None


class ConflictForecastService:
    """Predict conflicts with the base checkout and active reviewer worktrees."""

    def __init__(self, *, repo_root: str | Path | None = None) -> None:
        self.repo_root = Path(repo_root or _ROOT).resolve()

    def _changed_target_paths(
        self,
        worktree: Path,
        proposed_paths: list[str],
        *,
        base_sha: str | None,
    ) -> tuple[set[str], str | None]:
        if not proposed_paths:
            return set(), None
        changed: set[str] = set()
        try:
            status = _run_git(
                worktree,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--",
                *proposed_paths,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return set(), f"reviewer_observation_failed:{worktree}:{exc}"
        if status.returncode != 0:
            detail = (status.stderr or status.stdout or "unknown")[:160]
            return set(), f"reviewer_observation_failed:{worktree}:{detail}"
        records = status.stdout.split("\0")
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            candidates = [record[3:] if len(record) > 3 else ""]
            if record[:2].strip()[:1] in {"R", "C"} and index < len(records):
                candidates.append(records[index])
                index += 1
            for candidate in candidates:
                try:
                    rel = repo_relative(candidate)
                except PermissionError:
                    continue
                if rel in proposed_paths:
                    changed.add(rel)

        if base_sha:
            try:
                diff = _run_git(
                    worktree,
                    "diff",
                    "--name-only",
                    "-z",
                    f"{base_sha}...HEAD",
                    "--",
                    *proposed_paths,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return changed, f"reviewer_diff_failed:{worktree}:{exc}"
            if diff.returncode != 0:
                detail = (diff.stderr or diff.stdout or "unknown")[:160]
                return changed, f"reviewer_diff_failed:{worktree}:{detail}"
            for candidate in diff.stdout.split("\0"):
                if not candidate:
                    continue
                try:
                    rel = repo_relative(candidate)
                except PermissionError:
                    continue
                if rel in proposed_paths:
                    changed.add(rel)
        return changed, None

    def forecast(
        self,
        proposals: list[PlannerProposal],
        *,
        active_user_paths: list[str] | None = None,
        base_sha: str | None = None,
        ignore_paths: tuple[str, ...] | list[str] = (),
    ) -> ConflictForecast:
        reasons: list[str] = []
        if active_user_paths is None:
            worktrees, observation_error = _worktree_paths(self.repo_root)
            if observation_error:
                return ConflictForecast(
                    safe_to_implement=False,
                    reasons=[observation_error],
                )
        else:
            worktrees = active_user_paths

        ignored = tuple(Path(p).resolve() for p in ignore_paths)
        root_text = str(self.repo_root)
        user_wts = [
            w
            for w in worktrees
            # Safety (`is not True`): unknown main identity stays conflict-relevant.
            if inode_paths_equal(Path(w).resolve(), self.repo_root) is not True
            # Safety (`is True`): ignore only positive equality; unknown remains active.
            and not any(inode_paths_equal(Path(w).resolve(), item) is True for item in ignored)
        ]
        if len(user_wts) >= 3:
            reasons.append(f"many_active_worktrees={len(user_wts)}")

        proposed_paths: list[str] = []
        try:
            proposed_paths = sorted(
                {repo_relative(p) for prop in proposals for p in prop.affected_paths}
            )
        except PermissionError as exc:
            return ConflictForecast(
                safe_to_implement=False,
                reasons=[f"invalid_proposed_path:{exc}"],
            )

        overlapping: set[str] = set()
        # The base checkout can contain uncommitted reviewer edits too.
        candidates = [root_text, *user_wts]
        for raw_wt in candidates:
            wt = Path(raw_wt)
            if not wt.is_dir():
                return ConflictForecast(
                    safe_to_implement=False,
                    reasons=[f"worktree_missing_during_revalidation:{wt}"],
                    active_user_worktrees=user_wts[:20],
                )
            changed, error = self._changed_target_paths(
                wt,
                proposed_paths,
                # Safety (`is not True`): unknown main identity keeps the stricter base diff.
                base_sha=base_sha
                if inode_paths_equal(wt.resolve(), self.repo_root) is not True
                else None,
            )
            if error:
                return ConflictForecast(
                    safe_to_implement=False,
                    reasons=[error],
                    active_user_worktrees=user_wts[:20],
                    overlapping_paths=sorted(overlapping),
                )
            overlapping.update(changed)

        if overlapping:
            reasons.append("reviewer_edits_overlap_proposal")
        if any(p.startswith("dashboard/") for p in proposed_paths) and user_wts:
            reasons.append("dashboard_change_with_active_worktrees")

        skill_only = bool(proposed_paths) and all(p.startswith("vault/") for p in proposed_paths)
        blocking_reasons = [
            reason
            for reason in reasons
            if reason != f"many_active_worktrees={len(user_wts)}" or not skill_only
        ]
        safe = not blocking_reasons
        if not proposed_paths:
            safe = False
            reasons.append("no_paths_to_conflict")
        return ConflictForecast(
            safe_to_implement=safe,
            reasons=reasons,
            active_user_worktrees=user_wts[:20],
            overlapping_paths=sorted(overlapping),
        )


__all__ = ["ConflictForecastService"]
