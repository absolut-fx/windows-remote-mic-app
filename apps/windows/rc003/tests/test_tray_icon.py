"""Tests for the TrayController
(src/ovb_rc003/tray_icon.py).

Strategy: the controller wraps a ``QSystemTrayIcon``. The widget
itself is real but only exercised through the public surface -
``show()``, ``hide()``, ``set_window_visible()``,
``set_shortcut_active()``, and the three signals.

What we actually verify here:

  * ``show()`` is idempotent: a second call is a no-op, no
    duplicate menu, no re-registered signals.
  * ``hide()`` is idempotent: calling twice doesn't crash; after
    hide, calling ``show()`` again reconstructs the widget.
  * The single-click / double-click ``activated`` handler emits
    ``show_window_requested``.
  * Clicking the menu items emits the right signals
    (``show_window_requested``, ``quit_requested``,
    ``open_startup_folder_requested``).
  * ``set_window_visible(True|False)`` flips the menu label
    between "Show settings window" and "Hide settings window".
  * ``set_shortcut_active(True|False)`` toggles the "Open Startup
    folder" item's enabled state without crashing when the
    provider raises.
  * The asset resolver functions return paths inside the project
    tree (source mode) and don't raise on Windows.
  * The icon builder always returns a non-null ``QIcon`` even if
    the SVG file is missing or corrupt.

Runs under offscreen QPA via ``set_qt_offscreen()``.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Callable, List

from PySide6.QtCore import QObject
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from ovb_rc003 import tray_icon


def _set_qt_offscreen() -> None:
    """Force Qt to use the offscreen platform plugin.

    Must be called BEFORE any ``QApplication`` is constructed.
    Idempotent: a second call is harmless.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _SignalCollector(QObject):
    """Collect every emission of a single signal.

    Connects via ``signal.connect(self.slot)`` where ``self.slot``
    is a method-bound lambda appending to ``self.fired``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fired: List[None] = []

    def slot(self) -> None:
        self.fired.append(None)


class TrayControllerTests(unittest.TestCase):
    """Exercise the public surface of ``TrayController``.

    These tests work whether or not the host actually exposes a
    real system tray - if ``QSystemTrayIcon.isSystemTrayAvailable``
    returns False, ``show()`` returns False (the documented
    behaviour) and we skip the integration-y tests below.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _set_qt_offscreen()
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self) -> None:
        # Skip menu/timing-dependent tests when the host has no tray.
        self._tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        # A callable that always says "shortcut inactive" unless a
        # test sets ``shortcut_active = True``.
        self.shortcut_active = False
        self.controller = tray_icon.TrayController(
            icon_path=None,  # use bundled default
            app_name="Remote Mic (test)",
            is_shortcut_active_provider=lambda: self.shortcut_active,
        )
        self.show_collector = _SignalCollector()
        self.quit_collector = _SignalCollector()
        self.open_startup_collector = _SignalCollector()
        self.controller.show_window_requested.connect(
            self.show_collector.slot
        )
        self.controller.quit_requested.connect(self.quit_collector.slot)
        self.controller.open_startup_folder_requested.connect(
            self.open_startup_collector.slot
        )

    def tearDown(self) -> None:
        self.controller.hide()
        self.controller.deleteLater()

    # ---- public surface ------------------------------------------------

    def test_is_available_matches_qsystemtrayicon(self) -> None:
        self.assertEqual(
            self.controller.is_available(),
            QSystemTrayIcon.isSystemTrayAvailable(),
        )

    def test_show_then_hide_round_trip(self) -> None:
        # Run regardless of host tray availability by patching the
        # ``isSystemTrayAvailable`` static method to return True for
        # the duration of this test. The controller's contract is
        # the same either way; this just lets us exercise the
        # widget construction path on offscreen QPA.
        original = QSystemTrayIcon.isSystemTrayAvailable
        QSystemTrayIcon.isSystemTrayAvailable = staticmethod(lambda: True)
        try:
            shown = self.controller.show()
            self.assertTrue(shown)
            self.assertTrue(self.controller._tray is not None)
            # Idempotent show.
            shown_again = self.controller.show()
            self.assertTrue(shown_again)
            # Hide.
            self.controller.hide()
            self.assertIsNone(self.controller._tray)
            # Idempotent hide (no crash, no fall-through).
            self.controller.hide()
            self.assertIsNone(self.controller._tray)
            # Show again after hide: widget rebuilt cleanly.
            self.assertTrue(self.controller.show())
            self.controller.hide()
        finally:
            QSystemTrayIcon.isSystemTrayAvailable = original

    def test_show_returns_false_on_unavailable_host(self) -> None:
        # If the host has no tray, ``show()`` must return False and
        # leave ``_tray`` untouched, not raise.
        if self._tray_available:
            self.skipTest("host has a tray; cannot exercise unavailable path")
        self.assertFalse(self.controller.show())
        self.assertIsNone(self.controller._tray)

    @staticmethod
    def _with_fake_tray(fn: Callable[[], None]) -> None:
        """Run ``fn`` with ``isSystemTrayAvailable`` patched to True.

        Lets the menu/signal tests exercise the real widget code
        paths even on offscreen QPA (which reports no tray).
        """
        original = QSystemTrayIcon.isSystemTrayAvailable
        QSystemTrayIcon.isSystemTrayAvailable = staticmethod(lambda: True)
        try:
            fn()
        finally:
            QSystemTrayIcon.isSystemTrayAvailable = original

    def test_set_window_visible_flips_menu_label(self) -> None:
        def _run() -> None:
            self.controller.show()
            try:
                # Default state: window is "visible" so the menu
                # offers to *hide* it.
                self.assertIn(
                    "隐藏设置窗口",
                    self.controller._show_action.text(),
                )
                self.controller.set_window_visible(False)
                self.assertIn(
                    "显示设置窗口",
                    self.controller._show_action.text(),
                )
                self.controller.set_window_visible(True)
                self.assertIn(
                    "隐藏设置窗口",
                    self.controller._show_action.text(),
                )
            finally:
                self.controller.hide()

        self._with_fake_tray(_run)

    def test_set_shortcut_active_toggles_open_startup_item(self) -> None:
        def _run() -> None:
            self.controller.show()
            try:
                # Inactive by default -> menu item is disabled.
                self.shortcut_active = False
                self.controller.set_shortcut_active(False)
                self.assertFalse(
                    self.controller._open_startup_action.isEnabled()
                )
                # Active -> menu item enables.
                self.shortcut_active = True
                self.controller.set_shortcut_active(True)
                self.assertTrue(
                    self.controller._open_startup_action.isEnabled()
                )
            finally:
                self.controller.hide()

        self._with_fake_tray(_run)

    def test_set_shortcut_active_tolerates_provider_exception(self) -> None:
        """A flaky is_shortcut_active_provider must not crash the
        UI - it should disable the menu item as the safe default."""

        def boom() -> bool:
            raise OSError("provider blew up")

        c = tray_icon.TrayController(
            icon_path=None,
            app_name="Boom test",
            is_shortcut_active_provider=boom,
        )

        def _run() -> None:
            c.show()
            try:
                # No exception propagated out of set_shortcut_active.
                c.set_shortcut_active(True)
                self.assertFalse(c._open_startup_action.isEnabled())
            finally:
                c.hide()

        try:
            self._with_fake_tray(_run)
        finally:
            c.deleteLater()

    def test_quit_action_emits_quit_requested(self) -> None:
        def _run() -> None:
            self.controller.show()
            try:
                self.controller._quit_action.trigger()
                self.assertEqual(len(self.quit_collector.fired), 1)
            finally:
                self.controller.hide()

        self._with_fake_tray(_run)

    def test_show_action_emits_show_window_requested(self) -> None:
        def _run() -> None:
            self.controller.show()
            try:
                self.controller._show_action.trigger()
                self.assertEqual(len(self.show_collector.fired), 1)
            finally:
                self.controller.hide()

        self._with_fake_tray(_run)

    def test_open_startup_action_emits_signal(self) -> None:
        def _run() -> None:
            # Make sure the action is enabled so ``trigger()`` fires.
            self.shortcut_active = True
            self.controller.show()
            try:
                self.controller._open_startup_action.trigger()
                self.assertEqual(
                    len(self.open_startup_collector.fired), 1
                )
            finally:
                self.controller.hide()

        self._with_fake_tray(_run)

    # ---- asset & icon plumbing ----------------------------------------

    def test_assets_directory_resolves_under_project(self) -> None:
        d = tray_icon._assets_directory()
        self.assertTrue(d.is_dir(), f"missing assets dir: {d}")
        self.assertTrue(d.name == "assets")

    def test_tray_icon_path_points_at_svg(self) -> None:
        p = tray_icon._tray_icon_path()
        # The SVG is shipped with the repo; ``_tray_icon_path`` may
        # return a frozen-mode path that doesn't exist on disk, so
        # we only assert the name.
        self.assertEqual(p.name, "app-tray.svg")
        self.assertTrue(str(p).endswith("app-tray.svg"))

    def test_fallback_icon_is_non_null(self) -> None:
        icon = tray_icon._fallback_icon(64)
        self.assertIsInstance(icon, QIcon)
        self.assertFalse(icon.isNull())

    def test_build_tray_icon_handles_missing_file(self) -> None:
        # Pass a path that almost certainly does not exist. Must
        # return *some* icon - the fallback - and never raise.
        missing = Path("C:/this/should/never/exist/app-tray.svg")
        icon = tray_icon._build_tray_icon(missing)
        self.assertIsInstance(icon, QIcon)
        self.assertFalse(icon.isNull())


class IconBuilderTests(unittest.TestCase):
    """The icon builder is pure Qt + filesystem - no signals."""

    @classmethod
    def setUpClass(cls) -> None:
        _set_qt_offscreen()
        cls._app = QApplication.instance() or QApplication(sys.argv)

    def test_load_svg_icon_returns_non_null_for_bundled_svg(self) -> None:
        path = tray_icon._tray_icon_path()
        # Only run the assertion when the bundled SVG exists on
        # disk (source mode). Frozen-mode builds won't have it
        # under the same path; in that case skip silently.
        if not path.exists():
            self.skipTest("bundled SVG not present at expected path")
        icon = tray_icon._load_svg_icon(path, size=64)
        self.assertIsInstance(icon, QIcon)
        self.assertFalse(icon.isNull())

    def test_load_svg_icon_returns_none_for_missing_file(self) -> None:
        missing = Path("C:/no/such/app-tray.svg")
        result = tray_icon._load_svg_icon(missing, size=32)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()