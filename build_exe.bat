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

if not exist "%VENV_PY%" (
  echo ERROR: Virtual environment was not found.
  echo Run install.bat first.
  pause
  popd >nul 2>&1
  exit /b 1
)

"%VENV_PY%" -m PyInstaller --noconfirm --clean --windowed --name VoiceInputLocal --icon "voice_input_app\assets\icon.ico" --add-data "voice_input_app\assets;voice_input_app\assets" --collect-all PySide6 --collect-all faster_whisper --collect-all onnx_asr --hidden-import keyboard --hidden-import pyperclip --hidden-import pyautogui --hidden-import uiautomation --hidden-import sounddevice --hidden-import soundfile main.py
if errorlevel 1 (
  echo ERROR: EXE build failed.
  pause
  popd >nul 2>&1
  exit /b 1
)

echo EXE created: dist\VoiceInputLocal\VoiceInputLocal.exe
echo IMPORTANT: run or copy the whole dist\VoiceInputLocal folder. Do not run files from build\.
pause
popd >nul 2>&1
exit /b 0
