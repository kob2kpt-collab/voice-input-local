# -*- coding: utf-8 -*-
r"""Регресс-тест: клики по overlay переключают запись и выбирают модель.

Проверяется сам интерактивный контракт плашки и подключение сигнала к общему
MainWindow.toggle_recording. Тест не обращается к микрофону и работает headless.

Запуск (в venv приложения):
    .venv\Scripts\python.exe tests\test_overlay_toggle_recording.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from voice_input_app.overlay import RecordingOverlay

_APP = QApplication.instance() or QApplication(sys.argv)


def _double_click(overlay: RecordingOverlay) -> None:
    overlay.show()
    QTest.mouseDClick(overlay, Qt.LeftButton)
    _APP.processEvents()


def _window_does_not_accept_focus_flag():
    try:
        return Qt.WindowType.WindowDoesNotAcceptFocus
    except AttributeError:
        return Qt.WindowDoesNotAcceptFocus


def _show_without_activating_attribute():
    try:
        return Qt.WidgetAttribute.WA_ShowWithoutActivating
    except AttributeError:
        return Qt.WA_ShowWithoutActivating


def test_double_click_requests_toggle_outside_picker() -> None:
    overlay = RecordingOverlay()
    hits: list[bool] = []
    overlay.toggle_recording_requested.connect(lambda: hits.append(True))
    try:
        states = [
            lambda: overlay.show_idle(),
            lambda: overlay.show_recording(),
            lambda: overlay.show_processing(),
            lambda: overlay.show_error("Ошибка", seconds=60),
            lambda: overlay.show_cancelled(seconds=60),
            lambda: overlay.show_result_text("Готовый результат"),
        ]
        for expected_hits, set_state in enumerate(states, start=1):
            set_state()
            _double_click(overlay)
            assert len(hits) == expected_hits, (
                "визуальное состояние overlay не должно блокировать общий toggle_recording"
            )

        overlay.show_model_picker([("cloud:test:model", "Cloud model")])
        _double_click(overlay)
        assert len(hits) == len(states), "двойной клик не должен управлять записью внутри пикера"
    finally:
        overlay.close()


def test_single_click_does_not_request_toggle() -> None:
    overlay = RecordingOverlay()
    hits: list[bool] = []
    overlay.toggle_recording_requested.connect(lambda: hits.append(True))
    try:
        overlay.show_idle()
        overlay.show()
        QTest.mouseClick(overlay, Qt.LeftButton)
        _APP.processEvents()
        assert not hits, "одиночный клик не должен переключать запись или мешать drag"
    finally:
        overlay.close()


def test_right_click_requests_picker_only_in_ready() -> None:
    overlay = RecordingOverlay()
    hits: list[bool] = []
    overlay.picker_requested.connect(lambda: hits.append(True))
    try:
        overlay.show_idle()
        overlay.show()
        QTest.mouseClick(overlay, Qt.RightButton)
        _APP.processEvents()
        assert len(hits) == 1, "правый клик в Ready не запросил выбор модели"

        blocked_states = [
            lambda: overlay.show_recording(),
            lambda: overlay.show_processing(),
            lambda: overlay.show_result_text("Готовый результат"),
            lambda: overlay.show_model_picker([("cloud:test:model", "Cloud model")]),
        ]
        for set_state in blocked_states:
            set_state()
            QTest.mouseClick(overlay, Qt.RightButton)
            _APP.processEvents()
        assert len(hits) == 1, "правый клик открыл выбор модели не из Ready"
    finally:
        overlay.close()


def test_overlay_explains_both_mouse_actions() -> None:
    overlay = RecordingOverlay()
    try:
        tooltip = overlay.status_label.toolTip()
        assert "Двойной левый клик" in tooltip
        assert "Правый клик" in tooltip
    finally:
        overlay.close()


def test_overlay_accepts_focus_only_in_picker() -> None:
    overlay = RecordingOverlay()
    no_focus = _window_does_not_accept_focus_flag()
    show_without_activating = _show_without_activating_attribute()
    try:
        overlay.move(120, 80)
        overlay.show_idle()
        _APP.processEvents()
        original_position = overlay.pos()

        assert overlay.windowFlags() & no_focus, "обычная плашка не должна принимать фокус"
        assert overlay.testAttribute(show_without_activating)
        assert overlay.isVisible()

        overlay.show_model_picker([("cloud:test:model", "Cloud model")])
        _APP.processEvents()
        assert not (overlay.windowFlags() & no_focus), "пикеру нужен фокус для QComboBox"
        assert not overlay.testAttribute(show_without_activating)
        assert overlay.isVisible()
        assert overlay.pos() == original_position, "пикер изменил сохранённую позицию плашки"

        overlay.show_idle()
        _APP.processEvents()
        assert overlay.windowFlags() & no_focus, "после пикера защита фокуса не восстановлена"
        assert overlay.testAttribute(show_without_activating)
        assert overlay.isVisible()
        assert overlay.pos() == original_position, "выход из пикера изменил позицию плашки"
    finally:
        overlay.close()


def test_main_window_uses_common_toggle_handler() -> None:
    src = UI_PATH.read_text(encoding="utf-8")
    assert "self.overlay.toggle_recording_requested.connect(self.toggle_recording)" in src
    assert "self.overlay.picker_requested.connect(self.on_overlay_picker_requested)" in src
    assert "def on_overlay_picker_requested(self)" in src


def _run() -> None:
    tests = [
        test_double_click_requests_toggle_outside_picker,
        test_single_click_does_not_request_toggle,
        test_right_click_requests_picker_only_in_ready,
        test_overlay_explains_both_mouse_actions,
        test_overlay_accepts_focus_only_in_picker,
        test_main_window_uses_common_toggle_handler,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("Overlay toggle recording regression: ALL PASS")


if __name__ == "__main__":
    _run()
