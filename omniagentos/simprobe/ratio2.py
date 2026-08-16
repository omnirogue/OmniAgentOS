def safe_share(part: float, whole: float) -> float | None:
    """Return ``part / whole`` safely.

    A zero denominator must never yield a favourable-looking number; callers
    must handle ``None`` explicitly.
    """
    if whole == 0:
        return None
    return part / whole
