# Remote Mic RC003 — improvement list

Plain list, ordered by value. Everything here is either measured or read out of
the code; nothing is a guess. Work happens in
`%USERPROFILE%\projects\windows-remote-mic-app-work`, not your original folder.

**Verified baseline (2026-09-16, 10:12).** Nothing autostarts on this machine. The
`RemoteMicRC003-Elevated` scheduled task does not exist, and the Startup folder holds
only `desktop.ini`. Every session begins with you opening an administrator PowerShell
and running `launch-rmic-elevated.ps1` by hand. Live at time of writing: settings UI
PID 32592 (started 08:36:38), bridge PID 31228 (started 08:40:05) — the first bridge,
PID 18380 (08:36:40), is already gone, so the bridge restarted once on its own.

## Status: items 1, 3, 5, 6 and 7 are built (work copy, uncommitted)
Items 8 and 9 were fixed earlier in the same session. Items 2 and 4 are closed as
well - item 2 turned out to have been removed already, item 4's premise was false.
Item 7 is now confirmed in production (the 140 MB log rotated to `app.log.1` on the
first record the new build wrote). Item 6 has a shipped mitigation and a measured
root cause for 20 of its 812 failures; the dominant 703 are still open, and both are
written up below rather than papered over. Separately, the suite is now green with
the app running — see "The suite cannot pass while the app is running" below.

Measured with `<workspace>/probe_f5_wait.py`, which includes a control so the numbers
cannot be satisfied by disabling the wait for everything:

```
F5    (0x74) unarmed consume :    0.000 ms
RIGHT (0x26) unarmed consume :   60.178 ms   <- control: window still applies
should_suppress(0x74,0)=True   handle_key_event(0x74,0,True)=True   <- suppression intact
```

- **Item 3 done.** The temporary diagnostic is deleted; `arm key edge` and `consume key
  edge` are now DEBUG. Between them those three were **865,641 of 954,014 lines (90.7%)**
  of the log, and `consume key edge` alone was 243,257 lines logged from the hook thread.
  At INFO the module now emits nothing.
- **Item 5 done.** `_NEVER_ARMED_VK_CODES = frozenset({0x74})` is now shared by
  `arm_key_event` and `consume_armed_key_event`, so the voice key stops paying a wait
  window that can never match.
- **Tests:** 4 added, **20/20 green**, suite 0.397 s, and mutation-verified — emptying the
  exemption set fails 4 tests, passes 0 with the fix present. Twelve neighbouring modules
  re-run green.
- **Not yet verified in the running app.** These numbers are from the source-level probe.
  The real test is a restart with the mic button in use.

### The Startup-folder test hazard — **resolved 2026-09-16**

`tests/test_startup_shortcut.py` wrote a **real** `RemoteMicRC003.lnk` into your Startup
folder — an unelevated autostart, the exact thing this list exists to remove. Its cleanup
sat after the assertions, so a failure skipped it and the entry stayed. `addCleanup` was
already registered before the assertions — but that only helps if `disable()` can actually
delete the file.

**The earlier `ALREADY_ENABLED` / `FAILED` confusion was the harness, not the code.**
Re-measured 2026-09-16: `os.unlink` on a file in the Startup folder returns
`[WinError 5] Access is denied` from a sandboxed process, and succeeds immediately when the
sandbox is bypassed. So `disable()` returned `FAILED`, both its own call and its
`addCleanup` were effectively no-ops, and the `.lnk` stayed on disk.
**`startup_shortcut.disable()` was not broken.** I briefly concluded it *was*, from a run I
believed was unsandboxed and was not — the confound this document's own "a verifier that
cannot fail proves nothing" rule is about.

**The module and its tests were deleted later the same day** after the elevated-autostart
rework made them unreachable. The hazard no longer exists; this section is kept as a record
of the measurement.

The class was **opt-in** before deletion: it skipped unless `OVB_RC003_RUN_STARTUP_FOLDER_TESTS=1`, with a
docstring naming this incident. A plain `unittest discover` can no longer plant an autostart
entry on your machine. Verified after a full run: the folder holds only `desktop.ini`. The
two `.lnk` files that runs did create are quarantined in
`<workspace>/startup-folder-quarantine/` — moved, not deleted.

---

## The test suite's silent crash — **found and fixed 2026-09-16**

`unittest discover` over the whole suite was not runnable, and the reason was **not** the
hang. Once the hang was fixed the suite got further than it had ever been and died with
`0xC0000409` (`STATUS_STACK_BUFFER_OVERRUN` — what MSVC's `abort()` raises through
`__fastfail`), printing **no traceback, no `Ran N tests`, and no failure line**. Just dots,
then nothing.

Root cause: `unittest discover` runs every module in **one** process, and Qt permits one
application object per process — so the first module that needs one fixes its type for the
whole run. `test_qt_settings_app` built a bare `QGuiApplication`; `test_tray_icon` then
built its `QMenu` (a `QWidget`) on it, which is `qFatal` → `abort`.

Measured with a two-mode probe — same script, only the app class changed:

| app in the process | `QMenu()` | exit |
|---|---|---|
| `QApplication` (control) | constructs, then `QSystemTrayIcon` constructs | **0** |
| `QGuiApplication` | dies at `QMenu()` | **abort** |

**Pre-existing, not caused by this session's edits** — reproduced identically against the
pristine original checkout at the same commit (`ad909b4`). It was merely unreachable while
the hang stopped the suite earlier in the same file. The repo already knew discover was
unsafe: `build/build-candidate.ps1` carries a comment naming the hang and runs a curated
`@testArgs` list instead.

Fixed by one shared helper, `tests/qt_app.py:get_application()`. It always returns a
widgets-capable `QApplication` — a strict superset of `QGuiApplication`, so every consumer
still gets what it asks for — and raises an ordinary `RuntimeError`, instead of aborting
the process, if something else already locked in a non-widgets app.

`tests/test_qt_app_singleton.py` (9 tests) enforces it with an `ast` scan of every test
module. Validated against the control: the scan reports `[980]` on the original tree and
`[]` here. It deliberately allows `QGuiApplication.instance()` — a read that can never
create an app, and exactly what a legitimate consumer calls — while flagging construction.
It also ignores string literals, which is what lets the QML subprocess probe scripts keep
using `QGuiApplication` inside their own process.

Result: `discover` now **completes** — `Ran 1119 tests in ~120 s`, with a real report.

**Six public-boundary violations cleared** as well (`test_real_tree_has_zero_violations` is
green again). All six sat in untracked session files quoting your real absolute paths and
one real Bluetooth address; replaced with `%USERPROFILE%\...` and a redacted phrase.

### Both of those leads were wrong — corrected, and both fixed

My first pass at this named the wrong cause for each. Measured, then corrected:

**1. The 4 `test_windows_diagnostics` errors were not Qt.** Nothing to do with the
offscreen platform plugin. `test_qt_settings_app`'s
`test_resolve_autostart_target_in_source_mode_skips_sys_executable` assigned
`sys.executable` to a deliberately nonexistent temp `.exe`, and its `finally` block was a
bare `pass` behind a comment claiming it restored:

```python
sys.executable = str(Path(self._tmpdir.name) / "totally-not-a-real-exe.exe")
try:
    result = self.Controller._resolve_autostart_target()
finally:
    # Restore sys.executable so we don't pollute later tests.
    pass
```

`sys.executable` therefore stayed wrong for the rest of the process, and every later
`subprocess.Popen([sys.executable, ...])` failed with `FileNotFoundError [WinError 2]`.
Captured at the moment of failure by wrapping `Popen.__init__`:

```
argv[0]               : 'C:\...\Temp\tmpasjt67bi\totally-not-a-real-exe.exe'
exists(argv[0])       : False
sys.executable        : 'C:\...\Temp\tmpasjt67bi\totally-not-a-real-exe.exe'
exists(sys.executable): False
cwd                   : '<workspace>'   exists: True
```

Fixed: the test now restores `sys.executable` and asserts that it did, and compares
against the local fake path instead of the previously-mutated global — otherwise the
assertion would have gone vacuous the moment the leak was fixed.

**2. The `ResourceWarning` was not `ble_transport_winrt`/`connection_supervisor`.** Both
call `asyncio.get_event_loop()` *inside* `asyncio.run()`, which closes its loop; a ledger
of all **104** loops created during a full run found **0 offenders** in the main process.
The real leak was one helper in `tests/test_reconnect_policy.py`:

```python
return asyncio.new_event_loop().run_until_complete(coro)   # never closed
```

The suite's other three `run_until_complete` helpers (`test_app_wiring.py`,
`test_ble_transport_contract.py`, `test_connection_supervisor.py`) all create-run-**close**,
with a comment naming this exact warning. Measured on that module alone:

| tree | result |
|---|---|
| original (unfixed) | **3** `ResourceWarning` lines — the event loop *and* two unclosed self-pipe sockets |
| work copy (fixed) | `Ran 19 tests / OK`, **0** |

**3. `build-candidate.ps1`'s stated reason was wrong too**, and its `$testArgs` ordering
existed to work around a cause that never existed. Both comments are corrected. The
curated module list itself is now gone as well: the script runs the same full
`unittest discover` as CI (`-W error::ResourceWarning -m unittest discover -s tests -t .
-p "test_*.py"`), which the two fixes above made possible. Checked with the PowerShell
parser: `PARSE ERRORS: 0`.

### All of it was one bug — and one of the three was passing vacuously

| test | what it actually was | status |
|---|---|---|
| `test_qt_settings_app.ButtonsPage...direct_save` | the probe selected the settings tab by a **hard-coded index** that went stale when a `General` tab was inserted ahead of the mapping page, so it was clicking into an **invisible** page | **fixed** |
| `test_qt_settings_app.ButtonsPageHotspotGeometryTests...ok_true_center` | the *same* stale index — and it **passed anyway**, because the selection it asserted was already the controller's default | **fixed, and de-vacuumed** |
| `test_qt_lifecycle_cleanup` | asserts the full-suite subprocess exits 0 | **green** once the two above are |
| `test_build_artifacts...gated_by_resource_warning` | asserts the build script uses `-m unittest discover`; it used a curated `@testArgs` list instead | **fixed** — the script now runs full `discover`, which the fixes above made possible |

**The "click did not focus the ComboBox" failure was never about Qt focus.** `main.qml`'s
tab order is 通用 / 连接 / 按键映射 / 权限 / 检查与修复. When the probe was written the order
was 连接 / 按键 / 权限 / 检查与修复, so index 1 *was* the mapping page. A `General` tab was
later inserted at index 0, and from then on index 1 selected **Connection**. `StackLayout`
makes every control on a non-current page invisible, so the synthetic click landed on
nothing — and the assertion reported the one true-but-useless fact that followed from it.

Measured on the same probe, before and after:

| tab selection | `mappingList` height | visible | ComboBox | click |
|---|---|---|---|---|
| `currentIndex = 1` (stale) | `0.0` | `False` | `100×40`, invisible | `activeFocus` stays `False` |
| `mappingTabButton` lookup | `474.0` | `True` | `185×40`, visible | `activeFocus` `True`; text typed; value persisted |

The hotspot probe was the worse of the two, because it failed **silently**:

```
VISIBILITY mappingList= False  photo= False  currentIndex= 1
SELECTED_BEFORE= ok
SELECTED_AFTER= ok      <- the click changed nothing; the test passed anyway
```

`"ok"` is the controller's default selection, so a click that missed every hotspot left the
asserted value unchanged and the test was green while proving nothing. That is the same
class of error as a verifier that cannot fail.

Both probes now select the tab by its stable `objectName` (`mappingTabButton`), assert the
target page is **visible** before interacting with it, and the hotspot probe reports the
selection before *and* after the click so the assertion tests a transition, not a default.
Negative controls, all run: reverting the tab switch makes both probes fail on the new
visible-page assert; aiming the click at `power`'s centre instead of OK's makes the probe
report `"power"`, which the new transition assert rejects.

`tests/test_ui_probe_stable_selectors.py` enforces the rule mechanically — two AST passes
(the file, then each embedded probe script), so this document's own quotations of the bug
cannot trip it. Validated against the pre-fix file, where it reports exactly the three
offending lines, and against the fixed tree, where it reports none.

Measured suite state: **1127 tests, 0 errors, 0 failures, 8 skipped, 155.8 s, 0
`ResourceWarning`** — from 4 errors and 4 warnings at the start of this pass. `discover`
now runs clean end to end, so `build-candidate.ps1` uses it instead of a curated module
list.

---

## The suite cannot pass while the app is running — **found and fixed 2026-09-16**

`tests/test_main_entrypoint.py`'s argument-mode tests call the real `main()` with no
arguments (and with `--settings`), which routes through `__main__._run_settings_window()`
— and that enters the **real** `single_instance.SettingsInstanceGuard()` *before* it calls
`settings_ui.main()`. The tests stubbed the *bridge* guard and replaced `settings_ui.main`,
but never the settings guard, so they silently required that **no real settings window was
open on the machine**. Launching the app mid-session turned the suite red with no code
change at all:

```
ovb_rc003.single_instance.DuplicateInstanceError:
  another Remote Mic RC003 settings window is already running in this Windows session
```

One failure amplifies it: `test_qt_lifecycle_cleanup`'s
`test_full_test_suite_command_produces_no_resourcewarning_at_shutdown` runs the whole suite
in a subprocess and asserts it exits 0, so any environment-dependent failure anywhere is
reported twice — once as itself, and once as a failure of the suite.

Controlled, same code both times: `Ran 1138 tests ... OK (skipped=8)` with the app closed,
`FAILED (failures=1, errors=2)` with it open. Fixed by stubbing `SettingsInstanceGuard` in
the shared `setUp` — and, so that a stub cannot quietly replace a real check with nothing,
the two settings tests now also assert the settings path **reached** the guard.
Mutation-verified: making `_run_settings_window` skip the guard fails both with
`AssertionError: 0 != 1`, and the source is restored byte-identical.

After the fix: `Ran 1138 tests ... OK (skipped=8)` **with the app running** — which is the
state a developer is actually in when they run it. The general lesson is worth keeping:
a test that exercises a real entry point inherits that entry point's environmental
dependencies, and a guard whose whole job is to detect a second instance will detect
*yours*.

---

## The one thing

**The 7 dead keys were a privilege-ordering bug in the Frida injector, not an elevation
problem. Fixed, measured, tested. The elevation project was chasing the wrong thing.**

`inject_current_process` called `_target_process_name(pid)` **before**
`enable_debug_privilege()`. The RC003 host is a session-0 SYSTEM `wudfhost.exe` launched
by `services.exe`, and its process DACL denies even `PROCESS_QUERY_LIMITED_INFORMATION`
to a caller holding SeDebugPrivilege *present but disabled* — which is how every fresh
elevated token arrives. So the guard raised `WinError 5` first, which meant
`enable_debug_privilege()` never ran, which meant every 2-second retry failed
identically, forever.

Measured 2026-09-16 with the app's real High-integrity token, all in one process:

| step | result |
|---|---|
| `_target_process_name(2964)` | `PermissionError [WinError 5]` |
| `enable_debug_privilege()` | OK |
| `OpenProcess(2964)` — QUERY_LIMITED / QUERY / INJECTOR_RIGHTS | OK / OK / OK |
| `ProcessProtectionInformation` | `Type=0` — **not** a protected process |
| `inject_library(2964, dll)` | **SUCCEEDED** |

The same call on the same pid, before and after enabling the privilege:

```
WUDFHost 2964 created: <cannot open for query: winerror=5>   [pre-SeDebug]
WUDFHost 2964 created: 2026-09-16 08:32:27                   [post-SeDebug]
```

After the fix, `inject_current_process(2964)` returns cleanly. 5 regression tests added
(`tests/test_frida_hid_tap_injector.py`), green, and **mutation-verified** — moving the
call back fails 2 tests and errors 1, with the recorded order showing
`_target_process_name` at index 1 and `enable_debug_privilege` at index 4.

### Why it worked at 08:40, and why that was luck

The host **never restarted** — created 08:32:27, i.e. *before* the 08:40 attach — and the
same pid is unopenable now. So the difference was the caller's token.
SeDebugPrivilege **enablement is inherited across CreateProcess**; measured:

```
parent own state : present=True enabled=False     <- a fresh elevated process
arm A (child spawned BEFORE enabling): enabled=False
parent enable    : enabled
arm B (child spawned AFTER enabling) : enabled=True
```

Your 08:40 launch inherited an already-enabled SeDebugPrivilege from whatever started it.
Every launcher since produces a token with it disabled, and the old ordering then made it
impossible for the app to enable it itself. The fix removes the dependency on the launcher.

### What this does *not* change

**Elevation is still required.** A plain `python.exe` gets a filtered token, so the app
must run elevated for the tap to attach at all. But elevation alone was never sufficient,
and it was never the actual defect. Note `EnableLUA=1` with
`ConsentPromptBehaviorAdmin=0` — UAC is on but set to "elevate without prompting", so
`launch-source-fixed.cmd` elevates with no prompt. The scheduled-task work in item 1 is
still worth doing so that elevation survives a reboot; it is just no longer load-bearing
for the keys.

---

## Item 1 — **done 2026-09-16**

**Make "start with Windows" register the elevated scheduled task, and delete the
Startup-folder shortcut path.**

The task is **registered and verified**: `RemoteMicRC003-Elevated`, trigger
`MSFT_TaskLogonTrigger`, principal `absolut` with `RunLevel=Highest`, action = the installed
`RemoteMicRC003.exe`. `elevated_autostart.get_state_dict()` returns
`registered=True enabled=True state=READY`, and the status text the toggle will display is:

> 已启用：下次登录会以管理员权限自动启动，Frida 可以注入，音量/返回键可用。

The UI wiring is now in place. `SettingsController`'s "start with Windows" property no
longer reads the Startup-folder `.lnk` — it queries and drives the scheduled task through
`elevated_autostart`, turning it on registers the task if absent and then enables it, and
turning it off disables it. The old `startup_shortcut` import is gone from
`qt_settings_app.py`.

**Correction — the import was not actually gone.** It survived in an
orphaned tray slot (`_on_tray_open_startup_folder`) that called
`startup_shortcut.open_startup_folder`, a function that has never existed
in that module, and the miss was silently swallowed by a
``getattr(..., None)`` guard that logged "unavailable" instead of raising.
The slot was unreachable: no signal connected to it (the tray deliberately
has no "open startup folder" menu item, see the removed-API regression guard
in `test_tray_icon.py`), so it could not fail at runtime and no test could
notice it.

Two further orphaned slots turned up in the same source walk:
`_on_window_minimised_to_tray` and `_on_window_restored_from_tray`, also
unconnected and unused. All three were deleted. An AST-level guard now
asserts that every ``_on_tray_*`` / ``_on_window_*`` slot is referenced
somewhere and that every ``TrayController`` signal is connected, so the
next orphan fails the suite rather than shipping silently.

The `ovb_rc003.startup_shortcut` hiddenimport was dropped from the
PyInstaller spec because nothing in the frozen source references the module.
**The module and its tests were deleted** — it was dead weight, and the
orphaned-slot guard proved nothing else imported it. The code is in git
history if the Startup-folder mechanism ever needs to be revived.

Two design points worth recording, both measured:

- **The query is off the GUI thread.** Asking Task Scheduler costs **~1.2 s** (it spawns
  `powershell.exe`), so it could not run in the `SettingsController` constructor on the GUI
  thread. It now runs on a `threading.Thread` and marshals the answer back over a queued
  signal (`_autostartStateReady`), the same shape the diagnostics worker uses. An
  in-flight guard stops a window-show refresh and a user toggle overlapping into two
  concurrent PowerShell spawns.
- **The status is reported from a fresh read, not from what was asked for.** If the task
  cannot be read, the switch keeps its position and only the status line reports the
  failure — the toggle never claims a state that was not observed.

**Rebuilt and installed 2026-09-16 17:31**, so the exe you actually launch now carries all
of the above. `%LOCALAPPDATA%\RemoteMic\RC003`; the previous build is preserved as
`RemoteMicRC003.exe.bak-20260916-173401` + `_internal.bak-20260916-173401`.

Verified, not assumed:

| check | result |
|---|---|
| PyInstaller xref: frozen `.py` paths | 33 from the work copy, **0 from your original folder** |
| Bundled `ovb_rc003_qml/*.qml` vs source | all 7 **byte-identical** |
| Frozen `ovb_rc003.qt_settings_app` string constants | **220** (was 209); contains `openLogFile`, `_autostartStateReady`, the unreadable-state text |
| Same check against the **previous** exe | 209 constants, all three markers **absent** — so the check discriminates rather than always passing |
| `--dry-run` (imports every first-party module) | exit **0**, both the build and the installed copy |
| Task action | still `...\RemoteMic\RC003\RemoteMicRC003.exe` — it launches the new build automatically |

The task is unchanged and needs no re-registration: its action points at the install path,
so the rebuild is picked up on the next logon.

One defect in `%USERPROFILE%\register-rmic-autostart.ps1` is real and worth fixing:
`:47`'s `$LASTEXITCODE` guard can never fire, because `Register-ScheduledTask` is a
cmdlet that never sets it. The other one — `launch-rmic-elevated.ps1:17` forcing
`OVB_RC003_LOG_LEVEL = "DEBUG"` — is **not** a real defect; see item 4.

---

## The list

### 1. Start with Windows — one switch, elevated
Wire the General page toggle to the scheduled task. Turning it on registers the task
if needed and enables it; turning it off disables it. Status text tells you which of
the three states you are in: not registered / registered but off / on.
*Why it matters: this is the difference between the 7 keys working after a reboot and
not working.*

### 2. Delete the second autostart switch
The "bridge autostart" card in `GeneralPage.qml:184-209` binds
`SettingsController.bridgeAutoStart*`, and **no such property exists in
`qt_settings_app.py`** — grep finds `bridgeAutoStart` only in the QML. Qt renders it
with empty bindings, so that switch has never done anything.
*Why: two switches that both claim to control startup is a support trap. One is dead.*

### 3. Stop the log flood — **this is the best explanation for the flakiness**
Measured live at 10:12: the log is **139,946,063 bytes (133.5 MiB) / 954,014 lines**, and
still growing while the app runs. Of the last 3,000 lines, **2,822 (94.1%) come from
`legacy_key_suppressor_windows`** and **2,748 (91.6%) are `hook msg:` lines**. The code
itself labels it *"TEMPORARY DIAGNOSTIC ... After we identify the right message type, this
branch gets replaced"* (`:501-508`). That investigation is over — those keys are carried
by Frida, not by this hook.

Two things make this worse than "the log is big":

- It sits **inside the `WH_KEYBOARD_LL` callback**, formatting and writing a line for
  every key message before `CallNextHookEx`. Windows silently drops hook callbacks that
  overrun `LowLevelHooksTimeout` (300 ms default) — and a dropped hook means the remote
  keys stop firing until the app restarts. "Working, but a little flaky" is exactly what
  that looks like.
- It logs **every keystroke you type**, not just remote keys. The tail of the log shows
  `vk=0xE7` (VK_PACKET) records whose scan codes spell out the characters — it captured
  the message being typed into a terminal at 10:12:11. That is a full keystroke record in
  a world-readable file.

*Why: it is the only unbounded, high-frequency work on the global input hot path. Fix
this first and re-test the flakiness before chasing anything else.*

### 4. ~~Turn off DEBUG logging at logon~~ — **withdrawn, the premise is false**
`launch-rmic-elevated.ps1:17` sets `OVB_RC003_LOG_LEVEL = "DEBUG"`. Measured
2026-09-16: **nothing reads that variable.** Grep over the whole repo finds
`OVB_RC003_LOG_LEVEL` (and `OVB_RC003_PROFILE`) only in `docs/` and in launcher
`.cmd`/`.py` files — never in `src/`. `logging_setup.py:48` hardcodes
`logger.setLevel(logging.INFO)`. So the DEBUG setting has never had any effect, it is
not what makes the log large, and turning it off would change nothing.

The registered task also no longer runs that script: the action is the exe directly.
Log volume is item 3's problem, and item 3's diagnosis (the hook-path diagnostic in
`legacy_key_suppressor_windows`) stands on its own measurements.

### 5. Remove the 60 ms dead wait on every mic-button press
`LegacyKeySuppressor` is constructed with `{0x74}` (F5) as its suppress set
(`app.py:251-257`), and F5 is also in `rc003_vk_codes` via `KEYBOARD_VK_TO_BUTTON`
(`raw_input_windows.py:90`, `0x74: "mic"`). So in the hook callback, F5 reaches
`consume_armed_key_event`, which **waits out its full window** (`consume_wait_seconds`
default `0.060`, `:118`) before giving up. Measured in the log: `waited=0.063s`.

That wait **can never succeed**: `arm_key_event` deliberately refuses to arm `0x74`
(`:227-229`, *"F5 is handled by the dedicated voice path below"*). So every mic-button
press burns ~60 ms of the global keyboard hook on a match that is structurally
impossible. The suppression itself happens later, correctly, via `handle_key_event`
(`:197-206`).

**Correction to an earlier claim in this document.** I previously wrote that the
ATVV-versus-F5 race *was* your flakiness. It is not. F5 is genuinely swallowed —
`should_suppress(0x74)` is true, so `handle_key_event` returns `True` and the hook
returns 1. The `matched=False armed=0` lines are expected, not a leak. The race costs
churn (4,497 ATVV-first versus 3 F5-first, with 4,829 duplicates discarded), but it is
not the intermittent failure. The intermittent failure is item 3.

*Why: 60 ms of guaranteed-dead time on the input hook, per press, for nothing.*

### 6. The connect flake — **mitigated; root cause measured for 20 of 812, 703 still open**

The stated symptom was wrong. Connect does **not** fail with `ACCESS_DENIED` on the
per-service characteristic query: `ACCESS_DENIED` appears **0 times** in the whole
955,192-line production log. The 3,154 `Access is denied` lines in it are all
`frida_compat` HID-tap injection retries — a different problem. The `ACCESS_DENIED` I
had recorded came from my own probe scripts, not from the app.

What the app actually does, from the log's own arithmetic:

| line | count |
| --- | --- |
| `populate step` (full enumeration) | 824 — **all SUCCESS (0)**, `services_count=9` |
| `connection attempt failed: ATVV voice service not found` | 792 |
| `connection attempt failed: ATVV characteristic not found` | 20 |
| `connection attempt failed: Invalid parameter count` | 31 |
| `connection attempt failed: no paired RC003 candidate was discovered` | 15 |
| `voice capabilities received` (successes) | 144 |

824 = 812 + 12, and 812 + 31 + 15 = 858 = every failure. So the populate step is
**never** the flaky one, and the very next call is what fails: the UUID-filtered
`get_gatt_services_for_uuid_with_cache_mode_async(UNCACHED)`.

**The duration is the tell.** Pairing each `populate step` line with the outcome that
followed it (`analyze_connect_timing.py`):

| outcome after populate | n | min | median | max | under 1 s |
| --- | --- | --- | --- | --- | --- |
| `ATVV voice service not found` | 703 | 2.760 s | **7.687 s** | 7.701 s | **0** |
| success | 98 | 0.488 s | 1.865 s | 6.871 s | 15 |
| `ATVV characteristic not found` | 20 | 0.120 s | **0.148 s** | 7.639 s | 17 |

Two failure modes with *opposite* timing, and a status-only log cannot tell them apart:

* **703 failures ending 2.76–7.70 s after populate, none of them under a second, while
  no success ever exceeds 6.87 s.** The failures stop exactly where the slowest success
  stops — a **timeout ceiling**, not a permission or protocol error. See the contention
  section below: what is proven is that this call is the one that fails, and that it
  fails by waiting rather than by being refused.
* **20 failures ending in ~150 ms** — far too fast for a wire read. A refusal, not a
  wait.

### The second process is the thing — **measured, with a controlled before/after**

The 150 ms failures are `ACCESS_DENIED` (status 3), and they are reproducible on
demand. Read from the frozen bytecode, the *installed* build cannot report that: its
characteristic-step raise has no status in it, which is why three weeks of logs never
named it. Probing the live WinRT surface directly shows it plainly:

```
populate (full enumeration): status=SUCCESS n=9      <- ab5e0001 IS in the list
service step:                status=SUCCESS n=1 459ms
characteristic read x6 (3 different service objects, both cache modes, repeats):
                             status=ACCESS_DENIED n=0 ~11ms
```

Provenance makes no difference — the ATVV service obtained from the full enumeration,
from the UUID-filtered UNCACHED query, and from the default query all behave
identically, and so do CACHED and UNCACHED reads, and immediate repeats. So it is not
"how you read the characteristics". Something else owns the device.

That something was on screen the whole time. The timeline is unambiguous, because a
second app instance started in the middle of the probing:

| time | what | result |
| --- | --- | --- |
| 20:02:06 | connect probe, **no app running** | **6 of 6 SUCCESS**, no failure of any kind |
| **20:04:19** | **the app is launched** (`RemoteMicRC003.exe`, pid 35732, from Total Commander) | — |
| 20:05:11 | same probe, 150 s idle that *ended 48 s after the launch* | **2 of 2 `ACCESS_DENIED`** |
| 20:08–20:09 | strategy, provenance and app-path probes | every characteristic read `ACCESS_DENIED` |

The boundary is the app launch to the second. With the device free, six consecutive
connects succeed; with the app holding it, **not one** characteristic read does. That
is the "is the app contending with itself" question from the previous revision of this
item, answered: **yes, and a second consumer on the same RC003 gets `ACCESS_DENIED`
rather than a share of it.**

Which means the 20 production `ATVV characteristic not found` cases are not a cache
problem at all — they are two consumers on one device. In this app the two consumers
are the **bridge and the settings window**, and the log's failure bursts are all at
`startup: resolving RC003 identity`, i.e. exactly when a UI start can collide with a
bridge that is already up.

**What is *not* established:** the dominant 703 × 7.7 s service-step timeouts were
**not** reproduced by contention. Under contention the refusal came back in ~11 ms on
the *characteristic* step while the *service* step kept succeeding, which is the
opposite of the production signature. So contention explains the 20 — and the 703 are
explained by the next section, which changes what the recovery is expected to do.

### The bursts ramp, every time — so the recovery should collapse the dominant mode

Reconstructing all 145 connect bursts from the log attempt by attempt
(`analyze_bursts.py`), ranking each attempt by *how far it got* (service = got nothing,
char = got the service but not its characteristics, ok = connected): **1005 attempts,
858 failures, 147 successes**, and:

| attempts in burst | bursts | median burst duration | most common mode sequence |
| --- | --- | --- | --- |
| 1 | **90** | 0.0 s | `ok` |
| 2 | 5 | 13.1 s | `service→ok` |
| 3 | 12 | 30.2 s | `service→service→ok` |
| 4 | 8 | 36.0 s | `char→char→char→ok` |
| 5–23 | 23 | 78–1297 s | `service→…→ok` |
| 32–176 | 7 | 2089–31434 s | `service→…` (device off) |

**145 of 145 bursts ramp.** A later attempt never got *less* far than an earlier one —
not once. So this is not random contention and not a static condition: the link comes
up progressively, and the retries are what bring it up.

Two conclusions, and both move away from where this item was heading:

* **62% of connects need no retry at all** (90 of 145 bursts). This is a startup-race
  problem, not a broken connect path.
* **The dominant mode is exactly what round 3 recovers from.** "ATVV voice service not
  found" *is* "the UUID-filtered query returned nothing", and the enumeration it falls
  back to returned 9 services including `ab5e0001` on **all 824** attempts. So on those
  703 attempts the recovery has a service to find and should turn the attempt into a
  success instead of discarding it. That is a direct fix for the dominant mode, not a
  mitigation — the previous revision of this item understated it.

**The residual risk, and the one thing a live run must check:** whether the
*enumerated* service object is usable for the characteristic read in the state where
the filtered query failed. The provenance probe tried to answer exactly this and could
not — every read was `ACCESS_DENIED` because the app was holding the device. So the
open question is the runtime behaviour of the recovered service, not the recovery
logic, which is unit- and mutation-tested.

**Decision — leave the reconnect cadence alone.** The backoff is `3/6/12/24/48/60 s`
(`reconnect_policy.py`, deliberately labelled "Mac 1.8.15 parity"), and the median gap
between attempts inside a burst is **65.5 s** — which is what makes a burst feel like
minutes rather than seconds. It is tempting to speed that up. But once round 3 collapses
the `service` bursts, the only bursts left that retry at all are the `char` ones: 8 of
145, ~36 s each. Cutting the cadence would save a few seconds on 5% of connects, break a
stated parity invariant, and spend the remote's battery faster in the pathological case
(one burst retried for 8.7 hours). **Not worth it.**

**The fix** (`ble_transport_winrt.py`, round 3): when the UUID-filtered query yields no
service, recover it from the enumeration we were already handed for free — the populate
call that produced it returns 9 services in ~0.2 s, which is a cache hit rather than a
radio round-trip, so the fallback costs nothing measurable. The recovery sits on the
branch that would otherwise `raise`, so **no currently successful connect can change
behaviour**, and if the UUID is absent from the enumeration too, `connect()` raises
exactly as before.

**Round 3 also finished the diagnostic.** Round 2 logged the status but not the
duration; both service-side steps now log `elapsed_ms=`, and the recovery logs the full
enumerated UUID list — so one run names the condition *and* proves whether `AB5E0001`
was in the cached table. That is what turned "20 mysterious failures" into
`ACCESS_DENIED` in a single afternoon.

**A correction to round 2's own premise.** The comment on the populate call says it
exists to "force a full enumeration first so the subsequent UUID-filtered query can
succeed reliably". The production log falsifies that: populate succeeded on all 824
attempts and the next call still failed 812 times.

**And a correction to this item's own first draft.** It originally claimed the 20
characteristic failures were `ACCESS_DENIED`, I "corrected" that to a stale-cache read
on the strength of `ACCESS_DENIED` appearing 0 times in the log — and the live probe
then showed the original claim was right and my correction was wrong. The string is
absent from the log because the build that produced those 20 lines did not print the
status. Absence of a string in a log is evidence about the *logger*, not about the
device.

Two further stale claims, now checked: the installed exe is **not** behind round 2 —
reading the frozen bytecode (`check_round2_in_exe.py`) shows the 17:31 install and the
12:45 backup both carry all three markers 3/3, while the Aug 30 backup carries only
`populate step` (1/3), which is the build the old note was measuring. And the bridge has
**not** held the GATT service since 08:40 — it stopped at 17:00.

Verified: three new tests in `tests/test_ble_transport_contract.py`, and reverting the
recovery to the pre-change branch makes the first one fail with the **verbatim
production error** — `ConnectionError: ATVV voice service not found on this device
(UNKNOWN(1) (1), 0 services)` (`mutate_service_recovery.py`). Full suite:
`Ran 1138 tests ... OK (skipped=8)`.

*Still open, and now sharply defined: (a) **why** the filtered query stalls 7.7 s in the
first place — round 3 removes the consequence of the stall, not the stall; and (b) the
structural fix for the double consumer, which is the "merge the bridge into the app
process" step this document already argues for, since one process cannot collide with
itself. The recovery does nothing for the contention case either: when the device is
held elsewhere, the characteristic read is refused before the service step can matter.
The one question only a live run can settle is whether the *recovered* service object is
usable for that characteristic read.*

### 7. Add log rotation — **done 2026-09-16**

`get_logger()` wrote through a plain `logging.FileHandler`, so the app had one
unbounded file. Sized from measurement rather than taste:

| | measured |
|---|---|
| before the item-3 flood fix | **140,096,907 bytes (~954,000 lines) in ~14 hours** (~10 MB/hour) |
| after it (14:00→17:00) | **15,512 bytes in 3 hours (~5.2 KB/hour, ~125 KB/day)** |

Policy: **2 MiB per file, 5 backups** — the whole log stays inside **12 MiB**, a
~12x cut from the measured 140 MB. That is ~16 days of ordinary operation per
file, or ~12 minutes of a returned flood; five backups hold ~98 days or ~72
minutes respectively. `app.log` is still the live file, so the "view log" button
is unaffected, and the older segments sit beside it as `app.log.1`…`.5`.

The handler is deliberately **not** the stock `RotatingFileHandler`. Its
`doRollover` closes the stream, renames, and reopens inside one call that `emit`
wraps in `except Exception: handleError(...)` — and on Windows that rename fails
with `PermissionError` whenever anything else holds the file open, which here is
the *normal* case: the settings window's own "view log" button hands `app.log`
straight to Notepad. Measured on the identical scenario (512-byte cap, rename
forced to raise):

| handler | records written | file afterwards |
|---|---|---|
| stock `RotatingFileHandler` | **8 of 40**, then **0 of 10** | frozen at 456 bytes |
| `_ResilientRotatingFileHandler` | 40 of 40, then 10 of 10 | grew to 2890 bytes |

So the stock handler does not stop logging outright — `shouldRollover` reopens
the abandoned stream — but it drops every record that would cross the cap and
freezes just under it: 80% of the records here, and 100% of everything after.
And `handleError` reports each drop on stderr, which a windowed
(`console=False`) build does not have. This log is the only diagnostic channel
for the Frida HID tap and the BLE reconnect path, so a log that quietly freezes
at its cap is the outcome worth designing against.
`_ResilientRotatingFileHandler.doRollover` swallows the rename failure and
reopens unconditionally, degrading to "the file keeps growing past the cap" —
the pre-rotation behaviour, which is a state we already know how to read.

Tests: `tests/test_logging_rotation.py`, 8 tests, mutation-verified — reverting
`doRollover` to the stock implementation fails 2 of them, and making the rename
chain a no-op fails the other 2. The mutation run is also what revealed that the
"previous contents survive" test depends on the subclass too, which the first
draft of the expectations had missed.

**Confirmed in production, not just in the tests.** The prediction above was that the
existing 140 MB `app.log` would become `app.log.1` on the first rotation. That is
exactly what happened the first time the new build wrote a record — the oversized file
was rotated out whole and a fresh `app.log` started, with the app's own startup
sequence as its first lines. So the rotation path, including the rename of a 140 MB
file, is now observed working rather than merely asserted.

**One consequence worth knowing:** `app.log.1` is that 140 MB file, and it is aged out
after five more rotations. Delete it by hand if you want the space back now.

**And one measured cost of using `logging.handlers` at all.** Diffing the frozen
`PYZ` module lists of the 17:31 build against this one, the change adds exactly five
modules and ~232 KB to the exe:

```
+ logging.handlers
+ smtplib
+ win32con
+ win32evtlogutil
+ winerror
```

Four of those five are `logging.handlers`' own dependencies — `SMTPHandler` pulls in
`smtplib`, `NTEventLogHandler` pulls in the pywin32 event-log trio — none of which
`RotatingFileHandler` uses. It is dead weight in a 5.5 MB binary and it is not worth
avoiding (hand-rolling rotation is exactly the code that needed the 8 tests above),
but the delta is now accounted for rather than assumed.

### 8. `bridgeRunning` could never be true — **fixed**
`qt_settings_app.py` read `handle.process.is_alive()`. `handle` is a `BridgeHandle`,
which owns the liveness test (`bridge_launcher.py:180` — `Popen.poll() is None`); the raw
`Popen` has no `is_alive`. So the property raised `AttributeError` on every read and the
UI could never report the bridge as running. Now `handle.is_alive()`.
*Found by running from source with stderr visible — the packaged build swallows it.*

### 9. `_refresh_start_with_windows_status` called a method that does not exist — **fixed**
It called `self._tray_controller.set_shortcut_active(new_state)`. A repo-wide grep finds
that method **nowhere** — only the call site and its own error string. The tray's
canonical menu is Open / Help / About / Exit, so there is no startup-folder item to sync.
The call raised `AttributeError` into a broad `except` on every status refresh, silently.
Removed, with a note explaining why; and `docs/tray-autostart-handoff.md` — which
documented the method as if it existed — is corrected.

*Why these two matter beyond themselves: neither had a single test. Both now have
regression cover that fails when the bug is reintroduced (verified by mutation — 2 tests
error on the `is_alive` revert, 1 fails on the tray revert).*

---

## What is working, measured

So the fixes above do not break it:

- **Voice capture works.** Sessions at 10:08:16 and 10:11:57, both `result=signal`
  (`frames=229 samples=54960` and `frames=776 samples=186240`).
- **Frida HID TAP works.** `RC003 HID TAP volume_up=down` at 10:02:41, followed by
  `arm key edge: vk=0xAF ... thread=rc003-hidogatt-report-tap`.
- **F5 suppression works** — see item 5.
- One thing to look at separately: several voice frames report `peak=32768`, which is out
  of range for signed 16-bit (max 32767) and indicates the input is saturating.
  `clipped=3.750%` on the first frame of the 10:08 session. Not obviously breaking
  transcription, but the gain staging is hot.

---

## "Make it one app" — decision: **merge, but in this order**

**Revised. My earlier advice was "keep two processes"; that was wrong.** The original
argument was that two separately-launched elevated processes cannot be relied on to share
a session. Your transcript then showed they *do* share one when the UI spawns the bridge
from an elevated parent — both `SessionId = 1`, the child inheriting the token. So the
merge is **not** required for correctness of sessions.

It is still the right call, on narrower grounds: the launch race that
`launch-rmic-elevated.ps1:18-23` papers over with `Stop-Process` + `Start-Sleep 2` +
`Start-Sleep 3` (its own comment: *"so we don't pile up duplicates that race for port
30684 / Frida TAP ownership"*), and the silent-failure class where a UI started
**unelevated** yields an unelevated bridge, Frida cannot attach, and the 7 keys die with
no visible error. One process deletes that whole class instead of working around it.

### What I verified about the coupling

- **Port 30684 is not UI↔bridge.** `frida_compat.py:231-234` binds a loopback TCP server
  and the Frida agent injected into `WUDFHost.exe` connects to it
  (`frida_hid_tap_runtime.py:56`, `host = "127.0.0.1"`). It is bridge↔Frida-agent.
- **`doubao_rpc` is a named pipe to Doubao**, not to the UI:
  `\\.\pipe\ObricIme\oime-server`.
- **The UI and the bridge barely talk at all** — only launch and stop, via
  `BridgeHandle` (`_bridge_handle_global`, `register_bridge_handle`,
  `take_bridge_handle`).

So the split buys very little coupling-wise, while costing a whole class of launch-order
and token-inheritance bugs. That tips the balance.

### Why merging wins

1. **It makes the constraint structural instead of conditional.** One process cannot be in
   two sessions or two integrity levels. The failure mode disappears rather than being
   avoided by getting the launch sequence right.
2. **One elevation, once.** One exe at logon from the scheduled task. No second elevation
   prompt, and no Session-0-vs-interactive question for a second process.
3. **It deletes the dead switch.** `bridgeAutoStart` exists *only* because of the split.
   Merge, and the concept stops existing.
4. **It deletes the lifecycle code** — `BridgeHandle`, `stop_bridge`, the module-global
   handle, and the orphaned-process handling around them.
5. **`-Verb RunAs` stops mattering.** `launch-rmic-elevated.ps1:11-14` documents that it
   silently fails in a scheduled-task context (no desktop for the consent prompt). With one
   process there is no second process to elevate.

### What it actually costs — and how much of that is real

| Cost | Verdict |
| --- | --- |
| Lose the hard-kill bound on a hung WinRT call | **Mostly evaporates.** That is `--diagnose-ble-candidates`, an *on-demand* diagnostic launched from the settings page — not the bridge, and not on the logon path. It can **stay** a separate child process. |
| A GUI crash takes BLE/HID/audio with it | **Real, but narrower than it looks.** Closing the window to tray still works (the process lives on); only a genuine crash differs. Keep the audio/BLE work off the GUI thread — the bridge already runs a dedicated worker thread. |
| Qt event loop + asyncio loop in one process | **Real work, well-trodden.** Run the asyncio loop on a dedicated thread, or use `qasync`. |

### Do it in two steps, in this order

1. **First: the scheduled task launches the UI elevated.** Already built and tested
   (32/32, mutation-checked). This alone fixes autostart — the UI starts elevated, the
   bridge it spawns inherits the token, the 7 keys work after a reboot.
2. **Then: merge the bridge into the app process**, delete the child-process launch path,
   the `BridgeHandle` lifecycle, and the dead `bridgeAutoStart` switch.

The order matters because step 1 is done and low-risk, so you get working autostart
immediately; the merge is the durable fix that removes the failure class. Doing the merge
first means debugging the merge and the autostart at the same time.

## Not proposed

- Anything that changes the 7-key mapping, the ATVV protocol handling, or the Frida
  injection itself. Those work; the problems are autostart, logging and the trigger race.
