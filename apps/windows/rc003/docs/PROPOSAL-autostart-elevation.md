# Proposal: autostart-with-elevation, logging performance, and the connect flake

Status: proposed, not implemented. Working tree already carries the round-2
connect diagnostic (see "Debug" below).

> **Correction, 2026-09-16 — the premise of this document is wrong.**
>
> This proposal assumes the dead Vol+ / Vol- / Back keys were caused by the app
> running unelevated, and that elevation is the fix. That is not what the
> measurements show. The keys were dead because of a **privilege-ordering bug**
> in `frida_hid_tap_injector.inject_current_process`: it called
> `_target_process_name()` — which opens the target — *before*
> `enable_debug_privilege()`. The guard therefore raised `WinError 5` first, so
> the privilege was never enabled, so every retry failed identically forever.
> With the two lines reordered, injection succeeds from a normal elevated
> process. See `IMPROVEMENTS.md`, "The one thing".
>
> Two specific claims below do not survive measurement:
>
> - *"`-RunLevel Highest` gives the process ... `SeDebug` with no prompt"*
>   (below, "Why a scheduled task"). Measured: a fresh elevated token has
>   SeDebugPrivilege **present but disabled** (`present=True enabled=False`).
>   RunLevel Highest supplies the Administrators group and High integrity; it
>   does not hand you a usable SeDebugPrivilege. Assuming it did is exactly what
>   made the ordering bug look like an elevation problem. (Not measured for a
>   *task*-launched token specifically — only for one elevated through the shell's *Run as administrator* verb.)
> - *"Frida cannot attach without an admin token"* (below, "The one thing").
>   Half right: an admin token is necessary, but it is not sufficient, and it was
>   never what was missing.
>
> Elevation is **still required** — a plain `python.exe` gets a filtered token,
> so the app must run elevated for the tap to attach at all. The scheduled-task
> work is still worth doing so elevation survives a reboot. It is just no longer
> load-bearing for the keys, and it must not be sold as the fix for them.

> **Status update, 2026-09-16 — the scheduled task is now registered, and item 5
> below is based on a false premise.**
>
> Done:
>
> - `RemoteMicRC003-Elevated` is registered and verified: trigger
>   `MSFT_TaskLogonTrigger`, principal `absolut` with `RunLevel=Highest`, action
>   `%LOCALAPPDATA%\RemoteMic\RC003\RemoteMicRC003.exe`. `elevated_autostart.query_state()`
>   reports `RUNNING`, `is_registered()=True`, `is_enabled()=True`, so the future
>   UI toggle will read the real state.
> - The installed exe was rebuilt from the work copy so it actually contains the
>   privilege-ordering fix. Verified by inspecting the PyInstaller xref: exactly
>   one frozen `frida_hid_tap_injector.py`, and its path is
>   `windows-remote-mic-app-work`. The previous install (2026-08-30) predated the
>   fix and had dead Vol+/Vol-/Back. The prior build is preserved as
>   `RemoteMicRC003.exe.bak-20260916-124615` + `_internal.bak-20260916-124615`.
> - Functional proof from the installed binary: `RC003 HID TAP ATTACHED
>   pid=2964`, zero `injection retry` lines after the attach.
>
> Corrected — **item 5 ("stop launching the scheduled task at DEBUG") does not
> describe a real problem.** `OVB_RC003_LOG_LEVEL` and `OVB_RC003_PROFILE` are
> read by **nothing** in this repository: grep finds them only in docs and in
> launcher `.cmd`/`.py` files, never in `src/`. `logging_setup.py:48` hardcodes
> `logger.setLevel(logging.INFO)`. So `launch-rmic-elevated.ps1:17` setting
> `DEBUG` has never had any effect, and it is not a cause of the log bloat
> described below. The registered task also no longer goes through that script
> at all (the action is the exe directly), so the question is moot. The log
> volume needs the fix described under "Performance" instead.
>
> Still open: items 2 (repoint the in-app toggle at the task), 3 (make the
> Startup-folder path and the task mutually exclusive) and 4 (the dead
> `bridgeAutoStart` switch).

---

## The one thing

**Make "start with Windows" mean "register and verify the elevated scheduled
task", and delete the Startup-folder shortcut path.**

Everything else in this document is secondary to that sentence. The reason is
not stylistic: the in-app toggle currently writes a per-user `.lnk` into the
Startup folder, which launches the app **unelevated**. Frida cannot attach
without an admin token, so turning the existing toggle *on* produces a bridge
where Vol+ / Vol- / Back are dead. The toggle is therefore wired to the wrong
mechanism, and the correct mechanism already exists in the repo and is proven.

---

## Why a scheduled task, and not a UAC prompt

The ask was "prompt administrator access because of Frida". A UAC prompt is the
wrong instrument here, and the repo already documents why:

- `launch-rmic-elevated.ps1` lines 11-14: *"do NOT add `-Verb RunAs` here. The
  parent PowerShell already runs elevated (`RunLevel=Highest` in the Scheduled
  Task principal). `-Verb RunAs` would invoke the UAC consent prompt, which has
  no visible desktop in the scheduled task context, so the EXE would silently
  never start."*
- A Startup-folder shortcut cannot elevate at all. Windows will not let a logon
  shortcut raise its own token, and a UAC prompt at logon is a bad experience
  anyway.
- `New-ScheduledTaskPrincipal -RunLevel Highest` gives the process the
  Administrators group and `SeDebug` **with no prompt**, because the Task
  Scheduler service launches it. That is the supported path, and
  `%USERPROFILE%\register-rmic-autostart.ps1` already implements it
  (`RemoteMicRC003-Elevated`, trigger `AtLogOn`, `RunLevel Highest`,
  restart-on-failure).

So: no UAC prompt is needed. It is needed only if we keep the Startup-folder
mechanism, which we should not.

## What is actually wired today (measured)

| Mechanism | Where | Elevates? | State |
| --- | --- | --- | --- |
| Startup-folder `.lnk` | `startup_shortcut.py`, driven by the General page's "start with Windows" switch | **No** | Toggle OFF — Startup folder contains only `desktop.ini` |
| Elevated scheduled task | `register-rmic-autostart.ps1` + `launch-rmic-elevated.ps1` | Yes | Registration **unverified** — see caveat below |
| "Bridge autostart" switch | `GeneralPage.qml` lines 184-209 | n/a | **Dead.** Binds `SettingsController.bridgeAutoStart*`, which have no `Property` declaration in `qt_settings_app.py` — grep finds `bridgeAutoStart` only in QML. Qt renders it with empty bindings and toggling does nothing. |

The two live mechanisms are also **mutually unaware**. If both end up enabled you
get an unelevated instance and an elevated instance at logon, racing for port
30684 and Frida TAP ownership — the exact failure `launch-rmic-elevated.ps1`
lines 18-20 already defends against by killing stale processes.

Caveat: I could not query the scheduled task from this session. `schtasks.exe`
and `reg.exe` are both blocked by the sandbox's program blacklist, and the
PowerShell tool returned no output on three attempts. **Please confirm whether
`RemoteMicRC003-Elevated` is currently registered** before we build on it.

## Work

1. **Reinstate `elevated_autostart.py`.** It existed on 2026-08-26 (242 lines,
   per `.hermes/handoffs/2026-08-26-elevated-autostart-toggle.md`) and was lost
   in the `7f424d6` revert. Wrap
   `Get-ScheduledTask` / `Enable-ScheduledTask` / `Disable-ScheduledTask` for
   `RemoteMicRC003-Elevated`, exposing `is_registered()` / `is_enabled()` /
   `set_enabled()` / `get_state_dict()`.
2. **Repoint the "start with Windows" toggle at it.** Read path and write path
   both go through the scheduled task. Status text distinguishes
   "not registered" (needs the one-time registration) from
   "registered, disabled" from "registered, enabled".
3. **Make the two mechanisms mutually exclusive.** `startup_shortcut.enable()`
   must refuse (or self-disable) when the elevated task is registered, and vice
   versa. Today nothing prevents both.
4. **Fix or remove the dead `bridgeAutoStart` switch.** Add the missing
   `Property` declarations, or delete the card. A control that silently does
   nothing is worse than no control.
5. **Stop launching the scheduled task at `DEBUG`.** `launch-rmic-elevated.ps1`
   line 17 sets `$env:OVB_RC003_LOG_LEVEL = "DEBUG"` — and that script is what
   the scheduled task runs at every logon. Set it to `INFO`.

---

## Performance: one logger is 90.7% of a 134 MB log

Measured on `%LOCALAPPDATA%\RemoteMic\RC003\logs\app.log`:

```
size            134 MB
lines           950,665
hook msg lines  616,169
by logger:
  862,415  ovb_rc003.legacy_key_suppressor_windows   (90.7%)
   60,421  ovb_rc003
    1,431  ovb_rc003.frida_compat
      813  ovb_rc003.ble_transport_winrt
```

The source of that volume is self-labelled as temporary —
`legacy_key_suppressor_windows.py` lines 505-510:

> *"TEMPORARY DIAGNOSTIC (RC003 keys-not-firing investigation): log every message
> that the low-level keyboard hook sees ... After we identify the right message
> type, this branch gets replaced by a focused handler that forwards to
> `on_key_observe`."*

That investigation is over: Vol+ / Vol- / Back are carried by Frida TAP, not by
this hook. The branch is dead weight, and it is **on the hot path** — it runs
inside the `WH_KEYBOARD_LL` callback, calling `KBDLLHOOKSTRUCT.from_address` and
a synchronous formatted `_logger.info` for every key event before
`CallNextHookEx`. Windows enforces `LowLevelHooksTimeout` (300 ms default); a
hook callback that overruns it is silently dropped, which shows up as keys that
stop firing. This is a plausible contributor to the very symptom the diagnostic
was added to chase.

Fix: delete the branch, or gate it behind an explicit debug flag that is off by
default. Expected effect: ~90% less log I/O on the input path, 134 MB → ~12 MB
per three weeks. Separately, add log rotation — a single unbounded 134 MB file
is not a logging policy.

---

## Debug: the connect flake was instrumented on the wrong step

This is the substantive finding, and it is now fixed in the working tree.

Commit `ad909b4` added a diagnostic that logs the **populate** step's
`GattCommunicationStatus`. The production log answers it:

```
809  populate step status=SUCCESS
810  services_count=9
```

**809 out of 809 populate attempts succeeded**, always with exactly 9 services.
Populate is not the flaky step. Meanwhile connect still failed 792 times with
`ATVV voice service not found` and 18 times with `ATVV characteristic not found`.
The status of the steps that actually failed was never recorded, so three weeks
of logs could not name the condition.

Measured directly against the live remote
(`小米蓝牙语音遥控器`, (controller and host addresses redacted)):

- The device's GATT table **does** contain the ATVV service
  `ab5e0001-5a21-4f05-bc7d-af01f617b664` with all three characteristics
  (`ab5e0002` TX, `ab5e0003` audio, `ab5e0004` control).
- Full populate: `SUCCESS (0)`, 9 services — reproducible.
- The UUID-filtered service query: `SUCCESS (0)`, 1 service — sometimes.
- The **per-service characteristic query** is the failing step, and it returns
  `GattCommunicationStatus.ACCESS_DENIED (3)` with an empty list.
- Running the bridge's own `connect()` from source reproduces the flake
  (3 of 5 attempts succeeded, 2 failed on the characteristic step) — so the
  deployed exe is not the problem.

`ACCESS_DENIED` on a characteristic query means the service is held by someone
else. That is consistent with what I observed: the first probe succeeded while
the bridge was still failing to connect, and every probe after the bridge
connected at 08:40 got `ACCESS_DENIED` on all four service/query combinations.
So the condition is contention for exclusive GATT ownership — but whether the
app's *own* retry loop is self-contending (a session not released before the
next attempt) is **not yet established**. Settling it needs one clean run with
the bridge stopped, which I did not do because it would have interrupted the
live voice session in progress.

### What I changed

`src/ovb_rc003/ble_transport_winrt.py`:

- Added `_status_name(winrt, status)` — resolves the enum to
  `SUCCESS` / `UNREACHABLE` / `PROTOCOL_ERROR` / `ACCESS_DENIED`, falling back to
  `UNKNOWN(<int>)` so the integer always survives.
- The **service** step and the **characteristic** step now log status, counts,
  the wanted UUID, and the UUIDs actually returned.
- The raised errors name the status instead of the bare
  `ATVV characteristic not found` that sent round 1 looking in the wrong place.

`tests/test_ble_transport_contract.py`: three new tests —
the characteristic step logs a status on every connect; a denied lookup logs
`UNKNOWN(3)` plus `characteristics_count=0` and names the status in the error;
and `_status_name` resolves the symbolic names against a callable enum shaped
like the real winrt projection (the in-repo fake cannot exercise that branch).
**32/32 green** (was 29).

Verified live — the same failure now names itself:

```
populate step status=SUCCESS (0) services_count=9
service step status=SUCCESS (0) services_count=1 wanted=ab5e0001-...
characteristic step status=ACCESS_DENIED (3) characteristics_count=0
    wanted=ab5e0002-... found=[]
```

Nothing is committed; the tree also still carries the pre-existing uncommitted
`requirements*.txt` bumps and the `_set_start_with_windows` guard removal.

---

## Questions that are genuinely yours

1. **Is `RemoteMicRC003-Elevated` currently registered?** I could not check it
   from this session.
2. **Should the two autostart mechanisms collapse into one control?** See
   "One process, two processes" below for why the process topology is not the
   question — the question is only whether the General page keeps a separate
   "bridge autostart" switch.
3. **Kill the `bridgeAutoStart` card, or give it the missing properties?** Two
   switches that both claim to control startup is a support trap; I would delete
   it, but that is a product call.

---

## One process, two processes — **superseded, see the note at the top of this section**

> **Correction (later same day).** This section originally argued that the two-process
> topology should be left alone. Rick supplied the fact that decides it: when the UI and
> the bridge both have to be elevated, they must land in the **same session at the same
> integrity level**, and two separately-launched elevated processes do not reliably do
> that. So the topology *should* change — the bridge should be merged into the app
> process. Full reasoning and the cost table are in
> [`IMPROVEMENTS.md`](IMPROVEMENTS.md), section "Make it one app".
>
> **What still holds from this section:** the entry-point change described below is
> correct and is step 1 of the two-step sequence. Do it first — it is built and tested,
> and it fixes autostart on its own. The merge is step 2, and it is what removes the
> failure class rather than working around it.

### What the entry-point change does and does not touch

Even after the merge, the chain that has to be elevated is the same three logical steps;
only the *number of processes* changes.

| | Today | Step 1 (this proposal) | Step 2 (merge) |
| --- | --- | --- | --- |
| Step 1 | You open an administrator PowerShell and run `launch-rmic-elevated.ps1` | Task Scheduler fires at logon, `RunLevel Highest` | unchanged |
| Step 2 | settings exe starts (elevated) | settings exe starts (elevated) | the app process starts (elevated) |
| Step 3 | bridge child starts (inherits elevated token) | bridge child starts (inherits elevated token) | **gone** — the bridge runs inside step 2 |
| Frida | attaches — Vol+/Vol-/Back work | attaches — Vol+/Vol-/Back work | attaches — Vol+/Vol-/Back work |

The failure this proposal fixes is in step 1: the Startup-folder shortcut starts the
settings exe **unelevated**, so step 3's child is unelevated too, Frida cannot attach, and
those three keys are dead.

The elevated task should start **the settings UI** (not a bridge mode), because the
settings UI owns the bridge lifetime — it launches it, holds the `BridgeHandle`, and stops
it on close. Starting a bridge separately as well would create the competing second
instance that `launch-rmic-elevated.ps1:18-20` already defends against by killing stale
processes. After the merge in step 2 this distinction stops existing: there is one
process, and the task starts it.
