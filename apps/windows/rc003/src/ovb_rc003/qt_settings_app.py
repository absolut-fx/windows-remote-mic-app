"""PySide6-Essentials + Qt Quick/QML settings window (XRBM-030/XRBM-031),
replacing the previous Tk view. Every validation/save/launch/log-status
decision below still goes straight through the same pure functions in
``settings_ui.py`` - this module only bridges them to QML via a
`QAbstractListModel` (``ButtonMappingModel``) and two `QObject`s
(``SettingsController``, ``DiagnosticsController``); it never duplicates
that logic.

This module deliberately does NOT import PySide6 at module import time.
``_load_qt_classes()`` performs that import lazily (and caches the result),
so merely importing ``ovb_rc003.qt_settings_app`` - e.g. transitively via
``settings_ui`` in a ``--dry-run`` smoke check, or from a pure test that
only needs ``remote_layout``/``shell_targets`` - never requires PySide6 to
be installed. ``run_settings_window()`` is the only real entry point, and
raises ``QtUnavailableError`` with an actionable message if PySide6-
Essentials is missing (source/dev runs only: the frozen build always bundles
the Qt runtime itself - see build/RemoteMicRC003.spec - so end users
never need to separately install Python or Qt).

``DiagnosticsController`` (XRBM-031's "检查与修复" fourth page) runs every
``windows_diagnostics.run_diagnostics()`` check on a plain background
``threading.Thread`` - never on the Qt GUI thread, so a slow WinRT/PortAudio
call can never freeze the window - and delivers the result back via a
cross-thread Qt signal (``Signal(object)``), which Qt automatically queues
onto the GUI thread because the receiving ``QObject`` was constructed there;
this is the standard, documented way to marshal a background-thread result
back to a Qt object living on a different thread, and needs no ``QThread``
subclass or extra locking. An ``_is_refreshing`` guard refuses to start a
second worker while one is already running (repeated "重新检测" clicks never
overlap).

Thread lifecycle at window close / process exit (XRBM-035, hardened again in
RETRY 1 - a real Windows CI crash, not merely a theoretical race,
superseded the previous "best-effort atexit join + daemon=True is good
enough" contract described here before): every background thread this
controller starts is tracked in ``_diagnostics_threads``, and
``_shutdown_diagnostics_workers()`` signals shutdown and makes a BOUNDED
join attempt on each (``_DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS`` per
thread - DERIVED from ``windows_diagnostics.BLE_DISCOVERY_MAX_CANCELLATION_
SECONDS`` plus a safety margin, not an independently-guessed value - see
that constant's own definition below). The critical fix is WHEN this runs:
``run_settings_window()`` now calls it EXPLICITLY, synchronously, from a
``try/finally`` that starts right after ``DiagnosticsController`` is
constructed (i.e. right after its background worker could first exist) and
covers every exit path through ``app.exec()`` returning, ``engine.load()``
raising, or ``rootObjects()`` coming back empty - while every Qt/Python
object it built is still fully alive - not only via this module's
``atexit`` hook (``_shutdown_qt_settings_app_at_exit()``, still registered
as a defense-in-depth safety net for callers that bypass
``run_settings_window()``). A real Windows CI faulthandler dump proved the
old atexit-only timing insufficient: a background BLE-discovery worker was
still deep inside a native WinRT await when the interpreter itself began
finalizing, producing an ``0xC0000005`` access violation - daemon=True only
guarantees CPython does not block exit on a surviving thread, it says
nothing about whether that thread's native call can safely keep running
concurrently with interpreter teardown.

RETRY 1's independent review found the round-1 fix for the discovery side
itself - cancelling the asyncio Task awaiting ``discover_candidates()`` -
was ALSO insufficient: the locked pywinrt wrapper's own post-cancel wait is
itself unbounded (see ``windows_diagnostics.py``'s "-- BLE candidate --"
section for the exact source citation), so an in-process asyncio
cancellation request could never give a real hard bound either. BLE
candidate discovery therefore now runs in a genuinely separate, disposable
OS PROCESS (``windows_diagnostics._run_ble_diagnostics_subprocess()``) that
the parent can forcibly terminate/kill and CONFIRM dead within a real,
OS-enforced bound - the shutdown event doubles as that cancellation signal
(not just an emit-skip flag - see below), so a discovery attempt in flight
when shutdown begins is actually asked to stop and confirmed to have
stopped, giving the bounded join here a realistic chance to succeed instead
of only ever timing out. Every one of these threads is still created with
``daemon=True`` too, as a last-resort backstop if a future failure mode
ever defeats the process-level isolation above.

Shutdown-vs-teardown ordering (XRBM-031 RETRY 2, still true under XRBM-035):
a diagnostics worker still finishing around shutdown time must never emit
its result INTO a ``DiagnosticsController``/Qt runtime that
``_release_qt_classes_cache()`` may already have started tearing down. Two
things make this safe: ``_diagnostics_shutdown_event`` (a module-level
``threading.Event``) is set FIRST, before anything else, by
``_shutdown_diagnostics_workers()`` - ``refreshDiagnostics()`` refuses to
start a new worker once it is set, and a worker already running checks it
immediately before emitting and skips the emit entirely if it is set (any
exception the emit call raises anyway - the receiver could still be
mid-teardown despite the check - is caught and discarded, never crashing
the worker thread); and every worker's ``finally`` block unconditionally
calls ``_forget_diagnostics_thread()``, so the registry is cleaned up even
on that path. ``_shutdown_qt_settings_app_at_exit()`` still runs all three
shutdown steps (flag the shutdown, join outstanding workers, release the Qt
classes cache) in that explicit order, in one function - never relying on
Python's ``atexit`` LIFO-ordered execution of separately registered
functions (see that function's docstring for the full story).
"""

from __future__ import annotations

import atexit
import gc
import logging
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

from . import (
    audio_output,
    bridge_launcher,
    config,
    device_catalog,
    hotkey,
    hotkey_capture_windows,
    key_mapping,
    logging_setup,
    remote_layout,
    raw_input_windows,
    resources,
    settings_ui,
    shell_targets,
    startup_shortcut,
    tray_icon,
    vb_cable_bundle,
    windows_diagnostics,
)

logger = logging.getLogger(__name__)

# These are the reference-style semantic action choices offered in each
# editable ordinary-button mapping row.  The combo box stays editable (not
# "readonly"): any other ordinary chord or modifier-only chord is still
# accepted as a user custom shortcut through hotkey.HotkeySpec.parse.
_PRESET_ACTION_OPTIONS: List[str] = list(dict.fromkeys(settings_ui._PRESET_KEY_COMBOS))
# Keep the explicit optional-gesture state selectable and stable in an
# editable ComboBox.  Without a value that is present in the model, Qt can
# display the first real preset while the underlying model is actually blank.
_PRESET_ACTION_OPTIONS.insert(0, settings_ui.SECONDARY_UNCONFIGURED_DISPLAY)


class QtUnavailableError(RuntimeError):
    """Raised by run_settings_window() when PySide6-Essentials is not
    importable. See module docstring.
    """


def _qml_directory() -> Path:
    """Locates the ``qml/`` directory this module's QML files live in,
    mirroring resources.py's frozen-vs-source-checkout lookup: in a frozen
    (PyInstaller) build, ``build/RemoteMicRC003.spec`` collects the
    qml sources under ``ovb_rc003_qml`` inside the COLLECT output, which the
    bootloader exposes via ``sys._MEIPASS`` at runtime (see resources.py's
    module docstring for why ``sys._MEIPASS`` - not the exe's own directory
    - is the correct base path for bundled, non-Python data in a one-dir
    build). In an unfrozen (source checkout or ``pip install``) run, the
    qml/ directory is simply this module's own sibling directory.
    """

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "ovb_rc003_qml"
    return Path(__file__).resolve().parent / "qml"


_qt_classes_cache: Optional[dict] = None

# Every background diagnostics worker thread DiagnosticsController.
# refreshDiagnostics() starts is appended here (and discarded once it
# finishes - see _forget_diagnostics_thread()), purely so tests/diagnostics
# can observe how many are currently tracked. This is NOT what makes thread
# cleanup safe - see module docstring's "Thread lifecycle at process exit"
# section (XRBM-031 RETRY 1 item 6): _join_diagnostics_threads_at_exit()
# below only ever makes a bounded, best-effort join attempt; the actual
# safety property (process exit is never blocked indefinitely) comes from
# every one of these threads being created with daemon=True.
_diagnostics_threads: "list[threading.Thread]" = []
_diagnostics_threads_lock = threading.Lock()

# Set exactly once, by _begin_diagnostics_shutdown() (only ever called from
# _shutdown_qt_settings_app_at_exit() in production - see that function's
# docstring), BEFORE anything else happens at process exit (XRBM-031 RETRY
# 2). refreshDiagnostics() refuses to start a new worker once this is set;
# a worker already running checks it immediately before emitting its result
# and skips the emit entirely if it is set, so a diagnostics result can
# never be delivered into a DiagnosticsController/Qt runtime that may
# already be mid-teardown. Tests that set this directly (rather than via
# _shutdown_qt_settings_app_at_exit()) MUST .clear() it afterward - it is
# process-global, persistent state, not per-test.
_diagnostics_shutdown_event = threading.Event()

# Safety margin ON TOP OF windows_diagnostics.BLE_DISCOVERY_MAX_
# CANCELLATION_SECONDS below - covers the worker thread's own minimal
# cleanup after _run_ble_diagnostics_subprocess() returns/raises (returning
# through check_ble_candidate()/run_diagnostics()/_run_in_background()'s own
# finally block), not the subprocess termination itself.
_DIAGNOSTICS_THREAD_JOIN_SAFETY_MARGIN_SECONDS = 2.0

# Per-thread bound for the best-effort atexit join below - a module-level
# constant (rather than a literal inline) specifically so a test can lower
# it and prove the join is genuinely bounded/non-hanging without waiting
# out the real default (see tests/test_qt_settings_app.py).
#
# XRBM-035 RETRY 1 P1 #2: DERIVED from windows_diagnostics.BLE_DISCOVERY_
# MAX_CANCELLATION_SECONDS (poll-detection latency + both escalating
# subprocess-termination waits), not an independently-guessed flat value -
# an independent review found the previous flat 2.0s was actually SMALLER
# than that module's own worst-case termination bound (poll 0.1s +
# terminate_wait 2.0s + kill_wait 2.0s = 4.1s), so this join could return
# "timed out" even on the happy path where the subprocess layer behaved
# exactly as designed and eventually confirmed the child's death. Deriving
# this value FROM that module's own constant means the two can never
# silently drift apart again - if that module's termination bound ever
# changes, this one moves with it automatically.
_DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS = (
    windows_diagnostics.BLE_DISCOVERY_MAX_CANCELLATION_SECONDS
    + _DIAGNOSTICS_THREAD_JOIN_SAFETY_MARGIN_SECONDS
)


def _remember_diagnostics_thread(thread: "threading.Thread") -> None:
    with _diagnostics_threads_lock:
        _diagnostics_threads.append(thread)


def _forget_diagnostics_thread(thread: "threading.Thread") -> None:
    with _diagnostics_threads_lock:
        if thread in _diagnostics_threads:
            _diagnostics_threads.remove(thread)


def _begin_diagnostics_shutdown() -> None:
    """Flags that process shutdown has begun - see
    ``_diagnostics_shutdown_event``'s own comment above and
    ``_shutdown_qt_settings_app_at_exit()``'s docstring below for the full
    ordering contract this is one step of. Idempotent (``Event.set()`` is
    always safe to call more than once); kept as its own tiny function
    purely so a test can call/assert on this ONE step in isolation from the
    join/cache-release steps that follow it.
    """

    _diagnostics_shutdown_event.set()


def _join_diagnostics_threads_at_exit() -> None:
    """Best-effort courtesy only - see module docstring's "Thread lifecycle
    at process exit" section. Never treat this function returning as proof
    every thread it attempted to join has actually stopped. Deliberately
    does NOT itself touch ``_diagnostics_shutdown_event`` (that is
    ``_begin_diagnostics_shutdown()``'s job, called separately, first, by
    ``_shutdown_qt_settings_app_at_exit()``) - kept orthogonal so tests can
    still call this function alone (as XRBM-031 RETRY 1's tests already do)
    without it leaving global shutdown state behind for later tests.
    """

    with _diagnostics_threads_lock:
        threads = list(_diagnostics_threads)
    for thread in threads:
        thread.join(timeout=_DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS)


def _release_qt_classes_cache() -> None:
    """Drops this module's cached dynamically-created
    ``QObject``/``QAbstractListModel`` subclasses and asks the cyclic
    garbage collector to reclaim them while the interpreter is still fully
    alive, rather than leaving that to CPython's own shutdown-time
    finalization pass. Only ever called from
    ``_shutdown_qt_settings_app_at_exit()`` below (see that function's
    docstring for why it must run LAST, after diagnostics shutdown/join).

    Root cause this works around: ``PySide6.QtCore.Property`` descriptors
    hold a reference cycle back to their owning class that shiboken's C
    extension type does not fully support ``tp_clear`` for - reproduced
    with a minimal, completely unrelated repro (a single trivial
    ``Property``-having ``QObject`` subclass, left referenced at module
    scope with no closures, no caching, nothing else from this project
    involved). ``gc.collect()`` called explicitly *before* shutdown resolves
    that same cycle cleanly; the identical cycle left for ``Py_FinalizeEx``'s
    own final collection pass instead prints
    ``ResourceWarning: gc: N uncollectable objects at shutdown`` - a
    substring this project's ``windows-rc003-ci.yml`` test-suite step
    explicitly greps for and fails the build on (see
    tests/test_qt_lifecycle_cleanup.py for the subprocess-based regression
    proving this exact command stays clean).

    A deliberate no-op whenever ``_load_qt_classes()`` was never actually
    called in this process (the cache is still ``None``), which covers
    every non-Qt test process and ``--dry-run``.
    """

    global _qt_classes_cache
    if _qt_classes_cache is None:
        return
    _qt_classes_cache = None
    gc.collect()


def _shutdown_diagnostics_workers() -> None:
    """The ONE production shutdown contract for in-flight diagnostics
    workers (XRBM-035): flag shutdown, THEN bounded-wait for every tracked
    worker thread - steps 1+2 of ``_shutdown_qt_settings_app_at_exit()``'s
    three steps, factored out into their own function so both
    ``run_settings_window()`` (called explicitly right after ``app.exec()``
    returns, while every Qt/Python object it built is still fully alive -
    see that function) and the real QML load probe
    (``tests/test_qt_settings_app.py``'s ``_QML_LOAD_PROBE_SCRIPT``, which
    reproduces a settings window closing before a real background BLE
    discovery finishes) call the EXACT SAME helper, instead of each
    reimplementing shutdown or relying solely on this module's ``atexit``
    hook.

    Why this matters (XRBM-034 REPLAN, XRBM-035 red evidence): a Windows CI
    run's faulthandler dump showed the real crash thread still deep inside
    ``ble_transport_winrt.discover_candidates()``'s WinRT
    ``find_all_async_aqs_filter`` await, called from a
    ``DiagnosticsController`` background worker, well after this module's
    own ``atexit``-only join had already returned (its 2-second best-effort
    bound elapsing without the thread actually stopping) - by the time the
    interpreter itself began finalizing, that native WinRT call was still
    running concurrently with CPython/shiboken teardown, producing the
    observed ``0xC0000005`` access violation. Calling this function
    EXPLICITLY, synchronously, at the natural point the window is closing
    (not only via ``atexit``, which can fire arbitrarily late relative to
    Qt/native object teardown) gives every in-flight BLE discovery a real,
    bounded chance to be forcibly terminated and CONFIRMED dead at the OS
    process level (see
    ``windows_diagnostics._run_ble_diagnostics_subprocess()``) before that
    teardown ever begins - not merely a best-effort join on a thread nothing
    ever asked to stop.

    Idempotent - ``_begin_diagnostics_shutdown()`` is (``Event.set()`` is
    always safe to call more than once) and ``_join_diagnostics_threads_at_
    exit()`` is (an empty/already-finished registry joins instantly) - safe
    to call once here and again later via ``_shutdown_qt_settings_app_at_
    exit()`` as a defense-in-depth safety net for any path that does not go
    through ``run_settings_window()``.
    """

    _begin_diagnostics_shutdown()
    _join_diagnostics_threads_at_exit()


def _shutdown_qt_settings_app_at_exit() -> None:
    """The ONLY function this module registers via ``atexit`` (XRBM-031
    RETRY 2). Runs every shutdown step in one explicit, hard-coded order:

    0. ``take_bridge_handle()`` - if a bridge is still running, stop it.
       Runs FIRST so a bridge that finally exits as we tear down does not
       emit diagnostic frames into a half-torn-down Qt runtime, and so
       the bridge's HID pipe shutdown does not race with
       ``_release_qt_classes_cache()``.
    1.-2. ``_shutdown_diagnostics_workers()`` - flags shutdown, then makes a
       bounded join attempt on every tracked worker thread (see that
       function's own docstring for why this is also called explicitly by
       ``run_settings_window()``, not only reached here);
    3. ``_release_qt_classes_cache()`` - only now, after every worker has
       either finished or had its bounded join time out, release the
       cached Qt classes.

    Why this is ONE function instead of separately ``atexit.register()``-ing
    each step (which is what the original XRBM-031 submission and its RETRY
    1 fix both did): ``atexit`` runs its registered functions in LIFO order
    (last registered, first executed) - registering the join hook first and
    the cache-release hook second, as before, meant the cache release
    actually ran FIRST at real process exit, the reverse of the order this
    module's own docstring already claimed. A diagnostics worker finishing
    right around that window could then emit its result into a
    ``DiagnosticsController``/Qt runtime whose cached classes were already
    being torn down. Collapsing every step into one function and
    registering that ONE function removes the dependency on ``atexit``'s
    LIFO ordering (and the silent-breakage risk of some future edit
    reordering two separate ``atexit.register()`` calls) entirely - the
    order is just ordinary, explicit Python statement order here.

    This remains registered as a defense-in-depth safety net (e.g. for a
    caller that never reaches ``run_settings_window()``'s own explicit
    call) - production windows must never depend on ``atexit`` alone; see
    ``_shutdown_diagnostics_workers()``'s docstring for why.
    """

    handle = take_bridge_handle()
    if handle is not None:
        bridge_launcher.stop_bridge(handle)

    _shutdown_diagnostics_workers()
    _release_qt_classes_cache()


# Module-level bridge-handle cache.
#
# ``_shutdown_qt_settings_app_at_exit`` is module-level and runs from
# ``atexit`` (or, defensively, from ``run_settings_window()``'s own
# ``finally`` block) - it cannot reach a live ``SettingsController``
# QObject, and PySide6 finalising the QObject graph at process exit makes
# the relationship fragile. Instead the controller publishes the live
# ``BridgeHandle`` here whenever it successfully starts a bridge; the
# shutdown function consumes it. Mirrors the singleton-tracker pattern
# already used by ``_qt_classes_cache`` above (same module, same scope).
_bridge_handle_global: Optional[bridge_launcher.BridgeHandle] = None


def register_bridge_handle(handle: bridge_launcher.BridgeHandle) -> None:
    """Called by ``SettingsController.saveAndLaunch`` after a successful
    ``launch_bridge()``. Replaces any prior handle (the previously running
    bridge, if any, was either already stopped by ``stop_bridge()`` or
    was never tracked - so the only safe action is to release the prior
    handle to the caller before replacing)."""

    global _bridge_handle_global
    prior = _bridge_handle_global
    _bridge_handle_global = handle
    if prior is not None and prior is not handle:
        bridge_launcher.stop_bridge(prior)


def clear_bridge_handle() -> None:
    """Called by ``SettingsController.stopBridge`` (after stopping the
    bridge) and by ``_get_bridge_running`` when it observes the process
    has died on its own. Idempotent."""

    global _bridge_handle_global
    _bridge_handle_global = None


def take_bridge_handle() -> Optional[bridge_launcher.BridgeHandle]:
    """Called by ``_shutdown_qt_settings_app_at_exit``. Atomically removes
    AND returns the cached handle so the caller can stop exactly the
    process it observes (no second copy to fight over)."""

    global _bridge_handle_global
    handle = _bridge_handle_global
    _bridge_handle_global = None
    return handle


atexit.register(_shutdown_qt_settings_app_at_exit)


def _load_qt_classes() -> dict:
    """Imports PySide6 and defines every QObject/QAbstractListModel
    subclass this module needs, INSIDE this function body - not at module
    level - so that importing ``qt_settings_app`` itself never requires
    PySide6 (see module docstring). Cached after the first successful call
    within a process; a missing-PySide6 failure is never cached, so a
    caller that installs PySide6 into the same running process (unlikely in
    practice, but exercised by tests) would see a subsequent call succeed.
    """

    global _qt_classes_cache
    if _qt_classes_cache is not None:
        return _qt_classes_cache

    try:
        from PySide6.QtCore import (
            Property,
            QAbstractListModel,
            QByteArray,
            QCoreApplication,
            QEvent,
            QModelIndex,
            QObject,
            Qt,
            QUrl,
            Signal,
            Slot,
        )
        from PySide6.QtGui import QCloseEvent, QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterSingletonInstance
        from PySide6.QtQuickControls2 import QQuickStyle
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        raise QtUnavailableError(
            "PySide6-Essentials 未安装，无法打开 Qt 设置界面。源码运行请先在本项目"
            "的虚拟环境中执行 `pip install -r requirements.txt`（已包含 "
            "PySide6-Essentials）；打包后的 RemoteMicRC003.exe 自带 Qt 运行"
            "时，不需要终端用户单独安装 Python 或 Qt。"
        ) from exc

    _DisplayRole = Qt.ItemDataRole.DisplayRole
    _UserRole = Qt.ItemDataRole.UserRole

    class ButtonMappingModel(QAbstractListModel):
        """One row per physical RC003 button (13 total, in
        remote_layout.BUTTON_ORDER - 12 ordinary HID buttons plus the fixed
        mic), exposing both its product-photo hotspot geometry and its
        current mapping-action text to QML, so the photo's clickable
        hotspots and the mapping list are two views over the SAME row data
        rather than two independently-tracked selections.
        """

        ButtonIdRole = _UserRole + 1
        DisplayNameRole = _UserRole + 2
        HidUsageRole = _UserRole + 3
        ActionTextRole = _UserRole + 4
        DoubleClickTextRole = _UserRole + 5
        LongPressTextRole = _UserRole + 6
        IsMicRole = _UserRole + 7
        IsSelectedRole = _UserRole + 8
        XRole = _UserRole + 9
        YRole = _UserRole + 10
        WidthRole = _UserRole + 11
        HeightRole = _UserRole + 12
        IsVoiceRole = _UserRole + 13

        # Emitted whenever a QML combo box edits a row's action text
        # (button_id, new display text) - SettingsController does not need
        # this directly (it reads the model back at save time via
        # to_display_map()), but it is kept for any future listener/test.
        actionEdited = Signal(str, str)

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self._button_ids: List[str] = list(remote_layout.BUTTON_ORDER)
            self._action_text: Dict[str, str] = {bid: "" for bid in self._button_ids}
            self._secondary_action_text: Dict[str, Dict[str, str]] = {
                bid: {
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value: (
                        settings_ui.SECONDARY_UNCONFIGURED_DISPLAY
                    ),
                    key_mapping.ButtonTrigger.LONG_PRESS.value: (
                        settings_ui.SECONDARY_UNCONFIGURED_DISPLAY
                    ),
                }
                for bid in self._button_ids
            }
            self._selected_button_id: str = "ok"

        def rowCount(self, parent=QModelIndex()) -> int:  # noqa: B008 - QML model convention
            if parent.isValid():
                return 0
            return len(self._button_ids)

        def roleNames(self):
            return {
                self.ButtonIdRole: QByteArray(b"buttonId"),
                self.DisplayNameRole: QByteArray(b"displayName"),
                self.HidUsageRole: QByteArray(b"hidUsage"),
                self.ActionTextRole: QByteArray(b"actionText"),
                self.DoubleClickTextRole: QByteArray(b"doubleClickText"),
                self.LongPressTextRole: QByteArray(b"longPressText"),
                self.IsMicRole: QByteArray(b"isMic"),
                self.IsSelectedRole: QByteArray(b"isSelected"),
                self.XRole: QByteArray(b"hotspotX"),
                self.YRole: QByteArray(b"hotspotY"),
                self.WidthRole: QByteArray(b"hotspotWidth"),
                self.HeightRole: QByteArray(b"hotspotHeight"),
                self.IsVoiceRole: QByteArray(b"isVoice"),
            }

        def data(self, index, role: int = _DisplayRole):
            if not index.isValid() or not (0 <= index.row() < len(self._button_ids)):
                return None
            button_id = self._button_ids[index.row()]
            hotspot = remote_layout.hotspot_for(button_id)
            if role in (self.ButtonIdRole, _DisplayRole):
                return button_id
            if role == self.DisplayNameRole:
                return remote_layout.BUTTON_DISPLAY_NAMES[button_id]
            if role == self.HidUsageRole:
                return remote_layout.hid_usage_display(button_id)
            if role == self.ActionTextRole:
                if button_id == "mic":
                    return settings_ui._MIC_ROW_DISPLAY
                return self._action_text[button_id]
            if role == self.DoubleClickTextRole:
                return self._secondary_action_text[button_id][
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value
                ]
            if role == self.LongPressTextRole:
                return self._secondary_action_text[button_id][
                    key_mapping.ButtonTrigger.LONG_PRESS.value
                ]
            if role == self.IsMicRole:
                return button_id == "mic"
            if role == self.IsSelectedRole:
                return button_id == self._selected_button_id
            if role == self.XRole:
                return hotspot.x if hotspot else 0.0
            if role == self.YRole:
                return hotspot.y if hotspot else 0.0
            if role == self.WidthRole:
                return hotspot.width if hotspot else 0.0
            if role == self.HeightRole:
                return hotspot.height if hotspot else 0.0
            if role == self.IsVoiceRole:
                return bool(hotspot and hotspot.is_voice)
            return None

        def load_display_map(
            self,
            display_map: Dict[str, str],
            secondary_display_map: Optional[Dict[str, Dict[str, str]]] = None,
        ) -> None:
            """Resets every non-mic row's action text from a
            button_id -> display-text mapping (settings_ui.DefaultDisplayState
            or a loaded config's bindings) - the mic row is never taken from
            here (see data()'s ActionTextRole branch: it always renders the
            fixed settings_ui._MIC_ROW_DISPLAY regardless of what this dict
            contains).
            """

            self.beginResetModel()
            for button_id in self._button_ids:
                self._action_text[button_id] = display_map.get(button_id, "")
                trigger_map = (secondary_display_map or {}).get(button_id, {})
                self._secondary_action_text[button_id] = {
                    key_mapping.ButtonTrigger.DOUBLE_CLICK.value: trigger_map.get(
                        key_mapping.ButtonTrigger.DOUBLE_CLICK.value,
                        settings_ui.SECONDARY_UNCONFIGURED_DISPLAY,
                    ),
                    key_mapping.ButtonTrigger.LONG_PRESS.value: trigger_map.get(
                        key_mapping.ButtonTrigger.LONG_PRESS.value,
                        settings_ui.SECONDARY_UNCONFIGURED_DISPLAY,
                    ),
                }
            self.endResetModel()

        def to_display_map(self) -> Dict[str, str]:
            """Inverse of load_display_map() - what build_save_model()'s
            button_display_map argument needs. Deliberately excludes "mic"
            (build_save_model() forces that binding to VOICE unconditionally
            regardless of what is passed in, so there is nothing meaningful
            to report for it here either).
            """

            return {
                button_id: text
                for button_id, text in self._action_text.items()
                if button_id != "mic"
            }

        def to_secondary_display_map(self) -> Dict[str, Dict[str, str]]:
            return {
                button_id: {
                    trigger: (
                        ""
                        if text == settings_ui.SECONDARY_UNCONFIGURED_DISPLAY
                        else text
                    )
                    for trigger, text in trigger_map.items()
                }
                for button_id, trigger_map in self._secondary_action_text.items()
                if button_id != "mic"
            }

        def index_of(self, button_id: str) -> int:
            try:
                return self._button_ids.index(button_id)
            except ValueError:
                return -1

        @Slot(str, result=int)
        def indexOfButton(self, button_id: str) -> int:
            return self.index_of(button_id)

        @Slot(int, str)
        def setActionTextAt(self, row: int, text: str) -> None:
            if not (0 <= row < len(self._button_ids)):
                return
            button_id = self._button_ids[row]
            if button_id == "mic":
                return  # fixed, not editable - see data()'s ActionTextRole branch
            self._action_text[button_id] = text
            model_index = self.index(row, 0)
            self.dataChanged.emit(model_index, model_index, [self.ActionTextRole])
            self.actionEdited.emit(button_id, text)

        @Slot(int, str, str)
        def setSecondaryActionTextAt(self, row: int, trigger: str, text: str) -> None:
            if not (0 <= row < len(self._button_ids)):
                return
            if trigger not in {
                key_mapping.ButtonTrigger.DOUBLE_CLICK.value,
                key_mapping.ButtonTrigger.LONG_PRESS.value,
            }:
                return
            button_id = self._button_ids[row]
            if button_id == "mic":
                return
            self._secondary_action_text[button_id][trigger] = text
            model_index = self.index(row, 0)
            role = (
                self.DoubleClickTextRole
                if trigger == key_mapping.ButtonTrigger.DOUBLE_CLICK.value
                else self.LongPressTextRole
            )
            self.dataChanged.emit(model_index, model_index, [role])

        def set_selected_button(self, button_id: str) -> None:
            if button_id == self._selected_button_id or button_id not in self._action_text:
                return
            old_row = self.index_of(self._selected_button_id)
            self._selected_button_id = button_id
            new_row = self.index_of(button_id)
            for row in (old_row, new_row):
                if row >= 0:
                    model_index = self.index(row, 0)
                    self.dataChanged.emit(model_index, model_index, [self.IsSelectedRole])

        def selected_button_id(self) -> str:
            return self._selected_button_id

    class SettingsController(QObject):
        """QML-facing adapter over settings_ui.py's pure functions plus
        config.py/audio_output.py/bridge_launcher.py/logging_setup.py/
        shell_targets.py - every slot below is a thin wrapper that performs
        no validation or business logic of its own.
        """

        hotkeyTextChanged = Signal()
        triggerModeIndexChanged = Signal()
        endpointOptionsChanged = Signal()
        selectedEndpointIndexChanged = Signal()
        launchStatusTextChanged = Signal()
        statusMessageChanged = Signal()
        errorMessageChanged = Signal()
        selectedButtonIdChanged = Signal()
        selectedDeviceIndexChanged = Signal()
        selectedDeviceChanged = Signal()
        djiMicStatusTextChanged = Signal()
        keyDetectionActiveChanged = Signal()
        keyDetectionTextChanged = Signal()
        _rawKeyDetected = Signal(str, str)
        hotkeyCaptured = Signal(str)
        hotkeyCaptureError = Signal(str)
        _hotkeyCaptureResult = Signal(str)
        bridgeRunningChanged = Signal()
        trayEnabledChanged = Signal()
        trayAvailableChanged = Signal()
        trayStatusTextChanged = Signal()
        startWithWindowsChanged = Signal()
        startWithWindowsStatusTextChanged = Signal()
        startWithWindowsStatusIsErrorChanged = Signal()
        closingBehaviorTextChanged = Signal()

        _TRIGGER_MODE_ORDER = tuple(key_mapping.VoiceTriggerMode)
        _DEVICE_ORDER = tuple(profile.device_id for profile in device_catalog.DEVICE_PROFILES)

        def __init__(self, model: "ButtonMappingModel", parent=None) -> None:
            super().__init__(parent)
            self._model = model
            self._config_root = config.config_root()
            self._config = config.load_config(config.config_path(self._config_root))
            self._bindings = config.load_key_bindings(
                config.key_bindings_path(self._config_root)
            )

            self._hotkey_text = self._config.get(
                "voice_hotkey", hotkey.DEFAULT_VOICE_HOTKEY.serialize()
            )
            saved_trigger_mode = key_mapping.VoiceTriggerMode(
                self._config.get("voice_trigger_mode", "toggle")
            )
            self._trigger_mode_index = self._TRIGGER_MODE_ORDER.index(saved_trigger_mode)

            self._launch_status_text = settings_ui.LAUNCH_NOT_STARTED_TEXT
            self._status_message = ""
            self._error_message = ""
            self._selected_button_id = "ok"
            selected_device_id = device_catalog.normalize_device_id(
                self._config.get("selected_device_profile")
            )
            self._selected_device_fallback_id = selected_device_id
            self._selected_device_index = (
                self._DEVICE_ORDER.index(selected_device_id)
                if selected_device_id in self._DEVICE_ORDER
                else -1
            )
            self._dji_mic_status_text = ""
            self._key_detection_listener = None
            self._key_detection_active = False
            self._key_detection_text = (
                "尚未检测真实按键。点击“检测真实按键”后，再按一次遥控器按键。"
            )
            self._rawKeyDetected.connect(self._on_raw_key_detected)
            self._hotkey_capture = None
            self._hotkeyCaptureResult.connect(self._on_hotkey_capture_result)
            self._bridge_handle: Optional[bridge_launcher.BridgeHandle] = None

            self._tray_enabled: bool = bool(
                self._config.get("tray_enabled", True)
            )
            self._tray_controller: Optional["tray_icon.TrayController"] = None
            self._tray_available: bool = False
            self._tray_status_text: str = ""
            # start-with-Windows state is computed on demand by reading the
            # Startup folder - it cannot be cached, because another process
            # (the user, PowerShell, Windows itself) can drop / remove the
            # shortcut at any time. ``_start_with_windows_status_text``
            # is set the first time ``apply_runtime`` runs (see below)
            # and again every time the user toggles the switch.
            self._start_with_windows: bool = (
                startup_shortcut.is_enabled()
            )
            self._start_with_windows_status_text: str = ""
            self._start_with_windows_status_is_error: bool = False

            self._endpoint_options: List[str] = []
            self._selected_endpoint_index = -1
            self._refresh_endpoint_options()
            self._refresh_dji_mic_status()
            self._load_bindings_into_model()
            self._model.set_selected_button(self._selected_button_id)

        # -- window event filter (close-to-tray) -----------------------------
        def install_window_event_filter(self, window) -> None:
            classes = _load_qt_classes()
            QEvent = classes["QEvent"]
            filter_cls = classes["_WindowEventFilter"]
            self._window_event_filter = filter_cls(
                QEvent.Type.Close, window, self
            )
            window.installEventFilter(self._window_event_filter)

        def apply_runtime(self, window, qapp) -> None:
            """Wire the controller to the live QApplication + window.

            Called once from ``run_settings_window`` after the QML engine
            has loaded the root window. Idempotent: a second call with
            the same window is a no-op.

            Responsibilities:

            * Build the ``TrayController`` (if enabled) and connect its
              ``show_window_requested`` / ``quit_requested`` signals.
            * Install the window event filter so closing the window
              hides it instead of quitting (when ``_tray_enabled``).
            * Refresh the start-with-Windows status text now that we have
              a Qt thread to safely hit the disk.
            """
            if getattr(self, "_runtime_applied", False):
                return
            self._runtime_window = window
            self._runtime_qapp = qapp
            # Order matters: tray controller must exist before the
            # window event filter so the filter's
            # ``_controller._tray_enabled`` check sees the live state.
            self._apply_tray_to_runtime()
            self.install_window_event_filter(window)
            self._refresh_start_with_windows_status()
            self._runtime_applied = True

        def _apply_tray_to_runtime(self) -> None:
            """Show or hide the system tray icon according to
            ``_tray_enabled``. Also refreshes ``_tray_available`` /
            ``_tray_status_text`` so the QML toggle can reflect what
            actually happened on this host.
            """
            # Lazy-build the controller so PyInstaller frozen boot does
            # not hit ``QMenu`` / ``QSystemTrayIcon`` (which require a
            # live QGuiApplication) until ``show()`` is actually called.
            if self._tray_enabled:
                if self._tray_controller is None:
                    self._tray_controller = tray_icon.TrayController(
                        app_name="Remote Mic \u8bbe\u7f6e",
                        is_shortcut_active_provider=startup_shortcut.is_enabled,
                    )
                    # Connect the menu signals to the controller
                    # slots. The lambdas re-read attributes at click
                    # time so they always see the latest values.
                    self._tray_controller.show_window_requested.connect(
                        self._on_tray_show_window
                    )
                    self._tray_controller.quit_requested.connect(
                        self._on_tray_quit
                    )
                    # Sync the "Open Startup folder" menu item's
                    # enabled state with reality on every change.
                    self._tray_controller.set_shortcut_active(
                        self._start_with_windows
                    )
                shown = self._tray_controller.show()
                available = bool(shown)
                self._tray_available = available
                if available:
                    self._tray_status_text = (
                        "\u5df2\u542f\u7528\u7cfb\u7edf\u6258\u76d8\u56fe\u6807\uff0c"
                        "\u5173\u95ed\u8bbe\u7f6e\u7a97\u53e3\u540e\u4ecd\u53ef\u4ece\u6258\u76d8\u6062\u590d\u3002"
                    )
                else:
                    self._tray_status_text = (
                        "\u672c\u673a\u672a\u68c0\u6d4b\u5230\u53ef\u7528\u7684\u7cfb\u7edf\u6258\u76d8\uff0c"
                        "\u8bf7\u68c0\u67e5 Windows \u6258\u76d8\u670d\u52a1\u662f\u5426\u542f\u7528\u3002"
                    )
            else:
                if self._tray_controller is not None:
                    self._tray_controller.hide()
                self._tray_available = False
                self._tray_status_text = ""
            self.trayEnabledChanged.emit()
            self.trayAvailableChanged.emit()
            self.trayStatusTextChanged.emit()

        def _on_tray_show_window(self) -> None:
            window = getattr(self, "_runtime_window", None)
            if window is None:
                return
            try:
                window.show()
                window.raise_()
                window.activateWindow()
            except Exception:
                logger.exception("tray: failed to show settings window")

        def _on_tray_quit(self) -> None:
            qapp = getattr(self, "_runtime_qapp", None)
            if qapp is not None:
                try:
                    qapp.quit()
                    return
                except Exception:
                    logger.exception("tray: qapp.quit failed")
            # Fallback: walk sys.modules for a QCoreApplication instance.
            try:
                classes = _load_qt_classes()
                QCoreApplication = classes["QCoreApplication"]
                QCoreApplication.instance().quit()  # type: ignore[union-attr]
            except Exception:
                logger.exception("tray: fallback quit failed")

        def _on_tray_open_startup_folder(self) -> None:
            try:
                from . import startup_shortcut as _ss
                _ss_module = getattr(_ss, "open_startup_folder", None)
                if _ss_module is not None:
                    _ss_module()
                else:
                    # Fallback: just emit the signal/log; the menu
                    # item is informational anyway.
                    logger.info(
                        "tray: open_startup_folder unavailable"
                    )
            except Exception:  # noqa: BLE001
                logger.exception("tray: open startup folder failed")

        def _on_window_minimised_to_tray(self) -> None:
            if self._tray_controller is not None:
                try:
                    self._tray_controller.set_window_visible(False)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "tray: set_window_visible(False) failed"
                    )

        def _on_window_restored_from_tray(self) -> None:
            if self._tray_controller is not None:
                try:
                    self._tray_controller.set_window_visible(True)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "tray: set_window_visible(True) failed"
                    )

        def _refresh_start_with_windows_status(self) -> None:
            """Re-read ``startup_shortcut.is_enabled`` and update the
            status text + is_error flag. Cheap; safe to call any time."""
            new_state = bool(startup_shortcut.is_enabled())
            changed = new_state != self._start_with_windows
            self._start_with_windows = new_state
            if new_state:
                self._start_with_windows_status_text = (
                    "\u5df2\u52a0\u5165\u5f00\u673a\u542f\u52a8\u3002\u4e0b\u6b21\u767b\u5f55 Windows "
                    "\u4f1a\u81ea\u52a8\u6253\u5f00\u8bbe\u7f6e\u7a97\u53e3\u3002"
                )
                self._start_with_windows_status_is_error = False
            else:
                self._start_with_windows_status_text = (
                    "\u672a\u52a0\u5165\u5f00\u673a\u542f\u52a8\u3002\u6253\u5f00\u540e\u4e0b\u6b21\u767b\u5f55\u4f1a\u81ea\u52a8\u52a0\u8f7d\u3002"
                )
                self._start_with_windows_status_is_error = False
            if changed:
                self.startWithWindowsChanged.emit()
            self.startWithWindowsStatusTextChanged.emit()
            self.startWithWindowsStatusIsErrorChanged.emit()
            # Keep the tray's "Open Startup folder" menu item in sync
            # with reality. No-op if the tray has not been
            # constructed yet (apply_runtime hasn't run).
            if self._tray_controller is not None:
                try:
                    self._tray_controller.set_shortcut_active(new_state)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "tray: set_shortcut_active sync failed"
                    )

        # -- properties (tray + start-with-Windows) ---------------------------

        def _get_tray_enabled(self) -> bool:
            return self._tray_enabled

        def _set_tray_enabled(self, value: bool) -> None:
            target = bool(value)
            if target == self._tray_enabled:
                return
            self._tray_enabled = target
            self._config["tray_enabled"] = target
            self._save_config()
            self._apply_tray_to_runtime()

        def _get_tray_available(self) -> bool:
            return self._tray_available

        def _get_tray_status_text(self) -> str:
            return self._tray_status_text

        def _get_start_with_windows(self) -> bool:
            return self._start_with_windows

        def _set_start_with_windows(self, value: bool) -> None:
            target = bool(value)
            if target == self._start_with_windows and target == bool(
                startup_shortcut.is_enabled()
            ):
                return
            try:
                if target:
                    target_exe = self._resolve_autostart_target()
                    if target_exe is None:
                        self._start_with_windows_status_text = (
                            "\u542f\u52a8\u9879\u5931\u8d25: \u672a\u80fd\u5b9a\u4f4d\u53ef\u6267\u884c\u6587\u4ef6\u3002"
                        )
                        self._start_with_windows_status_is_error = True
                        self.startWithWindowsStatusTextChanged.emit()
                        self.startWithWindowsStatusIsErrorChanged.emit()
                        return
                    result = startup_shortcut.enable(target_exe)
                else:
                    result = startup_shortcut.disable()
            except Exception as exc:  # noqa: BLE001
                self._start_with_windows_status_text = (
                    f"\u542f\u52a8\u9879\u51fa\u9519: {exc}"
                )
                self._start_with_windows_status_is_error = True
                self.startWithWindowsStatusTextChanged.emit()
                self.startWithWindowsStatusIsErrorChanged.emit()
                return
            if result.outcome == startup_shortcut.StartupShortcutOutcome.FAILED:
                self._start_with_windows_status_text = result.message
                self._start_with_windows_status_is_error = True
            else:
                self._start_with_windows_status_text = result.message
                self._start_with_windows_status_is_error = False
            self._refresh_start_with_windows_status()

        @staticmethod
        def _resolve_autostart_target() -> Optional[Path]:
            """Return the executable path that should be placed in the
            Startup folder. ``None`` means we cannot autostart from
            here (e.g. running tests or a misconfigured install).
            """
            # Frozen build: sys.executable is the .exe itself.
            candidate = Path(getattr(sys, "executable", "") or "")
            if candidate and candidate.suffix.lower() == ".exe":
                return candidate.resolve()
            # Source-mode fallback: prefer the installed .exe in dist/
            # over the python interpreter, since the user probably
            # wants to autostart the app, not the dev launcher.
            project_root = Path(__file__).resolve().parent.parent.parent
            for rel in (
                "dist/RemoteMicRC003/RemoteMicRC003.exe",
                "dist-new/RemoteMicRC003/RemoteMicRC003.exe",
                "build/RemoteMicRC003/RemoteMicRC003.exe",
            ):
                probe = project_root / rel
                if probe.exists():
                    return probe.resolve()
            return None

        def _get_start_with_windows_status_text(self) -> str:
            return self._start_with_windows_status_text

        def _get_start_with_windows_status_is_error(self) -> bool:
            return self._start_with_windows_status_is_error

        def _get_closing_behavior_text(self) -> str:
            if self._tray_enabled and self._tray_available:
                return (
                    "\u70b9\u51fb\u5173\u95ed\u6309\u94ae\u540e\u8bbe\u7f6e\u7a97\u53e3\u4f1a"
                    "\u9690\u85cf\u5230\u7cfb\u7edf\u6258\u76d8\u3002"
                    "\u8981\u5b8c\u5168\u9000\u51fa\u8bf7\u53f3\u952e\u6258\u76d8\u56fe\u6807 -> \u9000\u51fa\u3002"
                )
            return (
                "\u70b9\u51fb\u5173\u95ed\u6309\u94ae\u4f1a\u9000\u51fa\u7a0b\u5e8f\u3002"
                "\u542f\u7528\u4e0a\u9762\u7684\u6258\u76d8\u9009\u9879\u540e\uff0c"
                "\u5173\u95ed\u4f1a\u9690\u85cf\u5230\u6258\u76d8\u3002"
            )

        @Slot()
        def setTrayEnabled(self, value: bool) -> None:  # noqa: N802 (QML slot)
            self._set_tray_enabled(value)

        @Slot()
        def setStartWithWindows(self, value: bool) -> None:  # noqa: N802
            self._set_start_with_windows(value)

        @Slot()
        def refreshStartWithWindowsStatus(self) -> None:  # noqa: N802
            """QML can call this on window show to re-read the on-disk
            state in case the shortcut was removed out-of-band."""
            self._refresh_start_with_windows_status()

        def _save_config(self) -> None:
            config.save_config(config.config_path(self._config_root), self._config)

        trayEnabled = Property(
            bool, _get_tray_enabled, _set_tray_enabled, notify=trayEnabledChanged
        )
        trayAvailable = Property(bool, _get_tray_available, notify=trayAvailableChanged)
        trayStatusText = Property(
            str, _get_tray_status_text, notify=trayStatusTextChanged
        )
        startWithWindows = Property(
            bool,
            _get_start_with_windows,
            _set_start_with_windows,
            notify=startWithWindowsChanged,
        )
        startWithWindowsStatusText = Property(
            str,
            _get_start_with_windows_status_text,
            notify=startWithWindowsStatusTextChanged,
        )
        startWithWindowsStatusIsError = Property(
            bool,
            _get_start_with_windows_status_is_error,
            notify=startWithWindowsStatusIsErrorChanged,
        )
        closingBehaviorText = Property(
            str, _get_closing_behavior_text, notify=closingBehaviorTextChanged
        )

        # -- internal helpers -------------------------------------------------

        def _refresh_endpoint_options(self) -> None:
            try:
                endpoints = audio_output.enumerate_output_endpoints()
                options = [settings_ui._endpoint_display(e) for e in endpoints]
            except audio_output.AudioOutputUnavailableError:
                options = []

            saved_name = self._config.get("output_endpoint_name", "")
            saved_display = ""
            if saved_name:
                saved_display = settings_ui._endpoint_display(
                    audio_output.AudioEndpoint(
                        name=saved_name,
                        host_api=self._config.get("output_endpoint_host_api", ""),
                    )
                )
            if saved_display and saved_display not in options:
                # The previously-saved device is no longer enumerated (e.g.
                # unplugged) - still show it as a selectable-but-absent
                # option rather than silently discarding the user's saved
                # choice, matching build_save_model()/_parse_endpoint_display()
                # round-tripping whatever text is present at save time.
                options = [saved_display] + options

            self._endpoint_options = options
            self._selected_endpoint_index = (
                options.index(saved_display) if saved_display in options else -1
            )

        def _load_bindings_into_model(self) -> None:
            bindings = self._bindings.get("bindings", {})
            display_map: Dict[str, str] = {}
            secondary_display_map: Dict[str, Dict[str, str]] = {}
            for button_id in remote_layout.BUTTON_ORDER:
                action_dict = bindings.get(button_id)
                if action_dict is not None:
                    try:
                        action = key_mapping.ButtonAction.from_dict(action_dict)
                        display_map[button_id] = settings_ui._action_to_display(action)
                    except (KeyError, TypeError, ValueError):
                        display_map[button_id] = ""
                else:
                    display_map[button_id] = ""
                if button_id != "mic":
                    secondary_display_map[button_id] = {}
                    raw_secondary = self._bindings.get("secondary_bindings", {}).get(
                        button_id, {}
                    )
                    if isinstance(raw_secondary, dict):
                        for trigger_name in (
                            key_mapping.ButtonTrigger.DOUBLE_CLICK.value,
                            key_mapping.ButtonTrigger.LONG_PRESS.value,
                        ):
                            action_dict = raw_secondary.get(trigger_name)
                            if not isinstance(action_dict, dict):
                                continue
                            try:
                                action = key_mapping.ButtonAction.from_dict(action_dict)
                            except (KeyError, TypeError, ValueError):
                                continue
                            secondary_display_map[button_id][trigger_name] = (
                                settings_ui._action_to_display(action)
                            )
            self._model.load_display_map(display_map, secondary_display_map)

        def _selected_device_id(self) -> str:
            if 0 <= self._selected_device_index < len(self._DEVICE_ORDER):
                return self._DEVICE_ORDER[self._selected_device_index]
            return self._selected_device_fallback_id

        def _refresh_dji_mic_status(self) -> None:
            try:
                endpoints = audio_output.enumerate_input_endpoints()
            except audio_output.AudioOutputUnavailableError:
                endpoints = []
            self._dji_mic_status_text = device_catalog.dji_mic_2_input_status(endpoints)
            self.djiMicStatusTextChanged.emit()

        def _set_launch_status(self, text: str) -> None:
            self._launch_status_text = text
            self.launchStatusTextChanged.emit()

        def _set_status_message(self, text: str) -> None:
            self._status_message = text
            self.statusMessageChanged.emit()

        def _set_error_message(self, text: str) -> None:
            self._error_message = text
            self.errorMessageChanged.emit()

        def _set_key_detection_text(self, text: str) -> None:
            if text != self._key_detection_text:
                self._key_detection_text = text
                self.keyDetectionTextChanged.emit()

        def _on_raw_input_event(self, event: raw_input_windows.RawInputEvent) -> None:
            if not event.is_pressed:
                return
            signature = raw_input_windows.physical_signature(event)
            if event.source == "keyboard":
                vkey = "--" if event.vkey is None else f"0x{event.vkey:02X}"
                make_code = "--" if event.make_code is None else f"0x{event.make_code:02X}"
                flags = "--" if event.flags is None else f"0x{event.flags:04X}"
                details = (
                    f"Raw Input 键盘事件：VKey={vkey}, "
                    f"MakeCode={make_code}, Flags={flags}"
                )
            else:
                details = f"Raw Input HID 报告：{event.report.hex(' ')}"
            if event.usages:
                details += " Usages=" + ",".join(
                    f"0x{usage:04X}" for usage in event.usages
                )
            if event.decode_error:
                details += f" 解码错误={event.decode_error}"
            details += f" Signature={signature}"
            self._rawKeyDetected.emit(event.button_id or "", details)

        def _on_raw_key_detected(self, button_id: str, details: str) -> None:
            """Handle one physical press on the Qt GUI thread.

            Raw Input emits only a logical button id; this detector never
            executes the configured action. It stops after the first press,
            selects the corresponding row, and leaves the user in control of
            choosing/saving the Windows mapping.
            """

            self.stopKeyDetection()
            if button_id:
                self.selectButton(button_id)
                display_name = remote_layout.BUTTON_DISPLAY_NAMES.get(button_id, button_id)
                usage = remote_layout.hid_usage_display(button_id)
                result = f"已捕获真实按键：{display_name}（{usage}）。"
            else:
                result = (
                    "已捕获未预置映射的真实按键；请保留 Signature，"
                    "再用 rc003_key_test capture --assign 适配它。"
                )
            self._set_key_detection_text(
                f"{result}{details} 现在可设置该行的 Windows 映射并保存。"
            )

        def _on_hotkey_capture_result(self, chord: str) -> None:
            """Forward a hook-thread result to QML on the GUI thread."""

            inferred_mode = key_mapping.voice_trigger_mode_for_hotkey(chord)
            if inferred_mode is not None:
                self._set_trigger_mode_preserving_hotkey(inferred_mode)
            self.hotkeyCaptured.emit(chord)

        def _set_trigger_mode_preserving_hotkey(
            self, trigger_mode: key_mapping.VoiceTriggerMode
        ) -> None:
            """Change the mode preset without overwriting a just-recorded chord."""

            index = self._TRIGGER_MODE_ORDER.index(trigger_mode)
            if index != self._trigger_mode_index:
                self._trigger_mode_index = index
                self.triggerModeIndexChanged.emit()

        def _save(self) -> bool:
            """Same validation as before (settings_ui.build_save_model);
            returns True only on an actual successful save, so
            saveAndLaunch() can gate the launch on it exactly like the
            previous Tk _save_and_launch() did.
            """

            trigger_mode = self._TRIGGER_MODE_ORDER[self._trigger_mode_index]
            endpoint_display = (
                self._endpoint_options[self._selected_endpoint_index]
                if 0 <= self._selected_endpoint_index < len(self._endpoint_options)
                else ""
            )
            try:
                new_config, new_bindings = settings_ui.build_save_model(
                    button_display_map=self._model.to_display_map(),
                    secondary_display_map=self._model.to_secondary_display_map(),
                    hotkey_text=self._hotkey_text,
                    trigger_mode=trigger_mode,
                    endpoint_display_text=endpoint_display,
                    base_config=self._config,
                    base_bindings=self._bindings,
                    selected_device_profile=self._selected_device_id(),
                )
            except settings_ui.SettingsValidationError as exc:
                title = f"「{exc.button_id}」映射无效" if exc.button_id else "语音热键无效"
                self._set_error_message(f"{title}：{exc.message}")
                return False

            config_path = config.config_path(self._config_root)
            bindings_path = config.key_bindings_path(self._config_root)
            try:
                config.save_config(config_path, new_config)
                config.save_key_bindings(bindings_path, new_bindings)
                # Read the files back through the same normalizers the bridge
                # uses. This prevents the UI from claiming success when the
                # file was not actually writable or the persisted shape was
                # not usable by the runtime.
                saved_config = config.load_config(config_path)
                saved_bindings = config.load_key_bindings(bindings_path)
            except Exception as exc:  # noqa: BLE001 - a Qt slot must not escape
                self._set_error_message(f"保存失败：{exc}")
                return False

            self._config = saved_config
            self._bindings = saved_bindings
            self._load_bindings_into_model()
            self._set_error_message("")
            if self._selected_device_id() == device_catalog.DJI_MIC_2_ID:
                self._set_status_message(
                    "已保存 DJI Mic 2 设备选择。它使用 Windows 系统录音输入，不需要启动 RC003 桥。"
                )
            else:
                self._set_status_message("已保存。重启桥接以应用新的连接/输出设置。")
            return True

        # -- properties ---------------------------------------------------

        def _get_hotkey_text(self) -> str:
            return self._hotkey_text

        def _set_hotkey_text(self, value: str) -> None:
            if value != self._hotkey_text:
                self._hotkey_text = value
                self.hotkeyTextChanged.emit()

        hotkeyText = Property(str, _get_hotkey_text, _set_hotkey_text, notify=hotkeyTextChanged)

        def _get_trigger_mode_options(self) -> List[str]:
            return [settings_ui._TRIGGER_MODE_LABELS[mode] for mode in self._TRIGGER_MODE_ORDER]

        triggerModeOptions = Property(list, _get_trigger_mode_options, constant=True)

        def _get_trigger_mode_index(self) -> int:
            return self._trigger_mode_index

        def _set_trigger_mode_index(self, value: int) -> None:
            if value != self._trigger_mode_index and 0 <= value < len(self._TRIGGER_MODE_ORDER):
                self._trigger_mode_index = value
                trigger_mode = self._TRIGGER_MODE_ORDER[value]
                self._set_hotkey_text(settings_ui.voice_hotkey_for_trigger_mode(trigger_mode))
                self.triggerModeIndexChanged.emit()

        triggerModeIndex = Property(
            int, _get_trigger_mode_index, _set_trigger_mode_index, notify=triggerModeIndexChanged
        )

        def _get_endpoint_options(self) -> List[str]:
            return list(self._endpoint_options)

        endpointOptions = Property(
            list, _get_endpoint_options, notify=endpointOptionsChanged
        )

        def _get_selected_endpoint_index(self) -> int:
            return self._selected_endpoint_index

        def _set_selected_endpoint_index(self, value: int) -> None:
            if value != self._selected_endpoint_index:
                self._selected_endpoint_index = value
                self.selectedEndpointIndexChanged.emit()

        selectedEndpointIndex = Property(
            int,
            _get_selected_endpoint_index,
            _set_selected_endpoint_index,
            notify=selectedEndpointIndexChanged,
        )

        def _get_launch_status_text(self) -> str:
            return self._launch_status_text

        launchStatusText = Property(str, _get_launch_status_text, notify=launchStatusTextChanged)

        def _get_bridge_running(self) -> bool:
            handle = self._bridge_handle
            if handle is None:
                return False
            if not handle.process.is_alive():
                self._bridge_handle = None
                clear_bridge_handle()
                return False
            return True

        bridgeRunning = Property(bool, _get_bridge_running, notify=bridgeRunningChanged)

        def _get_status_message(self) -> str:
            return self._status_message

        statusMessage = Property(str, _get_status_message, notify=statusMessageChanged)

        def _get_error_message(self) -> str:
            return self._error_message

        errorMessage = Property(str, _get_error_message, notify=errorMessageChanged)

        def _get_selected_button_id(self) -> str:
            return self._selected_button_id

        selectedButtonId = Property(str, _get_selected_button_id, notify=selectedButtonIdChanged)

        def _get_device_options(self) -> List[str]:
            return [profile.display_name for profile in device_catalog.DEVICE_PROFILES]

        deviceOptions = Property(list, _get_device_options, constant=True)

        def _get_device_catalog_available(self) -> bool:
            return device_catalog.CATALOG_ERROR is None

        deviceCatalogAvailable = Property(
            bool, _get_device_catalog_available, constant=True
        )

        def _get_device_catalog_error_text(self) -> str:
            if device_catalog.CATALOG_ERROR is None:
                return ""
            return "设备目录不可用：" + device_catalog.CATALOG_ERROR

        deviceCatalogErrorText = Property(
            str, _get_device_catalog_error_text, constant=True
        )

        def _get_selected_device_index(self) -> int:
            return self._selected_device_index

        def _set_selected_device_index(self, value: int) -> None:
            if value == self._selected_device_index or not (0 <= value < len(self._DEVICE_ORDER)):
                return
            self._selected_device_index = value
            self._selected_device_fallback_id = self._DEVICE_ORDER[value]
            self.selectedDeviceIndexChanged.emit()
            self.selectedDeviceChanged.emit()
            if self._selected_device_id() == device_catalog.DJI_MIC_2_ID:
                self._refresh_dji_mic_status()

        selectedDeviceIndex = Property(
            int,
            _get_selected_device_index,
            _set_selected_device_index,
            notify=selectedDeviceIndexChanged,
        )

        def _get_is_rc003_device(self) -> bool:
            return self._selected_device_id() == device_catalog.RC003_ID

        isRc003Device = Property(bool, _get_is_rc003_device, notify=selectedDeviceChanged)

        def _get_is_dji_mic_2_device(self) -> bool:
            return self._selected_device_id() == device_catalog.DJI_MIC_2_ID

        isDjiMic2Device = Property(bool, _get_is_dji_mic_2_device, notify=selectedDeviceChanged)

        def _get_selected_device_description(self) -> str:
            if device_catalog.CATALOG_ERROR is not None:
                return self._get_device_catalog_error_text()
            return device_catalog.profile_for(self._selected_device_id()).description

        selectedDeviceDescription = Property(
            str, _get_selected_device_description, notify=selectedDeviceChanged
        )

        def _get_mapping_page_title(self) -> str:
            return "按键映射" if self._get_is_rc003_device() else "设备控制"

        mappingPageTitle = Property(str, _get_mapping_page_title, notify=selectedDeviceChanged)

        def _get_dji_mic_status_text(self) -> str:
            return self._dji_mic_status_text

        djiMicStatusText = Property(
            str, _get_dji_mic_status_text, notify=djiMicStatusTextChanged
        )

        def _get_key_detection_active(self) -> bool:
            return self._key_detection_active

        keyDetectionActive = Property(
            bool,
            _get_key_detection_active,
            notify=keyDetectionActiveChanged,
        )

        def _get_key_detection_text(self) -> str:
            return self._key_detection_text

        keyDetectionText = Property(
            str,
            _get_key_detection_text,
            notify=keyDetectionTextChanged,
        )

        def _get_dji_control_rows(self) -> List[dict]:
            return [
                {
                    "name": control.display_name,
                    "behavior": control.hardware_behavior,
                    "mapping": control.windows_mapping,
                }
                for control in device_catalog.DJI_MIC_2_CONTROLS
            ]

        djiControlRows = Property(list, _get_dji_control_rows, constant=True)

        def _get_preset_action_options(self) -> List[str]:
            return list(_PRESET_ACTION_OPTIONS)

        presetActionOptions = Property(list, _get_preset_action_options, constant=True)

        def _get_mic_row_text(self) -> str:
            return settings_ui._MIC_ROW_DISPLAY

        micRowText = Property(str, _get_mic_row_text, constant=True)

        def _get_photo_source(self) -> str:
            photo_path = resources.find_remote_photo()
            if photo_path is None:
                return ""
            return QUrl.fromLocalFile(str(photo_path)).toString()

        photoSource = Property(str, _get_photo_source, constant=True)

        def _get_photo_available(self) -> bool:
            return resources.find_remote_photo() is not None

        photoAvailable = Property(bool, _get_photo_available, constant=True)

        # -- slots ----------------------------------------------------------

        @Slot(result=bool)
        def saveSettings(self) -> bool:
            return self._save()

        @Slot()
        def startKeyDetection(self) -> None:
            """Listen for one real RC003 press without executing its action."""

            if self._key_detection_active:
                return
            if self._selected_device_id() != device_catalog.RC003_ID:
                self._set_key_detection_text("当前设备不是 RC003，无法检测遥控器按键。")
                return
            try:
                paths = raw_input_windows.enumerate_matching_device_paths()
                device_path = raw_input_windows.hid_identity.select_single_device_path(paths)
                listener = raw_input_windows.RawInputButtonListener(
                    lambda *_: None,
                    self._on_raw_input_event,
                )
                set_physical_bindings = getattr(
                    listener, "set_physical_bindings", None
                )
                if callable(set_physical_bindings):
                    set_physical_bindings(self._bindings.get("physical_bindings", {}))
                listener.start(device_path)
            except Exception as exc:  # noqa: BLE001 - surface failure in the UI
                self._key_detection_listener = None
                self._key_detection_active = False
                self.keyDetectionActiveChanged.emit()
                self._set_key_detection_text(f"无法启动真实按键检测：{exc}")
                return

            self._key_detection_listener = listener
            self._key_detection_active = True
            self.keyDetectionActiveChanged.emit()
            self._set_key_detection_text(
                "正在监听 RC003。请现在按一次遥控器按键；不会执行该键的映射动作。"
            )

        @Slot()
        def startHotkeyCapture(self) -> None:
            """Start the EXE-owned physical keyboard shortcut recorder."""

            if self._hotkey_capture is not None:
                return
            capture = hotkey_capture_windows.HotkeyCapture(
                lambda chord: self._hotkeyCaptureResult.emit(chord)
            )
            self._hotkey_capture = capture
            try:
                capture.start()
            except Exception as exc:  # noqa: BLE001 - surface in the dialog
                self._hotkey_capture = None
                self.hotkeyCaptureError.emit(f"无法启动真实键盘录制：{exc}")
                return

        @Slot()
        def stopHotkeyCapture(self) -> None:
            """Stop the physical recorder, including Cancel/window close."""

            capture = self._hotkey_capture
            self._hotkey_capture = None
            if capture is None:
                return
            try:
                capture.stop()
            except Exception as exc:  # noqa: BLE001 - never crash the settings UI
                self.hotkeyCaptureError.emit(f"停止真实键盘录制时出错：{exc}")

        @Slot()
        def stopKeyDetection(self) -> None:
            listener = self._key_detection_listener
            self._key_detection_listener = None
            if listener is not None:
                try:
                    listener.stop()
                except Exception as exc:  # noqa: BLE001 - report, do not crash Qt
                    self._set_key_detection_text(f"停止真实按键检测时出错：{exc}")
            if self._key_detection_active:
                self._key_detection_active = False
                self.keyDetectionActiveChanged.emit()

        @Slot()
        def saveAndLaunch(self) -> None:
            """Saves first (using the exact same validation as
            "保存并应用"), and only launches the bridge if that save
            actually succeeded - a rejected mapping/hotkey must never be
            silently followed by starting the bridge with stale config
            anyway (unchanged XRBM-029 contract, now driven from QML).
            """

            if not self._save():
                return
            if self._selected_device_id() == device_catalog.DJI_MIC_2_ID:
                self._set_launch_status(
                    "DJI Mic 2 使用 Windows 系统录音输入，不启动 RC003 BLE/HID/ATVV 桥。"
                )
                return
            self._set_launch_status("正在启动…")
            result = bridge_launcher.launch_bridge()
            if result.handle is not None:
                self._bridge_handle = result.handle
                register_bridge_handle(result.handle)
            self._set_launch_status(settings_ui.describe_launch_result(result))
            self.bridgeRunningChanged.emit()

        @Slot()
        def stopBridge(self) -> None:
            """Stop the bridge subprocess that ``saveAndLaunch`` started.

            Settings window stays open - this is the Stop Bridge button,
            not a close-window handler. ``run_settings_window()``'s own
            ``finally`` clause and ``_shutdown_qt_settings_app_at_exit``
            separately guarantee the bridge is stopped on window close;
            this slot is the manual lever.
            """

            handle = self._bridge_handle
            self._bridge_handle = None
            clear_bridge_handle()
            result = bridge_launcher.stop_bridge(handle)
            self._set_launch_status(settings_ui.describe_stop_result(result))
            self.bridgeRunningChanged.emit()

        @Slot()
        def restoreDefaults(self) -> None:
            """Resets every widget's DISPLAYED value to the defaults - never
            writes to config.json/key_bindings.json itself (XRBM-030 RETRY 1
            blocker 4: this must never look/claim to be persisted). The
            status message says so explicitly, so a user who restores
            defaults and then simply closes the window without saving is
            not misled into thinking anything was written to disk.
            """

            defaults = settings_ui.default_display_state()
            self._model.load_display_map(
                defaults.button_display_map,
                defaults.secondary_display_map,
            )
            self._set_hotkey_text(defaults.hotkey_text)
            trigger_mode = next(
                mode
                for mode in self._TRIGGER_MODE_ORDER
                if settings_ui._TRIGGER_MODE_LABELS[mode] == defaults.trigger_mode_label
            )
            self._set_trigger_mode_index(self._TRIGGER_MODE_ORDER.index(trigger_mode))
            self._set_error_message("")
            self._set_status_message(
                "已恢复默认显示，尚未保存——点击「保存映射」或「保存并应用」才会写入设置。"
            )

        @Slot()
        def openLogLocation(self) -> None:
            result = logging_setup.open_log_location()
            self._set_status_message(settings_ui.describe_log_open_result(result))

        @Slot(str)
        def selectButton(self, button_id: str) -> None:
            if button_id == self._selected_button_id:
                return
            self._model.set_selected_button(button_id)
            self._selected_button_id = self._model.selected_button_id()
            self.selectedButtonIdChanged.emit()

        def _report_external_target(self, result) -> None:
            if result.outcome is shell_targets.ExternalTargetOutcome.OPENED:
                self._set_status_message(f"已打开：{result.target}")
            else:
                self._set_status_message(f"无法打开 {result.target}（{result.error}）")

        @Slot()
        def openBluetoothSettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(shell_targets.BLUETOOTH_SETTINGS_URI)
            )

        @Slot()
        def openMicrophonePrivacySettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(
                    shell_targets.MICROPHONE_PRIVACY_SETTINGS_URI
                )
            )

        @Slot()
        def openSpeechSettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(shell_targets.SPEECH_SETTINGS_URI)
            )

        @Slot()
        def openSoundSettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(shell_targets.SOUND_SETTINGS_URI)
            )

        @Slot()
        def refreshDjiMicStatus(self) -> None:
            self._refresh_dji_mic_status()

        @Slot()
        def openAppsSettings(self) -> None:
            self._report_external_target(
                shell_targets.open_external_target(shell_targets.APPS_SETTINGS_URI)
            )

        @Slot(str, str, result=bool)
        def selectAndPersistOutputEndpoint(self, name: str, host_api: str) -> bool:
            """Persists a SPECIFIC (name, host_api) pair directly - used by
            the "检查与修复" page's "选择检测到的 CABLE Input" action (XRBM-031
            In-scope item 5), which already knows the exact endpoint from its
            own enumeration rather than a combo-box display string. Bypasses
            build_save_model()'s hotkey/mapping validation entirely (there is
            nothing to validate about an endpoint name/host-API pair coming
            from a real enumeration) but still goes through the same
            config.save_config() persistence and refreshes this
            controller's own endpoint options/selection, so the "连接" page's
            dropdown reflects the change immediately without needing a
            restart. Only ever called after an explicit user click (see
            DiagnosticsController.selectDetectedCableInputAsOutput()) - never
            automatically.

            Returns ``False`` (never raises) if persistence itself fails
            (XRBM-031 RETRY 1 item 3) - e.g. a disk-full/permission error
            from ``config.save_config()`` - so this Slot can never let an
            uncaught exception escape into Qt's C++ call boundary, and so a
            caller can never mistake a failed save for a successful one.
            The in-memory config is only mutated AFTER a successful write,
            so a failed attempt leaves the previously-saved state intact
            rather than looking saved when it is not.
            """

            new_config = dict(self._config)
            new_config["output_endpoint_name"] = name
            new_config["output_endpoint_host_api"] = host_api
            try:
                config.save_config(config.config_path(self._config_root), new_config)
            except Exception:  # noqa: BLE001 - never let a persistence failure escape this Slot
                return False

            self._config = new_config
            self._refresh_endpoint_options()
            self.endpointOptionsChanged.emit()
            self.selectedEndpointIndexChanged.emit()
            return True

    def _diagnostics_check_to_row(check: "windows_diagnostics.CheckResult") -> dict:
        return {
            "checkId": check.check_id,
            "title": check.title,
            "group": check.group.value,
            "status": check.status.value,
            "detail": check.detail,
        }

    class DiagnosticsController(QObject):
        """QML-facing adapter for the "检查与修复" page (XRBM-031). Every
        check runs off the Qt GUI thread (see module docstring); every
        driver-launch/endpoint-select action is a thin wrapper with no
        business logic of its own, matching SettingsController's contract.
        """

        checkResultsChanged = Signal()
        isRefreshingChanged = Signal()
        diagnosticsErrorMessageChanged = Signal()
        driverStatusMessageChanged = Signal()
        driverInfoMessageChanged = Signal()
        driverErrorMessageChanged = Signal()
        # Internal only - never connected to from QML. Carries a
        # windows_diagnostics.DiagnosticsReport (or None on an unexpected
        # worker-thread exception) back from the background thread to this
        # object's own (GUI) thread - see module docstring for why a plain
        # Signal(object) connection is sufficient here.
        _diagnosticsReady = Signal(object)

        def __init__(self, settings_controller: "SettingsController", config_root, parent=None) -> None:
            super().__init__(parent)
            self._settings_controller = settings_controller
            self._config_root = config_root
            self._is_refreshing = False
            self._check_rows: List[dict] = []
            self._diagnostics_error_message = ""
            self._driver_status_message = ""
            self._driver_info_message = ""
            self._driver_error_message = ""
            self._diagnosticsReady.connect(self._on_diagnostics_ready)
            self.refreshDiagnostics()

        # -- internal helpers -------------------------------------------------

        def _saved_output_endpoint(self):
            saved_config = config.load_config(config.config_path(self._config_root))
            return (
                saved_config.get("output_endpoint_name", ""),
                saved_config.get("output_endpoint_host_api", ""),
            )

        def _set_driver_status(self, text: str) -> None:
            """Genuine, completed success only (e.g. an endpoint was
            actually selected and persisted) - the only message shown in
            the success/green color. See ``_set_driver_info`` for outcomes
            that are merely informational (XRBM-031 RETRY 1 item 7).
            """

            self._driver_status_message = text
            self._driver_info_message = ""
            self._driver_error_message = ""
            self.driverStatusMessageChanged.emit()
            self.driverInfoMessageChanged.emit()
            self.driverErrorMessageChanged.emit()

        def _set_driver_info(self, text: str) -> None:
            """Neutral, informational outcome - never the success/green
            color: a UAC cancellation installed nothing, and a launched
            vendor setup UI is not yet a confirmed install either (XRBM-031
            RETRY 1 item 7 - "still never say installed until endpoint
            recheck"). QML renders this in a neutral tone, distinct from
            both a real success and a real error.
            """

            self._driver_info_message = text
            self._driver_status_message = ""
            self._driver_error_message = ""
            self.driverInfoMessageChanged.emit()
            self.driverStatusMessageChanged.emit()
            self.driverErrorMessageChanged.emit()

        def _set_driver_error(self, text: str) -> None:
            self._driver_error_message = text
            self._driver_status_message = ""
            self._driver_info_message = ""
            self.driverErrorMessageChanged.emit()
            self.driverStatusMessageChanged.emit()
            self.driverInfoMessageChanged.emit()

        def _on_diagnostics_ready(self, report) -> None:
            """Delivered (cross-thread) once ``run_diagnostics()`` returns -
            or, if the background thread's own call raised something
            ``windows_diagnostics.run_diagnostics()``'s own per-check
            isolation did not anticipate (should be exceedingly rare now
            that every individual check is isolated - see
            ``windows_diagnostics._isolated()`` - but still possible, e.g.
            if constructing the report itself somehow failed), ``None``.

            XRBM-031 RETRY 1 item 2: a ``None`` report must never leave the
            PREVIOUS run's rows on screen looking current - stale green/red
            rows next to a page that silently failed to refresh would be
            actively misleading. The rows are cleared and a page-level
            error is shown instead, prominently, not only as a driver-card
            message below the fold.
            """

            if report is not None:
                self._check_rows = [_diagnostics_check_to_row(c) for c in report.checks]
                self._diagnostics_error_message = ""
            else:
                self._check_rows = []
                self._diagnostics_error_message = (
                    "检测过程出现意外错误，未能得到任何结果；请点击「重新检测」重试。"
                )
            self._is_refreshing = False
            self.checkResultsChanged.emit()
            self.diagnosticsErrorMessageChanged.emit()
            self.isRefreshingChanged.emit()

        # -- properties ---------------------------------------------------

        def _get_check_results(self) -> List[dict]:
            return list(self._check_rows)

        checkResults = Property(list, _get_check_results, notify=checkResultsChanged)

        def _get_is_refreshing(self) -> bool:
            return self._is_refreshing

        isRefreshing = Property(bool, _get_is_refreshing, notify=isRefreshingChanged)

        def _get_diagnostics_error_message(self) -> str:
            return self._diagnostics_error_message

        diagnosticsErrorMessage = Property(
            str, _get_diagnostics_error_message, notify=diagnosticsErrorMessageChanged
        )

        def _get_driver_status_message(self) -> str:
            return self._driver_status_message

        driverStatusMessage = Property(
            str, _get_driver_status_message, notify=driverStatusMessageChanged
        )

        def _get_driver_info_message(self) -> str:
            return self._driver_info_message

        driverInfoMessage = Property(
            str, _get_driver_info_message, notify=driverInfoMessageChanged
        )

        def _get_driver_error_message(self) -> str:
            return self._driver_error_message

        driverErrorMessage = Property(
            str, _get_driver_error_message, notify=driverErrorMessageChanged
        )

        # -- slots ----------------------------------------------------------

        def _emit_diagnostics_ready(self, report) -> None:
            """Thin wrapper around emitting ``_diagnosticsReady``, isolated
            into its own method purely so a test can inject a failure here
            (simulating a receiver/Qt runtime that is already mid-teardown)
            without needing to monkeypatch PySide6's own ``Signal``
            machinery directly. Never called if
            ``_diagnostics_shutdown_event`` is already set when the worker
            checks it - see ``refreshDiagnostics()``'s ``_run_in_background``
            below.
            """

            self._diagnosticsReady.emit(report)

        @Slot()
        def refreshDiagnostics(self) -> None:
            """Runs every check on a background thread. Repeated clicks
            while a check is already running are ignored outright (the
            guard below), so this can never start two overlapping workers -
            see the module docstring for the full thread-safety/lifecycle
            contract.

            Also refuses to start once process shutdown has begun
            (``_diagnostics_shutdown_event`` set - XRBM-031 RETRY 2): there
            would be nothing left alive to usefully receive this worker's
            result anyway, and starting one so late would only add another
            thread ``_shutdown_qt_settings_app_at_exit()``'s bounded join
            might not have time for.
            """

            if self._is_refreshing:
                return
            if _diagnostics_shutdown_event.is_set():
                return
            self._is_refreshing = True
            self.isRefreshingChanged.emit()
            saved_name, saved_host_api = self._saved_output_endpoint()

            def _run_in_background() -> None:
                try:
                    try:
                        report = windows_diagnostics.run_diagnostics(
                            saved_output_name=saved_name,
                            saved_output_host_api=saved_host_api,
                            # XRBM-035: the SAME event this module's
                            # shutdown helpers set - a discovery attempt
                            # still in flight when the settings window
                            # starts closing is now actually cancelled at
                            # the WinRT level (see
                            # windows_diagnostics._discover_candidates_
                            # cancellable()), not merely abandoned to run
                            # concurrently with interpreter shutdown.
                            cancel_event=_diagnostics_shutdown_event,
                        )
                    except Exception:  # noqa: BLE001 - never crash the worker thread
                        report = None
                    if _diagnostics_shutdown_event.is_set():
                        # Process shutdown began while this check was
                        # running (XRBM-031 RETRY 2) - the
                        # DiagnosticsController/Qt runtime this would emit
                        # into may already be mid-teardown by now. Skip the
                        # emit entirely rather than race it.
                        return
                    try:
                        self._emit_diagnostics_ready(report)
                    except Exception:  # noqa: BLE001 - a receiver/Qt runtime
                        # that is ALREADY tearing down despite the check
                        # just above (an inherent, disclosed narrowing-not-
                        # elimination of the race - see module docstring)
                        # must never crash this background thread; the
                        # registry cleanup below still runs regardless.
                        pass
                finally:
                    _forget_diagnostics_thread(threading.current_thread())

            thread = threading.Thread(target=_run_in_background, daemon=True)
            _remember_diagnostics_thread(thread)
            thread.start()

        @Slot(result=bool)
        def selectDetectedCableInputAsOutput(self) -> bool:
            """Re-enumerates playback endpoints (never trusts a possibly-
            stale prior diagnostics snapshot) and persists the unique
            detected CABLE Input endpoint as this app's voice output - only
            ever called from an explicit button click (XRBM-031 In-scope
            item 5), and only after this method itself confirms exactly one
            such endpoint currently exists.

            Never raises out of this Slot (XRBM-031 RETRY 1 item 3): both
            enumeration and persistence failures are caught and reported as
            an honest ``driverErrorMessage``, with no local path/device
            identifier in the text, and ``False`` is returned - a failed
            save is never reported as if it succeeded.
            """

            try:
                endpoints = audio_output.enumerate_output_endpoints()
            except audio_output.AudioOutputUnavailableError as exc:
                self._set_driver_error(f"无法枚举播放端点：{exc}")
                return False
            except Exception:  # noqa: BLE001 - never let an unexpected enumeration failure escape this Slot
                self._set_driver_error("枚举播放端点时出现意外错误。")
                return False

            matches = [e for e in endpoints if audio_output.is_cable_input_endpoint(e.name)]
            if not matches:
                self._set_driver_error(
                    "未检测到 CABLE Input 端点；请先确认 VB-CABLE 已安装，安装后需要重启电脑。"
                )
                return False
            if len(matches) > 1:
                self._set_driver_error(
                    f"检测到 {len(matches)} 个 CABLE Input 端点，无法唯一确定，请手动在"
                    "「连接」页选择。"
                )
                return False

            endpoint = matches[0]
            try:
                persisted = self._settings_controller.selectAndPersistOutputEndpoint(
                    endpoint.name, endpoint.host_api
                )
            except Exception:  # noqa: BLE001 - defense in depth: selectAndPersistOutputEndpoint
                # already catches its own persistence failures and returns
                # False rather than raising, but this Slot must still never
                # propagate an uncaught exception regardless.
                persisted = False

            if not persisted:
                self._set_driver_error(
                    "保存语音输出设置失败，请重试，或稍后在「连接」页手动选择该端点。"
                )
                return False

            self._set_driver_status(f"已选择 {endpoint.name} 作为语音输出设备并保存。")
            return True

        @Slot()
        def launchVbCableSetup(self) -> None:
            """Launches the bundled VB-CABLE vendor setup UI with UAC. Only
            ever reached from a slot the QML page calls after its OWN
            explicit confirmation dialog (see DiagnosticsPage.qml) - never
            automatically on page load/refresh. Never reports installation
            as successful merely because the process launched; see
            vb_cable_bundle.py's module docstring for the full contract.
            """

            try:
                vb_cable_bundle.prepare_and_launch_vendor_setup()
            except vb_cable_bundle.BundleNotFoundError as exc:
                self._set_driver_error(f"未找到随包的 VB-CABLE 安装包：{exc}")
            except vb_cable_bundle.UacCancelledError as exc:
                # Neutral/informational, never the success color (XRBM-031
                # RETRY 1 item 7): nothing was installed.
                self._set_driver_info(str(exc))
            except vb_cable_bundle.VbCableBundleError as exc:
                self._set_driver_error(f"启动 VB-CABLE 安装程序失败：{exc}")
            else:
                # Also neutral/informational, not the success color: only
                # that the vendor UI was launched, never a confirmed
                # install - that is only ever established later, by a
                # diagnostics recheck finding both endpoints present.
                self._set_driver_info(
                    "已启动 VB-CABLE 官方安装程序（会请求管理员权限）。请按提示完成安装并"
                    "重启电脑，然后点击「重新检测」确认两个虚拟音频端点已出现。"
                )

    class _WindowEventFilter(QObject):
        """Intercepts QCloseEvent on the settings window to route it to
        the tray instead of letting the window vanish. Constructed by
        ``SettingsController.install_window_event_filter``.

        Defined here (not at module scope) because PyInstaller's frozen
        import evaluates module-level class statements BEFORE
        ``_load_qt_classes`` runs - leaving a bare ``QObject`` reference
        at module scope would raise ``NameError`` on every frozen boot,
        the exact crash that prompted the previous workaround that
        turned this into a plain Python class (which then broke
        ``installEventFilter``, since that takes a real ``QObject``).
        Defining it inside this lazy loader lets it inherit ``QObject``
        cleanly without that hazard.
        """

        def __init__(self, close_event_type, window, controller) -> None:
            super().__init__()
            self._close_event_type = close_event_type
            self._window = window
            self._controller = controller

        def eventFilter(self, watched, event) -> bool:  # noqa: N802
            if (
                watched is self._window
                and event.type() == self._close_event_type
            ):
                if getattr(self._controller, "_tray_enabled", False):
                    self._window.hide()
                    return True
            return False

    _qt_classes_cache = {
        "QGuiApplication": QGuiApplication,
        "QQmlApplicationEngine": QQmlApplicationEngine,
        "QQuickStyle": QQuickStyle,
        "QUrl": QUrl,
        "Qt": Qt,
        "qmlRegisterSingletonInstance": qmlRegisterSingletonInstance,
        "ButtonMappingModel": ButtonMappingModel,
        "SettingsController": SettingsController,
        "DiagnosticsController": DiagnosticsController,
        # Tray / window-close wiring needs these at runtime
        # (the SettingsController's install_window_event_filter and
        # _WindowEventFilter pull them from this dict). They are imported
        # above as module-level names inside this function body, so
        # registering them here is the only safe way for the controller
        # to reach them after _load_qt_classes() has returned.
        "QEvent": QEvent,
        "QObject": QObject,
        "QCoreApplication": QCoreApplication,
        "QCloseEvent": QCloseEvent,
        "QApplication": QApplication,
        "_WindowEventFilter": _WindowEventFilter,
    }
    return _qt_classes_cache


# QML module/type names for the two QML singletons below - never
# "controller"/"model" (see run_settings_window()'s registration call for
# why).
_QML_MODULE_URI = "OvbRc003Settings"
_QML_CONTROLLER_TYPE_NAME = "SettingsController"
_QML_MAPPING_MODEL_TYPE_NAME = "ButtonMappingModel"
_QML_DIAGNOSTICS_TYPE_NAME = "DiagnosticsController"  # XRBM-031


def run_settings_window() -> int:
    """Builds and runs the Qt Quick/QML settings window. Blocks until the
    window is closed (``QGuiApplication.exec()``), then returns its exit
    code. Raises ``QtUnavailableError`` (via ``_load_qt_classes()``) if
    PySide6-Essentials is not installed, or if ``main.qml`` fails to load at
    all (e.g. a corrupted/incomplete frozen build missing its bundled qml/
    directory) - never silently opens a blank/broken window.
    """

    classes = _load_qt_classes()
    QApplication = classes["QApplication"]
    QGuiApplication = classes["QGuiApplication"]
    QQmlApplicationEngine = classes["QQmlApplicationEngine"]
    QQuickStyle = classes["QQuickStyle"]
    QUrl = classes["QUrl"]
    qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
    ButtonMappingModel = classes["ButtonMappingModel"]
    SettingsController = classes["SettingsController"]
    DiagnosticsController = classes["DiagnosticsController"]

    # QApplication (not just QGuiApplication) is required because the
    # tray uses QMenu/QSystemTrayIcon (QtWidgets). QGuiApplication lacks
    # widget support and would print "QWidget: Cannot create a QWidget
    # without QApplication" the first time we try to build the tray
    # icon or its context menu in frozen builds. QApplication is a
    # QGuiApplication subclass, so all existing call sites
    # (``app.exec()``, ``app.quit()``, ``app.quit.connect(...)``) keep
    # working unchanged.
    #
    # Dark-mode strategy (user-stated preference; do NOT revert without
    # asking): two style calls, two scopes.
    #   1. QQuickStyle.setStyle("FluentWinUI3")  -- Qt Quick Controls /
    #      QML side (the settings window itself). Must be set BEFORE the
    #      QML engine loads; calling it pre-QApplication() is the
    #      Qt 6.7+-blessed position. FluentWinUI3 honours the Windows
    #      11 system theme via the OS registry, so when Win11 is in
    #      dark mode the settings window renders dark too.
    #   2. app.setStyle("Fusion")  -- QtWidgets side (the tray icon's
    #      QMenu / context menu). Fusion is the only cross-platform
    #      style that respects QPalette dark colours reliably; on
    #      Windows the default "windowsvista"/"windowsui" style often
    #      ignores the palette and stays light.
    # The previous assistant dropped these two lines in commit a426942
    # ("Fusion dark-mode reverted") because a bare setStyle("Fusion")
    # did not darken QML by itself -- correct, because Fusion is a
    # QtWidgets style and has no effect on QQuickControls. Both calls
    # are required together. See docs/dark-mode-strategy.md.
    QQuickStyle.setStyle("FluentWinUI3")
    app = QApplication.instance() or QApplication(sys.argv)
    # Only call setStyle on the full QApplication -- module-level import of
    # qt_settings_app in offscreen test harness yields a bare QGuiApplication
    # via ``QGuiApplication.instance()``, which has no ``setStyle``. Without
    # this guard, importing the module in tests raises AttributeError.
    if isinstance(app, QApplication) and type(app) is QApplication:
        app.setStyle("Fusion")

    model = ButtonMappingModel()
    controller = SettingsController(model)
    diagnostics_controller = DiagnosticsController(controller, config.config_root())

    # XRBM-035 RETRY 1 P2: DiagnosticsController's own __init__() (just
    # above) already started a real background diagnostics worker (see that
    # class's docstring) - EVERYTHING from here through app.exec() returning
    # must therefore go through the SAME shutdown contract on every exit
    # path, not only the happy one. The independent review that requested
    # this found the previous version's try/finally started only at
    # app.exec() itself: an engine.load() exception or an empty
    # rootObjects() (both handled below, BOTH capable of raising before
    # app.exec() is ever reached) would skip _shutdown_diagnostics_workers()
    # entirely, leaving that worker to fall back to this module's atexit
    # hook alone - exactly the insufficient-timing contract XRBM-035's own
    # red evidence already disproved once.
    try:
        # Exposed to QML as SINGLETONS (resolved through the type/import
        # system at document-compile time), not as engine.rootContext()
        # context properties: a root-context property is resolved
        # dynamically through each QML object's context chain, and -
        # empirically, reproduced with a minimal isolated repro during this
        # task - a context property can observe a transient/incorrectly-null
        # value the first time it is read from a binding evaluated during a
        # child component's own construction (e.g. a ListView's
        # currentIndex binding, or any property evaluated inside a
        # ScrollView's deferred content), before every containing component
        # has finished having its own externally-supplied properties
        # assigned. A qmlRegisterSingletonInstance()-registered type has no
        # such hazard: every file that `import`s this module gets the exact
        # same already-fully-constructed instance immediately, with no
        # per-context propagation/ordering involved at all.
        qmlRegisterSingletonInstance(
            SettingsController,
            _QML_MODULE_URI,
            1,
            0,
            _QML_CONTROLLER_TYPE_NAME,
            controller,
        )
        qmlRegisterSingletonInstance(
            ButtonMappingModel,
            _QML_MODULE_URI,
            1,
            0,
            _QML_MAPPING_MODEL_TYPE_NAME,
            model,
        )
        qmlRegisterSingletonInstance(
            DiagnosticsController,
            _QML_MODULE_URI,
            1,
            0,
            _QML_DIAGNOSTICS_TYPE_NAME,
            diagnostics_controller,
        )

        engine = QQmlApplicationEngine()
        qml_dir = _qml_directory()
        engine.addImportPath(str(qml_dir))

        main_qml = qml_dir / "main.qml"
        engine.load(QUrl.fromLocalFile(str(main_qml)))
        if not engine.rootObjects():
            raise QtUnavailableError(
                f"\u65e0\u6cd5\u52a0\u8f7d QML \u8bbe\u7f6e\u754c\u9762\uff1f{main_qml} \u672a\u80fd\u6210\u529f\u52a0\u8f7d\u3002"
            )

        # Wire the live QApplication + root window into the controller
        # so the tray icon, system-tray menu, window-close-to-tray
        # behaviour, and startup-shortcut status refresh all become
        # effective. Called exactly once on first window load.
        root_window = engine.rootObjects()[0]
        try:
            controller.apply_runtime(root_window, app)
        except Exception:  # noqa: BLE001
            logger.exception(
                "controller.apply_runtime failed - tray/startup "
                "integration will be inactive this run"
            )

        return app.exec()
    finally:
        controller.stopHotkeyCapture()
        controller.stopKeyDetection()
        controller.stopBridge()
        # XRBM-035: called HERE, synchronously - whether app.exec()
        # returned normally, engine.load() raised, rootObjects() was empty,
        # or anything else in this block raised - and BEFORE this
        # function's own local Qt/Python objects (engine,
        # diagnostics_controller, controller, model) go out of scope - i.e.
        # while they are all still fully alive, not during interpreter
        # shutdown. Signals any in-flight diagnostics worker to stop and
        # gives it a real, bounded chance to actually finish (see
        # _shutdown_diagnostics_workers()'s own docstring for the full
        # story/red evidence this fixes) instead of relying solely on this
        # module's atexit hook, which fires arbitrarily later - possibly
        # after native Qt/WinRT teardown has already begun.
        _shutdown_diagnostics_workers()
