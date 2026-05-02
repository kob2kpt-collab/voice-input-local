from __future__ import annotations

import os
import sys
from pathlib import Path

from .logger import get_logger
from .paths import app_root

log = get_logger("autostart")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "VoiceInputLocal"


def _quote(value: str) -> str:
    return '"' + value.replace('"', r'\"') + '"'


def startup_command() -> str:
    """Return the command used by Windows autostart.

    For a PyInstaller build we start the executable directly. For the source
    package we use run_silent.vbs, which launches run.bat without a console
    window and keeps paths relative to the extracted application folder.
    """
    if getattr(sys, "frozen", False):
        return _quote(str(Path(sys.executable).resolve()))
    script = app_root() / "run_silent.vbs"
    if script.exists():
        return f"wscript.exe {_quote(str(script))}"
    # Fallback for development runs.
    pythonw = Path(sys.executable).with_name("pythonw.exe") if os.name == "nt" else Path(sys.executable)
    return f"cmd.exe /c cd /d {_quote(str(app_root()))} && {_quote(str(pythonw))} -m voice_input_app"


def is_supported() -> bool:
    return os.name == "nt"


def is_enabled() -> bool:
    if not is_supported():
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _typ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except Exception:
        log.exception("Autostart status check failed")
        return False


def set_enabled(enabled: bool) -> None:
    if not is_supported():
        raise RuntimeError("Автозагрузка поддерживается только в Windows.")
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            command = startup_command()
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
            log.info("Autostart enabled: %s", command)
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
                log.info("Autostart disabled")
            except FileNotFoundError:
                pass
