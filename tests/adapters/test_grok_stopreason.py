"""GrokAdapter._parse must not treat a turn-capped envelope as a completed review.

When the Grok CLI hits its turn cap it returns a well-formed JSON envelope
with ``stopReason: "cancelled"``, a short narration stub in ``.text``, and
the substantive reasoning -- including the verdict line -- stranded in
``.thought``. pipeline/ROUTING.md:66-69 requires callers to check
``stopReason == "end_turn"``; these tests pin that check at the adapter
chokepoint so it cannot be forgotten by an individual caller.
"""

from __future__ import annotations

import json

import pytest

from omniagentos.adapters.grok import GrokAdapter


def test_cancelled_envelope_raises() -> None:
    envelope = json.dumps(
        {
            "stopReason": "cancelled",
            "text": "short narration stub",
            "thought": "...the substantive reasoning with a verdict line...",
            "sessionId": "sess-1",
            "num_turns": 8,
        }
    )
    with pytest.raises(ValueError, match="cancelled"):
        GrokAdapter()._parse(envelope)


def test_end_turn_and_missing_stopreason_still_parse() -> None:
    end_turn_envelope = json.dumps(
        {
            "stopReason": "end_turn",
            "text": "a complete review",
            "sessionId": "sess-2",
        }
    )
    parsed = GrokAdapter()._parse(end_turn_envelope)
    assert parsed.text == "a complete review"
    assert parsed.session_ref == "sess-2"

    missing_stopreason_envelope = json.dumps(
        {
            "text": "an older-shape envelope with no stopReason key",
            "sessionId": "sess-3",
        }
    )
    parsed_missing = GrokAdapter()._parse(missing_stopreason_envelope)
    assert parsed_missing.text == "an older-shape envelope with no stopReason key"
    assert parsed_missing.session_ref == "sess-3"


def test_raised_message_names_the_stopreason_value() -> None:
    envelope = json.dumps(
        {
            "stopReason": "cancelled",
            "text": "short stub",
            "num_turns": 8,
        }
    )
    with pytest.raises(ValueError) as exc_info:
        GrokAdapter()._parse(envelope)
    assert "cancelled" in str(exc_info.value)
    assert "8" in str(exc_info.value)
