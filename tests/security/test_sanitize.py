"""Tests for key-aware credential sanitization and persistence."""

from __future__ import annotations

import json
import uuid
from typing import Any

from omniagentos.security import sanitize as sanitize_module
from omniagentos.security.sanitize import (
    MAX_JSON_PARSE_DEPTH,
    MAX_JSON_PARSE_SIZE,
    REDACTED,
    SCRUBBER_VERSION,
    sanitize_for_persistence,
)
from omniagentos.toolplane.scrub import scrub_text


def _test_credential(label: str) -> str:
    """A distinctive, test-generated secret-shaped string.

    Generated per call rather than written into the file: a literal that looks
    like a credential in a repo is a liability, and a value the scrubber could
    have memorised proves nothing about the scrubber.
    """
    return f"sk_live_{label}_{uuid.uuid4().hex}"


def _opaque_credential(label: str) -> str:
    """A secret-shaped value the PATTERN scrubber cannot see.

    :func:`_test_credential` returns an ``sk_live_`` string, and
    ``toolplane/scrub.py`` matches that on sight. A DoS-cap test built on it
    would therefore go green whether or not the cap fails closed -- the pattern
    scrubber would catch the value on the way out and the assertion could never
    fail for the reason it names.

    Key-aware redaction is the ONLY control that sees this shape, so it is the
    only value that can prove a cap. ``test_the_opaque_credential_is_actually
    _invisible_to_the_pattern_scrubber`` keeps that property honest.
    """
    return f"{label}-{uuid.uuid4().hex}{uuid.uuid4().hex}"


class TestBasicRedaction:
    """Basic credential key redaction tests."""

    def test_redact_value_under_credential_key(self) -> None:
        """Values under credential-shaped keys are redacted."""
        payload = {"api_key": "secret-token-12345"}
        sanitized, count, version = sanitize_for_persistence("test", payload)

        assert sanitized["api_key"] == REDACTED
        assert count >= 1
        assert version == SCRUBBER_VERSION

    def test_redact_multiple_credential_keys(self) -> None:
        """Multiple credential keys in same dict are all redacted."""
        payload = {
            "api_key": "secret1",
            "password": "secret2",
            "token": "secret3",
        }
        sanitized, count, version = sanitize_for_persistence("test", payload)

        assert sanitized["api_key"] == REDACTED
        assert sanitized["password"] == REDACTED
        assert sanitized["token"] == REDACTED
        assert count >= 3

    def test_non_credential_keys_preserved(self) -> None:
        """Non-credential keys and their values are preserved."""
        payload = {
            "username": "john_doe",
            "email": "john@example.com",
            "id": 12345,
        }
        sanitized, count, version = sanitize_for_persistence("test", payload)

        assert sanitized["username"] == "john_doe"
        assert sanitized["email"] == "john@example.com"
        assert sanitized["id"] == 12345
        assert count == 0

    def test_nested_credential_redaction(self) -> None:
        """Nested dicts with credential keys get redacted."""
        payload = {
            "outer": {
                "inner": {
                    "password": "deep_secret",
                    "name": "nested_value",
                }
            }
        }
        sanitized, count, version = sanitize_for_persistence("test", payload)

        assert sanitized["outer"]["inner"]["password"] == REDACTED
        assert sanitized["outer"]["inner"]["name"] == "nested_value"


class TestJsonStringParsing:
    """JSON string parsing and scrubbing tests."""

    def test_json_string_with_credential_key_redacted(self) -> None:
        """Quoted JSON strings with credential keys are parsed and scrubbed."""
        credential_obj = {"api_key": "secret123", "name": "service"}
        json_str = json.dumps(credential_obj)
        payload = {"config": json_str}
        sanitized, count, version = sanitize_for_persistence("test", payload)

        # The string should be parsed, scrubbed, and re-serialized
        assert isinstance(sanitized["config"], str)
        reparsed = json.loads(sanitized["config"])
        assert reparsed["api_key"] == REDACTED
        assert reparsed["name"] == "service"
        assert count >= 1

    def test_nested_json_strings_redacted(self) -> None:
        """Nested JSON strings with credentials are scrubbed."""
        inner_obj = {"password": "secret"}
        outer_obj = {"config": json.dumps(inner_obj)}
        payload = {"settings": json.dumps(outer_obj)}

        sanitized, count, version = sanitize_for_persistence("test", payload)

        # Outer string is parsed
        outer_parsed = json.loads(sanitized["settings"])
        # Inner string is also parsed (2 levels deep)
        inner_parsed = json.loads(outer_parsed["config"])
        assert inner_parsed["password"] == REDACTED

    def test_invalid_json_string_not_crashed(self) -> None:
        """Invalid JSON strings are left as-is (not parsed)."""
        payload = {"config": "not { valid json"}
        sanitized, count, version = sanitize_for_persistence("test", payload)

        # Should remain the same string
        assert sanitized["config"] == "not { valid json"

    def test_json_string_under_credential_key_redacted(self) -> None:
        """JSON strings under credential keys are fully redacted.

        This tests the documented gap from scrub.py — a credential value
        hidden inside a quoted JSON string should be caught.
        """
        payload = {
            "auth_config": json.dumps(
                {"token": "secret-token-xyz", "public": "info"}
            )
        }
        sanitized, count, version = sanitize_for_persistence("test", payload)

        # The entire value under "auth_config" (credential key) should be REDACTED
        # because the key itself is credential-shaped
        assert sanitized["auth_config"] == REDACTED


class TestListAndTupleHandling:
    """Tests for list and tuple redaction."""

    def test_list_of_dicts_with_credentials(self) -> None:
        """Lists containing dicts with credential keys are scrubbed."""
        payload = {
            "configs": [
                {"api_key": "key1", "name": "config1"},
                {"api_key": "key2", "name": "config2"},
            ]
        }
        sanitized, count, version = sanitize_for_persistence("test", payload)

        assert sanitized["configs"][0]["api_key"] == REDACTED
        assert sanitized["configs"][0]["name"] == "config1"
        assert sanitized["configs"][1]["api_key"] == REDACTED
        assert sanitized["configs"][1]["name"] == "config2"

    def test_tuple_with_credentials(self) -> None:
        """Tuples are preserved and credentials redacted."""
        payload = {"data": ({"password": "secret"}, "public_string")}
        sanitized, count, version = sanitize_for_persistence("test", payload)

        # Tuples are preserved
        assert isinstance(sanitized["data"], tuple)
        assert sanitized["data"][0]["password"] == REDACTED
        assert sanitized["data"][1] == "public_string"


class TestPatternBasedScrubbing:
    """Tests for inline pattern-based credential scrubbing."""

    def test_bearer_token_pattern_scrubbed(self) -> None:
        """Bearer tokens in strings are scrubbed."""
        payload = {
            "response": "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        }
        sanitized, count, version = sanitize_for_persistence("test", payload)

        # Pattern should be scrubbed
        assert "Bearer" not in sanitized["response"] or "[REDACTED]" in sanitized["response"]

    def test_api_key_pattern_scrubbed(self) -> None:
        """API key patterns are scrubbed."""
        payload = {"config": "api_key=sk_live_abcdefghijklmnopqrstuvwxyz"}
        sanitized, count, version = sanitize_for_persistence("test", payload)

        # Pattern should be scrubbed
        assert "sk_live" not in sanitized["config"] or "[REDACTED]" in sanitized["config"]


class TestDoSProtection:
    """Tests for DoS protection limits."""

    def test_oversized_json_string_is_not_parsed_and_is_not_handed_back_raw(self) -> None:
        """Over the size cap we decline the WORK -- we do not vouch for the value.

        The previous assertion here was ``sanitized["config"] == large_json``:
        it required the over-cap payload to round-trip verbatim, i.e. it pinned
        the bypass as correct. It is the same assertion shape
        ``test_deeply_nested_json_parsing_limited`` below explicitly rejected
        when the DEPTH cap was made fail-closed -- "true of every possible
        implementation including the one that returned the attacker's bytes
        verbatim" -- and its payload carried no credential-shaped key, so it
        could not have failed on a credential either way.

        What is still asserted, because it is the real DoS property: the string
        is not parsed into a structure (the cap bounds the work).
        """
        large_json = json.dumps({"data": "x" * 200_000})
        payload = {"config": large_json}

        sanitized, count, _version = sanitize_for_persistence("test", payload)

        # The cap still refuses the parse: no structure comes back.
        assert isinstance(sanitized["config"], str)
        # But declining to look is not a clean bill of health.
        assert sanitized["config"] != large_json, (
            "an over-cap JSON payload round-tripped verbatim; the cap is "
            "supposed to bound the WORK, not the guarantee"
        )
        assert count >= 1, "a value we stopped inspecting cannot be reported as clean"

    def test_the_opaque_credential_is_actually_invisible_to_the_pattern_scrubber(self) -> None:
        """Vacuity guard for every size-cap test below.

        If ``scrub_text`` ever learns this shape, the cap tests would go green
        through the pattern scrubber instead of through the cap, and would stop
        being able to fail for the reason they name. Then this test fails first
        and says so.
        """
        secret = _opaque_credential("vacuity")
        blob = json.dumps({"api_key": secret})

        assert scrub_text(blob) == blob, (
            "the pattern scrubber now catches the opaque test credential, so the "
            "size-cap tests can no longer prove the cap -- pick a new shape"
        )

    def test_size_cap_is_a_fail_closed_edge_exactly_like_the_depth_cap(self) -> None:
        """A credential one byte past the size cap must not survive.

        Under the cap the ordinary credential-key rule redacts it. Over the cap
        the key rule never runs, because the string is never parsed -- so unless
        the cap itself fails closed, the value is persisted verbatim with
        ``redactions: 0``. The under-cap row is the control: it proves the test
        is measuring the CAP and not merely the key rule.

        Payload shape is ``web_read.py``'s verbatim: the fetched body is a
        string under a non-credential key.
        """

        def _at(total_len: int) -> tuple[str, Any, int]:
            secret = _opaque_credential("size-cap")
            base = len(json.dumps({"api_key": secret, "pad": ""}))
            blob = json.dumps({"api_key": secret, "pad": "A" * (total_len - base)})
            assert len(blob) == total_len, "test built the wrong payload size"
            out, count, _v = sanitize_for_persistence("test", {"content": blob})
            return secret, out, count

        under_secret, under_out, under_count = _at(MAX_JSON_PARSE_SIZE)
        over_secret, over_out, over_count = _at(MAX_JSON_PARSE_SIZE + 1)

        # Control: one byte under, the key rule reaches it.
        assert under_secret not in json.dumps(under_out)
        assert under_count >= 1

        # The finding: one byte over, nothing reaches it unless the cap does.
        assert over_secret not in json.dumps(over_out), (
            "a credential one byte past MAX_JSON_PARSE_SIZE round-tripped "
            "verbatim; the size cap bounds the guarantee instead of the work"
        )
        assert over_count >= 1, "a value we stopped inspecting cannot be reported as clean"

    def test_over_cap_strings_that_are_not_json_keep_todays_treatment(self) -> None:
        """The fail-closed edge is scoped to content we DECLINED to inspect.

        A 200 KB HTML page was never going to parse as JSON, so there is no
        structured content the cap caused us to skip, and blanking it would
        destroy the payload ``web_fetch`` exists to return. It stays on the
        pattern-scrubbing path.
        """
        page = "<html><body>" + ("lorem ipsum " * 20_000) + "</body></html>"
        assert len(page) > MAX_JSON_PARSE_SIZE

        sanitized, _count, _v = sanitize_for_persistence("test", {"content": page})

        assert sanitized["content"] == page
        assert REDACTED not in sanitized["content"]

    def test_every_dos_cap_in_this_module_has_a_fail_closed_proof(self) -> None:
        """Glob the caps rather than listing them.

        A hand-written list of the caps known today has the same failure mode as
        the bypass it is testing: the SIZE cap sat open for a week beside a DEPTH
        cap that had been fixed, because nothing enumerated the pair. This
        discovers every ``MAX_JSON_PARSE_*`` constant in the module and fails if
        one has no over-cap proof, so a third cap cannot land silently.
        """
        discovered = {n for n in dir(sanitize_module) if n.startswith("MAX_JSON_PARSE_")}
        assert discovered, "discovered no cap constants; this test would pass vacuously"

        def _over_depth(secret: str) -> Any:
            node: Any = {"api_key": secret}
            for _ in range(MAX_JSON_PARSE_DEPTH + 4):
                node = {"level": node}
            return node

        def _over_size(secret: str) -> Any:
            return {"content": json.dumps({"api_key": secret, "pad": "A" * MAX_JSON_PARSE_SIZE})}

        proofs = {"MAX_JSON_PARSE_DEPTH": _over_depth, "MAX_JSON_PARSE_SIZE": _over_size}

        assert discovered == set(proofs), (
            f"cap constants without a fail-closed proof: {sorted(discovered - set(proofs))}; "
            f"proofs for constants that no longer exist: {sorted(set(proofs) - discovered)}"
        )

        for name, build in proofs.items():
            secret = _opaque_credential(name)
            out, count, _v = sanitize_for_persistence("test", build(secret))
            assert secret not in json.dumps(out), f"{name} does not fail closed"
            assert count >= 1, f"{name} reports a payload it never inspected as clean"

    def test_deeply_nested_json_parsing_limited(self) -> None:
        """Past the parse cap the JSON string is REDACTED, never handed back raw.

        The old assertion here was ``isinstance(result, (str, dict))``, which is
        true of every possible implementation including the one that returned the
        attacker's bytes verbatim. This one names the value that must not survive.
        """
        secret = _test_credential("nested-json")
        nested: Any = {"api_key": secret}
        for _ in range(15):
            nested = {"level": nested}

        json_str = json.dumps(nested)
        payload = {"config": json_str}

        sanitized, count, _version = sanitize_for_persistence("test", payload)

        assert secret not in json.dumps(sanitized), (
            "a credential nested past the DoS cap round-tripped verbatim; the cap "
            "is supposed to bound the WORK, not the guarantee"
        )
        assert count >= 1, "a value we stopped inspecting cannot be reported as clean"

    def test_deeply_nested_dict_handling(self) -> None:
        """The same fail-closed edge for a plain dict, not just a JSON string."""
        secret = _test_credential("nested-dict")
        nested: Any = {"api_key": secret}
        for _ in range(20):
            nested = {"level": nested}

        payload = {"data": nested}
        sanitized, count, _version = sanitize_for_persistence("test", payload)

        assert secret not in json.dumps(sanitized)
        assert count >= 1

    def test_over_cap_values_are_redacted_wholesale_not_merely_truncated(self) -> None:
        """The cap is a fail-CLOSED edge: what we stopped reading is redacted.

        Depth 12 with a credential-shaped key is Kimi's exact reproduction. The
        depth-5 control proves the test is measuring the cap and not simply the
        ordinary credential-key redaction.
        """
        deep_secret = _test_credential("depth-12")
        shallow_secret = _test_credential("depth-5")

        def _nest(levels: int, value: str) -> Any:
            node: Any = {"api_key": value}
            for _ in range(levels):
                node = {"level": node}
            return node

        deep_out, deep_count, _v = sanitize_for_persistence("test", _nest(12, deep_secret))
        shallow_out, shallow_count, _v2 = sanitize_for_persistence(
            "test", _nest(5, shallow_secret)
        )

        assert deep_secret not in json.dumps(deep_out)
        assert deep_count > 0
        # Control: the shallow one is redacted by the ordinary key rule.
        assert shallow_secret not in json.dumps(shallow_out)
        assert shallow_count > 0

    def test_the_cap_redacts_primitives_too_not_just_containers(self) -> None:
        """Past the cap we stopped looking at ALL of it, scalars included."""
        secret = _test_credential("bare-scalar")
        node: Any = secret
        for _ in range(14):
            node = {"harmless": node}

        sanitized, count, _version = sanitize_for_persistence("test", node)

        assert secret not in json.dumps(sanitized)
        assert count >= 1


class TestRedactionCounting:
    """Tests for accurate redaction counting."""

    def test_redaction_count_for_single_key(self) -> None:
        """Single credential redaction is counted."""
        payload = {"password": "secret"}
        sanitized, count, version = sanitize_for_persistence("test", payload)

        assert count >= 1

    def test_redaction_count_for_multiple_types(self) -> None:
        """Multiple redaction types contribute to count."""
        payload = {
            "api_key": "secret1",
            "password": "secret2",
            "token": "secret3",
            "description": "api_key=sk_live_token",  # Pattern scrubbing
        }
        sanitized, count, version = sanitize_for_persistence("test", payload)

        # At least 3 credential key redactions + 1 pattern scrubbing = 4+
        assert count >= 4

    def test_zero_redactions_when_no_secrets(self) -> None:
        """No redactions when payload has no secrets."""
        payload = {
            "name": "John",
            "age": 30,
            "email": "john@example.com",
        }
        sanitized, count, version = sanitize_for_persistence("test", payload)

        assert count == 0


class TestVersionTracking:
    """Tests for version tracking."""

    def test_version_returned(self) -> None:
        """Scrubber version is always returned."""
        payload = {"data": "test"}
        sanitized, count, version = sanitize_for_persistence("test", payload)

        assert version == SCRUBBER_VERSION
        assert isinstance(version, str)
        assert "." in version

    def test_version_matches_module_constant(self) -> None:
        """Returned version matches module constant."""
        payload = {"key": "value"}
        _, _, version = sanitize_for_persistence("test", payload)

        assert version == SCRUBBER_VERSION


class TestSurfaceParameter:
    """Tests for surface parameter usage."""

    def test_surface_parameter_accepted(self) -> None:
        """Surface parameter is accepted without error."""
        surfaces = [
            "toolplane_result",
            "persistence_checkpoint",
            "audit_log",
            "custom_surface",
        ]
        payload = {"api_key": "secret"}

        for surface in surfaces:
            sanitized, count, version = sanitize_for_persistence(surface, payload)
            assert sanitized["api_key"] == REDACTED


class TestTypePreservation:
    """Tests for type preservation."""

    def test_dict_returns_dict(self) -> None:
        """Dict payload returns dict result."""
        payload = {"key": "value"}
        sanitized, _, _ = sanitize_for_persistence("test", payload)

        assert isinstance(sanitized, dict)

    def test_list_returns_list(self) -> None:
        """List payload returns list result."""
        payload = [{"api_key": "secret"}, "public"]
        sanitized, _, _ = sanitize_for_persistence("test", payload)

        assert isinstance(sanitized, list)
        assert len(sanitized) == 2

    def test_string_returns_string(self) -> None:
        """String payload returns string result."""
        payload = "simple string"
        sanitized, _, _ = sanitize_for_persistence("test", payload)

        assert isinstance(sanitized, str)

    def test_primitives_unchanged(self) -> None:
        """Primitive types are unchanged."""
        payload = {"int": 42, "float": 3.14, "bool": True, "none": None}
        sanitized, _, _ = sanitize_for_persistence("test", payload)

        assert sanitized["int"] == 42
        assert sanitized["float"] == 3.14
        assert sanitized["bool"] is True
        assert sanitized["none"] is None


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_dict(self) -> None:
        """Empty dict is handled."""
        payload = {}
        sanitized, count, version = sanitize_for_persistence("test", payload)

        assert sanitized == {}
        assert count == 0

    def test_empty_list(self) -> None:
        """Empty list is handled."""
        payload = []
        sanitized, count, version = sanitize_for_persistence("test", payload)

        assert sanitized == []
        assert count == 0

    def test_empty_string(self) -> None:
        """Empty string is handled."""
        payload = {"text": ""}
        sanitized, count, version = sanitize_for_persistence("test", payload)

        assert sanitized["text"] == ""
        assert count == 0

    def test_none_value(self) -> None:
        """None value is handled."""
        payload = {"value": None}
        sanitized, count, version = sanitize_for_persistence("test", payload)

        assert sanitized["value"] is None

    def test_key_name_normalization(self) -> None:
        """Key matching is case-insensitive."""
        payload = {
            "API_KEY": "secret1",
            "Password": "secret2",
            "TOKEN": "secret3",
        }
        sanitized, count, version = sanitize_for_persistence("test", payload)

        assert sanitized["API_KEY"] == REDACTED
        assert sanitized["Password"] == REDACTED
        assert sanitized["TOKEN"] == REDACTED
