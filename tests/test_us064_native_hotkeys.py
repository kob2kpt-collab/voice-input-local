# -*- coding: utf-8 -*-
r"""Регресс-тесты US-064 (TASK-335): системная регистрация горячей клавиши.

Проверяется то, что нельзя проверить руками без перезапусков и второй машины:

1. Разбор комбинаций — включая клавиши, ради которых заведён TASK-333 (Enter,
   пунктуация, numpad, стрелки, PgUp/PgDn) и символ нелатинской раскладки,
   который должен разрешаться в физический виртуальный код.
2. Отклонение комбинации без модификатора: системная регистрация одиночной
   клавиши отобрала бы её у всех остальных программ.
3. Порядок регистрации: новая комбинация занимается ДО снятия старой, и при
   ошибке прежняя остаётся рабочей (правило проекта).
4. Совместимость сохранённых настроек прошлых версий.

Тест headless: Win32 не вызывается — бэкенд и Qt-приложение подменены.
Запуск (в venv приложения):
    .venv\Scripts\python.exe tests\test_us064_native_hotkeys.py
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app.hotkeys import (  # noqa: E402
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    MOD_WIN,
    VK_CONTROL,
    VK_DELETE,
    VK_ESCAPE,
    VK_F1,
    VK_HOME,
    VK_LEFT,
    VK_LWIN,
    VK_MENU,
    VK_NUMPAD0,
    VK_PRIOR,
    VK_RETURN,
    VK_RWIN,
    VK_SHIFT,
    VK_SPACE,
    WM_HOTKEY,
    HotkeyService,
    parse_hotkey,
)

UI_PATH = REPO_ROOT / "voice_input_app" / "ui.py"


class FakeApplication:
    def __init__(self) -> None:
        self.installed: list[object] = []
        self.removed: list[object] = []

    def installNativeEventFilter(self, event_filter) -> None:  # noqa: ANN001, N802
        self.installed.append(event_filter)

    def removeNativeEventFilter(self, event_filter) -> None:  # noqa: ANN001, N802
        self.removed.append(event_filter)


class FakeBackend:
    """Подмена RegisterHotKey/UnregisterHotKey с журналом вызовов."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.registered: list[tuple[int, object]] = []
        self.unregistered: list[int] = []
        self.fail_next = False

    def register(self, hotkey_id, hotkey) -> None:  # noqa: ANN001
        if self.fail_next:
            self.fail_next = False
            raise OSError(1409, "hotkey already registered")
        self.calls.append(("register", hotkey_id))
        self.registered.append((hotkey_id, hotkey))

    def unregister(self, hotkey_id: int) -> None:
        self.calls.append(("unregister", hotkey_id))
        self.unregistered.append(hotkey_id)


def make_service(on_trigger=lambda: None, **kwargs):  # noqa: ANN001, ANN003
    backend = FakeBackend()
    application = FakeApplication()
    service = HotkeyService(on_trigger, backend=backend, application=application, **kwargs)
    return service, backend, application


def test_parse_basic_combinations() -> None:
    cases = [
        ("Ctrl+Space", "ctrl+space", MOD_CONTROL | MOD_NOREPEAT, VK_SPACE),
        ("Control + Alt + Space", "ctrl+alt+space", MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_SPACE),
        ("Shift+Win+A", "shift+win+a", MOD_SHIFT | MOD_WIN | MOD_NOREPEAT, ord("A")),
        ("Ctrl+Alt+7", "ctrl+alt+7", MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("7")),
        ("Ctrl+F24", "ctrl+f24", MOD_CONTROL | MOD_NOREPEAT, VK_F1 + 23),
    ]
    for display, text, modifiers, virtual_key in cases:
        parsed = parse_hotkey(display)
        assert parsed.text == text, f"{display!r} -> {parsed.text!r}"
        assert parsed.modifiers == modifiers, display
        assert parsed.virtual_key == virtual_key, display
    # MOD_NOREPEAT обязателен: без него удержание давало бы поток срабатываний
    # с частотой автоповтора клавиатуры (для Push-to-Talk это фатально).
    assert parse_hotkey("Ctrl+Space").modifiers & MOD_NOREPEAT


def test_parse_extended_keys() -> None:
    """TASK-333: клавиши, которых не было в исходном варианте из PR #3."""
    cases = {
        "Ctrl+Enter": VK_RETURN,
        "Ctrl+Alt+Home": VK_HOME,
        "Ctrl+Alt+PgUp": VK_PRIOR,
        "Ctrl+Shift+Delete": VK_DELETE,
        "Ctrl+Alt+Left": VK_LEFT,
        "Ctrl+Num0": VK_NUMPAD0,
        "Ctrl+Alt+;": 0xBA,
        "Ctrl+Alt+,": 0xBC,
        "Ctrl+Alt+.": 0xBE,
        "Ctrl+Alt+-": 0xBD,
        "Ctrl++": 0xBB,
    }
    for display, virtual_key in cases.items():
        parsed = parse_hotkey(display)
        assert parsed.virtual_key == virtual_key, f"{display!r} -> 0x{parsed.virtual_key:02X}"


def test_non_latin_key_resolves_to_physical_code() -> None:
    """Символ кириллицы разрешается через VkKeyScan, а не таблицей кодов.

    Резолвер подменён, чтобы тест не зависел от установленных в системе
    раскладок: «ф» набирается на той же физической клавише, что и «a».
    """
    resolved = parse_hotkey("Ctrl+Alt+ф", vk_resolver=lambda ch: ord("A") if ch == "ф" else None)
    assert resolved.virtual_key == ord("A")
    assert resolved.text == "ctrl+alt+ф"

    # Символ, который не разрешился ни таблицей, ни системой, — понятная ошибка.
    try:
        parse_hotkey("Ctrl+Alt+ж", vk_resolver=lambda _ch: None)
    except ValueError:
        pass
    else:
        raise AssertionError("нерезрешимый символ принят как клавиша")


def test_combination_without_modifier_is_rejected() -> None:
    for display in ["space", "a", "f5", "esc"]:
        try:
            parse_hotkey(display)
        except ValueError as exc:
            assert "Ctrl" in str(exc), f"причина отказа не объяснена: {exc}"
        else:
            raise AssertionError(f"комбинация без модификатора принята: {display!r}")


def test_malformed_combinations_are_rejected() -> None:
    for display in ["", "ctrl", "ctrl+", "ctrl+a+b", "ctrl+k,ctrl+c", "ctrl+фыва"]:
        try:
            parse_hotkey(display, vk_resolver=lambda _ch: None)
        except ValueError:
            pass
        else:
            raise AssertionError(f"некорректная комбинация принята: {display!r}")


def test_legacy_saved_hotkeys_still_parse() -> None:
    """Настройки прошлых версий (формат библиотеки keyboard) продолжают работать."""
    legacy = ["ctrl+alt+space", "ctrl+space", "ctrl+shift+d", "alt+q", "ctrl+alt+f9", "win+alt+s"]
    for saved in legacy:
        parsed = parse_hotkey(saved)
        assert parsed.virtual_key, saved
        assert parsed.modifiers & ~MOD_NOREPEAT, saved


def test_watch_groups_cover_every_key_of_combination() -> None:
    """Для Push-to-Talk нужны коды всех клавиш комбинации, и только они."""
    parsed = parse_hotkey("Ctrl+Alt+Space")
    assert parsed.watch_groups == ((VK_CONTROL,), (VK_MENU,), (VK_SPACE,))
    # У Win нет «общего» кода — следим за левой и правой сразу, иначе удержание
    # левой читалось бы как отпускание правой.
    assert parse_hotkey("Win+Shift+S").watch_groups == ((VK_SHIFT,), (VK_LWIN, VK_RWIN), (ord("S"),))


def test_registration_installs_native_filter_and_dispatches() -> None:
    calls: list[str] = []
    service, backend, application = make_service(lambda: calls.append("trigger"))

    service.start("Ctrl+Space")

    assert application.installed == [service], "нативный фильтр событий не установлен"
    hotkey_id, registered = backend.registered[-1]
    assert registered.virtual_key == VK_SPACE
    service._dispatch_hotkey(hotkey_id)
    assert calls == ["trigger"]
    # Чужой идентификатор (горячая клавиша другой программы) игнорируется.
    service._dispatch_hotkey(hotkey_id + 777)
    assert calls == ["trigger"]


def test_new_hotkey_is_registered_before_the_old_one_is_released() -> None:
    """Правило проекта: сначала занять новую, потом снять старую."""
    service, backend, _app = make_service()
    service.start("Ctrl+Space")
    old_id = backend.registered[-1][0]
    backend.calls.clear()

    service.start("Ctrl+Alt+Space")

    new_id = backend.registered[-1][0]
    assert backend.calls == [("register", new_id), ("unregister", old_id)], backend.calls
    assert service.hotkey == "ctrl+alt+space"


def test_failed_registration_keeps_previous_hotkey_working() -> None:
    service, backend, _app = make_service()
    service.start("Ctrl+Space")
    old_id = backend.registered[-1][0]
    backend.fail_next = True

    try:
        service.start("Ctrl+Alt+Space")
    except RuntimeError as exc:
        assert "другую комбинацию" in str(exc)
    else:
        raise AssertionError("ошибка регистрации не доведена до UI")

    assert service.hotkey == "ctrl+space", "сервис забыл рабочую комбинацию"
    assert old_id not in backend.unregistered, "старая комбинация снята при неудачной замене"


def test_repeated_registration_of_same_hotkey_is_noop() -> None:
    """Defensive-вызовы register_hotkey из UI не должны ничего ломать.

    Повторная регистрация той же комбинации новым идентификатором завершилась
    бы ошибкой «уже занята» — её занимает наш же предыдущий идентификатор.
    """
    service, backend, _app = make_service()
    service.start("Ctrl+Space")
    service.start("Control + Space")
    service.start("ctrl+space")

    assert len(backend.registered) == 1
    assert backend.unregistered == []


def test_native_event_filter_reads_wm_hotkey() -> None:
    calls: list[str] = []
    service, backend, _app = make_service(lambda: calls.append("trigger"))
    service.start("Ctrl+Space")
    hotkey_id = backend.registered[-1][0]

    message = wintypes.MSG()
    message.message = WM_HOTKEY
    message.wParam = hotkey_id
    handled = service.nativeEventFilter(b"windows_dispatcher_MSG", ctypes.addressof(message))

    assert handled is False, "сообщение должно продолжать обычный путь обработки"
    assert calls == ["trigger"]

    # Прочие сообщения не трогаем вовсе.
    other = wintypes.MSG()
    other.message = 0x0100  # WM_KEYDOWN
    other.wParam = hotkey_id
    service.nativeEventFilter(b"windows_dispatcher_MSG", ctypes.addressof(other))
    assert calls == ["trigger"]


def test_stop_releases_registration_and_filter() -> None:
    service, backend, application = make_service()
    service.start("Ctrl+Space")
    hotkey_id = backend.registered[-1][0]

    service.stop()

    assert backend.unregistered == [hotkey_id]
    assert application.removed == [service]
    assert service.hotkey == ""


def test_escape_is_never_registered_as_system_hotkey() -> None:
    """US-065: Escape отбирался бы у всех программ, поэтому не регистрируется."""
    service, backend, _app = make_service()
    service.start("Ctrl+Space")
    service.start("Ctrl+Alt+Space", mode="ptt")

    registered_keys = [hotkey.virtual_key for _id, hotkey in backend.registered]
    assert VK_ESCAPE not in registered_keys

    ui_src = UI_PATH.read_text(encoding="utf-8")
    assert "register_cancel" not in ui_src, "в UI осталась регистрация Escape как горячей клавиши"


def test_ui_notifies_when_saved_hotkey_cannot_be_registered() -> None:
    """TASK-334: заметное уведомление, а не строка в статус-баре."""
    src = UI_PATH.read_text(encoding="utf-8")
    assert "def _notify_hotkey_registration_failed(self)" in src
    assert "if not self.register_hotkey(show_errors=False):" in src, "старт не проверяет результат регистрации"
    start = src.index("def _notify_hotkey_registration_failed(self)")
    body = src[start: src.index("\n    def ", start + 10)]
    assert "tray.showMessage" in body, "нет уведомления в трее"
    assert "QMessageBox.warning" in body, "нет модального уведомления"


def _run() -> None:
    tests = [
        test_parse_basic_combinations,
        test_parse_extended_keys,
        test_non_latin_key_resolves_to_physical_code,
        test_combination_without_modifier_is_rejected,
        test_malformed_combinations_are_rejected,
        test_legacy_saved_hotkeys_still_parse,
        test_watch_groups_cover_every_key_of_combination,
        test_registration_installs_native_filter_and_dispatches,
        test_new_hotkey_is_registered_before_the_old_one_is_released,
        test_failed_registration_keeps_previous_hotkey_working,
        test_repeated_registration_of_same_hotkey_is_noop,
        test_native_event_filter_reads_wm_hotkey,
        test_stop_releases_registration_and_filter,
        test_escape_is_never_registered_as_system_hotkey,
        test_ui_notifies_when_saved_hotkey_cannot_be_registered,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("US-064 regression: ALL PASS")


if __name__ == "__main__":
    _run()
