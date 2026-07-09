# -*- coding: utf-8 -*-
"""Регресс-тесты US-058: тихое централизованное обновление при ОТКРЫТОМ
ПРОСТАИВАЮЩЕМ приложении с показом процесса и авто-перезапуском.

Модель: при простое установщик НЕ полагается на CloseApplications (кросс-сессионно
SYSTEM->пользователь не закрывает трей-приложение — причина провала v4.17.3).
Он пишет маркер update-close и в том же прогоне (single-pass) ждёт освобождения
.exe. Приложение по тику busy-таймера видит маркер при простое -> показывает
сообщение, запускает фоновый релончер (powershell, без нового бинарника) и тихо
закрывается. После установки релончер перезапускает приложение с --after-update,
новая копия показывает тост «Обновлено до vX».

Инварианты:
1. Функциональный: update_signal — маркер update-close создаётся/читается/снимается.
2. Статический guard ui.py: idle-close только при простое (frozen), из тика таймера;
   спавнит релончер и закрывается; startup-clear маркера; тост при --after-update.
3. Статический guard установщика: idle single-pass (ExeIsFree/WaitForExeFree/
   CreateFileW/update-close), при таймауте код 101; US-057 (busy) не регрессирует.

Запуск: python tests/test_us058_idle_update.py
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


def test_update_close_marker_read_write_clear():
    u.clear_update_close()
    assert not u.is_update_close()
    assert u.close_request_path().name == "update-close.flag"
    assert "VoiceInputLocal" in str(u.close_request_path())

    u.set_update_close()
    assert u.is_update_close()
    u.clear_update_close()
    assert not u.is_update_close()

    # Контракт с установщиком: имя маркера зафиксировано.
    assert u.UPDATE_CLOSE_NAME == "update-close.flag"


def test_ui_wires_idle_update_close():
    ui_src = (REPO_ROOT / "voice_input_app" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(ui_src)

    for name in ("_check_idle_update_close", "_spawn_update_relauncher", "notify_after_update"):
        assert _find_func(tree, name) is not None, f"нет метода {name}"

    check = ast.get_source_segment(ui_src, _find_func(tree, "_check_idle_update_close")) or ""
    assert "is_update_close" in check, "idle-close должен читать маркер update-close"
    assert "_app_is_busy" in check, "idle-close должен закрываться ТОЛЬКО при простое"
    assert "frozen" in check, "idle-close только для frozen-сборки (в dev перезапуск бессмыслен)"
    assert "_spawn_update_relauncher" in check, "idle-close должен запускать релончер"
    assert "really_quit" in check, "idle-close должен полностью закрывать приложение"
    assert "_idle_update_closing" in check, "idle-close должен иметь re-entrancy guard"

    spawn = ast.get_source_segment(ui_src, _find_func(tree, "_spawn_update_relauncher")) or ""
    assert "sys.executable" in spawn, "релончер перезапускает тот же .exe"
    assert "--after-update" in spawn, "релончер стартует приложение с флагом --after-update"
    assert "EncodedCommand" in spawn, "PS передаётся через -EncodedCommand (без проблем с кавычками)"
    assert "LastWriteTimeUtc" in spawn, "готовность = смена времени модификации (установщик заменил .exe)"
    assert "DETACHED_PROCESS" in spawn, "релончер отцеплен (переживает закрытие приложения)"

    notify = ast.get_source_segment(ui_src, _find_func(tree, "notify_after_update")) or ""
    assert "showMessage" in notify, "после обновления — видимый тост"
    assert "__version__" in notify, "тост показывает новую версию"

    tick = ast.get_source_segment(ui_src, _find_func(tree, "_update_busy_marker")) or ""
    assert "_check_idle_update_close" in tick, "idle-close не вызывается из тика busy-таймера"

    # startup-clear устаревшего маркера (безопасность после крэша).
    assert "clear_update_close" in ui_src, "нет startup-clear маркера update-close"

    # run() показывает тост при перезапуске релончером.
    run = ast.get_source_segment(ui_src, _find_func(tree, "run")) or ""
    assert "--after-update" in run and "notify_after_update" in run, "run() не обрабатывает --after-update"


def test_installer_idle_single_pass():
    iss = (REPO_ROOT / "installer" / "VoiceInputLocal.iss").read_text(encoding="utf-8")
    # US-058: single-pass ожидание освобождения .exe через WinAPI.
    for token in ("function ExeIsFree", "function WaitForExeFree", "CreateFileW",
                  "CloseHandle", "update-close.flag", "CLOSE_WAIT_TIMEOUT_MS",
                  "WaitForExeFree(CLOSE_WAIT_TIMEOUT_MS)", "procedure WriteMarker"):
        assert token in iss, f"установщик не содержит {token}"
    # idle-ветка пишет update-close и ждёт (а не полагается на CloseApplications).
    assert "WriteMarker('update-close.flag')" in iss, "idle-ветка должна писать update-close"

    # US-057 (busy) не регрессирует: единый код 101, маркер pending, users-modify.
    for token in ("function InitializeSetup", "busy.lock", "update-pending.flag",
                  "ExitProcess", "EXIT_DEFERRED_BUSY = 101", "users-modify"):
        assert token in iss, f"US-057 регресс: нет {token}"
    # Упрощение US-057 сохранено: кода 100 / declined быть не должно.
    assert "update-declined" not in iss, "маркер update-declined должен отсутствовать"


def test_us057_dialog_not_regressed():
    """US-057 (занятый путь) остаётся: окно только при занятости, из тика."""
    ui_src = (REPO_ROOT / "voice_input_app" / "ui.py").read_text(encoding="utf-8")
    tree = ast.parse(ui_src)
    for name in ("_check_pending_update", "_accept_centralized_update", "_decline_centralized_update"):
        assert _find_func(tree, name) is not None, f"US-057 регресс: нет {name}"
    check = ast.get_source_segment(ui_src, _find_func(tree, "_check_pending_update")) or ""
    assert "is_update_pending" in check and "_app_is_busy" in check


def _run():
    tests = [
        test_update_close_marker_read_write_clear,
        test_ui_wires_idle_update_close,
        test_installer_idle_single_pass,
        test_us057_dialog_not_regressed,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    # На всякий случай не оставляем маркер (иначе frozen-экземпляр закрылся бы).
    u.clear_update_close()
    print("US-058 idle-update regression: ALL PASS")


if __name__ == "__main__":
    _run()
