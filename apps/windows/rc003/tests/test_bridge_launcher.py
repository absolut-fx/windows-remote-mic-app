"""Tests bridge_launcher.py's command construction and launch-outcome
detection (XRBM-029) purely via dependency injection - no real subprocess is
ever spawned and no real ``time.sleep`` ever runs, matching this package's
established "never touch a real OS resource in a test" convention (see e.g.
tests/test_single_instance.py's injected ``_create_mutex``/etc.).
"""

import unittest

from ovb_rc003 import bridge_launcher, single_instance


class BuildLaunchCommandTests(unittest.TestCase):
    def test_frozen_uses_the_current_executable_with_bridge_flag(self):
        command = bridge_launcher.build_launch_command(
            frozen=True, executable=r"C:\Apps\RemoteMicRC003.exe"
        )
        self.assertEqual(command, [r"C:\Apps\RemoteMicRC003.exe", "--bridge"])

    def test_frozen_command_never_recurses_into_settings(self):
        command = bridge_launcher.build_launch_command(
            frozen=True, executable=r"C:\Apps\RemoteMicRC003.exe"
        )
        self.assertNotIn("--settings", command)

    def test_frozen_settings_mode_still_launches_the_same_executable_with_bridge(self):
        # The single packaged exe handles both modes: no arguments / --settings
        # open the settings window, --bridge starts the bridge. There is no
        # separate settings sibling anymore.
        command = bridge_launcher.build_launch_command(
            frozen=True, executable=r"C:\Apps\RemoteMicRC003.exe"
        )
        self.assertEqual(command, [r"C:\Apps\RemoteMicRC003.exe", "--bridge"])

    def test_source_uses_the_current_interpreter_with_module_flag_and_bridge(self):
        command = bridge_launcher.build_launch_command(
            frozen=False, executable=r"C:\Python312\python.exe"
        )
        self.assertEqual(
            command, [r"C:\Python312\python.exe", "-m", "ovb_rc003", "--bridge"]
        )

    def test_source_command_never_recurses_into_settings(self):
        command = bridge_launcher.build_launch_command(
            frozen=False, executable=r"C:\Python312\python.exe"
        )
        self.assertNotIn("--settings", command)

    def test_empty_executable_fails_closed(self):
        with self.assertRaises(bridge_launcher.BridgeLaunchConfigurationError):
            bridge_launcher.build_launch_command(frozen=False, executable="")

    def test_defaults_read_the_real_sys_module_without_raising(self):
        # Exercises the frozen=None/executable=None default-resolution
        # branch directly (whatever this test process' own sys.executable/
        # sys.frozen happen to be) - just proves it never raises and always
        # returns a non-empty list, since the exact values are host-specific.
        command = bridge_launcher.build_launch_command()
        self.assertTrue(command)


class _FakeProcess:
    """Scripted stand-in for subprocess.Popen: ``poll_results`` is consumed
    one value at a time by each ``.poll()`` call, then the last value
    repeats - so a test can express "alive for N checks, then exit code X"
    as a short list.

    ``terminate()`` and ``kill()`` each flip an internal "exited" flag,
    after which ``poll()`` and ``wait()`` report the last scripted value,
    so ``stop_bridge``'s terminate/wait/kill branches can be exercised
    deterministically without a real subprocess.
    """

    def __init__(self, poll_results, *, pid=4242):
        self.pid = pid
        self._poll_results = list(poll_results)
        self._index = 0
        self._exited = False
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = []

    def _current(self):
        if self._exited:
            return self._poll_results[-1] if self._poll_results else 0
        if self._index < len(self._poll_results):
            value = self._poll_results[self._index]
            self._index += 1
            return value
        return self._poll_results[-1] if self._poll_results else None

    def poll(self):
        return self._current()

    def terminate(self):
        self.terminate_calls += 1
        self._exited = True

    def kill(self):
        self.kill_calls += 1
        self._exited = True

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self._exited:
            return self._poll_results[-1] if self._poll_results else 0
        # Per the real subprocess.Popen contract, an unexited process with a
        # timeout raises rather than returning. ``stop_bridge`` catches this
        # and escalates, so we raise to exercise that branch.
        import subprocess as _subprocess

        raise _subprocess.TimeoutExpired(cmd="fake", timeout=timeout)


def _make_handle(process):
    return bridge_launcher.BridgeHandle(process=process)


class LaunchBridgeTests(unittest.TestCase):
    def setUp(self):
        self._sleep_calls = []

    def _fake_sleep(self, seconds):
        self._sleep_calls.append(seconds)

    def test_process_still_alive_after_grace_period_is_started(self):
        process = _FakeProcess([None] * 20, pid=111)
        popen_calls = []

        def fake_popen(command):
            popen_calls.append(command)
            return process

        result = bridge_launcher.launch_bridge(
            ["exe"],
            grace_checks=3,
            poll_interval_seconds=0.01,
            _popen=fake_popen,
            _sleep=self._fake_sleep,
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.STARTED)
        self.assertEqual(result.pid, 111)
        self.assertIsNone(result.exit_code)
        self.assertEqual(popen_calls, [["exe"]])
        # Grace period exhausted (3 checks), never a real sleep.
        self.assertEqual(len(self._sleep_calls), 3)

    def test_exact_duplicate_instance_exit_code_is_already_running(self):
        process = _FakeProcess([single_instance.DUPLICATE_INSTANCE_EXIT_CODE])

        result = bridge_launcher.launch_bridge(
            ["exe"],
            _popen=lambda command: process,
            _sleep=self._fake_sleep,
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.ALREADY_RUNNING)
        self.assertEqual(result.exit_code, single_instance.DUPLICATE_INSTANCE_EXIT_CODE)
        # Detected on the very first poll - no grace-period sleeping needed.
        self.assertEqual(self._sleep_calls, [])

    def test_other_nonzero_exit_code_is_a_quick_exit_with_the_real_code_preserved(self):
        process = _FakeProcess([7])

        result = bridge_launcher.launch_bridge(
            ["exe"], _popen=lambda command: process, _sleep=self._fake_sleep
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.QUICK_EXIT)
        self.assertEqual(result.exit_code, 7)

    def test_clean_zero_exit_within_grace_period_is_still_a_quick_exit(self):
        # A bridge that exits 0 almost immediately did not stay up to run
        # the bridge - "0" must not be silently treated as "started" just
        # because it isn't an error code.
        process = _FakeProcess([0])

        result = bridge_launcher.launch_bridge(
            ["exe"], _popen=lambda command: process, _sleep=self._fake_sleep
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.QUICK_EXIT)
        self.assertEqual(result.exit_code, 0)

    def test_guard_unavailable_exit_code_is_a_distinct_quick_exit_not_already_running(self):
        process = _FakeProcess([single_instance.GUARD_UNAVAILABLE_EXIT_CODE])

        result = bridge_launcher.launch_bridge(
            ["exe"], _popen=lambda command: process, _sleep=self._fake_sleep
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.QUICK_EXIT)
        self.assertEqual(result.exit_code, single_instance.GUARD_UNAVAILABLE_EXIT_CODE)

    def test_popen_oserror_is_reported_as_launch_failed_not_raised(self):
        def raising_popen(command):
            raise OSError("[WinError 2] The system cannot find the file specified")

        result = bridge_launcher.launch_bridge(
            ["missing.exe"], _popen=raising_popen, _sleep=self._fake_sleep
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.LAUNCH_FAILED)
        self.assertIsNone(result.exit_code)
        self.assertIn("WinError 2", result.error)
        self.assertEqual(self._sleep_calls, [])

    def test_process_exits_partway_through_the_grace_period(self):
        # Alive for the first two checks, then exits - proves the polling
        # loop keeps checking rather than only ever looking once. Uses code
        # 9, deliberately distinct from single_instance.
        # DUPLICATE_INSTANCE_EXIT_CODE (3), to stay in the QUICK_EXIT branch.
        process = _FakeProcess([None, None, 9])

        result = bridge_launcher.launch_bridge(
            ["exe"],
            grace_checks=5,
            _popen=lambda command: process,
            _sleep=self._fake_sleep,
        )

        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.QUICK_EXIT)
        self.assertEqual(result.exit_code, 9)
        self.assertEqual(len(self._sleep_calls), 2)

    def test_no_command_argument_falls_back_to_build_launch_command(self):
        popen_calls = []

        def fake_popen(command):
            popen_calls.append(command)
            return _FakeProcess([None] * 5)

        bridge_launcher.launch_bridge(
            grace_checks=1,
            _popen=fake_popen,
            _sleep=self._fake_sleep,
        )

        self.assertEqual(len(popen_calls), 1)
        self.assertTrue(popen_calls[0])  # non-empty, host-dependent contents


if __name__ == "__main__":
    unittest.main()


class BridgeHandleTests(unittest.TestCase):
    def test_pid_returns_underlying_process_pid(self):
        process = _FakeProcess([None], pid=90210)
        handle = _make_handle(process)
        self.assertEqual(handle.pid(), 90210)

    def test_is_alive_is_true_while_poll_returns_none(self):
        process = _FakeProcess([None, None])
        handle = _make_handle(process)
        self.assertTrue(handle.is_alive())

    def test_is_alive_is_false_once_poll_returns_an_exit_code(self):
        process = _FakeProcess([0])
        handle = _make_handle(process)
        # Exhaust the alive checks.
        process.poll()
        self.assertFalse(handle.is_alive())


class StopBridgeTests(unittest.TestCase):
    def setUp(self):
        self._sleep_calls = []

    def _fake_sleep(self, seconds):
        self._sleep_calls.append(seconds)

    def test_none_handle_is_not_running(self):
        result = bridge_launcher.stop_bridge(None)
        self.assertEqual(result.outcome, bridge_launcher.StopOutcome.NOT_RUNNING)
        self.assertIsNone(result.pid)
        self.assertIsNone(result.exit_code)
        self.assertFalse(result.escalated)

    def test_already_exited_process_is_not_running(self):
        # Already exited (exit code 0). stop_bridge must NOT call
        # terminate() on a dead process.
        process = _FakeProcess([0])
        process.poll()  # exhaust alive checks; now poll() returns 0
        handle = _make_handle(process)

        result = bridge_launcher.stop_bridge(handle)

        self.assertEqual(result.outcome, bridge_launcher.StopOutcome.NOT_RUNNING)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(process.terminate_calls, 0)
        self.assertEqual(process.kill_calls, 0)

    def test_alive_process_exits_after_terminate_within_grace_period(self):
        # The process is alive on the first poll (None), ``terminate()``
        # flips it to "exited", and the follow-up ``wait()`` then returns
        # the last poll result (0) cleanly.
        process = _FakeProcess([None, 0], pid=777)
        handle = _make_handle(process)

        result = bridge_launcher.stop_bridge(handle, grace_seconds=5.0)

        self.assertEqual(result.outcome, bridge_launcher.StopOutcome.STOPPED)
        self.assertEqual(result.pid, 777)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.escalated)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 0)
        self.assertEqual(process.wait_calls, [5.0])

    def test_alive_process_ignores_terminate_and_must_be_killed(self):
        # A process that ignores terminate(): the underlying ``wait`` call
        # inside stop_bridge raises TimeoutExpired, the grace period
        # elapses, then ``kill()`` is called and the fake process's exit
        # flip lets the second ``wait`` return the final exit code.
        process = _FakeProcess([None, 0])
        # Make terminate() a no-op that does NOT flip ``_exited`` (process
        # ignores the signal), but still counts the call.
        process.terminate = lambda: setattr(process, "terminate_calls", process.terminate_calls + 1)
        # Override wait() so the first call raises TimeoutExpired (as it
        # would on a process that hasn't actually exited) and the second
        # call returns the final exit code (as it would after kill()).
        call_count = {"n": 0}

        def raising_first_wait(timeout=None):
            process.wait_calls.append(timeout)
            call_count["n"] += 1
            if call_count["n"] == 1:
                import subprocess as _subprocess

                raise _subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
            return 0

        process.wait = raising_first_wait
        handle = _make_handle(process)

        result = bridge_launcher.stop_bridge(handle, grace_seconds=2.0)

        self.assertEqual(result.outcome, bridge_launcher.StopOutcome.STOPPED)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.escalated)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        # wait() called twice - once after terminate (timeout), once after
        # kill (returns).
        self.assertEqual(process.wait_calls, [2.0, 2.0])

    def test_terminate_raises_oserror_is_treated_as_not_running(self):
        # Process vanished between poll() and terminate(): terminate()
        # itself raises OSError. stop_bridge should report NOT_RUNNING,
        # not escalate to kill().
        process = _FakeProcess([None])
        handle = _make_handle(process)

        def raising_terminate():
            process.terminate_calls += 1
            raise OSError("ERROR_ACCESS_DENIED")

        process.terminate = raising_terminate
        result = bridge_launcher.stop_bridge(handle)

        self.assertEqual(result.outcome, bridge_launcher.StopOutcome.NOT_RUNNING)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 0)

    def test_grace_seconds_zero_is_one_shot_terminate_then_kill_no_wait(self):
        # grace_seconds<=0 takes the fast path: terminate, then kill, but
        # no wait() is ever called. ``terminate()`` and ``kill()`` each flip
        # the fake's ``_exited`` flag, so the final poll() reports the last
        # scripted value.
        process = _FakeProcess([None, 0])
        handle = _make_handle(process)

        result = bridge_launcher.stop_bridge(handle, grace_seconds=0)

        # terminate() flips _exited, kill() flips it again - so the final
        # poll() reports the last scripted value.
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.wait_calls, [])
        # The fast path may still complete cleanly if the fake has already
        # exited; what matters is no wait() was attempted and both
        # terminate() and kill() were called exactly once.
        self.assertIn(
            result.outcome,
            (
                bridge_launcher.StopOutcome.STOPPED,
                bridge_launcher.StopOutcome.TIMED_OUT,
            ),
        )


class LaunchResultHandleFieldTests(unittest.TestCase):
    """Regression test: a STARTED launch must include a ``BridgeHandle``
    so the caller can pass it to ``stop_bridge``. A QUICK_EXIT launch
    (the subprocess died immediately) must include ``handle=None`` -
    there's nothing left to stop."""

    def _run(self, poll_results):
        process = _FakeProcess(poll_results)
        return bridge_launcher.launch_bridge(
            ["exe"],
            grace_checks=3,
            poll_interval_seconds=0.01,
            _popen=lambda command: process,
            _sleep=lambda s: None,
        )

    def test_started_result_includes_a_bridge_handle(self):
        result = self._run([None] * 5)
        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.STARTED)
        self.assertIsNotNone(result.handle)
        self.assertIsInstance(result.handle, bridge_launcher.BridgeHandle)
        self.assertEqual(result.handle.pid(), 4242)

    def test_quick_exit_result_handle_is_none(self):
        result = self._run([7])
        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.QUICK_EXIT)
        self.assertIsNone(result.handle)

    def test_already_running_result_handle_is_none(self):
        result = self._run([single_instance.DUPLICATE_INSTANCE_EXIT_CODE])
        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.ALREADY_RUNNING)
        self.assertIsNone(result.handle)

    def test_launch_failed_result_handle_is_none(self):
        def raising_popen(command):
            raise OSError("[WinError 2] The system cannot find the file specified")

        result = bridge_launcher.launch_bridge(
            ["missing.exe"], _popen=raising_popen, _sleep=lambda s: None
        )
        self.assertEqual(result.outcome, bridge_launcher.LaunchOutcome.LAUNCH_FAILED)
        self.assertIsNone(result.handle)
