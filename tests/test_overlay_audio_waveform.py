# -*- coding: utf-8 -*-
r"""Regression tests for the real-time recording waveform.

The suite is headless and replaces PortAudio's InputStream, so it never opens
the user's microphone.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LOCALAPPDATA"] = tempfile.gettempdir()

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from voice_input_app import audio_recorder
from voice_input_app.audio_recorder import (
    AudioOpenCandidate,
    AudioRecorder,
    _normalize_audio_level,
)
from voice_input_app.overlay import AudioLevelWaveform, RecordingOverlay

_APP = QApplication.instance() or QApplication(sys.argv)
UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"


def _red_pixel_count(widget: AudioLevelWaveform) -> int:
    widget.show()
    _APP.processEvents()
    image = widget.grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if color.red() > 180 and color.green() < 120 and color.blue() < 120 and color.alpha() > 0:
                count += 1
    return count


def test_audio_level_normalization() -> None:
    assert _normalize_audio_level(np.zeros(32, dtype=np.float32)) == 0.0
    midpoint_amplitude = 10.0 ** (-31.5 / 20.0)
    midpoint = _normalize_audio_level(np.full(32, midpoint_amplitude, dtype=np.float32))
    assert 0.48 <= midpoint <= 0.52
    assert _normalize_audio_level(np.ones(32, dtype=np.float32)) == 1.0
    with np.errstate(invalid="ignore"):
        assert _normalize_audio_level(np.array([np.nan], dtype=np.float32)) == 0.0


def test_recorder_captures_and_resets_latest_level() -> None:
    original_input_stream = audio_recorder.sd.InputStream

    class FakeInputStream:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            self.callback = kwargs["callback"]

        def start(self) -> None:
            samples = np.full((64, 1), 0.25, dtype=np.float32)
            self.callback(samples, len(samples), None, None)

        def stop(self) -> None:
            pass

        def close(self) -> None:
            pass

    recorder = AudioRecorder()
    recorder._candidate_devices = lambda: [AudioOpenCandidate(None, "Test input", "Test", (16000,), False)]
    audio_recorder.sd.InputStream = FakeInputStream
    try:
        recorder.start()
        assert recorder.input_level > 0.8
        recorder.cancel()
        assert recorder.input_level == 0.0
    finally:
        audio_recorder.sd.InputStream = original_input_stream


def test_waveform_height_follows_audio_level() -> None:
    waveform = AudioLevelWaveform()
    try:
        quiet_pixels = _red_pixel_count(waveform)
        for _ in range(waveform.BAR_COUNT):
            waveform.set_level(1.0)
        _APP.processEvents()
        loud_pixels = _red_pixel_count(waveform)
        assert loud_pixels > quiet_pixels * 2
        assert waveform.size().width() == 20
        assert waveform.size().height() == 14
    finally:
        waveform.close()


def test_overlay_switches_between_dot_and_waveform() -> None:
    overlay = RecordingOverlay()
    try:
        overlay.show_idle()
        assert not overlay.dot_label.isHidden()
        assert overlay.level_waveform.isHidden()

        overlay.show_recording()
        recording_size = overlay.size()
        assert overlay.dot_label.isHidden()
        assert not overlay.level_waveform.isHidden()

        for level in (0.2, 0.8, 0.4, 1.0):
            overlay.set_audio_level(level)
        pixels_before_refresh = _red_pixel_count(overlay.level_waveform)
        overlay.show_recording()
        pixels_after_refresh = _red_pixel_count(overlay.level_waveform)
        assert pixels_after_refresh == pixels_before_refresh
        assert overlay.size() == recording_size
        assert overlay.height() == overlay.COMPACT_HEIGHT
        assert overlay.width() <= overlay.COMPACT_MAX_WIDTH

        overlay.show_processing()
        assert not overlay.dot_label.isHidden()
        assert overlay.level_waveform.isHidden()
    finally:
        overlay.close()


def test_main_window_wires_audio_level_timer() -> None:
    source = UI_PATH.read_text(encoding="utf-8")
    assert "self._audio_level_timer.setInterval(50)" in source
    assert "self._audio_level_timer.timeout.connect(self._update_overlay_audio_level)" in source
    assert "self.overlay.set_audio_level(self.recorder.input_level)" in source
    assert "self._start_overlay_audio_level_updates()" in source
    assert "self._stop_overlay_audio_level_updates()" in source


def _run() -> None:
    tests = [
        test_audio_level_normalization,
        test_recorder_captures_and_resets_latest_level,
        test_waveform_height_follows_audio_level,
        test_overlay_switches_between_dot_and_waveform,
        test_main_window_wires_audio_level_timer,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("Overlay audio waveform regression: ALL PASS")


if __name__ == "__main__":
    _run()
