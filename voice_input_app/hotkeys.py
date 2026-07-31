from __future__ import annotations

import ctypes
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication


log = logging.getLogger(__name__)

WM_HOTKEY = 0x0312

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_F1 = 0x70

_MODIFIER_CODES = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}


def normalize_hotkey(display: str) -> str:
    text = display.strip().lower()
    text = text.replace("control", "ctrl")
    text = text.replace("windows", "win")
    text = text.replace("meta", "win")
    text = text.replace(" ", "")
    return text


@dataclass(frozen=True)
class NativeHotkey:
    text: str
    modifiers: int
    virtual_key: int


def parse_hotkey(display: str) -> NativeHotkey:
    """Convert a user shortcut to the Win32 RegisterHotKey representation.

    Voice Input Local deliberately supports one regular key plus at least one
    modifier. This keeps the system-wide registration explicit and avoids the
    all-key state tracking required for multi-key chords and sequences.
    """

    text = normalize_hotkey(display)
    if not text:
        raise ValueError("Горячая клавиша не задана.")
    if "," in text:
        raise ValueError("Последовательности клавиш не поддерживаются. Используйте одну комбинацию, например Ctrl+Space.")

    parts = text.split("+")
    if any(not part for part in parts):
        raise ValueError("Комбинация содержит неподдерживаемую клавишу.")

    modifiers = 0
    regular_keys: list[str] = []
    canonical_modifiers: list[str] = []
    for part in parts:
        if part in _MODIFIER_CODES:
            if part not in canonical_modifiers:
                canonical_modifiers.append(part)
                modifiers |= _MODIFIER_CODES[part]
        else:
            regular_keys.append(part)

    if not modifiers:
        raise ValueError("Добавьте Ctrl, Alt, Shift или Win к основной клавише.")
    if len(regular_keys) != 1:
        raise ValueError("Комбинация должна содержать ровно одну основную клавишу.")

    key = regular_keys[0]
    if len(key) == 1 and "a" <= key <= "z":
        virtual_key = ord(key.upper())
    elif len(key) == 1 and "0" <= key <= "9":
        virtual_key = ord(key)
    elif key in {"space", "spacebar"}:
        key = "space"
        virtual_key = VK_SPACE
    else:
        match = re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", key)
        if match:
            virtual_key = VK_F1 + int(match.group(1)) - 1
        else:
            raise ValueError("Поддерживаются буквы A–Z, цифры 0–9, Space и клавиши F1–F24.")

    ordered_modifiers = [name for name in ("ctrl", "alt", "shift", "win") if name in canonical_modifiers]
    canonical = "+".join([*ordered_modifiers, key])
    return NativeHotkey(canonical, modifiers | MOD_NOREPEAT, virtual_key)


class Win32HotkeyBackend:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Глобальные горячие клавиши поддерживаются только в Windows.")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self._user32.RegisterHotKey.restype = wintypes.BOOL
        self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.UnregisterHotKey.restype = wintypes.BOOL

    def register(self, hotkey_id: int, hotkey: NativeHotkey) -> None:
        ctypes.set_last_error(0)
        if not self._user32.RegisterHotKey(None, hotkey_id, hotkey.modifiers, hotkey.virtual_key):
            error_code = ctypes.get_last_error()
            detail = ctypes.FormatError(error_code).strip() if error_code else "комбинация занята другой программой"
            raise OSError(error_code, detail)

    def unregister(self, hotkey_id: int) -> None:
        if not self._user32.UnregisterHotKey(None, hotkey_id):
            error_code = ctypes.get_last_error()
            if error_code:
                log.warning("UnregisterHotKey failed: id=%s error=%s", hotkey_id, error_code)


class HotkeyService(QAbstractNativeEventFilter):
    """System-wide shortcuts implemented with RegisterHotKey, without hooks."""

    def __init__(
        self,
        on_trigger: Callable[[], None],
        *,
        backend: Win32HotkeyBackend | None = None,
        application: QCoreApplication | None = None,
    ) -> None:
        super().__init__()
        self.on_trigger = on_trigger
        self.callback = on_trigger  # Compatibility with older integrations.
        self._backend = backend
        self._application = application
        self._filter_installed = False
        self._main_id: int | None = None
        self._cancel_id: int | None = None
        self._cancel_callback: Callable[[], None] | None = None
        self._next_id = 0x5600
        self._hotkey = ""

    @property
    def hotkey(self) -> str:
        return self._hotkey

    @property
    def mode(self) -> str:
        return "toggle"

    def _ensure_runtime(self) -> None:
        if self._backend is None:
            self._backend = Win32HotkeyBackend()
        if self._application is None:
            self._application = QCoreApplication.instance()
        if self._application is None:
            raise RuntimeError("Qt application is not initialized.")
        if not self._filter_installed:
            self._application.installNativeEventFilter(self)
            self._filter_installed = True

    def _allocate_id(self) -> int:
        self._next_id += 1
        if self._next_id > 0xBFFF:
            self._next_id = 0x5601
        while self._next_id in {self._main_id, self._cancel_id}:
            self._next_id += 1
        return self._next_id

    def start(self, hotkey: str) -> None:
        new_hotkey = parse_hotkey(hotkey)
        self._ensure_runtime()
        if new_hotkey.text == self._hotkey and self._main_id is not None:
            return
        new_id = self._allocate_id()
        try:
            assert self._backend is not None
            self._backend.register(new_id, new_hotkey)
        except Exception as exc:
            raise RuntimeError(
                "Не удалось зарегистрировать глобальную горячую клавишу. "
                "Выберите другую комбинацию, например Ctrl+Alt+Space. "
                f"Детали: {exc}"
            ) from exc

        old_id = self._main_id
        self._main_id = new_id
        self._hotkey = new_hotkey.text
        if old_id is not None:
            self._backend.unregister(old_id)

    def register_cancel(self, callback: Callable[[], None]) -> None:
        if self._cancel_id is not None:
            self._cancel_callback = callback
            return
        self._ensure_runtime()
        cancel_id = self._allocate_id()
        cancel_hotkey = NativeHotkey("esc", MOD_NOREPEAT, VK_ESCAPE)
        assert self._backend is not None
        self._backend.register(cancel_id, cancel_hotkey)
        self._cancel_id = cancel_id
        self._cancel_callback = callback

    def unregister_cancel(self) -> None:
        cancel_id = self._cancel_id
        self._cancel_id = None
        self._cancel_callback = None
        if cancel_id is not None and self._backend is not None:
            self._backend.unregister(cancel_id)

    def _dispatch_hotkey(self, hotkey_id: int) -> None:
        try:
            if hotkey_id == self._main_id:
                self.on_trigger()
            elif hotkey_id == self._cancel_id and self._cancel_callback is not None:
                self._cancel_callback()
        except Exception:
            log.exception("Native hotkey callback failed: id=%s", hotkey_id)

    def nativeEventFilter(self, event_type, message):  # noqa: ANN001, N802
        if event_type in (b"windows_dispatcher_MSG", b"windows_generic_MSG"):
            try:
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY:
                    self._dispatch_hotkey(int(msg.wParam))
            except (TypeError, ValueError):
                log.exception("Could not decode native Windows message")
        return False

    def stop(self) -> None:
        self.unregister_cancel()
        if self._main_id is not None and self._backend is not None:
            self._backend.unregister(self._main_id)
        self._main_id = None
        self._hotkey = ""
        if self._filter_installed and self._application is not None:
            self._application.removeNativeEventFilter(self)
            self._filter_installed = False
