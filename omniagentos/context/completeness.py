"""Completeness checks over a ContextPackage.

Runs six independent checks unconditionally; one failure never masks another.
No warning severity: a failure is a failure.

Authorization path-boundary decisions use
``omniagentos.path_containment.inode_relative_parts`` (absolute paths directly;
logical relative package sources under a private absolute root so ``..`` is
filesystem-true). Other checks remain pure (no I/O).
"""

from __future__ import annotations

import fnmatch
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from omniagentos.context.package import ContextItem, ContextPackage
from omniagentos.path_containment import inode_relative_parts

__all__ = [
    "CHECK_NAMES",
    "CheckResult",
    "CompletenessVerdict",
    "FreshnessPolicy",
    "evaluate",
]

CHECK_NAMES: tuple[str, ...] = (
    "presence",
    "freshness",
    "consistency",
    "authorization",
    "token_budget",
    "acknowledgment",
)

_SKEW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    max_age_seconds: int = 86_400
    critical_max_age_seconds: int = 3_600


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    failed_item_ids: tuple[str, ...] = ()
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "failed_item_ids": list(self.failed_item_ids),
            "detail": self.detail,
            "data": dict(self.data),
        }


@dataclass(frozen=True, slots=True)
class CompletenessVerdict:
    ready: bool
    checks: tuple[CheckResult, ...]

    def check(self, name: str) -> CheckResult:
        for c in self.checks:
            if c.name == name:
                return c
        raise KeyError(name)

    def failed_checks(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.checks if not c.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "checks": [c.to_dict() for c in self.checks],
        }


def evaluate(
    package: ContextPackage,
    *,
    contract_required_ids: Sequence[str],
    token_budget: int,
    now: datetime,
    read_set: Sequence[str],
    authorized_secret_ids: Sequence[str] = (),
    policy: FreshnessPolicy | None = None,
    safety_margin: float = 0.1,
) -> CompletenessVerdict:
    """Run all six completeness checks independently; ready iff all pass."""
    if not (0.0 <= safety_margin < 1.0):
        raise ValueError(f"safety_margin must be in [0.0, 1.0), got {safety_margin!r}")
    if token_budget < 0:
        raise ValueError(f"token_budget must be non-negative, got {token_budget!r}")

    pol = policy if policy is not None else FreshnessPolicy()
    now_utc = _as_utc(now)

    checks: list[CheckResult] = [
        _check_presence(package, contract_required_ids),
        _check_freshness(package, now_utc, pol),
        _check_consistency(package),
        _check_authorization(package, read_set, authorized_secret_ids),
        _check_token_budget(package, token_budget, safety_margin),
        _check_acknowledgment(package),
    ]
    # Guarantee CHECK_NAMES order and exact length.
    by_name = {c.name: c for c in checks}
    ordered = tuple(by_name[name] for name in CHECK_NAMES)
    ready = all(c.passed for c in ordered)
    return CompletenessVerdict(ready=ready, checks=ordered)


def _check_presence(
    package: ContextPackage,
    contract_required_ids: Sequence[str],
) -> CheckResult:
    present = package.item_ids()
    missing = sorted({rid for rid in contract_required_ids if rid not in present})
    if not missing:
        return CheckResult(name="presence", passed=True, detail="all contract-required ids present")
    detail = f"missing contract-required artifact(s): {', '.join(missing)}"
    return CheckResult(
        name="presence",
        passed=False,
        failed_item_ids=tuple(missing),
        detail=detail,
    )


def _check_freshness(
    package: ContextPackage,
    now: datetime,
    policy: FreshnessPolicy,
) -> CheckResult:
    failed: list[str] = []
    stale_labeled: list[str] = []
    details: list[str] = []

    for item in package.items:
        age_ok, reason, is_stale_labeled = _item_freshness(item, now, policy)
        if is_stale_labeled:
            stale_labeled.append(item.id)
            details.append(f"{item.id}: accepted stale (stale_labeled)")
        if not age_ok:
            failed.append(item.id)
            details.append(f"{item.id}: {reason}")

    failed_sorted = tuple(sorted(set(failed)))
    stale_sorted = sorted(set(stale_labeled))
    data: dict[str, Any] = {}
    if stale_sorted:
        data["stale_labeled"] = stale_sorted

    if failed_sorted:
        detail = "; ".join(details) if details else "freshness failures"
        return CheckResult(
            name="freshness",
            passed=False,
            failed_item_ids=failed_sorted,
            detail=detail,
            data=data,
        )

    detail = (
        "; ".join(details)
        if details
        else "all items within freshness policy (or accepted stale_labeled)"
    )
    return CheckResult(name="freshness", passed=True, detail=detail, data=data)


def _item_freshness(
    item: ContextItem,
    now: datetime,
    policy: FreshnessPolicy,
) -> tuple[bool, str, bool]:
    """Return (ok, reason, is_accepted_stale_labeled)."""
    raw = (item.freshness or "").strip()
    if not raw:
        return False, "empty or unparseable freshness", False

    parsed = _parse_iso8601(raw)
    if parsed is None:
        return False, "empty or unparseable freshness", False

    # Future beyond 60s skew → fail closed.
    if parsed > now + timedelta(seconds=_SKEW_SECONDS):
        return False, "freshness timestamp is in the future beyond skew", False

    age_seconds = (now - parsed).total_seconds()
    # Negative age within skew is fine (clock skew).
    if age_seconds < 0:
        age_seconds = 0.0

    limit = policy.critical_max_age_seconds if item.critical else policy.max_age_seconds
    over_age = age_seconds > limit
    if not over_age:
        return True, "", False

    if item.critical:
        # Stale label never rescues a critical input.
        return (
            False,
            f"critical over-age ({age_seconds:.0f}s > {limit}s), stale_labeled ignored",
            False,
        )

    if item.stale_labeled:
        return True, "", True

    return False, f"unlabeled stale ({age_seconds:.0f}s > {limit}s)", False


def _check_consistency(package: ContextPackage) -> CheckResult:
    by_id = {it.id: it for it in package.items}
    failed: set[str] = set()
    pairs: list[list[str]] = []
    dangling: list[list[str]] = []

    for a in package.items:
        for bid in a.contradicts:
            b = by_id.get(bid)
            if b is None:
                # Absence/unknown must not map to clean: a contradiction
                # target missing from the package is a consistency failure.
                failed.add(a.id)
                ref = [a.id, bid]
                if ref not in dangling:
                    dangling.append(ref)
                continue
            # Fail only when neither side is resolved.
            if a.resolved is False and b.resolved is False:
                pair = sorted([a.id, b.id])
                if pair not in pairs:
                    pairs.append(pair)
                failed.add(a.id)
                failed.add(b.id)

    pairs_sorted = sorted(pairs, key=lambda p: (p[0], p[1]))
    dangling_sorted = sorted(dangling, key=lambda p: (p[0], p[1]))
    if failed:
        parts: list[str] = []
        if pairs_sorted:
            parts.append(f"unresolved contradicting pair(s): {pairs_sorted}")
        if dangling_sorted:
            parts.append(f"dangling contradiction target(s): {dangling_sorted}")
        return CheckResult(
            name="consistency",
            passed=False,
            failed_item_ids=tuple(sorted(failed)),
            detail="; ".join(parts),
            data={"pairs": pairs_sorted, "dangling": dangling_sorted},
        )
    return CheckResult(
        name="consistency",
        passed=True,
        detail="no unresolved contradictions",
        data={"pairs": [], "dangling": []},
    )


def _check_authorization(
    package: ContextPackage,
    read_set: Sequence[str],
    authorized_secret_ids: Sequence[str],
) -> CheckResult:
    secret_ok = frozenset(authorized_secret_ids)
    failed: list[str] = []
    details: list[str] = []

    for item in package.items:
        if item.kind == "secret":
            if item.id not in secret_ok:
                failed.append(item.id)
                details.append(f"{item.id}: secret not in authorized_secret_ids")
            continue
        if not _source_authorized(item.source, read_set):
            failed.append(item.id)
            details.append(f"{item.id}: source {item.source!r} not authorized by read_set")

    if failed:
        failed_sorted = tuple(sorted(set(failed)))
        return CheckResult(
            name="authorization",
            passed=False,
            failed_item_ids=failed_sorted,
            detail="; ".join(details),
        )
    return CheckResult(
        name="authorization",
        passed=True,
        detail="all sources and secrets authorized",
    )


@lru_cache(maxsize=1)
def _logical_auth_root() -> str:
    """Private absolute root so logical package sources can use path_containment."""
    return tempfile.mkdtemp(prefix="oaos-ctx-auth-")


def _is_logical_relative(path: str) -> bool:
    """True for non-empty package-relative path spellings (not abs/URI/home)."""
    if not path or "\x00" in path:
        return False
    text = path.replace("\\", "/").strip()
    if not text or text.startswith("/") or text.startswith("~"):
        return False
    if len(text) >= 2 and text[1] == ":":
        return False
    if "://" in text:
        return False
    return True


def _path_boundary_authorized(source: str, entry: str) -> bool:
    """True if source is entry or a contained child — never bare startswith.

    Security decision goes through ``inode_relative_parts`` from
    ``omniagentos.path_containment``. Absolute paths are checked directly.
    Logical relative package sources are joined under a private absolute root
    so ``..`` / symlink collapse is filesystem-true:
    ``src/app`` authorizes ``src/app/foo.py`` but not ``src/app/../../etc/passwd``.
    """
    if not source or not entry or "\x00" in source or "\x00" in entry:
        return False
    entry_clean = entry.rstrip("/\\")
    if not entry_clean:
        return False

    src_abs = os.path.isabs(source)
    ent_abs = os.path.isabs(entry_clean)
    if src_abs and ent_abs:
        return inode_relative_parts(source, entry_clean) is not None
    if src_abs or ent_abs:
        # Mixed absolute/relative cannot be compared soundly.
        return False
    if not _is_logical_relative(source) or not _is_logical_relative(entry_clean):
        return False
    # Refuse read_set entries that themselves traverse; fail closed.
    if ".." in entry_clean.replace("\\", "/").split("/"):
        return False

    root = _logical_auth_root()
    boundary_abs = os.path.join(root, entry_clean.replace("\\", "/"))
    try:
        os.makedirs(boundary_abs, exist_ok=True)
    except OSError:
        return False
    # Keep source spelling so path_containment sees ``..`` components.
    candidate_abs = os.path.join(root, source.replace("\\", "/"))
    return inode_relative_parts(candidate_abs, boundary_abs) is not None


def _source_authorized(source: str, read_set: Sequence[str]) -> bool:
    """Authorize source via path-boundary containment or fnmatch.

    Path-boundary: entry ``src/app`` authorizes ``src/app/foo.py`` but NOT
    ``src/application/secrets.py`` and NOT ``src/app/../../etc/passwd``.
    Exact source==entry is also decided by path-boundary (never bare string
    equality): a traversal-bearing read_set entry is refused the same way as
    a traversal-bearing child. Uses ``omniagentos.path_containment``; never
    bare ``str.startswith`` on the raw entry. Empty read_set authorizes
    nothing (fail-closed).
    """
    if not read_set:
        return False
    for entry in read_set:
        if not entry:
            continue
        # Path security always goes through path_containment via
        # _path_boundary_authorized — including exact source==entry.
        # Bare string equality would authorize traversal-bearing entries
        # that path_boundary correctly refuses.
        if _path_boundary_authorized(source, entry):
            return True
        # Glob patterns must not authorize traversal spellings.
        if ".." in source.replace("\\", "/").split("/"):
            continue
        if ".." in entry.replace("\\", "/").split("/"):
            continue
        if fnmatch.fnmatch(source, entry):
            return True
    return False


def _check_token_budget(
    package: ContextPackage,
    token_budget: int,
    safety_margin: float,
) -> CheckResult:
    total = package.total_tokens()
    effective = int(token_budget * (1.0 - safety_margin))
    overflow = total - effective
    data: dict[str, Any] = {
        "total_tokens": total,
        "token_budget": token_budget,
        "safety_margin": safety_margin,
        "effective_budget": effective,
        "overflow": overflow,
    }
    if total > effective:
        detail = (
            f"token budget exceeded: total_tokens={total} > effective_budget={effective} "
            f"(token_budget={token_budget} * (1.0 - safety_margin={safety_margin}) = {effective}; "
            f"overflow={overflow})"
        )
        return CheckResult(
            name="token_budget",
            passed=False,
            detail=detail,
            data=data,
        )
    detail = (
        f"within budget: total_tokens={total} <= effective_budget={effective} "
        f"(token_budget={token_budget}, safety_margin={safety_margin})"
    )
    return CheckResult(name="token_budget", passed=True, detail=detail, data=data)


def _check_acknowledgment(package: ContextPackage) -> CheckResult:
    token = package.ack_token
    if token is not None and str(token).strip():
        return CheckResult(
            name="acknowledgment",
            passed=True,
            detail="ack_token recorded",
        )
    return CheckResult(
        name="acknowledgment",
        passed=False,
        detail="package not-yet-acknowledged: ack_token is missing or blank",
    )


def _as_utc(dt: datetime) -> datetime:
    """Normalize now to timezone-aware UTC so comparisons never raise TypeError."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_iso8601(raw: str) -> datetime | None:
    """Parse ISO-8601; treat naive as UTC. Return None if unparseable."""
    text = raw.strip()
    if not text:
        return None
    # Support trailing Z.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
