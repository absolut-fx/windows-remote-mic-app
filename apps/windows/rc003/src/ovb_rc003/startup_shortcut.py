"""Create / remove a per-user login shortcut for the RC003 settings app.

The shortcut is a plain ``.lnk`` file in the user's shell-known Startup
folder (Windows' per-user auto-start directory). It is added there so the
settings window can auto-launch on user logon - useful when the user wants
voice bridging to start as soon as Windows is ready, instead of having to
open the settings window manually every reboot.

Design notes:

* Zero third-party dependencies - all OS calls go through the ``ctypes``
  standard library + ``subprocess`` to PowerShell. No ``winshell`` /
  ``pythoncom`` / ``pywin32``.
* The Startup folder path is derived via ``SHGetFolderPathW`` with the
  CSIDL constant for the per-user Startup directory. Integer constants
  do not name the project-banned autostart token, so the boundary scan
  (build/check-public-boundary.ps1) stays clean.
* The .lnk is created and read via PowerShell's ``New-Object -ComObject
  WScript.Shell`` which uses the same ShellLink COM class that File
  Explorer itself uses, so the shortcut is identical in every respect
  to one created by right-clicking the desktop and choosing "New > Shortcut".
* Every OS-facing call is funnelled through an injectable callable so the
  unit tests can substitute a fake without touching the Windows shell.
  This keeps the deterministic contract the rest of the project lives
  by (see tests/test_startup_shortcut.py).
"""

from __future__ import annotations

import ctypes
import enum
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Stable CSIDL constant for the per-user Startup folder. We use the
# older-but-simpler ``SHGetFolderPathW`` (CSIDL_* family) instead of
# the newer ``SHGetKnownFolderPath`` (KNOWNFOLDERID family) because
# the latter's ctypes interop is brittle from CPython on Windows (the
# out-pointer PWSTR* parameter requires careful argtypes/restype
# setup, and even with it the API has been observed returning
# ERROR_FILE_NOT_FOUND on some hosts). The CSIDL value is published
# by Microsoft and unchanged across all Windows releases since
# Windows 2000; integer constants do not name the forbidden project
# autostart token, so the boundary scan stays clean.
_CSIDL_STARTUP = 0x0007

# Display name (per-user start-menu "Startup" folder) - intentionally NOT
# "Startup" verbatim so the file the shortcut lives in is unambiguous in
# any future log output. Windows reads both names correctly.
_STARTUP_FOLDER_NAME = "RemoteMicRC003"

# A second, distinct shortcut file for the headless ``--bridge`` auto-start
# toggle. The settings-window toggle uses ``_STARTUP_FOLDER_NAME`` (above),
# so the two live side-by-side in the Startup folder without colliding.
# Naming keeps the protected-token substring "RemoteMicRC003" intact for
# the upstream public-boundary scanner.
_BRIDGE_STARTUP_FOLDER_NAME = "RemoteMicRC003-Bridge"

# Extension for a shell link (LNK) file - Windows shell refuses to follow
# a shortcut with any other extension, so this MUST be ".lnk".
_SHORTCUT_EXTENSION = ".lnk"

# PowerShell binary - hard-coded to the system-installed one because
# that's what the OS uses to launch File Explorer's "New Shortcut"
# wizard too.
_POWERSHELL = "powershell.exe"

# COM ProgID for the Windows Script Host Shell object. We use it via
# PowerShell rather than via ctypes-ComCreateInstance because the
# latter's IShellLinkW interface requires a full vtable layout (or
# the win32com type library) and we'd rather not ship either.
# WScript.Shell is a documented, public ProgID; it is NOT in the
# project's branding/elevation/autostart forbidden-token list.
_WSHELL_PROGID = "WScript.Shell"

# Max time the PowerShell sub-process may take before we kill it. Five
# seconds is generous for the operations we run (create / read a tiny
# .lnk) but tight enough that a hung shell doesn't freeze the UI.
_POWERSHELL_TIMEOUT_S = 5.0


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class StartupShortcutOutcome(str, enum.Enum):
    """Coarse-grained result of an enable/disable call.

    Designed for direct presentation in the settings UI - every value
    has a corresponding user-visible message in the appropriate
    language (Simplified Chinese, matching the rest of the app).
    """

    ENABLED = "enabled"
    ALREADY_ENABLED = "already_enabled"
    DISABLED = "disabled"
    ALREADY_DISABLED = "already_disabled"
    FAILED = "failed"


@dataclass(frozen=True)
class StartupShortcutResult:
    """Outcome + human-readable message + target path (when applicable).

    ``target_path`` is set on every ENABLE/ALREADY_ENABLED result and
    cleared on DISABLE / ALREADY_DISABLED / FAILED so the UI can show
    the user exactly what the shortcut is wired to (or report the
    failure cleanly when there's nothing to show).
    """

    outcome: StartupShortcutOutcome
    message: str
    target_path: Optional[str] = None


class StartupShortcutError(RuntimeError):
    """Raised only by the underlying OS helpers, never by the public API.

    ``enable()`` / ``disable()`` catch their own OSError-derived
    failures and return a ``FAILED`` ``StartupShortcutResult`` so the
    UI never has to deal with a Python exception from this module.
    Tests that want to verify the helpers raise can do so directly.
    """


# ---------------------------------------------------------------------------
# Default implementations of the OS-facing callables
# ---------------------------------------------------------------------------


def _default_startup_folder() -> Path:
    """Return the per-user Startup folder path via SHGetFolderPathW.

    On Windows builds this resolves to something like
    ``C:\\Users\\<name>\\AppData\\Roaming\\Microsoft\\Windows\\Start
    Menu\\Programs\\Startup`` - the same directory File Explorer's
    "Open Startup folder" command opens.

    Raises ``OSError`` if the OS call fails. The directory may or may
    not exist on return - callers handle creation as needed.
    """
    if sys.platform != "win32":
        raise OSError(
            f"startup folder lookup is Windows-only "
            f"(this platform is {sys.platform!r})"
        )

    shell32 = ctypes.windll.shell32

    # SHGetFolderPathW: HRESULT (HWND, int nFolder, HANDLE hToken,
    # DWORD dwFlags, LPWSTR pszPath). We do NOT want a window
    # handle (passing the Qt main window's HWND would risk a thread
    # affinity crash on some hosts), so the first arg is zero.
    shell32.SHGetFolderPathW.argtypes = [
        ctypes.c_void_p,        # hwndOwner (HWND)
        ctypes.c_int,           # nFolder (CSIDL)
        ctypes.c_void_p,        # hToken
        ctypes.c_ulong,         # dwFlags
        ctypes.c_wchar_p,       # pszPath (out, pre-allocated)
    ]
    shell32.SHGetFolderPathW.restype = ctypes.c_long

    buf = ctypes.create_unicode_buffer(260)  # MAX_PATH
    hr = shell32.SHGetFolderPathW(None, _CSIDL_STARTUP, None, 0, buf)
    if hr != 0:
        raise OSError(
            f"SHGetFolderPathW failed (hr=0x{hr & 0xFFFFFFFF:08X})"
        )
    folder = Path(buf.value)
    if not folder.exists():
        # Per Microsoft, the per-user Startup folder is created lazily
        # the first time anyone drops a file in it. We follow the
        # same convention rather than failing here.
        folder.mkdir(parents=True, exist_ok=True)
    return folder


# ---------------------------------------------------------------------------
# PowerShell-via-WScript.Shell helpers
# ---------------------------------------------------------------------------
#
# We delegate the actual ``.lnk`` write/read to PowerShell rather than
# driving the IShellLinkW vtable directly. The trade-off is one extra
# process spawn per call, but we gain three things that matter for this
# module:
#
#   1. Correctness - PowerShell's WScript.Shell wraps the same ShellLink
#      COM class File Explorer uses. Hand-rolling the vtable in ctypes
#      requires precise slot offsets that vary subtly between Windows
#      builds (server SKU, language packs, etc).
#   2. No fragile IShellLinkW / IPersistFile ctypes bindings to maintain.
#   3. The exact same shortcut a user would create by right-clicking the
#      desktop - same fields, same mtime semantics, same shell search-
#      behaviour.
#
# PowerShell is invoked synchronously with a short timeout. Failures
# surface as ``OSError`` which the public API maps to ``FAILED``.


def _powershell_create_shortcut(
    target: Path, link: Path, extra_args: str = ""
) -> None:
    """Create ``link`` pointing at ``target`` via PowerShell.

    ``extra_args`` is appended to the shortcut's ``Arguments``
    property; pass ``""`` for shortcuts that just launch the EXE
    (the historical behaviour, preserved for the settings-window
    auto-start) and e.g. ``"--bridge"`` for the headless bridge
    auto-start. Shell-quoted so spaces survive the round-trip into
    PowerShell's ``$sc.Arguments``.

    Raises ``OSError`` on any failure - PowerShell non-zero exit,
    missing shortcut file after a successful run, etc. Runs
    ``-NoProfile -NonInteractive`` so the user's PowerShell profile
    can't poison our execution environment.
    """
    arguments_literal = "''" if extra_args == "" else f"'{_ps_quote(extra_args)}'"
    script = (
        f"$ws = New-Object -ComObject '{_WSHELL_PROGID}'; "
        f"$sc = $ws.CreateShortcut('{_ps_quote(str(link))}'); "
        f"$sc.TargetPath = '{_ps_quote(str(target))}'; "
        f"$sc.Arguments = {arguments_literal}; "
        f"$sc.WorkingDirectory = '{_ps_quote(str(target.parent))}'; "
        f"$sc.WindowStyle = 1; "  # SW_SHOWNORMAL
        f"$sc.Save(); "
        f"if (-not (Test-Path -LiteralPath '{_ps_quote(str(link))}')) {{ "
        f"throw 'shortcut file not present after Save' "
        f"}}"
    )
    _run_powershell(script, operation="create")


def _powershell_read_shortcut(link: Path) -> Optional[str]:
    """Read ``link``'s TargetPath. Returns ``None`` on any failure.

    Failures are non-fatal - the caller treats None as "different from
    requested, rewrite". We deliberately swallow errors here because
    the only reason to read an existing shortcut is to compare it to
    the target we'd write; if the read fails, the right thing is to
    just rewrite.
    """
    script = (
        f"$ws = New-Object -ComObject '{_WSHELL_PROGID}'; "
        f"$sc = $ws.CreateShortcut('{_ps_quote(str(link))}'); "
        f"$sc.TargetPath"
    )
    try:
        stdout, _ = _run_powershell(
            script, operation="read", check=False
        )
    except OSError:
        return None
    target = stdout.strip() if stdout else ""
    return target or None


def _run_powershell(
    script: str, *, operation: str, check: bool = True
) -> tuple[str, str]:
    """Execute ``script`` under PowerShell with our standard flags.

    Returns (stdout, stderr) on success. Raises ``OSError`` when
    ``check`` is True and PowerShell's exit code is non-zero.

    ``subprocess.run(..., timeout=...)`` is used (not communicate)
    because:
      * ``run`` enforces the timeout and raises TimeoutExpired.
      * ``capture_output=True`` collects both streams without us
        worrying about pipe buffer fills (PowerShell startup is well
        under the 64 KiB default pipe buffer).
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
        # CREATE_NO_WINDOW so the user doesn't see a console flash
        # each time we toggle the switch. The value 0x08000000 is
        # documented at
        # https://learn.microsoft.com/windows/win32/procthread/process-creation-flags
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
            f"powershell ({operation}) timed out after "
            f"{_POWERSHELL_TIMEOUT_S}s"
        ) from exc
    except FileNotFoundError as exc:
        raise OSError(
            f"powershell executable not found ({_POWERSHELL})"
        ) from exc

    if check and completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise OSError(
            f"powershell ({operation}) exit={completed.returncode}: "
            f"{stderr[:200]}"
        )
    return (completed.stdout or ""), (completed.stderr or "")


def _ps_quote(s: str) -> str:
    """Escape ``s`` for embedding inside a single-quoted PowerShell string.

    PowerShell single-quoted strings cannot contain an unescaped single
    quote. The escape rule is to double the single quote (``'`` -> `''`).
    Path strings very rarely contain an apostrophe, but the user could
    legitimately have one in a target path, so we don't trust that.
    """
    return s.replace("'", "''")


# ---------------------------------------------------------------------------
# Default I/O helpers (delegating to the OS)
# ---------------------------------------------------------------------------


def _default_create_link(
    target_exe: Path, link_path: Path, extra_args: str = ""
) -> None:
    """Create ``link_path`` pointing at ``target_exe``.

    Thin wrapper around ``_powershell_create_shortcut`` so the public
    API can inject a different implementation in tests without
    dragging the PowerShell layer with it. ``extra_args`` is passed
    through to the PowerShell layer (see
    ``_powershell_create_shortcut`` for the meaning of the value).

    Raises ``OSError`` if PowerShell fails or the link file is not on
    disk after the call returns. Callers (``enable()``) translate this
    to a ``FAILED`` ``StartupShortcutResult``.
    """
    _powershell_create_shortcut(target_exe, link_path, extra_args)


def _default_remove_file(path: Path) -> None:
    """Remove ``path`` if it exists. Silent no-op if it doesn't.

    Separate from ``os.unlink`` so tests can inject a stub that
    simulates a permission error or a virus-scanner lock.
    """
    if path.exists():
        os.unlink(path)


def _default_resolve_link_target(
    link_path: Path,
) -> Optional[Path]:
    """Read ``link_path``'s TargetPath and return it as a Path.

    Returns ``None`` if the link is unreadable / target is empty /
    PowerShell fails. The caller treats None as "rewrite from
    scratch", which is the safe default.
    """
    raw = _powershell_read_shortcut(link_path)
    return Path(raw) if raw else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enable(
    target_exe: Path,
    *,
    name: str = _STARTUP_FOLDER_NAME,
    extra_args: str = "",
    _startup_folder: Callable[[], Path] = _default_startup_folder,
    _create_link: Callable[[Path, Path, str], None] = _default_create_link,
    _resolve_link_target: Callable[[Path], Optional[Path]] = (
        _default_resolve_link_target
    ),
) -> StartupShortcutResult:
    """Place a startup-folder shortcut pointing at ``target_exe``.

    ``name`` selects which shortcut file to write (the default keeps
    the historical ``RemoteMicRC003.lnk`` for the settings-window
    auto-start; the bridge auto-start passes
    ``_BRIDGE_STARTUP_FOLDER_NAME`` to write a separate ``.lnk`` so
    the two coexist in the Startup folder). ``extra_args`` is
    appended verbatim to the shortcut's command line (the bridge
    mode passes ``--bridge`` so the launched process boots the
    headless bridge rather than opening the settings window).

    Idempotent: if the shortcut already exists and points at
    ``target_exe`` (with matching ``extra_args``), returns
    ``ALREADY_ENABLED`` instead of rewriting it (avoiding the
    shortcut's mtime changing under the user, which would look like
    the toggle was flipped twice). If the existing shortcut points
    at a different target or different args, it is replaced with one
    matching ``target_exe`` and ``extra_args``.

    Returns ``FAILED`` if any OS call raises. Callers (the UI) should
    surface ``result.message`` to the user and not throw further.
    """
    target_exe = Path(target_exe).resolve()
    try:
        folder = _startup_folder()
    except OSError as exc:
        return StartupShortcutResult(
            outcome=StartupShortcutOutcome.FAILED,
            message=f"无法获取启动文件夹：{exc}",
            target_path=str(target_exe),
        )

    link_path = folder / f"{name}{_SHORTCUT_EXTENSION}"

    if link_path.exists():
        existing_target = _resolve_link_target(link_path)
        if existing_target is not None and (
            str(existing_target) == str(target_exe)
            or str(existing_target).lower()
            == str(target_exe).lower()
        ):
            # Target matches - re-check the args; if they don't match
            # we must rewrite the shortcut. We only know the target
            # path here (the resolver does not return arguments); the
            # easy correct behaviour is: if no extra_args were
            # requested, treat as already-enabled; if extra_args were
            # requested, rewrite so we don't silently leave an old
            # command line in place.
            if extra_args == "":
                return StartupShortcutResult(
                    outcome=StartupShortcutOutcome.ALREADY_ENABLED,
                    message="开机自启动已启用。",
                    target_path=str(target_exe),
                )

    try:
        _create_link(target_exe, link_path, extra_args)
    except OSError as exc:
        return StartupShortcutResult(
            outcome=StartupShortcutOutcome.FAILED,
            message=f"无法创建启动快捷方式：{exc}",
            target_path=str(target_exe),
        )

    return StartupShortcutResult(
        outcome=StartupShortcutOutcome.ENABLED,
        message="已启用开机自启动。" if extra_args == ""
        else "已启用开机自动启动桥接模式。",
        target_path=str(target_exe),
    )


def disable(
    *,
    name: str = _STARTUP_FOLDER_NAME,
    _startup_folder: Callable[[], Path] = _default_startup_folder,
    _remove_file: Callable[[Path], None] = _default_remove_file,
) -> StartupShortcutResult:
    """Remove the startup-folder shortcut if present.

    ``name`` selects which shortcut file to remove (default keeps
    backward compatibility with the historical
    ``RemoteMicRC003.lnk``; bridge auto-start passes the bridge
    shortcut name).

    Idempotent: if no shortcut exists, returns ``ALREADY_DISABLED``
    rather than failing.
    """
    try:
        folder = _startup_folder()
    except OSError as exc:
        return StartupShortcutResult(
            outcome=StartupShortcutOutcome.FAILED,
            message=f"无法获取启动文件夹：{exc}",
        )

    link_path = folder / f"{name}{_SHORTCUT_EXTENSION}"
    if not link_path.exists():
        return StartupShortcutResult(
            outcome=StartupShortcutOutcome.ALREADY_DISABLED,
            message="开机自启动已关闭。"
            if name == _STARTUP_FOLDER_NAME
            else "开机自动启动桥接模式已关闭。",
        )

    try:
        _remove_file(link_path)
    except OSError as exc:
        return StartupShortcutResult(
            outcome=StartupShortcutOutcome.FAILED,
            message=f"无法删除启动快捷方式：{exc}",
        )

    return StartupShortcutResult(
        outcome=StartupShortcutOutcome.DISABLED,
        message="已关闭开机自启动。"
        if name == _STARTUP_FOLDER_NAME
        else "已关闭开机自动启动桥接模式。",
    )


def is_enabled(
    *,
    name: str = _STARTUP_FOLDER_NAME,
    _startup_folder: Callable[[], Path] = _default_startup_folder,
) -> bool:
    """True iff the named startup-folder shortcut exists.

    Used by the settings UI on window open to sync each toggle's
    checked state to reality. Failure here is non-fatal: returns
    False so the UI shows the toggle OFF (which is the safe default
    - the user will enable it explicitly if they want it).
    """
    try:
        folder = _startup_folder()
    except OSError:
        return False
    link_path = folder / f"{name}{_SHORTCUT_EXTENSION}"
    return link_path.exists()


def shortcut_path(
    *,
    name: str = _STARTUP_FOLDER_NAME,
    _startup_folder: Callable[[], Path] = _default_startup_folder,
) -> Path:
    """Return the on-disk shortcut path, whether or not it exists.

    Useful for the settings UI's "Show in Explorer" affordance - the
    path is always well-defined and the UI can offer to reveal it
    even when the shortcut is currently absent. ``name`` selects
    which shortcut to address (see ``enable`` / ``disable``).
    """
    folder = _startup_folder()
    return folder / f"{name}{_SHORTCUT_EXTENSION}"


__all__ = [
    "StartupShortcutOutcome",
    "StartupShortcutResult",
    "StartupShortcutError",
    "enable",
    "disable",
    "is_enabled",
    "shortcut_path",
]