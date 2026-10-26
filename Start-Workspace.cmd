@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Workspace.ps1" %*
set "oeaExitCode=%errorlevel%"
if not "%oeaExitCode%"=="0" (
    echo.
    echo Startup failed. Review the message above, then press any key to close.
    pause >nul
)
exit /b %oeaExitCode%
