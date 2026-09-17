"""Enable / disable the elevated at-logon Scheduled Task for Remote Mic RC003.

Why this module exists
----------------------

The RC003's consumer-page keys - Vol+, Vol-, Back (the three the Frida HID TAP
carries; the other four buttons arrive through the normal keyboard path) - are
delivered by Frida, because Windows never surfaces those HID reports through the
ordinary input chain. Frida recovers them by attaching to ``WUDFHost.exe``, and
``OpenProcess`` on ``WUDFHost.exe`` requires the Administrators group plus
``SeDebug``. Without an admin token the attach fails and those keys are dead.

The Startup-folder shortcut mechanism in ``startup_shortcut.py`` **cannot**
elevate - Windows will not let a logon shortcut raise its own token, and a UAC
prompt at logon is not an acceptable substitute. So a Startup-folder shortcut
produces a bridge where Vol+/Vol-/Back do not work, which is exactly the
configuration this module exists to avoid.

A Scheduled Task with ``RunLevel Highest`` is launched by the Task Scheduler
service, so it starts elevated with **no UAC prompt**. That is the supported way
to automate what the user otherwise does by hand: opening an administrator
PowerShell window and starting the app from there.

Registration needs elevation once
---------------------------------

``Register-ScheduledTask`` with a Highest-privilege principal itself requires an
admin token, so the *first* registration needs one elevated launch (or the
installer, which already runs elevated). After that the task is persistent and
the toggle only needs Enable/Disable, which a normal-privilege process can do
for its own user's task. ``register()`` therefore reports the access-denied case
as a ``FAILED`` result carrying a message that says to run once as
administrator, rather than raising.

Design notes
------------

* Zero third-party dependencies - ``subprocess`` to PowerShell, nothing else.
  Same approach as ``startup_shortcut.py``.
* Every OS-facing call is funnelled through one injectable ``runner`` callable so
  the unit tests can substitute a fake without touching Windows. See
  ``tests/test_elevated_autostart.py``.
* The public API never raises: each function returns a result object (or a bool
  for the predicates) so the settings UI never has to handle an exception from
  here. Mirrors ``startup_shortcut.py``'s contract.
* PowerShell is invoked with ``-NoProfile -NonInteractive -ExecutionPolicy
  Bypass`` and ``CREATE_NO_WINDOW`` so toggling the switch does not flash a
  console window.
"""

from __future__ import annotations

import enum
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Name of the Scheduled Task. Kept identical to the one created by the
# historical manual registration script in the developer's home directory, so
# an existing registration keeps working and there is never more than one task
# competing for the device.
TASK_NAME = "RemoteMicRC003-Elevated"

_POWERSHELL = "powershell.exe"
_POWERSHELL_TIMEOUT_S = 30.0

# Task Scheduler's own state for a registered task. "Disabled" is what a
# Disable-ScheduledTask leaves behind; "Ready" is a task that is registered and
# will run at its trigger.
_STATE_ABSENT = "ABSENT"
_STATE_DISABLED = "DISABLED"
_STATE_UNKNOWN = "UNKNOWN"

# Exit code PowerShell returns when the caller lacks the rights to register a
# task for another user / with a highest-privilege principal.
_ACCESS_DENIED_MARKERS = (
    "access is denied",
    "拒绝访问",
    "accessdenied",
    "0x80070005",
)


class ElevatedAutostartOutcome(str, enum.Enum):
    """Coarse-grained result of a register/enable/disable call.

    Every value has a corresponding user-visible message in Simplified Chinese,
    matching ``startup_shortcut.StartupShortcutOutcome`` and the rest of the app.
    """

    ENABLED = "enabled"
    ALREADY_ENABLED = "already_enabled"
    DISABLED = "disabled"
    ALREADY_DISABLED = "already_disabled"
    REGISTERED = "registered"
    UNREGISTERED = "unregistered"
    NOT_REGISTERED = "not_registered"
    FAILED = "failed"


@dataclass(frozen=True)
class ElevatedAutostartResult:
    """Outcome + human-readable message + target path (when applicable)."""

    outcome: ElevatedAutostartOutcome
    message: str
    target_path: Optional[str] = None


# ``runner(script, operation) -> stdout``. Raises OSError on any failure
# (non-zero exit, timeout, missing powershell.exe).
PowerShellRunner = Callable[[str, str], str]


# ---------------------------------------------------------------------------
# Default implementation of the OS-facing callable
# ---------------------------------------------------------------------------


def _default_runner(script: str, operation: str) -> str:
    """Run ``script`` under PowerShell and return stdout.

    Raises ``OSError`` on a non-zero exit code, a timeout, or a missing
    ``powershell.exe``. Callers in this module catch that and convert it into a
    ``FAILED`` result - this function is the only place that touches the OS.
    """

    args = [
        _POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-Command", script,
    ]
    creationflags = 0
    if sys.platform == "win32":
        # CREATE_NO_WINDOW (0x08000000) so toggling the switch does not flash a
        # console window. Same flag and rationale as startup_shortcut.py.
        creationflags = 0x08000000
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_POWERSHELL_TIMEOUT_S,
            creationflags=creationflags,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError(
            f"powershell ({operation}) timed out after {_POWERSHELL_TIMEOUT_S}s"
        ) from exc
    except FileNotFoundError as exc:
        raise OSError(
            f"powershell executable not found ({_POWERSHELL})"
        ) from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise OSError(
            f"powershell ({operation}) exit={completed.returncode}: {stderr[:200]}"
        )
    return completed.stdout or ""


def _ps_quote(value: str) -> str:
    """Escape ``value`` for embedding inside a single-quoted PowerShell string.

    PowerShell escapes a literal single quote by doubling it.
    """

    return value.replace("'", "''")


def _looks_like_access_denied(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _ACCESS_DENIED_MARKERS)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def query_state(*, runner: PowerShellRunner = _default_runner) -> str:
    """Return the task's raw state as an upper-case token.

    One of ``ABSENT``, ``DISABLED``, ``READY``, ``RUNNING``, or ``UNKNOWN``.
    Never raises - any failure is reported as ``UNKNOWN`` so callers can treat
    "could not tell" differently from "definitely not registered".
    """

    script = (
        f"$t = Get-ScheduledTask -TaskName '{_ps_quote(TASK_NAME)}' "
        f"-ErrorAction SilentlyContinue; "
        f"if ($null -eq $t) {{ 'ABSENT' }} "
        f"else {{ $t.State.ToString().ToUpperInvariant() }}"
    )
    try:
        raw = runner(script, "query_state")
    except OSError as exc:
        logger.info("elevated_autostart: query_state failed: %s", exc)
        return _STATE_UNKNOWN
    token = (raw or "").strip().splitlines()
    if not token:
        return _STATE_UNKNOWN
    value = token[-1].strip().upper()
    return value or _STATE_UNKNOWN


def is_registered(*, runner: PowerShellRunner = _default_runner) -> bool:
    """True iff the elevated task exists.

    ``UNKNOWN`` (PowerShell unavailable, or the query failed) returns False so
    the UI shows the switch OFF - the safe default, and the same fail-closed
    choice ``startup_shortcut.is_enabled`` makes.
    """

    return query_state(runner=runner) not in (_STATE_ABSENT, _STATE_UNKNOWN)


def is_enabled(*, runner: PowerShellRunner = _default_runner) -> bool:
    """True iff the task is registered and not disabled.

    A task in ``READY`` or ``RUNNING`` state will fire at logon; ``DISABLED``
    will not; ``ABSENT`` has nothing to fire.
    """

    state = query_state(runner=runner)
    return state not in (_STATE_ABSENT, _STATE_DISABLED, _STATE_UNKNOWN)


def get_state_dict(*, runner: PowerShellRunner = _default_runner) -> dict:
    """Snapshot for the settings UI: registration + enablement + status text.

    Kept as a single call so the UI does not race two separate PowerShell
    invocations against each other while the user watches the switch.
    """

    state = query_state(runner=runner)
    registered = state not in (_STATE_ABSENT, _STATE_UNKNOWN)
    enabled = state not in (_STATE_ABSENT, _STATE_DISABLED, _STATE_UNKNOWN)

    if state == _STATE_UNKNOWN:
        status_text = "无法读取计划任务状态。请以管理员身份运行一次以完成注册。"
        is_error = True
    elif state == _STATE_ABSENT:
        status_text = "未注册计划任务：开机自启动尚未注册。请以管理员身份运行一次以完成注册。"
        is_error = False
    elif state == _STATE_DISABLED:
        status_text = "已注册，但当前已关闭。开启后下次登录会以管理员权限自动启动。"
        is_error = False
    else:
        status_text = "已启用：下次登录会以管理员权限自动启动，Frida 可以注入，音量/返回键可用。"
        is_error = False

    return {
        "registered": registered,
        "enabled": enabled,
        "state": state,
        "status_text": status_text,
        "is_error": is_error,
    }


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def set_enabled(
    value: bool, *, runner: PowerShellRunner = _default_runner
) -> ElevatedAutostartResult:
    """Enable or disable the already-registered task.

    Idempotent: enabling an enabled task (or disabling a disabled one) returns
    the corresponding ALREADY_* outcome rather than calling PowerShell again.
    """

    target = bool(value)
    state = query_state(runner=runner)

    if state == _STATE_ABSENT:
        return ElevatedAutostartResult(
            outcome=ElevatedAutostartOutcome.NOT_REGISTERED,
            message=(
                "未注册计划任务，无法切换开机自启动。"
                "请先以管理员身份运行一次完成注册。"
            ),
        )
    if state == _STATE_UNKNOWN:
        return ElevatedAutostartResult(
            outcome=ElevatedAutostartOutcome.FAILED,
            message="无法读取计划任务状态，开机自启动未改变。",
        )

    if target and state in ("READY", "RUNNING"):
        return ElevatedAutostartResult(
            outcome=ElevatedAutostartOutcome.ALREADY_ENABLED,
            message="开机自启动已启用（管理员权限）。",
        )
    if not target and state == _STATE_DISABLED:
        return ElevatedAutostartResult(
            outcome=ElevatedAutostartOutcome.ALREADY_DISABLED,
            message="开机自启动已关闭。",
        )

    cmdlet = "Enable-ScheduledTask" if target else "Disable-ScheduledTask"
    script = (
        f"{cmdlet} -TaskName '{_ps_quote(TASK_NAME)}' -ErrorAction Stop | Out-Null; "
        f"'OK'"
    )
    try:
        runner(script, cmdlet)
    except OSError as exc:
        return ElevatedAutostartResult(
            outcome=ElevatedAutostartOutcome.FAILED,
            message=f"切换开机自启动失败：{exc}",
        )

    return ElevatedAutostartResult(
        outcome=(
            ElevatedAutostartOutcome.ENABLED
            if target
            else ElevatedAutostartOutcome.DISABLED
        ),
        message=(
            "已启用开机自启动（管理员权限）。下次登录会自动启动。"
            if target
            else "已关闭开机自启动。"
        ),
    )


def register(
    target_exe: Path, *, runner: PowerShellRunner = _default_runner
) -> ElevatedAutostartResult:
    """Create (or replace) the at-logon elevated task for ``target_exe``.

    Needs an admin token: ``Register-ScheduledTask`` with a Highest-privilege
    principal is itself a privileged operation. When that is the reason for the
    failure the message says so explicitly, so the UI can tell the user "run
    once as administrator" instead of showing a raw PowerShell error.

    ``target_exe`` is the app executable. The task runs it directly rather than
    going through a helper script in the user's home directory, so an installed
    build is self-contained.
    """

    resolved = Path(target_exe).resolve()
    if not resolved.exists():
        return ElevatedAutostartResult(
            outcome=ElevatedAutostartOutcome.FAILED,
            message=f"注册失败：找不到可执行文件 {resolved}",
        )

    exe = _ps_quote(str(resolved))
    work_dir = _ps_quote(str(resolved.parent))
    task = _ps_quote(TASK_NAME)

    script = (
        f"$taskName = '{task}'; "
        # Drop any existing task first so re-registering cannot leave a stale
        # action pointing at an old install path.
        f"Unregister-ScheduledTask -TaskName $taskName -Confirm:$false "
        f"-ErrorAction SilentlyContinue; "
        f"$action = New-ScheduledTaskAction -Execute '{exe}' "
        f"-WorkingDirectory '{work_dir}'; "
        f"$trigger = New-ScheduledTaskTrigger -AtLogOn; "
        # RunLevel Highest is the whole point: the Task Scheduler service starts
        # the process with the Administrators token and SeDebug, which Frida
        # needs for OpenProcess(WUDFHost.exe) - and no UAC prompt is shown.
        f"$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME "
        f"-LogonType Interactive -RunLevel Highest; "
        f"$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        f"-DontStopIfGoingOnBatteries -StartWhenAvailable "
        f"-ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 3 "
        f"-RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances Queue; "
        f"Register-ScheduledTask -TaskName $taskName -Action $action "
        f"-Trigger $trigger -Principal $principal -Settings $settings "
        f"-Description 'Launch RemoteMicRC003 elevated at logon so Frida "
        f"can attach to WUDFHost.exe for the RC003 Vol+/Vol-/Back keys' "
        f"-Force | Out-Null; "
        f"'OK'"
    )
    try:
        runner(script, "register")
    except OSError as exc:
        text = str(exc)
        if _looks_like_access_denied(text):
            return ElevatedAutostartResult(
                outcome=ElevatedAutostartOutcome.FAILED,
                message=(
                    "注册失败：需要管理员权限。请以管理员身份运行一次本程序，"
                    "或使用安装程序完成注册。"
                ),
                target_path=str(resolved),
            )
        return ElevatedAutostartResult(
            outcome=ElevatedAutostartOutcome.FAILED,
            message=f"注册计划任务失败：{exc}",
            target_path=str(resolved),
        )

    return ElevatedAutostartResult(
        outcome=ElevatedAutostartOutcome.REGISTERED,
        message=(
            "已注册开机自启动计划任务（管理员权限）。"
            "下次登录会自动启动，Frida 可注入。"
        ),
        target_path=str(resolved),
    )


def unregister(*, runner: PowerShellRunner = _default_runner) -> ElevatedAutostartResult:
    """Remove the task entirely. Idempotent."""

    script = (
        f"Unregister-ScheduledTask -TaskName '{_ps_quote(TASK_NAME)}' "
        f"-Confirm:$false -ErrorAction Stop | Out-Null; 'OK'"
    )
    try:
        runner(script, "unregister")
    except OSError as exc:
        return ElevatedAutostartResult(
            outcome=ElevatedAutostartOutcome.FAILED,
            message=f"移除计划任务失败：{exc}",
        )
    return ElevatedAutostartResult(
        outcome=ElevatedAutostartOutcome.UNREGISTERED,
        message="已移除开机自启动计划任务。",
    )
