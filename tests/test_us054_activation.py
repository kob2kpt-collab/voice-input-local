# -*- coding: utf-8 -*-
"""Регресс-тест US-054: повторный запуск ярлыка разворачивает работающий экземпляр.

Механизм: первый экземпляр слушает QLocalServer; второй сигналит через QLocalSocket
(_activate_running_instance) и выходит; при недоступности канала — fallback на
прежнее уведомление «уже запущено».

1. Хелпер (реальный _activate_running_instance):
   - с живым сервером -> True (подключился и отправил сигнал);
   - без сервера -> False (сработает fallback-уведомление).
2. Канал: чистый синхронный round-trip QLocalServer/QLocalSocket доставляет "SHOW".
3. Статический guard: run() слушает канал (QLocalServer/newConnection -> show_from_tray)
   и сигналит во втором пути (_activate_running_instance) с fallback QMessageBox;
   show_from_tray выводит окно на передний план (Windows SetForegroundWindow).

Headless (QT_QPA_PLATFORM=offscreen). Запуск (в venv приложения):
    .venv\\Scripts\\python.exe tests\\test_us054_activation.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"

from PySide6.QtWidgets import QApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket

_APP = QApplication.instance() or QApplication(sys.argv)

from voice_input_app.ui import _activate_running_instance, _single_instance_server_name


def _method_src(src: str, name: str) -> str:
    marker = f"    def {name}(self"
    i = src.index(marker)
    j = src.find("\n    def ", i + len(marker))
    return src[i: j if j != -1 else len(src)]


def _func_src(src: str, name: str) -> str:
    marker = f"\ndef {name}("
    i = src.index(marker)
    j = src.find("\ndef ", i + len(marker))
    return src[i: j if j != -1 else len(src)]


def test_helper_signals_when_server_present() -> None:
    """Реальный _activate_running_instance подключается к живому серверу -> True."""
    name = _single_instance_server_name() + "-selftest-present"
    QLocalServer.removeServer(name)
    server = QLocalServer()
    assert server.listen(name), f"не удалось поднять QLocalServer: {server.errorString()}"
    try:
        ok = _activate_running_instance(name, timeout_ms=800)
        assert ok is True, "хелпер вернул False при живом сервере (сигнал не отправлен)"
    finally:
        server.close()
        QLocalServer.removeServer(name)


def test_helper_false_without_server() -> None:
    """Без сервера -> False (в run() сработает fallback-уведомление «уже запущено»)."""
    name = _single_instance_server_name() + "-selftest-absent"
    QLocalServer.removeServer(name)
    ok = _activate_running_instance(name, timeout_ms=300)
    assert ok is False, "при отсутствии сервера хелпер должен вернуть False"


def test_channel_delivers_show() -> None:
    """Чистый синхронный round-trip: QLocalServer/QLocalSocket доставляет сигнал SHOW."""
    name = _single_instance_server_name() + "-selftest-channel"
    QLocalServer.removeServer(name)
    server = QLocalServer()
    assert server.listen(name), f"listen failed: {server.errorString()}"
    client = QLocalSocket()
    try:
        client.connectToServer(name)
        assert client.waitForConnected(1000), "клиент не подключился"
        # Сначала принимаем соединение на сервере, потом пишем — соединение живо.
        assert server.waitForNewConnection(1000), "сервер не увидел соединение"
        incoming = server.nextPendingConnection()
        assert incoming is not None, "нет pending-соединения"
        client.write(b"SHOW\n")
        client.flush()
        client.waitForBytesWritten(1000)  # без assert: flush() мог уже отправить (тогда pending нет)
        assert incoming.waitForReadyRead(1000), "сервер не получил данные"
        data = bytes(incoming.readAll())
        assert b"SHOW" in data, f"не тот сигнал: {data!r}"
    finally:
        client.abort()
        server.close()
        QLocalServer.removeServer(name)


def test_run_static_guard() -> None:
    src = UI_PATH.read_text(encoding="utf-8")
    assert "def _activate_running_instance(" in src, "нет хелпера _activate_running_instance"
    run_src = _func_src(src, "run")
    assert "_activate_running_instance(" in run_src, "run() не сигналит работающему экземпляру"
    assert "QLocalServer" in run_src and "newConnection" in run_src, "run() не слушает канал активации"
    assert "show_from_tray()" in run_src, "run() не разворачивает окно по сигналу"
    assert "QMessageBox.information" in run_src, "нет fallback-уведомления «уже запущено»"
    sft = _method_src(src, "show_from_tray")
    assert "SetForegroundWindow" in sft, "show_from_tray не выводит окно на передний план (Windows)"
    assert "AllowSetForegroundWindow" in src, "нет обхода foreground-lock во втором процессе"


def _run() -> None:
    tests = [
        test_helper_signals_when_server_present,
        test_helper_false_without_server,
        test_channel_delivers_show,
        test_run_static_guard,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print("US-054 regression: ALL PASS")


if __name__ == "__main__":
    _run()
