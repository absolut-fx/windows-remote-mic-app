"""Tests for the elevated at-logon Scheduled Task helper
(src/ovb_rc003/elevated_autostart.py).

Pure-Python / no Qt, same conventions as tests/test_startup_shortcut.py:

  * The module is the Windows Task Scheduler wrapper behind the
    ``startWithWindows`` SettingsController property. Its correctness is what
    decides whether the bridge starts elevated at logon - and therefore whether
    Frida can attach and Vol+/Vol-/Back work at all. A Startup-folder shortcut
    cannot elevate, so this is the only mechanism that produces a working
    autostart; that makes the ``-RunLevel Highest`` + ``-AtLogOn`` content of
    the registration script load-bearing, and it is asserted below rather than
    assumed.
  * Every OS-facing call goes through one injectable ``runner``, so these tests
    never spawn PowerShell.

Run with::

    PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/test_elevated_autostart.py
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

from ovb_rc003 import elevated_autostart as ea


class _RecordingRunner:
    """Fake PowerShell runner: returns a canned reply, records every script."""

    def __init__(self, reply: str = "OK", raises: BaseException | None = None) -> None:
        self.reply = reply
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def __call__(self, script: str, operation: str) -> str:
        self.calls.append((script, operation))
        if self.raises is not None:
            raise self.raises
        return self.reply

    @property
    def operations(self) -> list[str]:
        return [op for _script, op in self.calls]


class QueryStateTests(unittest.TestCase):
    def test_returns_the_token_the_runner_reports(self):
        for state in ("ABSENT", "READY", "RUNNING", "DISABLED"):
            with self.subTest(state=state):
                runner = _RecordingRunner(state)
                self.assertEqual(ea.query_state(runner=runner), state)

    def test_normalises_case_and_surrounding_whitespace(self):
        runner = _RecordingRunner("  ready \r\n")
        self.assertEqual(ea.query_state(runner=runner), "READY")

    def test_uses_the_last_line_when_powershell_chatters(self):
        # PowerShell can emit a warning line before the value; the value is
        # always the last line of our single-expression script.
        runner = _RecordingRunner("WARNING: something\r\nREADY\r\n")
        self.assertEqual(ea.query_state(runner=runner), "READY")

    def test_unknown_when_the_runner_raises(self):
        runner = _RecordingRunner(raises=OSError("powershell not found"))
        self.assertEqual(ea.query_state(runner=runner), "UNKNOWN")

    def test_unknown_when_output_is_empty(self):
        self.assertEqual(ea.query_state(runner=_RecordingRunner("")), "UNKNOWN")

    def test_queries_by_task_name(self):
        runner = _RecordingRunner("READY")
        ea.query_state(runner=runner)
        self.assertIn(ea.TASK_NAME, runner.calls[0][0])
        self.assertIn("Get-ScheduledTask", runner.calls[0][0])


class PredicateTests(unittest.TestCase):
    def test_is_registered(self):
        self.assertFalse(ea.is_registered(runner=_RecordingRunner("ABSENT")))
        self.assertFalse(ea.is_registered(runner=_RecordingRunner("UNKNOWN")))
        self.assertTrue(ea.is_registered(runner=_RecordingRunner("READY")))
        # A disabled task is still registered - it just will not fire.
        self.assertTrue(ea.is_registered(runner=_RecordingRunner("DISABLED")))

    def test_is_enabled(self):
        self.assertFalse(ea.is_enabled(runner=_RecordingRunner("ABSENT")))
        self.assertFalse(ea.is_enabled(runner=_RecordingRunner("DISABLED")))
        self.assertFalse(ea.is_enabled(runner=_RecordingRunner("UNKNOWN")))
        self.assertTrue(ea.is_enabled(runner=_RecordingRunner("READY")))
        self.assertTrue(ea.is_enabled(runner=_RecordingRunner("RUNNING")))

    def test_fail_closed_when_powershell_is_unavailable(self):
        """A query failure must read as "not on", never as "on" - the same
        fail-closed choice startup_shortcut.is_enabled makes."""
        runner = _RecordingRunner(raises=OSError("boom"))
        self.assertFalse(ea.is_registered(runner=runner))
        self.assertFalse(ea.is_enabled(runner=runner))


class GetStateDictTests(unittest.TestCase):
    def test_absent_is_not_an_error_but_says_registration_is_needed(self):
        state = ea.get_state_dict(runner=_RecordingRunner("ABSENT"))
        self.assertFalse(state["registered"])
        self.assertFalse(state["enabled"])
        self.assertFalse(state["is_error"])
        self.assertIn("管理员", state["status_text"])

    def test_disabled_is_registered_but_not_enabled(self):
        state = ea.get_state_dict(runner=_RecordingRunner("DISABLED"))
        self.assertTrue(state["registered"])
        self.assertFalse(state["enabled"])
        self.assertFalse(state["is_error"])

    def test_ready_is_registered_and_enabled(self):
        state = ea.get_state_dict(runner=_RecordingRunner("READY"))
        self.assertTrue(state["registered"])
        self.assertTrue(state["enabled"])
        self.assertFalse(state["is_error"])

    def test_unknown_is_surfaced_as_an_error(self):
        state = ea.get_state_dict(runner=_RecordingRunner(raises=OSError("boom")))
        self.assertFalse(state["registered"])
        self.assertTrue(state["is_error"])

    def test_reads_the_task_only_once(self):
        """One PowerShell round trip per snapshot, so the UI cannot show a
        registration state and an enablement state read at different times."""
        runner = _RecordingRunner("READY")
        ea.get_state_dict(runner=runner)
        self.assertEqual(len(runner.calls), 1)


class SetEnabledTests(unittest.TestCase):
    def test_enable_from_ready_is_idempotent_and_does_not_call_powershell(self):
        runner = _RecordingRunner("READY")
        result = ea.set_enabled(True, runner=runner)
        self.assertEqual(result.outcome, ea.ElevatedAutostartOutcome.ALREADY_ENABLED)
        # Only the query ran - no mutation.
        self.assertEqual(runner.operations, ["query_state"])

    def test_disable_from_disabled_is_idempotent_and_does_not_call_powershell(self):
        runner = _RecordingRunner("DISABLED")
        result = ea.set_enabled(False, runner=runner)
        self.assertEqual(result.outcome, ea.ElevatedAutostartOutcome.ALREADY_DISABLED)
        self.assertEqual(runner.operations, ["query_state"])

    def test_enabling_a_disabled_task_calls_enable_scheduled_task(self):
        runner = _RecordingRunner("DISABLED")
        result = ea.set_enabled(True, runner=runner)
        self.assertEqual(result.outcome, ea.ElevatedAutostartOutcome.ENABLED)
        self.assertEqual(runner.operations, ["query_state", "Enable-ScheduledTask"])
        self.assertIn("Enable-ScheduledTask", runner.calls[1][0])

    def test_disabling_a_ready_task_calls_disable_scheduled_task(self):
        runner = _RecordingRunner("READY")
        result = ea.set_enabled(False, runner=runner)
        self.assertEqual(result.outcome, ea.ElevatedAutostartOutcome.DISABLED)
        self.assertEqual(runner.operations, ["query_state", "Disable-ScheduledTask"])
        self.assertIn("Disable-ScheduledTask", runner.calls[1][0])

    def test_an_absent_task_is_reported_as_not_registered_not_as_a_failure(self):
        runner = _RecordingRunner("ABSENT")
        result = ea.set_enabled(True, runner=runner)
        self.assertEqual(result.outcome, ea.ElevatedAutostartOutcome.NOT_REGISTERED)
        self.assertIn("管理员", result.message)
        # No mutation was attempted against a task that does not exist.
        self.assertEqual(runner.operations, ["query_state"])

    def test_unknown_state_does_not_attempt_a_mutation(self):
        runner = _RecordingRunner(raises=OSError("boom"))
        result = ea.set_enabled(True, runner=runner)
        self.assertEqual(result.outcome, ea.ElevatedAutostartOutcome.FAILED)
        self.assertEqual(runner.operations, ["query_state"])

    def test_a_failing_mutation_returns_failed_rather_than_raising(self):
        class _Flaky(_RecordingRunner):
            def __call__(self, script: str, operation: str) -> str:
                if operation != "query_state":
                    raise OSError("powershell (Enable-ScheduledTask) exit=1")
                return super().__call__(script, operation)

        result = ea.set_enabled(True, runner=_Flaky("DISABLED"))
        self.assertEqual(result.outcome, ea.ElevatedAutostartOutcome.FAILED)
        self.assertIn("失败", result.message)


class RegisterTests(unittest.TestCase):
    def test_a_missing_executable_fails_without_calling_powershell(self):
        runner = _RecordingRunner("OK")
        result = ea.register(Path("Z:/definitely/not/here.exe"), runner=runner)
        self.assertEqual(result.outcome, ea.ElevatedAutostartOutcome.FAILED)
        self.assertEqual(runner.calls, [])

    def test_registration_is_elevated_and_at_logon(self):
        """The two properties that make this mechanism work at all: the task
        runs with the highest available token (so Frida's
        OpenProcess(WUDFHost.exe) succeeds with no UAC prompt) and it fires at
        logon. If either is dropped, autostart silently stops delivering the
        Vol+/Vol-/Back keys."""
        runner = _RecordingRunner("OK")
        result = ea.register(Path(sys.executable), runner=runner)
        self.assertEqual(result.outcome, ea.ElevatedAutostartOutcome.REGISTERED)

        script = runner.calls[-1][0]
        self.assertIn("-RunLevel Highest", script)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", script)
        self.assertIn("New-ScheduledTaskPrincipal", script)
        self.assertIn("Register-ScheduledTask", script)
        self.assertIn(ea.TASK_NAME, script)

    def test_registration_targets_the_given_executable(self):
        runner = _RecordingRunner("OK")
        ea.register(Path(sys.executable), runner=runner)
        script = runner.calls[-1][0]
        self.assertIn(str(Path(sys.executable).resolve()), script)
        self.assertIn(str(Path(sys.executable).resolve().parent), script)

    def test_registration_clears_a_previous_task_first(self):
        """Re-registering after a move/upgrade must not leave a stale action
        pointing at the old install path."""
        runner = _RecordingRunner("OK")
        ea.register(Path(sys.executable), runner=runner)
        script = runner.calls[-1][0]
        self.assertIn("Unregister-ScheduledTask", script)
        self.assertLess(
            script.index("Unregister-ScheduledTask"),
            script.index("Register-ScheduledTask"),
        )

    def test_access_denied_is_reported_as_a_needs_admin_message(self):
        runner = _RecordingRunner(
            raises=OSError("powershell (register) exit=1: Access is denied")
        )
        result = ea.register(Path(sys.executable), runner=runner)
        self.assertEqual(result.outcome, ea.ElevatedAutostartOutcome.FAILED)
        self.assertIn("管理员", result.message)

    def test_a_non_permission_failure_keeps_the_underlying_reason(self):
        runner = _RecordingRunner(raises=OSError("powershell (register) exit=1: boom"))
        result = ea.register(Path(sys.executable), runner=runner)
        self.assertEqual(result.outcome, ea.ElevatedAutostartOutcome.FAILED)
        self.assertIn("boom", result.message)


class UnregisterTests(unittest.TestCase):
    def test_unregister_removes_the_task(self):
        runner = _RecordingRunner("OK")
        result = ea.unregister(runner=runner)
        self.assertEqual(result.outcome, ea.ElevatedAutostartOutcome.UNREGISTERED)
        self.assertIn("Unregister-ScheduledTask", runner.calls[-1][0])

    def test_a_failure_returns_failed_rather_than_raising(self):
        runner = _RecordingRunner(raises=OSError("boom"))
        result = ea.unregister(runner=runner)
        self.assertEqual(result.outcome, ea.ElevatedAutostartOutcome.FAILED)


class ContractTests(unittest.TestCase):
    def test_every_public_function_accepts_an_injected_runner(self):
        """The module must stay testable without Windows: each public entry
        point takes the injectable runner as a keyword argument."""
        for name in (
            "query_state",
            "is_registered",
            "is_enabled",
            "get_state_dict",
            "set_enabled",
            "register",
            "unregister",
        ):
            with self.subTest(function=name):
                signature = inspect.signature(getattr(ea, name))
                self.assertIn("runner", signature.parameters)

    def test_importing_the_module_does_not_require_qt(self):
        """The settings app imports this at module scope; a Qt dependency here
        would break every non-GUI entry point (--dry-run, the bridge)."""
        source = Path(ea.__file__).read_text(encoding="utf-8")
        self.assertNotIn("PySide6", source)
        self.assertNotIn("import Qt", source)

    def test_the_task_name_matches_the_one_the_manual_script_registers(self):
        """The historical manual registration
        (C:\\Users\\absolut\\register-rmic-autostart.ps1) uses this exact name.
        Keeping them identical means an existing registration keeps working and
        there is never a second task competing for the device."""
        self.assertEqual(ea.TASK_NAME, "RemoteMicRC003-Elevated")


if __name__ == "__main__":
    unittest.main()
