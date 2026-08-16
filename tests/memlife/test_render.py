"""L5 acceptance: render ACCEPTED lessons and surface them through recall.

Decisive: a graduated fixture lesson is returned by the recall path for a
matching query, end-to-end. The lesson text is asserted non-empty FIRST —
``"" in text`` is always true and is a vacuous assertion.

Counterfeit: a PROVISIONAL lesson must NOT surface through recall. The
accepted-only filter is load-bearing, not cosmetic.

Counterfeit: a correctly signed but non-accepted rendered row must still fail
the database cross-check at read time.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omniagentos.memlife.contracts import Lesson, LessonStatus, Provenance
from omniagentos.memlife.render import (
    LESSONS_BEGIN,
    LESSONS_END,
    _sign_block,
    render_lessons,
    search_rendered_lessons,
)

NOW = datetime(2026, 7, 28, 15, 0, tzinfo=UTC)

# Unique tokens so lexical overlap cannot accidentally hit unrelated content.
ACCEPTED_CLAIM = (
    "Sandbox clones must never share a git worktree when committing parallel lanes."
)
PROVISIONAL_CLAIM = (
    "Unreviewed claim about zebra-token-quokka routing that must never reach prompts."
)
RETRACTED_CLAIM = "Obsolete retracted guidance about unicorn-frobnicator defaults."


def _lesson(
    *,
    id: str,
    claim: str,
    status: LessonStatus,
    conditions: str = "",
) -> Lesson:
    return Lesson(
        id=id,
        candidate_id=f"cand_{id}",
        claim=claim,
        conditions=conditions,
        status=status,
        graduated_at=NOW,
        graduated_by="owner",
        evidence_ids=["ev_1"],
        provenance=Provenance(source="test"),
    )


@pytest.fixture()
def memories_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "memories"
    root.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_MEMORIES_DIR", str(root))
    return root


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A minimal authoritative lesson table for verified rendered recall."""
    path = tmp_path / "memlife.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE memlife_lessons (id TEXT PRIMARY KEY, status TEXT)")
        connection.commit()
    finally:
        connection.close()
    monkeypatch.setenv("OMNIAGENTOS_DB", str(path))
    return path


def _set_lesson_status(db_path: Path, lesson_id: str, status: str) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO memlife_lessons (id, status) VALUES (?, ?)",
            (lesson_id, status),
        )
        connection.commit()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Render unit behaviour
# ---------------------------------------------------------------------------


class TestRenderAcceptedOnly:
    def test_only_accepted_lessons_land_between_sentinels(
        self, memories_root: Path
    ) -> None:
        lessons = [
            _lesson(id="les_ok", claim=ACCEPTED_CLAIM, status=LessonStatus.ACCEPTED),
            _lesson(
                id="les_prov",
                claim=PROVISIONAL_CLAIM,
                status=LessonStatus.PROVISIONAL,
            ),
            _lesson(
                id="les_ret",
                claim=RETRACTED_CLAIM,
                status=LessonStatus.RETRACTED,
            ),
        ]
        path = render_lessons("fixture-project", lessons, memories_root=memories_root)
        text = path.read_text(encoding="utf-8")

        assert LESSONS_BEGIN in text
        assert LESSONS_END in text
        assert ACCEPTED_CLAIM in text
        assert PROVISIONAL_CLAIM not in text
        assert RETRACTED_CLAIM not in text

        # Block is strictly between sentinels.
        start = text.index(LESSONS_BEGIN) + len(LESSONS_BEGIN)
        end = text.index(LESSONS_END)
        block = text[start:end]
        assert ACCEPTED_CLAIM in block
        assert PROVISIONAL_CLAIM not in block

    def test_handwritten_content_outside_sentinels_is_preserved(
        self, memories_root: Path
    ) -> None:
        project = "handwrite"
        path = memories_root / project / "LESSONS.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "# Lessons\n\n"
            "HAND_WRITTEN_PREAMBLE_KEEP_ME\n\n"
            f"{LESSONS_BEGIN}\n"
            "- stale auto bullet\n"
            f"{LESSONS_END}\n\n"
            "HAND_WRITTEN_FOOTER_KEEP_ME\n",
            encoding="utf-8",
        )
        render_lessons(
            project,
            [_lesson(id="les_new", claim=ACCEPTED_CLAIM, status=LessonStatus.ACCEPTED)],
            memories_root=memories_root,
        )
        text = path.read_text(encoding="utf-8")
        assert "HAND_WRITTEN_PREAMBLE_KEEP_ME" in text
        assert "HAND_WRITTEN_FOOTER_KEEP_ME" in text
        assert "stale auto bullet" not in text
        assert ACCEPTED_CLAIM in text

    def test_empty_accepted_set_clears_generated_block(
        self, memories_root: Path
    ) -> None:
        project = "clear"
        render_lessons(
            project,
            [_lesson(id="les_ok", claim=ACCEPTED_CLAIM, status=LessonStatus.ACCEPTED)],
            memories_root=memories_root,
        )
        path = render_lessons(
            project,
            [
                _lesson(
                    id="les_prov",
                    claim=PROVISIONAL_CLAIM,
                    status=LessonStatus.PROVISIONAL,
                )
            ],
            memories_root=memories_root,
        )
        text = path.read_text(encoding="utf-8")
        assert ACCEPTED_CLAIM not in text
        assert PROVISIONAL_CLAIM not in text


# ---------------------------------------------------------------------------
# Decisive / counterfeit / revert — recall path
# ---------------------------------------------------------------------------


class TestRecallSurfacesAcceptedOnly:
    def test_decisive_accepted_lesson_returned_by_recall(
        self, memories_root: Path, db_path: Path
    ) -> None:
        """Decisive: graduated fixture is returned by recall() end-to-end."""
        assert ACCEPTED_CLAIM, "fixture lesson claim must be non-empty"
        assert ACCEPTED_CLAIM.strip(), "fixture lesson claim must be non-empty"
        _set_lesson_status(db_path, "les_ok", "accepted")

        render_lessons(
            "recall-proj",
            [
                _lesson(
                    id="les_ok",
                    claim=ACCEPTED_CLAIM,
                    status=LessonStatus.ACCEPTED,
                    conditions="when running parallel sandboxed clones",
                ),
                _lesson(
                    id="les_prov",
                    claim=PROVISIONAL_CLAIM,
                    status=LessonStatus.PROVISIONAL,
                ),
            ],
            memories_root=memories_root,
        )

        # Production recall front-door for knowledge+memlife (recall_bridge).
        # Knowledge/PG may be off; rendered lessons must still surface.
        from omniagentos.memory.recall_bridge import default_knowledge_recaller

        query = "sandbox clones git worktree parallel lanes"
        hits = default_knowledge_recaller(query, top_k=8)

        # Assert non-empty FIRST — never use ``"" in text`` style checks.
        joined = "\n".join(hits)
        assert ACCEPTED_CLAIM, "lesson text under test must be non-empty"
        assert any(
            ACCEPTED_CLAIM in hit for hit in hits
        ), f"accepted lesson missing from recall hits: {hits!r}"
        assert joined.strip(), "recall returned only empty strings"

    def test_counterfeit_provisional_must_not_surface(
        self, memories_root: Path
    ) -> None:
        """Counterfeit: provisional claim must not appear in recall results."""
        assert PROVISIONAL_CLAIM.strip(), "counterfeit claim must be non-empty"

        render_lessons(
            "counterfeit-proj",
            [
                _lesson(
                    id="les_ok",
                    claim=ACCEPTED_CLAIM,
                    status=LessonStatus.ACCEPTED,
                ),
                _lesson(
                    id="les_prov",
                    claim=PROVISIONAL_CLAIM,
                    status=LessonStatus.PROVISIONAL,
                ),
            ],
            memories_root=memories_root,
        )

        from omniagentos.memory.recall_bridge import default_knowledge_recaller

        # Query is tuned to the provisional claim tokens so a missing filter
        # would surface it preferentially.
        query = "zebra-token-quokka routing unreviewed"
        hits = default_knowledge_recaller(query, top_k=8)
        joined = "\n".join(hits)
        assert PROVISIONAL_CLAIM not in joined
        assert not any(PROVISIONAL_CLAIM in hit for hit in hits)

    def test_signed_provisional_counterfeit_is_rejected_at_read_time(
        self, memories_root: Path, db_path: Path
    ) -> None:
        """A signed file cannot override the authoritative DB status."""
        assert PROVISIONAL_CLAIM.strip(), "counterfeit claim must be non-empty"

        project = "revert-proj"
        path = memories_root / project / "LESSONS.md"
        path.parent.mkdir(parents=True)
        _set_lesson_status(db_path, "les_ok", "accepted")
        _set_lesson_status(db_path, "les_prov", "provisional")
        unsigned = (
            f"- {ACCEPTED_CLAIM}  <!-- id=les_ok status=accepted -->\n"
            f"- {PROVISIONAL_CLAIM}  <!-- id=les_prov status=provisional -->"
        )
        path.write_text(
            f"# Lessons\n\n{LESSONS_BEGIN}\n"
            f"{_sign_block(unsigned, memories_root=memories_root)}\n{LESSONS_END}\n",
            encoding="utf-8",
        )

        hits = search_rendered_lessons(
            "zebra-token-quokka routing",
            top_k=8,
            memories_root=memories_root,
        )
        assert not any(PROVISIONAL_CLAIM in hit for hit in hits)

        # Production render also excludes it before it ever reaches the file.
        render_lessons(
            project,
            [
                _lesson(
                    id="les_ok", claim=ACCEPTED_CLAIM, status=LessonStatus.ACCEPTED
                ),
                _lesson(
                    id="les_prov",
                    claim=PROVISIONAL_CLAIM,
                    status=LessonStatus.PROVISIONAL,
                ),
            ],
            memories_root=memories_root,
        )
        disk = path.read_text(encoding="utf-8")
        assert PROVISIONAL_CLAIM not in disk

        fixed_hits = search_rendered_lessons(
            "zebra-token-quokka routing", top_k=8, memories_root=memories_root
        )
        assert not any(PROVISIONAL_CLAIM in hit for hit in fixed_hits)


class TestSearchSkipsMissingRoot:
    def test_missing_memories_root_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "no-such-memories"
        monkeypatch.setenv("OMNIAGENTOS_MEMORIES_DIR", str(missing))
        assert search_rendered_lessons("anything", top_k=5) == []

    def test_empty_query_returns_empty(self, memories_root: Path) -> None:
        render_lessons(
            "p",
            [_lesson(id="les_ok", claim=ACCEPTED_CLAIM, status=LessonStatus.ACCEPTED)],
            memories_root=memories_root,
        )
        assert search_rendered_lessons("", top_k=5) == []
        assert search_rendered_lessons("   ", top_k=5) == []
        assert search_rendered_lessons("worktree", top_k=0) == []
