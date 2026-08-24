"""Launches the bridge process from the settings window (XRBM-029), and
reports what actually happened - not just "a process was created". This
module never conflates a launched/still-running process with "RC003 is
connected": that fact is only observable from ``app.log`` (see
``logging_setup.py``), never from process liveness alone.

Command construction (``build_launch_command``) covers exactly the two ways
this package's own entry point (``__main__.py``) is ever invoked, so a
future third mode cannot silently fall through unnoticed:

- **Frozen** (the packaged ``RemoteMicRC003.exe``, built from
  ``src/launcher.py`` - see that module's docstring): ``sys.executable`` IS
  that same exe, and running it again with ``--bridge`` enters
  ``__main__.main()``'s ``_run_bridge()`` branch. The no-argument form now
  opens the settings window, so the bridge is always launched EXPLICITLY -
  never ``--settings``, which would just open a second settings window
  instead of starting the bridge.
- **Source** (``python -m ovb_rc003``): ``sys.executable`` is the
  interpreter itself; ``[sys.executable, "-m", "ovb_rc003", "--bridge"]``
  enters the same bridge branch. This relies on the child inheriting the
  parent process's environment (``subprocess.Popen`` does this by default) -
  in particular ``PYTHONPATH=src``, which the settings window's own process
  needed to have been started with in order to import ``ovb_rc003`` at all
  (see the root README's "Running from source" section).

Both branches deliberately append ``--bridge`` and never ``--settings``:
that argument would recursively open another settings window instead of
starting the bridge (In-scope item 2's "不得递归打开 --settings").

Launch-outcome detection (``launch_bridge``) distinguishes four states by
polling the child for a short grace period rather than assuming
"``Popen()`` did not raise" means "the bridge is running":

- ``STARTED``: the process is still alive once the grace period elapses -
  the best evidence available from process state alone that startup is
  proceeding, NOT proof of an RC003 connection. ``launch_bridge`` ALSO
  returns a ``BridgeHandle`` (see below) attached to the ``LaunchResult`` so
  the caller - i.e. the settings window - can stop the same child process
  later instead of leaving it orphaned when the window closes.
- ``ALREADY_RUNNING``: the process exited within the grace period with
  exactly ``single_instance.DUPLICATE_INSTANCE_EXIT_CODE`` - the
  single-instance guard in ``single_instance.py``/``__main__.py`` refused a
  second concurrent bridge instance. Reusing that exact constant (rather
  than redefining a second one here) keeps the two modules from silently
  drifting apart if the exit code is ever renumbered. No handle is
  attached: there is no process the caller could stop.
- ``QUICK_EXIT``: the process exited within the grace period with any OTHER
  code (including ``GUARD_UNAVAILABLE_EXIT_CODE``/``CLEANUP_FAILED_EXIT_CODE``
  or an unhandled exception's implicit ``1``) - a real failure, whose exact
  code is always surfaced to the caller rather than swallowed, so a user or
  reviewer can distinguish it from a clean exit without guessing. No
  handle is attached.
- ``LAUNCH_FAILED``: ``Popen()`` itself raised ``OSError`` (e.g. the target
  executable is missing or not executable) - no process was ever created at
  all. No handle is attached.

Termination (``stop_bridge``) is the symmetric reverse: it takes the same
``BridgeHandle`` returned by ``launch_bridge`` and asks the underlying
``Popen`` to terminate, then waits for it to actually exit. On Windows,
``Popen.terminate()`` calls ``TerminateProcess`` (NOT a signal handler), so
the only way to confirm the child has actually gone is to poll ``poll()``
or ``wait()``; we do the latter with a bounded grace period, then fall
back to ``kill()`` (which on Windows is identical to ``terminate()`` -
``TerminateProcess`` is unconditional - so a process that survives
``terminate()`` is stuck on an unkillable Win32 syscall and the caller is
told ``TIMED_OUT`` rather than the function hanging forever). Returns one
of three ``StopOutcome`` values: ``STOPPED`` (process exited within
grace), ``TIMED_OUT`` (still alive after grace - the caller should warn
the user), and ``NOT_RUNNING`` (the handle's process had already exited
before ``stop_bridge`` was called).

Testability: every OS-facing call (``_popen``, ``_sleep``, ``_wait``,
``_terminate``, ``_kill``) is injectable, so tests/test_bridge_launcher.py
drives all launch outcomes AND all stop outcomes deterministically -
including the grace-period polling loops in both directions - without
spawning a real process or sleeping in real wall-clock time, the same
dependency-injection pattern this package's other Win32-facing modules
already use (see e.g. ``single_instance.py``'s ``_create_mutex``/
``_release_mutex``/``_close_handle`` parameters).
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import single_instance

# Reused, not redefined - see module docstring's ALREADY_RUNNING note.
ALREADY_RUNNING_EXIT_CODE = single_instance.DUPLICATE_INSTANCE_EXIT_CODE

DEFAULT_GRACE_CHECKS = 10
DEFAULT_POLL_INTERVAL_SECONDS = 0.15
DEFAULT_STOP_GRACE_SECONDS = 3.0


class BridgeLaunchConfigurationError(Exception):
    """Raised when a launch command cannot be constructed at all (e.g.
    ``sys.executable`` is empty - which CPython documents can happen in some
    embedding scenarios). Fails closed rather than handing ``Popen`` an
    empty/garbage argv[0].
    """


def build_launch_command(
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
) -> List[str]:
    """Builds the bridge launch command for the CURRENT process shape.
    Always appends ``--bridge``: the no-argument form of this exe now opens
    the settings window, so the bridge must be requested explicitly.
    ``frozen``/``executable`` are injectable so tests can exercise both
    branches deterministically on any OS - production callers should never
    pass them.
    """

    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if executable is None:
        executable = sys.executable

    if not executable:
        raise BridgeLaunchConfigurationError(
            "sys.executable is empty; cannot construct a bridge launch command"
        )

    if frozen:
        # The same frozen exe handles both modes: with no arguments (or
        # --settings) it opens the settings window, and with --bridge (as
        # launched here) it starts the bridge - see __main__.py's dispatch.
        return [executable, "--bridge"]
    # The current interpreter, `-m ovb_rc003 --bridge`.
    return [executable, "-m", "ovb_rc003", "--bridge"]


class LaunchOutcome(Enum):
    STARTED = "started"
    ALREADY_RUNNING = "already_running"
    QUICK_EXIT = "quick_exit"
    LAUNCH_FAILED = "launch_failed"


@dataclass(frozen=True)
class LaunchResult:
    outcome: LaunchOutcome
    command: Tuple[str, ...]
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    # Populated ONLY when ``outcome`` is ``STARTED`` - the live ``Popen``
    # handle the caller should hand back to ``stop_bridge`` to actually
    # terminate the same child. ``None`` for ``ALREADY_RUNNING`` /
    # ``QUICK_EXIT`` / ``LAUNCH_FAILED`` because in those branches the
    # process has either never existed or has already exited, so there is
    # nothing for ``stop_bridge`` to do.
    handle: Optional["BridgeHandle"] = None


@dataclass(frozen=True)
class BridgeHandle:
    """A live reference to a launched bridge subprocess, returned by
    ``launch_bridge`` and consumed by ``stop_bridge``. Wrapping the raw
    ``Popen`` (rather than exposing it directly) keeps the dependency-
    injection seam in ``stop_bridge`` testable: tests pass a
    ``_FakeProcess`` that quacks like a ``Popen`` (``poll``/``terminate``/
    ``kill``/``wait``), so we never need a real child to exercise the
    start/stop/timeout/already-dead branches.
    """

    process: "subprocess.Popen"

    def pid(self) -> Optional[int]:
        return getattr(self.process, "pid", None)

    def is_alive(self) -> bool:
        # ``Popen.poll()`` returns ``None`` while alive, the exit code once
        # terminated - so ``poll() is None`` is the canonical liveness test.
        return self.process.poll() is None


class StopOutcome(Enum):
    STOPPED = "stopped"
    TIMED_OUT = "timed_out"
    NOT_RUNNING = "not_running"


@dataclass(frozen=True)
class StopResult:
    outcome: StopOutcome
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    # ``True`` when ``stop_bridge`` had to escalate to ``kill()`` because the
    # process was still alive after the grace period. On Windows
    # ``kill()`` is the same ``TerminateProcess`` as ``terminate()``, so
    # ``escalated=True`` is informational (a process that survives
    # ``terminate()`` for ``grace_seconds`` is in an unkillable Win32 call,
    # not a real process); included for diagnostic logging only.
    escalated: bool = False


def launch_bridge(
    command: Optional[Sequence[str]] = None,
    *,
    grace_checks: int = DEFAULT_GRACE_CHECKS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    _popen: Callable[..., "subprocess.Popen"] = subprocess.Popen,
    _sleep: Callable[[float], None] = time.sleep,
    _popen_kwargs: Optional[Dict[str, object]] = None,
) -> LaunchResult:
    """Starts the bridge and watches it for a short grace period to tell a
    process that is actually running apart from one that merely got created
    and then immediately died. Never raises for an ordinary launch failure
    (``OSError`` from ``_popen``) - that is reported as ``LAUNCH_FAILED``
    instead, since this is called directly from a Tk button handler that
    must not crash the settings window over a failed launch.
    """

    resolved_command: Tuple[str, ...] = (
        tuple(command) if command is not None else tuple(build_launch_command())
    )

    popen_kwargs = dict(_popen_kwargs) if _popen_kwargs is not None else {}
    if _popen is subprocess.Popen and sys.platform == "win32":
        # A bridge subprocess is a console subsystem program (python.exe in a
        # source checkout) or a frozen ``console=False`` exe; without this
        # flag Windows flashes a black console window for the child even when
        # the parent is a GUI app. CREATE_NO_WINDOW only exists on Windows.
        popen_kwargs.setdefault(
            "creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

    try:
        process = _popen(list(resolved_command), **popen_kwargs)
    except OSError as exc:
        return LaunchResult(
            outcome=LaunchOutcome.LAUNCH_FAILED,
            command=resolved_command,
            error=str(exc),
        )

    pid = getattr(process, "pid", None)
    exit_code = process.poll()
    checks = 0
    while exit_code is None and checks < grace_checks:
        _sleep(poll_interval_seconds)
        exit_code = process.poll()
        checks += 1

    if exit_code is None:
        return LaunchResult(
            outcome=LaunchOutcome.STARTED,
            command=resolved_command,
            pid=pid,
            handle=BridgeHandle(process=process),
        )
    if exit_code == ALREADY_RUNNING_EXIT_CODE:
        return LaunchResult(
            outcome=LaunchOutcome.ALREADY_RUNNING,
            command=resolved_command,
            pid=pid,
            exit_code=exit_code,
        )
    return LaunchResult(
        outcome=LaunchOutcome.QUICK_EXIT,
        command=resolved_command,
        pid=pid,
        exit_code=exit_code,
    )


def stop_bridge(
    handle: Optional[BridgeHandle],
    *,
    grace_seconds: float = DEFAULT_STOP_GRACE_SECONDS,
    _terminate: Callable[[], None] = None,
    _kill: Callable[[], None] = None,
    _wait: Callable[[float], int] = None,
    _poll: Callable[[], Optional[int]] = None,
) -> StopResult:
    """Asks the bridge subprocess identified by ``handle`` to terminate,
    then waits up to ``grace_seconds`` for it to actually exit. If it has
    not exited by then, escalates to ``kill()`` (which on Windows is the
    same ``TerminateProcess`` as ``terminate()`` - so a process that
    survives both is in an unkillable Win32 syscall; we return ``TIMED_OUT``
    rather than hanging forever, and let the caller decide whether to warn
    the user or just log the diagnostic).

    Returns ``NOT_RUNNING`` if the process had already exited by the time
    ``stop_bridge`` was called (e.g. it crashed, or a previous call to
    ``stop_bridge`` already terminated it), so the caller can show the
    right status text without double-tearing-down.

    ``grace_seconds <= 0`` is a one-shot terminate-then-kill with no wait,
    which is the right answer for the ``WM_DELETE_WINDOW`` fast path where
    the parent is trying to exit *now* and any blocking wait is worse than
    a leaked child.

    All OS-facing calls are injectable (``_terminate``, ``_kill``, ``_wait``,
    ``_poll``) so tests/test_bridge_launcher.py drives every branch
    deterministically without a real subprocess. Production callers never
    pass these.
    """

    if handle is None:
        return StopResult(outcome=StopOutcome.NOT_RUNNING)
    process = handle.process

    # Resolve injectable defaults AFTER the None check, so a None handle
    # never touches the real ``Popen`` API (defensive against a partial
    # teardown where the underlying process has been garbage-collected).
    if _terminate is None:
        _terminate = process.terminate
    if _kill is None:
        _kill = process.kill
    if _wait is None:
        _wait = process.wait
    if _poll is None:
        _poll = process.poll

    pid = getattr(process, "pid", None)

    # Fast path: already exited before we got here - never call terminate/
    # kill on a dead process (raises on some platforms, and is misleading
    # in any case: there is nothing to stop).
    existing_exit = _poll()
    if existing_exit is not None:
        return StopResult(
            outcome=StopOutcome.NOT_RUNNING,
            pid=pid,
            exit_code=existing_exit,
        )

    try:
        _terminate()
    except OSError:
        # Treat a terminate failure (process vanished between the poll
        # above and the terminate call - Windows ERROR_ACCESS_DENIED on a
        # PROCESS_TERMINATE right we never had, etc.) as "already gone" -
        # there is nothing left to escalate against.
        final_exit = _poll()
        return StopResult(
            outcome=StopOutcome.NOT_RUNNING,
            pid=pid,
            exit_code=final_exit,
        )

    if grace_seconds > 0:
        try:
            exit_code = _wait(grace_seconds)
            return StopResult(
                outcome=StopOutcome.STOPPED,
                pid=pid,
                exit_code=exit_code,
            )
        except subprocess.TimeoutExpired:
            pass

    # Grace period elapsed without the process exiting. Escalate.
    try:
        _kill()
    except OSError:
        final_exit = _poll()
        return StopResult(
            outcome=StopOutcome.TIMED_OUT,
            pid=pid,
            exit_code=final_exit,
            escalated=True,
        )

    # Give kill() one bounded shot to take effect. On Windows ``kill()`` is
    # ``TerminateProcess`` and is unconditional, so this branch is
    # effectively the same code path as the terminate branch above - but
    # the structure here is what POSIX would need, so we keep it for
    # portability and let the tests assert the symmetric behavior.
    if grace_seconds > 0:
        try:
            exit_code = _wait(grace_seconds)
            return StopResult(
                outcome=StopOutcome.STOPPED,
                pid=pid,
                exit_code=exit_code,
                escalated=True,
            )
        except subprocess.TimeoutExpired:
            pass

    final_exit = _poll()
    return StopResult(
        outcome=StopOutcome.TIMED_OUT,
        pid=pid,
        exit_code=final_exit,
        escalated=True,
    )
