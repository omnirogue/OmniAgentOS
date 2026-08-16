"""RFC 8785 vectors for the hand-rolled canonicaliser.

canonical.py's docstring claimed these existed before they did — caught by the
planning loop reading its own dependencies. The claim mattered: two loops that
canonicalise differently produce different ids for one payload, dedup silently
stops working, and nothing errors.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bridge"))
import pytest
from canonical import canonicalise, content_id


@pytest.mark.parametrize("value,want", [
    ({"a": 1, "b": 2}, '{"a":1,"b":2}'),
    ({"b": 2, "a": 1}, '{"a":1,"b":2}'),          # key order normalised
    ({"x": 1.0}, '{"x":1}'),                       # ECMAScript number form
    ({"x": -0.0}, '{"x":0}'),                      # -0 collapses
    ({"x": 1e21}, '{"x":1e+21}'),
    ({"x": [1, {"z": 1, "y": 2}]}, '{"x":[1,{"y":2,"z":1}]}'),   # nested sort
    ({"x": "a\nb"}, '{"x":"a\\nb"}'),              # short escape, not \\u000a
    ({"x": True, "y": None}, '{"x":true,"y":null}'),
    ({}, "{}"),
    ({"x": []}, '{"x":[]}'),
])
def test_vectors(value, want):
    assert canonicalise(value) == want


def test_id_is_order_independent():
    a = content_id({"area": "gate", "observation": "slow", "why_not_a_fix": "unknown"})
    b = content_id({"why_not_a_fix": "unknown", "observation": "slow", "area": "gate"})
    assert a == b and a.startswith("sha256:") and len(a) == 71


def test_timestamp_in_payload_changes_id():
    """Why CONTRACT forbids a clock in the payload: it would defeat dedup."""
    assert content_id({"a": 1, "t": "10:00"}) != content_id({"a": 1, "t": "10:15"})


def test_non_finite_refused():
    """NaN/Infinity are not JSON. Refuse loudly rather than emit invalid output
    a reader rejects later, far from the cause."""
    with pytest.raises(ValueError):
        canonicalise({"x": float("nan")})
    with pytest.raises(ValueError):
        canonicalise({"x": float("inf")})


def test_utf16_key_ordering():
    """RFC 8785 orders by UTF-16 code unit, not code point. They diverge above
    the BMP: an emoji sorts AFTER a BMP char by code point but BEFORE it in
    UTF-16, because the surrogate pair starts 0xD83D."""
    out = canonicalise({"\U0001F600": 1, "ﬀ": 2})
    assert out.index("\U0001F600") < out.index("ﬀ")
