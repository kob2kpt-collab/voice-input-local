@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
  echo ERROR: Cannot switch to the application folder.
  echo Folder: %~dp0
  pause
  exit /b 1
)

where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo ERROR: PowerShell was not found.
  pause
  popd >nul 2>&1
  exit /b 1
)

echo Installing Voice Input Local...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\install.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo ERROR: Installation failed with code %EXIT_CODE%.
  pause
  popd >nul 2>&1
  exit /b %EXIT_CODE%
)

echo.
echo Installation completed. Now run run.bat.
pause
popd >nul 2>&1
exit /b 0
