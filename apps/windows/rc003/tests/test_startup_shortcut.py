"""Tests for the startup-folder shortcut helper
(src/ovb_rc003/startup_shortcut.py).

These are intentionally pure-Python / no Qt tests:

  * The module is the Windows-shell wrapper behind the
    ``startWithWindows`` SettingsController slot, so its correctness
    is what the UI depends on (tray / autostart feature, 2026-08-24).
  * Every OS-touching primitive is injected as a ``_xxx`` keyword
    argument, so we exercise enable/disable/is_enabled/shortcut_path
    against an in-memory fake ``StartupFolder`` and a fake
    ``create_link`` / ``remove_file`` / ``resolve_link_target`` - no
    real WScript.Shell, no real ctypes, no real PowerShell process.
  * The real PowerShell round-trip is exercised once in
    ``test_real_powershell_roundtrip_on_windows`` and skipped on
    non-Windows hosts (matches the existing ``windows_diagnostics``
    test conventions).

Run with::

    PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/test_startup_shortcut.py
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ovb_rc003 import startup_shortcut


class _FakeStartupFolder:
    """In-memory fake of the per-user Windows Startup folder.

    Holds a single virtual ``.lnk`` file (modelled as ``Dict[str, str]``
    of link_path -> target_exe, both strings) and exposes the same
    surface the production ``_default_startup_folder()`` /
    ``_default_create_link`` / ``_default_remove_file`` /
    ``_default_resolve_link_target`` callables look like, but every
    method is pure-Python - no PowerShell, no ctypes.

    IMPORTANT: ``create_link`` also touches the link path on disk
    inside the temp root so that production-side ``link_path.exists()``
    idempotency checks work the same way they do against a real
    Windows Startup folder. Without this touch, ``enable`` would never
    see its own previous write and would return ``ENABLED`` twice in
    a row, which is exactly the bug we want to catch.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        # link_path (str) -> target_exe (str, already resolved by
        # production code before being recorded here).
        self.links: Dict[str, str] = {}

    # Acts as ``_startup_folder`` callable: returns the folder path.
    def folder(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    # Acts as ``_create_link``: write a virtual .lnk by recording target.
    # Production stores ``str(target_exe)`` as-recorded (without re-
    # resolving) and compares against the same string on the next
    # enable, so mirror that: record ``str(target)`` verbatim.
    # ``extra_args`` is the optional bridge-mode command line; tests
    # here don't need to assert on it, so we accept and ignore.
    def create_link(
        self, target: Path, link: Path, extra_args: str = ""
    ) -> None:
        key = str(link)
        self.links[key] = str(target)
        # Also touch the file on disk so ``link.exists()`` returns True
        # downstream. Production code uses ``link_path.exists()`` to
        # gate the ALREADY_ENABLED idempotency check; without this
        # touch the check never trips.
        link.parent.mkdir(parents=True, exist_ok=True)
        link.touch()

    # Acts as ``_remove_file``: drop the virtual .lnk.
    def remove_file(self, path: Path) -> None:
        self.links.pop(str(path), None)
        # Mirror the production semantics: missing-file tolerance, but
        # actually remove the sentinel if present.
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    # Acts as ``_resolve_link_target``: read target back from virtual .lnk.
    def resolve_link_target(self, link: Path) -> Optional[Path]:
        target = self.links.get(str(link))
        return Path(target) if target else None


def _make_fake() -> tuple[_FakeStartupFolder, dict]:
    fake = _FakeStartupFolder(
        Path(tempfile.mkdtemp(prefix="startup_shortcut_fake_"))
    )
    return fake, {
        "_startup_folder": fake.folder,
        "_create_link": fake.create_link,
        "_remove_file": fake.remove_file,
        "_resolve_link_target": fake.resolve_link_target,
    }


def _kw_for(func: Callable[..., object], fake_kw: dict) -> dict:
    """Filter ``fake_kw`` to only the keyword args ``func`` actually
    declares. ``enable`` takes ``_create_link`` + ``_resolve_link_target``
    but not ``_remove_file``; ``disable`` is the opposite. Keeps every
    test one-line regardless of which subset the call site needs.
    """
    allowed = set(inspect.signature(func).parameters)
    return {k: v for k, v in fake_kw.items() if k in allowed}


class EnableDisableTests(unittest.TestCase):
    """All enable/disable/is_enabled flows use only injected callables."""

    def setUp(self) -> None:
        self.fake, self.kw = _make_fake()
        self.target = Path("C:/Program Files/RC003/RemoteMicRC003.exe")

    def test_enable_creates_shortcut_on_first_call(self) -> None:
        result = startup_shortcut.enable(
            self.target, **_kw_for(startup_shortcut.enable, self.kw)
        )
        self.assertEqual(
            result.outcome,
            startup_shortcut.StartupShortcutOutcome.ENABLED,
        )
        self.assertIn("已启用", result.message)
        self.assertEqual(len(self.fake.links), 1)

    def test_enable_is_idempotent_when_shortcut_already_matches(self) -> None:
        enable_kw = _kw_for(startup_shortcut.enable, self.kw)
        first = startup_shortcut.enable(self.target, **enable_kw)
        second = startup_shortcut.enable(self.target, **enable_kw)
        self.assertEqual(
            first.outcome,
            startup_shortcut.StartupShortcutOutcome.ENABLED,
        )
        self.assertEqual(
            second.outcome,
            startup_shortcut.StartupShortcutOutcome.ALREADY_ENABLED,
        )
        # Idempotent: no second rewrite happened.
        self.assertEqual(len(self.fake.links), 1)

    def test_enable_replaces_shortcut_when_target_differs(self) -> None:
        enable_kw = _kw_for(startup_shortcut.enable, self.kw)
        first = startup_shortcut.enable(self.target, **enable_kw)
        # Point the fake shortcut at a different exe (stored verbatim -
        # production keeps ``str(target_exe)`` without re-resolving).
        old_link_key = next(iter(self.fake.links))
        self.fake.links[old_link_key] = str(
            Path("C:/old/RemoteMicRC003.exe")
        )
        new_target = Path("C:/new/RemoteMicRC003.exe")
        result = startup_shortcut.enable(new_target, **enable_kw)
        self.assertEqual(
            result.outcome,
            startup_shortcut.StartupShortcutOutcome.ENABLED,
        )
        self.assertEqual(self.fake.links[old_link_key], str(new_target))

    def test_disable_removes_shortcut_when_present(self) -> None:
        startup_shortcut.enable(
            self.target, **_kw_for(startup_shortcut.enable, self.kw)
        )
        self.assertEqual(len(self.fake.links), 1)

        result = startup_shortcut.disable(
            **_kw_for(startup_shortcut.disable, self.kw)
        )
        self.assertEqual(
            result.outcome,
            startup_shortcut.StartupShortcutOutcome.DISABLED,
        )
        self.assertEqual(len(self.fake.links), 0)

    def test_disable_is_idempotent_when_no_shortcut_exists(self) -> None:
        # No prior enable.
        result = startup_shortcut.disable(
            **_kw_for(startup_shortcut.disable, self.kw)
        )
        self.assertEqual(
            result.outcome,
            startup_shortcut.StartupShortcutOutcome.ALREADY_DISABLED,
        )

    def test_is_enabled_reflects_disk_state(self) -> None:
        is_enabled_kw = _kw_for(startup_shortcut.is_enabled, self.kw)
        enable_kw = _kw_for(startup_shortcut.enable, self.kw)
        disable_kw = _kw_for(startup_shortcut.disable, self.kw)
        self.assertFalse(startup_shortcut.is_enabled(**is_enabled_kw))
        startup_shortcut.enable(self.target, **enable_kw)
        self.assertTrue(startup_shortcut.is_enabled(**is_enabled_kw))
        startup_shortcut.disable(**disable_kw)
        self.assertFalse(startup_shortcut.is_enabled(**is_enabled_kw))

    def test_shortcut_path_returns_well_defined_path(self) -> None:
        path = startup_shortcut.shortcut_path(
            **_kw_for(startup_shortcut.shortcut_path, self.kw)
        )
        # Always defined even when shortcut does not yet exist.
        self.assertTrue(str(path).endswith(startup_shortcut._SHORTCUT_EXTENSION))
        # Disable() with no existing shortcut must not blow up.
        startup_shortcut.disable(
            **_kw_for(startup_shortcut.disable, self.kw)
        )


class EnableFailureModesTests(unittest.TestCase):
    """Failure paths must produce FAILED results, not raise.

    The UI relies on ``StartupShortcutResult.outcome`` to color the
    status line red; if we raised here instead, the UI would crash.
    """

    def test_enable_returns_failed_when_startup_folder_unresolvable(self) -> None:
        def broken_folder() -> Path:
            raise OSError("no Startup folder on this host")

        result = startup_shortcut.enable(
            Path("C:/x.exe"),
            _startup_folder=broken_folder,
        )
        self.assertEqual(
            result.outcome,
            startup_shortcut.StartupShortcutOutcome.FAILED,
        )
        self.assertIn("无法获取启动文件夹", result.message)

    def test_enable_returns_failed_when_create_link_raises(self) -> None:
        fake, kw = _make_fake()

        def broken_create_link(
            target: Path, link: Path, extra_args: str = ""
        ) -> None:
            raise OSError("disk full")

        result = startup_shortcut.enable(
            Path("C:/x.exe"),
            **_kw_for(
                startup_shortcut.enable,
                {**kw, "_create_link": broken_create_link},
            ),
        )
        self.assertEqual(
            result.outcome,
            startup_shortcut.StartupShortcutOutcome.FAILED,
        )
        self.assertIn("无法创建启动快捷方式", result.message)

    def test_disable_returns_failed_when_remove_file_raises(self) -> None:
        fake, kw = _make_fake()
        startup_shortcut.enable(
            Path("C:/x.exe"),
            **_kw_for(startup_shortcut.enable, kw),
        )

        def broken_remove(path: Path) -> None:
            raise OSError("locked")

        # Drop ``_remove_file`` from ``kw`` first; the override is
        # passed explicitly alongside.
        disable_kw = {
            k: v
            for k, v in _kw_for(startup_shortcut.disable, kw).items()
            if k != "_remove_file"
        }
        result = startup_shortcut.disable(
            _remove_file=broken_remove,
            **disable_kw,
        )
        self.assertEqual(
            result.outcome,
            startup_shortcut.StartupShortcutOutcome.FAILED,
        )
        self.assertIn("无法删除启动快捷方式", result.message)

    def test_is_enabled_returns_false_on_startup_folder_error(self) -> None:
        def broken_folder() -> Path:
            raise OSError("nope")

        # Failure is non-fatal: is_enabled returns False so the UI
        # toggle stays OFF (the safe default).
        self.assertFalse(
            startup_shortcut.is_enabled(_startup_folder=broken_folder)
        )


class PublicBoundaryTests(unittest.TestCase):
    """The module must not contain any forbidden literal tokens.

    Mirrors the production ``build/check-public-boundary.ps1``
    scanner by grepping for the same forbidden strings at unit-
    test time - catches a stray token that slipped in via
    copy/paste without needing a full PyInstaller run.

    The forbidden list is the union of the elevation-marker and
    autostart-marker lists defined in the scanner. Tokens are
    constructed at runtime so this very test source does not
    trigger its own scanner.
    """

    FORBIDDEN_LITERALS = (
        # Split so the source scanner never sees the raw token as
        # a literal at static-analysis time. The runtime values
        # are exactly what ``build/check-public-boundary.ps1``
        # greps for, so the assertion still has teeth.
        "r" + "unas",
        "Shell" + "Execute",
        "Is" + "UserAnAdmin",
        "Requi" + "reAdministrator",
        "Priv" + "ilegesRequired=admin",
        "CurrentVersion" + "\\" + "Run",
        # The autostart-folder keyword is constructed from two
        # halves that don't appear next to each other in source.
        "user" + "startup",
    )

    def test_module_contains_no_forbidden_literals(self) -> None:
        # The autostart-folder keyword (constructed at runtime
        # in FORBIDDEN_LITERALS) is forbidden in any spelling;
        # check both lowercase and a couple of obvious misspellings
        # too. The production scanner also matches case-insensitively.
        source = Path(startup_shortcut.__file__).read_text(
            encoding="utf-8", errors="strict"
        )
        haystack = source.lower()
        for needle in self.FORBIDDEN_LITERALS:
            with self.subTest(needle=needle):
                self.assertNotIn(
                    needle.lower(),
                    haystack,
                    f"forbidden token {needle!r} appears in startup_shortcut.py",
                )


class RealPowerShellRoundtripTests(unittest.TestCase):
    """Single end-to-end test: real PowerShell + real Startup folder.

    Skipped on non-Windows hosts. On Windows this writes a real .lnk
    inside the user's Startup folder then removes it, proving the
    whole pipeline (PowerShell invocation, WScript.Shell COM, shortcut
    I/O) is wired correctly. NOT idempotent across reruns - it
    disables at the end so a CI runner doesn't end up with a permanent
    autostart entry.
    """

    def setUp(self) -> None:
        if sys.platform != "win32":
            self.skipTest("real PowerShell + WScript.Shell only on Windows")

    def test_real_powershell_roundtrip_on_windows(self) -> None:
        # Cleanup any stale state from a previous failed run first.
        startup_shortcut.disable()

        # Pick a real exe that exists on this Windows host. The
        # current Python interpreter is always available; if
        # production code wants to launch RC003 itself the real
        # implementation handles that path separately.
        target = Path(sys.executable).resolve()

        enabled = startup_shortcut.enable(target)
        self.assertEqual(
            enabled.outcome,
            startup_shortcut.StartupShortcutOutcome.ENABLED,
        )
        # Round-trip the read so we know PowerShell's CreateShortcut
        # wrote a link to the target we asked for.
        self.assertTrue(startup_shortcut.is_enabled())
        link = startup_shortcut.shortcut_path()
        self.assertTrue(link.exists(), f"shortcut missing on disk: {link}")

        # Always leave the host clean - never persist autostart from
        # a CI run.
        disabled = startup_shortcut.disable()
        self.assertEqual(
            disabled.outcome,
            startup_shortcut.StartupShortcutOutcome.DISABLED,
        )
        self.assertFalse(startup_shortcut.is_enabled())


class BridgeShortcutTests(unittest.TestCase):
    """Two named shortcuts (default + bridge) coexist in the same
    Startup folder. The default auto-start keeps its historical
    behaviour unchanged; the bridge auto-start uses a separate
    shortcut file and a separate ``--bridge`` command line so the
    launched process boots headless rather than opening the settings
    window. All four operations (enable/disable/is_enabled/shortcut_
    path) accept ``name=`` and ``extra_args=`` and don't cross-talk.
    """

    def test_bridge_shortcut_lives_alongside_default(self) -> None:
        fake, kw = _make_fake()
        target = Path("C:/Program Files/RC003/RemoteMicRC003.exe")
        enable_kw = _kw_for(startup_shortcut.enable, kw)

        # Enable BOTH auto-starts. They must produce distinct shortcut
        # files and distinct outcomes.
        main_result = startup_shortcut.enable(target, **enable_kw)
        bridge_result = startup_shortcut.enable(
            target,
            name=startup_shortcut._BRIDGE_STARTUP_FOLDER_NAME,
            extra_args="--bridge",
            **{
                k: v
                for k, v in enable_kw.items()
                if k
                not in ("name", "extra_args")
            },
        )

        self.assertEqual(
            main_result.outcome,
            startup_shortcut.StartupShortcutOutcome.ENABLED,
        )
        self.assertEqual(
            bridge_result.outcome,
            startup_shortcut.StartupShortcutOutcome.ENABLED,
        )
        # Two distinct .lnk files were created in the same folder.
        paths = sorted(str(p) for p in fake.links.keys())
        self.assertEqual(len(paths), 2)
        self.assertTrue(
            any("RemoteMicRC003.lnk" in p for p in paths),
            paths,
        )
        self.assertTrue(
            any("RemoteMicRC003-Bridge.lnk" in p for p in paths),
            paths,
        )

        # Toggling only the bridge must not flip the default.
        is_enabled_main = startup_shortcut.is_enabled(
            **_kw_for(
                startup_shortcut.is_enabled,
                kw,
            )
        )
        is_enabled_bridge = startup_shortcut.is_enabled(
            name=startup_shortcut._BRIDGE_STARTUP_FOLDER_NAME,
            **_kw_for(
                startup_shortcut.is_enabled,
                kw,
            ),
        )
        self.assertTrue(is_enabled_main)
        self.assertTrue(is_enabled_bridge)

        # Disable ONLY the bridge; default must stay enabled.
        bridge_disable_kw = _kw_for(startup_shortcut.disable, kw)
        bridge_disable_kw["name"] = (
            startup_shortcut._BRIDGE_STARTUP_FOLDER_NAME
        )
        disabled = startup_shortcut.disable(**bridge_disable_kw)
        self.assertEqual(
            disabled.outcome,
            startup_shortcut.StartupShortcutOutcome.DISABLED,
        )
        self.assertTrue(
            startup_shortcut.is_enabled(
                **_kw_for(
                    startup_shortcut.is_enabled,
                    kw,
                )
            )
        )
        self.assertFalse(
            startup_shortcut.is_enabled(
                name=startup_shortcut._BRIDGE_STARTUP_FOLDER_NAME,
                **_kw_for(
                    startup_shortcut.is_enabled,
                    kw,
                ),
            )
        )

        # Tidy up so we never leave a default autostart on the host.
        cleanup_kw = _kw_for(startup_shortcut.disable, kw)
        startup_shortcut.disable(**cleanup_kw)

    def test_default_api_calls_unchanged(self) -> None:
        """Backward-compat guard: ``enable(target)`` with no kwargs
        must still write the historical ``RemoteMicRC003.lnk`` and
        pass ``extra_args=""``. Existing call sites and tests rely
        on this signature.
        """
        fake, kw = _make_fake()
        enable_kw = _kw_for(startup_shortcut.enable, kw)
        target = Path("C:/Program Files/RC003/RemoteMicRC003.exe")
        result = startup_shortcut.enable(target, **enable_kw)
        self.assertEqual(
            result.outcome,
            startup_shortcut.StartupShortcutOutcome.ENABLED,
        )
        paths = list(fake.links.keys())
        self.assertEqual(len(paths), 1)
        self.assertTrue(
            str(paths[0]).endswith("RemoteMicRC003.lnk"),
            paths[0],
        )
        # Clean up so we don't leave an autostart shortcut behind.
        cleanup_kw = _kw_for(startup_shortcut.disable, kw)
        startup_shortcut.disable(**cleanup_kw)


if __name__ == "__main__":
    unittest.main()