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
VK_MENU = 0x12
VK_V = 0x56
VK_LWIN = 0x5B
VK_RWIN = 0x5C


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    ]


class MOUSEINPUT(ctypes.Structure):
    # INPUT is a union. MOUSEINPUT must be present even though this module only
    # sends keys; otherwise ctypes would calculate the wrong INPUT size on x64.
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
    """Small, write-only wrapper around Win32 SendInput.

    It does not install hooks or receive keyboard events. GetAsyncKeyState is
    queried once immediately before pasting so we do not mix Ctrl+V with keys
    that the user is physically holding.
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
    cleanup = [
        _keyboard_event(VK_V, key_up=True),
        _keyboard_event(VK_CONTROL, key_up=True),
    ]
    try:
        sent, error_code = backend.send(cleanup)
        if sent != len(cleanup):
            log.error(
                "Synthetic paste key cleanup incomplete: accepted=%d expected=%d error=%d",
                sent,
                len(cleanup),
                error_code,
            )
    except Exception:  # noqa: BLE001
        log.exception("Could not release synthetic paste keys after partial SendInput")


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


def foreground_window_handle() -> Optional[int]:
    """Return the current foreground window handle on Windows."""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()  # type: ignore[attr-defined]
        return int(hwnd) if hwnd else None
    except Exception:
        return None


def foreground_window_pid() -> Optional[int]:
    """Return the process id of the current foreground window on Windows."""
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
    """Return True when foreground window is exactly the provided HWND.

    The floating overlay belongs to this process too. After the user drags or
    clicks it, a process-level foreground check would incorrectly mark the
    recording as started inside the main app and suppress the final preview.
    """
    if hwnd is None:
        return False
    foreground = foreground_window_handle()
    return foreground is not None and int(foreground) == int(hwnd)


def _win32_caret_is_visible() -> Optional[bool]:
    """Return True when Win32 reports a caret/focused text control.

    This catches many classic Win32, Qt, Electron and browser text fields even when
    UI Automation exposes incomplete metadata. It returns None on non-Windows or if
    the Win32 call cannot be used.
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
    """Return True/False if Windows can decide, None if unavailable.

    The app must not paste when there is no active text field. The implementation
    therefore uses both UI Automation and a Win32 caret check. Some modern apps still
    expose focus poorly, so this remains best effort rather than a Windows guarantee.
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


def _send_ctrl_v(backend: InputBackend | None = None) -> bool:
    """Submit one atomic Ctrl+V batch through Win32 SendInput.

    True means Windows accepted all four input events. It does not claim that
    an arbitrary target application changed its document; Windows exposes no
    privacy-preserving, universal acknowledgement for that.
    """
    try:
        backend = backend or Win32InputBackend()
        guard_keys = (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN, VK_V)
        if backend.any_pressed(guard_keys):
            log.warning("Auto-paste skipped because a modifier or V is currently pressed")
            return False

        events = _ctrl_v_events()
        sent, error_code = backend.send(events)
        if sent == len(events):
            log.info("Auto-paste SendInput accepted all %d events", sent)
            return True
        if sent > 0:
            _release_paste_keys(backend)
        log.warning(
            "Auto-paste SendInput incomplete: accepted=%d expected=%d error=%d; showing fallback result",
            sent,
            len(events),
            error_code,
        )
        return False
    except Exception:  # noqa: BLE001
        log.exception("Auto-paste SendInput failed; showing fallback result")
        return False


def copy_and_maybe_paste(
    text: str,
    auto_paste: bool,
    only_when_text_field_detected: bool = True,
    *,
    allow_current_process: bool = False,
    expected_foreground_hwnd: int | None = None,
) -> bool:
    """Always copy text to clipboard. Return True if Ctrl+V was sent.

    When only_when_text_field_detected=True, paste is allowed only if Windows reports
    an active text field/caret. If no text field is detected, the text remains in the
    clipboard and history, but the app does not type into a random window.

    By default, Ctrl+V is blocked when the foreground window belongs to Voice Input
    Local itself. The app updates its own dictation field directly; sending Ctrl+V
    into that field would duplicate the transcript.
    """
    if not text:
        return False
    pyperclip.copy(text)
    if not auto_paste:
        return False
    if not allow_current_process and foreground_belongs_to_current_process():
        return False
    if expected_foreground_hwnd is not None and foreground_window_handle() != expected_foreground_hwnd:
        log.info("Auto-paste skipped because the foreground window changed since recording started")
        return False

    detection = focused_control_accepts_text()
    if only_when_text_field_detected and detection is not True:
        return False

    time.sleep(0.12)
    # Focus may change while clipboard/UI Automation work is in progress. Do a
    # final identity check immediately before emitting Ctrl+V.
    if expected_foreground_hwnd is not None and foreground_window_handle() != expected_foreground_hwnd:
        log.info("Auto-paste skipped because the foreground window changed before SendInput")
        return False
    return _send_ctrl_v()
