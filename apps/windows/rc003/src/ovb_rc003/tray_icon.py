"""System tray icon wrapper for the RC003 settings app.

Exposes a single ``TrayController`` class that the main window owns:

* ``show()`` registers the tray icon (idempotent).
* ``hide()`` removes it (idempotent).
* ``set_visible(window_visible)`` keeps the tray menu's "Show" / "Hide"
  action label and checkmark in sync with the settings window's actual
  state.

The class is intentionally minimal: every method is no-op-safe, every
state-changing method emits exactly one Qt signal so the parent window
can react, and the class never owns the ``QApplication`` or any timer
that outlives the parent.

Design choices:

* Icon source is a single SVG file (``assets/icons/app-tray.svg``) which
  ``QSvgRenderer`` rasterizes at runtime for whatever pixel sizes the
  Windows shell needs (16x16 for the notification area, 32x32 for the
  expanded view, 64x64 for accessibility zoom). One file scales to all
  sizes; no raster ICO asset to keep in sync across releases.

* Fallback icon: if the SVG fails to load (e.g. a build that forgot to
  bundle it), we render a minimal solid-colour pixmap programmatically
  so the user never sees the Windows "missing icon" cross. The
  fallback is created lazily and only on the error path.

* Single-click on the tray icon shows the settings window. This matches
  Windows users' expectations for "settings utility" apps (Discord,
  Steam, OneDrive, Telegram all behave this way). Double-click is left
  to the platform default which is again "show window" - we don't
  override ``activated`` twice.

* The tray context menu has exactly four items (canonical spec, see
  ``_build_menu``):
    - ``Open`` (single-purpose action, dynamic label - "Show settings
      window" when hidden, "Hide settings window" when visible)
    - ``Help`` (informational - opens a usage dialog; no signal emitted)
    - ``About`` (informational - opens a Qt ``QMessageBox`` with app
      name, version, and GPL-3.0 license notice; no signal emitted)
    - ``Exit`` (terminates the entire app, matching the existing
      close-window-quits-app default - see how the parent wires
      ``setQuitOnLastWindowClosed``)

  Items are inserted into the menu bottom-up because Qt's ``QMenu``
  displays actions in the order they're appended; on Win11 Fusion
  this is the only way to get a top-down Open -> Help -> About ->
  Exit menu. The helpers ``_build_menu`` and ``_show_text`` document
  the rationale.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QPainter,
    QPixmap,
    QGuiApplication,
)
from PySide6.QtSvg import QSvgRenderer

# Import the package version so the tray's About dialog and the main
# window's footer label always show the SAME string the CLI banner
# shows. Imported here (rather than as a module-level constant in the
# second block below) so the binding happens at import-time on every
# Python interpreter invocation, including PyInstaller's collection
# scan.
from . import __version__ as _PACKAGE_VERSION

logger = logging.getLogger(__name__)


# ``QMenu`` and ``QSystemTrayIcon`` are imported LAZILY inside the methods that
# actually use them (see ``_ensure_qtwidgets``). Importing ``PySide6.QtWidgets``
# at module load time triggers a STATUS_STACK_BUFFER_OVERRUN (0xc0000409) in
# Qt6Core when ``QGuiApplication`` has not yet been created, which is exactly
# the case when PyInstaller's frozen boot path imports this module via the
# ``hiddenimports`` entry in ``build/RemoteMicRC003.spec``. The first call to
# ``_ensure_qtwidgets`` is from ``TrayController.show()``, which is invoked
# only after ``install_window_event_filter`` has confirmed
# ``QGuiApplication.instance()`` is not None.


def _ensure_qtwidgets():
    """Import ``PySide6.QtWidgets`` lazily and bind ``QMenu``/``QSystemTrayIcon``
    into this module's globals so method bodies can use bare names.

    Safe to call repeatedly: the import itself is guarded, and the names are
    only ever rebound to the same class objects.
    """
    global QMenu, QSystemTrayIcon  # noqa: PLW0603
    from PySide6.QtWidgets import QMenu as _QMenu  # noqa: PLC0415
    from PySide6.QtWidgets import QSystemTrayIcon as _QSTI  # noqa: PLC0415
    QMenu = _QMenu
    QSystemTrayIcon = _QSTI
    return _QMenu, _QSTI


# Module-level placeholders so ``global QMenu, QSystemTrayIcon`` inside
# ``_ensure_qtwidgets`` can rebind these names from ``None`` to the real
# QtWidgets classes on the first call. Never call these directly - always
# go through ``_ensure_qtwidgets``.
QMenu = None  # type: ignore[assignment]
QSystemTrayIcon = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Asset path discovery (mirrors qt_settings_app._qml_directory)
# ---------------------------------------------------------------------------


def _assets_directory() -> Path:
    """Locate the ``assets/`` directory.

    Mirrors ``_qml_directory`` in qt_settings_app.py: in a frozen
    PyInstaller build we expect ``assets/`` to live under
    ``ovb_rc003_assets`` in COLLECT output (set up by
    ``RemoteMicRC003.spec``); in source checkouts it's a sibling of the
    ``ovb_rc003/`` Python package.

    The directory may not exist on disk (e.g. an unfrozen build where
    the user ran the app from a different cwd) - callers handle
    ``FileNotFoundError``.
    """
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "ovb_rc003_assets"
    package_root = Path(__file__).resolve().parent
    # ``tray_icon.py`` lives at <project>/src/ovb_rc003/tray_icon.py.
    # assets/ lives at <project>/assets/ - three parents up (parent is
    # ``ovb_rc003/``, parent's parent is ``src/``, parent's parent's
    # parent is the project root).
    return package_root.parent.parent / "assets"


def _tray_icon_path() -> Path:
    """The canonical tray icon asset path, regardless of frozen / unfrozen.

    Always returns a Path object - callers must check ``.exists()``.

    Resolution order matches ``_load_tray_icon``: prefer SVG (vector,
    scales cleanly to any DPI), fall back to PNG (raster, bundled as
    the Windows .ico extraction of the source app icon when no SVG
    has been authored yet). Both end up rendered by
    ``_build_tray_icon`` at multiple pixel sizes for the tray.
    """
    assets = _assets_directory() / "icons"
    for name in ("app-tray.svg", "app-tray.png"):
        candidate = assets / name
        if candidate.exists():
            return candidate
    # Return the canonical SVG path even if absent so the caller's
    # ``.exists()`` check produces a truthful ``missing: ..." log
    # message naming the expected file.
    return assets / "app-tray.svg"


# ---------------------------------------------------------------------------
# Icon construction
# ---------------------------------------------------------------------------


# Brand colour for the tray glyph. Kept as a module constant so the
# SVG and the fallback pixmap agree; if you ever change the SVG's
# accent colour, change this too.
_BRAND_TEAL = QColor(20, 184, 166)  # matches Tokens.qml "accent" in dark
_FALLBACK_BG = QColor(15, 23, 42)   # slate-900 - dark theme surface


def _load_svg_icon(svg_path: Path, size: int = 64) -> Optional[QIcon]:
    """Rasterize ``svg_path`` to a QIcon of the given pixel size.

    Returns ``None`` on any failure - callers fall back to a
    programmatically-generated pixmap so the user is never left
    without an icon.
    """
    if not svg_path.exists():
        logger.warning("tray icon SVG missing: %s", svg_path)
        return None
    try:
        renderer = QSvgRenderer(str(svg_path))
        if not renderer.isValid():
            logger.warning("tray icon SVG invalid: %s", svg_path)
            return None
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        try:
            renderer.render(painter)
        finally:
            painter.end()
        return QIcon(pixmap)
    except Exception as exc:  # Qt raises a variety of exceptions here
        logger.warning("tray icon SVG render failed (%s): %s", exc, svg_path)
        return None


def _load_png_icon(png_path: Path, size: int = 64) -> Optional[QIcon]:
    """Rasterize ``png_path`` to a QIcon of the given pixel size.

    Used as a tier-2 fallback when no SVG has been authored for the
    app. The PNG is expected to be 256x256 (the size Windows stores
    in the app .ico); Qt's ``QPixmap.scaled`` keeps aspect ratio
    while downsampling to the tray's 16/32/64/128 pixel targets.

    Returns ``None`` on any failure - callers fall back to the
    programmatic glyph.
    """
    if not png_path.exists():
        logger.warning("tray icon PNG missing: %s", png_path)
        return None
    try:
        source = QPixmap(str(png_path))
        if source.isNull():
            logger.warning("tray icon PNG failed to decode: %s", png_path)
            return None
        scaled = source.scaled(
            size, size,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        return QIcon(scaled)
    except Exception as exc:  # Qt raises a variety of exceptions here
        logger.warning("tray icon PNG render failed (%s): %s", exc, png_path)
        return None


def _fallback_icon(size: int = 64) -> QIcon:
    """Programmatically-generated icon used when the SVG can't load.

    A solid teal rounded square with a white microphone silhouette in
    the centre - readable in both light and dark Windows trays, and
    recognisable as "this app, in fallback mode" to anyone who has
    seen the real glyph.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)

        # Rounded background.
        painter.setBrush(_FALLBACK_BG)
        painter.setPen(Qt.NoPen)
        radius = max(4, size // 8)
        painter.drawRoundedRect(0, 0, size, size, radius, radius)

        # White capsule (microphone body).
        body_w = max(2, size // 4)
        body_h = max(2, size // 2)
        body_x = (size - body_w) // 2
        body_y = max(2, size // 5)
        painter.setBrush(QColor("white"))
        painter.drawRoundedRect(
            body_x, body_y, body_w, body_h,
            body_w // 2, body_w // 2,
        )

        # Mic stand: arc + stem.
        painter.setPen(QColor("white"))
        painter.setBrush(Qt.NoBrush)
        stand_w = max(1, 2)
        arc_y = body_y + body_h + max(2, size // 16)
        arc_h = max(2, size // 4)
        painter.drawArc(
            body_x - max(2, size // 8), arc_y,
            body_w + 2 * max(2, size // 8), arc_h,
            0, -180 * 16,  # 180deg, Qt angles are 1/16 degree
        )

        # Stem.
        stem_y_top = arc_y + arc_h // 2
        stem_y_bot = size - max(2, size // 6)
        painter.drawLine(
            size // 2, stem_y_top,
            size // 2, stem_y_bot,
        )

        # Foot.
        foot_w = max(2, size // 3)
        foot_x = size // 2 - foot_w // 2
        painter.drawLine(foot_x, stem_y_bot,
                         foot_x + foot_w, stem_y_bot)
    finally:
        painter.end()
    return QIcon(pixmap)


def _build_tray_icon(svg_path: Path) -> QIcon:
    """Tier the icon loaders: SVG > PNG > programmatic glyph.

    Always returns a usable QIcon. Never raises. Rasterizes at
    several sizes so the system tray gets a crisp icon at any DPI
    (Windows requests 16x16 for the notification area, 32x32 for
    the expanded view, and may go higher for accessibility zoom);
    a single 64x64 rasterization blurs when scaled back up.

    ``svg_path`` is the result of ``_tray_icon_path()`` - if the
    path's suffix is ``.png`` we go straight to the PNG loader and
    skip QSvg entirely (no point parsing a raster through an SVG
    renderer). The SVG/PNG pick keeps the tray icon visually
    consistent with the desktop shortcut and the EXE's embedded
    .ico - all three use the same single source asset on disk.
    """
    icon = QIcon()
    loaded_any = False
    loader = _load_png_icon if svg_path.suffix.lower() == ".png" else _load_svg_icon
    for size in (16, 32, 64, 128):
        loaded = loader(svg_path, size=size)
        if loaded is not None and not loaded.isNull():
            # ``QIcon.addPixmap`` is the documented API for adding
            # per-size rasters. ``QIcon::Mode::Normal`` is the only
            # mode used for the system tray; we don't bother with
            # disabled/selected/active variants.
            icon.addPixmap(loaded.pixmap(size, size))
            loaded_any = True
    if loaded_any and not icon.isNull():
        return icon
    # Source asset never produced a usable raster - fall back. Render the
    # fallback at multiple sizes too so each tray scale is crisp.
    fallback = QIcon()
    for size in (16, 32, 64, 128):
        fallback.addPixmap(_fallback_icon(size).pixmap(size, size))
    return fallback


# ---------------------------------------------------------------------------
# The wrapper class
# ---------------------------------------------------------------------------


# Application version surfaced in the tray's "About" dialog. Kept in
# one place so a release bump is a single edit. The tray module owns
# this constant (rather than importing it from elsewhere) to avoid a
# cross-module import cycle through ``__main__``.
# `_APP_VERSION` is bound to the package's `__version__` so the About
# dialog and the main-window footer always show the SAME string the
# CLI banner shows. See `_tray_about_text` for the consumer.
_APP_DISPLAY_NAME = "Remote Mic Settings"
_APP_VERSION = _PACKAGE_VERSION
_APP_LICENSE_NOTICE = (
    "This software is released under the GNU General Public "
    "License v3.0; see the LICENSE file for details."
)
_APP_HELP_TEXT = (
    "Remote Mic Settings:\n"
    "  \u00b7 Click Open to view or change configuration\n"
    "  \u00b7 After restarting the service, the app will automatically "
    "connect to the remote microphone\n"
    "  \u00b7 If something goes wrong, check the log window for details\n"
    "\n"
    "Hotkeys (enable reverse control in Settings first):\n"
    "  \u00b7 Back \u2192 Idle / default player, previous track\n"
    "  \u00b7 Vol+ \u2192 Increase output volume\n"
    "  \u00b7 Vol\u2212 \u2192 Decrease output volume"
)
# Tray menu labels - canonical 4-item layout, all English:
# Open / Help / About / Exit.
# The Open action toggles settings-window visibility; the label stays
# "Open" in both states (no dynamic Show/Hide flip) per spec.
_LABEL_OPEN_VISIBLE = "Open"
_LABEL_OPEN_HIDDEN = "Open"
_LABEL_HELP = "Help"
_LABEL_ABOUT = "About"
_LABEL_EXIT = "Exit"


class TrayController(QObject):
    """Owns the QSystemTrayIcon and its context menu.

    Menu layout (canonical top-down order):

    - ``Open`` (``_show_text(visible)``) - toggles between
      \u201c\u9690\u85cf\u8bbe\u7f6e\u7a97\u53e3\u201d and
      \u201c\u663e\u793a\u8bbe\u7f6e\u7a97\u53e3\u201d depending on the
      current window state. Emits ``show_window_requested``.
    - ``Help`` (``\u5e2e\u52a9``) - opens an in-process
      ``QMessageBox`` with concise usage instructions. Emits no
      signal.
    - ``About`` (``\u5173\u4e8e``) - opens a ``QMessageBox.about``
      with app name, version, and the GPL-3.0 license notice. Emits
      no signal.
    - ``Exit`` (``\u9000\u51fa``) - emits ``quit_requested``.

    Signals (all delivered on the Qt main thread):

    - ``show_window_requested`` - user wants the settings window
      toggled. The parent should call ``window.show(); window.
      raise_(); window.activateWindow()``.
    - ``quit_requested`` - user chose Exit from the tray menu. The
      parent should call ``QApplication.instance().quit()``.
    """

    show_window_requested = Signal()
    quit_requested = Signal()

    def __init__(
        self,
        *,
        icon_path: Optional[Path] = None,
        app_name: str = _APP_DISPLAY_NAME,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._app_name = app_name
        self._icon_path = icon_path or _tray_icon_path()
        self._tray: Optional[QSystemTrayIcon] = None
        self._menu: Optional[QMenu] = None
        self._show_action: Optional[QAction] = None
        self._help_action: Optional[QAction] = None
        self._about_action: Optional[QAction] = None
        self._quit_action: Optional[QAction] = None
        self._window_visible = True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """True iff the host actually supports a system tray.

        On Windows this is almost always True. We check rather than
        assume because some server SKUs / virtualised desktops lack
        the shell service entirely, and constructing
        ``QSystemTrayIcon`` then silently failing to show the icon is
        a worse UX than simply not showing the toggle in the UI.
        """
        _ensure_qtwidgets()
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self) -> bool:
        """Create and show the tray icon. Idempotent.

        Returns True if the icon is now visible, False if the host
        has no tray (e.g. server SKU). Never raises - tray failure is
        a UX issue, not a fatal one.
        """
        _ensure_qtwidgets()
        if self._tray is not None:
            return True
        if not self.is_available():
            logger.warning(
                "system tray unavailable - skipping tray registration"
            )
            return False
        try:
            icon = _build_tray_icon(self._icon_path)
            self._tray = QSystemTrayIcon(icon, self)
            self._tray.setToolTip(self._app_name)
            self._menu = self._build_menu()
            self._tray.setContextMenu(self._menu)
            self._tray.activated.connect(self._on_activated)
            self._sync_menu_visible_state()
            self._tray.show()
            return True
        except Exception as exc:
            logger.exception("tray show failed: %s", exc)
            self._teardown_tray()
            return False

    def hide(self) -> None:
        """Remove the tray icon. Idempotent."""
        self._teardown_tray()

    def set_window_visible(self, visible: bool) -> None:
        """Update the menu's "Open" label to match reality.

        Called by the parent whenever the settings window's visibility
        changes (initial show, the user closing it to tray, the user
        re-opening it from the tray, etc). Updates the action's text
        and check state.
        """
        self._window_visible = bool(visible)
        self._sync_menu_visible_state()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_menu(self) -> "QMenu":
        """Construct the context menu with the four canonical items.

        Layout (on-screen top-down):

            Open / Help / About / Exit

        Items are added in insertion order; the Fusion-styled Win32
        popup displays them top-down in that same order.  An earlier
        revision added items bottom-up assuming the native popup
        reversed them, but observed behaviour on Windows is the
        opposite - insertion order is the displayed order.
        """
        _ensure_qtwidgets()
        menu = QMenu()

        # Top: Open (toggles window visibility, label is dynamic).
        show_action = QAction(self._show_text(True), menu)
        show_action.triggered.connect(self.show_window_requested)
        menu.addAction(show_action)
        self._show_action = show_action

        # Help.
        help_action = QAction(_LABEL_HELP, menu)
        help_action.triggered.connect(self._show_help)
        menu.addAction(help_action)
        self._help_action = help_action

        # About.
        about_action = QAction(_LABEL_ABOUT, menu)
        about_action.triggered.connect(self._show_about)
        menu.addAction(about_action)
        self._about_action = about_action

        # Bottom: Exit.
        quit_action = QAction(_LABEL_EXIT, menu)
        quit_action.triggered.connect(self.quit_requested)
        menu.addAction(quit_action)
        self._quit_action = quit_action

        return menu

    def _show_text(self, visible: bool) -> str:
        """Localized label for the Open action."""
        if visible:
            return _LABEL_OPEN_VISIBLE  # "隐藏设置窗口"
        return _LABEL_OPEN_HIDDEN        # "显示设置窗口"

    def _show_help(self) -> None:
        """Open an in-process ``QMessageBox`` with usage instructions.

        Uses an injected ``_open_help_dialog`` callable when supplied
        (lets tests assert the trigger fired without showing a real
        dialog); falls back to a real ``QMessageBox.information``
        for production.
        """
        opener = getattr(self, "_open_help_dialog", None)
        if opener is None:
            opener = _default_show_help_dialog
        try:
            opener(self._app_name, _APP_HELP_TEXT)
        except Exception as exc:
            # Never let a broken help popup crash the tray - log
            # and continue. The user can still exit / open settings.
            logger.warning("tray help dialog failed: %s", exc)

    def _show_about(self) -> None:
        """Open a ``QMessageBox.about`` with app name, version, license.

        Same injection point as ``_show_help`` so tests can spy on it
        without spawning a real dialog.
        """
        opener = getattr(self, "_open_about_dialog", None)
        if opener is None:
            opener = _default_show_about_dialog
        try:
            opener(
                self._app_name,
                _APP_DISPLAY_NAME,
                _APP_VERSION,
                _APP_LICENSE_NOTICE,
            )
        except Exception as exc:
            logger.warning("tray about dialog failed: %s", exc)

    def _sync_menu_visible_state(self) -> None:
        if self._show_action is None:
            return
        self._show_action.setText(self._show_text(self._window_visible))

    def _on_activated(
        self, reason: "QSystemTrayIcon.ActivationReason"
    ) -> None:
        """Single-click on the tray icon shows the settings window."""
        _ensure_qtwidgets()
        if reason in (
            QSystemTrayIcon.Trigger,    # single-click on Windows
            QSystemTrayIcon.DoubleClick,
        ):
            self.show_window_requested.emit()

    def _teardown_tray(self) -> None:
        if self._tray is not None:
            try:
                self._tray.hide()
            except Exception:
                pass
            self._tray.deleteLater()
            self._tray = None
        self._menu = None
        self._show_action = None
        self._help_action = None
        self._about_action = None
        self._quit_action = None


def _default_show_help_dialog(app_name: str, body: str) -> None:
    """Real ``QMessageBox.information`` used by ``_show_help`` in production."""
    _ensure_qtwidgets()
    from PySide6.QtWidgets import QMessageBox  # local: avoid heavy import at module load
    QMessageBox.information(None, f"\u5e2e\u52a9 - {app_name}", body)  # "帮助 - <app>"


def _default_show_about_dialog(
    app_name: str, display_name: str, version: str, license_notice: str
) -> None:
    """Real ``QMessageBox.about`` used by ``_show_about`` in production."""
    _ensure_qtwidgets()
    from PySide6.QtWidgets import QMessageBox  # local: avoid heavy import at module load
    body = f"{display_name}\n\u7248\u672c\uff1a{version}\n\n{license_notice}"
    # body = "<display_name>\n版本：<version>\n\n<license>"
    QMessageBox.about(None, f"\u5173\u4e8e - {app_name}", body)  # "关于 - <app>"


__all__ = ["TrayController"]