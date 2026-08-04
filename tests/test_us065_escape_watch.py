# -*- coding: utf-8 -*-
r"""Регресс-тесты US-065 (TASK-338): отмена по Escape без захвата клавиши.

Escape НЕ регистрируется системной горячей клавишей — иначе Windows отдала бы
её нам одним, и в активной программе перестали бы закрываться диалоги,
подсказки автодополнения и полноэкранный режим. Вместо этого на время операции
включается точечное наблюдение за состоянием ОДНОЙ клавиши.

Проверяется:
1. Наблюдение опрашивает ровно один код (VK_ESCAPE) и ничего сверх него.
2. Escape отменяет и запись, и распознавание — вызывается настоящий
   MainWindow.cancel_current_action с mock-self.
3. Короткое нажатие, целиком уместившееся между тиками опроса, не теряется.
4. Наблюдение гаснет во всех точках выхода и после остановки не срабатывает.

Тест headless: Qt-виджеты не создаются, клавиатура не читается — состояние
клавиш подменено. Запуск (в venv приложения):
    .venv\Scripts\python.exe tests\test_us065_escape_watch.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LOCALAPPDATA"] = tempfile.gettempdir()

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from voice_input_app.hotkeys import VK_ESCAPE  # noqa: E402
from voice_input_app.key_watch import (  # noqa: E402
    KEY_DOWN_MASK,
    KEY_PRESSED_SINCE_MASK,
    KeyStateWatcher,
)

_APP = QCoreApplication.instance() or QCoreApplication(sys.argv)
UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"

# Точки выхода, в которых наблюдение обязано гаснуть (US-065: завершение,
# отмена, ошибка). Пропуск любой из них оставил бы работающий таймер.
EXIT_POINTS = [
    "stop_recording",
    "cancel_current_action",
    "on_transcription_done",
    "on_transcription_failed",
    "on_worker_failed",
    "on_dictation_no_speech",
    "copy_overlay_result",
    "really_quit",
]


class FakeKeyboard:
    """Состояние клавиш вместо Win32: down — удержание, tap — нажатие между тиками."""

    def __init__(self) -> None:
        self.down: set[int] = set()
        self.tapped: set[int] = set()
        self.reads: list[int] = []

    def read(self, virtual_key: int) -> int:
        self.reads.append(virtual_key)
        state = 0
        if virtual_key in self.down:
            state |= KEY_DOWN_MASK
        if virtual_key in self.tapped:
            state |= KEY_PRESSED_SINCE_MASK
            self.tapped.discard(virtual_key)  # бит сбрасывается чтением, как в Win32
        return state


def _method_source(name: str) -> str:
    src = UI_PATH.read_text(encoding="utf-8")
    start = src.index(f"    def {name}(self")
    end = src.find("\n    def ", start + 10)
    return src[start: end if end != -1 else len(src)]


def _make_window(keyboard: FakeKeyboard, *, recording: bool, transcribing: bool):
    """Mock-окно с НАСТОЯЩИМИ методами MainWindow, завязанными на Escape."""
    from voice_input_app.ui import MainWindow

    events: list[str] = []
    window = types.SimpleNamespace(
        events=events,
        escape_watcher=KeyStateWatcher(key_state_fn=keyboard.read),
        cfg=types.SimpleNamespace(overlay_enabled=True),
        recorder=types.SimpleNamespace(
            is_recording=recording,
            cancel=lambda: events.append("recorder.cancel"),
        ),
        transcribe_worker=types.SimpleNamespace(isRunning=lambda: transcribing) if transcribing else None,
        live_worker=None,
        pending_final=None,
        cancel_requested=False,
        result_preview_active=False,
        result_preview_text="",
        toggle_btn=types.SimpleNamespace(setText=lambda _t: None, setEnabled=lambda _v: None),
        record_badge=types.SimpleNamespace(setText=lambda _t: None, setStyleSheet=lambda _s: None),
        status_label=types.SimpleNamespace(setText=lambda t: events.append(f"status:{t}")),
        overlay=types.SimpleNamespace(
            show_cancelled=lambda seconds=0: events.append("overlay.cancelled"),
            show_idle=lambda: None,
        ),
        _stop_dictation_progress=lambda: None,
        _stop_overlay_audio_level_updates=lambda: events.append("audio_level.stopped"),
        _cleanup_wav=lambda _p: None,
    )
    for name in ("start_escape_watch", "stop_escape_watch", "cancel_current_action"):
        setattr(window, name, types.MethodType(getattr(MainWindow, name), window))
    # Сигнал доставляется синхронно — в приложении это QueuedConnection.
    window.cancel_signal = types.SimpleNamespace(
        triggered=types.SimpleNamespace(emit=window.cancel_current_action)
    )
    return window


def test_watcher_is_idle_until_operation_starts() -> None:
    keyboard = FakeKeyboard()
    watcher = KeyStateWatcher(key_state_fn=keyboard.read)
    assert watcher.is_active is False
    assert watcher.watched_codes == ()
    assert keyboard.reads == [], "в покое клавиатура не читается вовсе"


def test_escape_watch_reads_only_escape() -> None:
    keyboard = FakeKeyboard()
    window = _make_window(keyboard, recording=True, transcribing=False)
    window.start_escape_watch()

    assert window.escape_watcher.is_active
    assert window.escape_watcher.watched_codes == (VK_ESCAPE,)
    window.escape_watcher._poll()
    assert set(keyboard.reads) == {VK_ESCAPE}, f"прочитаны посторонние коды: {set(keyboard.reads)}"


def test_escape_cancels_recording() -> None:
    keyboard = FakeKeyboard()
    window = _make_window(keyboard, recording=True, transcribing=False)
    window.start_escape_watch()

    keyboard.down.add(VK_ESCAPE)
    window.escape_watcher._poll()

    assert "recorder.cancel" in window.events, "запись не остановлена"
    assert window.cancel_requested is True
    assert "overlay.cancelled" in window.events
    assert window.escape_watcher.is_active is False, "наблюдение осталось включённым после отмены"


def test_escape_cancels_transcription() -> None:
    keyboard = FakeKeyboard()
    window = _make_window(keyboard, recording=False, transcribing=True)
    window.start_escape_watch()

    keyboard.down.add(VK_ESCAPE)
    window.escape_watcher._poll()

    assert window.cancel_requested is True, "распознавание не отменено"
    assert "recorder.cancel" not in window.events, "запись не идёт — трогать рекордер незачем"
    assert any("Отменено" in event for event in window.events), window.events
    assert window.escape_watcher.is_active is False


def test_short_escape_tap_between_polls_is_not_lost() -> None:
    """Нажатие короче интервала опроса ловится младшим битом GetAsyncKeyState."""
    keyboard = FakeKeyboard()
    window = _make_window(keyboard, recording=True, transcribing=False)
    window.start_escape_watch()

    keyboard.tapped.add(VK_ESCAPE)  # нажали и отпустили между тиками
    window.escape_watcher._poll()

    assert window.cancel_requested is True, "короткое нажатие Escape потеряно"


def test_escape_held_at_start_does_not_cancel_immediately() -> None:
    """Клавиша, зажатая ДО начала операции, не считается новым нажатием."""
    keyboard = FakeKeyboard()
    keyboard.down.add(VK_ESCAPE)
    window = _make_window(keyboard, recording=True, transcribing=False)
    window.start_escape_watch()

    window.escape_watcher._poll()
    assert window.cancel_requested is False

    keyboard.down.discard(VK_ESCAPE)
    window.escape_watcher._poll()  # отпускание — не повод отменять
    assert window.cancel_requested is False

    keyboard.down.add(VK_ESCAPE)
    window.escape_watcher._poll()
    assert window.cancel_requested is True


def test_stop_ends_watching_and_later_presses_are_ignored() -> None:
    keyboard = FakeKeyboard()
    window = _make_window(keyboard, recording=True, transcribing=False)
    window.start_escape_watch()
    window.stop_escape_watch()

    assert window.escape_watcher.is_active is False
    assert window.escape_watcher.watched_codes == ()

    keyboard.reads.clear()
    keyboard.down.add(VK_ESCAPE)
    window.escape_watcher._poll()

    assert window.cancel_requested is False, "отмена сработала при выключенном наблюдении"
    assert keyboard.reads == [], "клавиатура читается после остановки наблюдения"


def test_repeated_start_does_not_duplicate_watching() -> None:
    keyboard = FakeKeyboard()
    window = _make_window(keyboard, recording=True, transcribing=False)
    window.start_escape_watch()
    window.start_escape_watch()
    assert window.escape_watcher.watched_codes == (VK_ESCAPE,)


def test_every_exit_point_stops_watching() -> None:
    for name in EXIT_POINTS:
        body = _method_source(name)
        assert "stop_escape_watch()" in body, f"{name} не гасит наблюдение за Escape"


def test_recording_start_arms_the_watch() -> None:
    body = _method_source("start_recording")
    assert "self.start_escape_watch()" in body, "запись стартует без отмены по Escape"


def test_escape_is_not_registered_and_not_swallowed() -> None:
    src = UI_PATH.read_text(encoding="utf-8")
    assert "keyboard.add_hotkey" not in src, "в UI осталась регистрация клавиши через хук"
    assert re.search(r"RegisterHotKey\W", src) is None, "UI не должен регистрировать клавиши напрямую"
    watch_call = _method_source("start_escape_watch")
    assert "VK_ESCAPE" in watch_call
    assert "suppress" not in watch_call, "клавиша не должна поглощаться"


def _run() -> None:
    tests = [
        test_watcher_is_idle_until_operation_starts,
        test_escape_watch_reads_only_escape,
        test_escape_cancels_recording,
        test_escape_cancels_transcription,
        test_short_escape_tap_between_polls_is_not_lost,
        test_escape_held_at_start_does_not_cancel_immediately,
        test_stop_ends_watching_and_later_presses_are_ignored,
        test_repeated_start_does_not_duplicate_watching,
        test_every_exit_point_stops_watching,
        test_recording_start_arms_the_watch,
        test_escape_is_not_registered_and_not_swallowed,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("US-065 regression: ALL PASS")


if __name__ == "__main__":
    _run()
