# -*- coding: utf-8 -*-
"""Регресс-тесты US-055/US-057 (упрощённая модель): централизованное обновление.

Модель после упрощения (4.17.3): при занятости установщик ВСЕГДА возвращает
101 «Отложено» (единый код), окно выбора показывает приложение. «Закрыть и
обновить» → приложение закрывается (следующая попытка ставит), «Отклонить» →
продолжаем работу. Отдельного кода «отклонено» (100) и маркера update-declined
БОЛЬШЕ НЕТ.

Инварианты:
1. Функциональный: update_signal — маркер update-pending создаётся/читается/снимается.
2. Статический guard ui.py: окно только при занятости, из тика таймера; accept
   снимает маркер + закрывает приложение; decline снимает pending (без declined).
3. Статический guard установщика: InitializeSetup + busy.lock/update-pending +
   ExitProcess + код 101; declined/код 100 отсутствуют; [Dirs] users-modify.

Запуск: python tests/test_us057_central_update.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app import update_signal as u


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_pending_marker_read_write_clear():
    u.clear_update_pending()
    assert not u.is_update_pending()
    assert u.pending_path().name == "update-pending.flag"
    assert "VoiceInputLocal" in str(u.pending_path())

    u.set_update_pending()
    assert u.is_update_pending()
    u.clear_update_pending()
    assert not u.is_update_pending()

    # Маркера «отклонено» в упрощённой модели быть не должно.
    assert not hasattr(u, "set_declined"), "update_signal не должен содержать set_declined"
    assert not hasattr(u, "declined_path"), "update_signal не должен содержать declined_path"


def test_ui_wires_decision_dialog():
    ui_src = (REPO_ROOT / "voice_input_app" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(ui_src)

    assert "update_signal" in ui_src, "ui.py не импортирует update_signal"
    assert "set_declined" not in ui_src, "в упрощённой модели ui.py не должен звать set_declined"

    for name in ("_check_pending_update", "_accept_centralized_update", "_decline_centralized_update"):
        assert _find_func(tree, name) is not None, f"нет метода {name}"

    check = ast.get_source_segment(ui_src, _find_func(tree, "_check_pending_update")) or ""
    assert "is_update_pending" in check and "_app_is_busy" in check, "окно только при pending И занятости"

    accept = ast.get_source_segment(ui_src, _find_func(tree, "_accept_centralized_update")) or ""
    assert "clear_update_pending" in accept and "really_quit" in accept, "accept снимает pending и закрывает приложение"

    decline = ast.get_source_segment(ui_src, _find_func(tree, "_decline_centralized_update")) or ""
    assert "clear_update_pending" in decline, "decline снимает pending (продолжаем работу)"

    tick = ast.get_source_segment(ui_src, _find_func(tree, "_update_busy_marker")) or ""
    assert "_check_pending_update" in tick, "проверка pending не вызывается из тика busy-таймера"


def test_installer_deferred_only():
    iss = (REPO_ROOT / "installer" / "VoiceInputLocal.iss").read_text(encoding="utf-8")
    assert "function InitializeSetup" in iss, "нет InitializeSetup"
    for token in ("busy.lock", "update-pending.flag", "ExitProcess", "EXIT_DEFERRED_BUSY = 101"):
        assert token in iss, f"установщик не использует {token}"
    assert "users-modify" in iss, "нет [Dirs] users-modify"
    # Регресс упрощения: код 100 и маркер declined удалены.
    assert "update-declined" not in iss, "маркер update-declined должен быть удалён"
    assert "EXIT_DECLINED_USER" not in iss, "код EXIT_DECLINED_USER должен быть удалён"


def _run():
    tests = [
        test_pending_marker_read_write_clear,
        test_ui_wires_decision_dialog,
        test_installer_deferred_only,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print("US-055/US-057 (simplified) regression: ALL PASS")


if __name__ == "__main__":
    _run()
