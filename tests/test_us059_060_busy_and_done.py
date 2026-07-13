# -*- coding: utf-8 -*-
"""Регресс-тесты US-059 (единый single-pass для занятого пути) и US-061
(powershell-релончер убран — антивирус расценивал его как троянское поведение;
US-060 «маркер update-done» удалён вместе с релончером).

US-059: при занятости установщик пишет update-pending и ЖДЁТ решения в том же
прогоне (WaitBusyDecision). «Закрыть и обновить» -> приложение показывает
сообщение и закрывается (БЕЗ спавна процессов); установщик ставит (код 0).
«Отклонить» -> update-decline -> установщик выходит 101.

US-061: приложение НЕ запускает никаких процессов при обновлении. Возврат
программы — через штатный автозапуск Windows.

Запуск: python tests/test_us059_060_busy_and_done.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app import update_signal as u


def _find(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def test_decline_marker_roundtrip():
    u.clear_update_decline()
    assert not u.is_update_decline()
    u.set_update_decline()
    assert u.is_update_decline()
    u.clear_update_decline()
    assert not u.is_update_decline()
    assert u.UPDATE_DECLINE_NAME == "update-decline.flag"
    # US-060 (маркер завершения) удалён вместе с релончером (US-061).
    assert not hasattr(u, "set_update_done"), "маркер update-done должен быть удалён (US-061)"
    assert not hasattr(u, "UPDATE_DONE_NAME"), "константа update-done должна быть удалена (US-061)"


def test_installer_busy_single_pass_no_done_marker():
    iss = (REPO_ROOT / "installer" / "VoiceInputLocal.iss").read_text(encoding="utf-8")
    # US-059: занятый путь ждёт решения пользователя.
    for token in ("function WaitBusyDecision", "BUSY_DECISION_TIMEOUT_MS",
                  "update-decline.flag", "if WaitBusyDecision() = 0 then"):
        assert token in iss, f"US-059: нет {token}"
    # US-061: маркер завершения и его запись убраны.
    assert "update-done.flag" not in iss, "US-061: update-done должен быть удалён из установщика"
    assert "CurStepChanged" not in iss, "US-061: CurStepChanged больше не нужен"
    # Регресс: детект занятости — на запись; краш {app} не вернулся.
    assert "GENERIC_WRITE" in iss and "ERROR_SHARING_VIOLATION" in iss
    assert "ExpandConstant('{app}" not in iss


def test_ui_accept_closes_without_relauncher():
    src = (REPO_ROOT / "voice_input_app" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    accept = ast.get_source_segment(src, _find(tree, "_accept_centralized_update")) or ""
    assert "really_quit" in accept, "US-059: accept закрывает приложение"
    assert "_spawn_update_relauncher" not in accept, "US-061: accept НЕ спавнит релончер"

    decline = ast.get_source_segment(src, _find(tree, "_decline_centralized_update")) or ""
    assert "set_update_decline" in decline, "US-059: decline пишет update-decline"

    # US-061: во всём ui.py нет релончера / powershell / маркера завершения / флага.
    for gone in ("_spawn_update_relauncher", "EncodedCommand", "--after-update", "update-done"):
        assert gone not in src, f"US-061: осталось упоминание {gone}"


def _run():
    for t in (test_decline_marker_roundtrip,
              test_installer_busy_single_pass_no_done_marker,
              test_ui_accept_closes_without_relauncher):
        t()
        print("PASS:", t.__name__)
    u.clear_update_decline()
    print("US-059/US-061 regression: ALL PASS")


if __name__ == "__main__":
    _run()
