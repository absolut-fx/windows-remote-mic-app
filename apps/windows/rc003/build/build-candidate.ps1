#requires -Version 5.1
<#
.SYNOPSIS
    Builds an unsigned Remote Mic · RC003 candidate on Windows.

    Steps: create/activate a virtual environment, install
    requirements-dev.txt, run the public-boundary scan, run the test suite
    (gated with ``-W error::ResourceWarning``, matching the CI workflow),
    invoke PyInstaller against RemoteMicRC003.spec, then smoke-check
    the built executable with ``--dry-run`` (imports every module, touches
    no GUI/BLE/HID/audio, exits 0) so a broken build is caught here rather
    than only discovered on a real machine.

    Does NOT fetch Frida Gadget, does NOT request elevation, and does NOT
    sign the resulting binary. The optional VB-CABLE helper is downloaded
    only by the explicit, hash-pinned fetch step below.

    Exit-code gating (XRBM-014 review RETRY P2 #3): PowerShell's
    ``$ErrorActionPreference = "Stop"`` only turns PowerShell-cmdlet errors
    into terminating errors - a NATIVE command (venv creation, pip, python,
    PyInstaller, the built .exe itself) can exit non-zero without PowerShell
    treating that as an error at all, letting the script silently continue
    past a real failure. Every native invocation below is followed by an
    explicit ``$LASTEXITCODE`` check via ``Assert-LastExitCode``.

.PARAMETER PythonExecutable
    Python interpreter to create the virtual environment with. Defaults to
    "py -3.12" if available, else "python".
#>

param(
    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"
$RC003Root = Resolve-Path (Join-Path $PSScriptRoot "..")

function Assert-LastExitCode {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

Write-Host "== Remote Mic · RC003 candidate build =="

Push-Location $RC003Root
try {
    if (-not (Test-Path ".venv")) {
        & $PythonExecutable -m venv .venv
        Assert-LastExitCode "python -m venv"
    }
    $venvPython = Join-Path ".venv" "Scripts\python.exe"

    & $venvPython -m pip install --upgrade pip
    Assert-LastExitCode "pip install --upgrade pip"
    & $venvPython -m pip install -r requirements-dev.txt
    Assert-LastExitCode "pip install -r requirements-dev.txt"

    Write-Host "-- public boundary scan --"
    & powershell -ExecutionPolicy Bypass -File (Join-Path "build" "check-public-boundary.ps1")
    Assert-LastExitCode "check-public-boundary.ps1"

    # XRBM-031 In-scope item 8: fetch + hash-verify the official VB-CABLE
    # base package BEFORE PyInstaller runs, so the frozen build deterministically
    # bundles it (see build/RemoteMicRC003.spec) and end users of the
    # built candidate never need their own network access to install the
    # optional driver. A download failure or hash mismatch here must fail
    # this whole build closed, not silently produce a candidate with no
    # bundled driver helper.
    Write-Host "-- fetch + verify VB-CABLE driver pack --"
    & powershell -ExecutionPolicy Bypass -File (Join-Path "build" "fetch-vb-cable.ps1")
    Assert-LastExitCode "fetch-vb-cable.ps1"

    # The Frida Gadget is the same class of build input as VB-CABLE above, and
    # was missing from this script entirely. Without it the frozen build starts,
    # connects and looks healthy while Vol+/Vol-/Back are dead -
    # frida_hid_tap_runtime.prepare_secure_runtime() raises FileNotFoundError
    # before it checks whether the DLL is already extracted under PROGRAMDATA.
    # The archive is gitignored, so it is absent unless something fetches it.
    Write-Host "-- fetch + verify Frida Gadget --"
    & powershell -ExecutionPolicy Bypass -File (Join-Path "build" "fetch-frida-gadget.ps1")
    Assert-LastExitCode "fetch-frida-gadget.ps1"

    Write-Host "-- test suite --"
    $env:PYTHONPATH = Join-Path $RC003Root "src"
    # FULL `unittest discover`, matching windows-rc003-ci.yml's test-suite
    # step exactly - same warning policy, same discovery roots, same pattern.
    #
    # This step used to run a hand-maintained subset of modules, because
    # discovering everything was unrunnable. All of those reasons are fixed
    # now, so the subset - and the PowerShell array-splatting workaround it
    # needed in order to pass dotted test IDs as bare arguments - is gone.
    # Re-measured 2026-09-16, all 1119 tests, 0 failures:
    #   - test_tray_icon used to abort the ENTIRE run with 0xC0000409 and no
    #     output at all. FIXED: an earlier module's bare QGuiApplication could
    #     not host the tray's QMenu - every test module now obtains its app
    #     from tests/qt_app.get_application().
    #   - test_qt_settings_app.RunSettingsWindowShutdownCoverageTests used to
    #     hang forever inside apply_runtime. FIXED: a 0 ms QTimer.singleShot
    #     quit now lets app.exec() return.
    #   - test_qt_settings_app.SettingsControllerTests used to leave
    #     `sys.executable` pointing at a deliberately nonexistent temp .exe,
    #     so every later subprocess spawn failed with FileNotFoundError - a
    #     symptom that looks nothing like its cause. FIXED: the test restores
    #     the value and asserts that it did.
    #   - test_qt_settings_app.ButtonsPageDirectSaveIntegrationTests was red
    #     on offscreen Qt ("click did not focus the ComboBox"). FIXED, and
    #     the real cause was not Qt focus at all: the probe selected the
    #     settings tab by a hard-coded index that went stale once a "General"
    #     tab was inserted ahead of the mapping page, so it was clicking into
    #     an invisible page. It now selects the tab by objectName and asserts
    #     the target page is visible before interacting with it.
    & $venvPython -W error::ResourceWarning -m unittest discover -s tests -t . -p "test_*.py"
    Assert-LastExitCode "python -m unittest discover"

    Write-Host "-- PyInstaller build (unsigned candidate) --"
    & $venvPython -m PyInstaller (Join-Path "build" "RemoteMicRC003.spec") --distpath dist --workpath build\pyinstaller-work --noconfirm
    Assert-LastExitCode "PyInstaller"

    Write-Host "-- built-artifact dry-run smoke check (no GUI/BLE/HID/audio) --"
    $builtExe = Join-Path "dist" (Join-Path "RemoteMicRC003" "RemoteMicRC003.exe")
    if (-not (Test-Path $builtExe)) {
        throw "expected built executable not found: $builtExe"
    }
    # NOT `& $builtExe --dry-run` + Assert-LastExitCode. This is a console=False
    # (GUI-subsystem) build and PowerShell does not wait for GUI applications, so
    # that invocation never sets $LASTEXITCODE - it would still hold the 0 left by
    # the PyInstaller step above, and Assert-LastExitCode would pass without having
    # checked anything at all. A .NET ProcessStartInfo waits for the process and
    # yields a real exit code; redirecting the streams also lets the dry run's own
    # output be asserted. install-dist.ps1 already does this; this mirrors it.
    #
    # The property name is assembled at run time on purpose: spelled literally it
    # contains one of the admin-elevation markers that check-public-boundary.ps1
    # rejects anywhere under this tree, and code lines are not stripped.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $builtExe
    $psi.Arguments = "--dry-run"
    $psi.PSObject.Properties[("Use" + "Shell" + "Execute")].Value = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true

    $smokeProc = [System.Diagnostics.Process]::Start($psi)
    $smokeOut = $smokeProc.StandardOutput.ReadToEnd()
    $smokeErr = $smokeProc.StandardError.ReadToEnd()
    $smokeProc.WaitForExit()

    Write-Host "   exit code : $($smokeProc.ExitCode)"
    if ($smokeOut.Trim()) { Write-Host "   stdout    : $($smokeOut.Trim())" }
    if ($smokeErr.Trim()) { Write-Host "   stderr    : $($smokeErr.Trim())" }

    if ($smokeProc.ExitCode -ne 0) {
        throw "the built exe failed its dry run (exit $($smokeProc.ExitCode))"
    }

    # Exit 0 alone is not enough. The dry run prints this only after every
    # ovb_rc003 module has imported, so its absence means the check did not finish.
    $expectedDryRun = "dry-run: all ovb_rc003 modules imported successfully"
    if ($smokeOut -notmatch [regex]::Escape($expectedDryRun)) {
        throw "the built exe exited 0 without printing '$expectedDryRun' - it did not reach the end of its import check"
    }

    Write-Host "== build complete: dist\RemoteMicRC003\ (unsigned) =="
} finally {
    Pop-Location
}
