# -*- coding: utf-8 -*-
r"""Регресс-тесты US-070: когда именно принимается решение о вставке.

Два дефекта, найденные при проверке US-067 на устройстве (VK Teams, Claude
Desktop — вставки нет, Блокнот — есть):

1. Настройка «Вставлять только если Windows видит активное поле ввода» на
   диктовку не влияла: она передавалась внутрь `copy_and_maybe_paste`, но
   внешний гейт в `_deliver_dictation_result` безусловно требовал
   `live_target_is_text_field`. В программах, рисующих поле самостоятельно
   (Chromium, Qt), включить вставку было нельзя вообще ничем.
2. Наличие поля определялось ОДИН раз — в начале записи. Если в тот момент
   курсор был не в поле, решение «не вставлять» фиксировалось навсегда, даже
   когда к концу диктовки курсор уже стоял где надо.

Тест headless: Win32 не вызывается, вставка и буфер обмена подменены.
Запуск (в venv приложения):
    .venv\Scripts\python.exe tests\test_us070_paste_gate.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LOCALAPPDATA"] = tempfile.gettempdir()

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app import insert as insert_module  # noqa: E402
from voice_input_app.insert import copy_and_maybe_paste  # noqa: E402

UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"


def _method_source(name: str) -> str:
    src = UI_PATH.read_text(encoding="utf-8")
    start = src.index(f"    def {name}(self")
    end = src.find("\n    def ", start + 10)
    return src[start: end if end != -1 else len(src)]


def _delivery_window(
    *,
    auto_paste: bool = True,
    safe_paste: bool = True,
    field_at_start: bool,
    own_window: bool = False,
    stopped_by_lock: bool = False,
):
    """Mock-окно с настоящим `MainWindow._deliver_dictation_result`."""
    from voice_input_app.ui import MainWindow

    events: list[str] = []
    window = types.SimpleNamespace(
        events=events,
        cfg=types.SimpleNamespace(
            auto_paste=auto_paste,
            paste_only_when_text_field_detected=safe_paste,
            overlay_enabled=True,
            selected_model="whisper:small",
            paste_target_window="on_finish",
        ),
        # Снимок, сделанный в начале записи: именно он раньше решал всё.
        live_target_is_text_field=field_at_start,
        recording_started_in_own_window=own_window,
        recording_target_hwnd=None,
        _dictation_stopped_by_lock=stopped_by_lock,
        last_text=types.SimpleNamespace(setPlainText=lambda _t: None),
        history=types.SimpleNamespace(add=lambda *a: events.append("history.add")),
        refresh_history=lambda: None,
        update_recording_badge=lambda: None,
        toggle_btn=types.SimpleNamespace(setEnabled=lambda _v: None, setText=lambda _t: None),
        result_preview_active=False,
        result_preview_text="",
        stop_escape_watch=lambda: None,
        start_escape_watch=lambda: None,
        overlay=types.SimpleNamespace(
            show_result_text=lambda _t: events.append("overlay.result"),
            show_idle=lambda: None,
        ),
        status_label=types.SimpleNamespace(setText=lambda t: events.append(f"status:{t}")),
        _cleanup_wav=lambda _p: None,
    )
    window._deliver_dictation_result = types.MethodType(MainWindow._deliver_dictation_result, window)
    return window


def _run_delivery(window, *, field_at_delivery: bool):
    """Доставить текст, подменив Win32-зависимости вставки."""
    from voice_input_app import ui as ui_module

    attempts: list[dict] = []
    copied: list[str] = []
    saved_ui = (ui_module.copy_and_maybe_paste, ui_module.pyperclip)
    saved_insert = (
        insert_module.pyperclip,
        insert_module.foreground_belongs_to_current_process,
        insert_module.foreground_window_handle,
        insert_module.focused_control_accepts_text,
        insert_module._send_ctrl_v,
        insert_module.time,
    )

    class _NoSleep:
        monotonic = staticmethod(lambda: 0.0)
        sleep = staticmethod(lambda _s: None)

    insert_module.pyperclip = types.SimpleNamespace(copy=lambda t: copied.append(t))
    insert_module.foreground_belongs_to_current_process = lambda: False
    insert_module.foreground_window_handle = lambda: 111
    insert_module.focused_control_accepts_text = lambda: field_at_delivery
    insert_module._send_ctrl_v = lambda: True
    insert_module.time = _NoSleep

    def spy(text, auto_paste, only_when_text_field_detected=True, **kwargs):
        attempts.append({"only_when_text_field_detected": only_when_text_field_detected})
        return copy_and_maybe_paste(text, auto_paste, only_when_text_field_detected, **kwargs)

    ui_module.copy_and_maybe_paste = spy
    ui_module.pyperclip = types.SimpleNamespace(copy=lambda t: copied.append(t))
    try:
        window._deliver_dictation_result("сказанный текст", 3.0, Path("nonexistent.wav"))
    finally:
        ui_module.copy_and_maybe_paste, ui_module.pyperclip = saved_ui
        (
            insert_module.pyperclip,
            insert_module.foreground_belongs_to_current_process,
            insert_module.foreground_window_handle,
            insert_module.focused_control_accepts_text,
            insert_module._send_ctrl_v,
            insert_module.time,
        ) = saved_insert
    return attempts, copied


def test_safe_paste_on_still_blocks_when_no_text_field() -> None:
    """Галочка включена и поля нет — вставки нет (прежнее защитное поведение)."""
    window = _delivery_window(field_at_start=True, safe_paste=True)
    attempts, copied = _run_delivery(window, field_at_delivery=False)

    assert attempts, "вставку даже не пробовали — гейт снова решает за пользователя"
    assert attempts[0]["only_when_text_field_detected"] is True
    assert copied, "текст обязан попасть в буфер обмена"
    assert "overlay.result" in window.events, "текст не показан в плашке"


def test_safe_paste_off_pastes_where_windows_sees_no_field() -> None:
    """ГЛАВНОЕ исправление: снятая галочка теперь действительно включает вставку."""
    window = _delivery_window(field_at_start=False, safe_paste=False)
    attempts, _copied = _run_delivery(window, field_at_delivery=False)

    assert attempts, "снятая «Безопасная вставка» по-прежнему ничего не меняет"
    assert attempts[0]["only_when_text_field_detected"] is False
    assert "overlay.result" not in window.events, "вставка прошла, а показан фолбэк"
    assert any("текст вставлен" in event for event in window.events), window.events


def test_decision_is_made_at_delivery_not_at_recording_start() -> None:
    """В начале записи поля не было, к концу — есть: вставка должна произойти."""
    window = _delivery_window(field_at_start=False, safe_paste=True)
    attempts, _copied = _run_delivery(window, field_at_delivery=True)

    assert attempts, "решение всё ещё принимается по снимку из начала записи"
    assert any("текст вставлен" in event for event in window.events), window.events


def test_auto_paste_off_still_disables_everything() -> None:
    window = _delivery_window(field_at_start=True, auto_paste=False)
    attempts, copied = _run_delivery(window, field_at_delivery=True)

    assert attempts == [], "вставка выполнена при выключенной автовставке"
    assert copied, "текст должен остаться в буфере"


def test_own_window_is_still_never_pasted_into() -> None:
    """Своё поле диктовки приложение заполняет напрямую — Ctrl+V дублировал бы текст."""
    window = _delivery_window(field_at_start=True, own_window=True)
    attempts, _copied = _run_delivery(window, field_at_delivery=True)

    assert attempts == [], "вставка в собственное окно приложения"


def test_lock_stop_still_suppresses_paste() -> None:
    """US-069 не должна сломаться: после блокировки экрана вставки нет."""
    window = _delivery_window(field_at_start=True, stopped_by_lock=True)
    attempts, copied = _run_delivery(window, field_at_delivery=True)

    assert attempts == [], "текст вставлен после остановки по блокировке экрана"
    assert copied, "текст потерян"


def test_gate_no_longer_reads_the_start_snapshot() -> None:
    body = _method_source("_deliver_dictation_result")
    assert "live_target_is_text_field" not in body, (
        "решение о вставке снова опирается на снимок, сделанный в начале записи"
    )
    assert "only_when_text_field_detected=self.cfg.paste_only_when_text_field_detected" in body, (
        "настройка «Безопасная вставка» не доходит до вставки"
    )


def test_blocked_paste_is_logged() -> None:
    """Молчаливый отказ не позволял диагностировать «не вставляет в программу X»."""
    src = (REPO_ROOT / "voice_input_app" / "insert.py").read_text(encoding="utf-8")
    marker = "if only_when_text_field_detected and detection is not True:"
    start = src.index(marker)
    block = src[start: start + 700]
    assert "log.info" in block, "отказ из-за ненайденного поля ввода не логируется"


class _FakeControl:
    def __init__(self, control_type: str, class_name: str, automation_id: str = "") -> None:
        self.ControlTypeName = control_type
        self.ClassName = class_name
        self.AutomationId = automation_id


def _detect(control: _FakeControl | None, *, caret: bool | None) -> bool | None:
    """Прогнать распознавание поля с подменёнными UI Automation и кареткой."""
    fake_module = types.ModuleType("uiautomation")
    fake_module.GetFocusedControl = lambda: control  # type: ignore[attr-defined]
    saved_module = sys.modules.get("uiautomation")
    saved_caret = insert_module._win32_caret_is_visible
    sys.modules["uiautomation"] = fake_module
    insert_module._win32_caret_is_visible = lambda: caret
    try:
        return insert_module.focused_control_accepts_text()
    finally:
        if saved_module is None:
            sys.modules.pop("uiautomation", None)
        else:
            sys.modules["uiautomation"] = saved_module
        insert_module._win32_caret_is_visible = saved_caret


# Данные ниже сняты на устройстве через scripts/diagnose_text_field.py.
def test_self_drawn_editor_is_recognised() -> None:
    """Claude Desktop: каретки нет, UIA отдаёт «группу» с классами редактора."""
    control = _FakeControl("GroupControl", "tiptap ProseMirror ProseMirror-focused")
    assert _detect(control, caret=False) is True, "поле ввода Claude Desktop снова не распознаётся"


def test_web_page_without_input_stays_blocked() -> None:
    """Chrome со страницей в фокусе: поля нет — вставлять туда нельзя."""
    control = _FakeControl("PaneControl", "Chrome_WidgetWin_1")
    assert _detect(control, caret=False) is not True, (
        "правило слишком широкое: текст улетит в произвольную веб-страницу"
    )


def test_button_inside_editor_is_not_a_text_field() -> None:
    control = _FakeControl("ButtonControl", "prosemirror-menu-item")
    assert _detect(control, caret=False) is not True, "кнопка редактора принята за поле ввода"


def test_classic_windows_fields_still_recognised() -> None:
    """Блокнот и VK Teams распознавались и раньше — проверяем, что не сломались."""
    notepad = _FakeControl("DocumentControl", "RichEditD2DPT")
    assert _detect(notepad, caret=True) is True
    vk_teams = _FakeControl("TextControl", "Ui::HistoryTextEdit", "Utils::IMApp.AS_MainWindow")
    assert _detect(vk_teams, caret=True) is True


def _run() -> None:
    tests = [
        test_self_drawn_editor_is_recognised,
        test_web_page_without_input_stays_blocked,
        test_button_inside_editor_is_not_a_text_field,
        test_classic_windows_fields_still_recognised,
        test_safe_paste_on_still_blocks_when_no_text_field,
        test_safe_paste_off_pastes_where_windows_sees_no_field,
        test_decision_is_made_at_delivery_not_at_recording_start,
        test_auto_paste_off_still_disables_everything,
        test_own_window_is_still_never_pasted_into,
        test_lock_stop_still_suppresses_paste,
        test_gate_no_longer_reads_the_start_snapshot,
        test_blocked_paste_is_logged,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("US-070 regression: ALL PASS")


if __name__ == "__main__":
    _run()
