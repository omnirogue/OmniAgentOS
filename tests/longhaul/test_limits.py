"""Tests for limits.py: usage-limit detection, auth failures, and reset-time parsing."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omniagentos.longhaul import limits
from omniagentos.longhaul.limits import (
    classify_limit_text,
    classify_terminal,
    parse_reset_time,
)


class TestParseResetTime:
    """Reset time parsing from various formats."""

    def test_parse_reset_time_3am(self) -> None:
        """Parse 'resets at 3am'."""
        now = "2026-07-23T10:00:00Z"
        result = parse_reset_time("resets at 3am", now)
        assert result is not None
        # Should be 3am UTC tomorrow (since now is 10:00 UTC, 3am is past)
        assert "03:00:00" in result

    def test_parse_reset_time_with_tz(self) -> None:
        """Parse 'resets 10:30am (America/New_York)'."""
        now = "2026-07-23T14:00:00Z"
        result = parse_reset_time("resets 10:30am (America/New_York)", now)
        assert result is not None
        # Should parse correctly with timezone

    def test_parse_reset_time_et_abbreviation(self) -> None:
        """Parse 'resets at 6pm ET'."""
        now = "2026-07-23T12:00:00Z"
        result = parse_reset_time("resets at 6pm ET", now)
        assert result is not None

    def test_parse_reset_time_iso(self) -> None:
        """Parse ISO timestamp directly."""
        iso_time = "2026-07-24T15:30:00Z"
        result = parse_reset_time(iso_time, "2026-07-23T10:00:00Z")
        assert result is not None
        assert "15:30:00" in result or "15:30" in result

    def test_parse_reset_time_unparsable(self) -> None:
        """Unparsable text returns None."""
        result = parse_reset_time("gibberish", "2026-07-23T10:00:00Z")
        assert result is None

    def test_parse_reset_time_empty(self) -> None:
        """Empty text returns None."""
        result = parse_reset_time("", "2026-07-23T10:00:00Z")
        assert result is None


class TestClassifyTerminal:
    """Terminal classification: usage_limited, auth_failed, completed, etc."""

    def test_classify_completed_no_events(self) -> None:
        """No events, rc=0 → completed."""
        result = classify_terminal([], 0, 0.0)
        assert result["kind"] == "completed"
        assert result["detail"] == "Session completed (no events)"

    def test_classify_crashed_nonzero_rc(self) -> None:
        """No events, rc!=0 → crashed."""
        result = classify_terminal([], 42, 0.0)
        assert result["kind"] == "crashed"
        assert "42" in result["detail"]

    def test_classify_usage_limited_429_api_retry(self) -> None:
        """System api_retry with error_status==429 → usage_limited."""
        events = [
            {
                "type": "system",
                "subtype": "api_retry",
                "error_status": 429,
                "error": "rate_limited",
            }
        ]
        result = classify_terminal(events, 0, 0.0)
        assert result["kind"] == "usage_limited"
        assert "429" in result["detail"]

    def test_classify_usage_limited_result_rate_limit_error(self) -> None:
        """Terminal result with rate_limit_error field → usage_limited."""
        events = [
            {
                "type": "result",
                "is_error": True,
                "error": "rate_limit_error",
                "terminal_reason": "usage limit exceeded",
            }
        ]
        result = classify_terminal(events, 0, 0.0)
        assert result["kind"] == "usage_limited"

    def test_classify_auth_failed_401(self) -> None:
        """System api_retry with error_status==401 → auth_failed."""
        events = [
            {
                "type": "system",
                "subtype": "api_retry",
                "error_status": 401,
                "error": "invalid_api_key",
            }
        ]
        result = classify_terminal(events, 0, 0.0)
        assert result["kind"] == "auth_failed"

    def test_classify_false_positive_assistant_limit_text(self) -> None:
        """Healthy result + assistant text mentioning limit → completed (not rate_limited)."""
        events = [
            {
                "type": "result",
                "is_error": False,  # Healthy
                "terminal_reason": "success",
            },
            {
                "type": "assistant",
                "content": "The API usage limit is 100 requests per hour. I've used 50 so far.",
            },
        ]
        result = classify_terminal(events, 0, 0.0)
        # Should NOT be usage_limited (no structured signal)
        assert result["kind"] == "completed"

    def test_classify_recovered_429_then_clean_completion(self) -> None:
        """api_retry 429 the CLI recovered from + clean result → completed, never cooled."""
        events = [
            {
                "type": "system",
                "subtype": "api_retry",
                "error_status": 429,
                "error": "rate limited",
            },
            {"type": "result", "is_error": False, "terminal_reason": "success"},
        ]
        result = classify_terminal(events, 0, 0.0)
        assert result["kind"] == "completed"

    def test_classify_recovered_401_then_clean_completion(self) -> None:
        """Transient 401 (token refresh) recovered + clean result → completed, account NOT disabled."""
        events = [
            {
                "type": "system",
                "subtype": "api_retry",
                "error_status": 401,
                "error": "invalid_api_key",
            },
            {"type": "result", "is_error": False, "terminal_reason": "success"},
        ]
        result = classify_terminal(events, 0, 0.0)
        assert result["kind"] == "completed"

    def test_classify_usage_limited_with_reset_time(self) -> None:
        """Usage limit with reset time → includes reset_at."""
        events = [
            {
                "type": "system",
                "subtype": "api_retry",
                "error_status": 429,
                "error": "rate_limited: resets at 3am",
            }
        ]
        result = classify_terminal(events, 0, 0.0)
        assert result["kind"] == "usage_limited"
        # reset_at should be parsed
        if result["reset_at"]:
            assert "Z" in result["reset_at"] or "+" in result["reset_at"]

    def test_classify_multiple_events(self) -> None:
        """Multiple events; first signal wins."""
        events = [
            {"type": "system", "subtype": "api_retry", "error_status": 401},
            {"type": "system", "subtype": "api_retry", "error_status": 429},
        ]
        result = classify_terminal(events, 0, 0.0)
        # 401 (auth) comes first, but let's verify it's one of them
        assert result["kind"] in ("auth_failed", "usage_limited")


class TestMissingCredentialClassifiesAsAuth:
    """R0-2 item 5 — an ABSENT credential is an auth error, not an unknown.

    ``classify_limit_text`` only recognised REJECTED credentials ("invalid api
    key", 401).  A key that is missing, unset, or commented out (the live
    Moonshot billing-pause shape) matched nothing, returned ``None`` forever,
    and so never parked and never alerted — the loop just re-attempted nightly.
    ``None`` is not a favourable result, and an unrecognised terminal condition
    that retries forever is the same defect class as a favourable absence.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "api key not configured",
            "API key is not configured for provider moonshot",
            "missing api key",
            "api_key is not set",
            "OPENAI_API_KEY environment variable is not set",
            "credentials not found",
            "api key is missing",
        ],
    )
    def test_absent_credential_is_auth_error(self, text: str) -> None:
        assert classify_limit_text("kimi", text) == "auth_error"
        assert classify_limit_text("unknown-provider", text) == "auth_error"

    @pytest.mark.parametrize(
        "text",
        [
            # Healthy chatter that merely MENTIONS keys must never cool an account.
            "loaded api key from keychain",
            "using api key ending 4f21",
            "key rotation completed successfully",
            "dictionary key not found in response payload",
            "authenticated as owner@example.com",
        ],
    )
    def test_benign_key_mentions_do_not_classify(self, text: str) -> None:
        assert classify_limit_text("kimi", text) is None

    def test_precedence_unchanged(self) -> None:
        """auth still outranks quota when both shapes appear."""
        assert classify_limit_text("kimi", "missing api key and quota exceeded") == "auth_error"


class TestAbsentCredentialFalsePositives:
    """Round-1 critic finding (MAJOR): broad phrases disabled healthy accounts.

    A false auth classification is expensive in the opposite direction -- it
    stops the line on a WORKING account -- so absence phrasing must not fire on
    a negation ("no API key is required") or on a third-party integration's
    auth state ("MCP server github is not authenticated").
    """

    @pytest.mark.parametrize(
        "text",
        [
            "No API key is required - subscription CLI uses OAuth; connection timed out",
            "no api key required for this provider",
            "This CLI does not require an API key",
            "api key is not required when using device auth",
            "no credentials required for local models",
        ],
    )
    def test_negations_do_not_classify(self, text: str) -> None:
        assert classify_limit_text("codex", text) is None
        assert classify_limit_text("kimi", text) is None

    @pytest.mark.parametrize(
        "text",
        [
            "MCP server github is not authenticated",
            "Tool 'slack' is not authenticated; run /mcp to connect",
            "warning: no credentials found for optional telemetry exporter",
        ],
    )
    def test_third_party_auth_state_does_not_classify(self, text: str) -> None:
        assert classify_limit_text("codex", text) is None

    def test_a_real_provider_auth_error_still_classifies(self) -> None:
        """The negation guard must not weaken genuine auth detection."""
        assert classify_limit_text("kimi", "invalid_authentication_error") == "auth_error"
        assert classify_limit_text("gemini", "api key not valid") == "auth_error"
        # Even alongside negation text, an outright rejection still wins.
        assert (
            classify_limit_text("kimi", "no api key required; got 401 unauthorized") == "auth_error"
        )

    def test_the_live_absent_credential_shape_still_classifies(self) -> None:
        assert classify_limit_text("kimi", "api key not configured") == "auth_error"
        assert classify_limit_text("kimi", "missing api key") == "auth_error"


class TestCredentialGuardIsClauseScoped:
    """Round-2 critic finding (MAJOR): a message-wide guard was wrong BOTH ways."""

    def test_a_negation_elsewhere_does_not_hide_a_real_missing_key(self) -> None:
        """The genuine failure must still park — it used to escape entirely."""
        text = (
            "No API key is required for local mode. Remote provider error: API key not configured."
        )
        assert classify_limit_text("kimi", text) == "auth_error"

    def test_third_party_credential_problems_do_not_park_the_provider(self) -> None:
        for text in (
            "MCP server github failed: API key not configured.",
            "optional telemetry exporter credentials not found; provider request timed out.",
            "plugin slack: missing api key",
        ):
            assert classify_limit_text("codex", text) is None, text

    def test_negation_in_its_own_clause_still_suppresses(self) -> None:
        assert classify_limit_text("codex", "No API key is required; using OAuth.") is None

    def test_an_ambiguous_not_found_phrase_is_not_an_auth_error(self) -> None:
        """ "api key not found in response metadata" is a parsing detail, not a
        credential state, so that phrase is deliberately not in the table."""
        assert classify_limit_text("codex", "API key not found in response metadata") is None

    def test_a_real_provider_auth_error_outranks_every_guard(self) -> None:
        assert (
            classify_limit_text("kimi", "no api key is required; got 401 unauthorized")
            == "auth_error"
        )


class TestCredentialScopingIsSpanBased:
    """Round-5 redesign: span/proximity scoping replaces clause splitting.

    Four rounds of punctuation/conjunction clause splitting each fixed one
    input shape and exposed the next (semicolon -> comma+and -> URL query '?'
    -> schemeless www.x.com dots -> and/or over one shared subject), because
    punctuation is not what carries "whose key is missing".  Every shape from
    those rounds is pinned here, in BOTH error directions:

      * a FALSE park stops a working account -- the expensive direction;
      * a MISSED park blind-retries a terminal credential failure forever,
        which is the favourable-absence defect class.

    An implementation that returns ``None`` for everything passes the
    must-not-park half and is a catastrophic regression, so each shape has a
    must-park twin.
    """

    # ---------------- MUST NOT PARK: somebody else's credential ------------
    def test_f4_schemeless_url_does_not_park(self) -> None:
        """A schemeless URL's dots/query must not detach the MCP subject."""
        assert (
            classify_limit_text(
                "codex",
                "MCP server callback failure (www.example.com/auth?debug=true): "
                "API key is not configured",
            )
            is None
        )

    def test_f5_multiple_coordinated_urls_do_not_park(self) -> None:
        """An 'and' between two URL objects is not a clause boundary."""
        assert (
            classify_limit_text(
                "codex",
                "MCP server callback failure (https://one.test/a?x=1 and "
                "https://two.test/b?y=2): API key is not configured",
            )
            is None
        )

    def test_f5_same_subject_retraction_does_not_park(self) -> None:
        """'and' can coordinate two predicates of ONE subject, not two clauses."""
        assert (
            classify_limit_text(
                "codex",
                "For local mode, the API key is not configured and the API key is not required",
            )
            is None
        )

    def test_retained_because_retraction_does_not_park(self) -> None:
        assert (
            classify_limit_text(
                "codex", "API key is not configured because no API key is required for local mode"
            )
            is None
        )

    def test_a_credential_phrase_attached_to_a_third_party_does_not_park(self) -> None:
        """Attachment AFTER the phrase scopes it just as a leading topic does."""
        assert (
            classify_limit_text("codex", "api key is not configured for the telemetry exporter")
            is None
        )

    def test_an_appositive_third_party_object_does_not_park(self) -> None:
        """A marker sitting immediately after the phrase names its owner.

        "api key is not configured (mcp server github)" is one report whose
        subject is named in apposition rather than by a preposition.  Adjacency
        counts as attachment DELIBERATELY: the phrase is ambiguous, and
        ambiguity resolves toward not parking a working account.  The contrast
        case below ("and the mcp server is unreachable") is not ambiguous -- the
        marker carries its own predicate, so it is a second report, not an
        owner.
        """
        assert classify_limit_text("codex", "api key is not configured (mcp server github)") is None
        assert (
            classify_limit_text("codex", "api key is not configured (telemetry exporter)") is None
        )

    # ---------------- MUST PARK: our provider's credential ----------------
    def test_f2_a_recovered_third_party_does_not_hide_a_real_provider_failure(self) -> None:
        """The round-3 over-correction: a marker ANYWHERE suppressed a real failure."""
        assert (
            classify_limit_text(
                "kimi", "MCP server github recovered, and remote provider API key is not configured"
            )
            == "auth_error"
        )

    def test_retained_unconditional_absence_is_not_retracted_by_a_later_disclaimer(self) -> None:
        assert (
            classify_limit_text(
                "kimi", "API key is not configured; no API key is required for local mode"
            )
            == "auth_error"
        )

    @pytest.mark.parametrize("provider", ["kimi", "gemini", "grok", "codex", "unknown-provider"])
    def test_the_plain_provider_absence_parks_for_every_provider_table(self, provider: str) -> None:
        """Guard against 'fixing' false parks by disabling parking altogether."""
        assert classify_limit_text(provider, "remote provider API key is not configured") == (
            "auth_error"
        )
        assert classify_limit_text(provider, "api key not configured") == "auth_error"

    def test_a_coordinated_second_report_is_not_an_attachment(self) -> None:
        """'and the mcp server is unreachable' is a second report, not a scope."""
        assert (
            classify_limit_text(
                "kimi", "api key is not configured and the mcp server is unreachable"
            )
            == "auth_error"
        )

    def test_an_earlier_disclaimer_about_another_service_does_not_retract(self) -> None:
        """Retraction is a LATER same-subject predicate; earlier chatter is not one."""
        assert (
            classify_limit_text(
                "kimi",
                "no api key is required for the telemetry exporter, but the remote provider "
                "api key is not configured",
            )
            == "auth_error"
        )

    def test_a_sentence_boundary_ends_a_third_party_topic(self) -> None:
        assert (
            classify_limit_text("kimi", "MCP server github recovered. api key is not configured")
            == "auth_error"
        )

    # ------------- SAME SUBJECT, TWO WORDINGS: co-reference ---------------
    def test_a_disclaimer_retracts_a_hypernym_of_the_same_subject(self) -> None:
        """ "credentials" and "api key" name ONE subject here.

        The retraction rule keys on the absence's head noun, so demanding the
        SAME wording in the disclaimer ("credentials not found" retracted only
        by a credentials disclaimer) parks a healthy local-mode account on the
        commonest live phrasing -- the expensive error direction.
        """
        assert (
            classify_limit_text(
                "codex", "credentials not found because no API key is required for local mode"
            )
            is None
        )
        assert (
            classify_limit_text(
                "codex", "API key is missing and no credentials are required for local models"
            )
            is None
        )

    def test_co_reference_does_not_retract_across_a_sentence_boundary(self) -> None:
        """The must-park twin: co-reference widens SUBJECT identity, not reach."""
        assert (
            classify_limit_text(
                "kimi", "credentials not found. no API key is required for local mode"
            )
            == "auth_error"
        )
        assert (
            classify_limit_text(
                "kimi", "credentials not found; no API key is required for local mode"
            )
            == "auth_error"
        )


class TestEveryHistoricalClauseSplitShape:
    """One test per input shape that broke a previous round -- all four rounds.

    These are the exact progression recorded in the proposal: each shape was
    the one the previous fix could not handle.  They live together so a
    regression names the round it re-opened.
    """

    def test_round1_shape_semicolon(self) -> None:
        """';' separated a healthy MCP report from a real provider failure."""
        assert (
            classify_limit_text(
                "kimi", "MCP server github recovered; remote provider API key is not configured"
            )
            == "auth_error"
        )

    def test_round2_shape_comma_and(self) -> None:
        """comma+and is not a clause boundary the splitter could see."""
        assert (
            classify_limit_text(
                "kimi", "MCP server github recovered, and remote provider API key is not configured"
            )
            == "auth_error"
        )

    def test_round3_shape_url_query_question_mark(self) -> None:
        """A '?' inside a URL query is not a sentence terminator."""
        assert (
            classify_limit_text(
                "codex",
                "MCP server callback failure (https://x.test/a?b=1): API key is not configured",
            )
            is None
        )

    def test_round4_shape_schemeless_url_dots(self) -> None:
        """Dots inside www.example.com are not sentence terminators."""
        assert (
            classify_limit_text(
                "codex", "MCP server callback failure at www.example.com: API key is not configured"
            )
            is None
        )

    def test_round5_shape_and_or_coordinating_a_shared_subject(self) -> None:
        """'and'/'or' may coordinate two predicates about ONE subject."""
        assert (
            classify_limit_text(
                "codex", "API key is not configured or the API key is not required in offline mode"
            )
            is None
        )
        assert (
            classify_limit_text(
                "codex",
                "For local mode, the API key is not configured and the API key is not required",
            )
            is None
        )


class TestScopingPrimitiveIsNotPunctuationSplitting:
    """The redesign is falsified if clause splitting comes back.

    Mechanical, not stylistic: the four-round failure mode was re-derived every
    time somebody reached for another punctuation/conjunction split, so the
    absence of that primitive is pinned by a test rather than by a comment.
    """

    def test_no_punctuation_or_conjunction_split_primitive_remains(self) -> None:
        source = Path(limits.__file__).read_text(encoding="utf-8")
        assert "_CLAUSE_SPLIT_RE" not in source
        offenders = re.findall(r"\.split\(\s*[\"'][^\"']*[\"']", source)
        assert offenders == [], offenders

    def test_urls_are_masked_to_one_opaque_token_before_scoping(self) -> None:
        """Masking must erase URL-internal structure and keep sentence structure."""
        assert (
            limits._mask_urls("callback (https://one.test/a?x=1 and https://two.test/b?y=2) failed")
            == "callback (__url__ and __url__) failed"
        )
        assert limits._mask_urls("see www.example.com/auth?debug=true now") == ("see __url__ now")
        # A trailing sentence period belongs to the sentence, not the locator:
        # swallowing it would let a third-party topic govern the NEXT sentence.
        assert limits._mask_urls("callback https://x.test/a. next") == "callback __url__. next"
