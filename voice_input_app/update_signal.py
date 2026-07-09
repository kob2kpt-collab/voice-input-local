# -*- coding: utf-8 -*-
"""US-057: сигнальный маркер централизованного обновления (установщик -> приложение).

Обмен через файл в общесистемной папке %ProgramData% (paths.program_data_dir()),
доступной установщику под SYSTEM и приложению под пользователем:

- update-pending.flag — установщик -> приложению: «пришло централизованное
  обновление, покажи окно выбора». Приложение показывает окно и снимает флаг.

Решение пользователя обрабатывается на стороне приложения и НЕ возвращается в KSC
отдельным кодом (упрощение после теста в KSC — см. CLAUDE.md): «Закрыть и обновить»
закрывает приложение (следующая попытка установщика ставит обновление); «Отклонить»
просто продолжает работу (при следующей занятой попытке окно покажется снова). При
занятости установщик всегда возвращает «Отложено» (единый код), поэтому отдельного
маркера «отклонено» больше нет.

ВАЖНО: имя файла и его расположение — это КОНТРАКТ с Pascal-кодом установщика
(installer/VoiceInputLocal.iss).
"""
from __future__ import annotations

from pathlib import Path

from .logger import get_logger
from .paths import program_data_dir

log = get_logger("update_signal")

UPDATE_PENDING_NAME = "update-pending.flag"
# US-058: маркер «закройся для тихого обновления при простое» (установщик ->
# приложение). При ПРОСТОЕ установщик не может закрыть трей-приложение
# кросс-сессионно (он под SYSTEM, сессия 0), поэтому просит приложение закрыться
# само и в том же прогоне ждёт освобождения .exe (single-pass). Приложение,
# закрываясь, оставляет фоновый релончер, который перезапустит его после
# установки. Имя/расположение файла — КОНТРАКТ с Pascal-кодом установщика.
UPDATE_CLOSE_NAME = "update-close.flag"


def pending_path() -> Path:
    return program_data_dir() / UPDATE_PENDING_NAME


def set_update_pending() -> None:
    """Записать сигнал «обновление ждёт» (пишет установщик; в приложении — для тестов)."""
    try:
        pending_path().write_text("1", encoding="ascii")
    except OSError:
        log.debug("Не удалось записать маркер update-pending", exc_info=True)


def is_update_pending() -> bool:
    return pending_path().exists()


def clear_update_pending() -> None:
    try:
        pending_path().unlink(missing_ok=True)
    except OSError:
        log.debug("Не удалось снять маркер update-pending", exc_info=True)


# --- US-058: маркер закрытия ради тихого обновления при простое ---

def close_request_path() -> Path:
    return program_data_dir() / UPDATE_CLOSE_NAME


def set_update_close() -> None:
    """Записать сигнал «закройся для обновления» (пишет установщик; в приложении — для тестов)."""
    try:
        close_request_path().write_text("1", encoding="ascii")
    except OSError:
        log.debug("Не удалось записать маркер update-close", exc_info=True)


def is_update_close() -> bool:
    return close_request_path().exists()


def clear_update_close() -> None:
    try:
        close_request_path().unlink(missing_ok=True)
    except OSError:
        log.debug("Не удалось снять маркер update-close", exc_info=True)
