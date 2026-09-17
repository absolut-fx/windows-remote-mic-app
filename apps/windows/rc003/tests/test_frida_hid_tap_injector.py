"""Ordering tests for src/ovb_rc003/frida_hid_tap_injector.py.

``inject_current_process()`` is the one function that decides whether the
optional Frida HID tap can run at all: the RC003's Vol+/Vol-/Back keys are
delivered only through Frida attaching to ``WUDFHost.exe``, and that
``OpenProcess`` needs an administrator token with ``SeDebug`` assigned.

The order of its steps is the contract these tests pin, because two of the
orderings are load-bearing:

* ``enable_debug_privilege()`` must run **first**, before the target is
  inspected. If it ran last, a caller without ``SeDebug`` would instead see
  "RC003 host changed before injection" or "refusing non-WUDFHost target" -
  messages that describe a target problem when the real problem is the
  caller's token.
* On a non-Windows host it must refuse **before** touching privileges, so
  importing and calling this on the wrong platform fails with one clear
  ``PermissionError`` instead of a ``WinDLL`` lookup error.

Every OS-facing collaborator is replaced, so nothing here touches Win32, a real
WUDFHost process, or the Frida Gadget. That also makes these tests meaningful
on non-Windows CI.

Run with::

    PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/test_frida_hid_tap_injector.py
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import frida_hid_tap_injector


class InjectCurrentProcessOrderingTests(unittest.TestCase):
    """The step order of ``inject_current_process()``."""

    PID = 4242
    DLL = Path("C:/gadget/gadget.dll")

    def setUp(self):
        # Records the collaborator names in the order they are actually called.
        self.order: list[str] = []

        def record(name, result):
            def _fn(*args, **kwargs):
                self.order.append(name)
                return result

            return _fn

        self.enable_debug_privilege = mock.Mock(
            side_effect=record("enable_debug_privilege", None)
        )
        self.find_host = mock.Mock(
            side_effect=record("find_rc003_hidogatt_host_pid", self.PID)
        )
        self.target_process_name = mock.Mock(
            side_effect=record("_target_process_name", "wudfhost.exe")
        )
        self.prepare_secure_runtime = mock.Mock(
            side_effect=record("prepare_secure_runtime", self.DLL)
        )
        self.sha256_file = mock.Mock(
            side_effect=record("sha256_file", frida_hid_tap_injector.GADGET_DLL_SHA256)
        )
        self.inject_library = mock.Mock(side_effect=record("inject_library", None))

        for name, replacement in (
            ("enable_debug_privilege", self.enable_debug_privilege),
            ("find_rc003_hidogatt_host_pid", self.find_host),
            ("_target_process_name", self.target_process_name),
            ("prepare_secure_runtime", self.prepare_secure_runtime),
            ("sha256_file", self.sha256_file),
            ("inject_library", self.inject_library),
        ):
            patcher = mock.patch.object(frida_hid_tap_injector, name, new=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_debug_privilege_is_the_first_step(self):
        frida_hid_tap_injector.inject_current_process(self.PID)

        self.assertTrue(self.order, "inject_current_process did nothing")
        self.assertEqual(
            self.order[0],
            "enable_debug_privilege",
            f"SeDebug must be enabled before anything else, got order {self.order}",
        )
        self.enable_debug_privilege.assert_called_once_with()

    def test_debug_privilege_is_enabled_before_the_wudfhost_guard(self):
        # Force the WUDFHost guard to refuse, then check the privilege had
        # already been enabled: the caller must be able to tell "my token is
        # wrong" from "that is not the right process".
        # Still records into self.order, so the ordering assertion below has
        # something to compare against.
        def refuse(*args, **kwargs):
            self.order.append("_target_process_name")
            return "notepad.exe"

        self.target_process_name.side_effect = refuse

        with self.assertRaises(RuntimeError):
            frida_hid_tap_injector.inject_current_process(self.PID)

        self.enable_debug_privilege.assert_called_once()
        self.assertLess(
            self.order.index("enable_debug_privilege"),
            self.order.index("_target_process_name"),
            f"privilege must be enabled before the target is inspected: {self.order}",
        )
        self.inject_library.assert_not_called()

    def test_happy_path_still_reaches_inject_library(self):
        frida_hid_tap_injector.inject_current_process(self.PID)

        self.inject_library.assert_called_once_with(self.PID, self.DLL)
        self.assertEqual(
            self.order,
            [
                "enable_debug_privilege",
                "find_rc003_hidogatt_host_pid",
                "_target_process_name",
                "prepare_secure_runtime",
                "sha256_file",
                "inject_library",
            ],
        )

    def test_non_windows_still_refuses_before_touching_privileges(self):
        # Replacing the module's own `os` reference keeps the real os module
        # (and therefore the test runner) untouched.
        fake_os = mock.Mock()
        fake_os.name = "posix"

        with mock.patch.object(frida_hid_tap_injector, "os", fake_os):
            with self.assertRaises(PermissionError):
                frida_hid_tap_injector.inject_current_process(self.PID)

        self.assertEqual(
            self.order,
            [],
            f"nothing may be touched before the platform check, got {self.order}",
        )
        self.enable_debug_privilege.assert_not_called()
        self.inject_library.assert_not_called()

    def test_unassigned_privilege_propagates_as_permission_error(self):
        # The exact failure a medium-integrity token produces.
        self.enable_debug_privilege.side_effect = PermissionError(
            "SeDebugPrivilege is not assigned"
        )

        with self.assertRaises(PermissionError) as caught:
            frida_hid_tap_injector.inject_current_process(self.PID)

        self.assertIn("SeDebugPrivilege", str(caught.exception))
        # Swallowing it would make "no privilege" indistinguishable from
        # "injection not needed", and the tap would fail silently.
        self.inject_library.assert_not_called()


if __name__ == "__main__":
    unittest.main()
