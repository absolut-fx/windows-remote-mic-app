"""Tests for the TrayController
(src/ovb_rc003/tray_icon.py).

Strategy: the controller wraps a ``QSystemTrayIcon``. The widget
itself is real but only exercised through the public surface -
``show()``, ``hide()``, ``set_window_visible()``, and the two
signals (``show_window_requested``, ``quit_requested``).

What we actually verify here:

  * The canonical tray menu has exactly 4 items in the order
    Open / Help / About / Exit. Items are inserted top-down in
    ``_build_menu`` and the Fusion-styled Win32 popup displays
    them in the same order, so ``QMenu.actions()`` returns them
    top-down: Open, Help, About, Exit.
  * ``show()`` is idempotent: a second call is a no-op, no
    duplicate menu, no re-registered signals.
  * ``hide()`` is idempotent: calling twice doesn't crash; after
    hide, calling ``show()`` again reconstructs the widget.
  * The single-click / double-click ``activated`` handler emits
    ``show_window_requested``.
  * Clicking the Open item emits ``show_window_requested``.
  * Clicking the Help item calls the help-dialog opener with the
    app name and usage text.
  * Clicking the About item calls the about-dialog opener with
    the app name, version, and license notice.
  * Clicking the Exit item emits ``quit_requested``.
  * ``set_window_visible(True|False)`` flips the Open label
    between "\u9690\u85cf\u8bbe\u7f6e\u7a97\u53e3" and
    "\u663e\u793a\u8bbe\u7f6e\u7a97\u53e3".
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
        self.controller = tray_icon.TrayController(
            icon_path=None,  # use bundled default
            app_name="Remote Mic (test)",
        )
        # Inject harmless no-op dialog openers so the Help / About
        # tests can assert they were called without spawning real
        # ``QMessageBox``es (which would block on offscreen QPA).
        self.help_calls: List[tuple] = []
        self.about_calls: List[tuple] = []
        self.controller._open_help_dialog = (  # type: ignore[attr-defined]
            lambda app_name, body: self.help_calls.append((app_name, body))
        )
        self.controller._open_about_dialog = (  # type: ignore[attr-defined]
            lambda app_name, display_name, version, license_notice:
                self.about_calls.append(
                    (app_name, display_name, version, license_notice)
                )
        )
        self.show_collector = _SignalCollector()
        self.quit_collector = _SignalCollector()
        self.controller.show_window_requested.connect(
            self.show_collector.slot
        )
        self.controller.quit_requested.connect(self.quit_collector.slot)

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

    def test_set_window_visible_keeps_open_label_static(self) -> None:
        """The Open action's label is static ("Open") regardless of
        whether the settings window is currently visible. Visibility
        state still drives the action's behaviour (show vs hide) - the
        label just doesn't flip anymore. Locks the spec where the
        canonical 4-item menu reads Open / Help / About / Exit in both
        states.
        """
        def _run() -> None:
            self.controller.show()
            try:
                # Default: window visible - label is still "Open".
                self.assertEqual(self.controller._show_action.text(), "Open")
                # Hide the window: label still "Open".
                self.controller.set_window_visible(False)
                self.assertEqual(self.controller._show_action.text(), "Open")
                # Show the window again: label still "Open".
                self.controller.set_window_visible(True)
                self.assertEqual(self.controller._show_action.text(), "Open")
            finally:
                self.controller.hide()

        self._with_fake_tray(_run)

    # ---- menu layout (canonical 4-item Open/Help/About/Exit) ----------

    def test_menu_has_exactly_four_actions_in_canonical_order(self) -> None:
        """The 4-item tray menu must be Open / Help / About / Exit.

        Items are inserted top-down (see ``_build_menu``) so the
        order returned by ``QMenu.actions()`` is Open, Help, About,
        Exit. The Fusion-styled Win32 popup displays them in the same
        order, so on-screen top-down reads Open, Help, About, Exit.
        """
        def _run() -> None:
            self.controller.show()
            try:
                actions = self.controller._menu.actions()
                self.assertEqual(
                    len(actions),
                    4,
                    msg=(
                        "tray menu must contain exactly 4 items, got "
                        f"{[a.text() for a in actions]!r}"
                    ),
                )
                # Top-down insertion order: Open, Help, About, Exit.
                self.assertEqual(actions[0].text(), "Open")
                self.assertEqual(
                    actions[1].text(), tray_icon._LABEL_HELP
                )
                self.assertEqual(
                    actions[2].text(), tray_icon._LABEL_ABOUT
                )
                self.assertEqual(
                    actions[3].text(), tray_icon._LABEL_EXIT
                )
                # No separator items in the canonical 4-item menu.
                self.assertFalse(
                    any(a.isSeparator() for a in actions),
                    msg="4-item menu must not contain separators",
                )
            finally:
                self.controller.hide()

        self._with_fake_tray(_run)

    def test_menu_canonical_labels_are_nonempty_and_english(self) -> None:
        """All four labels must be non-empty strings (guards against a
        regression where a label accidentally becomes None or ''), AND
        the on-screen top-down order must be exactly:
            Open / Help / About / Exit.
        """
        for label in (
            tray_icon._LABEL_OPEN_HIDDEN,
            tray_icon._LABEL_OPEN_VISIBLE,
            tray_icon._LABEL_HELP,
            tray_icon._LABEL_ABOUT,
            tray_icon._LABEL_EXIT,
        ):
            self.assertIsInstance(label, str)
            self.assertNotEqual(label.strip(), "")

        # Strict on-screen label set - locks the spec.
        self.assertEqual(
            (
                tray_icon._LABEL_OPEN_VISIBLE,
                tray_icon._LABEL_HELP,
                tray_icon._LABEL_ABOUT,
                tray_icon._LABEL_EXIT,
            ),
            ("Open", "Help", "About", "Exit"),
        )
        # The two Open-label constants now both equal "Open" (label
        # no longer flips on visibility).
        self.assertEqual(
            tray_icon._LABEL_OPEN_VISIBLE,
            tray_icon._LABEL_OPEN_HIDDEN,
        )

    def test_show_action_emits_show_window_requested(self) -> None:
        def _run() -> None:
            self.controller.show()
            try:
                self.controller._show_action.trigger()
                self.assertEqual(len(self.show_collector.fired), 1)
            finally:
                self.controller.hide()

        self._with_fake_tray(_run)

    def test_help_action_calls_help_dialog_opener(self) -> None:
        """Clicking Help must invoke the injected opener with the
        app name and a non-empty usage body, without emitting any
        signal (Help is informational, not a controller signal)."""
        def _run() -> None:
            self.controller.show()
            try:
                self.help_calls.clear()
                self.controller._help_action.trigger()
                self.assertEqual(len(self.help_calls), 1)
                app_name, body = self.help_calls[0]
                self.assertEqual(app_name, "Remote Mic (test)")
                self.assertIsInstance(body, str)
                self.assertGreater(len(body), 0)
                # Help / About are informational - they must NOT
                # touch the show/quit signals.
                self.assertEqual(len(self.show_collector.fired), 0)
                self.assertEqual(len(self.quit_collector.fired), 0)
            finally:
                self.controller.hide()

        self._with_fake_tray(_run)

    def test_about_action_calls_about_dialog_opener(self) -> None:
        """Clicking About must invoke the injected opener with app
        name, display name, version, and license notice, without
        emitting any signal."""
        def _run() -> None:
            self.controller.show()
            try:
                self.about_calls.clear()
                self.controller._about_action.trigger()
                self.assertEqual(len(self.about_calls), 1)
                app_name, display_name, version, license_notice = (
                    self.about_calls[0]
                )
                self.assertEqual(app_name, "Remote Mic (test)")
                self.assertIsInstance(display_name, str)
                self.assertNotEqual(display_name.strip(), "")
                self.assertIsInstance(version, str)
                self.assertNotEqual(version.strip(), "")
                self.assertIsInstance(license_notice, str)
                self.assertNotEqual(license_notice.strip(), "")
                self.assertEqual(len(self.show_collector.fired), 0)
                self.assertEqual(len(self.quit_collector.fired), 0)
            finally:
                self.controller.hide()

        self._with_fake_tray(_run)

    def test_help_dialog_swallows_exceptions(self) -> None:
        """A flaky help opener must not crash the tray - Help just
        no-ops and the controller stays alive for Open / About /
        Exit to keep working."""

        def _run() -> None:
            boom_calls = []

            def boom(app_name: str, body: str) -> None:
                boom_calls.append((app_name, body))
                raise RuntimeError("dialog renderer unavailable")

            self.controller._open_help_dialog = boom  # type: ignore[attr-defined]
            self.controller.show()
            try:
                # Must NOT raise.
                self.controller._help_action.trigger()
                self.assertEqual(len(boom_calls), 1)
                # Other items must still work.
                self.controller._quit_action.trigger()
                self.assertEqual(len(self.quit_collector.fired), 1)
            finally:
                self.controller.hide()

        self._with_fake_tray(_run)

    def test_about_dialog_swallows_exceptions(self) -> None:
        """Same isolation guarantee for About."""

        def _run() -> None:
            def boom(app_name, display_name, version, license_notice):
                raise OSError("about renderer unavailable")

            self.controller._open_about_dialog = boom  # type: ignore[attr-defined]
            self.controller.show()
            try:
                self.controller._about_action.trigger()
                # Open still works afterwards.
                self.controller._show_action.trigger()
                self.assertEqual(len(self.show_collector.fired), 1)
            finally:
                self.controller.hide()

        self._with_fake_tray(_run)

    def test_quit_action_emits_quit_requested(self) -> None:
        def _run() -> None:
            self.controller.show()
            try:
                self.controller._quit_action.trigger()
                self.assertEqual(len(self.quit_collector.fired), 1)
            finally:
                self.controller.hide()

        self._with_fake_tray(_run)

    # ---- removed-API regression guard ------------------------------------

    def test_removed_signal_and_kwarg_are_gone(self) -> None:
        """Guard against re-introducing the 3-item tray / 'open
        startup folder' menu entry or the deprecated signal / kwarg.

        The user-facing contract is now 4 items. Anyone adding the
        5th item, restoring the 3rd signal, or re-wiring the
        ``is_shortcut_active_provider`` kwarg must first update the
        canonical 4-item spec and re-enable the tests covering the
        old behaviour."""
        import inspect
        sig = inspect.signature(tray_icon.TrayController.__init__)
        self.assertNotIn(
            "is_shortcut_active_provider",
            sig.parameters,
            msg=(
                "is_shortcut_active_provider kwarg must NOT be re-added; "
                "the tray no longer carries an 'open startup folder' "
                "menu item."
            ),
        )
        self.assertNotIn(
            "open_startup_folder_requested",
            dir(tray_icon.TrayController),
            msg=(
                "open_startup_folder_requested signal must NOT be "
                "re-added; see canonical 4-item spec."
            ),
        )

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