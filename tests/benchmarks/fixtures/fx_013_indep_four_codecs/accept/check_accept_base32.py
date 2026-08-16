"""FROZEN acceptance check for fx_013_indep_four_codecs.

Copied into the workspace AFTER the agent finishes, overwriting anything at the
same path, so an agent cannot weaken it.
"""

from __future__ import annotations

from codec.base32 import CodecError, b32_decode, b32_encode


def test_b32_encode_vectors() -> None:
    # RFC 4648 Section 10 vectors
    assert b32_encode(b"") == ""
    assert b32_encode(b"f") == "MY======"
    assert b32_encode(b"fo") == "MZXQ===="
    assert b32_encode(b"foo") == "MZXW6==="
    assert b32_encode(b"foob") == "MZXW6YQ="
    assert b32_encode(b"fooba") == "MZXW6YTB"
    assert b32_encode(b"foobar") == "MZXW6YTBOI======"


def test_b32_decode_vectors() -> None:
    # RFC 4648 Section 10 vectors
    assert b32_decode("") == b""
    assert b32_decode("MY======") == b"f"
    assert b32_decode("MZXQ====") == b"fo"
    assert b32_decode("MZXW6===") == b"foo"
    assert b32_decode("MZXW6YQ=") == b"foob"
    assert b32_decode("MZXW6YTB") == b"fooba"
    assert b32_decode("MZXW6YTBOI======") == b"foobar"


def test_b32_decode_errors() -> None:
    # Bad length (must be multiple of 8)
    for bad in ("A", "AB", "ABC", "ABCD", "ABCDE", "ABCDEF", "ABCDEFG"):
        try:
            b32_decode(bad)
            raise AssertionError(f"Should raise CodecError for length {len(bad)}")
        except CodecError:
            pass

    # Accepts upper case only
    try:
        b32_decode("my======")
        raise AssertionError("Should raise CodecError for lower case")
    except CodecError:
        pass

    # Invalid characters
    try:
        b32_decode("MY1=====")
        raise AssertionError("Should raise CodecError for '1'")
    except CodecError:
        pass

    # Invalid padding position (padding in the middle)
    try:
        b32_decode("M=XW6===")
        raise AssertionError("Should raise CodecError for padding in middle")
    except CodecError:
        pass

    # Invalid padding count (2 or 5 or 7 padding chars are mathematically invalid)
    for bad_pad in ("MZXW6Y==", "MZXW===", "M======="):
        try:
            b32_decode(bad_pad)
            raise AssertionError(f"Should raise CodecError for invalid padding format {bad_pad}")
        except CodecError:
            pass

    # Non-zero leftover bits (last bits representing 0 padding must be zero)
    # "MY======" is valid (decodes b"f"), values: M(12), Y(24 - binary 11000). The last 2 bits are 00.
    # Changing Y to Z(25 - binary 11001) violates the non-zero leftover bits rule.
    try:
        b32_decode("MZ======")
        raise AssertionError("Should raise CodecError for non-zero leftover bits")
    except CodecError:
        pass


def test_b32_roundtrip() -> None:
    for data in (b"", b"hello", b"test data!", bytes(range(256))):
        assert b32_decode(b32_encode(data)) == data
