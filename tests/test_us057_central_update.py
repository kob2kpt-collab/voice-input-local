# -*- coding: utf-8 -*-
"""Регресс-тесты US-055/US-057: интерактивное централизованное обновление.

Инварианты:

1. Функциональный: update_signal — маркеры update-pending / update-declined
   в общесистемной папке приложения корректно создаются/читаются/снимаются.

2. Статический guard ui.py: диалог «Обновить/Отклонить» показывается только при
   занятости, вызывается из тика busy-таймера; accept снимает маркеры и закрывает
   приложение, decline пишет declined и снимает pending.

3. Статический guard установщика (installer/VoiceInputLocal.iss): InitializeSetup
   реализует протокол (busy.lock / update-pending / update-declined), коды
   возврата 100/101 через ExitProcess, секция [Dirs] с users-modify.

Тесты headless (без Qt). Запуск: python tests/test_us057_central_update.py
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


def test_marker_read_write_clear():
    u.clear_update_pending()
    u.clear_declined()
    assert not u.is_update_pending()
    assert not u.is_declined()

    assert u.pending_path().name == "update-pending.flag"
    assert u.declined_path().name == "update-declined.flag"
    assert "VoiceInputLocal" in str(u.pending_path())

    u.set_update_pending()
    assert u.is_update_pending()
    u.clear_update_pending()
    assert not u.is_update_pending()

    u.set_declined()
    assert u.is_declined()
    u.clear_declined()
    assert not u.is_declined()


def test_ui_wires_decision_dialog():
    ui_src = (REPO_ROOT / "voice_input_app" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(ui_src)

    assert "update_signal" in ui_src, "ui.py не импортирует update_signal"

    for name in ("_check_pending_update", "_accept_centralized_update", "_decline_centralized_update"):
        assert _find_func(tree, name) is not None, f"нет метода {name}"

    check = ast.get_source_segment(ui_src, _find_func(tree, "_check_pending_update")) or ""
    assert "is_update_pending" in check, "_check_pending_update не проверяет update-pending"
    assert "_app_is_busy" in check, "окно должно показываться только при занятости (_app_is_busy)"

    accept = ast.get_source_segment(ui_src, _find_func(tree, "_accept_centralized_update")) or ""
    assert "clear_update_pending" in accept and "really_quit" in accept, "accept должен снять pending и закрыть приложение"

    decline = ast.get_source_segment(ui_src, _find_func(tree, "_decline_centralized_update")) or ""
    assert "set_declined" in decline and "clear_update_pending" in decline, "decline должен записать declined и снять pending"

    tick = ast.get_source_segment(ui_src, _find_func(tree, "_update_busy_marker")) or ""
    assert "_check_pending_update" in tick, "проверка pending не вызывается из тика busy-таймера"


def test_installer_handshake_and_exit_codes():
    iss = (REPO_ROOT / "installer" / "VoiceInputLocal.iss").read_text(encoding="utf-8")
    assert "function InitializeSetup" in iss, "в установщике нет InitializeSetup"
    for token in ("busy.lock", "update-pending.flag", "update-declined.flag", "ExitProcess"):
        assert token in iss, f"установщик не использует {token}"
    assert "EXIT_DECLINED_USER = 100" in iss, "нет кода 100 (отклонено)"
    assert "EXIT_DEFERRED_BUSY = 101" in iss, "нет кода 101 (отложено)"
    assert "users-modify" in iss, "нет [Dirs] users-modify для обмена маркерами"


def _run():
    tests = [
        test_marker_read_write_clear,
        test_ui_wires_decision_dialog,
        test_installer_handshake_and_exit_codes,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print("US-055/US-057 regression: ALL PASS")


if __name__ == "__main__":
    _run()
