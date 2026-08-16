"""New-fact classifier scaffold.

``classify_fact(existing, incoming)`` decides whether an incoming statement is
``new``, a ``duplicate``, an ``update`` (same subject, refined/changed value), or
a ``contradiction`` (same subject, opposed meaning). This is a deliberately
*rule-based* MVP — the wiring point for a real LLM/NLI judge is noted in the
package's remaining-work list. Its contract is a safety one: contradictions are
*flagged*, never silently resolved, and the caller (not this function) decides
whether to write, supersede, or escalate. When polarity is ambiguous we err
toward flagging a contradiction rather than silently accepting an update.

Polarity is modelled conservatively as the parity of explicit negations combined
with a small antonym lexicon, so a double negation (``not without support``) and
an antonym (``blocks`` vs ``supports``) are both recognized as opposed instead of
being mislabelled duplicates or updates.
"""

from __future__ import annotations

import re
import unicodedata

from omniagentos.vaultgraph.contracts import FactClass, FactVerdict

# Unicode-aware word tokenizer (letters/digits of any script; ``3.5`` stays whole).
_WORD_RE = re.compile(r"[^\W_]+(?:\.[^\W_]+)*", re.UNICODE)
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
# Standalone negation words. Contractions (``doesn't``/``isn't``) are counted via
# the ``n't`` pattern below, so the tokenizer dropping the apostrophe is harmless.
_NEG_WORDS = frozenset(
    "not no never none cannot without lacks lack fails fail failed nor neither".split()
)
_NT_RE = re.compile(r"n't", re.UNICODE)
_STOPWORDS = frozenset(
    "the a an and or of to in on for with is are be at by from as this "
    "that it its has have had was were will do does did been being also".split()
)

# Antonym pairs: two poles asserting opposite things about the same subject. A
# token can belong to several pairs (support/block, support/prevent, ...), so we
# derive two structures: a symmetric relation (for opposition detection) and a
# connected-component representative (so every related pole collapses to one
# subject key regardless of which pair it came from).
_ANTONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("support", "block"),
    ("support", "prevent"),
    ("allow", "block"),
    ("allow", "prevent"),
    ("enable", "disable"),
    ("increase", "decrease"),
    ("fast", "slow"),
    ("faster", "slower"),
    ("high", "low"),
    ("more", "less"),
    ("add", "remove"),
    ("include", "exclude"),
    ("available", "unavailable"),
    ("present", "absent"),
    ("accept", "reject"),
    ("improve", "worsen"),
    ("rise", "fall"),
    ("gain", "loss"),
    ("true", "false"),
    ("open", "closed"),
)
_ANTONYMS: dict[str, set[str]] = {}
for _a, _b in _ANTONYM_PAIRS:
    _ANTONYMS.setdefault(_a, set()).add(_b)
    _ANTONYMS.setdefault(_b, set()).add(_a)


def _antonym_axis_map() -> dict[str, str]:
    """Map each antonym pole to a single connected-component representative, so
    e.g. support/block/prevent/allow all collapse to one shared subject key."""
    rep: dict[str, str] = {}
    for token in _ANTONYMS:
        if token in rep:
            continue
        stack, component = [token], []
        while stack:
            cur = stack.pop()
            if cur in rep:
                continue
            rep[cur] = ""  # visited marker
            component.append(cur)
            stack.extend(_ANTONYMS[cur] - set(rep))
        canonical = f"antonym:{min(component)}"
        for member in component:
            rep[member] = canonical
    return rep


_ANTONYM_AXIS: dict[str, str] = _antonym_axis_map()


def _casefold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _normalize(text: str) -> str:
    # Collapse whitespace and strip trailing sentence punctuation so a note that
    # differs only by a final ``.``/``!``/``?`` is still recognized as a duplicate.
    return " ".join(_casefold(text).split()).strip(" .!?;:,")


def _stem(token: str) -> str:
    """Crudely singularize a trailing plural ``s`` so ``supports``/``support`` and
    ``responses``/``response`` compare equal. Guarded to tokens longer than 3."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _content_tokens(text: str) -> set[str]:
    """Subject tokens: drop stopwords, negation markers (polarity is scored
    separately), and bare numbers, then light-stem the remainder."""
    return {
        _stem(tok)
        for tok in _WORD_RE.findall(_casefold(text))
        if tok not in _STOPWORDS and tok not in _NEG_WORDS and not _NUM_RE.fullmatch(tok)
    }


def _subject_tokens(content: set[str]) -> set[str]:
    """Collapse antonym poles to their shared axis so ``supports streaming`` and
    ``blocks streaming`` register as the *same subject* (their opposition is
    scored separately, not mistaken for a different topic)."""
    return {_ANTONYM_AXIS.get(tok, tok) for tok in content}


def _numbers(text: str) -> set[str]:
    return set(_NUM_RE.findall(text))


def _negation_count(text: str) -> int:
    cf = _casefold(text)
    count = len(_NT_RE.findall(cf))
    count += sum(1 for tok in _WORD_RE.findall(cf) if tok in _NEG_WORDS)
    return count


def _antonym_flip(content_e: set[str], content_i: set[str]) -> bool:
    """True when some token in one statement is an antonym of a token in the
    other (``supports`` vs ``blocks``) — an opposition on a shared subject axis."""
    return any(_ANTONYMS.get(tok, frozenset()) & content_i for tok in content_e)


def _jaccard(a: set[str], b: set[str]) -> float:
    # Empty ∩ empty is *not* perfect similarity: there is no subject signal to
    # measure. Returning 1.0 here used to score stopword-only / number-only
    # pairs as same-subject UPDATE with overlap 1.0 (non-result as favourable).
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


# Same-subject threshold: below this the incoming fact is about something else.
_SUBJECT_THRESHOLD = 0.4


def _require_nonempty(existing: str, incoming: str) -> None:
    if not existing.strip() or not incoming.strip():
        raise ValueError("classify_fact requires non-empty existing and incoming statements")
    # Normalization strips sentence punctuation; punctuation-only input collapses
    # to "" and must not be treated as a fact (or as a perfect duplicate of "").
    if not _normalize(existing) or not _normalize(incoming):
        raise ValueError("classify_fact requires non-empty existing and incoming statements")


def classify_fact(existing: str, incoming: str) -> FactVerdict:
    """Classify ``incoming`` against a single ``existing`` fact statement.

    Raises ``ValueError`` on empty/whitespace input — an empty statement is not a
    fact and must never be waved through as a duplicate or update."""
    _require_nonempty(existing, incoming)

    if _normalize(existing) == _normalize(incoming):
        return FactVerdict(FactClass.DUPLICATE, "identical after normalization", 1.0)

    content_e, content_i = _content_tokens(existing), _content_tokens(incoming)
    subj_e, subj_i = _subject_tokens(content_e), _subject_tokens(content_i)
    overlap = _jaccard(subj_e, subj_i)

    if overlap < _SUBJECT_THRESHOLD:
        return FactVerdict(
            FactClass.NEW, f"low subject overlap ({overlap:.2f}) — unrelated fact", overlap
        )

    # Opposed meaning = negation parity differs XOR an antonym pole is flipped.
    # (``not support`` vs ``blocks`` agree; ``not without support`` vs
    # ``not support`` are opposed.) Ambiguity here is flagged, never accepted.
    parity_opposed = (_negation_count(existing) % 2) != (_negation_count(incoming) % 2)
    if parity_opposed != _antonym_flip(content_e, content_i):
        return FactVerdict(
            FactClass.CONTRADICTION,
            f"same subject (overlap {overlap:.2f}) with opposed polarity",
            overlap,
        )

    # Same subject, changed numeric value -> a refresh, not a contradiction.
    nums_e, nums_i = _numbers(existing), _numbers(incoming)
    if nums_i and nums_e and nums_i != nums_e:
        return FactVerdict(
            FactClass.UPDATE,
            f"same subject (overlap {overlap:.2f}); numeric value changed "
            f"{sorted(nums_e)} -> {sorted(nums_i)}",
            overlap,
        )

    # Same subject, extra detail added or wording refined.
    return FactVerdict(
        FactClass.UPDATE,
        f"same subject (overlap {overlap:.2f}); refined or expanded wording",
        overlap,
    )


def classify_against_many(existing: list[str], incoming: str) -> FactVerdict:
    """Classify ``incoming`` against several existing facts, returning the most
    significant verdict. Precedence: CONTRADICTION > DUPLICATE > UPDATE > NEW.

    Every candidate is evaluated — no short-circuit — so a contradiction with any
    existing fact is surfaced even when the incoming statement also duplicates or
    updates another fact in the set (F2). Empty existing facts are skipped."""
    if not incoming.strip():
        raise ValueError("classify_against_many requires a non-empty incoming statement")
    if not existing:
        return FactVerdict(FactClass.NEW, "no existing facts to compare against", 0.0)

    precedence = {
        FactClass.CONTRADICTION: 3,
        FactClass.DUPLICATE: 2,
        FactClass.UPDATE: 1,
        FactClass.NEW: 0,
    }
    best = FactVerdict(FactClass.NEW, "no comparable existing fact", 0.0)
    for candidate in existing:
        if not candidate.strip():
            continue
        verdict = classify_fact(candidate, incoming)
        if precedence[verdict.label] > precedence[best.label]:
            best = verdict
    return best
