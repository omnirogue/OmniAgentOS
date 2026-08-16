"""Tests for delegation cost-model in swarm planner brief construction.

Verifies that the delegation cost-model decision block:
- Contains explicit veto rules (inter-agent dependency, overlapping owned_paths, shared state)
- Contains the real configured width value
- Is rendered in the planner prompt BEFORE rules section
"""


from omniagentos.swarm.planner import (
    TARGET_N_HARD_CEILING,
    TARGET_N_MAX,
    _build_delegation_costmodel_block,
    _plan_prompt,
    _target_cap,
)


class TestDelegationCostmodelBlock:
    """Tests for _build_delegation_costmodel_block function."""

    def test_block_contains_veto_rules(self) -> None:
        """Verify the cost-model block contains all HARD VETO rules."""
        block = _build_delegation_costmodel_block(target_width=5)

        # Check for all three hard veto categories
        assert "HARD VETOES" in block
        assert "Inter-agent task dependency" in block
        assert "Overlapping owned_paths" in block
        assert "Shared critical state" in block

    def test_block_contains_benefit_cost_sections(self) -> None:
        """Verify the block explains BENEFIT and COST of delegation."""
        block = _build_delegation_costmodel_block(target_width=5)

        # BENEFIT section
        assert "BENEFIT of delegation" in block
        assert "parallel wall-clock" in block
        assert "Specialist capability isolation" in block
        assert "Context isolation" in block

        # COST section
        assert "COST of delegation" in block
        assert "Agent startup overhead" in block
        assert "Duplicate discovery" in block
        assert "Coordination overhead" in block
        assert "State-conflict risk" in block

    def test_block_contains_dispatch_protocol(self) -> None:
        """Verify the block includes the smallest-useful-batch protocol."""
        block = _build_delegation_costmodel_block(target_width=5)

        assert "Dispatch protocol" in block
        assert "SMALLEST useful batch" in block
        assert "Wait for all tasks in the batch" in block
        assert "Re-evaluate remaining work" in block
        assert "launch the next batch" in block

    def test_block_contains_real_width_value(self) -> None:
        """Verify the block includes the actual configured width value."""
        for width in [2, 3, 5, 10, 20]:
            block = _build_delegation_costmodel_block(target_width=width)
            assert f"real configured width: {width}" in block
            assert f"more than {width} parallel agents" in block

    def test_block_respects_hard_ceiling(self) -> None:
        """Verify widths never exceed TARGET_N_HARD_CEILING."""
        block = _build_delegation_costmodel_block(target_width=TARGET_N_HARD_CEILING)
        assert f"real configured width: {TARGET_N_HARD_CEILING}" in block

    def test_block_minimum_width(self) -> None:
        """Verify block handles minimum width values."""
        block = _build_delegation_costmodel_block(target_width=1)
        assert "real configured width: 1" in block


class TestPlanPromptIntegration:
    """Tests verifying cost-model block is integrated into planner prompt."""

    def test_prompt_includes_costmodel_block(self) -> None:
        """Verify _plan_prompt includes the cost-model block."""
        prompt = _plan_prompt(
            goal="Implement feature X",
            assumptions=[],
            lessons="",
            target_width=5,
        )

        assert "DELEGATION COST-MODEL" in prompt
        assert "HARD VETOES" in prompt
        assert "Dispatch protocol" in prompt

    def test_prompt_uses_provided_target_width(self) -> None:
        """Verify _plan_prompt respects explicit target_width parameter."""
        for width in [2, 5, 10]:
            prompt = _plan_prompt(
                goal="Test goal",
                assumptions=[],
                lessons="",
                target_width=width,
            )
            assert f"real configured width: {width}" in prompt

    def test_prompt_defaults_to_target_cap_when_width_omitted(self) -> None:
        """Verify _plan_prompt defaults to _target_cap() when target_width is None."""
        prompt = _plan_prompt(
            goal="Test goal",
            assumptions=[],
            lessons="",
            target_width=None,
        )

        cap = _target_cap()
        assert f"real configured width: {cap}" in prompt
        assert cap == min(TARGET_N_MAX, TARGET_N_HARD_CEILING)

    def test_prompt_veto_rules_before_rules_section(self) -> None:
        """Verify cost-model HARD VETOES appear before task Rules."""
        prompt = _plan_prompt(
            goal="Test goal",
            assumptions=[],
            lessons="",
            target_width=5,
        )

        costmodel_pos = prompt.find("HARD VETOES")
        rules_pos = prompt.find("Rules:")

        assert costmodel_pos > 0, "Cost-model block not found"
        assert rules_pos > 0, "Rules section not found"
        assert costmodel_pos < rules_pos, (
            "Cost-model HARD VETOES should appear BEFORE Rules section"
        )

    def test_prompt_contains_all_veto_categories_in_order(self) -> None:
        """Verify all three veto categories are in the prompt and ordered."""
        prompt = _plan_prompt(
            goal="Test goal",
            assumptions=[],
            lessons="",
            target_width=5,
        )

        dep_pos = prompt.find("Inter-agent task dependency")
        owned_paths_pos = prompt.find("Overlapping owned_paths")
        state_pos = prompt.find("Shared critical state")

        assert dep_pos > 0, "Inter-agent dependency veto not found"
        assert owned_paths_pos > 0, "Overlapping owned_paths veto not found"
        assert state_pos > 0, "Shared critical state veto not found"

        # Verify they appear in the expected order
        assert dep_pos < owned_paths_pos < state_pos


class TestCostmodelPromptConsistency:
    """Tests verifying prompt and enforcer (config/width) never disagree."""

    def test_prompt_width_matches_target_cap(self) -> None:
        """Verify rendered prompt width always matches _target_cap()."""
        prompt = _plan_prompt(
            goal="Test",
            assumptions=[],
            lessons="",
            target_width=_target_cap(),
        )

        actual_cap = _target_cap()
        assert f"real configured width: {actual_cap}" in prompt

    def test_costmodel_block_is_deterministic(self) -> None:
        """Verify the block is deterministic for same width input."""
        width = 5
        block1 = _build_delegation_costmodel_block(target_width=width)
        block2 = _build_delegation_costmodel_block(target_width=width)

        assert block1 == block2, "Cost-model block should be deterministic"

    def test_veto_rules_are_stable(self) -> None:
        """Verify HARD VETO rules don't change across widths."""
        blocks = [
            _build_delegation_costmodel_block(target_width=w)
            for w in [2, 5, 10, 20]
        ]

        # All blocks should contain the same veto rule descriptions
        veto_text = "Inter-agent task dependency"
        assert all(veto_text in b for b in blocks), (
            "All blocks should contain stable veto rule descriptions"
        )
