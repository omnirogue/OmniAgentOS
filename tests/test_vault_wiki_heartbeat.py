"""Tests for omniagentos.vault_wiki: heartbeat pump around blocking LLM calls.

Test coverage:
- (a) Heartbeat pump emits multiple beats during a long operation
- (b) Heartbeat pump is cancelled after context exits (success path)
- (c) Heartbeat pump is cancelled after context exits (exception path)
- (d) Beats are side-effect-free (timestamps only)
- (e) Pump survives transient beat function errors
"""

from __future__ import annotations

import time

import pytest

from omniagentos.vault_wiki import with_heartbeat


class TestHeartbeatEmission:
    """Test (a): Heartbeat pump emits multiple beats on a fixed cadence."""

    def test_emits_multiple_beats_during_long_operation(self) -> None:
        """During a 0.3s operation at 1/0.1Hz, at least 2 beats should be emitted."""
        beats: list[float] = []

        def record_beat() -> None:
            beats.append(time.time())

        with with_heartbeat(record_beat, interval_s=0.1):
            time.sleep(0.25)

        # At 0.1s intervals over 0.25s, we expect at least 2 beats
        # (one at ~0.1s, one at ~0.2s; the third would be at ~0.3s but we exit at 0.25s)
        assert len(beats) >= 2, f"Expected at least 2 beats, got {len(beats)}"

    def test_beat_timing_respects_interval(self) -> None:
        """Beats should be spaced roughly at the requested interval."""
        beats: list[float] = []
        start = time.time()

        def record_beat() -> None:
            beats.append(time.time() - start)

        interval_s = 0.05
        with with_heartbeat(record_beat, interval_s=interval_s):
            time.sleep(0.15)

        # Expect at least 2 beats
        assert len(beats) >= 2
        # Check spacing: consecutive beats should be roughly interval_s apart
        if len(beats) >= 2:
            for i in range(1, len(beats)):
                spacing = beats[i] - beats[i - 1]
                # Allow 50% tolerance on spacing (timer imprecision)
                assert spacing >= interval_s * 0.5, f"Beat {i} spacing too tight: {spacing}s"

    def test_more_beats_emitted_in_longer_operation(self) -> None:
        """A longer operation should emit proportionally more beats."""
        beats_short: list[float] = []
        beats_long: list[float] = []

        def record_beat_short() -> None:
            beats_short.append(time.time())

        def record_beat_long() -> None:
            beats_long.append(time.time())

        interval_s = 0.05
        with with_heartbeat(record_beat_short, interval_s=interval_s):
            time.sleep(0.1)

        with with_heartbeat(record_beat_long, interval_s=interval_s):
            time.sleep(0.2)

        # Longer operation should have more beats
        assert len(beats_long) >= len(beats_short) + 1


class TestHeartbeatCancellation:
    """Test (b) & (c): Pump is cancelled after context exits (success and exception)."""

    def test_pump_is_cancelled_on_success(self) -> None:
        """After the context exits successfully, the pump should be cancelled."""
        beats: list[float] = []

        def record_beat() -> None:
            beats.append(time.time())

        # We can't directly access the timer, but we can verify that no new beats
        # are added after the context exits (by checking the heartbeat list is stable)
        with with_heartbeat(record_beat, interval_s=0.05):
            time.sleep(0.1)

        beat_count_at_exit = len(beats)
        # Wait a bit longer to ensure no additional beats are added
        time.sleep(0.1)
        beat_count_after = len(beats)

        # If the pump were still running, we'd see more beats added
        # Allow for timing slack (one additional beat might sneak in due to thread scheduling)
        assert beat_count_after <= beat_count_at_exit + 1, (
            f"Pump not cancelled: beats at exit={beat_count_at_exit}, after wait={beat_count_after}"
        )

    def test_pump_is_cancelled_on_exception(self) -> None:
        """After an exception in the context, the pump should still be cancelled."""
        beats: list[float] = []

        def record_beat() -> None:
            beats.append(time.time())

        try:
            with with_heartbeat(record_beat, interval_s=0.05):
                time.sleep(0.1)
                raise ValueError("Test exception")
        except ValueError:
            pass

        beat_count_at_exception = len(beats)
        # Wait to ensure pump doesn't continue after exception
        time.sleep(0.1)
        beat_count_after = len(beats)

        # Pump should be cancelled despite the exception
        assert beat_count_after <= beat_count_at_exception + 1, (
            f"Pump not cancelled after exception: "
            f"beats at exception={beat_count_at_exception}, after wait={beat_count_after}"
        )

    def test_exception_in_context_does_not_suppress_original_exception(self) -> None:
        """The context manager should re-raise the original exception."""
        def record_beat() -> None:
            pass

        with pytest.raises(RuntimeError, match="Original error"):
            with with_heartbeat(record_beat, interval_s=0.1):
                raise RuntimeError("Original error")


class TestHeartbeatSideEffects:
    """Test (d): Beats are side-effect-free (cheap, observable only via side channel)."""

    def test_beat_function_is_called_repeatedly(self) -> None:
        """The beat function should be called on each heartbeat."""
        call_count = [0]

        def counting_beat() -> None:
            call_count[0] += 1

        with with_heartbeat(counting_beat, interval_s=0.05):
            time.sleep(0.12)

        # Over 0.12s at 0.05s intervals, we expect at least 2 beats
        assert call_count[0] >= 2, f"Expected at least 2 beats, got {call_count[0]}"

    def test_timestamps_are_recorded_side_effect_free(self) -> None:
        """Recording timestamps in the beat function is a valid side-effect-free pattern."""
        beats: list[float] = []

        def record_timestamp() -> None:
            beats.append(time.time())

        start = time.time()
        with with_heartbeat(record_timestamp, interval_s=0.05):
            time.sleep(0.12)

        # Beats should have been recorded
        assert len(beats) >= 2
        # All timestamps should be within the context execution window
        for beat_time in beats:
            assert beat_time >= start


class TestHeartbeatErrorResilience:
    """Test (e): Pump survives transient beat function errors."""

    def test_pump_survives_beat_function_exception(self) -> None:
        """If the beat function raises, the pump should continue."""
        beats_recorded = [0]

        def failing_beat() -> None:
            beats_recorded[0] += 1
            if beats_recorded[0] % 2 == 1:
                # Fail on odd beats (1st, 3rd, 5th...)
                raise RuntimeError("Transient beat error")
            # Succeed on even beats (2nd, 4th, 6th...)

        # This should not raise despite transient errors in the beat function
        with with_heartbeat(failing_beat, interval_s=0.05):
            time.sleep(0.15)

        # We should have had multiple beat calls despite the errors
        assert beats_recorded[0] >= 2, f"Expected at least 2 beat calls, got {beats_recorded[0]}"

    def test_pump_continues_after_exception_in_beat(self) -> None:
        """The pump should reschedule even after the beat function raises."""
        call_count = [0]

        def always_failing_beat() -> None:
            call_count[0] += 1
            raise RuntimeError("Beat error")

        with with_heartbeat(always_failing_beat, interval_s=0.05):
            time.sleep(0.12)

        # Even though all beats fail, multiple calls should have been attempted
        assert call_count[0] >= 2, (
            f"Expected multiple beat attempts despite errors, got {call_count[0]}"
        )


class TestHeartbeatWithMockedAdapter:
    """Integration-like test: simulate heartbeat pump in presence of a mocked LLM call."""

    def test_heartbeat_pump_coexists_with_simulated_blocking_call(self) -> None:
        """Verify the heartbeat pump pattern works around a simulated blocking call."""
        beats: list[float] = []

        def beat() -> None:
            beats.append(time.time())

        def simulated_blocking_call() -> str:
            """Simulates a long LLM call (0.2s)."""
            time.sleep(0.2)
            return "response"

        start = time.time()
        with with_heartbeat(beat, interval_s=0.05):
            result = simulated_blocking_call()
        elapsed = time.time() - start

        assert result == "response"
        # Should have received multiple beats during the 0.2s call
        assert len(beats) >= 3, f"Expected at least 3 beats during 0.2s call, got {len(beats)}"
        # Total elapsed should be roughly the call duration (plus overhead)
        assert elapsed >= 0.2, f"Expected at least 0.2s elapsed, got {elapsed}"

    def test_heartbeat_pump_handles_simulated_call_exception(self) -> None:
        """Verify heartbeat pump is cancelled even if the blocking call raises."""
        beats: list[float] = []

        def beat() -> None:
            beats.append(time.time())

        def simulated_failing_call() -> None:
            """Simulates a long LLM call that fails."""
            time.sleep(0.15)
            raise ConnectionError("LLM connection failed")

        try:
            with with_heartbeat(beat, interval_s=0.05):
                simulated_failing_call()
        except ConnectionError:
            pass

        beat_count_at_exception = len(beats)
        # Wait to verify pump is cancelled despite the exception
        time.sleep(0.1)
        beat_count_after = len(beats)

        assert beat_count_at_exception >= 2, "Should have recorded beats before exception"
        assert beat_count_after <= beat_count_at_exception + 1, (
            "Pump not cancelled after exception"
        )


class TestHeartbeatEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_very_short_interval(self) -> None:
        """Heartbeat should work with a very short interval."""
        beats: list[float] = []

        def record_beat() -> None:
            beats.append(time.time())

        with with_heartbeat(record_beat, interval_s=0.01):
            time.sleep(0.05)

        # At 0.01s intervals over 0.05s, expect at least 4 beats
        assert len(beats) >= 4

    def test_very_short_operation_still_gets_beats(self) -> None:
        """Even a very short operation should emit at least one beat."""
        beats: list[float] = []

        def record_beat() -> None:
            beats.append(time.time())

        with with_heartbeat(record_beat, interval_s=0.01):
            time.sleep(0.02)

        # Should have at least one beat
        assert len(beats) >= 1

    def test_no_operation_still_emits_beats_while_alive(self) -> None:
        """Beats should be emitted even if no real work is done inside the context."""
        beats: list[float] = []

        def record_beat() -> None:
            beats.append(time.time())

        with with_heartbeat(record_beat, interval_s=0.05):
            # Do nothing, just hold the context open
            time.sleep(0.12)

        # Should still emit beats on the cadence
        assert len(beats) >= 2
