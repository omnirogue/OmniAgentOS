from omniagentos.simprobe.ratio2 import safe_share


def test_normal_division():
    assert safe_share(1, 4) == 0.25
    assert safe_share(3, 4) == 0.75
    assert safe_share(-1, 2) == -0.5
    assert safe_share(2.5, 5.0) == 0.5


def test_zero_part_returns_zero():
    assert safe_share(0, 5) == 0.0
    assert safe_share(0, 5) is not None


def test_zero_whole_returns_none():
    for part, whole in ((1, 0), (0, 0), (1, 0.0), (1, -0.0)):
        result = safe_share(part, whole)
        assert result is None
        if result is not None:
            assert result != 1.0 and result != 0.0
