from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

from .logger import get_logger

log = get_logger("hotkeys")

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_KEY_ALIASES = {
    "space": 0x20,
    "esc": 0x1B,
    "escape": 0x1B,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "ins": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "plus": 0xBB,
    "minus": 0xBD,
}


def normalize_hotkey(display: str) -> str:
    text = display.strip().lower()
    text = text.replace("control", "ctrl")
    text = text.replace(" ", "")
    return text


@dataclass(frozen=True)
class _Win32HotkeyHandle:
    hwnd: int
    hotkey_id: int


@dataclass(frozen=True)
class _KeyboardHotkeyHandle:
    handle: object


class HotkeyService:
    """Global hotkey service.

    On Windows, use the native RegisterHotKey API. It is more stable than the
    low-level keyboard hook used by the keyboard package when apps such as OBS,
    softphones, games or conferencing tools are running in the background.
    """

    _next_win32_id = 0xB120

    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self._handle: _Win32HotkeyHandle | _KeyboardHotkeyHandle | None = None
        self._hotkey = ""
        self._backend = ""

    @property
    def hotkey(self) -> str:
        return self._hotkey

    @property
    def backend(self) -> str:
        return self._backend

    def start(self, hotkey: str, window_handle: int | None = None) -> None:
        new_hotkey = normalize_hotkey(hotkey)
        if not new_hotkey:
            raise RuntimeError("Горячая клавиша не задана. Нажмите поле и выберите новую комбинацию.")

        if os.name == "nt" and window_handle:
            new_handle = self._register_win32_hotkey(new_hotkey, int(window_handle))
            new_backend = "win32"
        else:
            new_handle = self._register_keyboard_hotkey(new_hotkey)
            new_backend = "keyboard"

        old_handle = self._handle
        self._handle = new_handle
        self._hotkey = new_hotkey
        self._backend = new_backend
        self._unregister_handle(old_handle)
        log.info("Hotkey registered: %s backend=%s", self._hotkey, self._backend)

    def stop(self) -> None:
        self._unregister_handle(self._handle)
        self._handle = None
        self._backend = ""

    def handle_native_event(self, message) -> bool:  # noqa: ANN001
        """Return True when a Windows WM_HOTKEY event belongs to this service."""
        if not isinstance(self._handle, _Win32HotkeyHandle):
            return False
        try:
            msg = wintypes.MSG.from_address(int(message))
        except Exception:
            return False
        if int(msg.message) != WM_HOTKEY:
            return False
        if int(msg.wParam) != self._handle.hotkey_id:
            return False
        log.info("Hotkey event received: %s backend=win32", self._hotkey)
        return True

    def _register_keyboard_hotkey(self, hotkey: str) -> _KeyboardHotkeyHandle:
        try:
            import keyboard

            handle = keyboard.add_hotkey(hotkey, self.callback, suppress=False, trigger_on_release=False)
            return _KeyboardHotkeyHandle(handle=handle)
        except Exception as exc:
            raise RuntimeError(
                "Не удалось зарегистрировать глобальную горячую клавишу. "
                "Нажмите подсвеченное поле и выберите другую комбинацию, например Ctrl+Alt+Space. "
                f"Детали: {exc}"
            ) from exc

    def _register_win32_hotkey(self, hotkey: str, hwnd: int) -> _Win32HotkeyHandle:
        modifiers, vk = _parse_win32_hotkey(hotkey)
        hotkey_id = self._allocate_win32_id()
        user32 = _user32()
        ok = user32.RegisterHotKey(
            wintypes.HWND(hwnd),
            ctypes.c_int(hotkey_id),
            wintypes.UINT(modifiers | MOD_NOREPEAT),
            wintypes.UINT(vk),
        )
        if not ok:
            last_error = ctypes.get_last_error()
            if last_error == 1409:  # ERROR_HOTKEY_ALREADY_REGISTERED
                detail = "эта комбинация уже используется другой программой"
            else:
                detail = ctypes.FormatError(last_error).strip() if last_error else "Windows отказала в регистрации"
            raise RuntimeError(
                "Не удалось зарегистрировать глобальную горячую клавишу через Windows. "
                "Возможно, такая комбинация уже занята OBS, телефонией или другой программой. "
                "Выберите другую комбинацию, например Ctrl+Alt+Space или Ctrl+Alt+D. "
                f"Детали: {detail}"
            )
        return _Win32HotkeyHandle(hwnd=hwnd, hotkey_id=hotkey_id)

    @classmethod
    def _allocate_win32_id(cls) -> int:
        cls._next_win32_id += 1
        if cls._next_win32_id > 0xBFFF:
            cls._next_win32_id = 0xB120
        return cls._next_win32_id

    def _unregister_handle(self, handle: _Win32HotkeyHandle | _KeyboardHotkeyHandle | None) -> None:
        if handle is None:
            return
        if isinstance(handle, _Win32HotkeyHandle):
            try:
                _user32().UnregisterHotKey(wintypes.HWND(handle.hwnd), ctypes.c_int(handle.hotkey_id))
            except Exception:
                pass
            return
        try:
            import keyboard

            keyboard.remove_hotkey(handle.handle)
        except Exception:
            pass


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    user32.RegisterHotKey.restype = wintypes.BOOL
    user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL
    return user32


def _parse_win32_hotkey(hotkey: str) -> tuple[int, int]:
    parts = [part for part in normalize_hotkey(hotkey).split("+") if part]
    if not parts:
        raise RuntimeError("Горячая клавиша не задана.")

    modifiers = 0
    key = ""
    for part in parts:
        if part in {"ctrl", "control"}:
            modifiers |= MOD_CONTROL
        elif part == "alt":
            modifiers |= MOD_ALT
        elif part == "shift":
            modifiers |= MOD_SHIFT
        elif part in {"win", "meta", "cmd"}:
            modifiers |= MOD_WIN
        else:
            if key:
                raise RuntimeError(f"Не удалось разобрать горячую клавишу: {hotkey}")
            key = part

    if not key:
        raise RuntimeError("Горячая клавиша должна содержать не только модификаторы, но и клавишу.")

    if len(key) == 1 and "a" <= key <= "z":
        vk = ord(key.upper())
    elif len(key) == 1 and "0" <= key <= "9":
        vk = ord(key)
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in _KEY_ALIASES:
        vk = _KEY_ALIASES[key]
    else:
        raise RuntimeError(
            f"Клавиша «{key}» не поддерживается для системной горячей клавиши Windows. "
            "Выберите комбинацию с буквой, цифрой, F1–F24 или Space."
        )

    return modifiers, vk
