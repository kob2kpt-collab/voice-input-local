# -*- coding: utf-8 -*-
r"""Регресс-тесты US-069: блокировка экрана останавливает диктовку.

Диктовка, начатая не удержанием клавиши (двойной клик по плашке, кнопка окна,
трей, горячая клавиша в режиме «Переключатель»), продолжала писать в микрофон
после блокировки экрана. Теперь при блокировке запись останавливается обычным
путём — с расшифровкой, историей и буфером обмена, — но БЕЗ автоматической
вставки в активное окно.

Отдельное внимание уделено тому, чтобы не сломать уже работающую вставку:
`test_normal_delivery_still_pastes` и `test_normal_delivery_respects_existing_rules`
проверяют, что в обычной диктовке решение о вставке принимается ровно по
прежним правилам.

Тест headless: Win32 и Qt-виджеты подменены, микрофон и буфер обмена не
задействованы. Запуск (в venv приложения):
    .venv\Scripts\python.exe tests\test_us069_session_lock.py
"""
from __future__ import annotations

import ctypes
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

from voice_input_app.session_lock import (  # noqa: E402
    WTS_SESSIONSTATE_LOCK,
    WTS_SESSIONSTATE_UNLOCK,
    SessionLockWatcher,
    is_session_locked,
)

_APP = QCoreApplication.instance() or QCoreApplication(sys.argv)
UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"

# Точки, в которых наблюдение обязано гаснуть вместе с записью.
EXIT_POINTS = ["stop_recording", "cancel_current_action", "really_quit"]


def _method_source(name: str) -> str:
    src = UI_PATH.read_text(encoding="utf-8")
    start = src.index(f"    def {name}(self")
    end = src.find("\n    def ", start + 10)
    return src[start: end if end != -1 else len(src)]


class FakeSession:
    """Состояние сессии вместо Win32."""

    def __init__(self, locked: bool = False) -> None:
        self.locked = locked
        self.reads = 0
        self.raise_next = False

    def __call__(self) -> bool:
        self.reads += 1
        if self.raise_next:
            self.raise_next = False
            raise OSError("WTSQuerySessionInformation failed")
        return self.locked


def test_session_state_struct_matches_windows_layout() -> None:
    """Смещения полей WTSINFOEX. Ошибка здесь читала бы мусор вместо блокировки."""
    if sys.platform != "win32":
        return
    from voice_input_app.session_lock import _WTSINFOEX_LEVEL1_W, _WTSINFOEX_W

    assert ctypes.sizeof(_WTSINFOEX_W) == 232, ctypes.sizeof(_WTSINFOEX_W)
    offset = _WTSINFOEX_W.Data.offset + _WTSINFOEX_LEVEL1_W.SessionFlags.offset
    assert offset == 16, offset
    assert WTS_SESSIONSTATE_LOCK == 0 and WTS_SESSIONSTATE_UNLOCK == 1


def test_is_session_locked_never_raises() -> None:
    """Сбой обращения к системе не должен обрывать диктовку."""
    assert isinstance(is_session_locked(), bool)


def test_watcher_is_idle_until_recording_starts() -> None:
    session = FakeSession()
    watcher = SessionLockWatcher(locked_fn=session)
    assert watcher.is_active is False
    assert session.reads == 0, "состояние сессии читается в покое"


def test_lock_fires_callback_once() -> None:
    session = FakeSession(locked=False)
    watcher = SessionLockWatcher(locked_fn=session)
    calls: list[str] = []
    watcher.start(lambda: calls.append("locked"))

    watcher._poll()
    assert calls == [], "сработало без блокировки"

    session.locked = True
    watcher._poll()
    assert calls == ["locked"]

    watcher._poll()  # экран всё ещё заблокирован
    assert calls == ["locked"], "повторное срабатывание на той же блокировке"


def test_already_locked_at_start_is_not_an_event() -> None:
    session = FakeSession(locked=True)
    watcher = SessionLockWatcher(locked_fn=session)
    calls: list[str] = []
    watcher.start(lambda: calls.append("locked"))

    watcher._poll()
    assert calls == [], "уже заблокированный экран принят за новое событие"


def test_unlock_then_lock_fires_again() -> None:
    session = FakeSession(locked=False)
    watcher = SessionLockWatcher(locked_fn=session)
    calls: list[str] = []
    watcher.start(lambda: calls.append("locked"))

    session.locked = True
    watcher._poll()
    session.locked = False
    watcher._poll()
    session.locked = True
    watcher._poll()

    assert calls == ["locked", "locked"]


def test_stop_ends_watching() -> None:
    session = FakeSession(locked=False)
    watcher = SessionLockWatcher(locked_fn=session)
    calls: list[str] = []
    watcher.start(lambda: calls.append("locked"))
    watcher.stop()

    assert watcher.is_active is False
    session.locked = True
    reads_before = session.reads
    watcher._poll()

    assert calls == [], "колбэк сработал после остановки наблюдения"
    assert session.reads == reads_before, "состояние сессии читается после остановки"


def test_system_call_failure_is_treated_as_unlocked() -> None:
    session = FakeSession(locked=False)
    watcher = SessionLockWatcher(locked_fn=session)
    calls: list[str] = []
    watcher.start(lambda: calls.append("locked"))

    session.raise_next = True
    watcher._poll()

    assert calls == [], "сбой системного вызова принят за блокировку"


def _make_window(*, recording: bool):
    """Mock-окно с настоящими методами MainWindow, которые трогает US-069."""
    from voice_input_app.ui import MainWindow

    events: list[str] = []
    session = FakeSession()
    window = types.SimpleNamespace(
        events=events,
        session=session,
        session_lock_watcher=SessionLockWatcher(locked_fn=session),
        recorder=types.SimpleNamespace(is_recording=recording),
        status_label=types.SimpleNamespace(setText=lambda t: events.append(f"status:{t}")),
        stop_recording=lambda: events.append("stop_recording"),
        _dictation_stopped_by_lock=False,
    )
    for name in ("_start_session_lock_watch", "_stop_session_lock_watch", "_on_session_locked"):
        setattr(window, name, types.MethodType(getattr(MainWindow, name), window))
    return window


def test_lock_during_recording_stops_dictation() -> None:
    window = _make_window(recording=True)
    window._start_session_lock_watch()
    assert window.session_lock_watcher.is_active

    window.session.locked = True
    window.session_lock_watcher._poll()

    assert "stop_recording" in window.events, "запись не остановлена при блокировке"
    assert window._dictation_stopped_by_lock is True, "не помечено, что остановка вызвана блокировкой"
    assert any("Экран заблокирован" in e for e in window.events), window.events


def test_lock_without_recording_is_noop() -> None:
    """Push-to-Talk уже остановил запись отпусканием клавиш — второй остановки нет."""
    window = _make_window(recording=False)
    window._start_session_lock_watch()

    window.session.locked = True
    window.session_lock_watcher._poll()

    assert "stop_recording" not in window.events, "вторая остановка записи"
    assert window._dictation_stopped_by_lock is False
    assert window.session_lock_watcher.is_active is False, "наблюдение осталось включённым"


def _delivery_window(*, stopped_by_lock: bool, auto_paste: bool = True, target_is_text_field: bool = True, own_window: bool = False):
    """Mock-окно для проверки доставки текста диктовки."""
    from voice_input_app.ui import MainWindow

    events: list[str] = []
    window = types.SimpleNamespace(
        events=events,
        cfg=types.SimpleNamespace(
            auto_paste=auto_paste,
            paste_only_when_text_field_detected=True,
            overlay_enabled=True,
            selected_model="whisper:small",
        ),
        live_target_is_text_field=target_is_text_field,
        recording_started_in_own_window=own_window,
        _dictation_stopped_by_lock=stopped_by_lock,
        last_text=types.SimpleNamespace(setPlainText=lambda _t: None),
        history=types.SimpleNamespace(add=lambda *a: events.append("history.add")),
        refresh_history=lambda: None,
        update_recording_badge=lambda: None,
        toggle_btn=types.SimpleNamespace(setEnabled=lambda _v: None, setText=lambda _t: None),
        result_preview_active=False,
        result_preview_text="",
        stop_escape_watch=lambda: None,
        start_escape_watch=lambda: events.append("escape_watch.start"),
        overlay=types.SimpleNamespace(
            show_result_text=lambda _t: events.append("overlay.result"),
            show_idle=lambda: None,
        ),
        status_label=types.SimpleNamespace(setText=lambda t: events.append(f"status:{t}")),
        _cleanup_wav=lambda _p: None,
    )
    window._deliver_dictation_result = types.MethodType(MainWindow._deliver_dictation_result, window)
    return window


def _run_delivery(window, text: str = "сказанный текст"):
    """Выполнить доставку, подменив вставку и буфер обмена."""
    from voice_input_app import ui as ui_module

    pasted: list[str] = []
    copied: list[str] = []
    original_paste = ui_module.copy_and_maybe_paste
    original_clipboard = ui_module.pyperclip
    ui_module.copy_and_maybe_paste = lambda t, **kw: (pasted.append(t), True)[1]
    ui_module.pyperclip = types.SimpleNamespace(copy=lambda t: copied.append(t))
    try:
        window._deliver_dictation_result(text, 3.0, Path("nonexistent.wav"))
    finally:
        ui_module.copy_and_maybe_paste = original_paste
        ui_module.pyperclip = original_clipboard
    return pasted, copied


def test_normal_delivery_still_pastes() -> None:
    """ГЛАВНАЯ защита штатного пути: обычная диктовка вставляется как раньше."""
    window = _delivery_window(stopped_by_lock=False)
    pasted, copied = _run_delivery(window)

    assert pasted == ["сказанный текст"], "штатная автовставка сломана"
    assert copied == [], "при удачной вставке текст в буфер не копируется (прежнее поведение)"
    assert "history.add" in window.events


def test_normal_delivery_respects_existing_rules() -> None:
    """Прежние условия отказа от вставки продолжают действовать без изменений.

    Условие «Windows не видела поля ввода в начале записи» здесь намеренно
    отсутствует: US-070 перенесла эту проверку на момент доставки текста, где
    ею управляет настройка «Безопасная вставка». Поведение закреплено в
    tests/test_us070_paste_gate.py.
    """
    for kwargs in (
        {"auto_paste": False},
        {"own_window": True},
    ):
        window = _delivery_window(stopped_by_lock=False, **kwargs)
        pasted, copied = _run_delivery(window)
        assert pasted == [], f"вставка выполнена вопреки условиям {kwargs}"
        assert copied == ["сказанный текст"], f"текст не попал в буфер при {kwargs}"


def test_lock_delivery_does_not_paste_but_keeps_everything_else() -> None:
    window = _delivery_window(stopped_by_lock=True)
    pasted, copied = _run_delivery(window)

    assert pasted == [], "текст вставлен в активное окно после блокировки экрана"
    assert copied == ["сказанный текст"], "текст не попал в буфер обмена"
    assert "history.add" in window.events, "текст не сохранён в историю"
    assert "overlay.result" in window.events, "результат не показан в плашке"
    assert window.result_preview_active is True
    assert any("Экран был заблокирован" in e or "экран был заблокирован" in e for e in window.events), window.events
    assert window._dictation_stopped_by_lock is False, "признак должен быть одноразовым"


def test_lock_flag_is_reset_for_the_next_dictation() -> None:
    window = _delivery_window(stopped_by_lock=True)
    _run_delivery(window)
    window.live_target_is_text_field = True
    pasted, _copied = _run_delivery(window)

    assert pasted == ["сказанный текст"], "следующая диктовка осталась без вставки"


def test_watch_starts_only_with_recording() -> None:
    src = UI_PATH.read_text(encoding="utf-8")
    assert "def _start_session_lock_watch(self)" in src, "нет метода включения наблюдения"
    starts = re.findall(r"self\._start_session_lock_watch\(\)", src)
    assert len(starts) == 1, f"наблюдение включается из {len(starts)} мест, ожидалось одно — старт записи"
    assert "self._start_session_lock_watch()" in _method_source("start_recording")


def test_every_exit_point_stops_watching() -> None:
    for name in EXIT_POINTS:
        assert "_stop_session_lock_watch()" in _method_source(name), f"{name} не гасит наблюдение"
    assert "_stop_session_lock_watch()" in _method_source("start_recording"), "аварийный выход из старта записи"


def test_file_transcription_is_not_touched() -> None:
    """Расшифровка файла блокировкой экрана не прерывается."""
    body = _method_source("_on_session_locked")
    for forbidden in ("file_worker", "cancel_file_transcription", "file_cancel_requested", "FileTranscribeWorker"):
        assert forbidden not in body, f"обработчик блокировки трогает расшифровку файла: {forbidden}"
    assert "self.recorder.is_recording" in body, "нет защиты от остановки, когда запись не идёт"


def _run() -> None:
    tests = [
        test_session_state_struct_matches_windows_layout,
        test_is_session_locked_never_raises,
        test_watcher_is_idle_until_recording_starts,
        test_lock_fires_callback_once,
        test_already_locked_at_start_is_not_an_event,
        test_unlock_then_lock_fires_again,
        test_stop_ends_watching,
        test_system_call_failure_is_treated_as_unlocked,
        test_lock_during_recording_stops_dictation,
        test_lock_without_recording_is_noop,
        test_normal_delivery_still_pastes,
        test_normal_delivery_respects_existing_rules,
        test_lock_delivery_does_not_paste_but_keeps_everything_else,
        test_lock_flag_is_reset_for_the_next_dictation,
        test_watch_starts_only_with_recording,
        test_every_exit_point_stops_watching,
        test_file_transcription_is_not_touched,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("US-069 regression: ALL PASS")


if __name__ == "__main__":
    _run()
