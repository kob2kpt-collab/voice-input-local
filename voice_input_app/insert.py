# -*- coding: utf-8 -*-
"""Вставка расшифрованного текста в активное окно (US-067).

Раньше Ctrl+V отправлялся сторонней библиотекой keyboard, а при её отказе —
второй такой же библиотекой. Две беды: (1) обе сообщали об успехе, даже когда
вставка на самом деле не проходила — например, в окно программы, запущенной с
повышенными правами (Windows блокирует ввод из процесса с меньшим уровнем
целостности), и пользователь видел «текст вставлен», а текста не было;
(2) ради одной отправки Ctrl+V в сборку тянулись зависимости, одна из которых
ставит низкоуровневый клавиатурный хук.

Теперь Ctrl+V уходит одним пакетом через Win32 `SendInput`. Это ТОЛЬКО отправка
ввода: хук не ставится, чужие нажатия не читаются. Состояние клавиш
запрашивается у ОС (`GetAsyncKeyState`) по короткому явному списку и только
перед вставкой — чтобы не подмешать к Ctrl+V клавиши, которые пользователь
физически удерживает (иначе вместо вставки получится Ctrl+Shift+V или
Ctrl+Alt+V — в разных программах это разные команды).

Базовая реализация SendInput взята из PR #3 внешнего контрибьютора
(kob2kpt-collab/voice-input-local). Отличия: удерживаемые клавиши не отменяют
вставку с первой же попытки, а коротко ожидаются (US-067, TASK-344), и
добавлена привязка к окну, выбранному пользователем (`expected_foreground_hwnd`).
"""
from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes
from typing import Optional, Protocol

import pyperclip

log = logging.getLogger(__name__)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_V = 0x56
VK_LWIN = 0x5B
VK_RWIN = 0x5C

# Клавиши, которые нельзя удерживать в момент вставки: они превратили бы Ctrl+V
# в другую команду. Список фиксированный и короткий — читается только он.
PASTE_GUARD_KEYS = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN, VK_V)

# TASK-344: сколько ждать освобождения клавиш, прежде чем отказаться от вставки.
# Типичный случай — пользователь ещё держит горячую клавишу в момент, когда
# короткая диктовка уже расшифрована. Прежняя реализация из PR #3 отказывалась
# с первой попытки, и это выглядело как «иногда не вставляет».
MODIFIER_WAIT_SECONDS = 0.6
MODIFIER_POLL_SECONDS = 0.025

# Задержка перед Ctrl+V: буферу обмена Windows нужно время устояться.
CLIPBOARD_SETTLE_SECONDS = 0.12


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", RECT),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class MOUSEINPUT(ctypes.Structure):
    # INPUT — это union. MOUSEINPUT обязан присутствовать, хотя модуль шлёт
    # только клавиши: иначе ctypes посчитает неверный размер INPUT на x64.
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("value", _INPUTUNION),
    ]


class InputBackend(Protocol):
    def any_pressed(self, virtual_keys: tuple[int, ...]) -> bool: ...

    def send(self, inputs: list[INPUT]) -> tuple[int, int]: ...


class Win32InputBackend:
    """Тонкая обёртка над Win32 SendInput — только отправка ввода.

    Хук не ставится и события не перехватываются. `GetAsyncKeyState`
    запрашивается по фиксированному короткому списку клавиш и только перед
    вставкой.
    """

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Автовставка поддерживается только в Windows.")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self._user32.GetAsyncKeyState.restype = ctypes.c_short
        self._user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self._user32.SendInput.restype = wintypes.UINT

    def any_pressed(self, virtual_keys: tuple[int, ...]) -> bool:
        return any(bool(self._user32.GetAsyncKeyState(key) & 0x8000) for key in virtual_keys)

    def send(self, inputs: list[INPUT]) -> tuple[int, int]:
        batch = (INPUT * len(inputs))(*inputs)
        ctypes.set_last_error(0)
        sent = int(self._user32.SendInput(len(batch), batch, ctypes.sizeof(INPUT)))
        return sent, int(ctypes.get_last_error())


def foreground_window_handle() -> Optional[int]:
    """Дескриптор окна, активного в системе прямо сейчас."""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()  # type: ignore[attr-defined]
        return int(hwnd) if hwnd else None
    except Exception:
        return None


def foreground_window_pid() -> Optional[int]:
    """Идентификатор процесса, которому принадлежит активное окно."""
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = foreground_window_handle()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        value = int(pid.value)
        return value if value > 0 else None
    except Exception:
        return None


def foreground_belongs_to_current_process() -> bool:
    pid = foreground_window_pid()
    return pid is not None and pid == os.getpid()


def foreground_matches_window_handle(hwnd: int | None) -> bool:
    """True, если активно ровно указанное окно.

    Плавающая плашка принадлежит этому же процессу. После её перетаскивания или
    клика проверка «активное окно принадлежит нашему процессу» ошибочно считала
    бы диктовку начатой внутри приложения и подавляла показ результата.
    """
    if hwnd is None:
        return False
    foreground = foreground_window_handle()
    return foreground is not None and int(foreground) == int(hwnd)


def _win32_caret_is_visible() -> Optional[bool]:
    """True, если Win32 сообщает о курсоре ввода в активном окне.

    Ловит многие классические Win32-, Qt-, Electron- и браузерные поля даже
    тогда, когда UI Automation отдаёт неполные сведения. None — не Windows или
    вызов недоступен.
    """
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return None
        if info.hwndCaret:
            return True
        rect_has_size = (info.rcCaret.right - info.rcCaret.left) != 0 or (info.rcCaret.bottom - info.rcCaret.top) != 0
        if info.hwndFocus and rect_has_size:
            return True
        return False
    except Exception:
        return None


def focused_control_accepts_text() -> Optional[bool]:
    """True/False, если Windows может ответить; None — если сведений нет.

    Приложение не должно вставлять текст, когда активного поля ввода нет,
    поэтому используются и UI Automation, и проверка курсора через Win32.
    Некоторые современные программы плохо раскрывают фокус, так что это
    добросовестная оценка, а не гарантия Windows.
    """
    uia_result: Optional[bool] = None
    try:
        import uiautomation as auto  # type: ignore

        control = auto.GetFocusedControl()
        if control is not None:
            control_type = str(getattr(control, "ControlTypeName", ""))
            class_name = str(getattr(control, "ClassName", ""))
            automation_id = str(getattr(control, "AutomationId", ""))
            name = f"{control_type} {class_name} {automation_id}".lower()

            positive = (
                "edit" in name
                or "document" in name
                or "text" in name
                or "richedit" in name
                or "scintilla" in name
                or "textbox" in name
                or "chrome_renderwidgethosthwnd" in name
                or "mozillawindowclass" in name
                or "internet explorer_server" in name
                or "windows.ui.composition" in name
            )
            negative = any(term in name for term in ("button", "menu", "tab", "listitem", "checkbox", "combobox"))
            if positive and not negative:
                uia_result = True
            elif negative:
                uia_result = False
    except Exception:
        uia_result = None

    caret_result = _win32_caret_is_visible()
    if uia_result is True or caret_result is True:
        return True
    if uia_result is False and caret_result is False:
        return False
    if uia_result is not None:
        return uia_result
    return caret_result


def _keyboard_event(virtual_key: int, *, key_up: bool = False) -> INPUT:
    event = INPUT()
    event.type = INPUT_KEYBOARD
    event.ki = KEYBDINPUT(
        wVk=virtual_key,
        wScan=0,
        dwFlags=KEYEVENTF_KEYUP if key_up else 0,
        time=0,
        dwExtraInfo=0,
    )
    return event


def _ctrl_v_events() -> list[INPUT]:
    return [
        _keyboard_event(VK_CONTROL),
        _keyboard_event(VK_V),
        _keyboard_event(VK_V, key_up=True),
        _keyboard_event(VK_CONTROL, key_up=True),
    ]


def _release_paste_keys(backend: InputBackend) -> None:
    """Отпустить синтетические клавиши после неполной отправки.

    Если Windows приняла нажатие Ctrl, но не приняла отпускание, система
    осталась бы с «зажатым» Ctrl — и следующий обычный набор пользователя
    превратился бы в сочетания клавиш.
    """
    cleanup = [
        _keyboard_event(VK_V, key_up=True),
        _keyboard_event(VK_CONTROL, key_up=True),
    ]
    try:
        sent, error_code = backend.send(cleanup)
        if sent != len(cleanup):
            log.error(
                "Не удалось отпустить синтетические клавиши после неполной вставки: принято=%d из %d, ошибка=%d",
                sent,
                len(cleanup),
                error_code,
            )
    except Exception:  # noqa: BLE001
        log.exception("Сбой при отпускании синтетических клавиш после неполной вставки")


def wait_for_guard_keys_released(
    backend: InputBackend,
    *,
    timeout: float = MODIFIER_WAIT_SECONDS,
    poll: float = MODIFIER_POLL_SECONDS,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> bool:
    """Подождать, пока пользователь отпустит модификаторы и V (TASK-344).

    Возвращает True, если к концу ожидания клавиши свободны. Типичный случай —
    клавиши свободны сразу, и функция не ждёт вовсе.
    """
    deadline = monotonic() + max(0.0, timeout)
    while True:
        if not backend.any_pressed(PASTE_GUARD_KEYS):
            return True
        if monotonic() >= deadline:
            return False
        sleep(poll)


def _send_ctrl_v(backend: InputBackend | None = None) -> bool:
    """Отправить Ctrl+V одним пакетом через SendInput.

    True означает, что Windows приняла все четыре события ввода. Это НЕ
    утверждение, что произвольная программа изменила свой документ: такого
    универсального подтверждения Windows не даёт. Зато честно различается
    случай, когда система ввод отклонила (например, окно с повышенными
    правами) — тогда возвращается False, и пользователь получает текст в
    буфере и в плашке вместо ложного «текст вставлен».
    """
    try:
        backend = backend or Win32InputBackend()
        if not wait_for_guard_keys_released(backend):
            log.warning("Автовставка пропущена: модификатор или V удерживаются дольше ожидания")
            return False

        events = _ctrl_v_events()
        sent, error_code = backend.send(events)
        if sent == len(events):
            log.info("Автовставка: Windows приняла все %d события ввода", sent)
            return True
        if sent > 0:
            _release_paste_keys(backend)
        log.warning(
            "Автовставка не прошла: принято=%d из %d, ошибка=%d; показываю текст в плашке",
            sent,
            len(events),
            error_code,
        )
        return False
    except Exception:  # noqa: BLE001
        log.exception("Сбой отправки Ctrl+V; показываю текст в плашке")
        return False


def copy_and_maybe_paste(
    text: str,
    auto_paste: bool,
    only_when_text_field_detected: bool = True,
    *,
    allow_current_process: bool = False,
    expected_foreground_hwnd: int | None = None,
) -> bool:
    """Всегда кладёт текст в буфер обмена. True — если Ctrl+V был принят системой.

    При only_when_text_field_detected=True вставка разрешена только когда
    Windows сообщает об активном поле ввода. Если поля нет, текст остаётся в
    буфере и в истории, но приложение не печатает в случайное окно.

    По умолчанию Ctrl+V не отправляется, когда активно окно самого Voice Input
    Local: своё поле диктовки приложение заполняет напрямую, и вставка
    продублировала бы текст.

    `expected_foreground_hwnd` (US-067) — окно, в которое пользователь просил
    вставлять. Задаётся в режиме «в окно, активное на момент начала записи»;
    если к моменту вставки активно другое окно, вставка не выполняется.
    """
    if not text:
        return False
    pyperclip.copy(text)
    if not auto_paste:
        return False
    if not allow_current_process and foreground_belongs_to_current_process():
        return False
    if expected_foreground_hwnd is not None and foreground_window_handle() != expected_foreground_hwnd:
        log.info("Автовставка пропущена: активное окно сменилось с момента начала записи")
        return False

    detection = focused_control_accepts_text()
    if only_when_text_field_detected and detection is not True:
        return False

    time.sleep(CLIPBOARD_SETTLE_SECONDS)
    # Фокус мог смениться, пока шла работа с буфером и UI Automation. Последняя
    # проверка — непосредственно перед отправкой Ctrl+V.
    if expected_foreground_hwnd is not None and foreground_window_handle() != expected_foreground_hwnd:
        log.info("Автовставка пропущена: активное окно сменилось перед отправкой Ctrl+V")
        return False
    return _send_ctrl_v()
