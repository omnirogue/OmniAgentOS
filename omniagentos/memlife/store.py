"""Filesystem store for the memory lifecycle.

Derived from agentic-stack review_state / memory layout; substantially rewritten.

Layout under ``var/memories/<project>/``::

    episodic/events.jsonl
    candidates/<id>.json
    candidates/queue.json   # refreshed pending snapshot
    lessons/<id>.json
    quarantine/<id>.json

Writes are atomic (tmp + fsync + rename). A missing store directory is an
**error**, never an empty queue — the same defect class as an API returning
"0 pending" for a misconfigured path.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from omniagentos.memlife.contracts import Candidate, CandidateStatus, Lesson

_PENDING_STATUSES = frozenset(
    {
        CandidateStatus.STAGED,
        CandidateStatus.REOPENED,
    }
)


class StoreUnavailableError(FileNotFoundError):
    """Raised when the memlife store root is missing or not a directory.

    Callers must not collapse this into "zero pending" — absent and empty are
    distinct states.
    """


class CandidateNotFoundError(KeyError):
    """Raised when a candidate id is not present in the store."""


class LessonNotFoundError(KeyError):
    """Raised when a lesson id is not present in the store."""


def atomic_write_text(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` via tmp + fsync + rename.

    A process death, write error, or full disk before the final rename leaves
    the original file byte-identical (or absent). The temp file lives in the
    same directory so ``os.replace`` is atomic on the same volume.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class MemlifeStore:
    """Per-project on-disk memory lifecycle store."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # --- paths -------------------------------------------------------------

    @property
    def episodic_dir(self) -> Path:
        return self.root / "episodic"

    @property
    def candidates_dir(self) -> Path:
        return self.root / "candidates"

    @property
    def lessons_dir(self) -> Path:
        return self.root / "lessons"

    @property
    def quarantine_dir(self) -> Path:
        return self.root / "quarantine"

    @property
    def queue_path(self) -> Path:
        return self.candidates_dir / "queue.json"

    @property
    def events_path(self) -> Path:
        return self.episodic_dir / "events.jsonl"

    # --- layout / availability ---------------------------------------------

    def ensure_layout(self) -> None:
        """Create the store directory tree. Explicit only — reads never mkdir."""
        for d in (
            self.root,
            self.episodic_dir,
            self.candidates_dir,
            self.lessons_dir,
            self.quarantine_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def _require_root(self) -> None:
        if not self.root.is_dir():
            raise StoreUnavailableError(
                f"memlife store directory missing or not a directory: {self.root}"
            )

    # --- candidates --------------------------------------------------------

    def _candidate_path(self, candidate_id: str) -> Path:
        return self.candidates_dir / f"{candidate_id}.json"

    def save_candidate(self, candidate: Candidate) -> Path:
        self._require_root()
        path = self._candidate_path(candidate.id)
        atomic_write_text(path, candidate.model_dump_json(indent=2) + "\n")
        return path

    def load_candidate(self, candidate_id: str) -> Candidate:
        self._require_root()
        path = self._candidate_path(candidate_id)
        if not path.is_file():
            raise CandidateNotFoundError(candidate_id)
        return Candidate.model_validate_json(path.read_text(encoding="utf-8"))

    def list_candidates(self) -> list[Candidate]:
        self._require_root()
        if not self.candidates_dir.is_dir():
            raise StoreUnavailableError(
                f"candidates directory missing under store: {self.candidates_dir}"
            )
        out: list[Candidate] = []
        for path in sorted(self.candidates_dir.glob("*.json")):
            if path.name == "queue.json":
                continue
            out.append(Candidate.model_validate_json(path.read_text(encoding="utf-8")))
        return out

    def list_pending(self) -> list[Candidate]:
        """Candidates awaiting human judgement (staged or reopened).

        Missing store → :class:`StoreUnavailableError`, never ``[]``.
        """
        return [c for c in self.list_candidates() if c.status in _PENDING_STATUSES]

    # --- queue -------------------------------------------------------------

    def refresh_queue(self) -> list[str]:
        """Rewrite the pending-queue snapshot from candidate files on disk.

        Returns the ordered list of pending candidate ids. Raises on a missing
        store root — never invents an empty queue for an absent path.
        """
        self._require_root()
        pending = sorted(self.list_pending(), key=lambda c: c.id)
        ids = [c.id for c in pending]
        payload: dict[str, Any] = {"pending": ids, "count": len(ids)}
        atomic_write_text(self.queue_path, json.dumps(payload, indent=2) + "\n")
        return ids

    def load_queue(self) -> dict[str, Any]:
        """Load the queue snapshot. Missing store → error, not empty."""
        self._require_root()
        if not self.queue_path.is_file():
            # Snapshot not yet written, but store exists — refresh from truth.
            ids = self.refresh_queue()
            return {"pending": ids, "count": len(ids)}
        data = json.loads(self.queue_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "pending" not in data:
            raise StoreUnavailableError(
                f"queue snapshot unparseable or incomplete: {self.queue_path}"
            )
        pending = data["pending"]
        if not isinstance(pending, list):
            raise StoreUnavailableError(
                f"queue snapshot 'pending' is not a list: {self.queue_path}"
            )
        return {
            "pending": list(pending),
            "count": int(data.get("count", len(pending))),
        }

    # --- lessons -----------------------------------------------------------

    def _lesson_path(self, lesson_id: str) -> Path:
        return self.lessons_dir / f"{lesson_id}.json"

    def save_lesson(self, lesson: Lesson) -> Path:
        self._require_root()
        path = self._lesson_path(lesson.id)
        atomic_write_text(path, lesson.model_dump_json(indent=2) + "\n")
        return path

    def load_lesson(self, lesson_id: str) -> Lesson:
        self._require_root()
        path = self._lesson_path(lesson_id)
        if not path.is_file():
            raise LessonNotFoundError(lesson_id)
        return Lesson.model_validate_json(path.read_text(encoding="utf-8"))

    def list_lessons(self) -> list[Lesson]:
        self._require_root()
        if not self.lessons_dir.is_dir():
            raise StoreUnavailableError(
                f"lessons directory missing under store: {self.lessons_dir}"
            )
        out: list[Lesson] = []
        for path in sorted(self.lessons_dir.glob("*.json")):
            out.append(Lesson.model_validate_json(path.read_text(encoding="utf-8")))
        return out

    def delete_lesson(self, lesson_id: str) -> None:
        """Remove a lesson file if present. Used for graduation rollback only."""
        path = self._lesson_path(lesson_id)
        if path.is_file():
            path.unlink()

    # --- quarantine --------------------------------------------------------

    def save_quarantined(self, candidate: Candidate) -> Path:
        self._require_root()
        path = self.quarantine_dir / f"{candidate.id}.json"
        atomic_write_text(path, candidate.model_dump_json(indent=2) + "\n")
        return path

    def load_quarantined(self, candidate_id: str) -> Candidate:
        self._require_root()
        path = self.quarantine_dir / f"{candidate_id}.json"
        if not path.is_file():
            raise CandidateNotFoundError(candidate_id)
        return Candidate.model_validate_json(path.read_text(encoding="utf-8"))

    def write_quarantine_blob(self, name: str, content: str, *, reason: str = "") -> Path:
        """Retain unparseable raw input. Nothing is silently dropped."""
        self._require_root()
        safe = name.replace("/", "_").replace("..", "_")
        meta = {"reason": reason, "name": name}
        body = json.dumps(meta, indent=2) + "\n---\n" + content
        path = self.quarantine_dir / f"{safe}.raw"
        atomic_write_text(path, body)
        return path

    # --- episodic (append-only JSONL) --------------------------------------

    def append_event_json(self, line: str) -> None:
        """Append one JSON line to the episodic log (fsync'd)."""
        self._require_root()
        self.episodic_dir.mkdir(parents=True, exist_ok=True)
        path = self.events_path
        # Append is not a full rewrite; open, write, flush, fsync the file.
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line if line.endswith("\n") else line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    # --- multi-artifact graduation commit ----------------------------------

    def commit_graduation(
        self,
        *,
        previous: Candidate,
        updated: Candidate,
        lesson: Lesson,
    ) -> None:
        """Persist lesson + updated candidate + refreshed queue, or roll back.

        Partial success is failure. If queue refresh raises after the lesson
        was written, the lesson is removed and the candidate is restored to
        ``previous``.
        """
        self._require_root()
        lesson_path = self._lesson_path(lesson.id)
        cand_path = self._candidate_path(updated.id)
        previous_bytes = cand_path.read_text(encoding="utf-8") if cand_path.is_file() else None
        previous_queue = (
            self.queue_path.read_text(encoding="utf-8") if self.queue_path.is_file() else None
        )
        lesson_written = False
        candidate_written = False
        try:
            atomic_write_text(lesson_path, lesson.model_dump_json(indent=2) + "\n")
            lesson_written = True
            atomic_write_text(cand_path, updated.model_dump_json(indent=2) + "\n")
            candidate_written = True
            # Queue refresh is part of the commit — never swallowed.
            self.refresh_queue()
        except BaseException:
            if lesson_written and lesson_path.is_file():
                try:
                    lesson_path.unlink()
                except OSError:
                    pass
            if candidate_written and previous_bytes is not None:
                try:
                    atomic_write_text(cand_path, previous_bytes)
                except OSError:
                    pass
            if previous_queue is not None:
                try:
                    atomic_write_text(self.queue_path, previous_queue)
                except OSError:
                    pass
            raise


__all__ = [
    "CandidateNotFoundError",
    "LessonNotFoundError",
    "MemlifeStore",
    "StoreUnavailableError",
    "atomic_write_text",
]
