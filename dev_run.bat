@echo off
REM ============================================================================
REM dev_run.bat - Launch the source-tree dev build of RemoteMicRC003 alongside
REM the installed build, by setting the two env-var hooks introduced in
REM audit/batch1 (see ``apps/windows/rc003/src/ovb_rc003/config.py`` and
REM ``single_instance.py``):
REM
REM   REMOTEMIC_RC003_INSTANCE_SUFFIX  - appended to the named-mutex identifier
REM                                     so this dev build claims a different
REM                                     Win32 mutex than the installed .exe.
REM                                     Default below: "_Dev" -> mutex becomes
REM                                     ``Local\RemoteMicRC003_BridgeInstance_Dev``.
REM   REMOTEMIC_RC003_CONFIG_ROOT     - replaces ``%LOCALAPPDATA%\RemoteMic\RC003``
REM                                     entirely. We default to a sibling
REM                                     ``RC003-Dev`` directory under LOCALAPPDATA
REM                                     so the dev config is fully isolated from
REM                                     the installed app's config dir.
REM
REM The installed app never reads either variable (env vars it does not look at
REM have no effect), so the installed build keeps its own mutex and its own
REM config dir, and both can run simultaneously without colliding.
REM
REM To use:
REM   1.  Run ``dev_run.bat --bridge``   to launch the bridge from source
REM       Run ``dev_run.bat --settings`` to open the settings UI from source
REM       Run ``dev_run.bat`` (no args)  to invoke ``python -m ovb_rc003`` with
REM                                       the isolated env vars in place
REM
REM The batch file intentionally does not modify any file outside the project
REM tree - it only sets process-local env vars and invokes ``python``.
REM ============================================================================

REM ---- IMPORTANT: ``start /B python.exe ...`` (not ``start /B cmd /c ...``)
REM      per the AGENTS.md console-launch gotcha. Wrapping the python invocation
REM      in another ``cmd /c`` re-establishes the parent-console relationship
REM      that ``/B`` is trying to sever, and the child dies via Ctrl+C after
REM      ~5s when this console loses focus.

if not defined REMOTEMIC_RC003_INSTANCE_SUFFIX set REMOTEMIC_RC003_INSTANCE_SUFFIX=_Dev
if not defined REMOTEMIC_RC003_CONFIG_ROOT (
    if defined LOCALAPPDATA (
        set "REMOTEMIC_RC003_CONFIG_ROOT=%LOCALAPPDATA%\RemoteMic\RC003-Dev"
    ) else (
        set "REMOTEMIC_RC003_CONFIG_ROOT=%USERPROFILE%\AppData\Local\RemoteMic\RC003-Dev"
    )
)

set "PYTHONPATH=%~dp0apps\windows\rc003\src;%PYTHONPATH%"

echo ============================================================
echo  Dev build env-var isolation:
echo    REMOTEMIC_RC003_INSTANCE_SUFFIX = %REMOTEMIC_RC003_INSTANCE_SUFFIX%
echo    REMOTEMIC_RC003_CONFIG_ROOT    = %REMOTEMIC_RC003_CONFIG_ROOT%
echo    PYTHONPATH (prepended)         = %~dp0apps\windows\rc003\src
echo ============================================================

cd /d "%~dp0apps\windows\rc003"

set "TITLE=RemoteMicRC003 Dev (%REMOTEMIC_RC003_INSTANCE_SUFFIX%^|%REMOTEMIC_RC003_CONFIG_ROOT%)"
start "%TITLE%" /B python -m ovb_rc003 %*