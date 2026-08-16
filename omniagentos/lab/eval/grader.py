"""ProtectedGrader — the ONLY component in OmniAgentOS that reads held-out
expected values (blueprint Section 11.7; design review HD-001/HD-ALT/HD-009).

It owns a SEPARATE SQLite file (``paths.default_protected_db_path()``) —
never the shared ``omniagentos.db``: it does not import or share a connection
with ``omniagentos.db.store.SqliteStore`` / ``omniagentos.lab.db.LabStore`` at
all, on purpose, so there is no code path by which an ``expected`` value could
end up written into the shared store's tables.

Grading is out-of-process from the campaign/challenger/judge/curator:
``score_outputs`` receives candidate OUTPUTS + case ids only and returns an
``EvalResult`` whose ``metrics``/``per_case`` are plain floats — ``expected``
itself never appears in the return value, so nothing downstream (the shared
store, a vault note, the ledger, an API response) can leak it even by
accident, let alone by a challenger/judge/curator reading this module's
source (there is nothing here that would help: the *values* live only in the
separate database file, not in code).

As of the round-2 security repair, ``expected_json`` is a Fernet ciphertext,
not plaintext JSON. The per-grader key lives only in this process's memory and
transiently in each worker's spawn environment. A same-uid process can still
read the file (there is no OS sandbox; see ``eval.paths``), but recovers only
ciphertext it cannot use.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from statistics import fmean, pstdev
from threading import RLock
from typing import Any

from omniagentos.contracts import (
    default_db_path,
    default_ledger_dir,
    default_vault_dir,
    utc_now_iso,
)
from omniagentos.lab.contracts import EvalResult, EvalSplit
from omniagentos.lab.eval._crypto import (
    EVAL_KEY_ENV,
    decrypt_expected,
    encrypt_expected,
    generate_key,
)
from omniagentos.lab.eval.paths import PROTECTED_DB_ENV, default_protected_db_path
from omniagentos.lab.eval.scoring import aggregate_case_scores, score_case
from omniagentos.path_containment import (
    inode_path_is_within_anchored,
    inode_paths_equal,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS protected_expected (
    case_id       TEXT PRIMARY KEY,
    expected_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""

_WORKER_MODULE = "omniagentos.lab.eval._worker"

# Metric-jump audit thresholds (Section 11.7: "audit sudden metric jumps for
# leakage/shortcut"). Deliberately conservative, deterministic (pure
# arithmetic over already-graded EvalResult numbers — never a model call, so
# the audit itself cannot be gamed by a challenger) and documented so a
# reviewer can reproduce a flag by hand.
LEAK_ABS_JUMP = 0.5  # an unqualified jump at least this large is itself suspicious
LEAK_NEAR_PERFECT = 0.999  # a "too good to be true" score
LEAK_BASELINE_CEILING = 0.9  # ...only suspicious when the champion was well below it
LEAK_GENERALIZATION_GAP_MIN = 0.10


class MissingExpectedError(LookupError):
    """Raised when ``score_outputs`` is asked to grade a case id with no
    ``put_expected`` record. This is a contamination/integrity signal (an
    unknown or mismatched case id) — deliberately RAISED rather than
    silently downgraded into a low score, so a caller cannot accidentally
    ship a "passing" grade for a case that was never actually scored."""


def _connect(db_path: str) -> sqlite3.Connection:
    """Create an in-process connection (for testing with :memory: only)."""
    is_memory = db_path == ":memory:"
    is_new_file = False
    if not is_memory:
        expanded = Path(db_path).expanduser()
        expanded.parent.mkdir(parents=True, exist_ok=True)
        is_new_file = not expanded.exists()
        db_path = str(expanded)
    connection = sqlite3.connect(
        db_path, isolation_level=None, timeout=5.0, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    if not is_memory:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(_SCHEMA)
    if is_new_file:
        # Defense in depth: owner-only permissions on a freshly created
        # protected file (best-effort — some filesystems/CI sandboxes don't
        # support chmod; never fail store construction over it).
        try:
            os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    return connection


class ProtectedGrader:
    """The only component that reads held-out expected values.

    File-backed values are handled by a subprocess and stored as Fernet
    ciphertext. This trusted parent retains only the per-run key; expected
    plaintext never returns from the worker.
    """

    def __init__(
        self,
        protected_path: str | None = None,
        *,
        shared_db_path: str | None = None,
        shared_vault_dir: str | None = None,
        shared_ledger_dir: str | None = None,
    ) -> None:
        self._lock = RLock()
        self._key = generate_key()
        self._owns_default_dir = protected_path is None and not os.environ.get(PROTECTED_DB_ENV)
        self._path = protected_path or default_protected_db_path()
        self._is_memory = self._path == ":memory:"
        self._shared_db_path = shared_db_path or default_db_path()
        self._shared_vault_dir = shared_vault_dir or default_vault_dir()
        self._shared_ledger_dir = shared_ledger_dir or default_ledger_dir()

        if not self._is_memory:
            # Validate isolation for file-based DBs only (not for :memory: test DBs)
            self._validate_isolation()
            # Initialize the protected DB directory and parent directories
            expanded = Path(self._path).expanduser()
            expanded.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Initialize the DB via worker subprocess
            self._worker({"operation": "initialize"})
            self._harden_permissions()
        else:
            # For in-process :memory: DBs (testing only), initialize directly
            self._connection = _connect(self._path)

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        if self._is_memory:
            with self._lock:
                self._connection.close()
            return
        if self._owns_default_dir:
            shutil.rmtree(Path(self._path).parent, ignore_errors=True)

    def __enter__(self) -> ProtectedGrader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def put_expected(self, case_id: str, expected: dict[str, Any]) -> None:
        """Ingest an expected answer. Admin/seed only: called while
        authoring an eval suite (e.g. by a suite-loading script running in
        the SAME process as the grader) — NEVER from candidate/challenger/
        judge/curator code, which never holds a ``ProtectedGrader`` at all."""
        if not case_id:
            raise ValueError("case_id must not be empty")

        if self._is_memory:
            # For :memory: DBs, use in-process for testing convenience
            with self._lock:
                now = utc_now_iso()
                payload = encrypt_expected(self._key, expected)
                self._connection.execute(
                    "INSERT INTO protected_expected (case_id, expected_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(case_id) DO UPDATE SET "
                    "expected_json = excluded.expected_json, updated_at = excluded.updated_at",
                    (case_id, payload, now, now),
                )
        else:
            # For file DBs, delegate to isolated subprocess worker
            now = utc_now_iso()
            self._worker(
                {
                    "operation": "put",
                    "case_id": case_id,
                    "expected": expected,
                    "now": now,
                }
            )
            self._harden_permissions()

    def put_expected_many(self, cases: Mapping[str, dict[str, Any]]) -> None:
        """Upsert a batch of protected expectations with one worker launch."""
        if not cases or any(not case_id for case_id in cases):
            raise ValueError("put_expected_many requires non-empty case ids")
        now = utc_now_iso()
        if self._is_memory:
            with self._lock:
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    for case_id, expected in cases.items():
                        payload = encrypt_expected(self._key, expected)
                        self._connection.execute(
                            "INSERT INTO protected_expected "
                            "(case_id, expected_json, created_at, updated_at) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(case_id) DO UPDATE SET "
                            "expected_json = excluded.expected_json, updated_at = excluded.updated_at",
                            (case_id, payload, now, now),
                        )
                    self._connection.execute("COMMIT")
                except BaseException:
                    self._connection.execute("ROLLBACK")
                    raise
            return
        self._worker(
            {
                "operation": "put_many",
                "cases": dict(cases),
                "now": now,
            }
        )
        self._harden_permissions()

    def score_outputs(
        self, suite_id: str, split: str, arm: str, outputs: dict[str, dict[str, Any]]
    ) -> EvalResult:
        """Deterministic scoring vs protected expected. ``outputs`` (candidate
        text/json per case id) is the ONLY candidate-derived input; the
        looked-up ``expected`` values never leave this method — only the
        resulting floats do.

        Returns an EvalResult with ``experiment_id=""`` / ``suite_version=0``
        placeholders: this method has no experiment context (by design — it
        does not depend on ``LabStore`` at all). ``ProtectedEvaluator.
        run_deterministic`` (which DOES have store access) fills in
        ``suite_version``; the caller attaches ``experiment_id`` when it
        persists the result via ``LabStore.record_eval_result``.
        """
        if not outputs:
            raise ValueError("score_outputs: outputs is empty; nothing to grade")

        if self._is_memory:
            # For :memory: DBs, score in-process
            with self._lock:
                per_case: dict[str, dict[str, float]] = {}
                for case_id, output in outputs.items():
                    expected = self._get_expected(case_id)
                    if expected is None:
                        raise MissingExpectedError(
                            f"no protected expected for case {case_id!r} "
                            f"(suite={suite_id!r} split={split!r}) — contaminated grading run"
                        )
                    per_case[case_id] = score_case(expected, output)
        else:
            # For file DBs, delegate to isolated subprocess worker
            response = self._worker(
                {
                    "operation": "score",
                    "case_ids": sorted(outputs.keys()),
                    "outputs": outputs,
                }
            )
            per_case = response.get("per_case", {})
            # Check for missing expected cases
            for case_id in outputs.keys():
                if case_id not in per_case or not per_case[case_id]:
                    raise MissingExpectedError(
                        f"no protected expected for case {case_id!r} "
                        f"(suite={suite_id!r} split={split!r}) — contaminated grading run"
                    )

        aggregate = aggregate_case_scores(per_case)
        metrics = {
            "accuracy": aggregate.get("correct", 0.0),
            "mean_score": aggregate.get("score", 0.0),
        }
        return EvalResult(
            experiment_id="",
            arm=arm,
            suite_id=suite_id,
            suite_version=0,
            split=EvalSplit(split),
            metrics=metrics,
            per_case=per_case,
        )

    def audit_metric_jump(self, exp_id: str, champ: EvalResult, chal: EvalResult) -> list[str]:
        """Flag metric deltas shaped like a leak/shortcut rather than genuine
        improvement (Section 11.7). Pure arithmetic over already-graded
        ``EvalResult`` numbers — never a model call, so the audit cannot
        itself be gamed. Non-empty -> the caller's ``Scorecard.audit_flags``,
        which forces ``HUMAN_REVIEW`` regardless of how good the numbers
        otherwise look (contracts/lab-interfaces.md §L04-campaign).

        Scoped to ``[0, 1]``-bounded (quality/accuracy-shaped) metric values —
        exactly what ``score_outputs`` itself produces (``accuracy``,
        ``mean_score``), and what suite authors should keep primary/guardrail/
        hard_constraint metrics in. This signature has no ``MetricSpec``
        access (by design — it is pure arithmetic over two ``EvalResult``s),
        so it cannot know a metric's declared role; bounding by VALUE instead
        skips arbitrary-scale metrics (counts, milliseconds, ...) for which a
        fixed absolute threshold would be a category error — a held-out leak
        could inflate a *correctness* score, never shrink a latency number."""
        flags: list[str] = []
        names = sorted(set(champ.metrics) | set(chal.metrics))
        for name in names:
            champion_value = champ.metrics.get(name)
            challenger_value = chal.metrics.get(name)
            if champion_value is None or challenger_value is None:
                continue
            if not (_in_unit_range(champion_value) and _in_unit_range(challenger_value)):
                continue
            delta = challenger_value - champion_value
            if delta >= LEAK_ABS_JUMP:
                flags.append(
                    f"metric_jump:{name}:+{delta:.3f}:exp={exp_id}:"
                    f"champion={champion_value:.3f}:challenger={challenger_value:.3f}"
                )
            if (
                challenger_value >= LEAK_NEAR_PERFECT
                and champion_value < LEAK_BASELINE_CEILING
                and chal.split == EvalSplit.HELD_OUT
            ):
                flags.append(
                    f"suspicious_perfect:{name}:held_out:exp={exp_id}:"
                    f"champion={champion_value:.3f}:challenger={challenger_value:.3f}"
                )
        if chal.split == EvalSplit.HELD_OUT and _zero_variance_perfect(chal):
            flags.append(f"zero_variance_perfect:held_out:exp={exp_id}")
        return flags

    def audit_generalization_gap(
        self,
        exp_id: str,
        champion_dev: list[EvalResult],
        challenger_dev: list[EvalResult],
        champion_held: list[EvalResult],
        challenger_held: list[EvalResult],
    ) -> list[str]:
        """Flag held-out-only gains relative to observed dev replicate noise."""
        flags: list[str] = []
        names = sorted(
            set.intersection(
                *(
                    set(result.metrics)
                    for result in [
                        *champion_dev,
                        *challenger_dev,
                        *champion_held,
                        *challenger_held,
                    ]
                )
            )
        )
        for name in names:
            dev_pairs = list(zip(champion_dev, challenger_dev, strict=False))
            held_pairs = list(zip(champion_held, challenger_held, strict=False))
            dev_gains = [chal.metrics[name] - champ.metrics[name] for champ, chal in dev_pairs]
            held_gains = [chal.metrics[name] - champ.metrics[name] for champ, chal in held_pairs]
            values = [
                result.metrics[name]
                for result in [
                    *champion_dev,
                    *challenger_dev,
                    *champion_held,
                    *challenger_held,
                ]
            ]
            if not values or not all(_in_unit_range(value) for value in values):
                continue
            variance = pstdev(dev_gains) if len(dev_gains) > 1 else 0.0
            threshold = max(LEAK_GENERALIZATION_GAP_MIN, 3.0 * variance)
            dev_gain = fmean(dev_gains)
            held_gain = fmean(held_gains)
            gap = held_gain - dev_gain
            if gap >= threshold:
                flags.append(
                    f"generalization_gap_inversion:{name}:+{gap:.3f}:exp={exp_id}:"
                    f"dev_gain={dev_gain:.3f}:held_out_gain={held_gain:.3f}:"
                    f"noise_threshold={threshold:.3f}"
                )
        return flags

    def _validate_isolation(self) -> None:
        """Ensure the protected DB path doesn't alias the shared database or
        live inside candidate-visible directories (vault/ledger)."""
        protected = Path(self._path).expanduser().resolve()
        shared = Path(self._shared_db_path).expanduser().resolve()
        # Safety (`is not False`): reject unless databases are positively different.
        if inode_paths_equal(protected, shared) is not False:
            raise ValueError(
                "protected evaluator database must be separate from the shared database"
            )
        for visible_root in (self._shared_vault_dir, self._shared_ledger_dir):
            root = Path(visible_root).expanduser().resolve()
            # Safety (`is not False`): reject unless containment is positively excluded.
            if inode_path_is_within_anchored(protected, root) is not False:
                raise ValueError("protected evaluator database cannot live in vault or ledger")

    def _harden_permissions(self) -> None:
        """Set owner-only (0o600) permissions on the protected DB file and
        its WAL/SHM sidecar files."""
        expanded = Path(self._path).expanduser().resolve()
        if expanded.exists():
            try:
                expanded.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{expanded}{suffix}")
            if sidecar.exists():
                try:
                    sidecar.chmod(stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass

    def _worker(self, request: dict[str, Any]) -> dict[str, Any]:
        """Execute a request in an isolated subprocess with access to the
        protected DB. The subprocess is never given the DB path in argv/stdin
        (it's passed via environment), and its environment is scrubbed of
        other protected-related variables."""
        worker_env = os.environ.copy()
        # These values exist only in this spawn environment; the parent process
        # environment is never mutated and candidate launches use another path.
        worker_env[PROTECTED_DB_ENV] = self._path
        worker_env[EVAL_KEY_ENV] = self._key.decode("ascii")
        completed = subprocess.run(
            [sys.executable, "-m", _WORKER_MODULE],
            input=json.dumps(request, separators=(",", ":"), sort_keys=True),
            capture_output=True,
            text=True,
            env=worker_env,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            # Worker diagnostics are intentionally not propagated: an SQLite/JSON
            # failure must not turn expected values into a candidate-visible error.
            raise RuntimeError("protected grader worker failed")
        try:
            response = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("protected grader worker returned an invalid response") from exc
        if not isinstance(response, dict):
            raise RuntimeError("protected grader worker returned an invalid response")
        return response

    def _force_wal_checkpoint(self) -> None:
        """Force a WAL checkpoint to flush pending writes to disk. Used for
        testing to verify that expected values persist correctly."""
        if self._is_memory:
            # For in-process :memory: DBs, WAL checkpoint has no effect
            return
        # For file DBs, open a temporary connection to force checkpoint
        temp_conn = _connect(self._path)
        try:
            temp_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            temp_conn.close()

    def _get_expected(self, case_id: str) -> dict[str, Any] | None:
        """Retrieve expected value from in-process DB (for :memory: only)."""
        row = self._connection.execute(
            "SELECT expected_json FROM protected_expected WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            return None
        return decrypt_expected(self._key, row["expected_json"])


def _in_unit_range(value: float) -> bool:
    return 0.0 <= value <= 1.0


def _zero_variance_perfect(result: EvalResult) -> bool:
    """True if EVERY case in a nontrivial (>1 case) held-out result was
    scored perfectly correct — real generalization on unseen data essentially
    never hits exactly 100% across many diverse cases; this is a classic
    leak-shaped signature and is flagged independently of the absolute-jump
    check above (defense in depth: it fires even when the champion was ALSO
    already near-perfect, which the jump/ceiling checks above deliberately
    do not catch)."""
    scores = [case["correct"] for case in result.per_case.values() if "correct" in case]
    return len(scores) > 1 and all(score == 1.0 for score in scores)
