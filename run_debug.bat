@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
  echo ERROR: Cannot switch to the application folder.
  pause
  exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "VENV_PY=.venv\Scripts\python.exe"
set "LOG_FILE=launch_debug.log"

echo Voice Input Local debug launch > "%LOG_FILE%"
echo Folder: %CD% >> "%LOG_FILE%"
echo Date: %DATE% %TIME% >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"

if not exist "%VENV_PY%" (
  echo ERROR: Virtual environment was not found. >> "%LOG_FILE%"
  echo Expected file: %CD%\%VENV_PY% >> "%LOG_FILE%"
  type "%LOG_FILE%"
  pause
  popd >nul 2>&1
  exit /b 1
)

"%VENV_PY%" -m voice_input_app %* >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
echo. >> "%LOG_FILE%"
echo Exit code: %EXIT_CODE% >> "%LOG_FILE%"

type "%LOG_FILE%"
echo.
echo Debug log saved to: %CD%\%LOG_FILE%
pause
popd >nul 2>&1
exit /b %EXIT_CODE%
