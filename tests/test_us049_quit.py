# -*- coding: utf-8 -*-
"""Регресс-тест US-049: гарантированный полный выход из трея.

Баг: после «Выйти» из трея процесс оставался в Диспетчере задач, оверлей висел,
диктовка не работала. Реальная причина (подтверждена логами: running_workers=none,
overlay-пикер сработал через 33с после quit): QApplication.quit(), вызванный из
ВЛОЖЕННОГО event loop контекстного меню трея, завершает только вложенный цикл —
главный app.exec() продолжает крутиться. Фикс: really_quit гасит фон и в конце
ПРИНУДИТЕЛЬНО завершает процесс через os._exit(0).

Проверить баг вручную ненадёжно — это гонка (зависит от тайминга закрытия меню).
Поэтому тест детерминированный:

1. Поведенческий (главный): в ПОДПРОЦЕССЕ вызывается РЕАЛЬНЫЙ MainWindow.really_quit
   с mock-self. Если фикс на месте, процесс завершается кодом 0 (os._exit(0)
   достигнут). Если os._exit убрать — really_quit вернётся управление, подпроцесс
   дойдёт до маркерного os._exit(99) → тест упадёт. Если really_quit бросит
   исключение — подпроцесс завершится кодом 1 → тест упадёт.

2. Статический guard: really_quit содержит re-entrancy guard (_quitting), стоп
   таймеров (QTimer .stop()), вызов _shutdown_workers() и финальный os._exit(0);
   методы _shutdown_workers и _log_running_workers_on_quit существуют.

Тест headless (Qt-виджеты не создаются — всё замокано). Запуск (в venv приложения):
    .venv\\Scripts\\python.exe tests\\test_us049_quit.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"


def _method_src(src: str, name: str) -> str:
    """Вернуть текст метода name (от 'def name' до следующего метода того же уровня)."""
    marker = f"    def {name}(self"
    i = src.index(marker)
    j = src.find("\n    def ", i + len(marker))
    return src[i: j if j != -1 else len(src)]


def test_really_quit_static_guard() -> None:
    src = UI_PATH.read_text(encoding="utf-8")
    assert "def _shutdown_workers(self)" in src, "нет метода _shutdown_workers"
    assert "def _log_running_workers_on_quit(self)" in src, "нет метода _log_running_workers_on_quit"
    body = _method_src(src, "really_quit")
    assert "_quitting" in body, "really_quit без re-entrancy guard (_quitting)"
    assert "QTimer" in body and ".stop()" in body, "really_quit не останавливает таймеры (QTimer.stop)"
    assert "_shutdown_workers()" in body, "really_quit не зовёт _shutdown_workers()"
    assert "os._exit(0)" in body, "really_quit не гарантирует завершение — нет os._exit(0)"


def test_really_quit_terminates_process() -> None:
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")  # на случай создания Qt-объектов при импорте
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child-really-quit"],
        timeout=90,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert proc.returncode == 0, (
        f"really_quit не завершил процесс кодом 0 (rc={proc.returncode}). "
        f"Значит os._exit(0) не достигнут / был удалён, либо метод упал.\n"
        f"stderr: {proc.stderr.decode('utf-8', 'replace')[:800]}"
    )


def _child_really_quit() -> None:
    """Тело подпроцесса: вызвать РЕАЛЬНЫЙ really_quit с mock-self.

    Все внешние зависимости (Qt-виджеты, hotkey, рекордер) замокированы, поэтому
    выполняется настоящая логика really_quit (+ настоящий _shutdown_workers), но
    без реального GUI. Успешный фикс приводит к os._exit(0) внутри really_quit.
    """
    import types

    from voice_input_app.ui import MainWindow

    mock = types.SimpleNamespace(
        cfg=types.SimpleNamespace(api_enabled=True),
        recorder=types.SimpleNamespace(is_recording=False, cancel=lambda: None),
        hotkey=types.SimpleNamespace(stop=lambda: None),
        tray=types.SimpleNamespace(hide=lambda: None),
        overlay=types.SimpleNamespace(hide=lambda: None),
        stop_escape_watch=lambda: None,  # EPIC-13: отмена по Escape — наблюдение, не хоткей
        _stop_session_lock_watch=lambda: None,  # US-069: наблюдение за блокировкой экрана
        # _log замокан, чтобы тест не писал в app.log; _shutdown_workers — настоящий.
        _log_running_workers_on_quit=lambda: None,
    )
    mock._shutdown_workers = types.MethodType(MainWindow._shutdown_workers, mock)

    MainWindow.really_quit(mock)

    # Сюда попадать нельзя: really_quit обязан завершить процесс через os._exit(0).
    os._exit(99)


def _run() -> None:
    tests = [
        test_really_quit_static_guard,
        test_really_quit_terminates_process,
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print("US-049 regression: ALL PASS")


if __name__ == "__main__":
    if "--child-really-quit" in sys.argv:
        _child_really_quit()
    else:
        _run()
