# -*- coding: utf-8 -*-
"""US-048: маркер занятости приложения для безопасного централизованного обновления.

Пока идёт активная работа (запись, диктовка, расшифровка файла, суммаризация),
приложение периодически обновляет файл-маркер %ProgramData%\VoiceInputLocal\
busy.lock (heartbeat — перезапись обновляет время модификации файла). Установщик
при централизованном обновлении читает маркер и, если heartbeat свежий,
откладывает обновление (завершается с кодом «занято»), чтобы не прервать
активную работу пользователя. При простое маркер снимается.

Устаревший маркер (например, после аварийного завершения приложения) установщик
считает недействительным по возрасту файла и обновлению не мешает.
"""
from __future__ import annotations

import time
from pathlib import Path

from .logger import get_logger
from .paths import program_data_dir

log = get_logger("busy_marker")

BUSY_FILE_NAME = "busy.lock"


def busy_file_path() -> Path:
    return program_data_dir() / BUSY_FILE_NAME


def write_heartbeat() -> None:
    """Создать/обновить маркер занятости (обновляет время модификации файла)."""
    try:
        # program_data_dir() уже создаёт папку при необходимости.
        busy_file_path().write_text(str(int(time.time())), encoding="ascii")
    except OSError:
        log.debug("Не удалось записать маркер занятости", exc_info=True)


def clear() -> None:
    """Снять маркер занятости (приложение простаивает)."""
    try:
        busy_file_path().unlink(missing_ok=True)
    except OSError:
        log.debug("Не удалось снять маркер занятости", exc_info=True)
