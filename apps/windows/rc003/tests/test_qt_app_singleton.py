"""Guards for the shared Qt application (tests/qt_app.py).

Two things are enforced here, and the second one is the reason this file
exists at all:

1. ``qt_app.get_application()`` really is a **widgets-capable** singleton -
   it can host a ``QWidget``, and it is the same object every time.
2. No test module under ``tests/`` constructs a bare ``QGuiApplication`` **in
   process**. ``unittest discover`` runs every module in one process and the
   first one to need an application fixes its type for the whole run; a bare
   ``QGuiApplication`` cannot host QWidget-family classes, so the next
   ``QMenu``/``QMessageBox`` calls ``qFatal`` -> ``abort`` -> the run dies
   with ``0xC0000409``, printing no traceback and no test report at all.

The scan is an AST walk, not a regex, and that distinction is load-bearing:
this suite's probe scripts are held in string literals and legitimately
contain ``QGuiApplication(...)`` as *data* - they run in their own subprocess,
where a bare ``QGuiApplication`` is exactly right. An AST never sees a string
literal as code, so the data and the code are cleanly separable. The last test
below asserts the pattern is still present as data in the settings module, so
the scan cannot pass vacuously by having nothing to ignore.

Run with::

    PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/test_qt_app_singleton.py
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Iterator, List

from . import qt_app

_TESTS_ROOT = Path(__file__).resolve().parent
_SETTINGS_TEST_FILE = _TESTS_ROOT / "test_qt_settings_app.py"


def _has_pyside6() -> bool:
    try:
        import PySide6.QtCore  # noqa: F401
    except ImportError:
        return False
    return True


_HAS_PYSIDE6 = _has_pyside6()

_SKIP_REASON = (
    "PySide6-Essentials not installed - shared Qt application not verified here"
)


def _find_bare_qguiapplication(source: str) -> List[int]:
    """Line numbers of in-process ``QGuiApplication(...)`` calls.

    One entry per line, so two constructions on a single line are reported
    once - the point is to name the line a human must fix, not to count calls.
    ``QGuiApplication.instance()`` is an attribute access, not a construction,
    so a pure consumer is correctly left alone.
    """
    hits: List[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            name = None
        if name == "QGuiApplication":
            hits.append(node.lineno)
    return sorted(set(hits))


def _test_modules() -> Iterator[Path]:
    """Every Python module in the suite, including ``tests/windows/``."""
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


@unittest.skipUnless(_HAS_PYSIDE6, _SKIP_REASON)
class SharedApplicationTests(unittest.TestCase):
    """``qt_app.get_application()`` must be one widgets-capable application."""

    def test_get_application_is_a_widgets_capable_singleton(self):
        from PySide6.QtWidgets import QApplication

        app = qt_app.get_application()
        self.assertIsInstance(
            app,
            QApplication,
            "get_application() must return a QApplication (a superset of "
            "QGuiApplication), or QWidget-family classes abort the run",
        )
        self.assertIs(qt_app.get_application(), app, "not a singleton")
        self.assertIs(QApplication.instance(), app)

    def test_a_widget_can_actually_be_constructed_on_the_shared_application(self):
        # This is the assertion that would have caught the original failure.
        # If the shared application were a bare QGuiApplication, constructing
        # a QWidget here does not raise - it aborts the whole process with
        # 0xC0000409 and no traceback, which is precisely why the guard has to
        # be an executable test rather than a code review.
        from PySide6.QtWidgets import QMenu, QMessageBox, QWidget

        app = qt_app.get_application()
        widgets = [QWidget(), QMenu(), QMessageBox()]
        try:
            self.assertTrue(widgets, "widgets constructed")
            for widget in widgets:
                self.assertIs(widget.parent(), None)
        finally:
            for widget in widgets:
                widget.deleteLater()
            app.processEvents()

    def test_the_shared_application_also_satisfies_a_qguiapplication_consumer(self):
        from PySide6.QtGui import QGuiApplication

        app = qt_app.get_application()
        # Code written against QGuiApplication must keep working unchanged.
        self.assertIsInstance(app, QGuiApplication)
        self.assertIs(QGuiApplication.instance(), app)


class BareQGuiApplicationScanTests(unittest.TestCase):
    """The AST scanner, and the invariant it enforces across the suite."""

    def test_the_scanner_detects_direct_construction(self):
        self.assertEqual(_find_bare_qguiapplication("app = QGuiApplication([])"), [1])
        self.assertEqual(
            _find_bare_qguiapplication("app = QGuiApplication.instance() or QGuiApplication([])"),
            [1],
            "the 'or QGuiApplication([])' fallback still constructs one when no "
            "instance exists yet - it must be flagged",
        )

    def test_the_scanner_ignores_qguiapplication_inside_string_literals(self):
        source = "\n".join(
            [
                "probe = '''",
                "app = QGuiApplication.instance() or QGuiApplication([])",
                "'''",
            ]
        )
        self.assertEqual(_find_bare_qguiapplication(source), [])

    def test_the_scanner_reports_each_line_once_even_with_two_constructions(self):
        source = "a = QGuiApplication([]); b = QGuiApplication([])"
        self.assertEqual(_find_bare_qguiapplication(source), [1])

    def test_the_scanner_allows_a_qguiapplication_consumer(self):
        # Reading the existing application never creates one.
        self.assertEqual(_find_bare_qguiapplication("app = QGuiApplication.instance()"), [])
        self.assertEqual(_find_bare_qguiapplication("if QGuiApplication.instance(): pass"), [])

    def test_no_test_module_constructs_a_bare_qguiapplication_in_process(self):
        offenders = []
        scanned = 0
        for path in _test_modules():
            scanned += 1
            for line in _find_bare_qguiapplication(path.read_text(encoding="utf-8")):
                text = path.read_text(encoding="utf-8").splitlines()[line - 1].strip()
                offenders.append(f"{path.relative_to(_TESTS_ROOT)}:{line}: {text}")

        self.assertGreater(scanned, 10, "the scan found suspiciously few modules")
        self.assertEqual(
            offenders,
            [],
            "these test modules construct a Qt application directly instead of "
            "calling tests.qt_app.get_application(); a bare QGuiApplication "
            "cannot host QWidget classes and aborts the rest of the run with "
            "0xC0000409:\n  " + "\n  ".join(offenders),
        )

    def test_the_settings_module_still_contains_such_a_script_as_data(self):
        # Guards against a vacuous pass: if the probe scripts ever stopped
        # containing the pattern, the scan above would prove nothing about
        # whether it can tell data from code.
        source = _SETTINGS_TEST_FILE.read_text(encoding="utf-8")
        self.assertIn(
            "QGuiApplication(",
            source,
            "expected the settings test module to still hold probe scripts that "
            "construct a QGuiApplication as data",
        )
        self.assertEqual(
            _find_bare_qguiapplication(source),
            [],
            "the same file must contain no in-process construction - the scanner "
            "has to separate the two",
        )
