@echo off
REM Double-click this on Windows to build the app.
REM It only needs Python 3.11+ on PATH (the "py" launcher).
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
set code=%ERRORLEVEL%
if not "%code%"=="0" (
  echo.
  echo Build failed with exit code %code%.
)
pause
exit /b %code%
