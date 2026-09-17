"""Guards against positionally-selected UI elements in the Qt probe scripts.

Why this file exists
--------------------

The QML-interaction tests drive the real ``main.qml`` from a child process and
select controls by objectName. That works - until someone selects one by a
hard-coded index instead::

    tab_bar.setProperty("currentIndex", 1)

The General tab was later inserted at index 0, which shifted the 按键映射 page
from index 1 to 2. The stale index then selected the *Connection* page, the
ComboBox under test stayed on an inactive StackLayout page (present, with
geometry, but ``visible: false``), the synthesized click landed on the window's
content control, and the failure surfaced ~20 lines later as
``click did not focus the ComboBox`` - a message that points nowhere near the
real cause.

So: a probe that switches tabs must select its tab by objectName. A *runtime*
resolved index (``model.index_of(button_id)``) is fine - it is derived from
data, not from an assumption about layout order.

The scan works on raw text, not an AST, because the probe scripts live in
string literals and never appear as code to a parser. Whole-line comments are
blanked first, so a pattern *quoted* in a comment is not mistaken for a use of
it. This file is the only exclusion, because it necessarily contains the buggy
pattern as a fixture.

Run with::

    PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/test_ui_probe_stable_selectors.py
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

_TESTS_ROOT = Path(__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()
_SETTINGS_TEST_FILE = _TESTS_ROOT / "test_qt_settings_app.py"

# ``.setProperty("currentIndex", 1)`` / ``.setProperty('currentIndex', 1)``
_INDEX_ARGUMENT = re.compile(
    r"""\.setProperty\(\s*(?P<q>["'])currentIndex(?P=q)\s*,\s*(?P<index>\d+)\s*\)"""
)

# ``controls.currentIndex = 1``
_INDEX_ASSIGNMENT = re.compile(r"""\.currentIndex\s*=\s*(?P<index>\d+)\b""")

# The objectName a tab-switching probe must drive instead.
_TAB_BUTTON_OBJECT_NAME = "mappingTabButton"


def _blank_whole_line_comments(source: str) -> str:
    """Replace whole-line comments with empty lines, keeping line numbers.

    Only whole-line comments are removed. Deciding whether a ``#`` starts a
    trailing comment needs string-literal awareness, and a naive split would
    corrupt values like ``"#ffffff"`` - and every comment in this suite that
    quotes a selector sits on its own line.
    """
    return "\n".join(
        "" if line.lstrip().startswith("#") else line for line in source.splitlines()
    )


def _find_hard_coded_index_uses(source: str) -> List[Tuple[int, str]]:
    """``(line_number, text)`` for every numeric UI index in ``source``."""
    cleaned = _blank_whole_line_comments(source)
    hits: List[Tuple[int, str]] = []
    for lineno, line in enumerate(cleaned.splitlines(), start=1):
        if _INDEX_ARGUMENT.search(line) or _INDEX_ASSIGNMENT.search(line):
            hits.append((lineno, line.strip()))
    return hits


def _excluded_files() -> Set[Path]:
    """Files the scan deliberately skips. Only this one, by construction."""
    return {_THIS_FILE}


def _scanned_files() -> Iterator[Path]:
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.resolve() in _excluded_files():
            continue
        yield path


def _probe_scripts() -> Dict[str, str]:
    """Every module-level ``*_PROBE_SCRIPT`` string in the settings test."""
    scripts: Dict[str, str] = {}
    tree = ast.parse(_SETTINGS_TEST_FILE.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.endswith("_PROBE_SCRIPT"):
                scripts[target.id] = value.value
    return scripts


class NoPositionalUiIndexTests(unittest.TestCase):
    """The scanner, and the invariant it enforces across the probe scripts."""

    def test_the_scanner_flags_the_historical_bug(self):
        # The exact line whose stale index cost an afternoon.
        bug = 'tab_bar.setProperty("currentIndex", 1)'
        self.assertEqual(_find_hard_coded_index_uses(bug), [(1, bug)])

    def test_the_scanner_does_not_flag_a_runtime_resolved_index(self):
        # Derived from data, not from an assumption about layout order.
        for line in (
            'mapping_list.setProperty("currentIndex", model.index_of(button_id))',
            'mapping_list.setProperty("currentIndex", index)',
            'view.setProperty("currentIndex", self.row_count() - 1)',
        ):
            with self.subTest(line=line):
                self.assertEqual(_find_hard_coded_index_uses(line), [])

    def test_the_scanner_flags_attribute_assignment_too(self):
        # QML properties can also be driven by assignment, so matching only
        # setProperty() would leave an obvious way around the guard.
        self.assertEqual(
            _find_hard_coded_index_uses("controls.currentIndex = 3"),
            [(1, "controls.currentIndex = 3")],
        )
        self.assertEqual(
            _find_hard_coded_index_uses('controls.setProperty("currentIndex", 0)'),
            [(1, 'controls.setProperty("currentIndex", 0)')],
        )

    def test_the_scanner_ignores_the_pattern_quoted_in_a_comment(self):
        source = "\n".join(
            [
                "# The historical bug was:",
                '#     tab_bar.setProperty("currentIndex", 1)',
                "# which selected the wrong page once General was added.",
            ]
        )
        self.assertEqual(_find_hard_coded_index_uses(source), [])

    def test_the_scan_inspects_the_real_probe_scripts(self):
        # Guards against a scan that passes by reading nothing.
        scanned = list(_scanned_files())
        self.assertGreater(len(scanned), 20, "the scan found suspiciously few modules")
        total_bytes = sum(path.stat().st_size for path in scanned)
        self.assertGreater(total_bytes, 100_000, "the scan read suspiciously little")

        scripts = _probe_scripts()
        self.assertGreaterEqual(
            len(scripts), 5, f"expected the settings module's probe scripts, got {sorted(scripts)}"
        )
        self.assertIn("_DIRECT_SAVE_PROBE_SCRIPT", scripts)

    def test_the_only_excluded_file_is_this_one(self):
        self.assertEqual(_excluded_files(), {_THIS_FILE})
        scanned = {path.resolve() for path in _scanned_files()}
        self.assertNotIn(_THIS_FILE, scanned, "this file must exclude itself")
        self.assertIn(
            _SETTINGS_TEST_FILE.resolve(),
            scanned,
            "the module that actually holds the probe scripts must be scanned",
        )

    def test_no_probe_selects_a_ui_element_by_a_hard_coded_index(self):
        offenders: List[str] = []
        for path in _scanned_files():
            for lineno, text in _find_hard_coded_index_uses(
                path.read_text(encoding="utf-8")
            ):
                offenders.append(f"{path.relative_to(_TESTS_ROOT)}:{lineno}: {text}")
        self.assertEqual(
            offenders,
            [],
            "these UI probes select a control by a hard-coded index; select it by "
            "objectName instead, or resolve the index at runtime - a stale index "
            "silently drives the wrong page and the failure surfaces far away "
            "from the cause:\n  " + "\n  ".join(offenders),
        )

    def test_every_tab_switching_probe_selects_its_tab_by_object_name(self):
        checked = 0
        for name, body in sorted(_probe_scripts().items()):
            if '"tabBar"' not in body:
                continue
            checked += 1
            with self.subTest(probe=name):
                self.assertIn(
                    _TAB_BUTTON_OBJECT_NAME,
                    body,
                    f"{name} switches tabs but never names {_TAB_BUTTON_OBJECT_NAME}",
                )
                self.assertIn(
                    "checked",
                    body,
                    f"{name} must drive the tab via the TabButton's checked "
                    f"property rather than the TabBar's index",
                )
                self.assertEqual(
                    _find_hard_coded_index_uses(body),
                    [],
                    f"{name} still contains a hard-coded tab index",
                )
        self.assertGreaterEqual(
            checked, 3, "expected at least three tab-switching probes to inspect"
        )


if __name__ == "__main__":
    unittest.main()
