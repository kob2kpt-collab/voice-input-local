from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "VoiceInputLocal"


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_path(relative: str) -> Path:
    """Return a resource path for source and PyInstaller builds."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", app_root()))
        candidate = base / relative
        if candidate.exists():
            return candidate
        return app_root() / relative
    return app_root() / relative


def app_icon_path() -> Path:
    return resource_path("voice_input_app/assets/icon.ico")


def export_font_path() -> Path:
    """US-024: путь к встроенному кириллическому TTF для генерации PDF."""
    return resource_path("voice_input_app/assets/DejaVuSans.ttf")


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        path = Path(base) / APP_NAME
    else:
        path = Path.home() / ".voice_input_local"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_data_dir() / "config.json"


def history_db_path() -> Path:
    return app_data_dir() / "history.sqlite3"


def recordings_dir() -> Path:
    path = app_data_dir() / "recordings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_dir() -> Path:
    path = app_data_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def program_data_dir() -> Path:
    """Общесистемная папка (%ProgramData%\\VoiceInputLocal).

    Используется для маркера занятости (US-048): приложение работает от имени
    пользователя, а установщик при централизованном обновлении — от имени
    SYSTEM, поэтому маркер кладётся в общедоступное место, а не в профиль
    пользователя (%LOCALAPPDATA%). На не-Windows PROGRAMDATA нет — fallback на
    пользовательскую папку данных (маркер там осмыслен только локально).
    """
    base = os.environ.get("PROGRAMDATA")
    if base:
        path = Path(base) / APP_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path
    return app_data_dir()
