# -*- coding: utf-8 -*-
"""Сквозной регресс US-076: словарь переезжает при РЕАЛЬНОМ запуске программы.

Отдельно от tests/test_us076_dictionary_file.py, который проверяет само
хранилище. Здесь проверяется путь запуска целиком (MainWindow), потому что
самое дорогое в этой истории — потеря пользовательских терминов при обновлении:
хранилище может быть исправным, а вызов переноса — не на месте.

Сценарий:

1. Старый config.json со словарём внутри → после запуска словарь в
   dictionary.json, ни одна запись не потеряна, выключенный термин остался
   выключенным (AC 2).
2. Ключа postprocess_glossary в config.json больше нет (AC 1), выключатель
   словаря остался (AC 6).
3. Резервная копия настроек до переноса сохранена.
4. Термины видны в таблице вкладки «Словарь».
Повторный перенос (AC 2) и защита от подмены настроек (AC 3) проверяются на
уровне хранилища в tests/test_us076_dictionary_file.py: там та же логика, но без
Qt. ЗДЕСЬ окно строится РОВНО ОДИН раз. Раньше их было три, и процесс изредка
падал уже ПОСЛЕ печати «ALL PASS» — у каждого окна остаются свои таймеры и
стартовая проверка облака в отдельном потоке, а тест завершает процесс через
os._exit. Случайное падение в релизном гейте хуже отсутствия проверки: оно
блокирует выпуск без причины. Гасить таймеры и потоки руками пробовали — стало
только хуже (сегфолт в 4 прогонах из 5), поэтому уменьшено само количество окон.

Тест поднимает Qt в режиме offscreen на ВРЕМЕННОМ профиле, поэтому настройки и
словарь пользователя не трогает.

Запуск: python tests/test_us076_migration_e2e.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ВАЖНО: профиль и платформа Qt задаются ДО импорта модулей приложения.
_PROFILE = Path(tempfile.mkdtemp(prefix="vil-us076-e2e-"))
os.environ["LOCALAPPDATA"] = str(_PROFILE)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

APP_DIR = _PROFILE / "VoiceInputLocal"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG = APP_DIR / "config.json"
DICTIONARY = APP_DIR / "dictionary.json"

LEGACY_CONFIG = {
    "postprocess_enabled": True,
    "postprocess_glossary_enabled": True,
    "postprocess_glossary": [
        {"term": "Термин-один", "distortions": "термин один", "context": "описание", "exclusions": ""},
        {"term": "Термин-два", "distortions": "", "context": "", "exclusions": "", "enabled": False},
    ],
    "hotkey": "ctrl+space",
}
EXPECTED_TERMS = ["Термин-один", "Термин-два"]


# Окно теста: нужно на выходе, чтобы дождаться его потока предзагрузки.
_WINDOWS: list = []


def _dictionary() -> dict:
    return json.loads(DICTIONARY.read_text(encoding="utf-8"))


def _config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def test_migration_end_to_end():
    CONFIG.write_text(json.dumps(LEGACY_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")

    from PySide6.QtWidgets import QApplication

    from voice_input_app import glossary
    from voice_input_app.ui import MainWindow
    from voice_input_app.ui_window_constraints import apply_patches

    apply_patches()
    app = QApplication.instance() or QApplication(sys.argv)
    assert app is not None

    window = MainWindow()

    # 1. Перенос: обе записи на месте, выключенный термин остался выключенным.
    entries = _dictionary()["entries"]
    assert [e["term"] for e in entries] == EXPECTED_TERMS, entries
    assert entries[1]["enabled"] is False, "выключенный термин потерян при переносе"

    # 2. Ключа словаря в настройках больше нет, выключатель остался.
    cfg = _config()
    assert "postprocess_glossary" not in cfg, "ключ словаря остался в config.json"
    assert cfg.get("postprocess_glossary_enabled") is True, "потерян выключатель словаря"

    # 3. Резервная копия настроек до переноса.
    assert (APP_DIR / "config.pre-dictionary.bak").exists(), "нет резервной копии настроек"

    # 4. Термины видны в таблице вкладки «Словарь».
    shown = [
        window.dictionary_table.item(r, 1).text()
        for r in range(window.dictionary_table.rowCount())
    ]
    assert shown == EXPECTED_TERMS, shown

    # Повторный перенос и подмена настроек — на уровне хранилища, без второго
    # окна: tests/test_us076_dictionary_file.py, test_migration_from_config и
    # test_config_replacement_keeps_user_dictionary.
    assert glossary.migrate_from_config() == 0, "повторный перенос сработал ещё раз"
    assert [e["term"] for e in _dictionary()["entries"]] == EXPECTED_TERMS, _dictionary()

    _WINDOWS.append(window)


def _wait_for_preload() -> None:
    """Дождаться потока предзагрузки модели перед завершением процесса.

    MainWindow при создании начинает готовить выбранную модель в отдельном
    потоке (start_preload_selected_model). Whisper Small доступен из кэша
    faster-whisper, поэтому в тесте реально грузится модель — нативным кодом
    CTranslate2. Тест завершает процесс через os._exit, и если поток в этот
    момент внутри загрузки, процесс падает: отдельные прогоны проходили, а под
    scripts/run_tests.py (машина занята, попадание вероятнее) тест изредка
    засчитывался упавшим уже ПОСЛЕ печати «ALL PASS».

    Ждём ЕСТЕСТВЕННОГО конца потока и НИЧЕГО ему не делаем: попытка гасить его
    quit()/wait() вместе с обходом таймеров окна давала сегфолт в четырёх
    прогонах из пяти. Ожидание ограничено по времени — если модель почему-то
    грузится дольше, тест всё равно завершится, просто с прежним риском.
    """
    for window in _WINDOWS:
        worker = getattr(window, "preload_worker", None)
        if worker is None:
            continue
        try:
            if worker.isRunning():
                worker.wait(60000)
        except RuntimeError:
            pass


def _run():
    for t in [test_migration_end_to_end]:
        t()
        print("PASS: %s" % t.__name__)
    print("US-076 end-to-end: ALL PASS")


if __name__ == "__main__":
    _run()
    _wait_for_preload()
    # Qt-приложение с треем и таймерами не всегда завершается само.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
