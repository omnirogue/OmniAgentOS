"""Per-run reflection analysis (A4 durable-queue consumer).

Bounded to a single run — never runs the 36h harvest. Writes one retro line
under ``var/retro/run-retros.jsonl`` and, when pending reflection proposals
exist, invokes the Fable gate in-process. Every failure is caught and logged;
:func:`analyze_run` never raises. No model calls live here; the gate is the
only model consumer and it fail-closes on its own.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast

from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.path_containment import (
    inode_path_is_within_anchored,
    inode_paths_equal,
)
from omniagentos.reflection.normalize import normalize_provider
from omniagentos.reflection.settlement import Settlement, classify_settlement
from omniagentos.reflection.taxonomy import classify_content

LOG = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _repo_root() -> Path:
    home = os.environ.get("OMNIAGENTOS_HOME")
    if home:
        return Path(home)
    return _REPO_ROOT


def _open_ro(db_path: str) -> sqlite3.Connection | None:
    """Open SQLite read-only. Never falls back to a writable handle.

    Returns None when the path is missing or the read-only connection cannot
    be opened. Failures are logged at warning level.
    """
    if not db_path or not Path(db_path).exists():
        LOG.warning("perrun: state database is absent (%s); no SQLite source", db_path)
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as exc:
        LOG.warning(
            "perrun: read-only sqlite open failed for %s: %s: %s",
            db_path,
            type(exc).__name__,
            exc,
        )
        return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None
    except Exception as exc:
        LOG.warning(
            "perrun: table_exists(%s) failed: %s: %s",
            name,
            type(exc).__name__,
            exc,
        )
        return False


# SQL-side ceilings for finalize: never SELECT * payload blobs whole, never
# fetch unbounded step history, never materialize huge TEXT then slice in Python.
_PERRUN_SQL_TEXT_CAP = 16 * 1024
_PERRUN_SQL_STEP_CAP = 50


def _sql_text_truncated(raw_len: Any, cap: int) -> bool:
    """True when SQLite reported a pre-substr length strictly above *cap*."""
    try:
        return int(raw_len or 0) > cap
    except (TypeError, ValueError):
        return False


def _fetch_run_bundle(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """Read that run's classify-relevant columns (best-effort, size-capped).

    Projects only columns used by provider/failure collection, truncates large
    TEXT at the source via SQLite ``substr()``, and caps how many steps are
    loaded. Truncation / step-limit hits land in ``bundle["limit_hits"]`` for
    the caller to record as ``source_errors``.
    """
    text_cap = max(int(_PERRUN_SQL_TEXT_CAP), 1)
    step_cap = max(int(_PERRUN_SQL_STEP_CAP), 1)
    bundle: dict[str, Any] = {
        "run": None,
        "steps": [],
        "swarm_run": None,
        "attempts": [],
        "sessions": [],
        "limit_hits": [],
    }
    limit_hits: list[str] = bundle["limit_hits"]
    try:
        if _table_exists(conn, "runs"):
            row = conn.execute(
                """
                SELECT
                    id, model, harness, agent, state, session_ref, manifest_path,
                    substr(error, 1, ?) AS error,
                    COALESCE(length(error), 0) AS _error_len,
                    substr(output_text, 1, ?) AS output_text,
                    COALESCE(length(output_text), 0) AS _output_text_len
                FROM runs WHERE id = ?
                """,
                (text_cap, text_cap, run_id),
            ).fetchone()
            if row is not None:
                d = dict(row)
                if _sql_text_truncated(d.pop("_error_len", 0), text_cap):
                    limit_hits.append("sqlite_text_cap:runs.error")
                if _sql_text_truncated(d.pop("_output_text_len", 0), text_cap):
                    limit_hits.append("sqlite_text_cap:runs.output_text")
                bundle["run"] = d
        if _table_exists(conn, "steps"):
            rows = conn.execute(
                """
                SELECT
                    seq,
                    substr(error, 1, ?) AS error,
                    COALESCE(length(error), 0) AS _error_len
                FROM steps
                WHERE run_id = ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (text_cap, run_id, step_cap),
            ).fetchall()
            steps: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                if _sql_text_truncated(d.pop("_error_len", 0), text_cap):
                    limit_hits.append(f"sqlite_text_cap:steps.seq={d.get('seq')}.error")
                steps.append(d)
            bundle["steps"] = steps
            try:
                # An exact COUNT(*) must visit every matching row, which is
                # unbounded work on the serial finalize path. We only need to
                # know whether the cap was exceeded, and that is answerable
                # from at most cap+1 rows.
                n_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM (SELECT 1 FROM steps WHERE run_id = ? LIMIT ?)",
                    (run_id, step_cap + 1),
                ).fetchone()
                probed = int(n_row["n"] if n_row is not None else 0)
                if probed > step_cap:
                    # Report the threshold, not a total we deliberately did not count.
                    limit_hits.append(f"sqlite_step_cap:>{step_cap}")
            except Exception:
                LOG.warning(
                    "perrun: step-count query failed for %s; step cap not evaluated",
                    run_id,
                    exc_info=True,
                )
        if _table_exists(conn, "swarm_runs"):
            row = conn.execute(
                """
                SELECT
                    id, status,
                    substr(error, 1, ?) AS error,
                    COALESCE(length(error), 0) AS _error_len
                FROM swarm_runs WHERE id = ?
                """,
                (text_cap, run_id),
            ).fetchone()
            if row is not None:
                d = dict(row)
                if _sql_text_truncated(d.pop("_error_len", 0), text_cap):
                    limit_hits.append("sqlite_text_cap:swarm_runs.error")
                bundle["swarm_run"] = d
        if _table_exists(conn, "swarm_attempts"):
            # Prefer exact swarm_run_id match. Cap at 50 rows without sorting
            # every attempt for the run: the schema only indexes swarm_run_id
            # (no (swarm_run_id, started_at)), so ORDER BY started_at builds a
            # TEMP B-TREE over the full matching set before LIMIT applies.
            # ORDER BY rowid DESC walks the run-index slice newest-first and
            # stops at LIMIT — SEARCH, no temp sort, bounded examination.
            rows = conn.execute(
                """
                SELECT
                    provider, model, end_reason,
                    substr(detail, 1, ?) AS detail,
                    COALESCE(length(detail), 0) AS _detail_len
                FROM swarm_attempts
                WHERE swarm_run_id = ?
                ORDER BY rowid DESC
                LIMIT 50
                """,
                (text_cap, run_id),
            ).fetchall()
            attempts: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                if _sql_text_truncated(d.pop("_detail_len", 0), text_cap):
                    limit_hits.append("sqlite_text_cap:swarm_attempts.detail")
                attempts.append(d)
            bundle["attempts"] = attempts
        if _table_exists(conn, "sessions") and bundle["run"] is not None:
            session_ref = bundle["run"].get("session_ref")
            if session_ref:
                # id is the primary key (SEARCH). session_ref has no index in
                # the final schema (089), so `OR session_ref = ?` forces SCAN
                # of the whole table; LIMIT only bounds rows returned, and a
                # missing provider ref walks every session. Without a migration
                # we refuse that unindexed path and look up by id only.
                rows = conn.execute(
                    """
                    SELECT id, session_ref, provider, model
                    FROM sessions
                    WHERE id = ?
                    LIMIT 5
                    """,
                    (session_ref,),
                ).fetchall()
                bundle["sessions"] = [dict(r) for r in rows]
    except Exception as exc:
        LOG.exception("perrun: failed reading run bundle for %s", run_id)
        # The caller decides whether to claim this database as a source, and it
        # cannot decide honestly from an empty bundle that looks like "no rows".
        bundle["read_failed"] = f"{type(exc).__name__}: {exc}"
    return bundle


def _collect_providers(bundle: dict[str, Any], ledger_hits: list[str]) -> list[str]:
    raws: list[str] = []
    run = bundle.get("run") or {}
    for key in ("model", "harness", "agent"):
        val = run.get(key)
        if val:
            raws.append(str(val))
    for attempt in bundle.get("attempts") or []:
        for key in ("provider", "model"):
            val = attempt.get(key)
            if val:
                raws.append(str(val))
    for session in bundle.get("sessions") or []:
        for key in ("provider", "model"):
            val = session.get(key)
            if val:
                raws.append(str(val))
    raws.extend(ledger_hits)

    seen: list[str] = []
    out: list[str] = []
    for raw in raws:
        label = normalize_provider(raw)
        if label not in seen:
            seen.append(label)
            out.append(label)
    if not out:
        out = ["unknown"]
    return out


def _collect_failure_text(bundle: dict[str, Any]) -> str:
    parts: list[str] = []
    run = bundle.get("run") or {}
    for key in ("error", "output_text", "state"):
        val = run.get(key)
        if val:
            parts.append(str(val))
    swarm = bundle.get("swarm_run") or {}
    for key in ("error", "status"):
        val = swarm.get(key)
        if val:
            parts.append(str(val))
    for step in bundle.get("steps") or []:
        err = step.get("error")
        if err:
            parts.append(str(err))
    for attempt in bundle.get("attempts") or []:
        for key in ("detail", "end_reason"):
            val = attempt.get(key)
            if val:
                parts.append(str(val))
    return "\n".join(parts)


# Per-run finalize budget: small, fixed, reuses harvest's size-capped reader.
_PERRUN_FILE_BYTE_CAP = 256 * 1024
# Shared monthly JSONL: hard ceilings on the *scan* (every line, match or not).
# Content retention is separately capped by ``_PERRUN_FILE_BYTE_CAP``.
_PERRUN_JSONL_SCAN_LINE_CAP = 5000
_PERRUN_JSONL_SCAN_BYTE_CAP = 256 * 1024
_PERRUN_MAX_RELATED_FILES = 20
#: Directory entries examined before giving up — a log dir full of
#: non-matching files must not force a full walk at finalize.
_PERRUN_MAX_DIR_ENTRIES = 500

_LEDGER_SUFFIXES = {".log", ".jsonl", ".txt", ""}
_RUN_ID_SAFE_PUNCTUATION = frozenset("._-")
_WINDOWS_RESERVED_RUN_STEMS = frozenset(
    {
        "AUX",
        "CLOCK$",
        "CON",
        "CONIN$",
        "CONOUT$",
        "NUL",
        "PRN",
    }
)
_WINDOWS_NUMBERED_DEVICE_STEM = re.compile(r"(?:COM|LPT)(?:[1-9¹²³])", re.IGNORECASE)
_PERRUN_RUN_ID_CHAR_CAP = 128
_PERRUN_RUN_ID_UTF8_CAP = 240


def _canonical_run_id_segment(value: object) -> str | None:
    """Return an opaque canonical filesystem segment, or fail closed.

    Run identifiers become directory and flat-log names below ``var/logs``.
    They must therefore arrive already canonical: normalization is never an
    admission step.  The checks cover both POSIX and Windows spellings so a
    database copied between platforms cannot reinterpret an accepted ID.
    """
    if not isinstance(value, str) or not value:
        return None
    if len(value) > _PERRUN_RUN_ID_CHAR_CAP:
        return None
    try:
        if len(value.encode("utf-8")) > _PERRUN_RUN_ID_UTF8_CAP:
            return None
    except UnicodeError:
        return None
    if value != value.strip() or not value.isprintable():
        return None
    if unicodedata.normalize("NFC", value) != value:
        return None
    if not value[0].isalnum():
        return None
    if any(not (char.isalnum() or char in _RUN_ID_SAFE_PUNCTUATION) for char in value):
        return None
    if value.endswith("."):
        return None
    windows_stem = value.split(".", 1)[0]
    if windows_stem.upper() in _WINDOWS_RESERVED_RUN_STEMS:
        return None
    if _WINDOWS_NUMBERED_DEVICE_STEM.fullmatch(windows_stem):
        return None
    return value


def _stat_results_same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare two captured filesystem identities without re-resolving paths."""
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


@dataclass(frozen=True)
class _RelatedFileCandidate:
    """One enumerated path and the exact leaf identity seen at admission."""

    path: Path
    admitted_stat: os.stat_result | None = None
    run_components: tuple[str, ...] | None = None


@dataclass(frozen=True)
class _CanonicalLogChild:
    """An exact stored child spelling bound to its enumerated identity."""

    path: Path
    entry_stat: os.stat_result


def _canonical_log_child_binding(
    logs_root: Path,
    name: str,
    *,
    directory: bool,
    require_unique_inode: bool = False,
) -> _CanonicalLogChild | None:
    """Bind an exact, non-link child to the identity returned by enumeration."""
    try:
        with os.scandir(logs_root) as entries:
            for entry in entries:
                if entry.name != name:
                    continue
                if entry.is_symlink():
                    return None
                entry_stat = entry.stat(follow_symlinks=False)
                if directory:
                    if not stat.S_ISDIR(entry_stat.st_mode):
                        return None
                elif not stat.S_ISREG(entry_stat.st_mode):
                    return None
                if require_unique_inode and entry_stat.st_nlink != 1:
                    return None
                child = Path(entry.path)
                expected = logs_root / name
                current_stat = os.stat(child, follow_symlinks=False)
                if not _stat_results_same_inode(entry_stat, current_stat):
                    return None
                if inode_paths_equal(child, expected) is not True:
                    return None
                return _CanonicalLogChild(child, entry_stat)
    except (OSError, ValueError):
        return None
    return None


def _canonical_log_child(
    logs_root: Path,
    name: str,
    *,
    directory: bool,
    require_unique_inode: bool = False,
) -> Path | None:
    """Return an exact, non-link child whose enumerated identity is still live.

    Looking up ``logs_root / name`` is insufficient on case-insensitive
    filesystems: ``RUN_PEER`` can resolve to a stored ``run_peer`` entry.
    Likewise, containment alone permits a run-directory symlink to a sibling
    run because both endpoints remain below ``logs_root``.  Enumerating the
    already-anchored parent exposes the stored component spelling; the
    ``DirEntry.stat`` captures the object that was enumerated, then ``lstat``
    and an explicit device/inode comparison bind the returned path to that
    same object. Flat per-run logs additionally require a unique inode: a peer
    hard link is not a run-owned whole-file source.
    """
    binding = _canonical_log_child_binding(
        logs_root,
        name,
        directory=directory,
        require_unique_inode=require_unique_inode,
    )
    return binding.path if binding is not None else None


def _canonical_logs_root(root: Path) -> tuple[Path | None, str]:
    """Resolve the exact non-link ``root/var/logs`` chain.

    ``absent`` is distinct from ``invalid`` so a run with no per-run log tree
    may still inspect an explicitly supplied shared JSONL source.  A present
    case alias, symlink, or wrong-type component is invalid and stops all
    related-file enumeration.
    """
    requested_var = root / "var"
    var_dir = _canonical_log_child(root, "var", directory=True)
    if var_dir is None:
        status = "invalid" if os.path.lexists(requested_var) else "absent"
        return None, status

    requested_logs = var_dir / "logs"
    logs_root = _canonical_log_child(var_dir, "logs", directory=True)
    if logs_root is None:
        status = "invalid" if os.path.lexists(requested_logs) else "absent"
        return None, status
    return logs_root, "ok"


def _is_relative(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _path_label(path: Path, root: Path) -> str:
    return str(path.relative_to(root)) if _is_relative(path, root) else str(path)


def _resolve_extra_path(raw: str | Path, root: Path) -> Path:
    p = Path(str(raw))
    if p.is_absolute():
        return p
    return root / p


def _list_related_file_candidates(
    run_id: str,
    root: Path,
    extra_paths: list[str | Path] | None = None,
    limit_hits_dir: list[str] | None = None,
) -> list[_RelatedFileCandidate]:
    """Enumerate candidate ledger/log paths for a run (no content I/O).

    ``limit_hits_dir`` receives a marker when directory enumeration stops at
    the entry ceiling, so a truncated scan is never mistaken for an exhaustive
    one by the caller that reports sources.
    """
    rid = _canonical_run_id_segment(run_id)
    if rid is None:
        if limit_hits_dir is not None:
            limit_hits_dir.append("invalid_run_id")
        return []

    candidates: list[_RelatedFileCandidate] = []
    if limit_hits_dir is None:
        limit_hits_dir = []
    seen: set[str] = set()

    def _add(
        path: Path,
        *,
        admitted_stat: os.stat_result | None = None,
        run_components: tuple[str, ...] | None = None,
    ) -> None:
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            _RelatedFileCandidate(
                path=path,
                admitted_stat=admitted_stat,
                run_components=run_components,
            )
        )

    logs_root, logs_root_status = _canonical_logs_root(root)
    if logs_root_status == "invalid":
        limit_hits_dir.append("invalid_logs_root")
        return []
    if logs_root is not None:
        requested_logs_dir = logs_root / rid
        logs_dir = _canonical_log_child(logs_root, rid, directory=True)
        if logs_dir is not None:
            try:
                # os.scandir streams entries; sorted(iterdir()) materializes and
                # sorts the whole directory before the cap can stop the loop.
                # Bound the entries EXAMINED too, so a directory full of
                # non-matching files cannot force a full walk.
                examined = 0
                with os.scandir(logs_dir) as entries:
                    for entry in entries:
                        examined += 1
                        if examined > _PERRUN_MAX_DIR_ENTRIES:
                            limit_hits_dir.append(f"dir_scan_cap:{logs_dir}")
                            break
                        if not entry.is_file():
                            continue
                        path = Path(entry.path)
                        if path.suffix.lower() not in _LEDGER_SUFFIXES:
                            continue
                        _add(
                            path,
                            admitted_stat=entry.stat(follow_symlinks=False),
                            run_components=("var", "logs", rid, entry.name),
                        )
                        if len(candidates) >= _PERRUN_MAX_RELATED_FILES:
                            break
            except Exception:
                LOG.exception("perrun: ledger scan failed for %s", rid)
        else:
            # A present-but-noncanonical spelling (case-fold alias, symlink, or
            # wrong type) is an invalid run binding.  Stop before considering a
            # flat log or caller-supplied paths.
            if os.path.lexists(requested_logs_dir):
                limit_hits_dir.append("invalid_run_directory")
                return []

            flat_name = f"{rid}.log"
            requested_flat = logs_root / flat_name
            flat = _canonical_log_child_binding(
                logs_root,
                flat_name,
                directory=False,
                require_unique_inode=True,
            )
            if flat is not None:
                _add(
                    flat.path,
                    admitted_stat=flat.entry_stat,
                    run_components=("var", "logs", flat_name),
                )
            elif os.path.lexists(requested_flat):
                limit_hits_dir.append("invalid_run_log")
                return []

    for raw in extra_paths or []:
        if raw is None or str(raw).strip() == "":
            continue
        _add(_resolve_extra_path(raw, root))
        if len(candidates) >= _PERRUN_MAX_RELATED_FILES:
            break

    return candidates[:_PERRUN_MAX_RELATED_FILES]


def _is_run_scoped_path(path: Path, run_id: str, root: Path) -> bool:
    """True when *path* is already attributable to *run_id* by location.

    Per-run log directories (``var/logs/<run_id>/…``) and the flat
    ``var/logs/<run_id>.log`` are safe to sample as a whole. Shared/append-only
    ledgers (monthly ``runs-YYYYMM.jsonl``, etc.) are not.
    """
    rid = _canonical_run_id_segment(run_id)
    if rid is None:
        return False
    logs_root, logs_root_status = _canonical_logs_root(root)
    if logs_root is None or logs_root_status != "ok":
        return False

    # Prove every scope edge independently.  Checking only ``path`` against
    # ``logs_dir`` would trust a run directory symlink whose target is outside
    # the logs tree.  ``False`` and indeterminate ``None`` both fail closed.
    if inode_path_is_within_anchored(logs_root, root) is not True:
        return False
    logs_dir = _canonical_log_child(logs_root, rid, directory=True)
    if logs_dir is not None:
        if inode_path_is_within_anchored(logs_dir, logs_root) is not True:
            return False
        if inode_path_is_within_anchored(path, logs_dir) is True:
            return True

    flat = _canonical_log_child(
        logs_root,
        f"{rid}.log",
        directory=False,
        require_unique_inode=True,
    )
    if flat is None:
        return False
    if inode_path_is_within_anchored(flat, logs_root) is not True:
        return False
    return inode_paths_equal(path, flat) is True


def _jsonl_line_run_id(line: str) -> str | None:
    """Return the ``run_id`` field from a JSONL line, or None if unparseable."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    rid = obj.get("run_id")
    if rid is None:
        return None
    return str(rid)


def _read_jsonl_lines_for_run(
    path: Path,
    run_id: str,
    *,
    content_cap: int,
    scan_line_cap: int | None = None,
    scan_byte_cap: int | None = None,
) -> tuple[str, int, list[str]]:
    """Read a shared JSONL ledger; keep only lines for *run_id*.

    Hard ceilings (always apply, including when no record matches):

    * **scan_byte_cap** — at most this many bytes are read from the file.
      Prefer the **tail** (recent append-only records) when the file is larger.
    * **scan_line_cap** — at most this many lines are examined (match or not)
      within that window, newest-first.
    * **content_cap** — at most this many UTF-8 bytes of *matched* content are
      retained. The first matching line is truncated to fit; it is never kept
      whole when it alone exceeds the cap.

    Returns ``(matched_content, matched_line_count, limit_hits)`` where
    ``limit_hits`` are tokens such as ``file_scan_cap``, ``file_line_cap``,
    ``file_byte_cap`` (caller prefixes the path label for ``source_errors``).
    """
    line_cap = _PERRUN_JSONL_SCAN_LINE_CAP if scan_line_cap is None else int(scan_line_cap)
    byte_scan = _PERRUN_JSONL_SCAN_BYTE_CAP if scan_byte_cap is None else int(scan_byte_cap)
    if line_cap < 1:
        line_cap = 1
    if byte_scan < 1:
        byte_scan = 1
    if content_cap < 0:
        content_cap = 0

    limit_hits: list[str] = []

    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        start = 0
        if size > byte_scan:
            start = size - byte_scan
            limit_hits.append("file_scan_cap")
        fh.seek(start)
        raw = fh.read(byte_scan if start > 0 else size)

    if start > 0:
        # Window may start mid-line; drop the partial first line.
        nl = raw.find(b"\n")
        if nl < 0:
            raw = b""
        else:
            raw = raw[nl + 1 :]

    text = raw.decode("utf-8", errors="replace")
    all_lines = text.splitlines()

    # Newest-first within the byte window: a run's own record is near the tail.
    # Cap how many lines we examine (matching or not).
    if len(all_lines) > line_cap:
        examine = list(reversed(all_lines[-line_cap:]))
        if "file_line_cap" not in limit_hits:
            limit_hits.append("file_line_cap")
    else:
        examine = list(reversed(all_lines))
        # If the tail window excluded earlier lines via byte cap, line_cap may
        # still not fire — file_scan_cap already records that incompleteness.

    matched: list[str] = []
    matched_bytes = 0
    count = 0
    content_capped = False

    for line in examine:
        line_rid = _jsonl_line_run_id(line)
        if line_rid is None or line_rid != run_id:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        encoded = stripped.encode("utf-8", errors="replace")
        encoded_len = len(encoded)

        if content_cap <= 0:
            content_capped = True
            break

        if matched_bytes >= content_cap:
            content_capped = True
            break

        # Cap applies to the first matching line too: truncate, never keep whole.
        if matched_bytes + encoded_len > content_cap:
            remain = content_cap - matched_bytes
            if remain > 0:
                piece = encoded[:remain].decode("utf-8", errors="replace")
                if piece:
                    matched.append(piece)
                    matched_bytes += len(piece.encode("utf-8", errors="replace"))
                    count += 1
            content_capped = True
            break

        matched.append(stripped)
        matched_bytes += encoded_len
        count += 1
        if matched_bytes >= content_cap:
            content_capped = True
            break

    # Restore chronological order (we examined newest-first).
    matched.reverse()

    if content_capped and "file_byte_cap" not in limit_hits:
        limit_hits.append("file_byte_cap")

    return "\n".join(matched), count, limit_hits


class _BoundRunFileError(Exception):
    """Fail-closed descriptor-chain admission error."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _descriptor_relative_open_supported() -> bool:
    """Whether this OS exposes every primitive required for a safe chain."""
    return (
        os.open in os.supports_dir_fd
        and os.scandir in os.supports_fd
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and bool(getattr(os, "O_DIRECTORY", 0))
    )


def _exact_child_stat(parent_fd: int, name: str) -> os.stat_result:
    """Enumerate one exact stored child name relative to an opened directory."""
    with os.scandir(parent_fd) as entries:
        for entry in entries:
            if entry.name == name:
                return entry.stat(follow_symlinks=False)
    raise _BoundRunFileError("identity_changed")


def _open_exact_directory_child(parent_fd: int, name: str) -> int:
    """Open one exact non-link directory and bind it to its enumerated inode."""
    entry_stat = _exact_child_stat(parent_fd, name)
    if not stat.S_ISDIR(entry_stat.st_mode):
        raise _BoundRunFileError("identity_changed")

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        child_fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise _BoundRunFileError("identity_changed") from exc
    try:
        opened_stat = os.fstat(child_fd)
        if not stat.S_ISDIR(opened_stat.st_mode) or not _stat_results_same_inode(
            entry_stat, opened_stat
        ):
            raise _BoundRunFileError("identity_changed")
    except Exception:
        os.close(child_fd)
        raise
    return child_fd


def _open_bound_run_file(
    candidate: _RelatedFileCandidate, root: Path
) -> tuple[int, os.stat_result]:
    """Open an admitted run file through an exact descriptor-relative chain.

    The repository root is opened once without following a symlink. Every
    descendant is then enumerated and opened relative to its already-bound
    parent descriptor. The returned descriptor is the leaf whose identity was
    captured when the candidate was first enumerated.
    """
    admitted_stat = candidate.admitted_stat
    components = candidate.run_components
    if admitted_stat is None or components is None:
        raise _BoundRunFileError("identity_changed")
    if not stat.S_ISREG(admitted_stat.st_mode):
        raise _BoundRunFileError("identity_changed")
    if admitted_stat.st_nlink != 1:
        raise _BoundRunFileError("not_unique")
    if not _descriptor_relative_open_supported():
        raise _BoundRunFileError("unsupported")

    root_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_entry_stat = os.stat(root, follow_symlinks=False)
        root_fd = os.open(root, root_flags)
    except OSError as exc:
        raise _BoundRunFileError("identity_changed") from exc

    directory_fds = [root_fd]
    leaf_fd = -1
    try:
        root_opened_stat = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_entry_stat.st_mode)
            or not stat.S_ISDIR(root_opened_stat.st_mode)
            or not _stat_results_same_inode(root_entry_stat, root_opened_stat)
        ):
            raise _BoundRunFileError("identity_changed")

        parent_fd = root_fd
        for component in components[:-1]:
            parent_fd = _open_exact_directory_child(parent_fd, component)
            directory_fds.append(parent_fd)

        leaf_name = components[-1]
        leaf_entry_stat = _exact_child_stat(parent_fd, leaf_name)
        if (
            not stat.S_ISREG(leaf_entry_stat.st_mode)
            or leaf_entry_stat.st_nlink != 1
            or not _stat_results_same_inode(admitted_stat, leaf_entry_stat)
        ):
            raise _BoundRunFileError("identity_changed")

        leaf_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            leaf_fd = os.open(leaf_name, leaf_flags, dir_fd=parent_fd)
        except OSError as exc:
            raise _BoundRunFileError("identity_changed") from exc
        opened_stat = os.fstat(leaf_fd)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_nlink != 1
            or not _stat_results_same_inode(admitted_stat, opened_stat)
            or not _stat_results_same_inode(leaf_entry_stat, opened_stat)
        ):
            raise _BoundRunFileError("identity_changed")
        return leaf_fd, opened_stat
    except Exception:
        if leaf_fd >= 0:
            os.close(leaf_fd)
        raise
    finally:
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


class _OpenedFileView:
    """Path-shaped compatibility view whose bytes come only from an open file."""

    def __init__(self, handle: BinaryIO, name: str, byte_cap: int) -> None:
        self._handle = handle
        self.name = name
        self._byte_cap = max(byte_cap, 0)

    def exists(self) -> bool:
        return True

    def stat(self) -> os.stat_result:
        return os.fstat(self._handle.fileno())

    def fileno(self) -> int:
        return self._handle.fileno()

    def read_text(self, *, encoding: str, errors: str) -> str:
        self._handle.seek(0)
        raw = self._handle.read(self._byte_cap + 1)
        if len(raw) > self._byte_cap:
            raise OSError("opened file grew beyond the admitted byte cap")
        return raw.decode(encoding, errors=errors)


def _sample_oversized_open_file(
    handle: BinaryIO,
    byte_cap: int,
    total_bytes: int,
) -> tuple[str, bool, int]:
    """Bounded head/tail/taxonomy sample from the already-open file."""
    if byte_cap <= 0:
        return "", True, total_bytes
    half_cap = max(byte_cap // 2, 1)
    handle.seek(0)
    head_bytes = handle.read(half_cap)
    handle.seek(max(total_bytes - half_cap, 0))
    tail_bytes = handle.read(half_cap)
    head_text = head_bytes.decode("utf-8", errors="replace")
    tail_text = tail_bytes.decode("utf-8", errors="replace")

    from omniagentos.reflection.taxonomy import TAXONOMY_PATTERNS

    patterns = [pattern for tag_patterns in TAXONOMY_PATTERNS.values() for pattern in tag_patterns]
    combined_regex = re.compile("|".join(patterns), re.IGNORECASE)
    grep_byte_budget = 1 * 1024 * 1024
    handle.seek(0)
    raw_window = handle.read(grep_byte_budget)
    matching_lines: list[str] = []
    retained_match_bytes = 0
    for line_no, line in enumerate(
        raw_window.decode("utf-8", errors="replace").splitlines(),
        1,
    ):
        if (
            line_no > 20_000
            or len(matching_lines) >= 100
            or retained_match_bytes >= grep_byte_budget
        ):
            break
        if not line or not combined_regex.search(line):
            continue
        prefix = f"Line {line_no}: "
        prefix_bytes = len(prefix.encode("utf-8", errors="replace"))
        remain = grep_byte_budget - retained_match_bytes - prefix_bytes
        if remain <= 0:
            break
        stripped = line.strip()
        encoded = stripped.encode("utf-8", errors="replace")
        if len(encoded) > remain:
            stripped = encoded[:remain].decode("utf-8", errors="replace")
            entry = f"{prefix}{stripped}…[line_truncated]"
        else:
            entry = f"{prefix}{stripped}"
        matching_lines.append(entry)
        retained_match_bytes += len(entry.encode("utf-8", errors="replace"))

    grep_section = ""
    if matching_lines:
        grep_section = "\n\n[ERRORS GREPPED FROM TRUNCATED CONTENT]\n" + "\n".join(matching_lines)
    sampled_text = (
        f"{head_text}\n... [TRUNCATED {total_bytes - byte_cap} BYTES] ..."
        f"\n{tail_text}{grep_section}"
    )
    return sampled_text, True, total_bytes


def _read_and_sample_open_file(
    handle: BinaryIO,
    name: str,
    byte_cap: int,
) -> tuple[str, bool, int]:
    """Read only from *handle*; never resolve or reopen the admitted pathname."""
    total_bytes = os.fstat(handle.fileno()).st_size
    if total_bytes > byte_cap:
        return _sample_oversized_open_file(handle, byte_cap, total_bytes)

    # Retain the shared reader's small-file seam for callers/tests, but supply a
    # deliberately non-path-like view. Its stat and bytes are both backed by
    # this exact descriptor; attempting a pathname open on the view fails.
    from omniagentos.reflection.adapters import read_and_sample_file

    view = _OpenedFileView(handle, name, byte_cap)
    return read_and_sample_file(cast(Any, view), byte_cap)


def _read_related_files(
    run_id: str,
    root: Path,
    *,
    extra_paths: list[str | Path] | None = None,
    byte_cap: int | None = None,
) -> tuple[list[str], list[str], list[str], str]:
    """Open related ledger/log files with run-scoped content selection.

    Returns ``(sources_read, source_errors, provider_raw_hints, content_blob)``.

    * Per-run paths (``var/logs/<run_id>/…``) are size-capped whole-file reads
      (harvest ``read_and_sample_file``; taxonomy grep is also scan-bounded).
    * Shared append-only JSONL ledgers (e.g. monthly ``runs-YYYYMM.jsonl`` via
      ``manifest_path``) contribute **only** lines whose ``run_id`` equals this
      run, under hard scan line/byte ceilings — never the whole monthly file.
    * Any other non-run-scoped path that cannot be attributed is recorded as
      ``file_not_run_scoped:<path>`` and is not fed to the classifier.

    Only under-cap files that were actually opened (and, for shared JSONL,
    successfully line-filtered without a scan/content limit) go into
    ``sources_read``. Missing, over-cap, scan-capped, unreadable, and
    non-attributable paths go into ``source_errors``. Sampled / truncated
    content is still returned for taxonomy when present (the sample was read;
    it is just not claimed as a clean full source).
    """
    # Resolve at call time so tests can monkeypatch ``_PERRUN_FILE_BYTE_CAP``.
    cap = max(_PERRUN_FILE_BYTE_CAP if byte_cap is None else byte_cap, 0)

    sources: list[str] = []
    errors: list[str] = []
    provider_hints: list[str] = []
    content_parts: list[str] = []

    for candidate in _list_related_file_candidates(run_id, root, extra_paths, errors):
        path = candidate.path
        label = _path_label(path, root)
        run_scoped = candidate.run_components is not None and _is_run_scoped_path(
            path, run_id, root
        )

        if candidate.run_components is not None and not run_scoped:
            errors.append(f"file_not_run_scoped:{label}")
            continue

        # Shared / non-run-scoped paths must not be whole-file sampled.
        if not run_scoped:
            if not path.exists():
                errors.append(f"file_missing:{label}")
                continue
            if not path.is_file():
                errors.append(f"file_unreadable:{label}")
                continue
            if path.suffix.lower() == ".jsonl":
                try:
                    content, _matched, limit_hits = _read_jsonl_lines_for_run(
                        path, run_id, content_cap=cap
                    )
                except Exception as exc:
                    LOG.warning(
                        "perrun: shared jsonl read failed for %s (%s): %s: %s",
                        run_id,
                        label,
                        type(exc).__name__,
                        exc,
                    )
                    errors.append(f"file_read_failed:{type(exc).__name__}:{label}")
                    continue
                for hit in limit_hits:
                    errors.append(f"{hit}:{label}")
                if content:
                    content_parts.append(content)
                if limit_hits:
                    # Bounded/truncated — never claim a complete read.
                    if content:
                        provider_hints.append(path.name)
                    continue
                # Opened and line-filtered under budget — clean source claim.
                sources.append(label)
                provider_hints.append(path.name)
                continue

            # Cannot attribute a non-JSONL shared path to this run.
            errors.append(f"file_not_run_scoped:{label}")
            continue

        # Run-scoped path: size-capped whole-file sample is safe only from the
        # exact descriptor bound to the enumerated leaf identity.
        # A multiply linked inode can also belong to a peer run even when this
        # pathname is below the requested directory.
        if candidate.admitted_stat is not None and candidate.admitted_stat.st_nlink != 1:
            errors.append(f"file_not_uniquely_linked:{label}")
            continue
        leaf_fd = -1
        try:
            leaf_fd, opened_stat = _open_bound_run_file(candidate, root)
        except _BoundRunFileError as exc:
            if exc.reason == "not_unique":
                errors.append(f"file_not_uniquely_linked:{label}")
            elif exc.reason == "unsupported":
                errors.append(f"secure_open_unavailable:{label}")
            else:
                errors.append(f"file_identity_changed:{label}")
            continue
        try:
            with open(leaf_fd, "rb", closefd=False) as handle:
                content, byte_cap_hit, _total = _read_and_sample_open_file(
                    handle,
                    path.name,
                    cap,
                )
                after_read_stat = os.fstat(handle.fileno())
        except Exception as exc:
            LOG.warning(
                "perrun: related file read failed for %s (%s): %s: %s",
                run_id,
                label,
                type(exc).__name__,
                exc,
            )
            errors.append(f"file_read_failed:{type(exc).__name__}:{label}")
            continue
        finally:
            if leaf_fd >= 0:
                os.close(leaf_fd)
        if not (
            stat.S_ISREG(after_read_stat.st_mode)
            and _stat_results_same_inode(opened_stat, after_read_stat)
            and after_read_stat.st_nlink == 1
        ):
            errors.append(f"file_identity_changed:{label}")
            continue

        if byte_cap_hit:
            # Opened and sampled, but not a clean under-cap read claim.
            errors.append(f"file_byte_cap:{label}")
            if content:
                content_parts.append(content)
                provider_hints.append(path.name)
            continue

        if content == "" and opened_stat.st_size > 0:
            # Exists with bytes but the shared reader returned nothing usable.
            errors.append(f"file_unreadable:{label}")
            continue

        sources.append(label)
        provider_hints.append(path.name)
        if content:
            content_parts.append(content)

    return sources, errors, provider_hints, "\n".join(content_parts)


def _has_pending_proposals(db_path: str) -> bool:
    conn = _open_ro(db_path)
    if conn is None:
        return False
    try:
        # Probe first so a corrupt/unreadable DB raises into the logged except
        # below (unknown-as-favourable is forbidden). Missing tables stay quiet.
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        if not _table_exists(conn, "reflection_proposals"):
            return False
        row = conn.execute(
            "SELECT 1 FROM reflection_proposals WHERE status = 'pending' LIMIT 1"
        ).fetchone()
        return row is not None
    except Exception as exc:
        LOG.warning(
            "perrun: pending-proposals check failed for %s: %s: %s",
            db_path,
            type(exc).__name__,
            exc,
        )
        return False
    finally:
        try:
            conn.close()
        except Exception:
            LOG.warning("perrun: failed closing the state database handle", exc_info=True)


def _append_retro(root: Path, payload: dict[str, Any]) -> Path:
    retro_dir = root / "var" / "retro"
    retro_dir.mkdir(parents=True, exist_ok=True)
    path = retro_dir / "run-retros.jsonl"
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return path


def analyze_run(run_id: str, *, db_path: str | None = None) -> dict[str, Any]:
    """Analyze a single finished run; never raises.

    Returns a small status dict for callers/tests. Side effects: one append to
    ``var/retro/run-retros.jsonl`` when enough state is readable; optional
    in-process Fable gate when pending proposals exist.
    """
    result: dict[str, Any] = {
        "run_id": run_id,
        "ok": False,
        "retro_written": False,
        "gate_invoked": False,
        "failure_tags": [],
        "providers": [],
        "sources_read": [],
        "source_errors": [],
        "settlement": Settlement.UNGATEABLE.value,
        "error": None,
    }
    try:
        rid = _canonical_run_id_segment(run_id)
        if rid is None:
            result["error"] = "invalid run_id"
            return result

        db = db_path or default_db_path()
        root = _repo_root()
        sources_read: list[str] = []
        source_errors: list[str] = []
        providers: list[str] = ["unknown"]
        failure_tags: list[str] = []

        bundle: dict[str, Any] = {
            "run": None,
            "steps": [],
            "swarm_run": None,
            "attempts": [],
            "sessions": [],
        }
        conn = _open_ro(db)
        if conn is None:
            # Either way the run's primary evidence is unavailable, and that has
            # to show in the record: a retro with no sources is not a clean
            # retro. Distinguish the two causes but degrade on both.
            if db and Path(db).exists():
                source_errors.append(f"sqlite_unavailable:{db}")
            else:
                source_errors.append(f"sqlite_missing:{db}")
        else:
            try:
                # Probe readability before claiming a SQLite source. Corrupt
                # files often open under mode=ro but fail on the first query.
                try:
                    conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
                except Exception as exc:
                    LOG.warning(
                        "perrun: sqlite unreadable for run %s (%s): %s: %s",
                        rid,
                        db,
                        type(exc).__name__,
                        exc,
                    )
                    source_errors.append(f"sqlite_unreadable:{type(exc).__name__}:{db}")
                else:
                    try:
                        bundle = _fetch_run_bundle(conn, rid)
                        # Claiming a source is a claim to have read evidence. A
                        # failed query or an absent run row is not evidence, and
                        # recording it as one is what makes ok=True meaningless.
                        if bundle.get("read_failed"):
                            source_errors.append(
                                f"sqlite_query_failed:{db} ({bundle['read_failed']})"
                            )
                        elif bundle.get("run"):
                            sources_read.append(f"sqlite:{db}")
                        else:
                            source_errors.append(f"sqlite_run_absent:{db}:{rid}")
                            LOG.warning(
                                "perrun: no row for %s in %s; not claiming it as a source",
                                rid,
                                db,
                            )
                        # SQL-side text/step ceilings → source_errors (honest).
                        for hit in bundle.get("limit_hits") or []:
                            source_errors.append(str(hit))
                    except Exception as exc:
                        LOG.warning(
                            "perrun: sqlite read failed for run %s (%s): %s: %s",
                            rid,
                            db,
                            type(exc).__name__,
                            exc,
                        )
                        source_errors.append(f"sqlite_read_failed:{type(exc).__name__}:{db}")
            finally:
                try:
                    conn.close()
                except Exception:
                    LOG.warning("perrun: failed closing the state database handle", exc_info=True)

        extra_paths: list[str | Path] = []
        run_row = bundle.get("run") or {}
        manifest_path = run_row.get("manifest_path") if isinstance(run_row, dict) else None
        if manifest_path:
            extra_paths.append(manifest_path)

        (
            ledger_sources,
            ledger_errors,
            ledger_hints,
            ledger_text,
        ) = _read_related_files(rid, root, extra_paths=extra_paths)
        sources_read.extend(ledger_sources)
        source_errors.extend(ledger_errors)

        providers = _collect_providers(bundle, ledger_hints)
        failure_text = _collect_failure_text(bundle)
        if ledger_text:
            failure_text = f"{failure_text}\n{ledger_text}" if failure_text else ledger_text
        if failure_text:
            try:
                failure_tags = classify_content(failure_text)
            except Exception:
                LOG.exception("perrun: taxonomy classify failed for %s", rid)
                failure_tags = []

        retro = {
            "run_id": rid,
            "ts": utc_now_iso(),
            "providers": providers,
            "failure_tags": failure_tags,
            "sources_read": sources_read,
            "source_errors": source_errors,
        }
        try:
            _append_retro(root, retro)
            result["retro_written"] = True
        except Exception:
            LOG.exception("perrun: failed writing retro for %s", rid)

        result["providers"] = providers
        result["failure_tags"] = failure_tags
        result["sources_read"] = sources_read
        result["source_errors"] = source_errors

        # Gate only when pending proposals exist — never invent work.
        try:
            if _has_pending_proposals(db):
                from omniagentos.reflection import fable_gate

                fable_gate.run_gate(db_path=db)
                result["gate_invoked"] = True
        except Exception:
            LOG.exception("perrun: fable gate invocation failed for %s", rid)

        # ok means the analysis rests on evidence, not merely that nothing threw.
        # A retro that read no source is a refusal to conclude, not a success —
        # and it now says so explicitly via the one shared three-valued
        # classifier rather than being folded into a bare boolean.
        settlement = classify_settlement(evidence=bool(result.get("sources_read")))
        result["settlement"] = settlement.value
        result["ok"] = settlement is Settlement.OK
        if not result["ok"]:
            LOG.warning(
                "perrun: analyzed %s with no readable source; ok=False (errors=%s)",
                rid,
                source_errors,
            )
        return result
    except Exception as exc:  # noqa: BLE001 — never raise out of analyze_run
        LOG.exception("perrun: analyze_run failed for %s", run_id)
        result["error"] = f"{type(exc).__name__}:{exc}"
        result["settlement"] = classify_settlement(error=exc).value
        return result
