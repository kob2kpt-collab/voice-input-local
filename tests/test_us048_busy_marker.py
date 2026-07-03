# -*- coding: utf-8 -*-
"""Регресс-тест US-048: маркер занятости для безопасного централизованного обновления.

Инварианты:

1. Функциональный: busy_marker.write_heartbeat() создаёт файл-маркер в
   общесистемной папке (program_data_dir), clear() его снимает, путь оканчивается
   на busy.lock.

2. Статический guard ui.py: есть периодический таймер маркера, методы
   _app_is_busy / _update_busy_marker (учитывают is_dictation_busy, is_file_busy,
   summarize_worker), очистка устаревшего маркера при старте и импорт busy_marker.

3. Статический guard установщика: installer/VoiceInputLocal.iss в InitializeSetup
   проверяет busy.lock (иначе централизованное обновление может прервать работу).

Тест headless (без Qt). Запуск: python tests/test_us048_busy_marker.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app import busy_marker


def test_marker_write_and_clear():
    path = busy_marker.busy_file_path()
    assert path.name == "busy.lock", f"имя маркера не busy.lock: {path.name}"
    # Папка общесистемная (program_data_dir), не профиль пользователя.
    assert "VoiceInputLocal" in str(path), f"маркер не в папке приложения: {path}"

    busy_marker.clear()
    assert not path.exists(), "маркер не снят перед тестом"

    busy_marker.write_heartbeat()
    assert path.exists(), "маркер не создан write_heartbeat()"
    content = path.read_text(encoding="ascii").strip()
    assert content.isdigit(), f"в маркере не timestamp: {content!r}"

    busy_marker.clear()
    assert not path.exists(), "маркер не снят clear()"


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_ui_wires_busy_marker():
    ui_src = (REPO_ROOT / "voice_input_app" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(ui_src)

    assert "from . import busy_marker" in ui_src, "ui.py не импортирует busy_marker"
    assert "self._busy_marker_timer" in ui_src, "нет периодического таймера маркера занятости"

    upd = _find_func(tree, "_update_busy_marker")
    assert upd is not None, "нет метода _update_busy_marker"
    upd_src = ast.get_source_segment(ui_src, upd) or ""
    assert "write_heartbeat" in upd_src and "clear" in upd_src, "_update_busy_marker не пишет/не снимает маркер"

    busy = _find_func(tree, "_app_is_busy")
    assert busy is not None, "нет метода _app_is_busy"
    busy_src = ast.get_source_segment(ui_src, busy) or ""
    for needed in ("is_dictation_busy", "is_file_busy", "summarize_worker"):
        assert needed in busy_src, f"_app_is_busy не учитывает {needed}"

    # Очистка устаревшего маркера при старте (dependency-free busy_marker.clear()).
    assert "busy_marker.clear()" in ui_src, "нет startup-clear маркера в ui.py"


def test_installer_checks_busy_marker():
    iss = (REPO_ROOT / "installer" / "VoiceInputLocal.iss").read_text(encoding="utf-8")
    assert "InitializeSetup" in iss, "в установщике нет InitializeSetup"
    assert "busy.lock" in iss, "установщик не проверяет busy.lock — обновление может прервать работу"


def _run():
    tests = [
        test_marker_write_and_clear,
        test_ui_wires_busy_marker,
        test_installer_checks_busy_marker,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print("US-048 regression: ALL PASS")


if __name__ == "__main__":
    _run()
