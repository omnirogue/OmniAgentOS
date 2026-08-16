from __future__ import annotations

from omniagentos.lab.curator.prompt import curation_prompt


def test_curation_prompt_states_the_binding_guardrails() -> None:
    prompt = curation_prompt({})
    assert "RECOMMEND" in prompt
    assert "surfaces.promote()" in prompt
    assert "champions table" in prompt
    assert "not a mutable surface" in prompt
    assert "SANITIZED store" in prompt
    assert "held-out" in prompt


def test_curation_prompt_embeds_the_given_context_as_json() -> None:
    context = {"subjects": ["headline-copy"], "leaderboard": {"headline-copy": []}}
    prompt = curation_prompt(context)
    assert "headline-copy" in prompt
    assert '"subjects"' in prompt


def test_curation_prompt_is_pure_and_deterministic() -> None:
    context = {"a": 1, "b": [1, 2, 3]}
    assert curation_prompt(context) == curation_prompt(context)


def test_curation_prompt_handles_non_json_native_values_via_str_fallback() -> None:
    class Weird:
        def __str__(self) -> str:
            return "weird-value"

    prompt = curation_prompt({"thing": Weird()})
    assert "weird-value" in prompt
