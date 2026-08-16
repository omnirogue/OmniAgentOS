# check_roundtrip.py (seed version)
# This is a small visible round-trip check that the agent can run
from __future__ import annotations

import reader
import validator
import writer


def test_v1_roundtrip():
    record = {"id": 1, "name": "alice"}
    assert validator.validate(record) == []
    encoded = writer.encode(record)
    assert encoded == "id=1|name=alice"
    decoded = reader.decode(encoded)
    assert decoded == record


def test_v1_invalid():
    # id has wrong type
    record = {"id": "one", "name": "alice"}
    probs = validator.validate(record)
    assert len(probs) > 0
