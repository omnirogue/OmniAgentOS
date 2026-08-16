"""Render graduated memlife lessons into project LESSONS.md files.

Derived from agentic-stack ``.agent/memory/render_lessons.py``; substantially
rewritten. Their render includes provisional and retracted rows with markers;
ours must never put anything but ``LessonStatus.ACCEPTED`` into the generated
block — a provisional or retracted lesson reaching a prompt is an unreviewed
claim presented as knowledge.

The generated block sits between stable HTML sentinels so hand-written content
above/below is preserved across re-renders. The file is rewritten atomically
(temp + replace) so readers never see a half-written LESSONS.md.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

from omniagentos.contracts import default_db_path
from omniagentos.memlife.contracts import Lesson, LessonStatus

# Stable sentinels — regenerate ONLY the interior; hand-written content stays.
LESSONS_BEGIN = "<!-- omniagentos:memlife:lessons:begin -->"
LESSONS_END = "<!-- omniagentos:memlife:lessons:end -->"

LESSONS_FILENAME = "LESSONS.md"
ENV_MEMORIES_DIR = "OMNIAGENTOS_MEMORIES_DIR"

_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_BULLET_RE = re.compile(
    r"^\s*-\s+(?P<claim>.+?)\s+(?P<annotation><!--\s*id=[^\s>]+\s+"
    r"status=[^\s>]+\s*-->)\s*$"
)
_ANNOTATION_RE = re.compile(
    r"^<!--\s*id=(?P<id>[^\s>]+)\s+status=(?P<status>[^\s>]+)\s*-->$"
)
_HMAC_LINE_RE = re.compile(r"^(?P<body>.*)\n<!-- hmac=(?P<mac>[0-9a-f]{64}) -->$", re.DOTALL)

_LOG = logging.getLogger(__name__)

# A lightweight audit surface for callers/tests that need to count rejected rendered
# records without ever retaining a signing key or a rendered claim.
RENDERED_CLAIM_DIAGNOSTICS: Counter[str] = Counter()

_DEFAULT_HEADER = (
    "# Lessons\n\n"
    "> Auto-managed block between the omniagentos:memlife:lessons sentinels. "
    "Hand-curated content outside those markers is preserved across renders.\n"
)


def default_memories_root() -> Path:
    """Resolve ``var/memories`` (or ``OMNIAGENTOS_MEMORIES_DIR`` override)."""
    override = os.environ.get(ENV_MEMORIES_DIR)
    if override:
        return Path(override)
    # omniagentos/memlife/render.py → repo root is parents[2]
    return Path(__file__).resolve().parents[2] / "var" / "memories"


def lessons_path(project: str, *, memories_root: Path | str | None = None) -> Path:
    """``var/memories/<project>/LESSONS.md``."""
    root = Path(memories_root) if memories_root is not None else default_memories_root()
    return root / project / LESSONS_FILENAME


def _key_path(*, memories_root: Path | str | None = None) -> Path:
    """Return the installation-local key path (``var/memlife.key`` by default)."""
    root = Path(memories_root) if memories_root is not None else default_memories_root()
    return root.parent / "memlife.key"


def _get_or_create_key(*, memories_root: Path | str | None = None) -> bytes:
    """Load the persistent HMAC key, creating it atomically on first render."""
    path = _key_path(memories_root=memories_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = path.read_bytes()
    else:
        key = os.urandom(32)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    if len(key) != 32:
        raise RuntimeError(f"memlife HMAC key at {path} has an invalid length")
    return key


def _load_key(*, memories_root: Path | str | None = None) -> bytes | None:
    """Read an existing signing key without creating key material during recall."""
    try:
        key = _key_path(memories_root=memories_root).read_bytes()
    except OSError:
        return None
    return key if len(key) == 32 else None


def _sign_block(block: str, *, memories_root: Path | str | None = None) -> str:
    """Append an HMAC comment to an unsigned generated block.

    The MAC covers every byte of ``block``. The signature comment itself is not
    covered, avoiding a circular signature while ensuring all lesson content is.
    """
    mac = hmac.new(
        _get_or_create_key(memories_root=memories_root),
        block.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{block}\n<!-- hmac={mac} -->"


def _format_block(lessons: Sequence[Lesson]) -> str:
    """Render accepted lessons as markdown bullets (no trailing newline)."""
    if not lessons:
        return "_No accepted lessons yet._"
    lines: list[str] = []
    for lesson in lessons:
        claim = " ".join(lesson.claim.split())
        ann = f"id={lesson.id} status={lesson.status.value}"
        lines.append(f"- {claim}  <!-- {ann} -->")
        conditions = (lesson.conditions or "").strip()
        if conditions:
            lines.append(f"  - when: {' '.join(conditions.split())}")
    return "\n".join(lines)


def _replace_sentinel_block(content: str, block: str) -> str:
    """Swap the interior of the lessons sentinels, or append them if missing."""
    begin_idx = content.find(LESSONS_BEGIN)
    end_idx = content.find(LESSONS_END)
    interior = block if block.endswith("\n") else block + "\n"
    if begin_idx != -1 and end_idx != -1 and end_idx > begin_idx:
        return (
            content[: begin_idx + len(LESSONS_BEGIN)]
            + "\n"
            + interior
            + content[end_idx:]
        )
    # Missing or broken markers: append a fresh block without destroying text.
    prefix = content.rstrip()
    if prefix:
        return f"{prefix}\n\n{LESSONS_BEGIN}\n{interior}{LESSONS_END}\n"
    return f"{_DEFAULT_HEADER}\n{LESSONS_BEGIN}\n{interior}{LESSONS_END}\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def render_lessons(
    project: str,
    lessons: Sequence[Lesson],
    *,
    memories_root: Path | str | None = None,
) -> Path:
    """Write ACCEPTED lessons into ``var/memories/<project>/LESSONS.md``.

    Only ``LessonStatus.ACCEPTED`` lessons are rendered. Provisional and
    retracted inputs are dropped — never annotated, never half-included.

    Returns the path written.
    """
    if not project or not str(project).strip():
        raise ValueError("project must be a non-empty path segment")

    accepted = [lesson for lesson in lessons if lesson.status == LessonStatus.ACCEPTED]
    block = _sign_block(_format_block(accepted), memories_root=memories_root)
    path = lessons_path(project, memories_root=memories_root)

    if path.is_file():
        existing = path.read_text(encoding="utf-8")
        new_content = _replace_sentinel_block(existing, block)
    else:
        new_content = f"{_DEFAULT_HEADER}\n{LESSONS_BEGIN}\n{block}\n{LESSONS_END}\n"

    _atomic_write(path, new_content)
    return path


def _diagnose(reason: str, message: str, *args: object) -> None:
    """Record and loudly audit every rejected rendered lesson/block."""
    RENDERED_CLAIM_DIAGNOSTICS[reason] += 1
    _LOG.warning(message, *args)


def _verified_unsigned_block(
    block: str,
    *,
    path: Path,
    memories_root: Path | str | None,
) -> str | None:
    """Return the MAC-verified unsigned block, or ``None`` with an audit record."""
    # Sentinel formatting is deliberate: rendering adds exactly one newline on
    # either side of the signed content. Deviations are tampering.
    if not block.startswith("\n") or not block.endswith("\n"):
        _diagnose("invalid_block_layout", "memlife invalid signed block layout in %s", path)
        return None
    match = _HMAC_LINE_RE.fullmatch(block[1:-1])
    if match is None:
        _diagnose("missing_or_invalid_hmac", "memlife HMAC missing or malformed in %s", path)
        return None
    key = _load_key(memories_root=memories_root)
    if key is None:
        _diagnose("missing_hmac_key", "memlife HMAC key unavailable while reading %s", path)
        return None
    unsigned = match.group("body")
    expected = hmac.new(key, unsigned.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, match.group("mac")):
        _diagnose("hmac_verification_failed", "memlife HMAC verification failed in %s", path)
        return None
    return unsigned


def _parse_annotation(line: str) -> tuple[str, str] | None:
    """Extract the lesson ID and status annotation from one primary bullet."""
    match = _BULLET_RE.match(line)
    if match is None:
        return None
    annotation = _ANNOTATION_RE.fullmatch(match.group("annotation"))
    if annotation is None:
        return None
    return annotation.group("id"), annotation.group("status")


def _lesson_status_in_db(lesson_id: str, *, db_path: Path | str | None = None) -> str | None:
    """Return a lesson's authoritative SQLite status without creating a database."""
    resolved = str(db_path) if db_path is not None else default_db_path()
    if resolved == ":memory:":
        return None
    database = Path(resolved).expanduser()
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT status FROM memlife_lessons WHERE id = ?",
                (lesson_id,),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        _diagnose(
            "database_verification_unavailable",
            "memlife database verification failed for lesson %s (%s): %s",
            lesson_id,
            resolved,
            exc,
        )
        return None
    return str(row[0]) if row is not None else None


def _sentinel_block(text: str, *, path: Path) -> str | None:
    """Extract the sole sentinel block, rejecting duplicate or prefixed markers."""
    begin_count = text.count(LESSONS_BEGIN)
    end_count = text.count(LESSONS_END)
    if begin_count != 1 or end_count != 1:
        _diagnose(
            "duplicate_or_forged_sentinels",
            "memlife duplicate or forged sentinels in %s (begin=%d, end=%d)",
            path,
            begin_count,
            end_count,
        )
        return None

    begin_idx = text.rfind(LESSONS_BEGIN)
    end_idx = text.rfind(LESSONS_END)
    if end_idx <= begin_idx:
        _diagnose("invalid_sentinel_order", "memlife invalid sentinel ordering in %s", path)
        return None
    begin_line = text[:begin_idx].rsplit("\n", 1)[-1] + LESSONS_BEGIN
    end_line = LESSONS_END + text[end_idx + len(LESSONS_END) :].split("\n", 1)[0]
    if begin_line.strip() != LESSONS_BEGIN or end_line.strip() != LESSONS_END:
        _diagnose("prefixed_sentinel", "memlife prefixed or suffixed sentinel in %s", path)
        return None
    return text[begin_idx + len(LESSONS_BEGIN) : end_idx]


def extract_rendered_claims(
    path: Path | str,
    *,
    memories_root: Path | str | None = None,
    db_path: Path | str | None = None,
) -> list[str]:
    """Return claims from the sentinel block of a LESSONS.md file.

    Missing file, missing sentinels, or unreadable content → ``[]``
    (absent/unparseable is empty, never a flattering default).
    """
    p = Path(path)
    if not p.is_file():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    block = _sentinel_block(text, path=p)
    if block is None:
        return []
    unsigned = _verified_unsigned_block(block, path=p, memories_root=memories_root)
    if unsigned is None:
        return []

    claims: list[str] = []
    for line in unsigned.splitlines():
        # Conditions are generated child bullets, not independent lessons.
        if line.startswith(("  ", "\t")) or not line.strip():
            continue
        if line == "_No accepted lessons yet._":
            continue
        match = _BULLET_RE.match(line)
        if match is None:
            _diagnose("malformed_lesson_line", "memlife malformed lesson line in %s", p)
            continue
        parsed = _parse_annotation(line)
        if parsed is None:
            _diagnose("malformed_lesson_annotation", "memlife malformed lesson annotation in %s", p)
            continue
        lesson_id, rendered_status = parsed
        if rendered_status != LessonStatus.ACCEPTED.value:
            _diagnose(
                "rendered_status_not_accepted",
                "memlife rendered lesson %s in %s has non-accepted status %r",
                lesson_id,
                p,
                rendered_status,
            )
            continue
        authoritative_status = _lesson_status_in_db(lesson_id, db_path=db_path)
        if authoritative_status is None:
            _diagnose("missing_lesson_db_row", "memlife lesson %s in %s has no DB row", lesson_id, p)
            continue
        if authoritative_status != LessonStatus.ACCEPTED.value:
            _diagnose(
                "lesson_status_mismatch",
                "memlife lesson %s in %s says accepted but DB says %r",
                lesson_id,
                p,
                authoritative_status,
            )
            continue
        claim = match.group("claim").strip()
        if claim:
            claims.append(claim)
        else:
            _diagnose("empty_lesson_claim", "memlife empty lesson claim in %s", p)
    return claims


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text)}


def _score(claim: str, query_words: set[str]) -> float:
    """Lexical overlap in [0, 1]. Not semantic relevance — a cheap first pass."""
    if not query_words:
        return 0.0
    claim_words = _words(claim)
    if not claim_words:
        return 0.0
    hits = len(query_words & claim_words)
    return hits / len(query_words) if query_words else 0.0


def iter_lessons_files(
    *,
    memories_root: Path | str | None = None,
    project: str | None = None,
) -> Iterable[Path]:
    """Yield LESSONS.md paths under the memories root (or one project)."""
    root = Path(memories_root) if memories_root is not None else default_memories_root()
    if not root.is_dir():
        return
    if project is not None:
        path = root / project / LESSONS_FILENAME
        if path.is_file():
            yield path
        return
    try:
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            path = child / LESSONS_FILENAME
            if path.is_file():
                yield path
    except OSError:
        return


def search_rendered_lessons(
    query: str,
    *,
    top_k: int = 8,
    memories_root: Path | str | None = None,
    project: str | None = None,
    min_score: float = 0.01,
) -> list[str]:
    """Return claim texts from rendered LESSONS.md files matching ``query``.

    Only content inside the generated sentinel block is considered. Because
    :func:`render_lessons` writes only ACCEPTED lessons into that block, a
    provisional claim cannot appear unless the status filter was dropped.
    """
    if not query.strip() or top_k <= 0:
        return []
    query_words = _words(query)
    if not query_words:
        return []

    scored: list[tuple[float, str]] = []
    seen: set[str] = set()
    for path in iter_lessons_files(memories_root=memories_root, project=project):
        for claim in extract_rendered_claims(path, memories_root=memories_root):
            key = claim.casefold()
            if key in seen:
                continue
            score = _score(claim, query_words)
            if score >= min_score:
                seen.add(key)
                scored.append((score, claim))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [claim for _score_v, claim in scored[:top_k]]


__all__ = [
    "ENV_MEMORIES_DIR",
    "LESSONS_BEGIN",
    "LESSONS_END",
    "LESSONS_FILENAME",
    "RENDERED_CLAIM_DIAGNOSTICS",
    "default_memories_root",
    "extract_rendered_claims",
    "iter_lessons_files",
    "lessons_path",
    "render_lessons",
    "search_rendered_lessons",
]
