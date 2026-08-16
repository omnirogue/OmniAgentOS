"""Surface required-suite skips (M-41).

Environment-gated security/recovery suites must not disappear from the default
signal without an explicit report.

Suite membership is decided by path globs and markers alone. ``skip_reasons_expected``
classifies a matched skip — routine environment gate (``REQUIRED_SUITE_SKIPPED``)
versus one the suite never declared (``REQUIRED_SUITE_SKIPPED_UNEXPECTED``) — and
never suppresses one, so the anomalous skip is the loud case rather than the
silent one.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from omniagentos.testpolicy.policy_load import load_policy_dict


@dataclass(frozen=True)
class RequiredSuite:
    id: str
    description: str
    path_globs: tuple[str, ...]
    skip_reasons_expected: tuple[str, ...]
    env_requirement: str
    surface_when_skipped: bool
    # When non-empty, a collected item must carry at least one of these markers
    # to count as a run of the suite (M-41 markers_any).
    markers_any: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkipRecord:
    nodeid: str
    reason: str
    path: str = ""
    markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkipSurfaceReport:
    required_suites: tuple[RequiredSuite, ...]
    matched_skips: tuple[tuple[str, SkipRecord], ...]
    missing_required_runs: tuple[str, ...]
    surfaced_messages: tuple[str, ...]
    # Subset of ``matched_skips`` whose reason is NOT one the suite declared as
    # an expected environment gate — the anomalous case M-41 exists to catch.
    unexpected_skips: tuple[tuple[str, SkipRecord], ...] = ()

    @property
    def has_surfaced_skips(self) -> bool:
        return bool(self.matched_skips)

    @property
    def has_surfaced_signal(self) -> bool:
        """True when any required-suite skip or absence was emitted (CI signal)."""
        return bool(self.surfaced_messages)

    @property
    def has_unexpected_skips(self) -> bool:
        """True when a required suite was skipped for a reason it never declared."""
        return bool(self.unexpected_skips)

    def as_dict(self) -> dict[str, Any]:
        unexpected = {(sid, rec.nodeid, rec.reason) for sid, rec in self.unexpected_skips}
        return {
            "matched_skips": [
                {
                    "suite_id": sid,
                    "nodeid": rec.nodeid,
                    "reason": rec.reason,
                    "markers": list(rec.markers),
                    "reason_expected": (sid, rec.nodeid, rec.reason) not in unexpected,
                }
                for sid, rec in self.matched_skips
            ],
            "unexpected_skips": [
                {"suite_id": sid, "nodeid": rec.nodeid, "reason": rec.reason}
                for sid, rec in self.unexpected_skips
            ],
            "missing_required_runs": list(self.missing_required_runs),
            "surfaced_messages": list(self.surfaced_messages),
        }


def load_required_suites(path: str | None = None) -> tuple[RequiredSuite, ...]:
    data = load_policy_dict(path)
    rows = data.get("required_suites") or []
    out: list[RequiredSuite] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        raw_markers = row.get("markers_any") or ()
        markers = tuple(str(m) for m in raw_markers if str(m).strip())
        out.append(
            RequiredSuite(
                id=str(row["id"]),
                description=str(row.get("description") or row["id"]),
                path_globs=tuple(row.get("path_globs") or ()),
                skip_reasons_expected=tuple(row.get("skip_reasons_expected") or ()),
                env_requirement=str(row.get("env_requirement") or "none"),
                surface_when_skipped=bool(row.get("surface_when_skipped", True)),
                markers_any=markers,
            )
        )
    return tuple(out)


def surface_required_skips(
    skips: Sequence[SkipRecord | Mapping[str, Any]],
    *,
    collected_nodeids: Iterable[str] | None = None,
    collected_items: Sequence[Mapping[str, Any]] | None = None,
    suites: Sequence[RequiredSuite] | None = None,
    check_absent: bool = True,
) -> SkipSurfaceReport:
    """Match skip records against required suites and build operator-facing messages.

    ``collected_items`` may supply ``{nodeid, markers, path}`` so ``markers_any``
    can decide whether a collected test counts as a suite run (M-41).
    """
    required = tuple(suites) if suites is not None else load_required_suites()
    normalized: list[SkipRecord] = []
    for item in skips:
        if isinstance(item, SkipRecord):
            normalized.append(item)
        else:
            markers_raw = item.get("markers") or ()
            markers_t: tuple[str, ...]
            if isinstance(markers_raw, str):
                markers_t = (markers_raw,) if markers_raw.strip() else ()
            else:
                markers_t = tuple(str(m) for m in markers_raw if str(m).strip())
            normalized.append(
                SkipRecord(
                    nodeid=str(item.get("nodeid") or ""),
                    reason=str(item.get("reason") or item.get("skip_reason") or ""),
                    path=str(item.get("path") or ""),
                    markers=markers_t,
                )
            )

    matched: list[tuple[str, SkipRecord]] = []
    unexpected: list[tuple[str, SkipRecord]] = []
    messages: list[str] = []
    for suite in required:
        if not suite.surface_when_skipped:
            continue
        for rec in normalized:
            if not _matches_suite(suite, rec):
                continue
            matched.append((suite.id, rec))
            if _reason_is_expected(suite, rec):
                messages.append(
                    f"REQUIRED_SUITE_SKIPPED suite={suite.id} "
                    f"env={suite.env_requirement} nodeid={rec.nodeid} "
                    f"reason={rec.reason!r} — {suite.description}"
                )
            else:
                unexpected.append((suite.id, rec))
                messages.append(
                    f"REQUIRED_SUITE_SKIPPED_UNEXPECTED suite={suite.id} "
                    f"env={suite.env_requirement} nodeid={rec.nodeid} "
                    f"reason={rec.reason!r} — {suite.description} "
                    f"(not a declared environment gate: "
                    f"{list(suite.skip_reasons_expected)})"
                )

    missing: list[str] = []
    if check_absent and (collected_nodeids is not None or collected_items is not None):
        items = _normalize_collected(collected_nodeids, collected_items)
        for suite in required:
            if not suite.path_globs and not suite.markers_any:
                continue
            if not any(_item_matches_suite(suite, item) for item in items):
                # No matching test was collected — surface as missing/vanished.
                missing.append(suite.id)
                messages.append(
                    f"REQUIRED_SUITE_ABSENT suite={suite.id} "
                    f"env={suite.env_requirement} — {suite.description}"
                )

    return SkipSurfaceReport(
        required_suites=required,
        matched_skips=tuple(matched),
        missing_required_runs=tuple(missing),
        surfaced_messages=tuple(messages),
        unexpected_skips=tuple(unexpected),
    )


def _normalize_collected(
    collected_nodeids: Iterable[str] | None,
    collected_items: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if collected_items is not None:
        for item in collected_items:
            markers_raw = item.get("markers") or ()
            markers: tuple[str, ...]
            if isinstance(markers_raw, str):
                markers = (markers_raw,) if markers_raw.strip() else ()
            else:
                markers = tuple(str(m) for m in markers_raw if str(m).strip())
            nodeid = str(item.get("nodeid") or "")
            out.append(
                {
                    "nodeid": nodeid,
                    "path": str(item.get("path") or nodeid),
                    "markers": markers,
                }
            )
        return out
    if collected_nodeids is not None:
        for n in collected_nodeids:
            out.append({"nodeid": n, "path": n, "markers": ()})
    return out


def _item_matches_suite(suite: RequiredSuite, item: Mapping[str, Any]) -> bool:
    path = str(item.get("path") or item.get("nodeid") or "")
    if suite.path_globs and not _path_matches(suite.path_globs, path):
        return False
    if suite.markers_any:
        item_markers = {str(m).lower() for m in (item.get("markers") or ())}
        wanted = {m.lower() for m in suite.markers_any}
        if item_markers.isdisjoint(wanted):
            return False
    return True


def _matches_suite(suite: RequiredSuite, rec: SkipRecord) -> bool:
    """Does this skip belong to ``suite``? Membership is path + markers only.

    The reason string deliberately plays no part here. ``skip_reasons_expected``
    names the benign, environment-driven reasons a suite is *allowed* to vanish
    for; gating the report on it inverted the guard, because a required suite
    disabled for an unexpected reason (``@pytest.mark.skip(reason="flaky")``)
    then produced no signal at all — and ``REQUIRED_SUITE_ABSENT`` cannot cover
    it, since a skipped test is still collected. The reason now classifies the
    match instead of suppressing it (see ``_reason_is_expected``).
    """
    path = rec.path or rec.nodeid
    if suite.path_globs and not _path_matches(suite.path_globs, path):
        return False
    if suite.markers_any:
        rec_markers = {m.lower() for m in rec.markers}
        wanted = {m.lower() for m in suite.markers_any}
        # Skips without marker metadata still match on path (the hook attaches
        # markers when available).
        if rec.markers and rec_markers.isdisjoint(wanted):
            return False
    return True


def _reason_is_expected(suite: RequiredSuite, rec: SkipRecord) -> bool:
    """Is this skip one of the environment gates the suite declared?

    A suite that declares no expected reasons treats every skip as routine —
    it has said nothing about which reasons are anomalous.
    """
    if not suite.skip_reasons_expected:
        return True
    reason_l = rec.reason.lower()
    return any(token.lower() in reason_l for token in suite.skip_reasons_expected if token)


def _path_matches(globs: Sequence[str], path: str) -> bool:
    text = path.replace("\\", "/")
    for pattern in globs:
        pat = pattern.replace("\\", "/")
        if not pat:
            continue
        if fnmatch.fnmatch(text, pat) or fnmatch.fnmatch(text, "*/" + pat):
            return True
        # nodeid form: tests/knowledge/test_x.py::test_y
        if "::" in text:
            file_part = text.split("::", 1)[0]
            if fnmatch.fnmatch(file_part, pat) or fnmatch.fnmatch(file_part, "*/" + pat):
                return True
    return False
