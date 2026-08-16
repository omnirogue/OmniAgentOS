"""
FROZEN acceptance check for fx_015_shared_schema_consistency.
This is copied in after the agent finishes, so the agent cannot weaken it.
"""

from __future__ import annotations

import reader
import schema
import validator
import writer


def test_schema_version_and_fields():
    assert schema.VERSION == 2
    names = schema.field_names()
    assert names == ("id", "name", "active", "note")

    f_active = schema.field_by_name("active")
    assert f_active.kind == "bool"
    assert f_active.required is True

    f_note = schema.field_by_name("note")
    assert f_note.kind == "str"
    assert f_note.required is False


def test_writer_and_reader_roundtrip():
    # Record with all fields
    rec = {"id": 42, "name": "Alice\\Bob|Charlie", "active": True, "note": "Hello World"}

    assert validator.validate(rec) == []
    encoded = writer.encode(rec)

    # Check formatting and escaping
    # active is rendered as true, name's | and \ are escaped
    assert "id=42" in encoded
    assert "name=Alice\\\\Bob\\|Charlie" in encoded
    assert "active=true" in encoded
    assert "note=Hello World" in encoded

    # Check field order in encoded string
    expected_order = ["id=", "name=", "active=", "note="]
    positions = [encoded.index(prefix) for prefix in expected_order]
    assert positions == sorted(positions), f"Fields are not in schema order: {encoded}"

    # Decode and check equality
    decoded = reader.decode(encoded)
    assert decoded == rec


def test_reader_back_compat_and_defaults():
    # v1 line has only id and name. active defaults to False, note to ""
    v1_line = "id=100|name=Bob"
    decoded = reader.decode(v1_line)
    assert decoded == {"id": 100, "name": "Bob", "active": False, "note": ""}

    # Missing optional field on v2
    v2_line = "id=101|name=Charlie|active=true"
    decoded2 = reader.decode(v2_line)
    assert decoded2 == {"id": 101, "name": "Charlie", "active": True, "note": ""}


def test_reader_failures():
    # Missing required id
    try:
        reader.decode("name=Bob|active=true")
        raise AssertionError("Should raise DecodeError for missing required id")
    except reader.DecodeError:
        pass

    # Missing required active is back-compat and defaults to False, but missing name is not!
    try:
        reader.decode("id=123|active=true")
        raise AssertionError("Should raise DecodeError for missing name")
    except reader.DecodeError:
        pass

    # Wrong type for int
    try:
        reader.decode("id=abc|name=Bob|active=true")
        raise AssertionError("Should raise DecodeError for bad int type")
    except reader.DecodeError:
        pass

    # Wrong value for bool
    try:
        reader.decode("id=1|name=Bob|active=maybe")
        raise AssertionError("Should raise DecodeError for bad bool value")
    except reader.DecodeError:
        pass

    # Unknown key
    try:
        reader.decode("id=1|name=Bob|active=true|unknown=yes")
        raise AssertionError("Should raise DecodeError for unknown key")
    except reader.DecodeError:
        pass


def test_validator_problems():
    # Missing required field
    rec_missing = {"name": "Bob", "active": True}
    probs = validator.validate(rec_missing)
    assert len(probs) == 1
    assert "missing required field" in probs[0] and "id" in probs[0]

    # Unknown key
    rec_unknown = {"id": 1, "name": "Bob", "active": True, "extra": 123}
    probs2 = validator.validate(rec_unknown)
    assert len(probs2) == 1
    assert "unknown field" in probs2[0] and "extra" in probs2[0]

    # Wrong types
    rec_types = {"id": "1", "name": 123, "active": "true", "note": True}
    probs3 = validator.validate(rec_types)
    assert len(probs3) == 4
    assert any("id" in p and "invalid type" in p for p in probs3)
    assert any("name" in p and "invalid type" in p for p in probs3)
    assert any("active" in p and "invalid type" in p for p in probs3)
    assert any("note" in p and "invalid type" in p for p in probs3)


def test_dynamic_schema_monkeypatch():
    # The decisive test: build a NEW Field at runtime, append it to a COPY of the field tuple,
    # monkeypatch schema.FIELDS (and restore it in a finally), and assert that encode, decode and
    # validate ALL immediately honour the extra field.
    orig_fields = schema.FIELDS
    try:
        new_field = schema.Field(name="score", kind="int", required=True)
        schema.FIELDS = orig_fields + (new_field,)

        # 1. validator must check score
        rec = {"id": 1, "name": "Bob", "active": True, "note": "ok"}
        probs = validator.validate(rec)
        assert any("score" in p and "missing" in p for p in probs)

        rec["score"] = "not-an-int"
        probs = validator.validate(rec)
        assert any("score" in p and "invalid type" in p for p in probs)

        rec["score"] = 100
        assert validator.validate(rec) == []

        # 2. writer must encode score at the end
        encoded = writer.encode(rec)
        assert encoded.endswith("|score=100"), f"Expected score=100 at the end of {encoded}"

        # 3. reader must decode score
        decoded = reader.decode(encoded)
        assert decoded["score"] == 100

        # if score is missing on decode
        encoded_missing = "id=1|name=Bob|active=true|note=ok"
        try:
            reader.decode(encoded_missing)
            raise AssertionError("Should raise DecodeError because score is required and missing")
        except reader.DecodeError:
            pass

    finally:
        schema.FIELDS = orig_fields
