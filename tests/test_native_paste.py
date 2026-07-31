from __future__ import annotations

import ctypes
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app import insert


class FakeBackend:
    def __init__(self, *responses: tuple[int, int], pressed: bool = False) -> None:
        self.responses = list(responses)
        self.pressed = pressed
        self.checked_keys: tuple[int, ...] | None = None
        self.calls: list[list[insert.INPUT]] = []

    def any_pressed(self, virtual_keys: tuple[int, ...]) -> bool:
        self.checked_keys = virtual_keys
        return self.pressed

    def send(self, inputs: list[insert.INPUT]) -> tuple[int, int]:
        self.calls.append(inputs)
        if not self.responses:
            return len(inputs), 0
        return self.responses.pop(0)


def _event_signature(events: list[insert.INPUT]) -> list[tuple[int, int]]:
    return [(int(event.ki.wVk), int(event.ki.dwFlags)) for event in events]


def test_input_structure_matches_win32_abi() -> None:
    expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(insert.INPUT) == expected_size


def test_send_ctrl_v_submits_one_complete_ordered_batch() -> None:
    backend = FakeBackend((4, 0))

    assert insert._send_ctrl_v(backend) is True
    assert len(backend.calls) == 1
    assert _event_signature(backend.calls[0]) == [
        (insert.VK_CONTROL, 0),
        (insert.VK_V, 0),
        (insert.VK_V, insert.KEYEVENTF_KEYUP),
        (insert.VK_CONTROL, insert.KEYEVENTF_KEYUP),
    ]


def test_send_ctrl_v_refuses_to_mix_with_pressed_keys() -> None:
    backend = FakeBackend(pressed=True)

    assert insert._send_ctrl_v(backend) is False
    assert backend.calls == []
    assert backend.checked_keys is not None
    assert insert.VK_CONTROL in backend.checked_keys
    assert insert.VK_V in backend.checked_keys


def test_zero_event_failure_is_reported_without_cleanup() -> None:
    backend = FakeBackend((0, 5))

    assert insert._send_ctrl_v(backend) is False
    assert len(backend.calls) == 1


def test_partial_send_releases_v_and_ctrl_and_reports_failure() -> None:
    backend = FakeBackend((2, 5), (2, 0))

    assert insert._send_ctrl_v(backend) is False
    assert len(backend.calls) == 2
    assert _event_signature(backend.calls[1]) == [
        (insert.VK_V, insert.KEYEVENTF_KEYUP),
        (insert.VK_CONTROL, insert.KEYEVENTF_KEYUP),
    ]


def test_cleanup_failure_does_not_turn_partial_send_into_success() -> None:
    backend = FakeBackend((1, 5), (0, 5))

    assert insert._send_ctrl_v(backend) is False
    assert len(backend.calls) == 2


def test_copy_and_maybe_paste_returns_false_for_failed_send() -> None:
    copied: list[str] = []
    originals = (
        insert.pyperclip.copy,
        insert.foreground_belongs_to_current_process,
        insert.focused_control_accepts_text,
        insert._send_ctrl_v,
        insert.time.sleep,
    )
    try:
        insert.pyperclip.copy = copied.append
        insert.foreground_belongs_to_current_process = lambda: False
        insert.focused_control_accepts_text = lambda: True
        insert._send_ctrl_v = lambda backend=None: False
        insert.time.sleep = lambda _seconds: None

        inserted = insert.copy_and_maybe_paste("Проверка", auto_paste=True)
    finally:
        (
            insert.pyperclip.copy,
            insert.foreground_belongs_to_current_process,
            insert.focused_control_accepts_text,
            insert._send_ctrl_v,
            insert.time.sleep,
        ) = originals

    assert copied == ["Проверка"]
    assert inserted is False


def test_copy_and_maybe_paste_refuses_changed_foreground_window() -> None:
    calls: list[str] = []
    originals = (
        insert.pyperclip.copy,
        insert.foreground_belongs_to_current_process,
        insert.foreground_window_handle,
        insert.focused_control_accepts_text,
        insert._send_ctrl_v,
    )
    try:
        insert.pyperclip.copy = lambda text: calls.append(f"copy:{text}")
        insert.foreground_belongs_to_current_process = lambda: False
        insert.foreground_window_handle = lambda: 222
        insert.focused_control_accepts_text = lambda: True
        insert._send_ctrl_v = lambda backend=None: calls.append("send") or True

        inserted = insert.copy_and_maybe_paste(
            "Проверка",
            auto_paste=True,
            expected_foreground_hwnd=111,
        )
    finally:
        (
            insert.pyperclip.copy,
            insert.foreground_belongs_to_current_process,
            insert.foreground_window_handle,
            insert.focused_control_accepts_text,
            insert._send_ctrl_v,
        ) = originals

    assert inserted is False
    assert calls == ["copy:Проверка"]


def test_failed_send_routes_ui_to_overlay_result() -> None:
    source = (REPO_ROOT / "voice_input_app" / "ui.py").read_text(encoding="utf-8")
    delivery = source.split("def _deliver_dictation_result", 1)[1].split("def _start_dictation_postprocess", 1)[0]
    assert "if not inserted:" in delivery
    assert "show_overlay_result = not self.recording_started_in_own_window" in delivery
    assert "self.overlay.show_result_text(text)" in delivery


def test_runtime_no_longer_depends_on_pyautogui() -> None:
    files = [
        REPO_ROOT / "voice_input_app" / "insert.py",
        REPO_ROOT / "requirements.txt",
        REPO_ROOT / "build_exe.bat",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in files).lower()
    assert "import pyautogui" not in combined
    assert "pyautogui>=" not in combined
    assert "hidden-import pyautogui" not in combined


def _run() -> None:
    tests = [
        test_input_structure_matches_win32_abi,
        test_send_ctrl_v_submits_one_complete_ordered_batch,
        test_send_ctrl_v_refuses_to_mix_with_pressed_keys,
        test_zero_event_failure_is_reported_without_cleanup,
        test_partial_send_releases_v_and_ctrl_and_reports_failure,
        test_cleanup_failure_does_not_turn_partial_send_into_success,
        test_copy_and_maybe_paste_returns_false_for_failed_send,
        test_copy_and_maybe_paste_refuses_changed_foreground_window,
        test_failed_send_routes_ui_to_overlay_result,
        test_runtime_no_longer_depends_on_pyautogui,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("Native paste regression: ALL PASS")


if __name__ == "__main__":
    _run()
