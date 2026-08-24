# Dark-mode strategy

## User-stated preference

The user wants the settings window in dark mode. Do not revert dark-mode
without asking.

## Why it broke (history)

- Earlier attempts added `app.setStyle("Fusion")` and thought it would
  darken the QML settings window. It did not, because `Fusion` is a
  `QtWidgets` style and has no effect on `QtQuickControls` (QML).
- Commit `a426942` ("Fusion dark-mode reverted") on branch
  `verify/all-prs` then dropped *all* style calls as a "revert", even
  though the original working call was `QQuickStyle.setStyle("FluentWinUI3")`
  for QML — a different mechanism. The result was a light-mode app.

## The two-scope fix

`src/ovb_rc003/qt_settings_app.py` runs the settings window. The window is
QML (`QtQuickControls`) but the tray icon's context menu is `QtWidgets`
(`QSystemTrayIcon` + `QMenu`). They are different rendering pipelines and
each needs its own style call:

```python
# 1. QML side (settings window). Must run BEFORE engine.load(); calling it
#    before QApplication() is the Qt 6.7+-blessed position. FluentWinUI3
#    honours the Windows 11 system theme via the OS registry, so when
#    Windows is in dark mode the settings window renders dark too.
QQuickStyle.setStyle("FluentWinUI3")
app = QApplication.instance() or QApplication(sys.argv)
# 2. QtWidgets side (tray QMenu). Fusion is the only cross-platform
#    QtWidgets style that respects QPalette dark colours reliably.
app.setStyle("Fusion")
```

Both lines are required together. Removing either re-introduces a
light-mode component.

## What if FluentWinUI3 still renders light on a light-mode Windows?

FluentWinUI3 follows the OS theme. If the user's Windows 11 is set to
light mode at the system level, the settings window will be light by
design. The fix in that case is one of:

1. **Tell the user to flip Windows to dark mode** (Settings → Personalization
   → Colors → "Choose your color" = Dark). Cheapest, no app-side work.
2. **Force a dark palette on top of FluentWinUI3** via
   `QQuickStyle.setStyle("Basic")` + a hand-built dark `QPalette` applied
   to `QApplication`. More invasive; needs a QSS file or palette helper.

Before reaching for option 2, confirm with the user that option 1 is
acceptable.

## Regression test (future work)

A test that boots `qt_settings_app.py` offscreen, takes a screenshot of
the settings window, and asserts the dominant background pixel is dark
(`< #404040`) would catch a future "Fusion dark-mode reverted"-style
breakage automatically. Not yet implemented; out of scope of the
current fix.
