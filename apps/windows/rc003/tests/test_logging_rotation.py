"""Tests for the rotating log handler (src/ovb_rc003/logging_setup.py).

Pure-Python / no Qt, no real PowerShell, no real device - every test works in
its own temp directory.

Why the rotation mechanics are worth this much test weight: ``app.log`` is the
only diagnostic channel for the Frida HID tap and the BLE reconnect path, and
this package's own "view log" button hands ``app.log`` straight to Notepad. On
Windows the rotation rename therefore fails with ``PermissionError`` whenever
the user is looking at the log - the *normal* case, not an exotic one. The
stock ``RotatingFileHandler`` degrades quietly in that situation: it drops the
record that triggered the rotation, and then drops every later record that does
not fit in the remaining headroom, so the file creeps to a few bytes under its
cap and effectively stops growing. In a windowed (``console=False``) build even
the ``handleError`` report has nowhere to go, so there is no symptom at all.

``_ResilientRotatingFileHandler`` instead swallows the failed rename and
reopens the stream, degrading to "the file keeps growing past maxBytes" - a
state this app already knows how to read.

``test_the_stock_handler_really_does_drop_records_after_a_failed_rotation``
pins the bug being worked around, so the resilient handler cannot quietly
become a no-op that no test would notice.

Run with::

    PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/test_logging_rotation.py
"""

from __future__ import annotations

import logging
import logging.handlers
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb_rc003 import logging_setup


def _make_handler(
    path: Path,
    *,
    max_bytes: int,
    backup_count: int,
    handler_class=logging_setup._ResilientRotatingFileHandler,
):
    handler = handler_class(
        str(path), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _emit(handler: logging.Handler, message: str) -> None:
    """Deliver one record straight to the handler, bypassing logger plumbing."""
    handler.emit(logging.LogRecord("test", logging.INFO, __file__, 1, message, None, None))


def _segments(directory: Path, filename: str = "app.log") -> list[Path]:
    return sorted(directory.glob(f"{filename}*"))


class _RotationCase(unittest.TestCase):
    """Shared temp dir plus a handler whose stream is always closed."""

    # Chosen so that every emit after the first necessarily triggers a
    # rollover: one formatted record is ~71 bytes, so the live file is always
    # over the cap by the time the next record arrives. That makes the
    # failed-rename tests deterministic rather than dependent on where the cap
    # happens to fall relative to a record boundary.
    max_bytes = 100
    backup_count = 2
    payload = "x" * 60

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        self.log_path = self.directory / "app.log"

    def make_handler(
        self, *, path=None, max_bytes=None, backup_count=None, handler_class=None
    ):
        kwargs = {
            "max_bytes": self.max_bytes if max_bytes is None else max_bytes,
            "backup_count": self.backup_count if backup_count is None else backup_count,
        }
        if handler_class is not None:
            kwargs["handler_class"] = handler_class
        handler = _make_handler(self.log_path if path is None else path, **kwargs)
        self.addCleanup(handler.close)
        return handler

    def fill(self, handler, count: int, *, tag: str = "record"):
        for index in range(count):
            _emit(handler, f"{tag}-{index:03d}-{self.payload}")

    def contents(self, path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace")


class RotationPolicyTests(unittest.TestCase):
    """The policy ``get_logger()`` installs - bounds, and where the log lives."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        # get_logger() installs its handler once per process, so a test that
        # inspects or replaces it must take the module back to its
        # pre-configured state - and must put the original handlers back
        # afterwards, or every later test in this one-process run inherits
        # them.
        self._logger = logging.getLogger(logging_setup.LOGGER_NAME)
        self._saved_configured = logging_setup._configured
        self._saved_handlers = list(self._logger.handlers)
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
        logging_setup._configured = False
        self.addCleanup(self._restore)

    def _restore(self):
        for handler in list(self._logger.handlers):
            self._logger.removeHandler(handler)
            handler.close()
        for handler in self._saved_handlers:
            self._logger.addHandler(handler)
        logging_setup._configured = self._saved_configured

    def test_get_logger_installs_a_size_rotating_handler(self):
        logging_setup.get_logger(root=self.root)

        handlers = [
            h
            for h in self._logger.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        self.assertEqual(len(handlers), 1, f"expected one rotating handler, got {handlers}")
        handler = handlers[0]

        self.assertEqual(handler.maxBytes, logging_setup.LOG_MAX_BYTES)
        self.assertEqual(handler.backupCount, logging_setup.LOG_BACKUP_COUNT)
        # Specifically the resilient subclass: the stock handler's behaviour
        # under a failed rename is the bug this module exists to avoid.
        self.assertIsInstance(handler, logging_setup._ResilientRotatingFileHandler)

    def test_the_live_log_is_still_app_log(self):
        self.assertEqual(logging_setup.LOG_FILENAME, "app.log")
        self.assertEqual(logging_setup.log_file_path(self.root).name, "app.log")

        logger = logging_setup.get_logger(root=self.root)
        logger.info("hello from the rotation policy test")
        for handler in logger.handlers:
            handler.flush()

        # Rotation always leaves the live log at app.log, so log_file_path()
        # and the "view log" button keep working after any number of rolls.
        live = logging_setup.log_file_path(self.root)
        self.assertTrue(live.exists(), f"live log missing at {live}")
        self.assertIn("hello from the rotation policy test", live.read_text(encoding="utf-8"))

    def test_the_policy_is_bounded_and_the_backups_are_kept(self):
        # Bounded: a runaway log must not be able to fill the disk.
        self.assertGreater(logging_setup.LOG_MAX_BYTES, 0)
        self.assertLessEqual(
            logging_setup.LOG_MAX_BYTES,
            16 * 1024 * 1024,
            "the per-file cap grew past anything a diagnostic log needs",
        )
        # Kept: the cap must not be implemented by discarding history.
        self.assertGreaterEqual(
            logging_setup.LOG_BACKUP_COUNT,
            1,
            "backupCount 0 would rotate the log away and lose all history",
        )

        total_bound = logging_setup.LOG_MAX_BYTES * (logging_setup.LOG_BACKUP_COUNT + 1)
        self.assertLessEqual(
            total_bound,
            64 * 1024 * 1024,
            "live log plus every backup must stay within a sane on-disk budget",
        )


class RotationMechanicsTests(_RotationCase):
    """Rotation itself, including the failed-rename path Windows actually hits."""

    def test_exceeding_the_cap_rotates_and_bounds_the_live_file(self):
        handler = self.make_handler()
        total_written = 0
        for index in range(200):
            message = f"{index:03d}-{self.payload}"
            total_written += len(message) + 1
            _emit(handler, message)

        self.assertTrue(self.log_path.exists(), "the live log must survive rotation")
        self.assertTrue(
            (self.directory / "app.log.1").exists(),
            "exceeding maxBytes must have produced a backup",
        )
        live_size = self.log_path.stat().st_size
        self.assertLess(
            live_size,
            total_written,
            f"the live log ({live_size} B) should have been rotated, not grown "
            f"to hold all {total_written} B",
        )
        # The live file may overshoot by at most the record that triggered the
        # roll, so allow one record of slack.
        self.assertLessEqual(live_size, self.max_bytes + len(self.payload) + 64)

    def test_backups_never_exceed_backup_count(self):
        handler = self.make_handler()
        self.fill(handler, 200)

        present = [p.name for p in _segments(self.directory)]
        self.assertIn("app.log.1", present)
        self.assertIn("app.log.2", present)
        self.assertNotIn(
            "app.log.3",
            present,
            f"backupCount={self.backup_count} must cap the retained segments: {present}",
        )

    def test_a_failed_rotation_keeps_the_previous_contents(self):
        handler = self.make_handler(max_bytes=200, backup_count=2)
        self.fill(handler, 3, tag="before")
        before = self.contents(self.log_path)
        self.assertTrue(before)

        # Exactly the Windows case: another process holds app.log open, so the
        # rename cannot run.
        with mock.patch.object(handler, "rotate", side_effect=OSError("file in use")):
            self.fill(handler, 2, tag="after")

        after = self.contents(self.log_path)
        # "Keeps the previous contents" means the live file is appended to,
        # never truncated or rewritten - earlier records may legitimately have
        # moved into a backup by an *earlier, successful* rotation.
        self.assertTrue(
            after.startswith(before),
            "a failed rotation must append to what was already logged, never "
            f"truncate or rewrite it:\n  before={before!r}\n  after ={after!r}",
        )
        self.assertGreater(
            len(after),
            len(before),
            "records written after the failed rotation should have been appended "
            "to the same live file",
        )

    def test_a_failed_rotation_still_lets_the_log_keep_writing(self):
        handler = self.make_handler(max_bytes=200, backup_count=2)
        self.fill(handler, 3, tag="before")

        with mock.patch.object(handler, "rotate", side_effect=OSError("file in use")):
            for index in range(5):
                _emit(handler, f"after-{index:03d}-{self.payload}")

        after = self.contents(self.log_path)
        # The record that triggered the rotation must land, and so must every
        # later one - the stock handler drops all of them.
        for index in range(5):
            self.assertIn(
                f"after-{index:03d}",
                after,
                "the log stopped writing after a failed rotation - the file "
                "creeps to just under its cap and then freezes",
            )

    def test_the_stock_handler_really_does_drop_records_after_a_failed_rotation(self):
        # The control for the whole file. If this ever stops reproducing, the
        # resilient handler is no longer fixing anything and the tests above
        # are proving nothing.
        # Separate files, so the two handlers never share a path (Windows
        # refuses to unlink a file another handle still holds open).
        stock_path = self.directory / "stock.log"
        stock = self.make_handler(
            path=stock_path, handler_class=logging.handlers.RotatingFileHandler
        )
        self.fill(stock, 2, tag="before")
        with mock.patch.object(logging, "raiseExceptions", False):
            with mock.patch.object(stock, "rotate", side_effect=OSError("file in use")):
                _emit(stock, f"after-000-{self.payload}")

        self.assertNotIn(
            "after-000",
            self.contents(stock_path),
            "the stock RotatingFileHandler is expected to DROP the record that "
            "triggered the failed rotation - if it no longer does, this control "
            "has gone stale and the resilient handler's tests prove nothing",
        )

        # Same scenario, same failure, on the handler we actually ship:
        resilient_path = self.directory / "resilient.log"
        resilient = self.make_handler(path=resilient_path)
        self.fill(resilient, 2, tag="before")
        with mock.patch.object(resilient, "rotate", side_effect=OSError("file in use")):
            _emit(resilient, f"after-000-{self.payload}")

        self.assertIn(
            "after-000",
            self.contents(resilient_path),
            "the resilient handler must keep the record the stock one drops",
        )
