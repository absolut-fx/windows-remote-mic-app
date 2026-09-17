"""The one Qt application every test in this suite must share.

Why this module exists
----------------------

``unittest discover`` runs every test module in a **single** process, and the
first module that needs a Qt application fixes its *type* for the whole run.
A bare ``QGuiApplication`` cannot host QWidget-family classes, so as soon as a
later test constructs a ``QMenu``/``QMessageBox`` Qt calls ``qFatal`` ->
``abort``: the run dies with exit code ``0xC0000409``, printing no traceback
and no test report at all. That failure mode cost real time here - the suite
simply vanished.

So never construct an application in a test. Ask for it::

    from . import qt_app

    app = qt_app.get_application()

The returned object is always a ``QApplication``. That is a *superset* of
``QGuiApplication``, so existing code written against ``QGuiApplication`` -
including ``QGuiApplication.instance()`` and ``QGuiApplication`` type checks -
keeps working unchanged.

``tests/test_qt_app_singleton.py`` enforces both halves of this: that the
shared application really can host a widget, and that no test module anywhere
under ``tests/`` constructs a bare ``QGuiApplication`` in process (the probe
*scripts* held in string literals may, because they run in their own
subprocess - an AST scan never sees them as code).
"""

from __future__ import annotations

import os
from typing import Optional

#: The process-wide application, created on first use. Kept module-private so
#: the only way in is ``get_application()``.
_application: Optional[object] = None


def get_application():
    """Return the shared, widgets-capable Qt application.

    Creates it on first call and returns the same object thereafter. Importing
    PySide6 happens here rather than at module import so that merely importing
    this helper never pulls Qt into a process that does not want it.
    """
    global _application
    if _application is not None:
        return _application

    # Must be set before the application is constructed: a test run has no
    # display server/compositor, and Qt aborts if asked for a real one.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    existing = QCoreApplication.instance()
    if existing is not None:
        if not isinstance(existing, QApplication):
            # Someone built a bare QGuiApplication (or a QCoreApplication)
            # before us. Returning it would hand back an object that cannot
            # host a widget, which is the exact trap this module exists to
            # close - so say so plainly instead of aborting 200 tests later.
            raise RuntimeError(
                "a non-widgets Qt application already exists in this process "
                f"({type(existing).__name__}); it was created by a test module "
                "that constructed one directly. Use tests.qt_app."
                "get_application() instead of constructing QGuiApplication - a "
                "bare QGuiApplication cannot host QWidget classes and makes the "
                "rest of the run abort with 0xC0000409."
            )
        _application = existing
        return _application

    _application = QApplication([])
    return _application
