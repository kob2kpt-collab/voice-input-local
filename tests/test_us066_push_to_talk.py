# -*- coding: utf-8 -*-
r"""Регресс-тесты US-066 (TASK-339…341): Push-to-Talk без перехвата клавиатуры.

RegisterHotKey сообщает только о срабатывании комбинации и не даёт события
отпускания вовсе. Поэтому режим «зажал — говорю, отпустил — расшифровка»
работает так: старт — по WM_HOTKEY, стоп — по отпусканию, которое ловит
точечное наблюдение ровно за теми клавишами, из которых состоит комбинация
пользователя, и только на время записи.

Проверяется:
1. Старт по срабатыванию комбинации и стоп по отпусканию ЛЮБОЙ её клавиши.
2. В режиме «Переключатель» наблюдение не включается вовсе.
3. Смена режима и комбинации применяется без перезапуска приложения.
4. Блокировка Windows при удержанной комбинации не оставляет запись включённой.
5. В hotkeys.py нет зависимости keyboard и низкоуровневого хука.

Тест headless: Win32 не вызывается — бэкенд, Qt-приложение и состояние клавиш
подменены. Запуск (в venv приложения):
    .venv\Scripts\python.exe tests\test_us066_push_to_talk.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from voice_input_app.hotkeys import (  # noqa: E402
    VK_CONTROL,
    VK_MENU,
    VK_SPACE,
    HotkeyService,
)
from voice_input_app.key_watch import KEY_DOWN_MASK, KeyStateWatcher  # noqa: E402

_APP = QCoreApplication.instance() or QCoreApplication(sys.argv)

HOTKEYS_PATH = REPO_ROOT / "voice_input_app" / "hotkeys.py"
KEY_WATCH_PATH = REPO_ROOT / "voice_input_app" / "key_watch.py"
UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"
CONFIG_PATH = REPO_ROOT / "voice_input_app" / "config.py"

COMBO = "Ctrl+Alt+Space"
COMBO_CODES = (VK_CONTROL, VK_MENU, VK_SPACE)


class FakeApplication:
    def __init__(self) -> None:
        self.installed: list[object] = []
        self.removed: list[object] = []

    def installNativeEventFilter(self, event_filter) -> None:  # noqa: ANN001, N802
        self.installed.append(event_filter)

    def removeNativeEventFilter(self, event_filter) -> None:  # noqa: ANN001, N802
        self.removed.append(event_filter)


class FakeBackend:
    def __init__(self) -> None:
        self.registered: list[tuple[int, object]] = []
        self.unregistered: list[int] = []

    def register(self, hotkey_id, hotkey) -> None:  # noqa: ANN001
        self.registered.append((hotkey_id, hotkey))

    def unregister(self, hotkey_id: int) -> None:
        self.unregistered.append(hotkey_id)


class FakeKeyboard:
    def __init__(self, down: tuple[int, ...] = ()) -> None:
        self.down: set[int] = set(down)
        self.reads: list[int] = []

    def read(self, virtual_key: int) -> int:
        self.reads.append(virtual_key)
        return KEY_DOWN_MASK if virtual_key in self.down else 0


def make_service(keyboard: FakeKeyboard):
    events: list[str] = []
    backend = FakeBackend()
    application = FakeApplication()
    service = HotkeyService(
        lambda: events.append("trigger"),
        on_press=lambda: events.append("press"),
        on_release=lambda: events.append("release"),
        backend=backend,
        application=application,
        watcher=KeyStateWatcher(key_state_fn=keyboard.read),
    )
    return service, backend, events


def _hotkey_id(backend: FakeBackend) -> int:
    return backend.registered[-1][0]


def test_hold_starts_recording_and_release_stops_it() -> None:
    keyboard = FakeKeyboard(COMBO_CODES)
    service, backend, events = make_service(keyboard)
    service.start(COMBO, mode="ptt")

    service._dispatch_hotkey(_hotkey_id(backend))
    assert events == ["press"], events
    assert service.watched_codes == COMBO_CODES, service.watched_codes

    service._watcher._poll()  # комбинация всё ещё удерживается
    assert events == ["press"], "запись остановилась, хотя клавиши не отпускали"

    keyboard.down.discard(VK_SPACE)  # отпустили основную клавишу
    service._watcher._poll()
    assert events == ["press", "release"], events
    assert service.watched_codes == (), "наблюдение осталось включённым после отпускания"


def test_release_of_any_combination_key_stops_recording() -> None:
    for released in COMBO_CODES:
        keyboard = FakeKeyboard(COMBO_CODES)
        service, backend, events = make_service(keyboard)
        service.start(COMBO, mode="ptt")
        service._dispatch_hotkey(_hotkey_id(backend))

        keyboard.down.discard(released)
        service._watcher._poll()

        assert events == ["press", "release"], f"отпускание vk=0x{released:02X}: {events}"


def test_only_combination_keys_are_read() -> None:
    keyboard = FakeKeyboard(COMBO_CODES)
    service, backend, _events = make_service(keyboard)
    service.start(COMBO, mode="ptt")
    service._dispatch_hotkey(_hotkey_id(backend))
    keyboard.reads.clear()

    service._watcher._poll()

    assert set(keyboard.reads) <= set(COMBO_CODES), f"прочитаны посторонние коды: {set(keyboard.reads)}"


def test_repeated_trigger_while_holding_does_not_restart_recording() -> None:
    keyboard = FakeKeyboard(COMBO_CODES)
    service, backend, events = make_service(keyboard)
    service.start(COMBO, mode="ptt")

    service._dispatch_hotkey(_hotkey_id(backend))
    service._dispatch_hotkey(_hotkey_id(backend))

    assert events == ["press"], events


def test_very_short_tap_still_stops_recording() -> None:
    """Комбинацию успели отпустить, пока WM_HOTKEY шёл через очередь сообщений.

    Без допущения «на старте клавиши нажаты» отпускания не случилось бы никогда
    и запись осталась бы висеть. Слишком короткую запись отбрасывает уже
    stop_recording (US-066: тап меньше секунды).
    """
    keyboard = FakeKeyboard()  # ни одна клавиша не удерживается
    service, backend, events = make_service(keyboard)
    service.start(COMBO, mode="ptt")

    service._dispatch_hotkey(_hotkey_id(backend))
    service._watcher._poll()

    assert events == ["press", "release"], events


def test_windows_lock_does_not_leave_recording_stuck() -> None:
    """После блокировки состояние клавиш читается у ОС — «залипнуть» нечему."""
    keyboard = FakeKeyboard(COMBO_CODES)
    service, backend, events = make_service(keyboard)
    service.start(COMBO, mode="ptt")
    service._dispatch_hotkey(_hotkey_id(backend))

    keyboard.down.clear()  # экран заблокирован: удержания больше не видно
    service._watcher._poll()

    assert events == ["press", "release"], events
    assert service.watched_codes == ()


def test_toggle_mode_does_not_watch_keys_at_all() -> None:
    keyboard = FakeKeyboard(COMBO_CODES)
    service, backend, events = make_service(keyboard)
    service.start(COMBO, mode="toggle")

    service._dispatch_hotkey(_hotkey_id(backend))

    assert events == ["trigger"], events
    assert service.watched_codes == (), "в режиме «Переключатель» включилось наблюдение"
    assert keyboard.reads == [], "в режиме «Переключатель» клавиатура читается"


def test_mode_switch_applies_without_reregistration() -> None:
    keyboard = FakeKeyboard(COMBO_CODES)
    service, backend, events = make_service(keyboard)
    service.start(COMBO, mode="toggle")
    registrations = len(backend.registered)

    service.start(COMBO, mode="ptt")
    assert service.mode == "ptt"
    assert len(backend.registered) == registrations, "смена режима перерегистрировала комбинацию"

    service._dispatch_hotkey(_hotkey_id(backend))
    assert events == ["press"], events

    # Обратно в «Переключатель» — наблюдение обязано погаснуть.
    service.start(COMBO, mode="toggle")
    assert service.mode == "toggle"
    assert service.watched_codes == ()
    service._dispatch_hotkey(_hotkey_id(backend))
    assert events == ["press", "trigger"], events


def test_combination_change_keeps_ptt_mode() -> None:
    keyboard = FakeKeyboard((VK_CONTROL, VK_SPACE))
    service, backend, events = make_service(keyboard)
    service.start(COMBO, mode="ptt")
    service.start("Ctrl+Space", mode="ptt")

    assert service.hotkey == "ctrl+space"
    service._dispatch_hotkey(_hotkey_id(backend))
    assert events == ["press"]
    assert service.watched_codes == (VK_CONTROL, VK_SPACE)


def test_stop_releases_watching() -> None:
    keyboard = FakeKeyboard(COMBO_CODES)
    service, backend, _events = make_service(keyboard)
    service.start(COMBO, mode="ptt")
    service._dispatch_hotkey(_hotkey_id(backend))
    assert service.watched_codes == COMBO_CODES

    service.stop()

    assert service.watched_codes == (), "выход из приложения оставил наблюдение включённым"


def test_hotkeys_module_has_no_keyboard_hook_dependency() -> None:
    """TASK-341: зависимость keyboard снята, хук не ставится."""
    sources = "\n".join(path.read_text(encoding="utf-8") for path in (HOTKEYS_PATH, KEY_WATCH_PATH))
    for forbidden in ("import keyboard", "keyboard.hook", "keyboard.add_hotkey", "keyboard.is_pressed"):
        assert forbidden not in sources, f"осталась зависимость от библиотеки keyboard: {forbidden}"
    for forbidden in ("SetWindowsHookEx", "WH_KEYBOARD_LL", "RegisterRawInputDevices"):
        assert forbidden not in sources, f"добавлен перехват клавиатуры: {forbidden}"


def test_ptt_stays_available_in_settings_and_config() -> None:
    """Режим не удалён из настроек: US-026 перенесён, а не отменён."""
    config_src = CONFIG_PATH.read_text(encoding="utf-8")
    assert "hotkey_mode" in config_src, "поле hotkey_mode пропало из настроек"

    ui_src = UI_PATH.read_text(encoding="utf-8")
    assert "def on_hotkey_press(self)" in ui_src
    assert "def on_hotkey_release(self)" in ui_src
    assert '"Зажать и держать (Push-to-Talk)", "ptt"' in ui_src, "выбор режима пропал из настроек"
    assert 'mode=getattr(self.cfg, "hotkey_mode", "toggle")' in ui_src, "режим не передаётся в сервис"


def _run() -> None:
    tests = [
        test_hold_starts_recording_and_release_stops_it,
        test_release_of_any_combination_key_stops_recording,
        test_only_combination_keys_are_read,
        test_repeated_trigger_while_holding_does_not_restart_recording,
        test_very_short_tap_still_stops_recording,
        test_windows_lock_does_not_leave_recording_stuck,
        test_toggle_mode_does_not_watch_keys_at_all,
        test_mode_switch_applies_without_reregistration,
        test_combination_change_keeps_ptt_mode,
        test_stop_releases_watching,
        test_hotkeys_module_has_no_keyboard_hook_dependency,
        test_ptt_stays_available_in_settings_and_config,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("US-066 regression: ALL PASS")


if __name__ == "__main__":
    _run()
