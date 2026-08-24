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

* The tray context menu has exactly three items:
    - ``Show settings`` (single-purpose action, dynamic label)
    - ``Open Startup folder`` (convenience - lets users inspect the
      start-with-Windows shortcut without opening File Explorer
      manually; only enabled when the shortcut exists)
    - ``Quit`` (terminates the entire app, matching the existing
      close-window-quits-app default - see how the parent wires
      ``setQuitOnLastWindowClosed``)
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
)
from PySide6.QtSvg import QSvgRenderer

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
    """The canonical SVG asset path, regardless of frozen / unfrozen.

    Always returns a Path object - callers must check ``.exists()``.
    """
    return _assets_directory() / "icons" / "app-tray.svg"


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
    """Try the SVG, fall back to the programmatic pixmap.

    Always returns a usable QIcon. Never raises. Rasterizes at
    several sizes so the system tray gets a crisp icon at any DPI
    (Windows requests 16x16 for the notification area, 32x32 for
    the expanded view, and may go higher for accessibility zoom);
    a single 64x64 rasterization blurs when scaled back up.
    """
    icon = QIcon()
    loaded_any = False
    for size in (16, 32, 64, 128):
        svg_icon = _load_svg_icon(svg_path, size=size)
        if svg_icon is not None and not svg_icon.isNull():
            # ``QIcon.addPixmap`` is the documented API for adding
            # per-size rasters. ``QIcon::Mode::Normal`` is the only
            # mode used for the system tray; we don't bother with
            # disabled/selected/active variants.
            icon.addPixmap(svg_icon.pixmap(size, size))
            loaded_any = True
    if loaded_any and not icon.isNull():
        return icon
    # SVG never produced a usable raster - fall back. Render the
    # fallback at multiple sizes too so each tray scale is crisp.
    fallback = QIcon()
    for size in (16, 32, 64, 128):
        fallback.addPixmap(_fallback_icon(size).pixmap(size, size))
    return fallback


# ---------------------------------------------------------------------------
# The wrapper class
# ---------------------------------------------------------------------------


class TrayController(QObject):
    """Owns the QSystemTrayIcon and its context menu.

    Signals (all delivered on the Qt main thread):

    - ``show_window_requested`` - user wants the settings window shown.
      The parent should call ``window.show(); window.raise_(); window.
      activateWindow()``.
    - ``quit_requested`` - user chose Quit from the tray menu. The
      parent should call ``QApplication.instance().quit()``.
    - ``open_startup_folder_requested`` - convenience signal for the
      "open shell:startup" menu item.
    """

    show_window_requested = Signal()
    quit_requested = Signal()
    open_startup_folder_requested = Signal()

    def __init__(
        self,
        *,
        icon_path: Optional[Path] = None,
        app_name: str = "Remote Mic",
        parent: Optional[QObject] = None,
        # ``is_shortcut_active`` is injected so the
        # "Open Startup folder" menu item enables/disables itself to
        # match reality. The parent calls ``set_shortcut_active`` to
        # refresh it after any change.
        is_shortcut_active_provider: Callable[[], bool] = lambda: False,
    ) -> None:
        super().__init__(parent)
        self._app_name = app_name
        self._icon_path = icon_path or _tray_icon_path()
        self._is_shortcut_active = is_shortcut_active_provider
        self._tray: Optional[QSystemTrayIcon] = None
        self._menu: Optional[QMenu] = None
        self._show_action: Optional[QAction] = None
        self._open_startup_action: Optional[QAction] = None
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
            self._sync_startup_item_state()
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
        """Update the menu's "Show/Hide" label to match reality.

        Called by the parent whenever the settings window's visibility
        changes (initial show, the user closing it to tray, the user
        re-opening it from the tray, etc). Updates the action's text
        and check state.
        """
        self._window_visible = bool(visible)
        self._sync_menu_visible_state()

    def set_shortcut_active(self, active: bool) -> None:
        """Refresh the "Open Startup folder" menu item's enabled state.

        Called by the parent after ``startup_shortcut.enable`` /
        ``disable`` completes, so the menu item matches what's on
        disk.
        """
        self._sync_startup_item_state()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_menu(self) -> "QMenu":
        """Construct the context menu with all three items wired up."""
        _ensure_qtwidgets()
        menu = QMenu()

        show_action = QAction(self._show_text(True), menu)
        show_action.triggered.connect(self.show_window_requested)
        menu.addAction(show_action)
        self._show_action = show_action

        # Separator before the secondary actions so the menu reads
        # top-down: primary action, secondary, quit.
        menu.addSeparator()

        open_startup = QAction(
            "\u6253\u5f00\u542f\u52a8\u6587\u4ef6\u5939",  # "打开启动文件夹"
            menu,
        )
        open_startup.triggered.connect(
            self.open_startup_folder_requested
        )
        menu.addAction(open_startup)
        self._open_startup_action = open_startup

        quit_action = QAction("\u9000\u51fa", menu)  # "退出"
        quit_action.triggered.connect(self.quit_requested)
        menu.addAction(quit_action)
        self._quit_action = quit_action

        return menu

    def _show_text(self, visible: bool) -> str:
        """Localized label for the show/hide action."""
        if visible:
            return "\u9690\u85cf\u8bbe\u7f6e\u7a97\u53e3"  # "隐藏设置窗口"
        return "\u663e\u793a\u8bbe\u7f6e\u7a97\u53e3"  # "显示设置窗口"

    def _sync_menu_visible_state(self) -> None:
        if self._show_action is None:
            return
        self._show_action.setText(self._show_text(self._window_visible))

    def _sync_startup_item_state(self) -> None:
        if self._open_startup_action is None:
            return
        try:
            enabled = bool(self._is_shortcut_active())
        except Exception:
            enabled = False
        self._open_startup_action.setEnabled(enabled)

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
        self._open_startup_action = None
        self._quit_action = None


__all__ = ["TrayController"]