@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
  echo ERROR: Cannot switch to the application folder.
  pause
  exit /b 1
)

if not exist "dist\VoiceInputLocal\VoiceInputLocal.exe" (
  echo ERROR: dist\VoiceInputLocal\VoiceInputLocal.exe was not found.
  echo Run build_exe.bat first.
  pause
  popd >nul 2>&1
  exit /b 1
)

where ISCC.exe >nul 2>&1
if errorlevel 1 (
  echo ERROR: Inno Setup compiler ISCC.exe was not found in PATH.
  echo Install Inno Setup from https://jrsoftware.org/isinfo.php and try again.
  pause
  popd >nul 2>&1
  exit /b 1
)

if not exist "release" mkdir "release"
ISCC.exe "installer\VoiceInputLocal.iss"
if errorlevel 1 (
  echo ERROR: Installer build failed.
  pause
  popd >nul 2>&1
  exit /b 1
)

echo Installer created in release\
pause
popd >nul 2>&1
exit /b 0
