"""
FROZEN acceptance check for fx_009_seq_diff_patch_roundtrip.
This file is copied in after the agent finishes so the agent cannot weaken it.
"""

from __future__ import annotations

import os

import mydiff
import mypatch
import roundtrip


def test_lcs_length_basic():
    # Basic LCS checks
    assert mydiff.lcs_length([], []) == 0
    assert mydiff.lcs_length(["A"], []) == 0
    assert mydiff.lcs_length([], ["B"]) == 0
    assert mydiff.lcs_length(["A"], ["A"]) == 1
    assert mydiff.lcs_length(["A", "B", "C"], ["A", "C"]) == 2
    assert mydiff.lcs_length(["A", "B", "C"], ["D", "E"]) == 0
    assert mydiff.lcs_length(["A", "B", "C", "D"], ["B", "D", "A"]) == 2
    assert mydiff.lcs_length(["g", "e", "n", "o", "m", "e"], ["m", "e", "n", "t", "o", "r"]) == 3


def test_minimality_formula():
    # Test cases to check that diff satisfies the minimality formula
    test_cases = [
        ([], []),
        (["A"], []),
        ([], ["B"]),
        (["A"], ["B"]),
        (["A", "B"], ["B", "A"]),
        (["A", "B", "C"], ["X", "B", "Y"]),
        (["apple", "banana", "cherry"], ["apple", "cherry", "dragonfruit"]),
    ]

    # Also load the sample files if they exist in the current workspace
    if os.path.exists("samples/before.txt") and os.path.exists("samples/after.txt"):
        with open("samples/before.txt", encoding="utf-8") as f:
            before = f.read().splitlines()
        with open("samples/after.txt", encoding="utf-8") as f:
            after = f.read().splitlines()
        test_cases.append((before, after))

    for a, b in test_cases:
        ops = mydiff.diff_lines(a, b)
        lcs_len = mydiff.lcs_length(a, b)

        deletions = [op for op in ops if op[0] == "-"]
        insertions = [op for op in ops if op[0] == "+"]

        # Verify the minimality condition
        assert len(deletions) + len(insertions) == len(a) + len(b) - 2 * lcs_len


def test_exact_op_list():
    # Verify the exact tie-breaking behavior for deterministic results.
    # Case 1: a = ["A", "B"], b = ["B", "A"]
    # LCS is 1. Under the DP rules:
    # We should delete 'A', keep 'B', insert 'A'.
    # This places deletion BEFORE insertion.
    ops1 = mydiff.diff_lines(["A", "B"], ["B", "A"])
    assert ops1 == [("-", "A"), ("=", "B"), ("+", "A")]

    # Case 2: a = ["A"], b = ["B"]
    # LCS is 0. Deletion '-' comes before insertion '+'.
    ops2 = mydiff.diff_lines(["A"], ["B"])
    assert ops2 == [("-", "A"), ("+", "B")]

    # Case 3: Empty sequences
    assert mydiff.diff_lines([], []) == []


def test_reconstruction():
    test_cases = [
        (["X", "Y", "Z"], ["A", "Y", "B"]),
        (["A", "B", "C", "D"], ["B", "D", "E"]),
        (["hello", "world"], ["world", "hello"]),
    ]
    for a, b in test_cases:
        ops = mydiff.diff_lines(a, b)

        # "=" and "-" recreate a
        recreated_a = [op[1] for op in ops if op[0] in ("=", "-")]
        assert recreated_a == a

        # "=" and "+" recreate b
        recreated_b = [op[1] for op in ops if op[0] in ("=", "+")]
        assert recreated_b == b


def test_roundtrip_and_inverse():
    test_cases = [
        ([], []),
        (["A"], []),
        ([], ["B"]),
        (["A", "B", "C"], ["A", "B", "C"]),  # Identical
        (["A", "B", "C"], ["X", "Y", "Z"]),  # Complete replacement
        (["A"], ["A", "B"]),  # Single insert
        (["A", "B"], ["A"]),  # Single delete
        (["A", "B", "A"], ["B", "A", "B"]),  # Duplicates and swaps
    ]
    for a, b in test_cases:
        ops = mydiff.diff_lines(a, b)

        # Forward patch
        patched_b = mypatch.apply_ops(a, ops)
        assert patched_b == b

        # Inverse patch
        inv_ops = mypatch.invert_ops(ops)
        patched_a = mypatch.apply_ops(b, inv_ops)
        assert patched_a == a


def test_patch_error_mismatch():
    # 1. Mismatch on keeping/deleting
    # ops expects "A" to be kept but we pass ["B"]
    ops = [("=", "A")]
    try:
        mypatch.apply_ops(["B"], ops)
        raise AssertionError("Should have raised PatchError")
    except mypatch.PatchError as e:
        assert "0" in str(e), f"Expected 0-based op index '0' in message: {e}"

    # 2. Mismatch on deletion
    ops = [("-", "A")]
    try:
        mypatch.apply_ops(["B"], ops)
        raise AssertionError("Should have raised PatchError")
    except mypatch.PatchError as e:
        assert "0" in str(e), f"Expected 0-based op index '0' in message: {e}"

    # 3. Invalid tag
    ops = [("?", "A")]
    try:
        mypatch.apply_ops(["A"], ops)
        raise AssertionError("Should have raised PatchError")
    except mypatch.PatchError as e:
        assert "0" in str(e), f"Expected 0-based op index '0' in message: {e}"


def test_patch_error_leftover_and_short():
    # 1. Unexpected end of input when expecting '='
    ops = [("=", "A")]
    try:
        mypatch.apply_ops([], ops)
        raise AssertionError("Should have raised PatchError")
    except mypatch.PatchError as e:
        assert "0" in str(e), f"Expected 0-based op index '0' in message: {e}"

    # 2. Unexpected end of input when expecting '-'
    ops = [("=", "A"), ("-", "B")]
    try:
        mypatch.apply_ops(["A"], ops)
        raise AssertionError("Should have raised PatchError")
    except mypatch.PatchError as e:
        assert "1" in str(e), f"Expected 0-based op index '1' in message: {e}"

    # 3. Leftover lines at end of ops
    ops = [("=", "A")]
    try:
        mypatch.apply_ops(["A", "B"], ops)
        raise AssertionError("Should have raised PatchError")
    except mypatch.PatchError as e:
        assert "1" in str(e), f"Expected index '1' (len(ops)) in message: {e}"


def test_stats():
    ops = [("=", "A"), ("-", "B"), ("+", "C"), ("=", "D"), ("+", "E")]
    res = roundtrip.stats(ops)
    assert res == {"same": 2, "removed": 1, "added": 2}

    assert roundtrip.stats([]) == {"same": 0, "removed": 0, "added": 0}


def test_summarize():
    assert roundtrip.summarize(["A", "B"], ["B", "A"]) == "+1 -1 =1"
    assert roundtrip.summarize(["A"], ["B"]) == "+1 -1 =0"
    assert roundtrip.summarize([], []) == "+0 -0 =0"


def test_verify():
    # Valid roundtrips
    assert roundtrip.verify(["A", "B"], ["B", "A"]) is True
    assert roundtrip.verify([], []) is True
    assert roundtrip.verify(["apple", "banana"], ["apple", "cherry"]) is True
