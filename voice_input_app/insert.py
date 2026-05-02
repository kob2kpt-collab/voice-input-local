from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from typing import Optional

import pyperclip


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


def _send_ctrl_v() -> bool:
    """Send Ctrl+V using the most reliable available method."""
    try:
        import keyboard  # type: ignore

        keyboard.send("ctrl+v")
        return True
    except Exception:
        pass

    try:
        import pyautogui  # type: ignore

        pyautogui.hotkey("ctrl", "v")
        return True
    except Exception:
        return False


def copy_and_maybe_paste(text: str, auto_paste: bool, only_when_text_field_detected: bool = True, *, allow_current_process: bool = False) -> bool:
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

    detection = focused_control_accepts_text()
    if only_when_text_field_detected and detection is not True:
        return False

    time.sleep(0.12)
    return _send_ctrl_v()
