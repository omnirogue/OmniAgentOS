"""Classify terminal stream-json outcomes for longhaul attempts."""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NamedTuple, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omniagentos.sessions.supervisor import _auth_failure_detail, _is_auth_failure


class Classification(TypedDict):
    kind: Literal["completed", "usage_limited", "auth_failed", "crashed", "unfinished_exit"]
    reset_at: str | None
    detail: str


_LIMIT_PHRASES = (
    "usage limit",
    "you've reached your limit",
    "you have reached your limit",
    "5-hour limit",
    "weekly limit",
    "limit resets",
    "out of extra usage",
    "upgrade to continue",
)

# ---------------------------------------------------------------------------
# Per-provider output-pattern tables (WP2 / longhaul unification #1).
#
# CLIs swallow rate-limit headers, so the ONLY classification signal is error
# text. Patterns are deliberately conservative -- documented API error types
# and exact provider phrasings, not loose keywords -- because a false limit
# classification cools a healthy account. Unknown providers fall back to the
# generic table. All matching is lowercase-substring.
#
# Four classes (mirrors omniagentos.routing.limit_state's outcome table):
#   auth   -> auth_error            (stop the line: disable + notify, no retry)
#   quota  -> quota_exhausted       (cool until window reset)
#   rate   -> transient_rate_limit  (short jittered cooldown)
#   over   -> overloaded            (30-120s backoff, NOT a status change)
#
# BARE NUMERIC status codes ("401"/"429"/"503") are matched context-safely via
# _BARE_NUMERIC_RE, never as raw substrings: a node stack-trace frame like
# "geminiChat.js:429:12" or a file:///...:401:5 line number must NEVER cool or
# disable a healthy account (live gemini crash streams are full of such
# frames). Word patterns stay plain lowercase-substring.
# ---------------------------------------------------------------------------

_GENERIC_TEXT_PATTERNS: dict[str, tuple[str, ...]] = {
    "auth": (
        "authentication failed",
        "authentication_failed",
        "invalid api key",
        "unauthorized",
        "401",
    ),
    "quota": ("quota exceeded", "usage limit", "out of extra usage"),
    "rate": ("rate limit", "rate_limit", "too many requests", "429"),
    "over": ("overloaded",),
}

_PROVIDER_TEXT_PATTERNS: dict[str, dict[str, tuple[str, ...]]] = {
    # Gemini CLI / Google AI: gRPC-style status names + documented messages.
    "gemini": {
        "auth": (
            "api key not valid",
            "api_key_invalid",
            "unauthenticated",
            "invalid_grant",
            "permission_denied",
            "permission denied",
            "401",
        ),
        "quota": ("daily limit", "daily quota", "quota exceeded", "quota will reset"),
        "rate": (
            "resource_exhausted",
            "resource has been exhausted",
            "429",
            "rate limit",
            "too many requests",
            "ratelimitexceeded",
        ),
        "over": ("model is overloaded", "service unavailable", "currently unavailable", "503"),
    },
    # Kimi (Moonshot AI): documented API error TYPES land verbatim in output.
    "kimi": {
        "auth": (
            "invalid_authentication_error",
            "invalid authentication",
            "incorrect api key",
            "invalid api key",
            "auth_failed",
            "permission_denied_error",
            "401",
        ),
        "quota": ("exceeded_current_quota_error", "insufficient balance", "quota exceeded"),
        "rate": ("rate_limit_reached_error", "rate limit", "too many requests", "429"),
        "over": ("engine_overloaded_error", "engine is currently overloaded", "server is busy"),
    },
    # Grok (xAI): less-documented CLI surface -> generic shapes + credit phrasing.
    "grok": {
        "auth": (
            "invalid api key",
            "incorrect api key",
            "unauthorized",
            "invalid bearer token",
            "authentication failed",
            "401",
        ),
        "quota": (
            "out of credits",
            "insufficient credits",
            "no credits",
            "quota exceeded",
            "spending limit",
        ),
        "rate": ("rate limit", "rate_limit", "too many requests", "429"),
        "over": ("overloaded", "service unavailable", "503"),
    },
}

# ---------------------------------------------------------------------------
# ABSENT-credential phrasings (R0-2 item 5, 2026-08-08).
#
# The provider tables above only recognise a REJECTED credential -- "invalid api
# key", "permission_denied", a bare 401. A credential that is MISSING, unset, or
# commented out (the live shape: configs/loop_models.yaml pins the Moonshot
# kimi-k3 alias whose provider key is commented out for the 2026-08-05 billing
# pause) matched nothing, so this function returned ``None``, the caller never
# parked and never alerted, and the loop re-attempted the same dead credential
# every night. ``None`` is never favourable, and a terminal condition that
# retries forever is the same defect class as a favourable absence -- so an
# absent credential now classifies ``auth_error``, parks, and alerts ONCE.
#
# These are checked for EVERY provider (unioned onto whichever "auth" tuple
# applies) because absence is a property of this machine's configuration, not
# of any vendor's error vocabulary. They are deliberately full phrases, never
# the bare token "api key": healthy chatter mentions keys constantly ("loaded
# api key from keychain", "key rotation completed"), and a false auth
# classification disables a HEALTHY account.
# ---------------------------------------------------------------------------
_ABSENT_CREDENTIAL_PATTERNS: tuple[str, ...] = (
    "api key not configured",
    "api key is not configured",
    "no api key configured",
    "api key not set",
    "api key is not set",
    "api key is missing",
    "missing api key",
    "api_key is not set",
    "api_key not set",
    "api_key environment variable is not set",
    "credential not found",
    "credentials not found",
    "missing credential",
)

# Markers that the credential being reported belongs to SOMETHING ELSE -- an
# MCP server, a tool integration, a telemetry exporter -- not the model
# provider. Parking a healthy provider account because an optional exporter
# has no key is a false positive that stops the line.
#
# Deliberately NOT widened here: "upstream"/"downstream" most often denote OUR
# model provider ("upstream provider api key"), so admitting them as
# third-party markers would hide real provider failures. This table is the
# reviewed round-2 vocabulary verbatim; this redesign changes SCOPING only.
_THIRD_PARTY_CREDENTIAL_MARKERS: tuple[str, ...] = (
    "mcp server",
    "mcp tool",
    "telemetry",
    "exporter",
    "integration",
    "plugin",
    "webhook",
)

# Modifiers that attribute a credential phrase to OUR model provider. One entry
# is enough because it is matched as a substring inside the absence phrase's
# noun-phrase window, so "remote provider api key", "model provider api key"
# and "the provider's api key" all resolve to us. Keep it minimal: every
# addition here turns a suppressed third-party phrase into a park, which is the
# expensive error direction.
_OUR_PROVIDER_ATTRIBUTORS: tuple[str, ...] = ("provider",)

# Phrases that make an absent-credential match a FALSE POSITIVE. Both shapes are
# live CLI output, and both would otherwise disable a HEALTHY account:
#
#   * NEGATION -- "No API key is required; this CLI uses OAuth" is a statement
#     that the credential is UNNECESSARY, the exact opposite of missing.
#   * THIRD-PARTY AUTH -- "MCP server github is not authenticated" is a tool
#     integration's problem, not the model provider's credential. (This is why
#     the bare phrases "not authenticated", "no credentials" and "no api key"
#     are NOT in the table above: they cannot distinguish the two, and a false
#     auth classification costs a working account.)
_CREDENTIAL_NEGATION_PATTERNS: tuple[str, ...] = (
    "no api key is required",
    "no api key required",
    "api key is not required",
    "api key not required",
    "no api key is needed",
    "api key is not needed",
    "does not require an api key",
    "no credentials required",
    "no credentials are required",
)

# Context-safe matching for bare HTTP status codes: the digits must not be
# embedded in a larger number, a stack-frame line:column position, or a
# version-like dotted run ("429" in ":429:12", "1429", "0.429" never match).
# The trailing guard only rejects digit-continuation and a separator FOLLOWED
# BY a digit -- a bare ':' or '.' followed by non-digit text is real error
# phrasing, so "error 429: x", "HTTP 429.", and "HTTP Error 401: Unauthorized"
# all still classify (a plain (?![:.]) guard wrongly rejected those).
_BARE_NUMERIC_RE: dict[str, re.Pattern[str]] = {
    code: re.compile(rf"(?<![\d:.]){code}(?!\d)(?![:.]\d)") for code in ("401", "429", "503")
}


# ---------------------------------------------------------------------------
# CREDENTIAL-ABSENCE SCOPING -- span + proximity, never a clause split.
#
# The question is not "which punctuation-delimited fragment does the phrase sit
# in", it is "WHOSE key is being reported absent". Four consecutive rounds of
# clause splitting (semicolon -> comma+and -> URL query '?' -> schemeless
# www.x.com dots -> and/or over one shared subject) each fixed one input shape
# and broke the next, because punctuation does not carry that fact: a URL is
# full of punctuation that is not a boundary, and a conjunction may join two
# independent reports OR two predicates about a single subject. So NO clause
# split happens here at all -- there is no str.split on '.', ';', ',', '?',
# 'and' or 'or' anywhere in this path. Instead:
#
#   1. MASK URLS FIRST, to one opaque word token, so no locator's internal
#      dots/slashes/queries and no "and" coordinating two locators can ever be
#      read as structure.
#   2. LOCATE SPANS: every credential-absence phrase, every third-party marker,
#      every credential-negation phrase and every our-provider attributor is
#      found by regex over the masked text and mapped onto word-token indices,
#      so "how far apart" is measured in WORDS rather than in fragments.
#   3. ATTRIBUTE each absence span by what modifies its own head noun (the
#      nearest attributor inside a short noun-phrase window). "remote provider
#      api key is not configured" names US however much third-party chatter
#      shares the sentence; "mcp server github failed: api key not configured"
#      names nobody, so the topic that introduced it governs.
#   4. GOVERN an unattributed absence by the nearest third-party marker in the
#      same sentence within a bounded token window before it, or attached after
#      it by a preposition ("... is not configured for the telemetry exporter").
#      Never "a marker appears somewhere in the message" -- that was the round-3
#      over-correction that hid real provider failures.
#   5. RETRACT an absence when a LATER predicate about the SAME head noun in the
#      SAME sentence says the key is not required. Same-subject, not same-clause,
#      so it is independent of whether an "and" sits between the two predicates.
#
# ERROR DIRECTION (deliberate, pinned by tests in BOTH directions): a MISSED
# park costs one nightly retry of a dead credential; a FALSE park stops a
# working account. Ambiguity therefore resolves toward NOT parking -- but only
# ambiguity. An absence phrase that names our provider, or names nobody else at
# all, is OURS and parks.
# ---------------------------------------------------------------------------

# One opaque, punctuation-free token: it tokenizes as a single word and matches
# no marker, absence, negation or attributor pattern.
_URL_PLACEHOLDER = "__url__"

_URL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # scheme://host/path?query -- http(s), file, ws, anything schemed.
    re.compile(r"[a-z][a-z0-9+.\-]*://[^\s<>\"'()\[\]{}]+"),
    # schemeless www.host/path?query (the F4 shape).
    re.compile(r"www\.[^\s<>\"'()\[\]{}]+"),
    # host.tld/path?query -- a path makes it unambiguously a locator.
    re.compile(r"[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+/[^\s<>\"'()\[\]{}]*"),
    # bare host.tld for common public/test suffixes, so "... at example.com:"
    # cannot contribute dots either.
    re.compile(
        r"\b[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)*"
        r"\.(?:com|org|net|io|dev|ai|co|app|sh|cloud|run|test|example|local|localhost|internal)\b"
    ),
)

# Trailing sentence punctuation belongs to the SENTENCE, not to the locator:
# swallowing it into the mask would erase a real boundary and let a third-party
# topic govern across sentences.
_URL_TRAILING_PUNCTUATION = ".,;:!?"

_WORD_RE = re.compile(r"[a-z0-9_]+")

# A sentence boundary is a newline, or terminal punctuation FOLLOWED BY
# whitespace/end -- never a bare '.' or '?' inside a token. With URLs already
# masked, this leaves "geminichat.js:429:12" and "v1.2.3" intact and treats
# ':' and ',' as the non-boundaries they are ("mcp callback failure: api key
# is not configured" is one statement about one subject).
_SENTENCE_END_RE = re.compile(r"[\n\r]|[.;!?](?=[\s\"'`)\]]|$)")


def _phrase_alternation(patterns: tuple[str, ...]) -> re.Pattern[str]:
    """Compile a phrase table into one longest-match-first alternation.

    No word boundaries: the tables are matched with the same substring
    semantics the reviewed round-2 code used, so recognition breadth is
    unchanged (``OPENAI_API_KEY environment variable is not set`` still
    matches -- a leading ``\\b`` would break it, since ``_`` is a word char).
    """
    ordered = sorted(patterns, key=len, reverse=True)
    return re.compile("|".join(re.escape(pattern) for pattern in ordered))


_ABSENCE_RE = _phrase_alternation(_ABSENT_CREDENTIAL_PATTERNS)
_NEGATION_RE = _phrase_alternation(_CREDENTIAL_NEGATION_PATTERNS)
_THIRD_PARTY_RE = _phrase_alternation(_THIRD_PARTY_CREDENTIAL_MARKERS)
_OUR_PROVIDER_RE = _phrase_alternation(_OUR_PROVIDER_ATTRIBUTORS)

# Head nouns, canonicalised so "api_key"/"api-key"/"api key" are one subject
# and "credential"/"credentials" are one subject. ``credential`` is the
# HYPERNYM: "credentials not found because no api key is required for local
# mode" is one subject described two ways, and refusing to co-refer them would
# park a healthy account -- the expensive error direction. The distinction is
# kept rather than collapsed so that a future absence phrase about a DIFFERENT
# subject (an oauth session, a seat token) is not silently retracted by an
# api-key disclaimer that has nothing to do with it.
_HEAD_NOUN_RES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"api[ _\-]?key"), "api key"),
    (re.compile(r"credentials?"), "credential"),
)
_HEAD_NOUN_HYPERNYM = "credential"

# Tuning, in WORD TOKENS (never characters, never fragments):
#   ATTRIBUTION -- modifiers of the absence phrase's own head noun.
#   GOVERNING   -- how far back a third-party topic reaches inside a sentence.
#   RETRACTION  -- how far a later same-subject disclaimer reaches.
_ATTRIBUTION_WINDOW = 3
_GOVERNING_WINDOW = 8
_RETRACTION_WINDOW = 12

# Function words that can stand between a credential phrase and a third-party
# noun it is ATTACHED to ("api key is not configured for the telemetry
# exporter"). A conjunction is deliberately absent: "api key is not configured
# and the mcp server is unreachable" is two reports, not one attachment.
_ATTACHMENT_WORDS = frozenset(
    {
        "a",
        "an",
        "at",
        "by",
        "external",
        "for",
        "from",
        "in",
        "its",
        "local",
        "of",
        "on",
        "optional",
        "our",
        "remote",
        "the",
        "their",
        "this",
        "to",
        "with",
        "your",
    }
)


class _Span(NamedTuple):
    """A located phrase: character bounds plus the word tokens it covers."""

    start: int
    end: int
    first_token: int
    last_token: int
    text: str


class _Scoped(NamedTuple):
    """One text, tokenized and indexed once.

    The index lists exist so every lookup below is a bisect for the NEAREST
    span rather than a scan of all of them: error blobs can be hundreds of
    kilobytes of model output, and this runs on every failed attempt.
    """

    tokens: list[tuple[str, int, int]]
    breaks: list[int]
    markers: list[_Span]
    marker_last_tokens: list[int]
    marker_first_tokens: list[int]
    attributors: list[_Span]
    attributor_last_tokens: list[int]
    negations: list[_Span]
    negation_starts: list[int]


def _mask_urls(lowered: str) -> str:
    """Reduce every URL-ish run to one opaque token, keeping trailing punctuation."""

    def replace(match: re.Match[str]) -> str:
        matched = match.group(0)
        trimmed = matched.rstrip(_URL_TRAILING_PUNCTUATION)
        return _URL_PLACEHOLDER + matched[len(trimmed) :]

    masked = lowered
    for pattern in _URL_PATTERNS:
        masked = pattern.sub(replace, masked)
    return masked


def _find_spans(
    masked: str, pattern: re.Pattern[str], tokens: list[tuple[str, int, int]], starts: list[int]
) -> list[_Span]:
    """Locate every match of *pattern*, resolved to the word tokens it covers."""
    spans: list[_Span] = []
    for match in pattern.finditer(masked):
        first = bisect_right(starts, match.start()) - 1
        if first < 0 or tokens[first][2] <= match.start():
            first += 1
        last = bisect_right(starts, max(match.end() - 1, match.start())) - 1
        if first >= len(tokens) or last < first:
            continue  # a match containing no word token cannot be scoped
        spans.append(_Span(match.start(), match.end(), first, last, match.group(0)))
    return spans


def _head_noun(phrase: str) -> str | None:
    for pattern, canonical in _HEAD_NOUN_RES:
        if pattern.search(phrase):
            return canonical
    return None


def _co_refer(left: str | None, right: str | None) -> bool:
    """True when two head nouns denote the same credential."""
    if left is None or right is None:
        return False
    return left == right or _HEAD_NOUN_HYPERNYM in (left, right)


def _crosses_sentence_end(breaks: list[int], left: int, right: int) -> bool:
    """True when a sentence boundary lies between character offsets *left*/*right*."""
    if right <= left:
        return False
    index = bisect_left(breaks, left)
    return index < len(breaks) and breaks[index] < right


def _nearest_before(spans: list[_Span], last_tokens: list[int], token: int) -> _Span | None:
    """The last span that ENDS before word *token*, or None."""
    index = bisect_left(last_tokens, token) - 1
    return spans[index] if index >= 0 else None


def _nearest_after(spans: list[_Span], first_tokens: list[int], token: int) -> _Span | None:
    """The first span that STARTS after word *token*, or None."""
    index = bisect_right(first_tokens, token)
    return spans[index] if index < len(spans) else None


def _is_retracted(absence: _Span, scoped: _Scoped) -> bool:
    """True when a LATER same-subject predicate disclaims this absence.

    Same-subject, not same-clause: "for local mode, the api key is not
    configured and the api key is not required" retracts across an "and", while
    "api key is not configured; no api key is required for local mode" does
    NOT -- the sentence boundary makes the absence an unconditional report that
    a subsequent, separate remark cannot take back.

    Directional on purpose. A disclaimer that PRECEDES an unconditional absence
    report does not cancel it ("no api key is required for the telemetry
    exporter, but the remote provider api key is not configured" is a real
    provider failure); letting earlier unrelated text suppress a later failure
    is precisely the defect class that degraded genuine auth errors to
    retry-forever.

    Subject identity is ``_co_refer``, not string equality: "credentials not
    found because no api key is required for local mode" names one subject two
    ways, and demanding the same wording there parks a healthy account -- the
    expensive direction.
    """
    head = _head_noun(absence.text)
    if head is None:
        return False
    index = bisect_left(scoped.negation_starts, absence.end)
    for negation in scoped.negations[index:]:
        if negation.first_token - absence.last_token - 1 > _RETRACTION_WINDOW:
            break  # negations are ordered, so every later one is further still
        if _crosses_sentence_end(scoped.breaks, absence.end, negation.start):
            break
        if _co_refer(_head_noun(negation.text), head):
            return True
    return False


def _local_attribution(absence: _Span, scoped: _Scoped) -> str | None:
    """Whose key is it, judged by the modifiers of the absence's own head noun.

    Only the NEAREST preceding span can be a modifier of the head noun, and if
    it is out of the noun-phrase window (or on the other side of a sentence
    boundary) every earlier one is too. A third-party marker inside the noun
    phrase outranks an our-provider one ("telemetry provider api key" is the
    exporter's), because not parking is the safe direction when the phrase
    itself is ambiguous.
    """
    floor = absence.first_token - _ATTRIBUTION_WINDOW

    def modifies(span: _Span | None) -> bool:
        return (
            span is not None
            and span.last_token >= floor
            and not _crosses_sentence_end(scoped.breaks, span.end, absence.start)
        )

    if modifies(_nearest_before(scoped.markers, scoped.marker_last_tokens, absence.first_token)):
        return "third_party"
    if modifies(
        _nearest_before(scoped.attributors, scoped.attributor_last_tokens, absence.first_token)
    ):
        return "ours"
    return None


def _attaches_after(absence: _Span, marker: _Span, scoped: _Scoped) -> bool:
    """True when *marker* is the object the absence phrase is attached to."""
    if _crosses_sentence_end(scoped.breaks, absence.end, marker.start):
        return False
    between = scoped.tokens[absence.last_token + 1 : marker.first_token]
    return all(token[0] in _ATTACHMENT_WORDS for token in between)


def _third_party_governs(absence: _Span, scoped: _Scoped) -> bool:
    """True when this absence is somebody else's credential, not our provider's."""
    attribution = _local_attribution(absence, scoped)
    if attribution is not None:
        return attribution == "third_party"
    # A topic introduced earlier in the SAME sentence, close enough to still be
    # the subject ("mcp server callback failure (<url>): api key is not
    # configured"). Bounded in tokens so a marker that merely shares a long
    # sentence -- a service reported RECOVERED before an unrelated provider
    # failure -- cannot silently suppress it. Only the nearest marker can
    # qualify: any earlier one is further away and behind at least as much
    # punctuation.
    before = _nearest_before(scoped.markers, scoped.marker_last_tokens, absence.first_token)
    if (
        before is not None
        and absence.first_token - before.last_token - 1 <= _GOVERNING_WINDOW
        and not _crosses_sentence_end(scoped.breaks, before.end, absence.start)
    ):
        return True
    # Attachment the other way ("... is not configured for the telemetry
    # exporter"). Again only the nearest can qualify: a marker beyond it is
    # separated from the phrase by that marker's own words, which are not
    # attachment words.
    after = _nearest_after(scoped.markers, scoped.marker_first_tokens, absence.last_token)
    return after is not None and _attaches_after(absence, after, scoped)


def _absent_credential_match(lowered: str) -> bool:
    """True when the text reports OUR provider's credential as absent.

    Returns False when every absence phrase in the text is either attributed to
    a third party or retracted by a later same-subject disclaimer. See the
    section comment above for the scoping model and the deliberate error
    direction.
    """
    if _ABSENCE_RE.search(lowered) is None:
        return False
    masked = _mask_urls(lowered)
    tokens = [(match.group(0), match.start(), match.end()) for match in _WORD_RE.finditer(masked)]
    if not tokens:
        return False
    starts = [token[1] for token in tokens]
    absences = _find_spans(masked, _ABSENCE_RE, tokens, starts)
    if not absences:
        return False
    markers = _find_spans(masked, _THIRD_PARTY_RE, tokens, starts)
    attributors = _find_spans(masked, _OUR_PROVIDER_RE, tokens, starts)
    negations = _find_spans(masked, _NEGATION_RE, tokens, starts)
    scoped = _Scoped(
        tokens=tokens,
        breaks=[match.start() for match in _SENTENCE_END_RE.finditer(masked)],
        markers=markers,
        marker_last_tokens=[span.last_token for span in markers],
        marker_first_tokens=[span.first_token for span in markers],
        attributors=attributors,
        attributor_last_tokens=[span.last_token for span in attributors],
        negations=negations,
        negation_starts=[span.start for span in negations],
    )
    for absence in absences:
        if _is_retracted(absence, scoped):
            continue
        if _third_party_governs(absence, scoped):
            continue
        return True
    return False


def _pattern_matches(candidate: str, lowered: str) -> bool:
    numeric = _BARE_NUMERIC_RE.get(candidate)
    if numeric is not None:
        return numeric.search(lowered) is not None
    return candidate in lowered


# Text class -> the four-class outcome names limit_state consumes.
_TEXT_CLASS_TO_OUTCOME = {
    "auth": "auth_error",
    "quota": "quota_exhausted",
    "rate": "transient_rate_limit",
    "over": "overloaded",
}


def classify_limit_text(provider: str, text: str) -> str | None:
    """Map raw CLI error text to one of the four limit classes, or ``None``.

    Returns ``'auth_error' | 'quota_exhausted' | 'transient_rate_limit' |
    'overloaded'`` -- the exact outcome names ``limit_state.report_outcome``
    takes. Precedence: auth > quota > rate > overloaded (auth is highest
    priority among failures; quota messages often also contain rate wording).

    STRUCTURED-FIRST (binding invariant, docs/architecture/longhaul.md):
    callers must apply this ONLY to output of a genuinely failed attempt --
    never to intermediate text a session recovered from. A clean completion
    always wins over any hint this function would find."""
    lowered = text.lower()
    patterns = _PROVIDER_TEXT_PATTERNS.get(provider, _GENERIC_TEXT_PATTERNS)
    for text_class in ("auth", "quota", "rate", "over"):
        candidates = patterns.get(text_class) or _GENERIC_TEXT_PATTERNS[text_class]
        if any(_pattern_matches(candidate, lowered) for candidate in candidates):
            return _TEXT_CLASS_TO_OUTCOME[text_class]
        if text_class == "auth" and _absent_credential_match(lowered):
            # Absence is a property of THIS machine's configuration, not of any
            # vendor's error vocabulary, so it is recognised for every provider.
            # It is checked AFTER the provider's own patterns so it can only
            # ever widen recognition -- a genuine 401 or "invalid api key" still
            # classifies first, whatever else the message says.
            return _TEXT_CLASS_TO_OUTCOME["auth"]
    return None


_TIME_RE = re.compile(
    r"\b(?:resets?\s+(?:at\s+)?)?(?P<hour>1[0-2]|0?[1-9])(?::(?P<minute>[0-5]\d))?\s*(?P<ampm>a\.??m\.??|p\.??m\.??)\b",
    re.IGNORECASE,
)
_ISO_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})\b"
)


def _as_utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_now(now: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def parse_reset_time(text: str, now: str) -> str | None:
    """Parse provider reset text into a UTC ISO timestamp, or ``None``."""

    for match in _ISO_RE.finditer(text):
        try:
            return _as_utc_iso(datetime.fromisoformat(match.group(0).replace("Z", "+00:00")))
        except ValueError:
            continue
    reference = _parse_now(now)
    time_match = _TIME_RE.search(text)
    if reference is None or time_match is None:
        return None
    parenthesized = re.search(r"\(([^()]+)\)", text)
    if parenthesized:
        zone_name = parenthesized.group(1).strip()
    else:
        zone_match = re.search(r"\b(ET|EST|EDT|UTC|GMT)\b", text, re.IGNORECASE)
        zone_name = zone_match.group(1).upper() if zone_match else "UTC"
    zones = {
        "ET": "America/New_York",
        "EST": "America/New_York",
        "EDT": "America/New_York",
        "UTC": "UTC",
        "GMT": "UTC",
    }
    try:
        zone = ZoneInfo(zones.get(zone_name, zone_name))
    except ZoneInfoNotFoundError:
        return None
    hour = int(time_match.group("hour"))
    minute = int(time_match.group("minute") or 0)
    ampm = re.sub(r"\.", "", time_match.group("ampm").lower())
    if ampm == "am" and hour == 12:
        hour = 0
    elif ampm == "pm" and hour != 12:
        hour += 12
    local_now = reference.astimezone(zone)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return _as_utc_iso(candidate)


def _result_is_error(event: dict[str, Any]) -> bool:
    if str(event.get("type") or "").lower() != "result":
        return False
    subtype = str(event.get("subtype") or "").lower()
    terminal_reason = str(event.get("terminal_reason") or "").lower()
    return bool(event.get("is_error")) or "error" in subtype or "error" in terminal_reason


def _result_is_success(event: dict[str, Any]) -> bool:
    """Check if a result event shows successful completion."""
    if str(event.get("type") or "").lower() != "result":
        return False
    return not bool(event.get("is_error"))


def _limit_detail(event: dict[str, Any]) -> str | None:
    structured = " ".join(
        str(event.get(key) or "") for key in ("subtype", "terminal_reason", "error")
    ).lower()
    if "rate_limit_error" in structured or "overloaded_error" in structured:
        return structured
    own_error_text = " ".join(str(event.get(key) or "") for key in ("error", "result")).lower()
    return own_error_text if any(phrase in own_error_text for phrase in _LIMIT_PHRASES) else None


def _provider_error_text(event: dict[str, Any]) -> str:
    return " ".join(
        str(event.get(key) or "") for key in ("error", "result", "message", "text", "detail")
    )


def classify_terminal(
    events: list[dict], return_code: int, cost_usd: float, *, provider: str = "claude"
) -> Classification:
    """Classify an attempt terminal state; cost never suppresses a true limit.

    MAJOR fix: api_retry signals require actual terminal failure (no successful recovery).
    If session has a successful result (is_error=False), ignore api_retry signals.

    ``provider`` selects the output-pattern table for non-claude CLIs
    (grok/gemini/kimi -- see ``classify_limit_text``); the default keeps the
    claude path byte-identical. The STRUCTURED-FIRST order is provider-
    independent: a clean completion beats every intermediate hint regardless
    of what the pattern tables would match. Non-claude 'overloaded' matches
    are folded into ``usage_limited`` here (longhaul's kinds are frozen); the
    class survives in the detail prefix, and four-class consumers use
    ``classify_limit_text`` directly.
    """

    del cost_usd
    terminal_errors = [event for event in events if _result_is_error(event)]
    successful_result = next((event for event in events if _result_is_success(event)), None)

    # Clean completion beats every intermediate hint: a transient 401 (token
    # refresh) or 429 the CLI recovered from must never cool or disable an
    # account whose attempt finished successfully (opus re-verify MAJOR).
    if successful_result is not None and not terminal_errors and return_code == 0:
        return {"kind": "completed", "reset_at": None, "detail": "process exited successfully"}

    # Check for auth failure first (highest priority among failures)
    for event in reversed(events):
        if _is_auth_failure(event):
            detail = _auth_failure_detail(event) or "authentication failure"
            return {"kind": "auth_failed", "reset_at": None, "detail": detail[:500]}

    # Non-claude providers: classify terminal error text against the
    # provider's pattern table (same position in the order as the claude
    # limit-phrase scan below -- after auth, only on genuine terminal errors).
    if provider != "claude":
        for event in reversed(terminal_errors):
            text = _provider_error_text(event)
            limit_class = classify_limit_text(provider, text)
            if limit_class == "auth_error":
                return {"kind": "auth_failed", "reset_at": None, "detail": text[:500]}
            if limit_class is not None:
                reset_at = (
                    None
                    if limit_class == "overloaded"
                    else parse_reset_time(text, datetime.now(UTC).isoformat())
                )
                return {
                    "kind": "usage_limited",
                    "reset_at": reset_at,
                    "detail": f"{limit_class}: {text[:300]}",
                }

    # Check for limit phrases in terminal errors (primary signal)
    for event in reversed(terminal_errors):
        limit_text = _limit_detail(event)
        if limit_text is not None:
            return {
                "kind": "usage_limited",
                "reset_at": parse_reset_time(limit_text, datetime.now(UTC).isoformat()),
                "detail": f"terminal rate limit: {limit_text[:300]}",
            }

    # MAJOR: api_retry is gated on failure; successful recovery overrides api_retry signal
    # If there's a successful result (recovered-429-then-clean-completion), return completed
    if successful_result:
        return {"kind": "completed", "reset_at": None, "detail": "process exited successfully"}

    # No successful result: honor api_retry signals (attempt actually failed)
    for event in events:
        if (
            str(event.get("type") or "").lower() == "system"
            and str(event.get("subtype") or "").lower() == "api_retry"
        ):
            if event.get("error_status") == 429:
                text = " ".join(str(event.get(key) or "") for key in ("error", "result", "message"))
                return {
                    "kind": "usage_limited",
                    "reset_at": parse_reset_time(text, datetime.now(UTC).isoformat()),
                    "detail": f"system api_retry error_status 429: {text or 'rate limited'}",
                }

    # Successful completion (rc=0, no errors)
    if return_code == 0 and not events:
        return {"kind": "completed", "reset_at": None, "detail": "Session completed (no events)"}
    if return_code == 0:
        return {"kind": "completed", "reset_at": None, "detail": "process exited successfully"}

    # Crashed or killed
    if terminal_errors:
        last = terminal_errors[-1]
        return {
            "kind": "crashed",
            "reset_at": None,
            "detail": str(last.get("error") or last.get("result") or "terminal error")[:500],
        }
    return {
        "kind": "crashed",
        "reset_at": None,
        "detail": f"process exited with code {return_code}",
    }


__all__ = ["Classification", "classify_limit_text", "classify_terminal", "parse_reset_time"]
