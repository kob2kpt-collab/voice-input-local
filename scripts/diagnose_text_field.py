# -*- coding: utf-8 -*-
r"""Диагностика: что Windows сообщает о поле ввода в активном окне.

Нужен, когда автовставка не срабатывает в какой-то программе (VK Teams, Claude
Desktop и другие приложения на Chromium и Qt рисуют поля ввода сами, поэтому
системной каретки у них нет). Скрипт ТОЛЬКО читает состояние и печатает его —
никаких нажатий не отправляет и ничего не меняет.

Запуск из папки проекта:
    .venv\Scripts\python.exe scripts\diagnose_text_field.py

Скрипт делает несколько снимков с паузами: после запуска переключитесь в нужную
программу и поставьте курсор в поле ввода. Для сравнения полезно снять и
Блокнот — в нём автовставка работает.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app.insert import (  # noqa: E402
    GUITHREADINFO,
    _win32_caret_is_visible,
    focused_control_accepts_text,
    foreground_window_handle,
)

SNAPSHOTS = 4
DELAY_SECONDS = 8


def _window_text(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = int(user32.GetWindowTextLengthW(wintypes.HWND(hwnd)))
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(wintypes.HWND(hwnd), buffer, length + 1)
    return buffer.value


def _window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(wintypes.HWND(hwnd), buffer, 256)
    return buffer.value


def _process_name(hwnd: int) -> str:
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return f"pid={pid.value} (нет доступа)"
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return f"{Path(buffer.value).name} (pid={pid.value})"
        return f"pid={pid.value}"
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _gui_thread_info(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    thread_id = user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None)
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        return "GetGUIThreadInfo не ответил (ошибка %d)" % ctypes.get_last_error()
    caret_width = info.rcCaret.right - info.rcCaret.left
    caret_height = info.rcCaret.bottom - info.rcCaret.top
    return (
        f"hwndFocus={info.hwndFocus or 0} hwndCaret={info.hwndCaret or 0} "
        f"каретка {caret_width}x{caret_height}"
    )


def _uia_focused() -> str:
    try:
        import uiautomation as auto  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return f"UI Automation недоступна: {exc}"
    try:
        control = auto.GetFocusedControl()
    except Exception as exc:  # noqa: BLE001
        return f"GetFocusedControl упал: {exc}"
    if control is None:
        return "GetFocusedControl вернул None"
    parts = []
    for attribute in ("ControlTypeName", "ClassName", "AutomationId", "Name"):
        value = str(getattr(control, attribute, "") or "")
        parts.append(f"{attribute}={value[:60]!r}")
    return " ".join(parts)


def snapshot(index: int) -> None:
    hwnd = foreground_window_handle()
    print(f"\n=== Снимок {index} из {SNAPSHOTS} ===")
    if not hwnd:
        print("Активного окна нет.")
        return
    print(f"Окно:      {_window_text(hwnd)!r}")
    print(f"Класс:     {_window_class(hwnd)}")
    print(f"Процесс:   {_process_name(hwnd)}")
    print(f"Win32:     {_gui_thread_info(hwnd)}")
    print(f"Каретка:   {_win32_caret_is_visible()}")
    print(f"UIA:       {_uia_focused()}")
    verdict = focused_control_accepts_text()
    print(f"ИТОГ приложения: focused_control_accepts_text() = {verdict}")
    if verdict is True:
        print("           -> автовставка разрешена")
    else:
        print("           -> при включённой «Безопасной вставке» автовставка блокируется")


def main() -> int:
    if sys.platform != "win32":
        print("Диагностика работает только в Windows.")
        return 1
    print(
        f"Будет сделано {SNAPSHOTS} снимка(ов) с паузой {DELAY_SECONDS} с.\n"
        "После каждого обратного отсчёта переключитесь в нужную программу и\n"
        "поставьте курсор в поле ввода. Полезный порядок: VK Teams, Claude Desktop,\n"
        "Блокнот (в нём вставка работает — эталон для сравнения), любое другое окно."
    )
    for index in range(1, SNAPSHOTS + 1):
        for remaining in range(DELAY_SECONDS, 0, -1):
            print(f"\rСнимок {index}: осталось {remaining:2d} с… ", end="", flush=True)
            time.sleep(1)
        snapshot(index)
    print("\nГотово. Пришлите этот вывод — по нему видно, чего именно не хватает детектору.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
