"""An integrity checker must not report its own failures as success.

MEASURED 2026-08-08 against the live queue: `integrity.py --category invariants`
printed ``{"failures": 1}`` with ``FAIL rejection.has_expires_at`` on every run,
and `launchctl list` recorded ``-  0  com.threeloops.integrity-invariants``.

That is the favourable-absence class these very checks exist to catch, committed
by the instrument itself. It also disabled the layer above: health-sentinel flags
a job by its nonzero last exit code, so a checker that always exits 0 is
permanently invisible to the meta-watchdog.

The three-value contract pinned here is the house one, and it is already half
implemented (a missing queue root returns 2):

    0  checks ran, nothing failed
    1  checks ran, something FAILED and a human/monitor should act
    2  the check COULD NOT RUN -- an instrument error, never a verdict on the queue

Suspicions and alerts deliberately do NOT fail the run. They are observations,
and exiting non-zero on them would make red the resting state, which trains the
operator to ignore the signal -- the same defect wearing the opposite sign.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1] / "bridge" / "integrity.py"


def _queue(root: Path) -> Path:
    """A structurally valid, empty loopqueue."""
    for sub in ("findings", "proposals", "candidates", "inquiries", "rejected",
                "parked", "claims", "receipts", "state", "logs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    # One valid event: a ZERO-BYTE ledger is treated as unreadable (and must
    # exit 2, pinned separately), so it is not a "clean queue".
    (root / "ledger.jsonl").write_text(json.dumps({
        "ts": "2026-08-08T00:00:00Z", "role": "planner", "event": "proposed",
        "id": "sha256:" + "a" * 64, "detail": {},
    }) + "\n")
    (root / "state" / "budget.json").write_text(json.dumps({
        "disk_free_gb_min": 20, "load_avg_1m_max": 16, "wip_cap": 4,
        "updated_at": "2026-08-08T00:00:00Z",
    }))
    return root


def _run(root: Path, category: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(BRIDGE), "--loops-root", str(root), "--category", category],
        capture_output=True, text=True, timeout=120,
    )


def test_clean_queue_exits_zero(tmp_path: Path) -> None:
    p = _run(_queue(tmp_path / "q"), "invariants")
    assert p.returncode == 0, f"clean queue should pass: rc={p.returncode} {p.stdout}{p.stderr}"


def test_a_failure_exits_nonzero(tmp_path: Path) -> None:
    """The regression this file exists to prevent."""
    root = _queue(tmp_path / "q")
    # A `rejected` LEDGER EVENT carrying no detail.expires_at is a permanent,
    # unexpirable ban — the exact invariant that was failing live while the
    # process exited 0. Note the check reads the ledger, not rejected/: a
    # tombstone file alone does not trip it.
    with (root / "ledger.jsonl").open("a") as fh:
        fh.write(json.dumps({
            "ts": "2026-08-08T00:00:01Z", "role": "implementer", "event": "rejected",
            "id": "sha256:" + "d" * 64,
            "detail": {"reason": "no TTL — permanent ban", "class": "candidate-defect"},
        }) + "\n")
    p = _run(root, "invariants")
    payload = json.loads(p.stdout.splitlines()[0])
    assert payload["failures"] >= 1, f"fixture did not trip the invariant: {p.stdout}"
    assert p.returncode == 1, (
        f"reported {payload['failures']} failure(s) and still exited {p.returncode}. "
        "launchctl and health-sentinel read the exit code, not stdout."
    )


def test_unrunnable_check_exits_two_not_one(tmp_path: Path) -> None:
    """An instrument error must stay distinguishable from a real failure."""
    p = _run(tmp_path / "does-not-exist", "invariants")
    assert p.returncode == 2, (
        f"a missing queue root exited {p.returncode}; 2 means 'could not run' and "
        "must never collapse into 1 ('the queue is broken')."
    )


def test_unreadable_ledger_exits_two_not_one(tmp_path: Path) -> None:
    """An instrument error must never be reported as a queue defect.

    A zero-byte ledger trips ``ledger.readable`` with ``checks_run: 0`` — nothing
    about the queue was actually examined. Collapsing that into exit 1 would tell
    the monitor "the queue is broken" when the truth is "I could not read it",
    and send whoever responds to debug the wrong thing. 64 of 90 gate refusals on
    this estate were exactly this misclassification.
    """
    root = _queue(tmp_path / "q")
    (root / "ledger.jsonl").write_text("")
    p = _run(root, "invariants")
    payload = json.loads(p.stdout.splitlines()[0])
    assert payload["checks_run"] == 0, "fixture no longer produces an unread ledger"
    assert p.returncode == 2, (
        f"unreadable ledger exited {p.returncode}; 1 would mean 'the queue failed' "
        "when nothing about the queue was examined."
    )

