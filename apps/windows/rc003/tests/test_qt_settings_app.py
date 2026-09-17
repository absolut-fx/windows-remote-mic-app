"""Qt-gated tests for qt_settings_app.py (XRBM-030): ButtonMappingModel,
SettingsController, and an offscreen load of the real main.qml. Every test
class here self-skips with an explicit reason if PySide6-Essentials is not
installed - same "skip with reason, never silently pass" convention as
tests/windows/test_windows_only.py, except gated on PySide6 availability
rather than the host OS, since these are meant to run for REAL wherever Qt
is actually installed (including the real Windows CI runner, now that
PySide6-Essentials is a pinned requirement - see requirements.txt).

No real Windows device or real BLE/HID/audio hardware is ever touched here:
bridge_launcher.launch_bridge()/shell_targets.open_external_target() are
monkeypatched exactly like tests/test_bridge_launcher.py and
tests/test_shell_targets.py already do at their own layer. Real, disposable
CHILD PROCESSES are deliberately spawned by some tests below (XRBM-035
RETRY 1): DiagnosticsController's real BLE candidate check now runs its
discovery in a genuinely separate OS process (see windows_diagnostics.py's
"-- BLE candidate --" section) - this file's own shutdown/probe tests
exercise that real process boundary rather than mocking it away, since the
whole point of that design is a real, OS-confirmed hard bound this project
cannot prove any other way.
"""

import ast
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import (
    audio_output,
    bridge_launcher,
    config,
    device_catalog,
    hotkey,
    key_mapping,
    qt_settings_app,
    remote_layout,
    settings_ui,
    shell_targets,
    tray_icon,
    vb_cable_bundle,
    windows_diagnostics,
)

# Shared, widgets-capable Qt application. Never construct one directly in a
# test - see tests/qt_app.py for the 0xC0000409 abort that causes.
from . import qt_app


def _has_pyside6() -> bool:
    try:
        import PySide6.QtCore  # noqa: F401
    except ImportError:
        return False
    return True


_HAS_PYSIDE6 = _has_pyside6()

if _HAS_PYSIDE6:
    # Must be set before any QGuiApplication is constructed anywhere in this
    # process - a real display server/compositor is neither available nor
    # wanted in a test run (matches how this task's own isolated-venv
    # screenshot step invokes the same app code). ``qt_settings_app`` itself
    # is already imported unconditionally above (importing it never
    # requires PySide6 - only actually calling ``_load_qt_classes()``/
    # constructing a QGuiApplication does, both of which only happen later,
    # inside test methods, after this env var is already set).
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    # Every test below that needs to drive a real QQmlApplicationEngine/QML
    # window (load, click, type, render) does so inside an isolated
    # subprocess - see _QML_LOAD_PROBE_SCRIPT/_DIRECT_SAVE_PROBE_SCRIPT/
    # _CONTRAST_PROBE_SCRIPT below for why: QQuickStyle is a process-global,
    # set-once setting, and separately, multiple QQmlApplicationEngine
    # instances loading Qt Quick Controls QML within one process turned out
    # to conflict over other internal per-style singletons too (both
    # reproduced empirically while writing these tests). This module's own
    # process therefore never needs to import PySide6's GUI/QML/Test
    # classes directly - only ``qt_settings_app`` itself, for the
    # ButtonMappingModel/SettingsController unit tests above, which never
    # construct a QQmlApplicationEngine at all.


_SKIP_REASON = "PySide6-Essentials not installed - Qt settings UI not verified here"


class DiagnosticsThreadLifecycleAtExitTests(unittest.TestCase):
    """Pure-Python coverage of the diagnostics worker-thread registry and
    its atexit join hook - deliberately NOT gated on PySide6 (importing
    ``qt_settings_app`` never requires it), since none of this touches Qt at
    all. XRBM-031 RETRY 1 item 6: proves the atexit hook is a genuinely
    BOUNDED, best-effort courtesy - never a guarantee every tracked thread
    has actually stopped - matching the corrected module docstring.
    """

    def test_join_at_exit_is_bounded_and_returns_even_if_a_thread_never_finishes(self):
        blocker = threading.Event()
        thread = threading.Thread(target=blocker.wait, daemon=True)
        qt_settings_app._remember_diagnostics_thread(thread)
        thread.start()
        try:
            # A tiny timeout (not the real 2.0s default) keeps this test
            # fast without weakening the production default anywhere.
            with mock.patch.object(
                qt_settings_app, "_DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS", 0.05
            ):
                started = time.monotonic()
                qt_settings_app._join_diagnostics_threads_at_exit()
                elapsed = time.monotonic() - started
            # The hook must return promptly - it must never block on a
            # thread that simply never finishes.
            self.assertLess(elapsed, 2.0)
            # The thread is genuinely still alive - the join was bounded,
            # not a guarantee of completion, exactly as documented.
            self.assertTrue(thread.is_alive())
        finally:
            blocker.set()
            thread.join(timeout=5.0)
            qt_settings_app._forget_diagnostics_thread(thread)

    def test_join_at_exit_actually_joins_a_thread_that_finishes_in_time(self):
        release_event = threading.Event()
        thread = threading.Thread(target=release_event.wait, daemon=True)
        qt_settings_app._remember_diagnostics_thread(thread)
        thread.start()
        release_event.set()
        try:
            qt_settings_app._join_diagnostics_threads_at_exit()
            self.assertFalse(thread.is_alive())
        finally:
            qt_settings_app._forget_diagnostics_thread(thread)

    def test_diagnostics_worker_thread_is_created_as_a_daemon_thread(self):
        # The actual safety property against a process-exit hang: the real
        # production code's own diagnostics-worker Thread() construction
        # must pass daemon=True, so CPython itself never waits for it at
        # interpreter shutdown regardless of what the best-effort atexit
        # join above managed to join in time. A static source check (no
        # PySide6 needed) rather than constructing a real DiagnosticsController.
        import inspect

        source = inspect.getsource(qt_settings_app)
        self.assertIn('threading.Thread(target=_run_in_background, daemon=True)', source)


class DiagnosticsShutdownOrderingTests(unittest.TestCase):
    """XRBM-031 RETRY 2: an independent review found that the previous fix
    registered ``_join_diagnostics_threads_at_exit`` and
    ``_release_qt_classes_cache`` as two SEPARATE ``atexit`` callbacks -
    since ``atexit`` runs registered functions in LIFO order, the
    second-registered function (cache release) actually ran FIRST at real
    process exit, the reverse of what the module docstring claimed. These
    tests are deliberately NOT gated on PySide6 (none of this touches Qt -
    the functions under test are pure Python, and are exercised here via
    mocks standing in for the real join/cache-release steps).
    """

    def tearDown(self):
        # Defensive: no test below should leave this set, but clear it
        # unconditionally so a failure here never leaks into a later test
        # in the same process (this is process-global, persistent state).
        qt_settings_app._diagnostics_shutdown_event.clear()

    def test_module_registers_exactly_one_atexit_shutdown_hook(self):
        # Guards against silently reintroducing the two-separate-
        # registrations bug this RETRY fixes: an AST-level count of actual
        # atexit.register(...) CALL expressions (not a naive substring
        # count, which would also match this module's own docstring prose
        # explaining why there is only one) - introspecting the real source
        # rather than CPython's private atexit internals.
        import ast
        import inspect

        source = inspect.getsource(qt_settings_app)
        tree = ast.parse(source)
        register_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "atexit"
        ]
        self.assertEqual(len(register_calls), 1)
        (call,) = register_calls
        self.assertEqual(call.args[0].id, "_shutdown_qt_settings_app_at_exit")

    def test_shutdown_at_exit_runs_flag_then_join_then_release_cache_in_order(self):
        calls = []
        with mock.patch.object(
            qt_settings_app,
            "_begin_diagnostics_shutdown",
            side_effect=lambda: calls.append("begin_shutdown"),
        ):
            with mock.patch.object(
                qt_settings_app,
                "_join_diagnostics_threads_at_exit",
                side_effect=lambda: calls.append("join"),
            ):
                with mock.patch.object(
                    qt_settings_app,
                    "_release_qt_classes_cache",
                    side_effect=lambda: calls.append("release_cache"),
                ):
                    qt_settings_app._shutdown_qt_settings_app_at_exit()
        self.assertEqual(calls, ["begin_shutdown", "join", "release_cache"])

    def test_begin_diagnostics_shutdown_sets_the_event(self):
        self.assertFalse(qt_settings_app._diagnostics_shutdown_event.is_set())
        qt_settings_app._begin_diagnostics_shutdown()
        self.assertTrue(qt_settings_app._diagnostics_shutdown_event.is_set())

    def test_join_diagnostics_threads_at_exit_never_touches_the_shutdown_event(self):
        # Kept orthogonal on purpose (see _join_diagnostics_threads_at_exit's
        # docstring) so RETRY 1's own tests, which call this function
        # directly without expecting a global side effect, keep working.
        self.assertFalse(qt_settings_app._diagnostics_shutdown_event.is_set())
        qt_settings_app._join_diagnostics_threads_at_exit()
        self.assertFalse(qt_settings_app._diagnostics_shutdown_event.is_set())


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class ButtonMappingModelTests(unittest.TestCase):
    def setUp(self):
        self.Model = qt_settings_app._load_qt_classes()["ButtonMappingModel"]

    def test_row_count_is_thirteen(self):
        model = self.Model()
        self.assertEqual(model.rowCount(), 13)

    def test_role_names_cover_every_expected_role(self):
        model = self.Model()
        role_names = {value.data().decode() for value in model.roleNames().values()}
        for expected in (
            "buttonId", "displayName", "hidUsage", "actionText", "isMic",
            "doubleClickText", "longPressText",
            "isSelected", "hotspotX", "hotspotY", "hotspotWidth",
            "hotspotHeight", "isVoice",
        ):
            self.assertIn(expected, role_names)

    def test_data_matches_remote_layout_for_the_ok_button(self):
        model = self.Model()
        index = model.index(model.index_of("ok"), 0)
        self.assertEqual(model.data(index, model.ButtonIdRole), "ok")
        self.assertEqual(
            model.data(index, model.DisplayNameRole),
            remote_layout.BUTTON_DISPLAY_NAMES["ok"],
        )
        self.assertEqual(
            model.data(index, model.HidUsageRole), remote_layout.hid_usage_display("ok")
        )
        self.assertFalse(model.data(index, model.IsMicRole))

    def test_hotspot_geometry_matches_remote_layout_for_mic(self):
        model = self.Model()
        index = model.index(model.index_of("mic"), 0)
        hotspot = remote_layout.hotspot_for("mic")
        self.assertEqual(model.data(index, model.XRole), hotspot.x)
        self.assertEqual(model.data(index, model.YRole), hotspot.y)
        self.assertEqual(model.data(index, model.WidthRole), hotspot.width)
        self.assertEqual(model.data(index, model.HeightRole), hotspot.height)
        self.assertTrue(model.data(index, model.IsVoiceRole))

    def test_mic_row_always_shows_the_fixed_display_regardless_of_loaded_map(self):
        model = self.Model()
        model.load_display_map({"mic": "should be ignored entirely"})
        index = model.index(model.index_of("mic"), 0)
        self.assertTrue(model.data(index, model.IsMicRole))
        self.assertEqual(model.data(index, model.ActionTextRole), settings_ui._MIC_ROW_DISPLAY)

    def test_set_action_text_at_updates_a_non_mic_row(self):
        model = self.Model()
        row = model.index_of("power")
        model.setActionTextAt(row, "escape")
        index = model.index(row, 0)
        self.assertEqual(model.data(index, model.ActionTextRole), "escape")

    def test_set_action_text_at_is_a_no_op_for_the_mic_row(self):
        model = self.Model()
        row = model.index_of("mic")
        model.setActionTextAt(row, "should not apply")
        index = model.index(row, 0)
        self.assertEqual(model.data(index, model.ActionTextRole), settings_ui._MIC_ROW_DISPLAY)

    def test_secondary_action_text_can_be_set_and_round_tripped(self):
        model = self.Model()
        row = model.index_of("power")
        model.setSecondaryActionTextAt(row, "double_click", "f5")
        model.setSecondaryActionTextAt(row, "long_press", "系统音量 +")
        index = model.index(row, 0)
        self.assertEqual(model.data(index, model.DoubleClickTextRole), "f5")
        self.assertEqual(model.data(index, model.LongPressTextRole), "系统音量 +")
        self.assertEqual(
            model.to_secondary_display_map()["power"],
            {"double_click": "f5", "long_press": "系统音量 +"},
        )

    def test_to_display_map_round_trips_non_mic_entries_and_excludes_mic(self):
        model = self.Model()
        model.load_display_map({"power": "escape", "up": "up"})
        result = model.to_display_map()
        self.assertEqual(result["power"], "escape")
        self.assertEqual(result["up"], "up")
        self.assertNotIn("mic", result)

    def test_unconfigured_secondary_actions_have_an_explicit_display_value(self):
        model = self.Model()
        model.load_display_map({"power": "escape"})
        index = model.index(model.index_of("power"), 0)
        self.assertEqual(
            model.data(index, model.DoubleClickTextRole),
            settings_ui.SECONDARY_UNCONFIGURED_DISPLAY,
        )
        self.assertEqual(
            model.data(index, model.LongPressTextRole),
            settings_ui.SECONDARY_UNCONFIGURED_DISPLAY,
        )
        self.assertEqual(
            model.to_secondary_display_map()["power"],
            {"double_click": "", "long_press": ""},
        )

    def test_selecting_a_button_flags_only_that_row_as_selected(self):
        model = self.Model()
        self.assertEqual(model.selected_button_id(), "ok")
        model.set_selected_button("power")
        self.assertEqual(model.selected_button_id(), "power")
        power_index = model.index(model.index_of("power"), 0)
        ok_index = model.index(model.index_of("ok"), 0)
        self.assertTrue(model.data(power_index, model.IsSelectedRole))
        self.assertFalse(model.data(ok_index, model.IsSelectedRole))

    def test_index_of_button_slot_matches_the_plain_python_helper(self):
        model = self.Model()
        self.assertEqual(model.indexOfButton("tv"), model.index_of("tv"))

    def test_index_of_button_returns_negative_one_for_unknown_id(self):
        model = self.Model()
        self.assertEqual(model.indexOfButton("does_not_exist"), -1)


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class SettingsControllerTests(unittest.TestCase):
    def setUp(self):
        classes = qt_settings_app._load_qt_classes()
        self.Model = classes["ButtonMappingModel"]
        self.Controller = classes["SettingsController"]
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(os.environ, {"LOCALAPPDATA": self._tmpdir.name})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def _make_controller(self):
        model = self.Model()
        controller = self.Controller(model)
        return controller, model

    def test_hotkey_text_defaults_to_the_configured_default(self):
        controller, _ = self._make_controller()
        self.assertEqual(controller.hotkeyText, "ralt+space")

    def test_trigger_mode_options_has_exactly_toggle_and_hold(self):
        controller, _ = self._make_controller()
        self.assertEqual(len(controller.triggerModeOptions), 2)

    def test_trigger_mode_switch_also_switches_the_paired_voice_hotkey(self):
        controller, _ = self._make_controller()
        controller.triggerModeIndex = 1
        self.assertEqual(controller.hotkeyText, "ralt")
        controller.triggerModeIndex = 0
        self.assertEqual(controller.hotkeyText, "ralt+space")

    def test_recording_left_ctrl_win_switches_to_hold_without_replacing_capture(self):
        controller, _ = self._make_controller()
        controller._on_hotkey_capture_result("lctrl+lwin")
        self.assertEqual(controller.triggerModeIndex, 1)
        controller.hotkeyText = "lctrl+lwin"
        self.assertEqual(controller.hotkeyText, "lctrl+lwin")

    def test_launch_status_starts_as_the_not_started_constant(self):
        controller, _ = self._make_controller()
        self.assertEqual(controller.launchStatusText, settings_ui.LAUNCH_NOT_STARTED_TEXT)

    def test_mic_row_text_matches_settings_ui_constant(self):
        controller, _ = self._make_controller()
        self.assertEqual(controller.micRowText, settings_ui._MIC_ROW_DISPLAY)

    def test_photo_available_and_source_are_consistent(self):
        controller, _ = self._make_controller()
        # This repository ships Resources/RC003-remote-photo.png, so this
        # should be true in every real checkout - but the assertion is
        # written to hold either way (never a stretched/fake image, see
        # task DoD): availability and a non-empty source string must agree.
        self.assertEqual(controller.photoAvailable, bool(controller.photoSource))

    def test_device_selector_defaults_to_rc003_for_existing_users(self):
        controller, _ = self._make_controller()
        self.assertEqual(
            controller.selectedDeviceIndex,
            controller._DEVICE_ORDER.index(device_catalog.RC003_ID),
        )
        self.assertTrue(controller.isRc003Device)
        self.assertFalse(controller.isDjiMic2Device)
        self.assertEqual(controller.mappingPageTitle, "按键映射")

    def test_selecting_dji_changes_the_ui_contract_and_persists(self):
        controller, _ = self._make_controller()
        controller.selectedDeviceIndex = controller._DEVICE_ORDER.index(
            device_catalog.DJI_MIC_2_ID
        )
        self.assertFalse(controller.isRc003Device)
        self.assertTrue(controller.isDjiMic2Device)
        self.assertEqual(controller.mappingPageTitle, "设备控制")
        self.assertIn("Windows 录音输入", controller.selectedDeviceDescription)
        self.assertTrue(controller.saveSettings())
        stored = config.load_config(config.config_path(Path(self._tmpdir.name) / "RemoteMic" / "RC003"))
        self.assertEqual(stored["selected_device_profile"], device_catalog.DJI_MIC_2_ID)

    def test_dji_control_rows_match_the_truthful_device_catalog(self):
        controller, _ = self._make_controller()
        self.assertEqual(
            [row["name"] for row in controller.djiControlRows],
            ["录音键", "连接键", "电源键"],
        )
        self.assertTrue(all("映射" in row["mapping"] for row in controller.djiControlRows))

    def test_dji_save_and_launch_never_starts_the_rc003_bridge(self):
        controller, _ = self._make_controller()
        controller.selectedDeviceIndex = controller._DEVICE_ORDER.index(
            device_catalog.DJI_MIC_2_ID
        )
        with mock.patch.object(bridge_launcher, "launch_bridge") as fake_launch:
            controller.saveAndLaunch()
        fake_launch.assert_not_called()
        self.assertIn("不启动 RC003", controller.launchStatusText)

    def test_save_settings_persists_and_clears_error_message(self):
        controller, model = self._make_controller()
        model.setActionTextAt(model.index_of("power"), "escape")
        self.assertTrue(controller.saveSettings())
        self.assertEqual(controller.errorMessage, "")
        self.assertIn("已保存", controller.statusMessage)

    def test_save_settings_reports_a_persistence_failure(self):
        controller, _ = self._make_controller()
        with mock.patch.object(
            config, "save_key_bindings", side_effect=OSError("settings file is locked")
        ):
            self.assertFalse(controller.saveSettings())
        self.assertIn("保存失败", controller.errorMessage)
        self.assertIn("settings file is locked", controller.errorMessage)

    def test_save_settings_with_empty_hotkey_fails_and_reports_error(self):
        controller, _ = self._make_controller()
        controller.hotkeyText = ""
        self.assertFalse(controller.saveSettings())
        self.assertNotEqual(controller.errorMessage, "")

    def test_save_and_launch_never_launches_when_save_fails(self):
        controller, _ = self._make_controller()
        controller.hotkeyText = ""
        with mock.patch.object(bridge_launcher, "launch_bridge") as fake_launch:
            controller.saveAndLaunch()
        fake_launch.assert_not_called()
        self.assertEqual(controller.launchStatusText, settings_ui.LAUNCH_NOT_STARTED_TEXT)

    def test_save_and_launch_launches_and_reports_started_when_save_succeeds(self):
        controller, _ = self._make_controller()
        fake_result = bridge_launcher.LaunchResult(
            outcome=bridge_launcher.LaunchOutcome.STARTED, command=("exe",), pid=4321
        )
        with mock.patch.object(bridge_launcher, "launch_bridge", return_value=fake_result):
            controller.saveAndLaunch()
        self.assertIn("4321", controller.launchStatusText)
        self.assertNotIn("已连接", controller.launchStatusText)

    def test_restore_defaults_resets_hotkey_and_trigger_mode(self):
        controller, _ = self._make_controller()
        controller.hotkeyText = "shift+z"
        controller.triggerModeIndex = 1
        controller.restoreDefaults()
        self.assertEqual(controller.hotkeyText, hotkey.DEFAULT_VOICE_HOTKEY.serialize())
        self.assertEqual(controller.triggerModeIndex, 0)

    def test_select_button_updates_both_the_controller_and_the_model(self):
        controller, model = self._make_controller()
        controller.selectButton("power")
        self.assertEqual(controller.selectedButtonId, "power")
        self.assertEqual(model.selected_button_id(), "power")

    def test_real_key_detection_selects_captured_button_without_executing_mapping(self):
        controller, model = self._make_controller()
        callbacks = []

        class FakeListener:
            def __init__(self, _button_callback, raw_callback):
                callbacks.append(raw_callback)
                self.started_with = None
                self.stop_calls = 0

            def start(self, device_path):
                self.started_with = device_path

            def stop(self):
                self.stop_calls += 1

        with mock.patch.object(
            qt_settings_app.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["rc003-device-path"],
        ), mock.patch.object(
            qt_settings_app.raw_input_windows.hid_identity,
            "select_single_device_path",
            return_value="rc003-device-path",
        ), mock.patch.object(
            qt_settings_app.raw_input_windows,
            "RawInputButtonListener",
            FakeListener,
        ):
            controller.startKeyDetection()

        self.assertTrue(controller.keyDetectionActive)
        callbacks[0](
            qt_settings_app.raw_input_windows.RawInputEvent(
                source="keyboard",
                is_pressed=True,
                button_id="power",
                vkey=0xFF,
                make_code=0x5E,
                flags=0x0002,
                message=0x0100,
            )
        )
        self.assertFalse(controller.keyDetectionActive)
        self.assertEqual(controller.selectedButtonId, "power")
        self.assertEqual(model.selected_button_id(), "power")
        self.assertIn("电源键", controller.keyDetectionText)
        self.assertIn("0x0066", controller.keyDetectionText)

    def test_real_key_detection_failure_is_reported_in_the_ui(self):
        controller, _ = self._make_controller()
        with mock.patch.object(
            qt_settings_app.raw_input_windows,
            "enumerate_matching_device_paths",
            side_effect=RuntimeError("Raw Input unavailable"),
        ):
            controller.startKeyDetection()
        self.assertFalse(controller.keyDetectionActive)
        self.assertIn("Raw Input unavailable", controller.keyDetectionText)

    def test_open_log_location_reports_honestly_when_never_run(self):
        controller, _ = self._make_controller()
        controller.openLogLocation()
        self.assertIn("尚不存在", controller.statusMessage)

    def test_open_bluetooth_settings_reports_the_uri_it_opened(self):
        controller, _ = self._make_controller()
        fake_result = shell_targets.ExternalTargetResult(
            outcome=shell_targets.ExternalTargetOutcome.OPENED,
            target=shell_targets.BLUETOOTH_SETTINGS_URI,
        )
        with mock.patch.object(
            shell_targets, "open_external_target", return_value=fake_result
        ) as fake_open:
            controller.openBluetoothSettings()
        fake_open.assert_called_once_with(shell_targets.BLUETOOTH_SETTINGS_URI)
        self.assertIn(shell_targets.BLUETOOTH_SETTINGS_URI, controller.statusMessage)

    def test_open_speech_settings_reports_a_failure_honestly(self):
        controller, _ = self._make_controller()
        fake_result = shell_targets.ExternalTargetResult(
            outcome=shell_targets.ExternalTargetOutcome.OPEN_FAILED,
            target=shell_targets.SPEECH_SETTINGS_URI,
            error="no handler registered",
        )
        with mock.patch.object(
            shell_targets, "open_external_target", return_value=fake_result
        ):
            controller.openSpeechSettings()
        self.assertIn("no handler registered", controller.statusMessage)

    def test_select_and_persist_output_endpoint_succeeds_and_updates_options(self):
        controller, _ = self._make_controller()
        result = controller.selectAndPersistOutputEndpoint("CABLE Input", "Windows WASAPI")
        self.assertTrue(result)
        reloaded = config.load_config(config.config_path(controller._config_root))
        self.assertEqual(reloaded["output_endpoint_name"], "CABLE Input")
        self.assertEqual(reloaded["output_endpoint_host_api"], "Windows WASAPI")

    def test_select_and_persist_output_endpoint_returns_false_on_persistence_failure(self):
        # XRBM-031 RETRY 1 item 3: a config-save failure (disk full,
        # permission denied, ...) must never raise out of this Slot and
        # must never be reported as a successful save.
        controller, _ = self._make_controller()
        original_config = dict(controller._config)
        with mock.patch.object(config, "save_config", side_effect=OSError("disk full")):
            result = controller.selectAndPersistOutputEndpoint("CABLE Input", "Windows WASAPI")
        self.assertFalse(result)
        # The in-memory config must not look saved when it was not.
        self.assertEqual(controller._config, original_config)

    def test_select_and_persist_output_endpoint_never_raises_on_unexpected_error(self):
        controller, _ = self._make_controller()
        with mock.patch.object(config, "save_config", side_effect=RuntimeError("boom")):
            result = controller.selectAndPersistOutputEndpoint("CABLE Input", "")
        self.assertFalse(result)

    def test_resolve_autostart_target_in_source_mode_skips_sys_executable(self):
        """Bug-A regression guard. In source mode (sys.frozen unset), the
        candidate branch must NOT pick up sys.executable - that is the
        dev venv's python.exe, not a real autostart target. The function
        must fall through to project-relative probes instead, otherwise
        the installer would write a .lnk that points at a python.exe
        that no longer exists on a different machine.
        """
        # Force source-mode conditions even if the test runner is a
        # frozen build, so the test contract is independent of how
        # ``python -m unittest`` itself is being invoked.
        with mock.patch.object(sys, "frozen", new=None, create=True):
            # Confirm a misleading python.exe .exe path is NOT chosen.
            original_executable = sys.executable
            sys.executable = str(
                Path(self._tmpdir.name) / "totally-not-a-real-exe.exe"
            )
            try:
                # When _resolve_autostart_target() returns None, the
                # setter surfaces a hard error to the user instead of
                # silently writing a bogus .lnk - the bug we are
                # locking in is that source mode no longer trusts
                # sys.executable.
                result = self.Controller._resolve_autostart_target()
            finally:
                # Restore sys.executable so we don't pollute later tests.
                # This is a DIRECT assignment, so mock.patch.object cannot
                # undo it. Leaving it pointing at a nonexistent path made
                # every later subprocess-spawning test in the same run fail
                # with FileNotFoundError [WinError 2] - measured on
                # test_windows_diagnostics, four tests, from a module that
                # never touches sys.executable itself.
                sys.executable = original_executable

        # The test runner is itself python.exe, so the candidate branch
        # WOULD have grabbed it pre-fix. Post-fix, the function must
        # either return a dist/... path or None - never the python.exe
        # path itself.
        if result is not None:
            self.assertNotEqual(
                Path(result).resolve(),
                Path(sys.executable).resolve(),
                "_resolve_autostart_target() leaked sys.executable "
                "into the autostart .lnk target - this would write a "
                ".lnk pointing at python.exe, not RemoteMicRC003.exe",
            )
            # Whatever it returned must be a real exe-shaped path.
            self.assertEqual(
                Path(result).suffix.lower(),
                ".exe",
                f"_resolve_autostart_target() returned a non-exe path: {result}",
            )

    def test_resolve_autostart_target_in_frozen_mode_returns_sys_executable(self):
        """Bug-A companion test. When the app is actually frozen
        (PyInstaller build), sys.executable IS the correct target -
        so the candidate branch must still fire in that mode.
        """
        fake_exe = (
            Path(self._tmpdir.name)
            / "RemoteMicRC003.exe"
        )
        # create=True handles the case where sys.frozen is unset.
        with mock.patch.object(sys, "frozen", True, create=True):
            with mock.patch.object(sys, "executable", str(fake_exe)):
                result = self.Controller._resolve_autostart_target()
        self.assertIsNotNone(
            result,
            "frozen-mode resolution returned None - the candidate branch "
            "did not fire when sys.frozen was True",
        )
        self.assertEqual(
            Path(result).resolve(),
            fake_exe.resolve(),
            "frozen-mode did not return sys.executable as the autostart target",
        )


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class SettingsControllerUntestedSurfaceRegressionTests(unittest.TestCase):
    """The corners of SettingsController the rest of this file never reaches:
    the bridge-handle liveness property, the background autostart status read,
    and the tray surface the controller is allowed to call.

    They sit together because they share one shape - each guards a silent
    failure that produced no exception and no error line: a property that
    reported a stale value, a status read that reported a state it could not
    observe, or a tray call that did not exist.
    """

    def setUp(self):
        classes = qt_settings_app._load_qt_classes()
        self.Model = classes["ButtonMappingModel"]
        self.Controller = classes["SettingsController"]
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": self._tmpdir.name}
        )
        self._env_patch.start()
        # ``_get_bridge_running`` clears the module-level registry, so put it
        # back afterwards - a leaked handle would be stopped by atexit.
        self._original_bridge_handle = qt_settings_app._bridge_handle_global

    def tearDown(self):
        qt_settings_app._bridge_handle_global = self._original_bridge_handle
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def _make_controller(self):
        return self.Controller(self.Model())

    # -- bridgeRunning -----------------------------------------------------

    def test_bridge_running_is_false_without_a_handle(self):
        controller = self._make_controller()
        self.assertIsNone(controller._bridge_handle)
        self.assertFalse(controller.bridgeRunning)

    def test_bridge_running_is_true_for_a_live_handle(self):
        controller = self._make_controller()
        handle = mock.Mock()
        handle.is_alive.return_value = True
        controller._bridge_handle = handle

        self.assertTrue(controller.bridgeRunning)
        # A live handle must be left exactly where it is.
        self.assertIs(controller._bridge_handle, handle)

    def test_bridge_running_is_false_for_a_dead_handle_and_clears_it(self):
        """A bridge that died on its own must flip the QML property back.

        Nothing else notices a bridge that exits by itself, so if this property
        kept reporting True the window would offer a "stop" button for a
        process that is already gone. The dead handle is also dropped from the
        module registry, which is what ``stopBridge`` and the atexit hook read.
        """

        controller = self._make_controller()
        handle = mock.Mock()
        handle.is_alive.return_value = False
        controller._bridge_handle = handle
        qt_settings_app._bridge_handle_global = handle

        self.assertFalse(controller.bridgeRunning)
        self.assertIsNone(controller._bridge_handle)
        self.assertIsNone(
            qt_settings_app._bridge_handle_global,
            "the dead handle is still registered - the atexit hook would try "
            "to stop a process that is already gone",
        )

    # -- the background autostart status read ------------------------------

    def test_a_second_refresh_while_one_is_in_flight_is_a_no_op(self):
        """One query at a time.

        Asking Task Scheduler spawns ``powershell.exe`` and costs ~1.2 s, and
        the window asks for the state on every show, so overlapping queries
        would pile up processes. A call while one is already running must do
        nothing at all.
        """

        controller = self._make_controller()
        started = threading.Event()
        release = threading.Event()
        calls = []

        def blocking_get_state_dict():
            calls.append(1)
            started.set()
            release.wait(5.0)
            return {"status_text": "on", "is_error": False, "enabled": True}

        with mock.patch.object(
            qt_settings_app.elevated_autostart,
            "get_state_dict",
            side_effect=blocking_get_state_dict,
        ):
            controller._refresh_start_with_windows_status()
            self.assertTrue(started.wait(5.0), "the first refresh never started")

            # Both entry points, while the first query is still blocked.
            controller._refresh_start_with_windows_status()
            controller.refreshStartWithWindowsStatus()

            release.set()
            self.assertTrue(
                _pump_until(lambda: not controller._autostart_query_in_flight),
                "the in-flight guard was never released",
            )

        self.assertEqual(
            len(calls), 1, "a second Task Scheduler query was started"
        )

    def test_status_refresh_keeps_the_switch_put_when_the_state_is_unreadable(self):
        """Never invent a state we could not observe.

        When the query fails there is no ``enabled`` answer, and the switch must
        stay exactly where it was - only the status line reports the failure.
        Flipping it to False would tell the user autostart is off when we simply
        do not know, and the next save would then write that guess to disk.
        """

        controller = self._make_controller()
        controller._start_with_windows = True

        with mock.patch.object(
            qt_settings_app.elevated_autostart,
            "get_state_dict",
            side_effect=RuntimeError("Task Scheduler is unreachable"),
        ):
            # assertLogs also swallows the traceback the worker logs, so the
            # failure is asserted rather than merely printed.
            with self.assertLogs("ovb_rc003.qt_settings_app", level="ERROR"):
                controller._refresh_start_with_windows_status()
                self.assertTrue(
                    _pump_until(lambda: not controller._autostart_query_in_flight),
                    "the refresh never reported back",
                )

        self.assertTrue(
            controller.startWithWindows,
            "the switch was moved on a state that could not be read",
        )
        self.assertTrue(controller.startWithWindowsStatusIsError)
        self.assertTrue(controller.startWithWindowsStatusText)

        # Control: a state we CAN read does move the switch, so the assertion
        # above is about the unreadable case and not about the slot being inert.
        controller._on_autostart_state_ready("已关闭", False, False)
        self.assertFalse(controller.startWithWindows)

    def test_status_refresh_does_not_call_a_missing_tray_method(self):
        """Every tray method the controller reaches for must exist.

        The status refresh used to call a tray helper that had been removed
        (``set_shortcut_active``). The AttributeError was swallowed and
        surfaced only as a status line, so the window looked healthy while the
        tray integration was dead. This checks every
        ``self._tray_controller.<name>`` reference against the real class, then
        drives the refresh for real.
        """

        referenced = set(_TRAY_METHOD_REFERENCE.findall(_qt_settings_source()))
        self.assertTrue(
            referenced, "the tray-reference scan found nothing - it cannot fail"
        )
        missing = sorted(
            name for name in referenced if not hasattr(tray_icon.TrayController, name)
        )
        self.assertEqual(
            missing,
            [],
            "SettingsController calls tray methods TrayController does not "
            f"have: {missing}",
        )

        controller = self._make_controller()
        with mock.patch.object(
            qt_settings_app.elevated_autostart,
            "get_state_dict",
            return_value={
                "status_text": "开机自启动已启用。",
                "is_error": False,
                "enabled": True,
            },
        ):
            controller._refresh_start_with_windows_status()
            self.assertTrue(
                _pump_until(lambda: not controller._autostart_query_in_flight),
                "the refresh never reported back",
            )

        self.assertFalse(controller.startWithWindowsStatusIsError)
        self.assertTrue(controller.startWithWindows)

    def test_tray_controller_has_no_set_shortcut_active(self):
        """``set_shortcut_active`` was removed from the tray and must stay gone.

        Asserting its absence is what makes the check above a live guard rather
        than a description of the current code: if the method were restored
        without also restoring its call site (or vice versa) this fails instead
        of the mismatch hiding behind a swallowed AttributeError.
        """

        self.assertFalse(
            hasattr(tray_icon.TrayController, "set_shortcut_active"),
            "set_shortcut_active is back on TrayController - either wire it up "
            "properly or remove it again, but do not leave it half-restored",
        )
        self.assertNotIn(
            "set_shortcut_active",
            _qt_settings_source(),
            "the controller references a tray method that no longer exists",
        )


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class OpenLogFileControllerTests(unittest.TestCase):
    """``openLogFile()`` QML slot must call ``logging_setup.open_log_file``
    (not ``open_log_location``) and surface the result via the
    controller's status-message property.

    Sits next to the diagnostics controller tests but uses a simpler
    headless setup (no QGuiApplication needed - the controller is
    constructed and the slot is invoked directly).
    """

    def setUp(self):
        classes = qt_settings_app._load_qt_classes()
        self.Model = classes["ButtonMappingModel"]
        self.Controller = classes["SettingsController"]
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": self._tmpdir.name}
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_open_log_file_slot_invokes_open_log_file_and_publishes_status(self):
        from ovb_rc003 import logging_setup as ls
        from ovb_rc003 import settings_ui as sui

        # Real log dir + file exists.
        log_dir = self._tmpdir.name + "\\logs"
        Path(log_dir).mkdir()
        log_file = Path(log_dir) / "app.log"
        log_file.write_text("entry\n", encoding="utf-8")

        captured: list = []
        fake_result = ls.LogOpenResult(
            outcome=ls.LogOpenOutcome.OPENED,
            location=ls.LogLocation(
                status=ls.LogLocationStatus.READY,
                directory=Path(log_dir),
                file_path=log_file,
            ),
        )

        with mock.patch.object(
            ls, "open_log_file", return_value=fake_result
        ) as fake_open_file:
            model = self.Model()
            controller = self.Controller(model)
            # Capture the message the controller would show in the status
            # bar (real QML will bind to ``statusMessage``).
            with mock.patch.object(
                controller, "_set_status_message", side_effect=captured.append
            ) as fake_status:
                controller.openLogFile()

        fake_open_file.assert_called_once_with()
        fake_status.assert_called_once()
        # Status text comes from settings_ui.describe_log_open_result,
        # which we trust (its own unit tests cover all four outcomes).
        msg = captured[0]
        self.assertIn("已打开", msg)
        self.assertEqual(
            sui.describe_log_open_result(fake_result),
            msg,
        )

    def test_open_log_file_slot_distinct_from_open_log_location_slot(self):
        """Sanity: the slot exists and is named ``openLogFile`` (not
        ``openLogLocation``). Catches accidental rename regressions."""
        controller = self.Controller(self.Model())
        self.assertTrue(hasattr(controller, "openLogFile"))
        self.assertTrue(callable(controller.openLogFile))
        # And the existing slot is still there.
        self.assertTrue(hasattr(controller, "openLogLocation"))


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class DiagnosticsControllerTests(unittest.TestCase):
    """DiagnosticsController (XRBM-031) runs every check on a background
    thread and delivers results via a cross-thread Qt signal - unlike
    SettingsControllerTests above, these tests need a real (offscreen)
    QGuiApplication instance so ``processEvents()`` can actually pump the
    queued cross-thread delivery; constructing one here never touches
    QQuickStyle/QML, so it does not conflict with the separate
    once-per-process QQuickStyle constraint the QML-engine subprocess tests
    below work around.
    """

    def setUp(self):
        classes = qt_settings_app._load_qt_classes()
        self.Model = classes["ButtonMappingModel"]
        self.SettingsController = classes["SettingsController"]
        self.DiagnosticsController = classes["DiagnosticsController"]
        self.app = qt_app.get_application()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(os.environ, {"LOCALAPPDATA": self._tmpdir.name})
        self._env_patch.start()
        # Matches config_root() with the SAME LOCALAPPDATA-derived path
        # SettingsController itself computes internally (config.config_root()),
        # NOT the bare tmpdir - otherwise DiagnosticsController would read/
        # write a different directory than the one SettingsController's own
        # config actually lives in.
        self._config_root = config.config_root()
        # Defensive (XRBM-031 RETRY 2): this is process-global, persistent
        # state - never start a test with it left set by a previous
        # test/failure, and never leave it set for the next one.
        qt_settings_app._diagnostics_shutdown_event.clear()

    def tearDown(self):
        qt_settings_app._diagnostics_shutdown_event.clear()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def _make_settings_controller(self):
        model = self.Model()
        return self.SettingsController(model)

    def _pump_until(self, predicate, timeout_seconds=5.0):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_initial_construction_starts_a_refresh_that_completes(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self.assertTrue(diag.isRefreshing)
        self.assertTrue(self._pump_until(lambda: not diag.isRefreshing))
        # 21be9ce added windows_default_input; this counter must follow.
        self.assertEqual(len(diag.checkResults), 8)
        ids = {row["checkId"] for row in diag.checkResults}
        self.assertIn("dictation", ids)

    def test_check_results_never_contain_a_placeholder_raw_path_or_address(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        for row in diag.checkResults:
            self.assertNotIn("VID_", row["detail"])
            self.assertNotIn("\\\\?\\", row["detail"])

    def test_unexpected_report_failure_clears_stale_results_and_shows_a_page_error(self):
        # XRBM-031 RETRY 1 item 2: a background-thread report failure (e.g.
        # windows_diagnostics.run_diagnostics() itself raising, despite its
        # own per-check isolation) must never leave a PREVIOUS successful
        # run's rows on screen looking current, and must surface a clear,
        # page-level error - not only a driver-card message below the fold.
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self.assertTrue(self._pump_until(lambda: not diag.isRefreshing))
        # 21be9ce added windows_default_input; this counter must follow.
        self.assertEqual(len(diag.checkResults), 8)
        self.assertEqual(diag.diagnosticsErrorMessage, "")

        with mock.patch.object(
            windows_diagnostics, "run_diagnostics", side_effect=RuntimeError("boom")
        ):
            diag.refreshDiagnostics()
            self.assertTrue(self._pump_until(lambda: not diag.isRefreshing))

        self.assertEqual(diag.checkResults, [])
        self.assertNotEqual(diag.diagnosticsErrorMessage, "")
        self.assertIn("重新检测", diag.diagnosticsErrorMessage)

    def test_successful_refresh_after_a_failure_clears_the_page_error(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        with mock.patch.object(
            windows_diagnostics, "run_diagnostics", side_effect=RuntimeError("boom")
        ):
            diag.refreshDiagnostics()
            self._pump_until(lambda: not diag.isRefreshing)
        self.assertNotEqual(diag.diagnosticsErrorMessage, "")

        diag.refreshDiagnostics()
        self.assertTrue(self._pump_until(lambda: not diag.isRefreshing))
        self.assertEqual(diag.diagnosticsErrorMessage, "")
        # 21be9ce added windows_default_input; this counter must follow.
        self.assertEqual(len(diag.checkResults), 8)

    def test_worker_thread_is_deregistered_once_finished(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        self.assertTrue(
            self._pump_until(lambda: len(qt_settings_app._diagnostics_threads) == 0)
        )

    def test_repeated_refresh_click_is_ignored_while_a_check_is_already_running(self):
        release_event = threading.Event()
        call_count = {"n": 0}

        def _blocking_run_diagnostics(**kwargs):
            call_count["n"] += 1
            release_event.wait(timeout=5.0)
            return windows_diagnostics.DiagnosticsReport(checks=())

        settings_controller = self._make_settings_controller()
        with mock.patch.object(
            windows_diagnostics, "run_diagnostics", side_effect=_blocking_run_diagnostics
        ):
            diag = self.DiagnosticsController(settings_controller, self._config_root)
            # __init__ already started one worker, currently blocked on
            # release_event - both of these must be no-ops, never a second
            # concurrent call into the (blocking) fake.
            diag.refreshDiagnostics()
            diag.refreshDiagnostics()
            release_event.set()
            self.assertTrue(self._pump_until(lambda: not diag.isRefreshing))
        self.assertEqual(call_count["n"], 1)

    def test_no_worker_thread_survives_after_completion(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        self._pump_until(lambda: len(qt_settings_app._diagnostics_threads) == 0)
        for thread in list(qt_settings_app._diagnostics_threads):
            self.assertFalse(thread.is_alive())

    def test_refresh_diagnostics_refuses_to_start_once_shutdown_has_begun(self):
        # XRBM-031 RETRY 2: once process shutdown is flagged, a "重新检测"
        # click (or anything else calling refreshDiagnostics()) must be a
        # no-op - there is nothing left alive to usefully deliver a result
        # to, and starting a new thread this late only works against the
        # atexit hook's own bounded join.
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        self._pump_until(lambda: len(qt_settings_app._diagnostics_threads) == 0)

        qt_settings_app._diagnostics_shutdown_event.set()
        diag.refreshDiagnostics()
        # Give any (incorrectly) started worker a real chance to flip
        # isRefreshing/register itself before asserting neither happened.
        self.app.processEvents()
        self.assertFalse(diag.isRefreshing)
        self.assertEqual(len(qt_settings_app._diagnostics_threads), 0)

    def test_worker_already_running_skips_emit_once_shutdown_begins_mid_run(self):
        # XRBM-031 RETRY 2: a worker that was already running when shutdown
        # began must notice the flag and skip delivering its result,
        # rather than emitting into a DiagnosticsController/Qt runtime that
        # may already be mid-teardown by the time it finishes.
        release_event = threading.Event()

        def _blocking_run_diagnostics(**kwargs):
            release_event.wait(timeout=5.0)
            return windows_diagnostics.DiagnosticsReport(checks=())

        settings_controller = self._make_settings_controller()
        with mock.patch.object(
            windows_diagnostics, "run_diagnostics", side_effect=_blocking_run_diagnostics
        ):
            diag = self.DiagnosticsController(settings_controller, self._config_root)
            # __init__ already started one worker, currently blocked.
            self.assertTrue(diag.isRefreshing)

            qt_settings_app._diagnostics_shutdown_event.set()
            release_event.set()
            # The worker still finishes and deregisters itself (proven via
            # the registry, since the - correctly skipped - emit means
            # isRefreshing/checkResults are never touched by
            # _on_diagnostics_ready and so cannot be used as the signal
            # here).
            self.assertTrue(
                self._pump_until(lambda: len(qt_settings_app._diagnostics_threads) == 0)
            )
        # The skipped emit is exactly why these are still in their
        # constructed-but-never-updated state.
        self.assertTrue(diag.isRefreshing)
        self.assertEqual(diag.checkResults, [])

    def test_registry_is_cleaned_up_even_if_emit_raises(self):
        # XRBM-031 RETRY 2: a receiver/Qt runtime that is already tearing
        # down could still make the emit call itself raise (the shutdown-
        # flag check narrows this window but cannot close it perfectly) -
        # the worker thread must still deregister itself from
        # _diagnostics_threads regardless, via its `finally` block.
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)
        self._pump_until(lambda: len(qt_settings_app._diagnostics_threads) == 0)

        with mock.patch.object(
            diag, "_emit_diagnostics_ready", side_effect=RuntimeError("boom")
        ):
            diag.refreshDiagnostics()
            self.assertTrue(
                self._pump_until(lambda: len(qt_settings_app._diagnostics_threads) == 0)
            )
        # The worker thread itself never crashed the process/left a
        # dangling registry entry despite the injected emit failure.
        self.assertEqual(len(qt_settings_app._diagnostics_threads), 0)

    def test_shutdown_helper_actually_kills_a_hanging_ble_diagnostics_subprocess_and_worker_never_emits(self):
        # XRBM-035 RETRY 1: end-to-end reproduction of the real Windows CI
        # crash cause - a hung WinRT BLE discovery still running when the
        # process started exiting - now using the REAL, unmodified
        # windows_diagnostics.run_diagnostics()/check_ble_candidate()/
        # _discover_ble_candidates_sync()/_run_ble_diagnostics_subprocess()
        # call chain. Only the CHILD PROCESS COMMAND itself is replaced
        # (build_ble_diagnostics_subprocess_command()) with one that spawns
        # a genuine, artificially-hanging OS process instead of the real
        # ovb_rc003 entrypoint - discovery still runs in a real, separate
        # process, and the real terminate()/kill()/wait() escalation this
        # RETRY exists for is exercised for real, through the production
        # _shutdown_diagnostics_workers() helper - the same one
        # run_settings_window() and the real QML load probe both call.
        hang_script = "import time\ntime.sleep(120)\n"

        def _fake_command(result_path, **kwargs):
            return [sys.executable, "-c", hang_script]

        settings_controller = self._make_settings_controller()
        with mock.patch.object(
            windows_diagnostics, "build_ble_diagnostics_subprocess_command", _fake_command
        ):
            diag = self.DiagnosticsController(settings_controller, self._config_root)
            self.assertTrue(diag.isRefreshing)
            self.app.processEvents()

            # Give the worker thread a real chance to actually spawn the
            # hanging child before shutdown is requested - not strictly
            # required for correctness (the subprocess module handles a
            # not-yet-spawned/racing spawn fine either way), but makes this
            # test reliably exercise the "kill an already-running child"
            # path rather than sometimes short-circuiting before spawn.
            time.sleep(0.2)

            started = time.monotonic()
            qt_settings_app._shutdown_diagnostics_workers()
            elapsed = time.monotonic() - started

        self.assertLess(
            elapsed,
            qt_settings_app._DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS + 1.0,
            "shutdown must bound-wait, not hang on the killed worker",
        )
        self.assertEqual(len(qt_settings_app._diagnostics_threads), 0, "the worker thread must have finished")
        # The worker was cancelled before it could finish - its (never
        # produced) result must never have been emitted, matching the
        # never-emit-after-shutdown contract the RETRY 2 tests above cover
        # for the non-hanging case.
        self.assertTrue(diag.isRefreshing)
        self.assertEqual(diag.checkResults, [])

    def test_select_detected_cable_input_persists_via_settings_controller(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        endpoint = audio_output.AudioEndpoint(name="CABLE Input", host_api="Windows WASAPI")
        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=[endpoint]
        ):
            result = diag.selectDetectedCableInputAsOutput()

        self.assertTrue(result)
        self.assertIn("CABLE Input", diag.driverStatusMessage)
        # Persisted for real - reloading config from disk shows the change.
        reloaded = config.load_config(config.config_path(self._config_root))
        self.assertEqual(reloaded["output_endpoint_name"], "CABLE Input")
        self.assertEqual(reloaded["output_endpoint_host_api"], "Windows WASAPI")

    def test_select_detected_cable_input_fails_closed_when_none_found(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        with mock.patch.object(audio_output, "enumerate_output_endpoints", return_value=[]):
            result = diag.selectDetectedCableInputAsOutput()

        self.assertFalse(result)
        self.assertIn("未检测到", diag.driverErrorMessage)

    def test_select_detected_cable_input_reports_an_honest_error_on_persistence_failure(self):
        # XRBM-031 RETRY 1 item 3: a config persistence failure must never
        # raise out of this Slot, must never be reported as a successful
        # save, and must never expose a local path/device identifier.
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        endpoint = audio_output.AudioEndpoint(name="CABLE Input", host_api="Windows WASAPI")
        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=[endpoint]
        ):
            with mock.patch.object(
                settings_controller,
                "selectAndPersistOutputEndpoint",
                side_effect=RuntimeError("boom"),
            ):
                result = diag.selectDetectedCableInputAsOutput()

        self.assertFalse(result)
        self.assertNotIn("boom", diag.driverErrorMessage)
        self.assertEqual(diag.driverStatusMessage, "")
        error_lower = diag.driverErrorMessage.lower()
        self.assertNotIn(str(self._config_root).lower(), error_lower)

    def test_select_detected_cable_input_reports_an_honest_error_when_persistence_returns_false(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        endpoint = audio_output.AudioEndpoint(name="CABLE Input", host_api="Windows WASAPI")
        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=[endpoint]
        ):
            with mock.patch.object(
                settings_controller, "selectAndPersistOutputEndpoint", return_value=False
            ):
                result = diag.selectDetectedCableInputAsOutput()

        self.assertFalse(result)
        self.assertNotEqual(diag.driverErrorMessage, "")
        self.assertEqual(diag.driverStatusMessage, "")

    def test_select_detected_cable_input_fails_closed_when_ambiguous(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        endpoints = [
            audio_output.AudioEndpoint(name="CABLE Input", host_api="A"),
            audio_output.AudioEndpoint(name="CABLE Input", host_api="B"),
        ]
        with mock.patch.object(
            audio_output, "enumerate_output_endpoints", return_value=endpoints
        ):
            result = diag.selectDetectedCableInputAsOutput()

        self.assertFalse(result)
        self.assertIn("无法唯一确定", diag.driverErrorMessage)

    def test_launch_vb_cable_setup_reports_bundle_not_found_as_an_error(self):
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        with mock.patch.object(
            vb_cable_bundle,
            "prepare_and_launch_vendor_setup",
            side_effect=vb_cable_bundle.BundleNotFoundError("no bundle"),
        ):
            diag.launchVbCableSetup()

        self.assertIn("未找到", diag.driverErrorMessage)

    def test_launch_vb_cable_setup_reports_uac_cancellation_as_neutral_info_not_error_or_success(self):
        # XRBM-031 RETRY 1 item 7: a UAC cancellation must never render in
        # the success/green color (driverStatusMessage) - it is neutral
        # informational text (driverInfoMessage) instead.
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        with mock.patch.object(
            vb_cable_bundle,
            "prepare_and_launch_vendor_setup",
            side_effect=vb_cable_bundle.UacCancelledError("用户取消了 UAC 提升请求；未安装任何内容。"),
        ):
            diag.launchVbCableSetup()

        self.assertEqual(diag.driverErrorMessage, "")
        self.assertEqual(diag.driverStatusMessage, "")
        self.assertIn("取消", diag.driverInfoMessage)

    def test_launch_vb_cable_setup_success_is_neutral_info_never_claims_installation_succeeded(self):
        # XRBM-031 RETRY 1 item 7: launching the vendor UI is informational
        # (driverInfoMessage), not a completed success (driverStatusMessage)
        # - only a later endpoint recheck can confirm an actual install.
        settings_controller = self._make_settings_controller()
        diag = self.DiagnosticsController(settings_controller, self._config_root)
        self._pump_until(lambda: not diag.isRefreshing)

        with mock.patch.object(
            vb_cable_bundle, "prepare_and_launch_vendor_setup", return_value=None
        ):
            diag.launchVbCableSetup()

        self.assertEqual(diag.driverErrorMessage, "")
        self.assertEqual(diag.driverStatusMessage, "")
        self.assertIn("重新检测", diag.driverInfoMessage)
        self.assertNotIn("安装成功", diag.driverInfoMessage)


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class RunSettingsWindowShutdownCoverageTests(unittest.TestCase):
    """XRBM-035 RETRY 1 P2/E: ``run_settings_window()``'s production
    shutdown contract must cover every exit path starting right after
    ``DiagnosticsController`` is constructed (that constructor already
    started a real background worker) - not only ``app.exec()`` returning
    normally, which is all the previous round's ``try/finally`` covered.
    Drives the REAL function end to end (never a source-level/AST proxy) -
    only the Qt WINDOW plumbing (``QGuiApplication``/``QQmlApplicationEngine``
    /``QQuickStyle``/``QUrl``/``qmlRegisterSingletonInstance``) is replaced
    with small, fully-controllable fakes (real PySide6/shiboken C++ types
    are not reliably monkeypatchable, and no real QML window needs to exist
    to prove this contract) - ``ButtonMappingModel``/``SettingsController``/
    ``DiagnosticsController`` stay the REAL classes ``_load_qt_classes()``
    itself already produced, so the background worker this test is actually
    about is completely real.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(os.environ, {"LOCALAPPDATA": self._tmpdir.name})
        self._env_patch.start()
        qt_settings_app._diagnostics_shutdown_event.clear()

    def tearDown(self):
        qt_settings_app._diagnostics_shutdown_event.clear()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def _fake_classes(self, *, load_side_effect=None, root_objects=None, exec_return=0):
        real_classes = qt_settings_app._load_qt_classes()
        root_objects = [] if root_objects is None else root_objects

        class _FakeEngine:
            def addImportPath(self, path):
                pass

            def load(self, url):
                if load_side_effect is not None:
                    raise load_side_effect

            def rootObjects(self):
                return root_objects

        class _FakeApp:
            def __init__(self, argv=None):
                pass

            @staticmethod
            def instance():
                return None

            @staticmethod
            def setStyle(name):
                # run_settings_window() calls app.setStyle("Fusion") whenever
                # the app it built is a real QApplication - which this fake is
                # from its own point of view, so the call must be absorbed.
                pass

            def exec(self):
                return exec_return

        class _FakeQQuickStyle:
            @staticmethod
            def setStyle(name):
                pass

        class _FakeQUrl:
            @staticmethod
            def fromLocalFile(path):
                return path

        fake_classes = dict(real_classes)
        # BOTH application keys must be faked. run_settings_window() reads
        # classes["QApplication"] to build the app (it needs QtWidgets for the
        # tray's QMenu/QSystemTrayIcon) and only reaches for QGuiApplication
        # elsewhere. Faking the latter alone leaves the REAL QApplication in
        # place, so app.exec() enters a real Qt event loop and this test blocks
        # forever - 0% CPU, no child process, no failure, no report.
        fake_classes["QApplication"] = _FakeApp
        fake_classes["QGuiApplication"] = _FakeApp
        fake_classes["QQmlApplicationEngine"] = _FakeEngine
        fake_classes["QQuickStyle"] = _FakeQQuickStyle
        fake_classes["QUrl"] = _FakeQUrl
        fake_classes["qmlRegisterSingletonInstance"] = lambda *args, **kwargs: None
        return fake_classes

    def test_engine_load_raising_still_runs_the_production_shutdown_helper(self):
        fake_classes = self._fake_classes(load_side_effect=RuntimeError("simulated engine.load() failure"))
        with mock.patch.object(qt_settings_app, "_load_qt_classes", return_value=fake_classes):
            with mock.patch.object(
                qt_settings_app,
                "_shutdown_diagnostics_workers",
                wraps=qt_settings_app._shutdown_diagnostics_workers,
            ) as shutdown_spy:
                with self.assertRaises(RuntimeError):
                    qt_settings_app.run_settings_window()

        shutdown_spy.assert_called()
        self.assertEqual(len(qt_settings_app._diagnostics_threads), 0)

    def test_empty_root_objects_still_runs_the_production_shutdown_helper(self):
        fake_classes = self._fake_classes(root_objects=[])
        with mock.patch.object(qt_settings_app, "_load_qt_classes", return_value=fake_classes):
            with mock.patch.object(
                qt_settings_app,
                "_shutdown_diagnostics_workers",
                wraps=qt_settings_app._shutdown_diagnostics_workers,
            ) as shutdown_spy:
                with self.assertRaises(qt_settings_app.QtUnavailableError):
                    qt_settings_app.run_settings_window()

        shutdown_spy.assert_called()
        self.assertEqual(len(qt_settings_app._diagnostics_threads), 0)

    def test_app_exec_returning_normally_still_runs_the_production_shutdown_helper(self):
        fake_classes = self._fake_classes(root_objects=[object()], exec_return=0)
        with mock.patch.object(qt_settings_app, "_load_qt_classes", return_value=fake_classes):
            with mock.patch.object(
                qt_settings_app,
                "_shutdown_diagnostics_workers",
                wraps=qt_settings_app._shutdown_diagnostics_workers,
            ) as shutdown_spy:
                exit_code = qt_settings_app.run_settings_window()

        self.assertEqual(exit_code, 0)
        shutdown_spy.assert_called()
        self.assertEqual(len(qt_settings_app._diagnostics_threads), 0)


# Loads the REAL qml/main.qml (not a stand-in snippet) with QT_QPA_
# PLATFORM=offscreen and reports rootObjects()/warnings/window size as
# JSON - run in an isolated subprocess (see OffscreenQmlLoadTests below for
# why: a second QQmlApplicationEngine loading ComboBox-containing QML
# within the SAME process as another engine that already loaded one -
# regardless of matching style - empirically broke with "Type ComboBox
# unavailable" / "TextEditingContextMenu unavailable", a separate QQC2
# per-process-singleton limitation from the FluentWinUI3 Config one
# documented on RenderedContrastTests above).
_QML_LOAD_PROBE_SCRIPT = r"""
import faulthandler
import json
import sys

# XRBM-034's "engine.warnings connected to a Python callback" theory for
# this probe's 0xC0000005 was disproven by real Windows CI evidence
# (XRBM-034 REPLAN, run 29681031609): a named-callback connect()/
# disconnect() pair still crashed AFTER printing STAGE:connected/loaded/
# disconnected, and faulthandler's own thread dump showed the actual crash
# thread deep inside ble_transport_winrt.discover_candidates()'s WinRT
# await - a background DiagnosticsController worker this script starts
# below, not anything QML-warnings-related. faulthandler.enable() is kept
# (XRBM-035 In-scope item 6) purely as cheap forensic insurance if this
# script ever fails again - it costs nothing on the passing path.
faulthandler.enable()

from ovb_rc003 import qt_settings_app as m

classes = m._load_qt_classes()
QGuiApplication = classes["QGuiApplication"]
QQmlApplicationEngine = classes["QQmlApplicationEngine"]
QQuickStyle = classes["QQuickStyle"]
QUrl = classes["QUrl"]
qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
ButtonMappingModel = classes["ButtonMappingModel"]
SettingsController = classes["SettingsController"]
DiagnosticsController = classes["DiagnosticsController"]

QQuickStyle.setStyle("Basic")
app = QGuiApplication.instance() or QGuiApplication([])
model = ButtonMappingModel()
controller = SettingsController(model)
diagnostics_controller = DiagnosticsController(controller, m.config.config_root())
qmlRegisterSingletonInstance(SettingsController, "OvbRc003Settings", 1, 0, "SettingsController", controller)
qmlRegisterSingletonInstance(ButtonMappingModel, "OvbRc003Settings", 1, 0, "ButtonMappingModel", model)
qmlRegisterSingletonInstance(DiagnosticsController, "OvbRc003Settings", 1, 0, "DiagnosticsController", diagnostics_controller)

engine = QQmlApplicationEngine()
qml_dir = m._qml_directory()
engine.addImportPath(str(qml_dir))

warnings = []
engine.warnings.connect(lambda ws: warnings.extend(ws))
print("STAGE:connected", file=sys.stderr, flush=True)

engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
print("STAGE:loaded", file=sys.stderr, flush=True)

root_objects = engine.rootObjects()
result = {
    "root_count": len(root_objects),
    "warnings": [w.toString() for w in warnings],
    "width": root_objects[0].property("width") if root_objects else None,
    "height": root_objects[0].property("height") if root_objects else None,
}

# XRBM-035: the real fast-close gate this probe exists to be - calls the
# EXACT SAME production shutdown helper run_settings_window() calls right
# after app.exec() returns (see qt_settings_app.py's module docstring and
# _shutdown_diagnostics_workers()'s own docstring), while every Qt/Python
# object built above (including the DiagnosticsController constructed by
# _load_qt_classes()/DiagnosticsController(...) above, which already
# started a REAL background BLE-discovery worker thread in its own
# __init__ - never faked/skipped here) is still fully alive. This is what
# actually reproduces the real settings-window-closes-quickly race, and
# actually exercises the fix for it.
m._shutdown_diagnostics_workers()
print("STAGE:shutdown", file=sys.stderr, flush=True)

print(json.dumps(result))
"""


_DJI_DEVICE_PAGE_PROBE_SCRIPT = r"""
import json

from ovb_rc003 import qt_settings_app as m


def find_child(root, name):
    for child in root.children():
        if child.objectName() == name:
            return child
        found = find_child(child, name)
        if found is not None:
            return found
    return None


classes = m._load_qt_classes()
QGuiApplication = classes["QGuiApplication"]
QQmlApplicationEngine = classes["QQmlApplicationEngine"]
QQuickStyle = classes["QQuickStyle"]
QUrl = classes["QUrl"]
qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
ButtonMappingModel = classes["ButtonMappingModel"]
SettingsController = classes["SettingsController"]
DiagnosticsController = classes["DiagnosticsController"]

QQuickStyle.setStyle("Basic")
app = QGuiApplication.instance() or QGuiApplication([])
model = ButtonMappingModel()
controller = SettingsController(model)
controller.selectedDeviceIndex = controller._DEVICE_ORDER.index(m.device_catalog.DJI_MIC_2_ID)
diagnostics_controller = DiagnosticsController(controller, m.config.config_root())
qmlRegisterSingletonInstance(SettingsController, "OvbRc003Settings", 1, 0, "SettingsController", controller)
qmlRegisterSingletonInstance(ButtonMappingModel, "OvbRc003Settings", 1, 0, "ButtonMappingModel", model)
qmlRegisterSingletonInstance(DiagnosticsController, "OvbRc003Settings", 1, 0, "DiagnosticsController", diagnostics_controller)

engine = QQmlApplicationEngine()
qml_dir = m._qml_directory()
engine.addImportPath(str(qml_dir))
engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
assert len(engine.rootObjects()) == 1, "main.qml failed to load"
window = engine.rootObjects()[0]
window.show()
tab_bar = find_child(window, "tabBar")
assert tab_bar is not None
# Tab order is now 通用/连接/按键映射/权限/检查与修复 (GeneralPage was added
# as index 0), so the 按键映射 tab lives at index 2. Probe by objectName
# instead of positional index so the test survives future re-orders - there is
# deliberately no index-based fallback, since a stale index here silently
# selects the wrong page and makes the probe assert about the wrong widgets
# (see tests/test_ui_probe_stable_selectors.py).
mapping_tab_button = find_child(window, "mappingTabButton")
assert mapping_tab_button is not None, "mappingTabButton not found"
mapping_tab_button.setProperty("checked", True)
for _ in range(10):
    window.grabWindow()
    app.processEvents()

dji_layout = find_child(window, "djiControlLayout")
rc003_layout = find_child(window, "rc003MappingLayout")
assert dji_layout is not None
assert rc003_layout is not None
result = {
    "dji_visible": bool(dji_layout.property("visible")),
    "rc003_visible": bool(rc003_layout.property("visible")),
    "mapping_page_title": controller.mappingPageTitle,
    "control_names": [row["name"] for row in controller.djiControlRows],
}
m._shutdown_diagnostics_workers()
print(json.dumps(result))
"""


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class OffscreenQmlLoadTests(unittest.TestCase):
    """Loads the REAL qml/main.qml (not a stand-in snippet), in an isolated
    subprocess (see ``_QML_LOAD_PROBE_SCRIPT`` above), and fails the test on
    ANY QML warning - this is the same check this task's own screenshot
    step relies on, so a future change that breaks the singleton wiring (see
    qt_settings_app.py and main.qml's module docstrings for the exact Qt
    Quick Controls internal-property-name collision this works around)
    fails fast in CI instead of only being noticed visually.
    """

    def test_main_qml_loads_with_zero_warnings_and_a_reasonable_window_size(self):
        import json
        import subprocess

        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env["LOCALAPPDATA"] = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, "-c", _QML_LOAD_PROBE_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode, 0, f"QML load probe subprocess failed: {result.stderr}"
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])

        self.assertEqual(data["root_count"], 1, "main.qml failed to instantiate")
        self.assertEqual(
            data["warnings"], [], "main.qml produced QML warnings/errors during load"
        )
        self.assertGreater(data["width"], 0)
        self.assertGreater(data["height"], 0)

    def test_dji_device_page_hides_rc003_mapping_and_shows_dji_controls(self):
        import json
        import subprocess

        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env["LOCALAPPDATA"] = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, "-c", _DJI_DEVICE_PAGE_PROBE_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode, 0, f"DJI QML page probe failed: {result.stderr}"
        )
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(data["dji_visible"])
        self.assertFalse(data["rc003_visible"])
        self.assertEqual(data["mapping_page_title"], "设备控制")
        self.assertEqual(data["control_names"], ["录音键", "连接键", "电源键"])


class QmlLoadProbeCallsProductionShutdownHelperTests(unittest.TestCase):
    """XRBM-035: a source-level, PySide6-independent regression guard
    (never touches Qt, matching ``DiagnosticsShutdownOrderingTests``/
    ``OffscreenQmlLoadTests`` above) that ``_QML_LOAD_PROBE_SCRIPT`` calls
    the SAME production shutdown helper ``run_settings_window()`` calls
    right after ``app.exec()`` returns - see that function and
    ``_shutdown_diagnostics_workers()``'s own docstrings. Without this, a
    future edit could silently start relying on this module's ``atexit``
    hook alone again (the exact contract a real Windows CI crash already
    disproved), or start mocking/skipping the real BLE discovery this probe
    deliberately still exercises - defeating the whole point of this being
    a REAL fast-close race reproduction rather than a stand-in for one.
    """

    def test_probe_script_calls_the_production_shutdown_helper_before_printing_result(self):
        self.assertIn("m._shutdown_diagnostics_workers()", _QML_LOAD_PROBE_SCRIPT)
        shutdown_index = _QML_LOAD_PROBE_SCRIPT.index("m._shutdown_diagnostics_workers()")
        result_index = _QML_LOAD_PROBE_SCRIPT.index("result = {")
        print_index = _QML_LOAD_PROBE_SCRIPT.index("print(json.dumps(result))")

        self.assertLess(
            result_index,
            shutdown_index,
            "the probe must build its `result` dict from the real QML load "
            "BEFORE requesting diagnostics-worker shutdown",
        )
        self.assertLess(
            shutdown_index,
            print_index,
            "the production shutdown helper must run before the probe prints "
            "its result and exits, mirroring run_settings_window() calling "
            "it before returning",
        )

    def test_probe_script_never_mocks_or_skips_the_real_diagnostics_controller(self):
        # This probe's entire value is exercising the REAL background BLE
        # discovery a real DiagnosticsController.__init__() starts - a
        # future "just make CI green" edit replacing it with a fake/no-op
        # would silently stop testing the actual crash this task fixed.
        self.assertIn("DiagnosticsController(controller,", _QML_LOAD_PROBE_SCRIPT)
        self.assertNotIn("mock", _QML_LOAD_PROBE_SCRIPT.lower())


# Real mouse clicks and real key events via QTest, delivered through the
# ACTUAL QQmlApplicationEngine-loaded main.qml (not a Python-level call to
# setActionTextAt()) - types a custom chord into the "power" row's visible
# ComboBox and clicks "保存映射" WITHOUT ever pressing Enter. Run in an
# isolated subprocess (see OffscreenQmlLoadTests/RenderedContrastTests
# above for the two separate same-process-multi-engine QQC2 limitations
# this sidesteps); the config file it persists to (LOCALAPPDATA, passed
# via env by the outer test) is read back by the OUTER test afterward,
# since that is real state on disk that survives the subprocess exiting.
_DIRECT_SAVE_PROBE_SCRIPT = r"""
import sys

from ovb_rc003 import qt_settings_app as m
from PySide6.QtCore import QObject, QPointF, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest


def _find_child_by_object_name(root, name):
    for child in root.children():
        if child.objectName() == name:
            return child
        found = _find_child_by_object_name(child, name)
        if found is not None:
            return found
    return None


def _find_mapping_row_combo(mapping_list, button_id, model):
    mapping_list.setProperty("currentIndex", model.index_of(button_id))
    current_item = mapping_list.property("currentItem")
    if current_item is None:
        return None
    for child in current_item.children():
        for grandchild in child.children():
            if grandchild.objectName() == "actionCombo_" + button_id:
                return grandchild
    return None


classes = m._load_qt_classes()
QGuiApplication = classes["QGuiApplication"]
QQmlApplicationEngine = classes["QQmlApplicationEngine"]
QQuickStyle = classes["QQuickStyle"]
QUrl = classes["QUrl"]
qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
ButtonMappingModel = classes["ButtonMappingModel"]
SettingsController = classes["SettingsController"]
DiagnosticsController = classes["DiagnosticsController"]

QQuickStyle.setStyle("Basic")
app = QGuiApplication.instance() or QGuiApplication([])
model = ButtonMappingModel()
controller = SettingsController(model)
diagnostics_controller = DiagnosticsController(controller, m.config.config_root())
qmlRegisterSingletonInstance(SettingsController, "OvbRc003Settings", 1, 0, "SettingsController", controller)
qmlRegisterSingletonInstance(ButtonMappingModel, "OvbRc003Settings", 1, 0, "ButtonMappingModel", model)
qmlRegisterSingletonInstance(DiagnosticsController, "OvbRc003Settings", 1, 0, "DiagnosticsController", diagnostics_controller)

engine = QQmlApplicationEngine()
qml_dir = m._qml_directory()
engine.addImportPath(str(qml_dir))
engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
assert len(engine.rootObjects()) == 1, "main.qml failed to load"
window = engine.rootObjects()[0]
# Real Qt Quick Controls delegates (ListView rows in particular) only get
# their real size/instantiate their children once a real layout+render
# pass has actually run - under the offscreen platform, plain
# processEvents() calls alone are not sufficient; grabWindow() (which
# forces a real frame to be produced) interleaved with processEvents()
# reliably settles it (confirmed empirically while writing this test - a
# mapping row's ComboBox does not exist at all, and its ListView reports
# height 0, without this).
window.show()
for _ in range(10):
    window.grabWindow()
    app.processEvents()

# Switch to the "按键" tab - the ComboBox under test only exists once
# ButtonsPage is the active StackLayout page. Drive the tab by its
# objectName instead of a positional index: the General tab was inserted at
# index 0 after this probe was written, which shifted the mapping page from
# index 1 to 2. With the stale index the probe silently selected the
# Connection page, found the ComboBox on an inactive StackLayout page, and
# the real click below landed on the window's content control - surfacing as
# "click did not focus the ComboBox" ~20 lines further down.
tab_bar = _find_child_by_object_name(window, "tabBar")
assert tab_bar is not None
mapping_tab_button = _find_child_by_object_name(tab_bar, "mappingTabButton")
assert mapping_tab_button is not None, "mappingTabButton not found in the tab bar"
mapping_tab_button.setProperty("checked", True)
for _ in range(10):
    window.grabWindow()
    app.processEvents()

mapping_list = _find_child_by_object_name(window, "mappingList")
assert mapping_list is not None
combo = _find_mapping_row_combo(mapping_list, "power", model)
assert combo is not None, "power row's ComboBox not found - is it in view?"
# Precondition, checked before the click so a page-selection regression fails
# here with an accurate message instead of as a focus assertion later: an
# inactive StackLayout page leaves its ComboBox present but invisible.
assert combo.property("visible"), (
    "the mapping page is not the active StackLayout page - the ComboBox "
    "exists but is not visible, so a real click cannot reach it"
)

# Real mouse click into the ComboBox's editable text area -
# forceActiveFocus() on the ComboBox item alone is NOT equivalent (proven
# while writing this test: it does not hand keyboard focus to the internal
# editable TextInput the way a real click does).
center = combo.mapToScene(
    QPointF(combo.property("width") / 2, combo.property("height") / 2)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, center)
app.processEvents()
assert combo.property("activeFocus"), "click did not focus the ComboBox"

# Select all existing text, then really TYPE the replacement - one real
# QKeyEvent per character, through the window, exactly as a user's
# keystrokes would arrive. Lowercase ASCII ordinals (not the Qt.Key_X enum,
# whose values are the UPPERCASE-letter ASCII codes) so the synthesized
# text matches real lowercase typing exactly.
QTest.keySequence(window, QKeySequence.SelectAll)
app.processEvents()
typed = "ctrl+shift+p"
for ch in typed:
    QTest.keyClick(window, Qt.Key_Plus if ch == "+" else ord(ch))
    app.processEvents()

assert combo.property("editText") == typed
# The model already reflects the live, uncommitted-by-Enter edit (blocker
# 1's fix) - checked BEFORE any save runs, so a regression that removes the
# live-commit wiring but leaves onAccepted/onActivated intact cannot
# silently pass this test by "saving" a value it only just picked up.
assert model.to_display_map()["power"] == typed

# Real click on "保存映射" - deliberately never press Enter/Return anywhere
# in this test.
save_button = _find_child_by_object_name(window, "saveMappingButton")
assert save_button is not None
save_center = save_button.mapToScene(
    QPointF(save_button.property("width") / 2, save_button.property("height") / 2)
).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, save_center)
app.processEvents()

assert controller.errorMessage == "", f"save reported a validation error: {controller.errorMessage}"
print("OK")
"""


# -- QML / controller contract helpers ------------------------------------
#
# The .qml files are the only specification of which SettingsController
# members and which design tokens have to exist: QML is what Qt binds against
# at load time, and a name that is not there is not a load failure - it is an
# "Unable to assign [undefined]" warning and a control that renders blank and
# silently does nothing. Reading the QML the way Qt does means comments must
# not count: the removed bridge-autostart card is still *described* in a
# comment in GeneralPage.qml, and treating that prose as a binding would
# resurrect a control that no longer exists.

_QML_CONTROLLER_MEMBER = re.compile(r"SettingsController\.([A-Za-z_][A-Za-z0-9_]*)")
_QML_TOKEN_MEMBER = re.compile(r"\btokens\.([A-Za-z_][A-Za-z0-9_]*)")
_QML_DECLARED_PROPERTY = re.compile(
    r"\bproperty\s+[A-Za-z_][A-Za-z0-9_.<>]*\s+([A-Za-z_][A-Za-z0-9_]*)"
)
_TRAY_METHOD_REFERENCE = re.compile(r"self\._tray_controller\.([A-Za-z_][A-Za-z0-9_]*)")


def _strip_qml_comments(source: str) -> str:
    """Remove ``//`` and ``/* */`` comments, leaving string literals intact.

    Both directions matter. A name inside a comment is not a binding, so
    leaving comments in invents requirements; and a literal like
    ``"https://example.invalid//path"`` contains a ``//`` that is not a
    comment, so stripping naively corrupts real source. QML strings use single
    or double quotes with backslash escapes, which is all this has to track.
    """

    out: list = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char in "\"'":
            quote = char
            out.append(char)
            index += 1
            while index < length:
                out.append(source[index])
                if source[index] == "\\" and index + 1 < length:
                    out.append(source[index + 1])
                    index += 2
                    continue
                if source[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "/":
            while index < length and source[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and source[index + 1] == "*":
            index += 2
            while index + 1 < length and not (
                source[index] == "*" and source[index + 1] == "/"
            ):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _qml_sources() -> dict:
    """Every shipped .qml file, keyed by file name, read through the same
    frozen-vs-checkout lookup the app itself uses."""

    directory = qt_settings_app._qml_directory()
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(Path(directory).glob("*.qml"))
    }


def _qml_controller_members() -> set:
    members: set = set()
    for source in _qml_sources().values():
        members.update(_QML_CONTROLLER_MEMBER.findall(_strip_qml_comments(source)))
    return members


def _qml_token_members() -> set:
    members: set = set()
    for name, source in _qml_sources().items():
        if name == "Tokens.qml":
            continue
        members.update(_QML_TOKEN_MEMBER.findall(_strip_qml_comments(source)))
    return members


def _qt_settings_source() -> str:
    return Path(qt_settings_app.__file__).read_text(encoding="utf-8")


def _pump_until(predicate, timeout: float = 5.0) -> bool:
    """Wait for ``predicate`` while delivering queued Qt signals.

    ``SettingsController`` hands background results back through a signal, and
    a signal emitted from a worker thread is delivered as a *queued* call on
    the receiver's thread. A plain sleep loop therefore never sees the result:
    the slot that clears the in-flight guard only runs when the event queue is
    processed.
    """

    application = qt_app.get_application()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    application.processEvents()
    return predicate()


def _settings_controller_slots() -> dict:
    """``{slot_name: line_number}`` for every @Slot-decorated
    ``SettingsController`` method, read from the source rather than from the
    live class so the check also covers a slot that fails to build."""

    tree = ast.parse(_qt_settings_source())
    slots: dict = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "SettingsController":
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in item.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if getattr(target, "id", None) == "Slot":
                    slots[item.name] = item.lineno
    return slots


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class OrphanedControllerSlotRegressionTests(unittest.TestCase):
    """A slot nothing can reach is worse than a missing one: it looks like a
    feature and behaves like a no-op. These two tests walk the two
    reachability directions the settings window actually uses - signals into
    slots, and QML into the controller - and fail on an orphan either way.
    """

    def test_every_tray_signal_is_connected_by_the_controller(self):
        """Every signal ``TrayController`` declares must be wired up.

        The tray menu is the only way the icon can reach the app, and the
        wiring is a hand-written list of ``connect`` calls, so a signal added
        to the tray is silently dead until someone remembers to add the
        matching line here. Connectivity is read from Qt's own receiver count
        rather than from the source text, so a ``connect`` to something that
        is not a slot cannot pass.

        Note: the signature handed to ``receivers()`` is spelled for a
        zero-argument signal, which is what both tray signals are. A future
        signal that takes arguments would need its signature spelled out here
        - it would fail loudly rather than pass quietly.
        """

        classes = qt_settings_app._load_qt_classes()
        qt_app.get_application()

        declared = [
            name
            for name, value in vars(tray_icon.TrayController).items()
            if isinstance(value, tray_icon.Signal)
        ]
        self.assertTrue(declared, "TrayController declares no signals to check")

        # A stand-in carrying the real signal set but skipping the parts that
        # need a live system tray (QMenu/QSystemTrayIcon). The controller's
        # wiring path is untouched: it only ever calls show()/hide()/
        # set_window_visible() on whatever it is handed.
        class _StandInTray(tray_icon.TrayController):
            def __init__(self, **kwargs):
                tray_icon.QObject.__init__(self)
                self.show_calls = 0

            def show(self):
                self.show_calls += 1
                return False  # "no usable system tray" - the graceful path

            def hide(self):
                return None

            def set_window_visible(self, visible):
                return None

        created: list = []

        def _factory(**kwargs):
            instance = _StandInTray(**kwargs)
            created.append(instance)
            return instance

        model = classes["ButtonMappingModel"]()
        controller = classes["SettingsController"](model)
        controller._tray_enabled = True
        with mock.patch.object(tray_icon, "TrayController", _factory):
            controller._apply_tray_to_runtime()

        self.assertEqual(len(created), 1, "the tray controller was never built")
        tray = created[0]
        self.assertEqual(tray.show_calls, 1)
        unconnected = [name for name in declared if tray.receivers(f"2{name}()") < 1]
        self.assertEqual(
            unconnected,
            [],
            "TrayController signal(s) declared but never connected by "
            f"SettingsController, so the menu item does nothing: {unconnected}",
        )

    def test_no_controller_slot_is_orphaned(self):
        """Every @Slot on SettingsController must be reachable from somewhere.

        Reachable means one of: named by QML, mentioned anywhere else in the
        module (a ``connect`` target or a call from DiagnosticsController both
        count), or listed below as deliberately-retained public API.
        """

        slots = _settings_controller_slots()
        self.assertTrue(slots, "the @Slot scan found nothing - it cannot fail")

        source = _qt_settings_source()
        qml_members = _qml_controller_members()
        self.assertTrue(qml_members, "no SettingsController members found in QML")

        # ``refreshStartWithWindowsStatus`` is public API kept for QML to call
        # on window show, "in case the shortcut was removed out-of-band" (see
        # its docstring). No QML calls it today and it is deliberately not
        # removed - this is that one documented exception, not a blanket
        # allowance for unused slots.
        retained_public_api = {"refreshStartWithWindowsStatus"}

        orphans = []
        for name, lineno in sorted(slots.items(), key=lambda item: item[1]):
            if name in qml_members or name in retained_public_api:
                continue
            word = rf"\b{re.escape(name)}\b"
            mentions = len(re.findall(word, source))
            definitions = len(re.findall(rf"\bdef\s+{re.escape(name)}\b", source))
            if mentions > definitions:
                continue
            orphans.append(f"{name} (line {lineno})")

        self.assertEqual(
            orphans,
            [],
            "SettingsController slot(s) nothing can reach: " + ", ".join(orphans),
        )


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class QmlControllerContractTests(unittest.TestCase):
    """The QML files are the contract; the controller has to satisfy it.

    Qt resolves ``SettingsController.<member>`` and ``tokens.<member>`` when it
    loads the QML. A name that is not there is not a load failure - it is an
    "Unable to assign [undefined] to ..." warning plus a control that renders
    blank and does nothing, which is exactly how the removed bridge-autostart
    card survived a release.
    """

    def test_the_comment_stripper_ignores_comments_but_keeps_strings(self):
        """Guard on the guard: both failure directions of the stripper.

        Too little stripping invents requirements out of prose; too much
        corrupts string literals that legitimately contain ``//``.
        """

        source = (
            "a: 1 // SettingsController.removedOne\n"
            'b: "https://example.invalid//path" + "SettingsController.keptOne"\n'
            "c: 2 /* SettingsController.removedTwo */\n"
        )
        stripped = _strip_qml_comments(source)
        self.assertNotIn("SettingsController.removedOne", stripped)
        self.assertNotIn("SettingsController.removedTwo", stripped)
        self.assertIn("SettingsController.keptOne", stripped)
        self.assertIn("https://example.invalid//path", stripped)

    def test_the_scan_actually_finds_the_qml_sources(self):
        """A scan that finds nothing must not read as "nothing wrong"."""

        sources = _qml_sources()
        self.assertGreaterEqual(len(sources), 5, sorted(sources))
        for expected in ("main.qml", "Tokens.qml", "GeneralPage.qml", "ButtonsPage.qml"):
            self.assertIn(expected, sources)

        members = _qml_controller_members()
        self.assertGreaterEqual(len(members), 20, sorted(members))
        self.assertIn("saveSettings", members)

    def test_every_settings_controller_member_named_by_qml_exists(self):
        classes = qt_settings_app._load_qt_classes()
        Controller = classes["SettingsController"]

        members = _qml_controller_members()
        self.assertTrue(members, "no SettingsController members found in the QML")

        missing = sorted(name for name in members if not hasattr(Controller, name))
        self.assertEqual(
            missing,
            [],
            "QML binds SettingsController members the controller never "
            f"declares - blank controls and silent no-ops: {missing}",
        )

    def test_every_token_named_by_qml_is_declared_in_tokens_qml(self):
        sources = _qml_sources()
        declared = set(
            _QML_DECLARED_PROPERTY.findall(_strip_qml_comments(sources["Tokens.qml"]))
        )
        self.assertTrue(declared, "Tokens.qml declares no properties")

        used = _qml_token_members()
        self.assertTrue(used, "no tokens.* references found in the QML")

        missing = sorted(name for name in used if name not in declared)
        self.assertEqual(
            missing,
            [],
            f"QML uses tokens.* that Tokens.qml never declares: {missing}",
        )


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class ButtonsPageDirectSaveIntegrationTests(unittest.TestCase):
    """XRBM-030 RETRY 1 blocker 1: a REAL Qt/QML interaction test (see
    ``_DIRECT_SAVE_PROBE_SCRIPT`` above) proving a user can type a custom
    chord into a specific visible mapping row's ComboBox and click "保存
    映射" WITHOUT ever pressing Enter, and have that typed value actually
    reach the persisted key_bindings.json - the exact gap the previous
    onAccepted/onActivated-only wiring left open.
    """

    def test_typed_chord_survives_a_direct_save_click_with_no_enter_pressed(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmpdir:
            env = dict(os.environ)
            env.setdefault("QT_QPA_PLATFORM", "offscreen")
            env["LOCALAPPDATA"] = tmpdir
            result = subprocess.run(
                [sys.executable, "-c", _DIRECT_SAVE_PROBE_SCRIPT],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"direct-save probe subprocess failed: {result.stdout}\n{result.stderr}",
            )
            self.assertIn("OK", result.stdout)

            # The real, persisted file - read back from the SAME
            # LOCALAPPDATA the subprocess wrote to, after it has exited.
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmpdir}):
                bindings = config.load_key_bindings(
                    config.key_bindings_path(config.config_root())
                )
            action = key_mapping.ButtonAction.from_dict(bindings["bindings"]["power"])
            self.assertEqual(action.kind, key_mapping.ActionKind.KEY_COMBO)
            self.assertEqual(action.keys, ("ctrl", "shift", "p"))


def _contrast_ratio(luminance_a, luminance_b):
    """WCAG 2.x contrast ratio (always >= 1.0) between two luminances."""

    lighter, darker = max(luminance_a, luminance_b), min(luminance_a, luminance_b)
    return (lighter + 0.05) / (darker + 0.05)


# Renders the REAL main.qml with the REAL "FluentWinUI3" style in a fresh,
# throwaway subprocess and prints one JSON line with the darkest-pixel
# luminance and corner-background luminance sampled inside each of three
# controls (see RenderedContrastTests below for why this must be a
# SEPARATE process rather than sharing one with the rest of this test
# file's Qt-loading tests): QQuickStyle is a process-global, set-once
# setting - once any OTHER test in this file has loaded Qt Quick Controls
# QML with "Basic" (or any other style), a later attempt to set
# "FluentWinUI3" in the SAME process either has no effect or raises
# "QQuickStyle::setStyle() must be called before loading QML that imports
# Qt Quick Controls 2." (both reproduced while writing this test).
_CONTRAST_PROBE_SCRIPT = r"""
import json
import sys

from ovb_rc003 import qt_settings_app as m
from PySide6.QtCore import QPointF, QObject


def _linearize(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(color):
    return 0.2126 * _linearize(color.red()) + 0.7152 * _linearize(color.green()) + 0.0722 * _linearize(color.blue())


def _find(root, name):
    for child in root.children():
        if child.objectName() == name:
            return child
        found = _find(child, name)
        if found is not None:
            return found
    return None


classes = m._load_qt_classes()
QGuiApplication = classes["QGuiApplication"]
QQmlApplicationEngine = classes["QQmlApplicationEngine"]
QQuickStyle = classes["QQuickStyle"]
QUrl = classes["QUrl"]
qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
ButtonMappingModel = classes["ButtonMappingModel"]
SettingsController = classes["SettingsController"]
DiagnosticsController = classes["DiagnosticsController"]

QQuickStyle.setStyle("FluentWinUI3")
app = QGuiApplication.instance() or QGuiApplication([])
model = ButtonMappingModel()
controller = SettingsController(model)
diagnostics_controller = DiagnosticsController(controller, m.config.config_root())
qmlRegisterSingletonInstance(SettingsController, "OvbRc003Settings", 1, 0, "SettingsController", controller)
qmlRegisterSingletonInstance(ButtonMappingModel, "OvbRc003Settings", 1, 0, "ButtonMappingModel", model)
qmlRegisterSingletonInstance(DiagnosticsController, "OvbRc003Settings", 1, 0, "DiagnosticsController", diagnostics_controller)

engine = QQmlApplicationEngine()
qml_dir = m._qml_directory()
engine.addImportPath(str(qml_dir))
engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
if len(engine.rootObjects()) != 1:
    print(json.dumps({"error": "main.qml failed to load"}))
    sys.exit(1)

window = engine.rootObjects()[0]
window.show()
for _ in range(10):
    window.grabWindow()
    app.processEvents()
image = window.grabWindow()

results = {}
for object_name in ("connectionTabButton", "openLogButton", "hotkeyField"):
    item = _find(window, object_name)
    if item is None:
        print(json.dumps({"error": object_name + " not found"}))
        sys.exit(1)
    margin = 3
    corner = item.mapToScene(QPointF(1, 1))
    background_luminance = _luminance(image.pixelColor(int(corner.x()), int(corner.y())))
    top_left = item.mapToScene(QPointF(margin, margin))
    width = int(item.property("width") - 2 * margin)
    height = int(item.property("height") - 2 * margin)
    x0, y0 = int(top_left.x()), int(top_left.y())
    darkest = 1.0
    for x in range(x0, x0 + width):
        for y in range(y0, y0 + height):
            luminance = _luminance(image.pixelColor(x, y))
            if luminance < darkest:
                darkest = luminance
    results[object_name] = {"background": background_luminance, "darkest": darkest}

print(json.dumps(results))
"""


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class RenderedContrastTests(unittest.TestCase):
    """XRBM-030 RETRY 1 blocker 2: renders the REAL main.qml with the REAL
    "FluentWinUI3" style (in an isolated subprocess - see
    ``_CONTRAST_PROBE_SCRIPT`` above for why) and asserts a real, measured
    WCAG contrast ratio between each sampled control's own background and
    its darkest rendered pixel - not merely "zero QML warnings" (which the
    white-on-light regression this fixes produced zero of; a rendering bug
    is not a QML error). Calibrated against the actual pre-fix regression:
    removing ``main.qml``'s explicit ``palette.*`` bindings (see that
    file's own module docstring for why they are needed) reproduces a
    measured contrast ratio of exactly 1.00 (the "text" is pixel-identical
    to the background - completely invisible) on every one of the three
    controls checked here; this test's threshold (3.0) sits far below the
    actual fixed measurement (18+) and far above the broken one (1.00), so
    it cannot pass by accident either way.
    """

    _MIN_CONTRAST_RATIO = 3.0
    _LABELS = {
        "connectionTabButton": "「连接」tab label",
        "openLogButton": "「打开日志目录」button",
        "hotkeyField": "语音热键 TextField",
    }

    def test_tab_button_plain_button_and_text_field_are_all_readable(self):
        import json
        import subprocess

        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env["LOCALAPPDATA"] = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, "-c", _CONTRAST_PROBE_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode, 0, f"contrast probe subprocess failed: {result.stderr}"
        )
        last_line = result.stdout.strip().splitlines()[-1]
        data = json.loads(last_line)
        self.assertNotIn("error", data, data.get("error"))

        for object_name, label in self._LABELS.items():
            measurement = data[object_name]
            ratio = _contrast_ratio(measurement["background"], measurement["darkest"])
            self.assertGreaterEqual(
                ratio,
                self._MIN_CONTRAST_RATIO,
                f"{label} contrast ratio {ratio:.2f} is below "
                f"{self._MIN_CONTRAST_RATIO} - text is not reliably "
                "readable against its own background",
            )


# XRBM-032: proves, against the REAL main.qml ButtonsPage rendered offscreen,
# that (a) every one of the 13 hotspot Items' visual CENTER coincides with
# the calibrated center fraction from remote_layout.py (mapped through
# the live PreserveAspectFit painted geometry), and (b) a REAL QTest mouse
# click delivered at the OK button's TRUE physical center actually selects
# OK. Before the fix (which subtracted half the item's width/height to turn
# the center fraction into a QML top-left) each Item's top-left sat at the
# center fraction, so every hotspot drifted down-right by half its own size
# and a click at OK's true center missed the OK Item entirely - so this test
# fails on the pre-fix geometry, not just passes trivially. Run in an
# isolated subprocess for the same per-process QQuickStyle/QQC2 reasons the
# other engine-loading probes above document. No fixed sleeps: a
# grabWindow()+processEvents() loop (which forces real frames) is the render
# gate, exactly as the direct-save probe uses.
_HOTSPOT_GEOMETRY_PROBE_SCRIPT = r"""
import json
import sys

from ovb_rc003 import qt_settings_app as m
from ovb_rc003 import remote_layout
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest


# Depth-first search of the VISUAL item tree (childItems). Repeater-created
# hotspot delegates are visual children of the photo frame but are NOT
# QObject children reachable from the window, so a QObject.children() walk
# misses them; a childItems() walk from the window contentItem does not.
def _find(root, name):
    for child in root.childItems():
        if child.objectName() == name:
            return child
        found = _find(child, name)
        if found is not None:
            return found
    return None


classes = m._load_qt_classes()
QGuiApplication = classes["QGuiApplication"]
QQmlApplicationEngine = classes["QQmlApplicationEngine"]
QQuickStyle = classes["QQuickStyle"]
QUrl = classes["QUrl"]
qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
ButtonMappingModel = classes["ButtonMappingModel"]
SettingsController = classes["SettingsController"]
DiagnosticsController = classes["DiagnosticsController"]

QQuickStyle.setStyle("Basic")
app = QGuiApplication.instance() or QGuiApplication([])
model = ButtonMappingModel()
controller = SettingsController(model)
diagnostics_controller = DiagnosticsController(controller, m.config.config_root())
qmlRegisterSingletonInstance(SettingsController, "OvbRc003Settings", 1, 0, "SettingsController", controller)
qmlRegisterSingletonInstance(ButtonMappingModel, "OvbRc003Settings", 1, 0, "ButtonMappingModel", model)
qmlRegisterSingletonInstance(DiagnosticsController, "OvbRc003Settings", 1, 0, "DiagnosticsController", diagnostics_controller)

engine = QQmlApplicationEngine()
qml_dir = m._qml_directory()
engine.addImportPath(str(qml_dir))
engine.load(QUrl.fromLocalFile(str(qml_dir / "main.qml")))
assert len(engine.rootObjects()) == 1, "main.qml failed to load"
window = engine.rootObjects()[0]
window.show()
content_item = window.property("contentItem")
for _ in range(10):
    window.grabWindow()
    app.processEvents()

# Switch to the "按键" tab so ButtonsPage is the active page and its
# photo/hotspots actually lay out and paint. Selected by objectName rather
# than a positional index: General is now the leftmost tab, so the mapping
# page sits at index 2 (same fix as in the direct-save probe above).
tab_bar = _find(content_item, "tabBar")
assert tab_bar is not None
mapping_tab_button = _find(content_item, "mappingTabButton")
assert mapping_tab_button is not None, "mappingTabButton not found"
mapping_tab_button.setProperty("checked", True)
for _ in range(10):
    window.grabWindow()
    app.processEvents()

photo = _find(content_item, "photoImage")
assert photo is not None, "photoImage not found"
painted_w = photo.property("paintedWidth")
painted_h = photo.property("paintedHeight")
assert painted_w > 0 and painted_h > 0, "photo not painted (paintedWidth/paintedHeight == 0)"

# The PreserveAspectFit letterbox offset inside the Image's own box, exactly
# as ButtonsPage.qml computes offsetX/offsetY.
img_w = photo.property("width")
img_h = photo.property("height")
letterbox_x = (img_w - painted_w) / 2.0
letterbox_y = (img_h - painted_h) / 2.0


def expected_center_scene(hotspot):
    return photo.mapToScene(
        QPointF(
            letterbox_x + hotspot.x * painted_w,
            letterbox_y + hotspot.y * painted_h,
        )
    )


results = {}
for hotspot in remote_layout.BUTTON_HOTSPOTS:
    item = _find(content_item, "photoHotspot_" + hotspot.button_id)
    assert item is not None, "hotspot Item not found: " + hotspot.button_id
    iw = item.property("width")
    ih = item.property("height")
    actual = item.mapToScene(QPointF(iw / 2.0, ih / 2.0))
    expected = expected_center_scene(hotspot)
    results[hotspot.button_id] = {
        "dx": actual.x() - expected.x(),
        "dy": actual.y() - expected.y(),
        "expected": [expected.x(), expected.y()],
        "actual": [actual.x(), actual.y()],
    }

# Real QTest click delivered at OK's TRUE physical center (computed from the
# painted photo geometry, NOT from the OK Item's own position) - this is the
# exact point the pre-fix geometry left OUTSIDE the OK hotspot. Selection is
# read back from the controller the QML TapHandler drives.
ok_hotspot = remote_layout.BUTTON_HOTSPOTS_BY_ID["ok"]
ok_center = expected_center_scene(ok_hotspot).toPoint()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, ok_center)
for _ in range(5):
    window.grabWindow()
    app.processEvents()

results_out = {
    "hotspots": results,
    "selected_after_ok_click": controller.property("selectedButtonId"),
    "painted_w": painted_w,
    "painted_h": painted_h,
}
print(json.dumps(results_out))
"""


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class ButtonsPageHotspotGeometryTests(unittest.TestCase):
    """XRBM-032: every hotspot's rendered visual center must match the
    calibrated center fraction (remote_layout.py), and a real click at
    OK's true center must select OK. See ``_HOTSPOT_GEOMETRY_PROBE_SCRIPT``.
    """

    # Sub-pixel: the only expected discrepancy is float rounding between the
    # QML binding's own evaluation and this test's recomputation of the same
    # arithmetic. The pre-fix bug offsets each hotspot by half its own size
    # (tens of pixels), so this threshold cannot mask it.
    _MAX_CENTER_ERROR_PX = 1.0

    def _run_probe(self):
        import json
        import subprocess

        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        env["LOCALAPPDATA"] = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, "-c", _HOTSPOT_GEOMETRY_PROBE_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"hotspot geometry probe subprocess failed: {result.stdout}\n{result.stderr}",
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_all_thirteen_hotspot_centers_match_calibrated_fractions(self):
        data = self._run_probe()
        hotspots = data["hotspots"]
        self.assertEqual(
            set(hotspots),
            {h.button_id for h in remote_layout.BUTTON_HOTSPOTS},
            "probe did not report exactly the 13 expected hotspots",
        )
        for button_id, geo in hotspots.items():
            self.assertLessEqual(
                abs(geo["dx"]),
                self._MAX_CENTER_ERROR_PX,
                f"{button_id} X center off by {geo['dx']:.3f}px "
                f"(expected {geo['expected']}, got {geo['actual']})",
            )
            self.assertLessEqual(
                abs(geo["dy"]),
                self._MAX_CENTER_ERROR_PX,
                f"{button_id} Y center off by {geo['dy']:.3f}px "
                f"(expected {geo['expected']}, got {geo['actual']})",
            )

    def test_real_click_at_ok_true_center_selects_ok(self):
        data = self._run_probe()
        self.assertEqual(
            data["selected_after_ok_click"],
            "ok",
            "a real QTest click at OK's true physical center did not select "
            "OK - the OK hotspot is not centered on its own coordinate",
        )


if __name__ == "__main__":
    unittest.main()
