@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
  echo ERROR: Cannot switch to the application folder.
  echo Folder: %~dp0
  pause
  exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "VENV_PY=.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
  echo ERROR: Virtual environment was not found.
  echo Expected file: %CD%\%VENV_PY%
  echo.
  echo Run install.bat from this folder first.
  echo If you already ran install.bat, run it again from this extracted folder.
  echo.
  pause
  popd >nul 2>&1
  exit /b 1
)

if not exist "voice_input_app\__main__.py" (
  echo ERROR: Application files were not found in this folder.
  echo Current folder: %CD%
  echo Please extract the ZIP archive completely and run this file from the extracted folder.
  echo.
  pause
  popd >nul 2>&1
  exit /b 1
)

echo Starting Voice Input Local...
echo Current folder: %CD%
echo Command: "%VENV_PY%" -m voice_input_app %*
echo.

"%VENV_PY%" -m voice_input_app %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo ERROR: Voice Input Local exited with code %EXIT_CODE%.
  echo Logs folder: %LOCALAPPDATA%\VoiceInputLocal\logs
  echo.
  echo To capture the full startup output, run run_debug.bat.
  echo.
  pause
  popd >nul 2>&1
  exit /b %EXIT_CODE%
)

popd >nul 2>&1
exit /b 0
