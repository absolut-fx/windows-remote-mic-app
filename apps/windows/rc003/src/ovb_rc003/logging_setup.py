"""Logging configuration that never touches voice content or device identity.

Callers must only ever log opaque markers (button names, session ids,
sample/frame counts, boolean flags) - never a Bluetooth address, HID
interface path, device token, or decoded voice content. This module doesn't
attempt to scrub arbitrary strings after the fact (a redaction filter would
give false confidence); the guarantee instead comes from code review plus
tests/test_privacy_contract.py, which statically scans this package's source
for the sensitive field names and MAC-address-shaped literals.

``log_dir``/``log_file_path`` (XRBM-029) expose this module's canonical
``%LOCALAPPDATA%\\RemoteMic\\RC003\\logs\\app.log`` location WITHOUT the
side effect ``get_logger()`` has of creating that directory - the settings
window's "open log directory" entry needs to tell a user "this doesn't exist
yet, the app hasn't run" as an honest fact, which would be impossible if
merely asking the question always created the directory first.
``describe_log_location``/``open_log_location`` build on those two path
functions to answer "does it exist" and "open it" respectively, the latter
via an injectable ``_open_directory`` callable (mirrors this package's other
OS-facing modules, e.g. ``single_instance.py``) so
tests/test_logging_setup_location.py can exercise every branch without
actually invoking Windows Explorer.

``get_logger()`` writes through a size-rotating handler rather than an
unbounded ``FileHandler`` (``LOG_MAX_BYTES`` records the measured sizing, and
``_ResilientRotatingFileHandler`` explains why it is not the stock one).
Rotation always leaves the live log at ``app.log``, so ``log_file_path()``,
``describe_log_location()`` and the settings window's "view log" button are
all unaffected; the older segments appear beside it as ``app.log.1`` ...
``app.log.5``.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from . import config

LOGGER_NAME = "ovb_rc003"
LOG_FILENAME = "app.log"

# 2 MiB per file x 5 backups = a 12 MiB ceiling for a log that is the only
# diagnostic channel for the Frida HID tap and the BLE reconnect path.
#
# Sizing is measured, not guessed: the app writes ~5 KB/hour after the log
# flood fix (it was ~10 MB/hour before, which is what made an unbounded
# FileHandler untenable - app.log.1 alone reached 140 MB). At ~5 KB/hour a
# 2 MiB segment holds roughly two weeks of history, so rotation is rare
# enough that the one-retry-per-record cost described on
# _ResilientRotatingFileHandler below is never reached in practice.
#
# Rotation always leaves the live log at app.log, so log_file_path(),
# describe_log_location() and the settings window's "view log" button are
# all unaffected; the older segments appear beside it as app.log.1 ... app.log.5.
# Keep these two values together - the ceiling is the product of them.
LOG_MAX_BYTES = 2097152
LOG_BACKUP_COUNT = 5

_configured = False


class _ResilientRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A size-rotating handler that degrades to "keeps growing" rather than
    "stops logging" when the rotation rename cannot run.

    ``logging.handlers.RotatingFileHandler.doRollover`` closes the stream,
    renames ``app.log`` to ``app.log.1``, and reopens - and its ``emit`` wraps
    that whole call in ``except Exception: self.handleError(record)``. On
    Windows the rename fails with ``PermissionError`` whenever any other
    process holds the file open, which is the *normal* case here: this
    package's own "view log" button hands ``app.log`` straight to Notepad.
    When that happens the stock handler degrades badly, and quietly:

    * the record that triggered the rotation is dropped - ``emit`` never
      reaches ``FileHandler.emit``, because ``doRollover`` raised first;
    * ``shouldRollover`` reopens the abandoned stream on the next record, so
      the file is not lost, but it immediately reports "over the limit" again
      and ``doRollover`` raises again - so **every later record that does not
      fit in the remaining headroom is dropped too**;
    * an ordinary log line is far larger than that headroom, so the file
      creeps to a few bytes under ``maxBytes`` and then effectively stops
      growing. Logging does not literally stop - a record short enough to fit
      still lands - which is precisely what makes this hard to notice; and
    * ``handleError`` reports each drop on stderr, which a windowed
      (``console=False``) build does not have, so there is no symptom at all.

    This log is the only diagnostic channel for the Frida HID tap and the BLE
    reconnect path, so a log that quietly freezes at its cap is the outcome
    worth designing against. Swallowing the rename failure and reopening the
    stream unconditionally degrades to "the file keeps growing past
    ``maxBytes``" - exactly the behaviour this app had before rotation
    existed, which is a state we already know how to read.

    The cost of that choice is one retry per record while the file is over the
    limit (a close, a failed rename chain, and a reopen). At the measured
    ~5 KB/hour that is irrelevant; it is only reached at all when a user is
    holding the log open.
    """

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        try:
            self._shift_backups()
        except OSError:
            # Deliberately swallowed. A failed rename (another process holding
            # app.log open - see the class docstring) must not cost us the
            # record that triggered the rotation: degrading to "the file keeps
            # growing past maxBytes" beats dropping records with no symptom.
            pass
        if not self.delay:
            self.stream = self._open()

    def _shift_backups(self) -> None:
        """``RotatingFileHandler.doRollover``'s rename chain, extracted so it
        can fail as one unit and be caught as one unit.
        """

        if self.backupCount <= 0:
            return
        for index in range(self.backupCount - 1, 0, -1):
            source = self.rotation_filename(f"{self.baseFilename}.{index}")
            destination = self.rotation_filename(f"{self.baseFilename}.{index + 1}")
            if os.path.exists(source):
                if os.path.exists(destination):
                    os.remove(destination)
                os.rename(source, destination)
        newest = self.rotation_filename(f"{self.baseFilename}.1")
        if os.path.exists(newest):
            os.remove(newest)
        self.rotate(self.baseFilename, newest)


def get_logger(root: Optional[Path] = None) -> logging.Logger:
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return logger

    logger.setLevel(logging.INFO)
    directory = log_dir(root)
    directory.mkdir(parents=True, exist_ok=True)

    handler = _ResilientRotatingFileHandler(
        directory / LOG_FILENAME,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    _configured = True
    return logger


def log_dir(root: Optional[Path] = None) -> Path:
    """The canonical log directory - does NOT create it (see module
    docstring); only ``get_logger()`` does that, as a side effect of
    actually needing to write to it.
    """

    return (root or config.config_root()) / "logs"


def log_file_path(root: Optional[Path] = None) -> Path:
    return log_dir(root) / LOG_FILENAME


class LogLocationStatus(Enum):
    DIRECTORY_MISSING = "directory_missing"
    FILE_MISSING = "file_missing"
    READY = "ready"


@dataclass(frozen=True)
class LogLocation:
    status: LogLocationStatus
    directory: Path
    file_path: Path


def describe_log_location(root: Optional[Path] = None) -> LogLocation:
    """A pure filesystem check - never creates anything (DoD: "不存在时给出
    诚实提示；不得创建伪日志"). ``FILE_MISSING`` (directory exists, no
    ``app.log`` yet) is distinct from ``DIRECTORY_MISSING`` (the app has
    never even been run on this machine) so a caller can phrase each
    honestly instead of collapsing them into one generic "not found".
    """

    directory = log_dir(root)
    file_path = log_file_path(root)
    if not directory.is_dir():
        return LogLocation(LogLocationStatus.DIRECTORY_MISSING, directory, file_path)
    if not file_path.is_file():
        return LogLocation(LogLocationStatus.FILE_MISSING, directory, file_path)
    return LogLocation(LogLocationStatus.READY, directory, file_path)


class LogOpenOutcome(Enum):
    OPENED = "opened"
    DIRECTORY_MISSING = "directory_missing"
    FILE_MISSING = "file_missing"
    OPEN_FAILED = "open_failed"


@dataclass(frozen=True)
class LogOpenResult:
    outcome: LogOpenOutcome
    location: LogLocation
    error: Optional[str] = None


def _default_open_directory(directory: Path) -> None:
    # os.startfile only exists on Windows (CPython does not define it on
    # other platforms at all) - checked explicitly rather than caught as an
    # AttributeError, so the resulting OSError message is clear about why.
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise OSError("os.startfile is only available on Windows")
    startfile(str(directory))


def _default_open_file(file_path: Path) -> None:
    """Open a SINGLE file in the OS default application.

    Mirrors ``_default_open_directory`` byte-for-byte but targets the
    file directly instead of its containing directory. ``os.startfile``
    on Windows follows the registered handler for the file's
    extension - ``.log`` is normally bound to Notepad, which is the
    one-step "show me the log" UX the user asked for.
    """
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise OSError("os.startfile is only available on Windows")
    startfile(str(file_path))


def open_log_location(
    root: Optional[Path] = None,
    *,
    _open_directory: Callable[[Path], None] = _default_open_directory,
) -> LogOpenResult:
    """Opens the log directory in the OS file browser - never the log FILE
    directly, since which application handles a bare ``.log`` extension is
    unpredictable and not this project's concern. Refuses to open (or
    create) a directory that does not exist yet; any exception the actual
    open call raises is caught and reported rather than propagated, since
    this is called directly from a Tk button handler that must not crash
    the settings window over it.
    """

    location = describe_log_location(root)
    if location.status is LogLocationStatus.DIRECTORY_MISSING:
        return LogOpenResult(LogOpenOutcome.DIRECTORY_MISSING, location)
    try:
        _open_directory(location.directory)
    except Exception as exc:  # noqa: BLE001 - report, never crash the settings window
        return LogOpenResult(LogOpenOutcome.OPEN_FAILED, location, error=str(exc))
    return LogOpenResult(LogOpenOutcome.OPENED, location)


def open_log_file(
    root: Optional[Path] = None,
    *,
    _open_file: Callable[[Path], None] = _default_open_file,
) -> LogOpenResult:
    """Opens the ``app.log`` file directly in the OS default application.

    Sibling of ``open_log_location`` for the one-step View-log UX the
    user asked for: click "查看日志" once, Notepad (or whatever the
    user's registered ``.log`` handler is) pops up with the live
    log file - no need to first open the directory, then double-click
    the file, then wait.

    Difference vs. ``open_log_location``:
        - Targets the file, not the directory.
        - Returns ``FILE_MISSING`` (not ``OPENED``) when the file
          itself does not exist yet - the user has not run the bridge
          since the last reinstall, and we MUST NOT silently open
          the directory instead, because the contract for the new
          button is "I want to see the log, not a folder picker".
        - Returns ``OPENED`` only when the open call actually ran
          against an existing ``app.log``.

    Errors are caught and reported via ``LogOpenResult`` so the
    Qt button handler that calls this stays crash-free.
    """

    location = describe_log_location(root)
    if location.status is LogLocationStatus.DIRECTORY_MISSING:
        return LogOpenResult(LogOpenOutcome.DIRECTORY_MISSING, location)
    if location.status is LogLocationStatus.FILE_MISSING:
        # Do NOT fall back to opening the directory: that would lie
        # to the user about what just happened. Be honest instead.
        return LogOpenResult(LogOpenOutcome.FILE_MISSING, location)
    try:
        _open_file(location.file_path)
    except Exception as exc:  # noqa: BLE001 - report, never crash the settings window
        return LogOpenResult(LogOpenOutcome.OPEN_FAILED, location, error=str(exc))
    return LogOpenResult(LogOpenOutcome.OPENED, location)
