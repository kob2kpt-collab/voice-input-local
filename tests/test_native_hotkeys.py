from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from ctypes import wintypes

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app.hotkeys import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    MOD_WIN,
    VK_ESCAPE,
    VK_F1,
    VK_SPACE,
    WM_HOTKEY,
    HotkeyService,
    parse_hotkey,
)


class FakeApplication:
    def __init__(self) -> None:
        self.installed = []
        self.removed = []

    def installNativeEventFilter(self, event_filter) -> None:  # noqa: ANN001, N802
        self.installed.append(event_filter)

    def removeNativeEventFilter(self, event_filter) -> None:  # noqa: ANN001, N802
        self.removed.append(event_filter)


class FakeBackend:
    def __init__(self) -> None:
        self.registered = []
        self.unregistered = []
        self.fail_next = False

    def register(self, hotkey_id, hotkey) -> None:  # noqa: ANN001
        if self.fail_next:
            self.fail_next = False
            raise OSError(1409, "already registered")
        self.registered.append((hotkey_id, hotkey))

    def unregister(self, hotkey_id: int) -> None:
        self.unregistered.append(hotkey_id)


def make_service(callback):  # noqa: ANN001
    backend = FakeBackend()
    application = FakeApplication()
    service = HotkeyService(callback, backend=backend, application=application)
    return service, backend, application


def test_parse_hotkey() -> None:
    cases = [
        ("Ctrl+Space", "ctrl+space", MOD_CONTROL | MOD_NOREPEAT, VK_SPACE),
        ("Control + Alt + 7", "ctrl+alt+7", MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("7")),
        ("Shift+Win+A", "shift+win+a", MOD_SHIFT | MOD_WIN | MOD_NOREPEAT, ord("A")),
        ("Ctrl+F24", "ctrl+f24", MOD_CONTROL | MOD_NOREPEAT, VK_F1 + 23),
    ]
    for display, text, modifiers, virtual_key in cases:
        parsed = parse_hotkey(display)
        assert parsed.text == text
        assert parsed.modifiers == modifiers
        assert parsed.virtual_key == virtual_key


def test_parse_hotkey_rejects_unsupported_combinations() -> None:
    for display in ["", "space", "ctrl", "ctrl+a+b", "ctrl+k,ctrl+c", "ctrl+enter", "ctrl++"]:
        try:
            parse_hotkey(display)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsupported hotkey was accepted: {display!r}")


def test_service_registers_system_hotkey_and_dispatches_callback() -> None:
    calls = []
    service, backend, application = make_service(lambda: calls.append("main"))

    service.start("Ctrl+Space")
    hotkey_id, registered = backend.registered[-1]

    assert service.hotkey == "ctrl+space"
    assert registered.virtual_key == VK_SPACE
    assert application.installed == [service]

    service._dispatch_hotkey(hotkey_id)
    assert calls == ["main"]


def test_same_hotkey_registration_is_noop() -> None:
    service, backend, _application = make_service(lambda: None)

    service.start("Ctrl+Space")
    service.start("Control + Space")

    assert len(backend.registered) == 1
    assert backend.unregistered == []


def test_failed_replacement_keeps_previous_hotkey_registered() -> None:
    service, backend, _application = make_service(lambda: None)
    service.start("Ctrl+Space")
    previous_id = backend.registered[-1][0]
    backend.fail_next = True

    try:
        service.start("Alt+Space")
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed replacement must raise RuntimeError")

    assert service.hotkey == "ctrl+space"
    assert previous_id not in backend.unregistered


def test_successful_replacement_unregisters_previous_hotkey() -> None:
    service, backend, _application = make_service(lambda: None)
    service.start("Ctrl+Space")
    previous_id = backend.registered[-1][0]

    service.start("Alt+Space")

    assert service.hotkey == "alt+space"
    assert backend.unregistered == [previous_id]


def test_escape_cancel_registration_is_temporary() -> None:
    calls = []
    service, backend, _application = make_service(lambda: calls.append("main"))
    service.start("Ctrl+Space")
    service.register_cancel(lambda: calls.append("cancel"))
    cancel_id, cancel_hotkey = backend.registered[-1]

    assert cancel_hotkey.virtual_key == VK_ESCAPE
    service._dispatch_hotkey(cancel_id)
    assert calls == ["cancel"]

    service.unregister_cancel()
    assert cancel_id in backend.unregistered
    service._dispatch_hotkey(cancel_id)
    assert calls == ["cancel"]


def test_native_event_filter_dispatches_wm_hotkey() -> None:
    calls = []
    service, backend, _application = make_service(lambda: calls.append("main"))
    service.start("Ctrl+Space")
    hotkey_id = backend.registered[-1][0]
    message = wintypes.MSG()
    message.message = WM_HOTKEY
    message.wParam = hotkey_id

    assert service.nativeEventFilter(b"windows_dispatcher_MSG", ctypes.addressof(message)) is False
    assert calls == ["main"]


def test_stop_unregisters_everything_and_removes_event_filter() -> None:
    service, backend, application = make_service(lambda: None)
    service.start("Ctrl+Space")
    main_id = backend.registered[-1][0]
    service.register_cancel(lambda: None)
    cancel_id = backend.registered[-1][0]

    service.stop()

    assert set(backend.unregistered) == {main_id, cancel_id}
    assert application.removed == [service]


def test_runtime_sources_do_not_import_keyboard_hook_library() -> None:
    root = Path(__file__).resolve().parents[1]
    runtime_files = [
        root / "voice_input_app" / "hotkeys.py",
        root / "voice_input_app" / "insert.py",
        root / "voice_input_app" / "ui.py",
        root / "requirements.txt",
        root / "build_exe.bat",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in runtime_files).lower()
    assert "import keyboard" not in combined
    assert "keyboard.add_hotkey" not in combined
    assert "keyboard.hook" not in combined
    assert "hidden-import keyboard" not in combined
    assert "keyboard>=0.13.5" not in combined


def _run() -> None:
    tests = [
        test_parse_hotkey,
        test_parse_hotkey_rejects_unsupported_combinations,
        test_service_registers_system_hotkey_and_dispatches_callback,
        test_same_hotkey_registration_is_noop,
        test_failed_replacement_keeps_previous_hotkey_registered,
        test_successful_replacement_unregisters_previous_hotkey,
        test_escape_cancel_registration_is_temporary,
        test_native_event_filter_dispatches_wm_hotkey,
        test_stop_unregisters_everything_and_removes_event_filter,
        test_runtime_sources_do_not_import_keyboard_hook_library,
    ]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print("Native hotkeys regression: ALL PASS")


if __name__ == "__main__":
    _run()
