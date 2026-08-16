from __future__ import annotations

from omniagentos.lab.eval.blind import build_blind_pairs, unlinkable_token


def test_unlinkable_token_is_high_entropy_and_unique() -> None:
    tokens = {unlinkable_token() for _ in range(500)}
    assert len(tokens) == 500  # no collisions
    assert all(len(token) >= 24 for token in tokens)


def test_unlinkable_token_never_embeds_the_arm_or_case_id() -> None:
    # A naive "blind" scheme might hash/derive the token from case_id+arm
    # (e.g. sha256(f"{case_id}{arm}")) — that LOOKS random but is reversible
    # via a known-plaintext dictionary attack over the small, enumerable set
    # of real case ids/arms. secrets.token_urlsafe carries no such structure.
    case_id, arm = "evc_abcdef0123456789abcd", "challenger"
    for _ in range(50):
        token = unlinkable_token()
        assert case_id not in token
        assert arm not in token
        assert "champion" not in token


def test_build_blind_pairs_strips_arm_and_is_recoverable_via_token_map() -> None:
    case_outputs = {
        "evc_1": {
            "champion": {"text": "champion answer 1"},
            "challenger": {"text": "challenger answer 1"},
        },
        "evc_2": {
            "champion": {"text": "champion answer 2"},
            "challenger": {"text": "challenger answer 2"},
        },
    }
    pairs, token_map, seed = build_blind_pairs(case_outputs, seed=1234)

    assert len(pairs) == 4
    assert len(token_map) == 4
    # BlindPair structurally carries no `arm` key at all.
    for pair in pairs:
        assert set(pair.keys()) == {"case_id", "blind_token", "output"}

    # every pair is recoverable via the (evaluator-only) token map, and the
    # recovered arm's output round-trips exactly.
    for pair in pairs:
        recovered_case_id, arm = token_map[pair["blind_token"]]
        assert recovered_case_id == pair["case_id"]
        assert pair["output"] == case_outputs[pair["case_id"]][arm]

    assert seed == 1234


def test_build_blind_pairs_tokens_are_all_distinct() -> None:
    case_outputs = {
        f"evc_{i}": {"champion": {"text": str(i)}, "challenger": {"text": str(i)}}
        for i in range(20)
    }
    pairs, token_map, _ = build_blind_pairs(case_outputs)
    tokens = [pair["blind_token"] for pair in pairs]
    assert len(set(tokens)) == len(tokens) == 40
    assert len(token_map) == 40


def test_build_blind_pairs_records_seed_and_is_order_reproducible_by_seed() -> None:
    case_outputs = {
        f"evc_{i}": {"champion": {"text": str(i)}, "challenger": {"text": str(i)}}
        for i in range(10)
    }
    pairs_a, _, seed_a = build_blind_pairs(case_outputs, seed=99)
    pairs_b, _, seed_b = build_blind_pairs(case_outputs, seed=99)

    assert seed_a == seed_b == 99
    # presentation ORDER is reproducible from the seed...
    order_a = [
        (pair["case_id"], case_outputs[pair["case_id"]]["champion"] == pair["output"])
        for pair in pairs_a
    ]
    order_b = [
        (pair["case_id"], case_outputs[pair["case_id"]]["champion"] == pair["output"])
        for pair in pairs_b
    ]
    assert order_a == order_b
    # ...but the blind_token VALUES are never reproducible from the seed —
    # they are a fresh `secrets` draw every call, by design (HD-015).
    tokens_a = [pair["blind_token"] for pair in pairs_a]
    tokens_b = [pair["blind_token"] for pair in pairs_b]
    assert tokens_a != tokens_b
    assert set(tokens_a).isdisjoint(tokens_b)


def test_build_blind_pairs_without_a_seed_still_returns_a_usable_seed() -> None:
    case_outputs = {"evc_1": {"champion": {"text": "a"}, "challenger": {"text": "b"}}}
    _, _, seed = build_blind_pairs(case_outputs)
    assert isinstance(seed, int)
