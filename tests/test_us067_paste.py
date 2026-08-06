# -*- coding: utf-8 -*-
r"""Регресс-тесты US-067: выбор окна для вставки и отправка Ctrl+V через SendInput.

Что закрывается:
1. Выбор целевого окна («на момент завершения записи» — прежнее поведение по
   умолчанию, «на момент начала записи» — вставка только в то же окно).
2. Честный отчёт о вставке: раньше `keyboard.send` сообщал об успехе, даже
   когда система ввод не приняла (окно с повышенными правами), и пользователь
   видел «текст вставлен» при пустом поле.
3. Ожидание освобождения удерживаемых клавиш вместо отказа с первой попытки.
4. Отпускание синтетических клавиш после неполной отправки — иначе в системе
   остался бы «зажатый» Ctrl.
5. Зависимости `keyboard` и `pyautogui` удалены из кода и сборки.

Тест headless: Win32 не вызывается — бэкенд ввода, буфер обмена и определение
активного окна подменены. Запуск (в venv приложения):
    .venv\Scripts\python.exe tests\test_us067_paste.py
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app import insert as insert_module  # noqa: E402
from voice_input_app.insert import (  # noqa: E402
    INPUT,
    KEYEVENTF_KEYUP,
    PASTE_GUARD_KEYS,
    VK_CONTROL,
    VK_V,
    _ctrl_v_events,
    _send_ctrl_v,
    copy_and_maybe_paste,
    wait_for_guard_keys_released,
)

UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"
CONFIG_PATH = REPO_ROOT / "voice_input_app" / "config.py"
INSERT_PATH = REPO_ROOT / "voice_input_app" / "insert.py"
REQUIREMENTS_PATH = REPO_ROOT / "requirements.txt"
BUILD_PATH = REPO_ROOT / "build_exe.bat"


class FakeBackend:
    """Подмена SendInput: считает отправленное и умеет отказывать."""

    def __init__(self, *, pressed: tuple[int, ...] = (), accept: int | None = None) -> None:
        self.pressed = set(pressed)
        self.accept = accept  # сколько событий «примет» Windows; None — все
        self.batches: list[list[INPUT]] = []
        self.press_checks = 0
        self.checked_keys: list[tuple[int, ...]] = []

    def any_pressed(self, virtual_keys: tuple[int, ...]) -> bool:
        self.press_checks += 1
        self.checked_keys.append(tuple(virtual_keys))
        return any(key in self.pressed for key in virtual_keys)

    def send(self, inputs: list[INPUT]) -> tuple[int, int]:
        self.batches.append(list(inputs))
        if self.accept is None:
            return len(inputs), 0
        return min(self.accept, len(inputs)), 5  # ERROR_ACCESS_DENIED


def _describe(batch: list[INPUT]) -> list[tuple[int, bool]]:
    return [(int(item.ki.wVk), bool(item.ki.dwFlags & KEYEVENTF_KEYUP)) for item in batch]


def test_ctrl_v_batch_is_well_formed() -> None:
    """Порядок событий: Ctrl вниз, V вниз, V вверх, Ctrl вверх."""
    assert _describe(_ctrl_v_events()) == [
        (VK_CONTROL, False),
        (VK_V, False),
        (VK_V, True),
        (VK_CONTROL, True),
    ]
    # Размер INPUT критичен: ошибка в union дала бы отказ SendInput на x64.
    assert ctypes.sizeof(INPUT) in (28, 40), ctypes.sizeof(INPUT)


def test_paste_reports_success_only_when_windows_accepted_everything() -> None:
    backend = FakeBackend()
    assert _send_ctrl_v(backend) is True
    assert len(backend.batches) == 1


def test_rejected_injection_is_reported_as_failure() -> None:
    """Окно с повышенными правами: система ввод не принимает вовсе."""
    backend = FakeBackend(accept=0)
    assert _send_ctrl_v(backend) is False, "отказ системы выдан за успешную вставку"
    assert len(backend.batches) == 1, "лишние попытки после отказа"


def test_partial_injection_releases_keys_and_reports_failure() -> None:
    """Приняли часть событий — Ctrl нельзя оставлять нажатым."""
    backend = FakeBackend(accept=2)
    assert _send_ctrl_v(backend) is False
    assert len(backend.batches) == 2, "не выполнено принудительное отпускание клавиш"
    assert _describe(backend.batches[1]) == [(VK_V, True), (VK_CONTROL, True)]


def test_held_keys_are_awaited_not_rejected_immediately() -> None:
    """TASK-344: удержанный модификатор даёт ожидание, а не отказ."""
    backend = FakeBackend(pressed=(VK_CONTROL,))
    clock = {"now": 0.0}

    def monotonic() -> float:
        return clock["now"]

    def sleep(seconds: float) -> None:
        clock["now"] += seconds
        if clock["now"] >= 0.1:  # пользователь отпустил клавишу
            backend.pressed.clear()

    assert wait_for_guard_keys_released(backend, timeout=0.6, poll=0.025, monotonic=monotonic, sleep=sleep) is True
    assert backend.press_checks > 1, "не было ни одной повторной проверки — ожидание не работает"


def test_keys_held_for_too_long_fall_back() -> None:
    backend = FakeBackend(pressed=(VK_CONTROL,))
    clock = {"now": 0.0}

    def monotonic() -> float:
        return clock["now"]

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    assert wait_for_guard_keys_released(backend, timeout=0.2, poll=0.025, monotonic=monotonic, sleep=sleep) is False
    assert _send_ctrl_v(FakeBackend(pressed=(VK_CONTROL,))) is False, "вставка выполнена с удержанным Ctrl"


def test_guard_reads_only_a_short_fixed_key_list() -> None:
    """Проверяется короткий явный список клавиш, а не произвольный ввод."""
    backend = FakeBackend()
    _send_ctrl_v(backend)
    assert backend.checked_keys, "состояние клавиш не проверялось вовсе"
    for keys in backend.checked_keys:
        assert keys == PASTE_GUARD_KEYS
    assert len(PASTE_GUARD_KEYS) == 6


class ClipboardSpy:
    def __init__(self) -> None:
        self.copied: list[str] = []

    def copy(self, text: str) -> None:
        self.copied.append(text)


def _run_paste(monkey: dict, **kwargs) -> tuple[bool, ClipboardSpy, list[str]]:
    """Выполнить copy_and_maybe_paste с подменёнными Win32-зависимостями."""
    clipboard = ClipboardSpy()
    sent: list[str] = []
    state = {"foreground": monkey.get("foreground", 111)}
    originals = {
        "pyperclip": insert_module.pyperclip,
        "foreground_belongs_to_current_process": insert_module.foreground_belongs_to_current_process,
        "foreground_window_handle": insert_module.foreground_window_handle,
        "focused_control_accepts_text": insert_module.focused_control_accepts_text,
        "_send_ctrl_v": insert_module._send_ctrl_v,
        "_request_window_activation": insert_module._request_window_activation,
        "time": insert_module.time,
    }

    def request_activation(_hwnd: int) -> bool:
        """monkey['activates']: куда встанет фокус, либо None — Windows отказала."""
        target = monkey.get("activates")
        if target is None:
            return False
        state["foreground"] = target
        return True

    insert_module.pyperclip = clipboard
    insert_module.foreground_belongs_to_current_process = lambda: monkey.get("own_process", False)
    insert_module.foreground_window_handle = lambda: state["foreground"]
    insert_module.focused_control_accepts_text = lambda: monkey.get("text_field", True)
    insert_module._send_ctrl_v = lambda: (sent.append("ctrl+v"), True)[1]
    insert_module._request_window_activation = request_activation

    class _NoSleep:
        monotonic = staticmethod(lambda: 0.0)
        sleep = staticmethod(lambda _s: None)

    insert_module.time = _NoSleep
    try:
        result = copy_and_maybe_paste("текст", auto_paste=True, **kwargs)
    finally:
        for name, value in originals.items():
            setattr(insert_module, name, value)
    return result, clipboard, sent


def test_default_mode_pastes_into_whatever_window_is_active() -> None:
    """Режим «на момент завершения»: окно не сверяется (прежнее поведение)."""
    result, clipboard, sent = _run_paste({"foreground": 999}, expected_foreground_hwnd=None)
    assert result is True and sent == ["ctrl+v"]
    assert clipboard.copied == ["текст"], "текст обязан попадать в буфер в любом случае"


def test_same_window_mode_pastes_when_window_unchanged() -> None:
    result, _clipboard, sent = _run_paste({"foreground": 111}, expected_foreground_hwnd=111)
    assert result is True and sent == ["ctrl+v"]


def test_same_window_mode_returns_focus_and_pastes() -> None:
    """US-071: фокус возвращается окну, в котором начиналась запись."""
    result, _clipboard, sent = _run_paste(
        {"foreground": 222, "activates": 111},
        expected_foreground_hwnd=111,
    )
    assert result is True, "текст не попал в окно, где начиналась диктовка"
    assert sent == ["ctrl+v"]


def test_same_window_mode_falls_back_when_windows_refuses() -> None:
    """Windows вправе не отдать фокус — тогда честный фолбэк, а не вставка вслепую."""
    result, clipboard, sent = _run_paste(
        {"foreground": 222, "activates": None},
        expected_foreground_hwnd=111,
    )
    assert result is False, "вставка выполнена, хотя фокус остался у чужого окна"
    assert sent == [], "Ctrl+V ушёл в чужое окно"
    assert clipboard.copied == ["текст"], "текст потерян — его нет даже в буфере"


def test_activation_waits_for_the_window_to_actually_come_forward() -> None:
    """Запрос активации принят — но результат проверяется опросом, а не на веру."""
    state = {"foreground": 222}
    clock = {"now": 0.0}

    def request(_hwnd: int) -> bool:
        return True

    def sleep(seconds: float) -> None:
        clock["now"] += seconds
        if clock["now"] >= 0.1:
            state["foreground"] = 111  # окно вышло вперёд не мгновенно

    saved = (insert_module._request_window_activation, insert_module.foreground_window_handle)
    insert_module._request_window_activation = request
    insert_module.foreground_window_handle = lambda: state["foreground"]
    try:
        assert insert_module.activate_window(
            111, timeout=0.6, poll=0.025, monotonic=lambda: clock["now"], sleep=sleep
        ) is True
        state["foreground"] = 222
        clock["now"] = 0.0
        assert insert_module.activate_window(
            111, timeout=0.2, poll=0.025, monotonic=lambda: clock["now"], sleep=lambda _s: clock.__setitem__("now", clock["now"] + _s)
        ) is False, "молчаливый отказ Windows принят за успешную активацию"
    finally:
        insert_module._request_window_activation, insert_module.foreground_window_handle = saved


def test_paste_is_skipped_without_text_field() -> None:
    result, clipboard, sent = _run_paste({"text_field": False}, only_when_text_field_detected=True)
    assert result is False and sent == []
    assert clipboard.copied == ["текст"]


def test_paste_is_skipped_for_own_window() -> None:
    result, _clipboard, sent = _run_paste({"own_process": True})
    assert result is False and sent == []


def test_config_has_paste_target_setting() -> None:
    src = CONFIG_PATH.read_text(encoding="utf-8")
    assert 'paste_target_window: str = "on_finish"' in src, "нет настройки целевого окна или изменён дефолт"

    from voice_input_app.config import AppConfig

    assert AppConfig().paste_target_window == "on_finish", "поведение существующих пользователей изменилось"


def test_ui_wires_the_setting() -> None:
    src = UI_PATH.read_text(encoding="utf-8")
    assert '"В окно, активное на момент завершения записи", "on_finish"' in src
    assert '"В окно, активное на момент начала записи", "on_start"' in src
    assert "self.recording_target_hwnd = foreground_window_handle()" in src, "окно начала записи не запоминается"
    assert "expected_foreground_hwnd=expected_hwnd" in src, "выбор окна не доходит до вставки"
    assert 'self.cfg.paste_target_window = str(self.paste_target_combo.currentData() or "on_finish")' in src


def test_keyboard_and_pyautogui_are_gone() -> None:
    """TASK-345: библиотеки удалены из кода и сборки."""
    insert_src = INSERT_PATH.read_text(encoding="utf-8")
    for forbidden in ("import keyboard", "keyboard.send", "import pyautogui", "pyautogui.hotkey"):
        assert forbidden not in insert_src, f"в insert.py осталось: {forbidden}"

    requirements = REQUIREMENTS_PATH.read_text(encoding="utf-8").lower()
    assert "keyboard>=" not in requirements
    assert "pyautogui" not in requirements

    build = BUILD_PATH.read_text(encoding="utf-8").lower()
    assert "hidden-import keyboard" not in build
    assert "hidden-import pyautogui" not in build

    ui_src = UI_PATH.read_text(encoding="utf-8")
    assert "import keyboard" not in ui_src
    assert "pyautogui" not in ui_src


def test_no_keyboard_hook_is_installed_anywhere() -> None:
    """Отправка ввода — не перехват: хук не ставится и сырой ввод не читается."""
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (INSERT_PATH, REPO_ROOT / "voice_input_app" / "hotkeys.py", REPO_ROOT / "voice_input_app" / "key_watch.py")
    )
    for forbidden in ("SetWindowsHookEx", "WH_KEYBOARD_LL", "RegisterRawInputDevices"):
        assert forbidden not in sources, f"добавлен перехват клавиатуры: {forbidden}"


def _run() -> None:
    tests = [
        test_ctrl_v_batch_is_well_formed,
        test_paste_reports_success_only_when_windows_accepted_everything,
        test_rejected_injection_is_reported_as_failure,
        test_partial_injection_releases_keys_and_reports_failure,
        test_held_keys_are_awaited_not_rejected_immediately,
        test_keys_held_for_too_long_fall_back,
        test_guard_reads_only_a_short_fixed_key_list,
        test_default_mode_pastes_into_whatever_window_is_active,
        test_same_window_mode_pastes_when_window_unchanged,
        test_same_window_mode_returns_focus_and_pastes,
        test_same_window_mode_falls_back_when_windows_refuses,
        test_activation_waits_for_the_window_to_actually_come_forward,
        test_paste_is_skipped_without_text_field,
        test_paste_is_skipped_for_own_window,
        test_config_has_paste_target_setting,
        test_ui_wires_the_setting,
        test_keyboard_and_pyautogui_are_gone,
        test_no_keyboard_hook_is_installed_anywhere,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("US-067 regression: ALL PASS")


if __name__ == "__main__":
    _run()
