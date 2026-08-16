"""ONE shared, artifact-truthful settlement classifier for the reflection loop.

Design rule (S18): **a recorded status is a function of the ARTIFACT a stage
produced, never of the code path that ran.**

Before this module the nightly loop recorded a row whose stage columns all read
``ok`` while ``bytes_read`` was 0, ``sources_read`` was empty, and no briefing
file existed at all — the status was a transcript of which ``try`` blocks did
not raise, not a statement about what the run produced.

:class:`Settlement` is deliberately THREE-valued:

``OK``
    A real artifact exists and is non-empty (or an explicit positive evidence
    flag was supplied).  This is the only value that may ever be recorded as a
    success, and it is never reachable without proof.
``FAILED``
    The stage raised, or a *required* artifact is missing/empty.
``UNGATEABLE``
    The stage produced nothing that can be graded.  This is an EXPLICIT third
    outcome so a run nobody can grade is excluded from the acceptance floor
    *by construction* rather than being silently bucketed as unfavourable.

Every writer in ``omniagentos/reflection`` routes through
:func:`classify_settlement`.  No writer may assign a bare success literal.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from omniagentos.contracts import new_id

__all__ = [
    "AcceptanceFloor",
    "RUN_ID_PREFIX",
    "Settlement",
    "acceptance_floor",
    "artifact_bytes",
    "classify_settlement",
    "counts_toward_acceptance_floor",
    "empty_artifacts",
    "is_run_id",
    "new_run_id",
    "run_settlement",
]


# --------------------------------------------------------------------------
# One id scheme.  The loop used to mint `refr_…` in the runner and `ref_…` in
# the harvester, so every night wrote TWO rows that disagreed with each other.
# There is exactly one prefix now and exactly one place that mints it.
# --------------------------------------------------------------------------
RUN_ID_PREFIX = "refr"


def new_run_id() -> str:
    """Mint the one and only reflection run id (``refr_…``)."""
    return new_id(RUN_ID_PREFIX)


def is_run_id(value: object) -> bool:
    """True when *value* is a run id under the single canonical scheme."""
    return isinstance(value, str) and value.startswith(f"{RUN_ID_PREFIX}_")


class Settlement(StrEnum):
    """Explicit three-valued outcome of a reflection stage or run."""

    OK = "ok"
    FAILED = "failed"
    UNGATEABLE = "ungateable"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


SETTLEMENT_VALUES: tuple[str, ...] = tuple(member.value for member in Settlement)


def artifact_bytes(artifact: str | os.PathLike[str] | None) -> int | None:
    """Size of *artifact* in bytes, or ``None`` when it is not a readable file.

    ``None`` means "no artifact to judge" — it is never confused with 0, which
    means "an artifact exists and is empty".
    """
    if artifact is None:
        return None
    try:
        path = Path(os.fspath(artifact))
    except TypeError:
        return None
    try:
        if not os.path.isfile(path):
            return None
        return int(os.path.getsize(path))
    except OSError:
        return None


def classify_settlement(
    artifact: str | os.PathLike[str] | None = None,
    *,
    error: BaseException | str | None = None,
    evidence: bool | None = None,
    required: bool = False,
    min_bytes: int = 1,
) -> Settlement:
    """Classify one stage/run outcome from its artifact.

    Parameters
    ----------
    artifact:
        Path to the file the stage was supposed to produce.  ``OK`` requires
        ``os.path.getsize(artifact) >= min_bytes`` — a zero-byte artifact is
        never a success.
    error:
        A raised exception (or message).  Any truthy value settles ``FAILED``
        regardless of the artifact.
    evidence:
        Explicit positive/negative evidence for stages that legitimately have
        no file artifact (e.g. "validation produced decisions").  ``None``
        means "unknown", which can never settle ``OK``.
    required:
        When the artifact is missing/empty: ``True`` settles ``FAILED`` (the
        producer owed us this file), ``False`` settles ``UNGATEABLE`` (we
        simply have nothing to grade).
    min_bytes:
        Minimum non-empty size, default 1.
    """
    if error is not None and (not isinstance(error, str) or error.strip()):
        return Settlement.FAILED

    missing = Settlement.FAILED if required else Settlement.UNGATEABLE

    if artifact is not None:
        size = artifact_bytes(artifact)
        if size is None or size < min_bytes:
            return missing
        if evidence is False:
            return Settlement.UNGATEABLE
        return Settlement.OK

    if evidence is True:
        return Settlement.OK
    return missing


def counts_toward_acceptance_floor(settlement: Settlement | str | None) -> bool:
    """``UNGATEABLE`` (and unrecorded) outcomes never enter the floor."""
    if settlement is None:
        return False
    value = settlement.value if isinstance(settlement, Settlement) else str(settlement)
    return value in (Settlement.OK.value, Settlement.FAILED.value)


@dataclass(frozen=True)
class AcceptanceFloor:
    """Result of an acceptance-floor evaluation.

    ``meets`` is ``None`` when nothing was gateable — an undecidable floor is
    reported as undecidable rather than as a pass or a failure.
    """

    floor: float
    ok: int
    failed: int
    ungateable: int
    ratio: float | None
    meets: bool | None

    @property
    def gateable(self) -> int:
        return self.ok + self.failed


def acceptance_floor(
    settlements: Iterable[Settlement | str | None], floor: float = 1.0
) -> AcceptanceFloor:
    """Evaluate *settlements* against *floor*, excluding ungateable outcomes.

    Ungateable outcomes are removed from BOTH the numerator and the
    denominator, so a run that could not be graded neither passes nor fails the
    floor on its own account.
    """
    ok = failed = ungateable = 0
    for item in settlements:
        value = (
            item.value if isinstance(item, Settlement) else (None if item is None else str(item))
        )
        if value == Settlement.OK.value:
            ok += 1
        elif value == Settlement.FAILED.value:
            failed += 1
        else:
            ungateable += 1

    gateable = ok + failed
    ratio = (ok / gateable) if gateable else None
    meets = None if ratio is None else ratio >= floor
    return AcceptanceFloor(
        floor=floor, ok=ok, failed=failed, ungateable=ungateable, ratio=ratio, meets=meets
    )


def run_settlement(stage_values: Iterable[Settlement | str | None]) -> Settlement:
    """Fold recorded stage settlements into one run-level settlement."""
    recorded = 0
    all_ok = True
    for item in stage_values:
        value = (
            item.value if isinstance(item, Settlement) else (None if item is None else str(item))
        )
        if value is None:
            continue
        recorded += 1
        if value == Settlement.FAILED.value:
            return Settlement.FAILED
        if value != Settlement.OK.value:
            all_ok = False
    if recorded and all_ok:
        return Settlement.OK
    return Settlement.UNGATEABLE


def empty_artifacts(paths: Iterable[str | os.PathLike[str]]) -> list[Path]:
    """Return the paths that the same artifact rule retires as ungateable.

    Used to retire zero-byte apply logs: a log file of length 0 is evidence of
    nothing, so it may never be counted as a successful apply.  This function
    only CLASSIFIES — it never deletes.
    """
    retired: list[Path] = []
    for raw in paths:
        path = Path(os.fspath(raw))
        if classify_settlement(path) is not Settlement.OK:
            retired.append(path)
    return retired
