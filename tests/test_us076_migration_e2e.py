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
5. Повторный запуск не дублирует записи (AC 2).
6. Подмена файла настроек чужим словарём не меняет словарь пользователя (AC 3).

Тест поднимает Qt в режиме offscreen на ВРЕМЕННОМ профиле, поэтому настройки и
словарь пользователя не трогает. Идёт заметно дольше остальных (несколько
десятков секунд): главное окно строится трижды.

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


def _dictionary() -> dict:
    return json.loads(DICTIONARY.read_text(encoding="utf-8"))


def _config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def test_migration_end_to_end():
    CONFIG.write_text(json.dumps(LEGACY_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")

    from PySide6.QtWidgets import QApplication

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

    # 5. Повторный запуск не дублирует записи.
    MainWindow()
    assert [e["term"] for e in _dictionary()["entries"]] == EXPECTED_TERMS, _dictionary()

    # 6. Подложили чужой файл настроек со СВОИМ словарём — словарь остался нашим.
    CONFIG.write_text(
        json.dumps({"postprocess_glossary": [{"term": "Чужой-термин"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    MainWindow()
    assert [e["term"] for e in _dictionary()["entries"]] == EXPECTED_TERMS, _dictionary()


def _run():
    for t in [test_migration_end_to_end]:
        t()
        print("PASS: %s" % t.__name__)
    print("US-076 end-to-end: ALL PASS")


if __name__ == "__main__":
    _run()
    # Qt-приложение с трея/таймерами не всегда завершается само.
    sys.stdout.flush()
    os._exit(0)
