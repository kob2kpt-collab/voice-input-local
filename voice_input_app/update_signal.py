# -*- coding: utf-8 -*-
"""US-057: сигнальные маркеры централизованного обновления (приложение <-> установщик).

Обмен идёт через файлы в общесистемной папке %ProgramData% (paths.program_data_dir()),
доступной установщику под SYSTEM и приложению под пользователем:

- update-pending.flag  — установщик -> приложение: «централизованное обновление
  ждёт, спроси пользователя». Приложение показывает окно и снимает флаг.
- update-declined.flag — приложение -> установщик: «пользователь отклонил в этот
  цикл». Установщик возвращает код «отклонено» и снимает флаг (при следующей
  занятой попытке окно показывается снова).

«Согласие» (accept) отдельного маркера НЕ требует: приложение снимает занятость
(busy.lock, US-048) и закрывается, и следующая попытка установщика видит простой
и ставит обновление.

ВАЖНО: имена файлов и их расположение — это КОНТРАКТ с Pascal-кодом установщика
(installer/VoiceInputLocal.iss). Менять согласованно с обеих сторон.
"""
from __future__ import annotations

from pathlib import Path

from .logger import get_logger
from .paths import program_data_dir

log = get_logger("update_signal")

UPDATE_PENDING_NAME = "update-pending.flag"
UPDATE_DECLINED_NAME = "update-declined.flag"


def pending_path() -> Path:
    return program_data_dir() / UPDATE_PENDING_NAME


def declined_path() -> Path:
    return program_data_dir() / UPDATE_DECLINED_NAME


def _write(path: Path) -> None:
    try:
        path.write_text("1", encoding="ascii")
    except OSError:
        log.debug("Не удалось записать маркер %s", path.name, exc_info=True)


def _clear(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.debug("Не удалось снять маркер %s", path.name, exc_info=True)


# --- update-pending: установщик пишет, приложение читает и снимает ---
def set_update_pending() -> None:
    _write(pending_path())


def is_update_pending() -> bool:
    return pending_path().exists()


def clear_update_pending() -> None:
    _clear(pending_path())


# --- update-declined: приложение пишет, установщик читает и снимает ---
def set_declined() -> None:
    _write(declined_path())


def is_declined() -> bool:
    return declined_path().exists()


def clear_declined() -> None:
    _clear(declined_path())
