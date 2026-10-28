@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\deploy\Update-HRAssist.ps1" %*
set "update_result=%ERRORLEVEL%"
echo.
pause
exit /b %update_result%
