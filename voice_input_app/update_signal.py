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
