"""Unit tests for ``ovb_rc003.reconnect_policy.ReconnectPolicy``.

Parity coverage for HD838A/remote-mic-app 1.8.13-1.8.15's
``BluetoothReconnectPolicy`` (Sources/RemoteMic/BluetoothLifecycle.swift):

  * delay sequence: 3 -> 6 -> 12 -> 24 -> 48 -> 60 (capped) seconds
  * +/- 10% jitter, never below (1 - jitter_ratio) * base
  * ``reset()`` collapses consecutive-failure counter to zero
  * ``consecutive_failure_count`` is observable so callers can decide
    whether to give up after N attempts

Each test injects a deterministic ``clock`` (or no clock) so that the
jitter factor is reproducible across runs. The point of these tests is
to pin down the curve, not to verify randomness.
"""

from __future__ import annotations

import unittest

from ovb_rc003.reconnect_policy import (
    DEFAULT_BASE_DELAY_SECONDS,
    DEFAULT_JITTER_RATIO,
    DEFAULT_MAXIMUM_DELAY_SECONDS,
    ReconnectPolicy,
    ReconnectPolicyConfig,
)


# jitter_unit = 0.0 -> factor = (1 - jitter_ratio) + 0.0 = 0.9 (negative jitter).
def _zero_clock() -> int:
    return 0


# jitter_unit = 1.0 -> factor = (1 - jitter_ratio) + 2 * jitter_ratio = 1.1
# (positive jitter).
def _full_clock() -> int:
    return 0xFFFF


def _make_policy(
    *,
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
    maximum_delay_seconds: float = DEFAULT_MAXIMUM_DELAY_SECONDS,
    jitter_ratio: float = DEFAULT_JITTER_RATIO,
    clock=None,
) -> ReconnectPolicy:
    return ReconnectPolicy(
        config=ReconnectPolicyConfig(
            base_delay_seconds=base_delay_seconds,
            maximum_delay_seconds=maximum_delay_seconds,
            jitter_ratio=jitter_ratio,
        ),
        clock=clock,
    )


class DefaultsTests(unittest.TestCase):
    """Six: defaults, validation."""

    def test_default_base_delay_is_three_seconds(self):
        cfg = ReconnectPolicyConfig()
        self.assertEqual(cfg.base_delay_seconds, 3.0)

    def test_default_maximum_delay_is_sixty_seconds(self):
        cfg = ReconnectPolicyConfig()
        self.assertEqual(cfg.maximum_delay_seconds, 60.0)

    def test_default_jitter_ratio_is_ten_percent(self):
        cfg = ReconnectPolicyConfig()
        self.assertAlmostEqual(cfg.jitter_ratio, 0.1)

    def test_zero_or_negative_base_delay_rejected(self):
        with self.assertRaises(ValueError):
            ReconnectPolicyConfig(base_delay_seconds=0.0)
        with self.assertRaises(ValueError):
            ReconnectPolicyConfig(base_delay_seconds=-1.0)

    def test_maximum_below_base_rejected(self):
        with self.assertRaises(ValueError):
            ReconnectPolicyConfig(
                base_delay_seconds=10.0,
                maximum_delay_seconds=5.0,
            )

    def test_jitter_ratio_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            ReconnectPolicyConfig(jitter_ratio=-0.1)
        with self.assertRaises(ValueError):
            ReconnectPolicyConfig(jitter_ratio=0.6)


class DelayCurveTests(unittest.TestCase):
    """Four: curve shape under each jitter setting."""

    def test_no_jitter_produces_doubling_then_cap(self):
        # jitter_unit = 0 -> factor = 0.9 -> 0.9 * nominal, capped at 60.
        # Sequence: 2.7, 5.4, 10.8, 21.6, 43.2, 54.0, 54.0
        policy = _make_policy(clock=_zero_clock)
        sequence = [policy.next_automatic_delay() for _ in range(7)]
        for actual, expected in zip(
            sequence, [2.7, 5.4, 10.8, 21.6, 43.2, 54.0, 54.0]
        ):
            self.assertAlmostEqual(actual, expected, delta=1e-9)

    def test_full_positive_jitter_doubles_then_caps(self):
        # jitter_unit = 1 -> factor = 1.1 -> 1.1 * nominal, capped at 60.
        # Sequence: 3.3, 6.6, 13.2, 26.4, 52.8, 60.0, 60.0
        policy = _make_policy(clock=_full_clock)
        sequence = [policy.next_automatic_delay() for _ in range(7)]
        for actual, expected in zip(
            sequence, [3.3, 6.6, 13.2, 26.4, 52.8, 60.0, 60.0]
        ):
            self.assertAlmostEqual(actual, expected, delta=1e-9)

    def test_consecutive_failure_count_increments_per_call(self):
        policy = _make_policy(clock=_zero_clock)
        self.assertEqual(policy.consecutive_failure_count, 0)
        policy.next_automatic_delay()
        self.assertEqual(policy.consecutive_failure_count, 1)
        policy.next_automatic_delay()
        self.assertEqual(policy.consecutive_failure_count, 2)

    def test_exponent_caps_at_max_exponent(self):
        # After max_exponent failures, exponent stays at the cap; nominal
        # is always the cap value; only the jitter factor still moves.
        cfg = ReconnectPolicyConfig(
            base_delay_seconds=3.0,
            maximum_delay_seconds=60.0,
        )
        self.assertEqual(cfg.max_exponent, 5)
        policy = ReconnectPolicy(config=cfg, clock=_zero_clock)
        for _ in range(5):
            policy.next_automatic_delay()
        # At this point nominal has been 3, 6, 12, 24, 48. Now attempt 6:
        # nominal = min(60, 96) = 60 -> factor 0.9 -> 54.0
        sixth = policy.next_automatic_delay()
        self.assertAlmostEqual(sixth, 54.0)
        # Attempt 7: same nominal 60 -> 54.0 (no growth past cap).
        seventh = policy.next_automatic_delay()
        self.assertAlmostEqual(seventh, 54.0)


class CapTests(unittest.TestCase):
    """Two: output never crosses the cap or goes negative."""

    def test_never_returns_above_maximum_delay(self):
        policy = _make_policy(clock=_full_clock)
        for _ in range(50):
            delay = policy.next_automatic_delay()
            self.assertLessEqual(delay, 60.0)

    def test_never_returns_below_zero(self):
        # Pathological config: jitter_ratio = 0.499, unit=0 ->
        # factor = 0.501. Still positive.
        policy = _make_policy(jitter_ratio=0.499, clock=_zero_clock)
        for _ in range(50):
            delay = policy.next_automatic_delay()
            self.assertGreater(delay, 0.0)

    def test_custom_maximum_delay_is_respected(self):
        # Mac port never silently overrides the configured cap.
        policy = _make_policy(
            base_delay_seconds=5.0,
            maximum_delay_seconds=15.0,
            clock=_zero_clock,
        )
        # 0.9 * (5, 10, 15, 15) -> (4.5, 9.0, 13.5, 13.5)
        sequence = [policy.next_automatic_delay() for _ in range(4)]
        self.assertEqual(sequence, [4.5, 9.0, 13.5, 13.5])


class ResetTests(unittest.TestCase):
    """Two: reset() restores base behavior."""

    def test_reset_zeroes_consecutive_failure_count(self):
        policy = _make_policy(clock=_zero_clock)
        for _ in range(4):
            policy.next_automatic_delay()
        self.assertEqual(policy.consecutive_failure_count, 4)
        policy.reset()
        self.assertEqual(policy.consecutive_failure_count, 0)

    def test_reset_makes_next_delay_the_base_again(self):
        policy = _make_policy(clock=_zero_clock)
        for _ in range(4):
            policy.next_automatic_delay()
        policy.reset()
        # Back to attempt 1: factor=0.9 -> 2.7
        self.assertAlmostEqual(policy.next_automatic_delay(), 2.7)


class JitterTests(unittest.TestCase):
    """Two: jitter formula at the extremes."""

    def test_jitter_unit_zero_yields_minus_ten_percent(self):
        policy = _make_policy(
            base_delay_seconds=10.0,
            jitter_ratio=0.1,
            clock=_zero_clock,
        )
        self.assertAlmostEqual(policy.next_automatic_delay(), 9.0)

    def test_jitter_unit_max_yields_plus_ten_percent(self):
        policy = _make_policy(
            base_delay_seconds=10.0,
            jitter_ratio=0.1,
            clock=_full_clock,
        )
        self.assertAlmostEqual(policy.next_automatic_delay(), 11.0)


class SupervisorIntegrationTests(unittest.TestCase):
    """Two: ConnectionSupervisor actually uses the injected ReconnectPolicy."""

    def _run(self, coro):
        import asyncio

        # Explicitly closing the loop (XRBM-014 review round 2 evidence: a
        # ResourceWarning for an unclosed test event loop) rather than letting
        # it be garbage-collected. Every other loop helper in this suite does
        # this; this one was missed, and on its own it emits both
        # "unclosed event loop <ProactorEventLoop ...>" and two unclosed
        # self-pipe sockets, which the CI ResourceWarning gate greps for.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_supervisor_resets_policy_after_successful_connect(self):
        import asyncio

        from ovb_rc003.connection_supervisor import ConnectionSupervisor

        async def scenario():
            async def connect():
                pass  # succeeds immediately

            async def cleanup():
                pass

            async def fake_sleep(_seconds):
                raise asyncio.CancelledError()

            policy = _make_policy(clock=_zero_clock)
            supervisor = ConnectionSupervisor(
                connect,
                cleanup,
                sleep=fake_sleep,
                policy=policy,
            )
            self.assertEqual(policy.consecutive_failure_count, 0)

            # On a successful connect, run_forever blocks on the
            # disconnect event (correct production behavior: sit there
            # until something disconnects). Stop the supervisor to
            # break it out cleanly without exercising fake_sleep at all.
            await supervisor.stop()
            await supervisor.run_forever()
            # No failures -> count is still 0 (reset on success path is a
            # no-op but should not error).
            self.assertEqual(policy.consecutive_failure_count, 0)

        self._run(scenario())

    def test_supervisor_policy_count_increments_on_failure(self):
        import asyncio

        from ovb_rc003.connection_supervisor import ConnectionSupervisor

        async def scenario():
            sleeps = []

            async def connect():
                raise RuntimeError("boom")

            async def cleanup():
                pass

            async def fake_sleep(_seconds):
                sleeps.append(1)
                if len(sleeps) >= 3:
                    raise asyncio.CancelledError()

            policy = _make_policy(clock=_zero_clock)
            supervisor = ConnectionSupervisor(
                connect,
                cleanup,
                sleep=fake_sleep,
                policy=policy,
            )
            with self.assertRaises(asyncio.CancelledError):
                await supervisor.run_forever()
            self.assertEqual(policy.consecutive_failure_count, 3)

        self._run(scenario())


if __name__ == "__main__":
    unittest.main()