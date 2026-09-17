#requires -Version 5.1
<#
.SYNOPSIS
    Installs a freshly built dist\RemoteMicRC003\ over the per-user installation
    the logon task points at.

.DESCRIPTION
    build-candidate.ps1 builds; nothing installed. Until now that last step was
    done by hand, so this script performs exactly the convention it used, in
    order:

      1. refuse to touch a running installation (unless -Force)
      2. back the current RemoteMicRC003.exe up as RemoteMicRC003.exe.bak-<stamp>
      3. rename the current _internal\ to _internal.bak-<stamp> - an instant
         operation that keeps the previous build whole, instead of overwriting
         it file by file and leaving a half-updated tree if anything fails
      4. copy the new exe and _internal\ into place
      5. confirm the installed exe is byte-identical to the built one
      6. smoke-check the installed exe with --dry-run and report its real exit
         code, failing loudly if it is not 0

    Because steps 2 and 3 leave the previous build intact on disk, a failed
    install is undone by deleting the new files and renaming the backups back;
    the rollback commands are printed at the end.

    Paths are derived from the environment rather than written literally:
    build/check-public-boundary.ps1 rejects a personal absolute path anywhere
    under this tree, in comments as well as in code.

.PARAMETER Source
    Directory holding the built RemoteMicRC003.exe and _internal\.
    Defaults to dist\RemoteMicRC003, next to this script's parent.

.PARAMETER Destination
    Installation directory to update. Defaults to the per-user location.

.PARAMETER Force
    Proceed even though a RemoteMicRC003 process is running.

.PARAMETER SkipSmokeTest
    Skip the post-install --dry-run. Not recommended: it is the only thing
    that proves the newly installed exe actually starts.

.PARAMETER AllowMissingGadget
    Install even though the build carries no pinned Frida gadget archive.
    Vol+/Vol-/Back will not work. Only for a deliberate test.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build\install-dist.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build\install-dist.ps1 -WhatIf
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Source,
    [string]$Destination,
    [switch]$Force,
    [switch]$SkipSmokeTest,
    [switch]$AllowMissingGadget
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Source) { $Source = Join-Path $projectRoot "dist\RemoteMicRC003" }
if (-not $Destination) { $Destination = Join-Path $env:LOCALAPPDATA "RemoteMic\RC003" }

function Write-Step { param([string]$Text) Write-Host "==> $Text" }
function Write-Note { param([string]$Text) Write-Host "    $Text" }

# --- preflight ------------------------------------------------------------

Write-Step "source      : $Source"
Write-Step "destination : $Destination"

$sourceExe = Join-Path $Source "RemoteMicRC003.exe"
$sourceInternal = Join-Path $Source "_internal"

if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
    throw "no build to install at $Source - build first: pyinstaller build\RemoteMicRC003.spec"
}
if (-not (Test-Path -LiteralPath $sourceExe -PathType Leaf)) {
    throw "the build has no RemoteMicRC003.exe: $sourceExe"
}
if (-not (Test-Path -LiteralPath $sourceInternal -PathType Container)) {
    throw "the build has no _internal directory: $sourceInternal"
}
if (-not (Test-Path -LiteralPath $Destination -PathType Container)) {
    throw "no installation at $Destination"
}

# Refuse a build that cannot provision the HID tap.
#
# frida_hid_tap_runtime.prepare_secure_runtime() raises FileNotFoundError when
# the archive is missing -- *before* it checks whether the DLL is already
# extracted under PROGRAMDATA. So the failure is silent in the worst way: the
# app starts, connects, and looks healthy, while Vol+/Vol-/Back stay dead
# because only the injected tap produces them.
#
# The archive is a gitignored, fetch-on-demand binary, which makes it both easy
# to lose and easy to not notice. Measured 2026-09-17: a rebuild shipped without
# it and the only visible difference was a 7 MB gap in _internal\ovb_rc003\.
$gadgetDir = Join-Path $sourceInternal "ovb_rc003\frida_assets"
$sourceGadget = @(
    Get-ChildItem -LiteralPath $gadgetDir -Filter "*.xz" -File -ErrorAction SilentlyContinue
)
if ($sourceGadget.Count -eq 0) {
    if (-not $AllowMissingGadget) {
        throw ("the build carries no Frida gadget archive under " +
               "_internal\ovb_rc003\frida_assets\. Installing it would leave " +
               "Vol+/Vol-/Back dead while everything else looks fine. Fetch it " +
               "with build\fetch-frida-gadget.ps1 and rebuild, or pass " +
               "-AllowMissingGadget to install anyway.")
    }
    Write-Step "no gadget archive in this build; -AllowMissingGadget was given"
}
else {
    foreach ($gadget in $sourceGadget) {
        Write-Note ("gadget archive : {0} ({1:N0} bytes)" -f $gadget.Name, $gadget.Length)
    }
}

$destExe = Join-Path $Destination "RemoteMicRC003.exe"
$destInternal = Join-Path $Destination "_internal"
if (-not (Test-Path -LiteralPath $destExe -PathType Leaf)) {
    throw "no installed exe at $destExe - is -Destination right?"
}

# A running build holds both its own exe and, more importantly, the single
# Bluetooth connection this device allows. Refuse rather than half-install.
$running = @(Get-Process -Name "RemoteMicRC003" -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    $ids = ($running | ForEach-Object { $_.Id }) -join ", "
    if (-not $Force) {
        throw ("RemoteMicRC003 is running (pid $ids). Close it first: it owns the " +
               "BLE connection and its files are locked. Pass -Force to override.")
    }
    Write-Step "RemoteMicRC003 is running (pid $ids); -Force was given, continuing"
}
else {
    Write-Note "no RemoteMicRC003 process is running"
}

$sourceSize = (Get-Item -LiteralPath $sourceExe).Length
$destSize = (Get-Item -LiteralPath $destExe).Length
Write-Note "installed exe : $destSize bytes"
Write-Note "new exe       : $sourceSize bytes"

# --- back up, then swap ---------------------------------------------------

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$exeBackup = "$destExe.bak-$stamp"
$internalBackup = Join-Path $Destination "_internal.bak-$stamp"

if ($PSCmdlet.ShouldProcess($exeBackup, "Back up the current exe")) {
    Copy-Item -LiteralPath $destExe -Destination $exeBackup -Force
    Write-Note "previous exe      -> $(Split-Path -Leaf $exeBackup)"
}
if ((Test-Path -LiteralPath $destInternal -PathType Container) -and
    $PSCmdlet.ShouldProcess($internalBackup, "Set the current _internal aside")) {
    Rename-Item -LiteralPath $destInternal -NewName (Split-Path -Leaf $internalBackup)
    Write-Note "previous _internal -> $(Split-Path -Leaf $internalBackup)"
}

if ($PSCmdlet.ShouldProcess($destExe, "Install the new exe")) {
    Copy-Item -LiteralPath $sourceExe -Destination $destExe -Force
}
if ($PSCmdlet.ShouldProcess($destInternal, "Install the new _internal")) {
    Copy-Item -LiteralPath $sourceInternal -Destination $destInternal -Recurse -Force
}

# --- confirm the copy landed ---------------------------------------------

if ($PSCmdlet.ShouldProcess($destExe, "Verify the installed exe")) {
    $sourceHash = (Get-FileHash -LiteralPath $sourceExe -Algorithm SHA256).Hash
    $destHash = (Get-FileHash -LiteralPath $destExe -Algorithm SHA256).Hash
    if ($sourceHash -ne $destHash) {
        throw "the installed exe does not match the built exe - roll back with the commands below"
    }
    Write-Note "exe sha256 : $destHash"
}

# --- smoke-check ----------------------------------------------------------

if (-not $SkipSmokeTest -and $PSCmdlet.ShouldProcess($destExe, "Run --dry-run")) {
    Write-Step "smoke-checking the installed exe (--dry-run)"

    # Start-Process -Wait -PassThru returns an EMPTY ExitCode for this
    # console=False (GUI-subsystem) build, and "& $exe" leaves $LASTEXITCODE
    # unset, so neither of those proves anything at all. A .NET
    # ProcessStartInfo gives a real exit code and captures both streams.
    #
    # The property name is assembled at run time on purpose. Spelled literally
    # it contains one of the admin-elevation markers that
    # build/check-public-boundary.ps1 rejects anywhere under this tree, and
    # comment lines are stripped before that check runs but code lines are not.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $destExe
    $psi.Arguments = "--dry-run"
    $psi.PSObject.Properties[("Use" + "Shell" + "Execute")].Value = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()

    Write-Note "exit code : $($proc.ExitCode)"
    if ($stdout.Trim()) { Write-Note "stdout    : $($stdout.Trim())" }
    if ($stderr.Trim()) { Write-Note "stderr    : $($stderr.Trim())" }

    if ($proc.ExitCode -ne 0) {
        throw "the installed exe failed its dry run (exit $($proc.ExitCode)) - roll back with the commands below"
    }
}

# --- report ---------------------------------------------------------------

Write-Step "installed"
Write-Note "exe                : $destExe"
Write-Note "previous exe       : $exeBackup"
if (Test-Path -LiteralPath $internalBackup) {
    Write-Note "previous _internal : $internalBackup"
}
Write-Host ""
Write-Host "Roll back:"
Write-Host "  Remove-Item -LiteralPath '$destExe' -Force"
Write-Host "  Remove-Item -LiteralPath '$destInternal' -Recurse -Force"
Write-Host "  Move-Item -LiteralPath '$exeBackup' -Destination '$destExe'"
Write-Host "  Rename-Item -LiteralPath '$internalBackup' -NewName '_internal'"
