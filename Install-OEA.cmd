@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-OEA.ps1" %*
set "oeaExitCode=%errorlevel%"
if not "%oeaExitCode%"=="0" (
    echo.
    echo Installation failed. Review the message above, then press any key to close.
    pause >nul
)
exit /b %oeaExitCode%
