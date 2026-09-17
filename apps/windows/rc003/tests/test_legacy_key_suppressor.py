import threading
import time
import unittest

from ovb_rc003 import legacy_key_suppressor_windows as suppressor


class LegacyKeySuppressorDecisionTests(unittest.TestCase):
    def test_right_alt_transform_event_has_one_physical_identity(self):
        target = suppressor.PhysicalKeyTarget(
            vk_code=0xA5,
            scan_code=0x38,
            extended=True,
            system_key=True,
        )

        down, down_message = suppressor.build_physical_key_event(target, True, 123)
        up, up_message = suppressor.build_physical_key_event(target, False, 124)

        self.assertEqual((down.vkCode, down.scanCode, down.flags, down.time), (0xA5, 0x38, suppressor.LLKHF_EXTENDED, 123))
        self.assertEqual((up.vkCode, up.scanCode, up.flags, up.time), (0xA5, 0x38, suppressor.LLKHF_EXTENDED | suppressor.LLKHF_UP, 124))
        self.assertEqual(down_message, suppressor.WM_SYSKEYDOWN)
        self.assertEqual(up_message, suppressor.WM_SYSKEYUP)

    def test_suppresses_configured_non_injected_vk(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        self.assertTrue(gate.should_suppress(0x74, 0))

    def test_does_not_suppress_sendinput_injected_vk(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        self.assertFalse(gate.should_suppress(0x74, suppressor.LLKHF_INJECTED))

    def test_physicalizes_only_marked_right_alt_event(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        event = suppressor.KBDLLHOOKSTRUCT(
            vkCode=0xA5,
            scanCode=0x38,
            flags=(
                suppressor.LLKHF_EXTENDED
                | suppressor.LLKHF_INJECTED
                | suppressor.LLKHF_LOWER_IL_INJECTED
            ),
            time=123,
            dwExtraInfo=suppressor.VOICE_EVENT_EXTRA_INFO,
        )

        self.assertTrue(gate.physicalize_injected_event(event))
        self.assertEqual(event.flags, suppressor.LLKHF_EXTENDED)
        self.assertEqual(event.dwExtraInfo, 0)

    def test_does_not_physicalize_unmarked_or_other_injected_events(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        for vk_code, extra_info in (
            (0xA5, 0),
            (0xA4, suppressor.VOICE_EVENT_EXTRA_INFO),
        ):
            event = suppressor.KBDLLHOOKSTRUCT(
                vkCode=vk_code,
                scanCode=0x38,
                flags=suppressor.LLKHF_EXTENDED | suppressor.LLKHF_INJECTED,
                time=123,
                dwExtraInfo=extra_info,
            )
            original = (int(event.flags), int(event.dwExtraInfo))
            self.assertFalse(gate.physicalize_injected_event(event))
            self.assertEqual((int(event.flags), int(event.dwExtraInfo)), original)

    def test_does_not_suppress_unconfigured_vk_codes(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        self.assertFalse(gate.should_suppress(0x5B, 0))  # VK_LWIN
        self.assertFalse(gate.should_suppress(0x48, 0))  # H

    def test_suppressed_physical_edge_is_forwarded_without_forwarding_injected_input(self):
        events = []
        gate = suppressor.LegacyKeySuppressor(
            {0x74}, on_key_event=lambda vk_code, is_pressed: events.append(
                (vk_code, is_pressed)
            )
        )

        self.assertTrue(gate.handle_key_event(0x74, 0, True))
        self.assertTrue(gate.handle_key_event(0x74, 0, False))
        self.assertFalse(gate.handle_key_event(0x74, suppressor.LLKHF_INJECTED, True))
        self.assertEqual(events, [(0x74, True), (0x74, False)])

    def test_armed_raw_input_edge_is_consumed_once_and_only_with_exact_identity(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        gate.arm_key_event(0x26, 0x48, True, True)

        self.assertFalse(gate.consume_armed_key_event(0x26, 0x48, False, True))
        self.assertTrue(gate.consume_armed_key_event(0x26, 0x48, True, True))
        self.assertFalse(gate.consume_armed_key_event(0x26, 0x48, True, True))

    def test_armed_five_is_left_to_the_dedicated_voice_suppressor(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        gate.arm_key_event(0x74, 0x3F, False, True)
        self.assertFalse(gate.consume_armed_key_event(0x74, 0x3F, False, True))


class LegacyKeySuppressorRaceTests(unittest.TestCase):
    """The low-level hook and the Raw Input thread race for the same physical
    press. The consumer must wait briefly for an arming edge that is in
    flight, without blocking ordinary keyboard input."""

    def test_consume_waits_for_an_arm_that_arrives_right_afterward(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        # The hook fires before the Raw Input thread delivers the same
        # physical press, so the arming edge lands a moment later. The
        # consumer must wait for it instead of releasing a double action.
        def raw_input_thread():
            import time

            time.sleep(0.01)
            gate.arm_key_event(0x26, 0x48, True, True)

        thread = threading.Thread(target=raw_input_thread)
        thread.start()
        try:
            matched = gate.consume_armed_key_event(
                0x26, 0x48, True, True, wait_seconds=0.200
            )
        finally:
            thread.join(timeout=2)
        self.assertTrue(matched)
        # The edge is consumed exactly once.
        self.assertFalse(gate.consume_armed_key_event(0x26, 0x48, True, True))

    def test_no_recent_arm_returns_immediately_without_blocking(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        # No RC003 press has been armed recently; the hook must pass an
        # unrelated physical key straight through after the short wait.
        self.assertFalse(gate.consume_armed_key_event(0x26, 0x48, True, True))

    def test_unrelated_key_does_not_wait_for_a_pending_arm(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        gate.arm_key_event(0x26, 0x48, True, True)
        # A different key passing through the hook must not be blocked or
        # consumed just because some RC003 edge is armed.
        self.assertFalse(gate.consume_armed_key_event(0x27, 0x49, True, True))

    def test_key_outside_rc003_set_passes_through_immediately(self):
        gate = suppressor.LegacyKeySuppressor(
            {0x74},
            rc003_vk_codes=frozenset({0x74, 0x26, 0x27, 0x25, 0x28}),
            consume_wait_seconds=10.0,
        )
        # Ordinary keyboard letters are not part of the RC003 surface, so the
        # hook must not wait (or block) for an arming edge at all.
        start = time.monotonic()
        self.assertFalse(gate.consume_armed_key_event(0x4E, 0x31, False, True))
        self.assertLess(time.monotonic() - start, 0.25)

    def test_rc003_key_waits_for_a_late_arming_edge_by_default(self):
        gate = suppressor.LegacyKeySuppressor(
            {0x74},
            rc003_vk_codes=frozenset({0x74, 0x26, 0x27, 0x25, 0x28}),
        )

        def raw_input_thread():
            time.sleep(0.03)
            gate.arm_key_event(0x26, 0x48, True, True)

        thread = threading.Thread(target=raw_input_thread)
        thread.start()
        try:
            matched = gate.consume_armed_key_event(0x26, 0x48, True, True)
        finally:
            thread.join(timeout=2)
        self.assertTrue(matched)


class LegacyKeySuppressorVoiceKeyWaitTests(unittest.TestCase):
    """The voice key is the one RC003 key that can never be armed, so it must
    never pay the arming wait either.

    The RC003 microphone key's physical edge is rewritten in place by the voice
    path, so nothing emits a matching Raw Input arming edge for it. Waiting the
    consume window for a match that can never arrive would add that whole
    window of latency to every press of the microphone key - the key the user
    holds while talking. ``_NEVER_ARMED_VK_CODES`` is what prevents that, and
    the exemption has to stay *narrow*: the other RC003 keys do get a real
    arming edge, and exempting them would bring back the double-action the
    window exists to prevent.
    """

    _RC003_KEYS = frozenset({0x74, 0x26, 0x27, 0x25, 0x28})
    _VOICE_VK = 0x74  # VK_F5, the RC003 microphone key
    _VOICE_SCAN = 0x3F  # F5's scan code

    def _gate(self, *, consume_wait_seconds=0.060):
        return suppressor.LegacyKeySuppressor(
            {self._VOICE_VK},
            rc003_vk_codes=self._RC003_KEYS,
            consume_wait_seconds=consume_wait_seconds,
        )

    def test_only_the_voice_key_is_exempt_from_arming(self):
        # The exemption must name exactly one key. A blanket exemption for the
        # RC003 set would look identical in the two tests below while silently
        # disabling arming for the arrow keys.
        self.assertEqual(
            suppressor._NEVER_ARMED_VK_CODES,
            frozenset({self._VOICE_VK}),
            "the never-armed set must contain the voice key and nothing else",
        )

        for vk in sorted(self._RC003_KEYS):
            with self.subTest(vk=hex(vk)):
                gate = self._gate()
                gate.arm_key_event(vk, 0x48, True, True)
                if vk == self._VOICE_VK:
                    self.assertEqual(
                        gate._armed_events,
                        [],
                        "the voice key must not be armable",
                    )
                else:
                    self.assertEqual(
                        len(gate._armed_events),
                        1,
                        f"RC003 key {vk:#x} must still be armable",
                    )

    def test_voice_key_is_never_added_to_the_armed_list(self):
        gate = self._gate()
        gate.arm_key_event(self._VOICE_VK, self._VOICE_SCAN, False, True)
        gate.arm_key_event(self._VOICE_VK, self._VOICE_SCAN, False, False)
        self.assertEqual(gate._armed_events, [])

        # Control: the list is genuinely being written to in this setup, so the
        # assertion above is about the voice key and not about arm_key_event
        # being a no-op here.
        gate.arm_key_event(0x26, 0x48, True, True)
        self.assertEqual(len(gate._armed_events), 1)

    def test_voice_key_skips_the_consume_window_entirely(self):
        # A deliberately absurd window: if the exemption were dropped this test
        # would block for ten seconds and then fail the bound, rather than
        # passing because the wait happened to be short.
        gate = self._gate(consume_wait_seconds=10.0)
        start = time.monotonic()
        self.assertFalse(
            gate.consume_armed_key_event(
                self._VOICE_VK, self._VOICE_SCAN, False, True
            )
        )
        self.assertLess(
            time.monotonic() - start,
            0.25,
            "the voice key waited for an arming edge that can never arrive",
        )

    def test_other_rc003_keys_still_pay_the_window(self):
        # The control for the test above: with nothing armed, a non-voice RC003
        # key really does block for the consume window. Without this, the voice
        # key's instant return would be indistinguishable from the window never
        # being applied at all.
        window = 0.30
        gate = self._gate(consume_wait_seconds=window)
        start = time.monotonic()
        self.assertFalse(gate.consume_armed_key_event(0x26, 0x48, True, True))
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(
            elapsed,
            window * 0.8,
            f"a non-voice RC003 key returned after only {elapsed:.3f}s - the "
            f"consume window was not applied",
        )


class LegacyKeySuppressorLifecycleTests(unittest.TestCase):
    def test_empty_suppressor_is_a_noop(self):
        gate = suppressor.LegacyKeySuppressor(set())
        gate.start(_run_target=lambda: None)
        self.assertFalse(gate.is_running)

    def test_rejects_second_start_while_thread_is_running(self):
        gate = suppressor.LegacyKeySuppressor({0x74})
        release = threading.Event()

        def fake_run():
            gate._ready_event.set()
            release.wait()

        try:
            gate.start(_run_target=fake_run)
            with self.assertRaises(suppressor.LegacyKeySuppressorUnavailableError):
                gate.start(_run_target=fake_run)
        finally:
            release.set()
            gate.stop()


if __name__ == "__main__":
    unittest.main()
