"""AT-17 progress metric — count xfail items via pytest, never via text scan.

The plan originally proposed ``grep -c xfail tests/acceptance/test_17_control_plane.py``
as the ratchet metric. That is broken twice over and must not ship:

1. ``grep -c`` counts matching **lines**, not markers. A four-line
   ``@pytest.mark.xfail(...)`` decorator counts as 1 by luck; a line that
   mentions ``xfail`` in a comment or docstring counts as a marker; a
   parametrized xfail that expands to 3 items counts as 1; two markers on one
   line count as 1. Line counting and item counting are not the same thing.

2. On a **missing file** ``grep`` exits 2 and prints nothing, so
   ``$(grep -c ...)`` captures the empty string and every downstream shell
   arithmetic reads **0** — indistinguishable from a fully-ratcheted suite.
   Deleting or renaming the file would therefore read as "100% complete".

This is the same defect class fixed in commit ``cdde223`` (a mechanical gate
counted ruff OUTPUT LINES and so failed on a clean tree). Ask pytest itself:
pytest resolves multi-line decorators, class-level marks, ``pytestmark``, and
parametrization correctly; a text scan cannot.

Usage::

    uv run python -m tests.acceptance.suites.at17_progress
    uv run python -m tests.acceptance.suites.at17_progress --json
    uv run python -m tests.acceptance.suites.at17_progress --target PATH
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TARGET = REPO_ROOT / "tests" / "acceptance" / "test_17_control_plane.py"

_SENTINEL = "__AT17_JSON__"

# Worker program: collect items through pytest's own API and emit one JSON line.
# Kept as a string so the metric always runs in a subprocess (never nested
# in-process — collect() is itself called from inside a test).
_WORKER = r"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


class _Collector:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def pytest_collection_modifyitems(self, items):
        for item in items:
            mark = item.get_closest_marker("xfail")
            self.rows.append(
                {
                    "nodeid": item.nodeid,
                    "xfail": mark is not None,
                    "strict": bool(mark.kwargs.get("strict", False)) if mark else False,
                    "reason": str(mark.kwargs.get("reason", "")) if mark else "",
                }
            )


def main() -> int:
    target = sys.argv[1]
    collector = _Collector()
    code = pytest.main(
        [
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            target,
        ],
        plugins=[collector],
    )
    payload = {
        "exit_code": int(code),
        "rows": collector.rows,
        "target": str(Path(target).resolve()),
    }
    print("__AT17_JSON__" + json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


class AT17ProgressError(RuntimeError):
    """Raised when the progress metric cannot produce a trustworthy report."""


@dataclass(frozen=True)
class XfailItem:
    nodeid: str
    xfail: bool
    strict: bool
    reason: str


@dataclass(frozen=True)
class XfailReport:
    target: Path
    total: int
    xfail: int
    strict_xfail: int
    items: tuple[XfailItem, ...]

    @property
    def implemented(self) -> int:
        return self.total - self.xfail

    @property
    def percent_ratcheted(self) -> float:
        if self.total == 0:
            return 0.0
        return self.implemented / self.total * 100

    @property
    def non_gap_reasons(self) -> tuple[str, ...]:
        """Nodeids whose xfail reason does not start with ``GAP:``."""
        return tuple(
            item.nodeid for item in self.items if item.xfail and not item.reason.startswith("GAP:")
        )


def collect(target: Path = DEFAULT_TARGET) -> XfailReport:
    """Collect xfail markers on ``target`` via a subprocess pytest collect.

    A missing file raises :class:`AT17ProgressError` *before* pytest runs —
    never a zero-count report. A clean collect with zero xfail markers is a
    legitimate fully-ratcheted result and returns ``xfail=0`` with ``total>0``.
    """
    target = Path(target)
    if not target.is_file():
        raise AT17ProgressError(
            f"AT-17 progress target is missing: {target} — a missing file is NOT zero xfails"
        )

    try:
        proc = subprocess.run(
            [sys.executable, "-c", _WORKER, str(target.resolve())],
            cwd=str(REPO_ROOT),
            timeout=120,
            text=True,
            capture_output=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AT17ProgressError(f"AT-17 progress worker timed out after 120s on {target}") from exc
    except OSError as exc:
        raise AT17ProgressError(f"AT-17 progress worker failed to launch: {exc}") from exc

    sentinel_line: str | None = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith(_SENTINEL):
            sentinel_line = line
            break

    if sentinel_line is None:
        stderr_tail = (proc.stderr or "")[-2000:]
        raise AT17ProgressError(
            f"AT-17 progress worker produced no JSON sentinel "
            f"(subprocess exit {proc.returncode}); stderr tail:\n{stderr_tail}"
        )

    try:
        payload = json.loads(sentinel_line[len(_SENTINEL) :])
    except json.JSONDecodeError as exc:
        raise AT17ProgressError(
            f"AT-17 progress worker emitted unparseable JSON: {exc}; line={sentinel_line[:500]!r}"
        ) from exc

    worker_exit = int(payload.get("exit_code", -1))
    # pytest exit 0 = success; anything else (5=no tests, 4=usage, 3=internal)
    # is not a trustworthy zero-count report.
    if worker_exit != 0:
        stderr_tail = (proc.stderr or "")[-2000:]
        raise AT17ProgressError(
            f"AT-17 progress worker pytest exit {worker_exit} on {target}; "
            f"stderr tail:\n{stderr_tail}"
        )

    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise AT17ProgressError(
            f"AT-17 progress worker payload missing rows list: {type(rows).__name__}"
        )

    items: list[XfailItem] = []
    for row in rows:
        if not isinstance(row, dict):
            raise AT17ProgressError(
                f"AT-17 progress worker row is not an object: {type(row).__name__}"
            )
        items.append(
            XfailItem(
                nodeid=str(row.get("nodeid", "")),
                xfail=bool(row.get("xfail")),
                strict=bool(row.get("strict")),
                reason=str(row.get("reason", "")),
            )
        )

    xfail_count = sum(1 for item in items if item.xfail)
    strict_count = sum(1 for item in items if item.xfail and item.strict)
    return XfailReport(
        target=target.resolve(),
        total=len(items),
        xfail=xfail_count,
        strict_xfail=strict_count,
        items=tuple(items),
    )


def _report_to_jsonable(report: XfailReport) -> dict[str, object]:
    return {
        "target": str(report.target),
        "total": report.total,
        "xfail": report.xfail,
        "strict_xfail": report.strict_xfail,
        "implemented": report.implemented,
        "percent_ratcheted": report.percent_ratcheted,
        "non_gap_reasons": list(report.non_gap_reasons),
        "items": [asdict(item) for item in report.items],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AT-17 xfail progress metric")
    parser.add_argument(
        "--target",
        type=Path,
        default=DEFAULT_TARGET,
        help="Path to the acceptance module to measure (default: test_17_control_plane.py)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the full report as JSON",
    )
    args = parser.parse_args(argv)

    try:
        report = collect(args.target)
    except AT17ProgressError as exc:
        print(f"AT17 PROGRESS ERROR: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(_report_to_jsonable(report), indent=2, sort_keys=True))
    else:
        print(
            f"AT-17 ratchet: {report.implemented}/{report.total} implemented "
            f"({report.percent_ratcheted:.1f}%) — {report.xfail} xfail "
            f"({report.strict_xfail} strict)"
        )

    if report.non_gap_reasons:
        print(
            "WARNING: xfail reasons that do not start with 'GAP:': "
            + ", ".join(report.non_gap_reasons),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
