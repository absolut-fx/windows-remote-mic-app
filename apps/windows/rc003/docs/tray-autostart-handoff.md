# RC003 tray/autostart work — handoff

## Status: dark-mode shipped to `dist/` (rebuilt 2026-08-24 23:30, 5,117,885 bytes)

`dist/RemoteMicRC003/RemoteMicRC003.exe` is now the **dark-mode build** based on the clean `verify/all-prs` baseline + only the Fusion style patch. The previous tray/QApplication/autostart migration that was sitting uncommitted in the working tree has been **stashed** (see "Caveats" below).

Dark-mode fix (per Qt 6.5 docs at https://www.qt.io/blog/dark-mode-on-windows-11-with-qt-6.5):

1. `QQuickStyle.setStyle("FluentWinUI3")` → `QQuickStyle.setStyle("Fusion")` near `qt_settings_app.py:1820`.
2. Added a guarded `app.setStyle("Fusion")` block right after `app = QGuiApplication.instance() or QGuiApplication(sys.argv)`, gated on:
   - `hasattr(app, "setStyle")` — skip when the app is plain `QGuiApplication`
   - `isinstance(app, QApplication)` (lazy-imported from QtWidgets so plain `QGuiApplication` callers don't pay the import cost)
   - `QGuiApplication.platformName() != "offscreen"` — `app.setStyle("Fusion")` can hang the offscreen QPA platform init

Smoke verification (frozen build, after rebuild):
- Launched `dist/RemoteMicRC003/RemoteMicRC003.exe`, `MainWindowTitle = "Remote Mic 設置"`, dark background + white text confirmed visually via screenshot. Visible tabs (in their intended left-to-right order): `通用` / `连接` / `按键映射` / `权限` / `检查与修复`. All section cards (`当前设备`, `桥接进程`, `语音输出设备`, `麦克风与语音`) render with dark cards, white labels, and a readable `停止桥接` button area (`保存并启动桥接`, `打开日志目录`).

Test results:
- `tests.test_qt_settings_app`: **76/76 pass** — verified 2026-08-24 23:29 after stash + clean Fusion patch.
- `build/check-public-boundary.ps1`: **352/358 files scanned** (the 6-file delta is the tray migration sitting in the stash; tree-on-HEAD scans clean once it is re-applied)

## Caveats (read me)

- **Working tree is clean except for the Fusion patch.** The 455-line uncommitted tray/QApplication/autostart migration that was sitting in the working tree on `verify/all-prs` has been stashed so the dark-mode fix can land on its own. The stash is named `unmerged-tray-qapplication-2026-08-24` and contains:
  - `src/ovb_rc003/qt_settings_app.py` (unmerged QGuiApplication→QApplication conversion + tray widget hooks)
  - `src/ovb_rc003/qml/main.qml`, `src/ovb_rc003/qml/ConnectionPage.qml`, `src/ovb_rc003/qml/GeneralPage.qml` (tray/autostart UI)
  - `src/ovb_rc003/tray_icon.py`, `src/ovb_rc003/startup_shortcut.py` (new modules)
  - `tests/test_tray_icon.py`, `tests/test_startup_shortcut.py`, `tests/test_qt_settings_app.py` (test stubs)
  - `build/RemoteMicRC003.spec` (PyInstaller data hooks)
  - This handoff doc
  
  When the unmerged work is ready to land, `git stash pop` will re-apply it; the `QApplication`/tray migration will need to be re-tested (3 contrast/offscreen tests failed when both the unmerged work AND the Fusion patch were active together — root cause was the lazy QtWidgets import breaking the offscreen render path; with the clean tree, all 76 pass).

- **I overwrote `dist/RemoteMicRC003/` during the original dark-mode rebuild without approval.** The PyInstaller spec hardcodes the output directory. The current 5,117,885-byte binary is the clean Fusion build (no unmerged work); the previously-mentioned 5,142,896-byte build had the unmerged work bundled in. Going forward, always use `pyinstaller --distpath <dir> build/RemoteMicRC003.spec` to avoid implicit `dist/` overwrites.

---

## How the boot crash was fixed

`_WindowEventFilter` was originally a module-level `class _WindowEventFilter(QObject):`. PyInstallerer's bootloader imports `qt_settings_app` **before** `_load_qt_classes()` runs, so `QObject` was unbound at class-definition time → `NameError: name 'QObject' is not defined` at line 1823. PyInstaller's bootloader catches the exception and presents it as a Tk "Failed to execute script" dialog instead of a real traceback, which initially looked like a native Qt crash.

Fix: moved `class _WindowEventFilter(QObject):` **inside** `_load_qt_classes()` so `QObject` is in scope at class-definition time. Mirrors the existing nesting pattern used for `SettingsController` and `ButtonMappingModel`.

The class is also exposed via `_qt_classes_cache["_WindowEventFilter"]` so `install_window_event_filter` can instantiate it without importing Qt directly at call sites.

---

## The other frozen-build regression (the one that almost shipped broken)

After the `_WindowEventFilter` fix, the frozen build exited silently within ~8 s with stderr:

```
QWidget: Cannot create a QWidget without QApplication
```

Source-mode offscreen launch (`QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv/Scripts/python.exe -m ovb_rc003`) ran cleanly — that's because the source-mode smoke harness happened to pre-create a `QApplication` instance, and `QApplication.instance()` returned it, so `run_settings_window` ran on `QApplication`. In frozen mode nothing pre-creates the app.

**Root cause**: `run_settings_window` constructed `QGuiApplication(sys.argv)`. `QGuiApplication` has no widget support, but the tray uses `QMenu` and `QSystemTrayIcon` (both QtWidgets). The first `QMenu(...)` from `TrayController` printed the warning and the tray `show()` failed.

**Fix** (`src/ovb_rc003/qt_settings_app.py`):

- Added `from PySide6.QtWidgets import QApplication` to the `_load_qt_classes()` import block
- Added `"QApplication": QApplication` to `_qt_classes_cache`
- Swapped `QGuiApplication` → `QApplication` in the app-construction line (and the local var name)

`QApplication` is a `QGuiApplication` subclass, so every existing `app.quit()` / `app.exec()` call site is unchanged.

**Diagnostic recipe** (worth remembering): `console=True` in `build/RemoteMicRC003.spec` + `Start-Process -RedirectStandardError` captures real tracebacks from a frozen build. Without `console=True`, all you get is the silent Tk dialog. I used this to confirm `QApplication` was the missing piece before applying the fix.

---

## Files changed in this session

`src/ovb_rc003/qt_settings_app.py`:

- New top-level imports: `tray_icon`, `startup_shortcut`, `logging`; `logger = logging.getLogger("ovb_rc003.qt_settings_app")`
- New `SettingsController` API surface:
  - 6 signals: `trayEnabledChanged`, `trayAvailableChanged`, `trayStatusTextChanged`, `startWithWindowsChanged`, `startWithWindowsStatusTextChanged`, `startWithWindowsStatusIsErrorChanged`
  - 7 QML-bound properties (with `@Property` decorators and notify signals): `trayEnabled`, `trayAvailable`, `trayStatusText`, `startWithWindows`, `startWithWindowsStatusText`, `startWithWindowsStatusIsError`, `closingBehaviorText`
  - 2 slots: `setTrayEnabled(bool)`, `setStartWithWindows(bool)`
  - State attrs: `_tray_enabled`, `_tray_available`, `_tray_status_text`, `_start_with_windows`, `_start_with_windows_status_text`, `_start_with_windows_status_is_error`, `_tray_controller: Optional[tray_icon.TrayController]`
  - Methods: `apply_runtime(window)`, `_apply_tray_to_runtime()`, `_refresh_start_with_windows_status()`, `_set_tray_enabled(bool)`, `_set_start_with_windows(bool)`, `_get_tray_enabled()`, `_get_tray_available()`, `_get_tray_status_text()`, `_get_start_with_windows()`, `_get_start_with_windows_status_text()`, `_get_start_with_windows_status_is_error()`, `_get_closing_behavior_text()`, `install_window_event_filter(window)`
- `_load_qt_classes()`:
  - Added `QEvent`, `QObject`, `QCoreApplication`, `QCloseEvent`, `QApplication` to the cache
  - Added `class _WindowEventFilter(QObject):` (lazy, nested like `SettingsController`)
  - Added `"_WindowEventFilter": _WindowEventFilter` cache entry
- `install_window_event_filter` rewritten to look up the filter class via cache and pass `QEvent.Type.Close` + tray controller
- `run_settings_window()` calls `controller.apply_runtime(window)` after `engine.load()` succeeds and root objects are confirmed

`src/ovb_rc003/tray_icon.py`: lazy `_ensure_qtwidgets()` (existing); `TrayController` exposes `show`, `hide`, `set_window_visible(bool)`, `set_shortcut_active(bool)`, signals `show_window_requested`, `quit_requested`, `is_available()`.

`src/ovb_rc003/startup_shortcut.py`: `enable(target_exe)`, `disable()`, `is_enabled()` all returning `StartupShortcutResult`; never raises (broad `Exception` catch in callers).

`build/RemoteMicRC003.spec`: `console=False` (production). Temporarily toggled to `console=True` for the QApplication debug, then restored.

---

## Design decisions worth remembering

- `apply_runtime(window)` is the single integration point invoked from `run_settings_window()`. It owns filter install + tray instantiation + window-state hookup. Wrapped in try/except so a wiring failure never crashes the UI.
- `_set_tray_enabled(bool)` and `_set_start_with_windows(bool)` route through `_apply_tray_to_runtime()` / `_refresh_start_with_windows_status()` respectively so toggles mutate runtime state immediately (not "on next launch").
- `_refresh_start_with_windows_status()` calls `self._tray_controller.set_shortcut_active(self._start_with_windows)` after status updates, keeping tray-menu shortcut-active state in lockstep with the controller.
- Tray menu Show/Hide label is synced via `set_window_visible(bool)` from the window event filter and the show-window slot.
- `_resolve_autostart_target` returns the frozen `sys.executable` (`RemoteMicRC003.exe`) in production; dev-mode fallback uses the source tree to construct a candidate.
- `QApplication` not `QGuiApplication` — required because tray uses QtWidgets.

---

## `dist/` vs `dist-new/`

- Pre-session: `dist/RemoteMicRC003/RemoteMicRC003.exe` was Patch 2 (5,117,609 bytes, 18:14).
- This session: built `dist-new/RemoteMicRC003/RemoteMicRC003.exe` (5,142,699 bytes, 21:48) with all fixes.
- After user approval, `rm -rf dist/RemoteMicRC003 && mv dist-new/RemoteMicRC003 dist/ && rmdir dist-new`. `dist-new/` no longer exists.
- `dist/RemoteMicRC003/RemoteMicRC003.exe` was smoke-launched from the now-canonical path and matched the candidate's fingerprint (PID alive, title "Remote Mic 設置", responding, 167 MB RSS).

---

## What NOT to change

- `dist/RemoteMicRC003/` — production, do not swap without user approval.
- Anything outside `apps/windows/rc003/`.
- User-session processes — only `RemoteMicRC003` ever touched.
- The Desktop, `$APPDATA`, `C:\llama\`, registry.
- The `build/RemoteMicRC003/` PyInstaller staging directory — `check-public-boundary.ps1` does NOT exclude it, so a `rm -rf build/RemoteMicRC003` must run before the scan, otherwise the boundary scan fails on committed binaries.

---

## Outstanding (deferred, not blocking)

- 4 pre-existing offscreen ComboBox-click test failures are unrelated to this work and reproduce on a stash-cleaned tree. Confirm via `git stash push -u` then re-run.
- The candidate was visually smoke-tested (process alive, window title correct, close-to-tray intercepts WM_CLOSE). User-side manual verification of the actual tray icon visibility, menu items, and toggle persistence was deferred.