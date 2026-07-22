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


def test_double_click_requests_toggle_in_ready_and_recording() -> None:
    overlay = RecordingOverlay()
    hits: list[bool] = []
    overlay.toggle_recording_requested.connect(lambda: hits.append(True))
    try:
        overlay.show_idle()
        _double_click(overlay)
        assert len(hits) == 1, "двойной клик в Ready не запросил старт записи"

        overlay.show_recording()
        _double_click(overlay)
        assert len(hits) == 2, "двойной клик во время записи не запросил остановку"
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


def test_double_click_is_ignored_in_noninteractive_states() -> None:
    overlay = RecordingOverlay()
    hits: list[bool] = []
    overlay.toggle_recording_requested.connect(lambda: hits.append(True))
    try:
        states = [
            lambda: overlay.show_processing(),
            lambda: overlay.show_error("Ошибка", seconds=60),
            lambda: overlay.show_cancelled(seconds=60),
            lambda: overlay.show_result_text("Готовый результат"),
            lambda: overlay.show_model_picker([("cloud:test:model", "Cloud model")]),
        ]
        for set_state in states:
            set_state()
            _double_click(overlay)
        assert not hits, "двойной клик сработал во время обработки/результата/пикера"
    finally:
        overlay.close()


def test_main_window_uses_common_toggle_handler() -> None:
    src = UI_PATH.read_text(encoding="utf-8")
    assert "self.overlay.toggle_recording_requested.connect(self.toggle_recording)" in src
    assert "self.overlay.picker_requested.connect(self.on_overlay_picker_requested)" in src
    assert "def on_overlay_picker_requested(self)" in src


def _run() -> None:
    tests = [
        test_double_click_requests_toggle_in_ready_and_recording,
        test_single_click_does_not_request_toggle,
        test_right_click_requests_picker_only_in_ready,
        test_overlay_explains_both_mouse_actions,
        test_double_click_is_ignored_in_noninteractive_states,
        test_main_window_uses_common_toggle_handler,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("Overlay toggle recording regression: ALL PASS")


if __name__ == "__main__":
    _run()
