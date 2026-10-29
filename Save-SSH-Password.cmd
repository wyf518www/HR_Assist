@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\deploy\Update-HRAssist.ps1" -SavePassword %*
set "save_result=%ERRORLEVEL%"
echo.
pause
exit /b %save_result%
